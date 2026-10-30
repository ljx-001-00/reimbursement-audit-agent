"""Policy Q&A engine behind the 校验能手 in-app assistant.

Two speed rules shape this module:

* Retrieval and answer framing are deterministic and take about a millisecond, so
  the UI can always paint a real answer before any network call starts.
* The model is only used to rephrase retrieved clauses into plain language. It is
  asked for a short reply, gets a small prompt, and is bounded by a hard timeout.

Without a model the assistant still answers: a matching clause is quoted, and an
unmatched question gets the general tax/accounting baseline instead of "I don't
know". It never raises.
"""
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from models import ROOT, Policy
from model_settings import (DEFAULT_BASE_URL, DEFAULT_MODEL, is_local_endpoint,
                            model_endpoint, validate_model)

KNOWLEDGE_FILE = ROOT / "data" / "policy_kb.json"
MAX_HISTORY_TURNS = 3          # keeps a small local context window in budget
MAX_QUESTION_CHARS = 400
MAX_ANSWER_CHARS = 900

# A clause needs this much evidence to be quoted outright; between the two values
# it is offered as "the closest match"; below SOFT_SCORE the question is treated
# as uncovered and answered with the general baseline.
MIN_SCORE = 5
STRONG_SCORE = 6
SOFT_SCORE = 3
# A runner-up clause is only kept when it is nearly as relevant as the best one;
# "报销期限" must not drag the archive-retention clause into the answer.
RELEVANCE_FLOOR = 0.65

# Answering speed: a short reply, a short prompt and a hard ceiling on waiting.
LOCAL_TIMEOUT = 55
CLOUD_TIMEOUT = 22
LOCAL_CLAUSES = 2
CLOUD_CLAUSES = 3
LOCAL_CLAUSE_CHARS = 150
CLOUD_CLAUSE_CHARS = 240
LOCAL_HISTORY_TURNS = 1
LOCAL_MAX_TOKENS = 240
CLOUD_MAX_TOKENS = 400
KEEP_ALIVE = "30m"             # Ollama keeps the weights warm between questions

SYSTEM_PROMPT = (
    "你是「校验能手」，报销审核智能体内的财税助手。"
    "只依据【制度参数】【条文片段】回答，不得编造条款、编号、金额或比例；"
    "片段没覆盖时先说明库里没有这条，再给通用财税口径，并建议问财务。"
    "简体中文，120 字以内，多条用短列表，直接回答，不复述问题，不下单据结论。"
)

SUGGESTIONS = (
    "报销期限是多久？",
    "发票抬头和税号有什么要求？",
    "需要准备哪些附件？",
    "差旅住宿标准是多少？",
    "业务招待费能全额扣除吗？",
    "常用的财税法规有哪些？",
)

CHIPS = ("报销时限多久？", "发票怎么开？", "招待费能扣多少？")

GREETING = ("Hi，我是校验能手。\n"
            "报销制度、发票规则、财税法规，都可以直接问我。")

# What an uncovered question still deserves: the three conditions every expense
# has to satisfy before any rule can even apply.
BASELINE = ("① 业务真实发生，凭证与业务对得上；"
            "② 取得合规发票或其他合法凭证；"
            "③ 在制度规定的时限和审批权限内。")

SCOPE_WORDS = set("报票税费额扣差凭证合同付现账审规财金发缴销用支款单")

OUT_OF_SCOPE = ("这个问题不在我的范围里——我只答报销制度、发票规则和财税法规。\n"
                "试试问我：报销时限、发票抬头、附件清单、差旅标准、招待费扣除、进项抵扣。")

# Retrieval reads the same 15 KB file on every keystroke-sized question; keying the
# parse on (mtime, size) keeps edits visible without paying for JSON each time.
_KNOWLEDGE_CACHE = {}
_REPLY_CACHE = {}
_REPLY_CACHE_LIMIT = 48


@dataclass
class Reply:
    text: str
    sources: list[str] = field(default_factory=list)
    mode: str = "off"          # off / local / cloud
    degraded: str = ""         # why the model was bypassed, if it was
    notice: str = ""           # caveat shown under the bubble
    level: str = ""            # strong / soft / none / model
    cached: bool = False


def _normalise(text):
    return re.sub(r"\s+", "", str(text)).lower()


def load_knowledge(path=None):
    """Read the clause file, cached by mtime; a broken file must not break the app."""
    target = Path(path) if path else KNOWLEDGE_FILE
    try:
        stat = target.stat()
    except OSError:
        return {"scope": "", "version": "", "entries": []}
    key = (str(target), stat.st_mtime_ns, stat.st_size)
    cached = _KNOWLEDGE_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        entries = [item for item in payload.get("entries", [])
                   if isinstance(item, dict) and item.get("title") and item.get("content")]
        data = {"scope": str(payload.get("scope") or ""), "version": str(payload.get("version") or ""),
                "entries": entries}
        data["_index"] = [_indexed(entry) for entry in entries]
    except (OSError, ValueError, TypeError):
        return {"scope": "", "version": "", "entries": []}
    if len(_KNOWLEDGE_CACHE) > 4:
        _KNOWLEDGE_CACHE.clear()
    _KNOWLEDGE_CACHE[key] = data
    return data


def score_entry(question, entry):
    """Keyword-weighted scoring; deliberately simple and explainable."""
    text = _normalise(question)
    if not text:
        return 0
    score = 0
    title = _normalise(entry.get("title", ""))
    if title and title in text:
        score += 12
    else:
        overlap = sum(1 for char in set(title) if char in text)
        score += min(6, overlap)
    for keyword in entry.get("keywords", []):
        word = _normalise(keyword)
        if word and word in text:
            score += 4 + min(4, len(word) // 2)
    content = _normalise(entry.get("content", ""))
    hits = sum(1 for keyword in entry.get("keywords", [])
               if _normalise(keyword) and _normalise(keyword) in content and _normalise(keyword) in text)
    return score + min(4, hits)


def retrieve(question, knowledge=None, limit=3, threshold=MIN_SCORE):
    knowledge = knowledge or load_knowledge()
    text = _normalise(question)
    if not text:
        return []
    index = knowledge.get("_index")
    ranked = []
    for position, entry in enumerate(knowledge["entries"]):
        if index and position < len(index):
            score = _score_indexed(text, index[position])
        else:
            score = score_entry(question, entry)
        if score > 0:
            ranked.append((score, entry))
    ranked.sort(key=lambda item: (-item[0], item[1]["title"]))
    if not ranked or ranked[0][0] < threshold:
        return []
    top = ranked[0][0]
    floor = max(threshold, top * RELEVANCE_FLOOR)
    # A single shared character is coincidence, not a match.
    return [entry for score, entry in ranked if score >= floor][:limit]


def _indexed(entry):
    title = entry.get("title", "")
    keywords = [word for word in (_normalise(item) for item in entry.get("keywords", [])) if word]
    content = _normalise(entry.get("content", ""))
    return (_normalise(title), set(title), keywords, content)


def _score_indexed(text, row):
    title, title_chars, keywords, content = row
    score = 0
    if title and title in text:
        score += 12
    else:
        score += min(6, sum(1 for char in title_chars if char in text))
    hits = 0
    for word in keywords:
        if word in text:
            score += 4 + min(4, len(word) // 2)
            if word in content:
                hits += 1
    return score + min(4, hits)


def classify(question, knowledge=None, limit=2):
    """Return (level, clauses): strong / soft / none."""
    knowledge = knowledge or load_knowledge()
    strong = retrieve(question, knowledge, limit=limit, threshold=STRONG_SCORE)
    if strong:
        return "strong", strong
    soft = retrieve(question, knowledge, limit=limit, threshold=SOFT_SCORE)
    if soft:
        return "soft", soft
    return "none", []


def policy_brief(policy=None):
    policy = policy or Policy()
    attachments = "、".join(policy.required_attachments) if policy.required_attachments else "无"
    return (f"公司全称：{policy.company_name}；纳税人识别号：{policy.company_tax_id}；"
            f"报销时限：开票后 {policy.max_days} 个自然日内；必需附件：{attachments}")


def _clip(text, limit):
    text = re.sub(r"[ \t]+", " ", str(text)).strip()
    if len(text) <= limit:
        return text
    return text[:max(1, limit - 1)].rstrip() + "…"


def _blocks(clauses, chars):
    # "**标题**" renders as the accent line on top of the bubble.
    return "\n\n".join(f"**{entry['title']}**\n{_clip(entry['content'], chars)}" for entry in clauses)


def in_scope(question):
    return any(char in SCOPE_WORDS for char in str(question))


def instant_answer(question, policy=None, knowledge=None):
    """Sub-millisecond reply the UI can paint before any model call starts.

    Always returns text: a quoted clause, a closest-match clause, the general
    baseline, or a scope hint. Never empty, never invented.
    """
    question = str(question or "").strip()
    if not question:
        return Reply(text="想问点什么？比如：" + SUGGESTIONS[0], level="none")
    knowledge = knowledge or load_knowledge()
    level, clauses = classify(question, knowledge, limit=2)
    if level == "strong":
        return Reply(text=_blocks(clauses, 300), sources=[entry["title"] for entry in clauses],
                     level="strong")
    if level == "soft":
        picked = clauses[:1]
        return Reply(text="没有完全对口的条款，最接近的是：\n\n" + _blocks(picked, 300),
                     sources=[entry["title"] for entry in picked], level="soft",
                     notice="制度库里没有完全对应的条文，以上为最接近的一条。")
    if not in_scope(question):
        return Reply(text=OUT_OF_SCOPE, level="none")
    return Reply(text=("制度与法规库里没有这一条。任何一笔支出要成立，都要满足三项底线：\n"
                       + BASELINE + "\n\n具体口径请咨询财务，或者换个说法再问我。"),
                 level="none", notice="未匹配到条文，以上为通用财税口径。")


def build_messages(question, knowledge=None, policy=None, history=None, limit=3,
                   clause_chars=260, history_turns=MAX_HISTORY_TURNS):
    knowledge = knowledge or load_knowledge()
    policy = policy or Policy()
    picked = retrieve(question, knowledge, limit=limit)
    clauses = "\n".join(f"〔{entry['title']}〕{_clip(entry['content'], clause_chars)}" for entry in picked)
    system = (SYSTEM_PROMPT
              + "\n\n【制度参数】" + policy_brief(policy)
              + ("\n\n【条文片段】\n" + clauses if clauses else "\n\n【条文片段】无匹配条目"))
    messages = [{"role": "system", "content": system}]
    for item in (history or [])[-history_turns * 2:]:
        role = "assistant" if item.get("role") == "assistant" else "user"
        messages.append({"role": role, "content": _clip(item.get("content", ""), 200)})
    messages.append({"role": "user", "content": _clip(question, MAX_QUESTION_CHARS)})
    return messages, picked


def offline_answer(question, knowledge=None, policy=None, limit=1):
    """Compatibility wrapper around :func:`instant_answer`."""
    reply = instant_answer(question, policy, knowledge)
    reply.degraded = "纯规则模式 · 未调用模型"
    return reply


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def _post(endpoint, payload, headers, timeout):
    request = urllib.request.Request(endpoint, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                     headers=headers, method="POST")
    # Never forward credentials through a redirect.
    with urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
        body = response.read(400_001)
    if len(body) > 400_000:
        raise ValueError("模型响应超过限制")
    message = json.loads(body)["choices"][0]["message"]
    if not isinstance(message, dict):
        raise ValueError("模型响应格式不正确")
    return message


def chat(messages, api_key="", *, base_url=DEFAULT_BASE_URL, model=DEFAULT_MODEL,
         timeout=None, max_tokens=CLOUD_MAX_TOKENS):
    """Minimal chat call tuned for latency: no tools, small output, keep_alive."""
    endpoint = model_endpoint(base_url)
    model = validate_model(model)
    local = is_local_endpoint(endpoint)
    if timeout is None:
        timeout = LOCAL_TIMEOUT if local else CLOUD_TIMEOUT
    key = api_key.strip()
    if key and not endpoint.lower().startswith("https://") and not local:
        raise ValueError("远程模型携带密钥时须使用 HTTPS 地址")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    payload = {"model": model, "messages": messages, "max_tokens": max_tokens,
               "temperature": 0.2, "stream": False}
    if local:
        payload["keep_alive"] = KEEP_ALIVE
    try:
        return _post(endpoint, payload, headers, timeout)
    except urllib.error.HTTPError as exc:
        # Servers that predate keep_alive reject the field; retry once without it.
        if local and exc.code in (400, 422):
            payload.pop("keep_alive", None)
            return _post(endpoint, payload, headers, timeout)
        raise


def _strip_list_markers(text):
    return re.sub(r"(?m)^\s*\d+\s*[.、)．]\s*", "", text)


def ungrounded_numbers(answer, context):
    """Numbers the model introduced that never appeared in what we handed it."""
    allowed = set(re.findall(r"\d+(?:\.\d+)?", context))
    found = set(re.findall(r"\d+(?:\.\d+)?", _strip_list_markers(answer)))
    return sorted(number for number in found if number not in allowed)


def _cache_key(question, base_url, model, policy):
    # The endpoint is part of the key: two providers can expose the same model name.
    return (question, base_url, model, getattr(policy, "max_days", None),
            tuple(getattr(policy, "required_attachments", ()) or ()))


def ask(question, *, history=None, policy=None, base_url=DEFAULT_BASE_URL, model=DEFAULT_MODEL,
        api_key="", enabled=False, timeout=None):
    """One turn of the conversation. Never raises: the widget always gets a reply."""
    question = str(question or "").strip()
    if not question:
        return Reply(text="想问点什么？比如：" + SUGGESTIONS[0], level="none")
    if len(question) > MAX_QUESTION_CHARS:
        question = question[:MAX_QUESTION_CHARS]
    knowledge = load_knowledge()
    policy = policy or Policy()

    if not enabled:
        return offline_answer(question, knowledge, policy)

    try:
        local = is_local_endpoint(model_endpoint(base_url))
    except ValueError:
        local = False
    key = _cache_key(question, base_url, model, policy)
    hit = _REPLY_CACHE.get(key)
    if hit is not None:
        return Reply(text=hit[0], sources=list(hit[1]), mode="local" if local else "cloud",
                     notice=hit[2], level="model", cached=True)

    limit = LOCAL_CLAUSES if local else CLOUD_CLAUSES
    turns = LOCAL_HISTORY_TURNS if local else MAX_HISTORY_TURNS
    messages, pinned = build_messages(
        question, knowledge, policy, history, limit=limit, history_turns=turns,
        clause_chars=LOCAL_CLAUSE_CHARS if local else CLOUD_CLAUSE_CHARS)
    context = "\n".join(message["content"] for message in messages)
    try:
        message = chat(messages, api_key, base_url=base_url, model=model, timeout=timeout,
                       max_tokens=LOCAL_MAX_TOKENS if local else CLOUD_MAX_TOKENS)
        text = message.get("content")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("模型没有返回文字")
    except Exception as exc:  # network, quota, bad config: fall back to clauses
        reply = instant_answer(question, policy, knowledge)
        reply.degraded = f"模型调用失败（{type(exc).__name__}）· 已改为引用条文"
        return reply

    text = text.strip()[:MAX_ANSWER_CHARS]
    notice = ""
    missing = ungrounded_numbers(text, context)
    if missing:
        notice = "回答中出现制度未载明的数字（" + "、".join(missing[:3]) + "），请以制度原文为准。"
    reply = Reply(text=text, sources=[entry["title"] for entry in pinned],
                  mode="local" if local else "cloud", notice=notice, level="model")
    if len(_REPLY_CACHE) >= _REPLY_CACHE_LIMIT:
        _REPLY_CACHE.clear()
    _REPLY_CACHE[key] = (reply.text, tuple(reply.sources), reply.notice)
    return reply


def provider_is_local(base_url):
    try:
        return is_local_endpoint(model_endpoint(base_url))
    except Exception:
        return False


def warm(base_url=DEFAULT_BASE_URL, model=DEFAULT_MODEL, api_key="", timeout=90):
    """Load a local model into memory before the user finishes typing.

    Only worth doing for a local endpoint: hosted APIs have no cold start, and
    a stray request there would cost money for nothing.
    """
    if not provider_is_local(base_url):
        return False
    try:
        chat([{"role": "user", "content": "hi"}], api_key, base_url=base_url, model=model,
             timeout=timeout, max_tokens=1)
        return True
    except Exception:
        return False


def greeting(policy=None):
    policy = policy or Policy()
    return (GREETING + f"\n当前制度：{policy.company_name} · 开票后 {policy.max_days} 天内提交 · "
            f"必需附件 {len(policy.required_attachments)} 类")
