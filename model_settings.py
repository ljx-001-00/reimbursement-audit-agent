"""Model addresses and non-secret user profiles, independent of project data."""
import ipaddress
import json
import os
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
DEFAULT_MODEL = "glm-4.7-flash"
DEFAULT_PROFILE = {"name": "智谱默认", "base_url": DEFAULT_BASE_URL, "model": DEFAULT_MODEL}

# Ollama exposes an OpenAI-compatible endpoint under /v1 and its own API under /api.
DEFAULT_LOCAL_BASE_URL = "http://127.0.0.1:11434/v1"
LOCAL_SERVICE_HINT = "启动 Ollama 后会自动列出本机已安装的模型。"
KIND_LABELS = {"local": "本地模型", "cloud": "在线模型", "off": "规则引擎"}


def model_endpoint(base_url):
    value = str(base_url).strip().rstrip("/")
    if len(value) > 2000 or any(char.isspace() or ord(char) < 32 for char in value):
        raise ValueError("接口地址格式不正确")
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme in ("http", "https") and parsed.hostname and
                 parsed.username is None and parsed.password is None and not parsed.query and not parsed.fragment)
        parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("接口地址须为 HTTP/HTTPS 地址，不能包含账号、密钥、查询参数或片段")
    return value if parsed.path.endswith("/chat/completions") else value + "/chat/completions"


def validate_model(model):
    value = str(model).strip()
    if not value or len(value) > 200 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("请填写有效的模型名称（不超过 200 个字符）")
    return value


def is_local_endpoint(endpoint):
    host = urlsplit(endpoint).hostname
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def provider_kind(base_url):
    """Classify an endpoint as a loopback model service or a remote provider."""
    try:
        return "local" if is_local_endpoint(model_endpoint(base_url)) else "cloud"
    except ValueError:
        return "cloud"


def provider_label(base_url):
    return KIND_LABELS[provider_kind(base_url)]


def local_service_root(base_url=DEFAULT_LOCAL_BASE_URL):
    """Return scheme://host:port for a loopback service; refuse remote probes."""
    value = str(base_url).strip().rstrip("/")
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("接口地址格式不正确")
    root = f"{parsed.scheme}://{parsed.netloc}"
    if not is_local_endpoint(root + "/"):
        raise ValueError("只能探测本机运行的模型服务")
    return root


def _no_proxy_opener():
    # Never route a loopback probe through the user's HTTP proxy.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def discover_local_models(base_url=DEFAULT_LOCAL_BASE_URL, timeout=2.0):
    """Return locally installed models (currently Ollama) without touching the network."""
    root = local_service_root(base_url)
    request = urllib.request.Request(root + "/api/tags", headers={"Accept": "application/json"})
    with _no_proxy_opener().open(request, timeout=timeout) as response:
        payload = json.loads(response.read(4_000_001).decode("utf-8", "replace"))
    models = []
    for item in payload.get("models", []):
        name = str(item.get("name") or item.get("model") or "").strip()
        if not name:
            continue
        details = item.get("details") or {}
        size = item.get("size") or 0
        models.append({
            "name": name,
            "size": f"{size / 1024 ** 3:.1f} GB" if size else "",
            "parameters": str(details.get("parameter_size") or ""),
            "tools": "tools" in (item.get("capabilities") or ["tools"]),
            "base_url": base_url,
        })
    models.sort(key=lambda item: item["name"])
    return models


def probe_local_service(base_url=DEFAULT_LOCAL_BASE_URL, timeout=1.5):
    """Return (models, message) so the UI can explain why nothing was found."""
    try:
        models = discover_local_models(base_url, timeout=timeout)
    except ValueError as exc:
        return [], str(exc)
    except Exception:
        return [], "未检测到本机模型服务，请先启动 Ollama（默认 127.0.0.1:11434）"
    if not models:
        return [], "服务已连接，但还没有安装模型"
    return models, f"已发现 {len(models)} 个本机模型"


def profile_path():
    folder = Path(os.environ["APPDATA"]) if os.environ.get("APPDATA") else Path.home() / ".config"
    return folder / "ReimbursementAudit" / "model_profiles.json"


def _clean_profile(profile):
    name = str(profile["name"]).strip()
    if not name or len(name) > 80 or any(ord(char) < 32 for char in name):
        raise ValueError("配置名称须为 1 至 80 个字符")
    base_url = str(profile["base_url"]).strip().rstrip("/")
    model_endpoint(base_url)
    return {"name": name, "base_url": base_url, "model": validate_model(profile["model"])}


def load_profiles():
    try:
        payload = json.loads(profile_path().read_text(encoding="utf-8"))
        profiles = [_clean_profile(item) for item in payload["profiles"]]
        names = [item["name"] for item in profiles]
        if not profiles or len(profiles) > 50 or len(set(names)) != len(names):
            raise ValueError("模型配置列表无效")
        return profiles, payload["active"] if payload.get("active") in names else names[0]
    except (OSError, ValueError, TypeError, KeyError):
        return [dict(DEFAULT_PROFILE)], DEFAULT_PROFILE["name"]


def save_profiles(profiles, active):
    clean = [_clean_profile(item) for item in profiles]
    names = [item["name"] for item in clean]
    if not clean or len(clean) > 50 or len(set(names)) != len(names) or active not in names:
        raise ValueError("模型配置列表无效")
    path = profile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"active": active, "profiles": clean}, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
