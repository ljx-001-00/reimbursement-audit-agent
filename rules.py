"""Deterministic reimbursement checks; model output never changes the verdict."""
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from pathlib import Path
import re
import unicodedata

from models import Application, Check, Policy, PASS, FAIL, REVIEW, NA, FIELD_LABELS

CENT = Decimal("0.01")
DIGITS = "零壹贰叁肆伍陆柒捌玖"
CN_MAP = str.maketrans("貳參陸萬億圓圆正〇一二三四五六七八九两", "贰叁陆万亿元元整零壹贰叁肆伍陆柒捌玖贰")


def _text(value):
    return unicodedata.normalize("NFKC", str("" if value is None else value)).strip()


def _number(value):
    value = re.sub(r"[¥￥,，\s]", "", _text(value)).removesuffix("元")
    try:
        number = Decimal(value)
        # Guard unbounded exponents from corrupt OCR/user input before arithmetic.
        if not number.is_finite() or number < 0 or number.adjusted() > 24:
            return None
        return number
    except InvalidOperation:
        return None


def _cent(value):
    with localcontext() as ctx:
        ctx.prec = 64
        return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _rate(value):
    value = _text(value)
    if value in ("免税", "不征税", "零税率"):
        return Decimal(0)
    number = _number(value.removesuffix("%"))
    if number is None:
        return None
    number = number / 100 if "%" in value or number > 1 else number
    return number if number <= 1 else None


def _date(value):
    value = _text(value).replace("年", "-").replace("月", "-").removesuffix("日")
    match = re.fullmatch(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", value)
    try:
        return date(*map(int, match.groups())) if match else None
    except ValueError:
        return None


def parse_chinese_amount(value):
    """Parse financial Chinese RMB; malformed/negative amounts raise ValueError."""
    value = re.sub(r"\s", "", _text(value).translate(CN_MAP))
    for prefix in ("人民币", "(大写)", "大写:", "大写"):
        value = value.removeprefix(prefix)
    value = value.lstrip(":")
    if not value or any(c not in DIGITS + "拾佰仟万亿元角分整" for c in value):
        raise ValueError("无效的人民币大写金额")
    if value.count("元") != 1:
        raise ValueError("大写金额须包含元")
    integer, fraction = value.split("元")
    if not integer:
        raise ValueError("缺少元整数部分")
    small = {"拾": 10, "佰": 100, "仟": 1000}

    def parse_section(text):
        section = digit = 0
        previous_digit = False
        last_unit = 10000
        for char in text:
            if char in DIGITS:
                new_digit = DIGITS.index(char)
                if previous_digit and digit and new_digit:
                    raise ValueError("大写数字缺少单位")
                digit, previous_digit = new_digit, True
            elif char in small:
                unit = small[char]
                if unit >= last_unit or (previous_digit and digit == 0):
                    raise ValueError("大写数字单位顺序错误")
                section += (digit or 1) * unit
                digit, previous_digit, last_unit = 0, False, unit
            else:
                raise ValueError("大写金额单位错误")
        return section + digit

    parts = re.split(r"(万亿|亿|万)", integer)
    total, last_big = 0, 10000000000000000
    big = {"万": 10000, "亿": 100000000, "万亿": 1000000000000}
    for index in range(0, len(parts) - 1, 2):
        unit = big[parts[index + 1]]
        if not parts[index] or unit >= last_big:
            raise ValueError("大写数字单位顺序错误")
        total += parse_section(parts[index]) * unit
        last_big = unit
    total += parse_section(parts[-1])
    match = re.fullmatch(r"(?:([零壹贰叁肆伍陆柒捌玖])角)?(?:零?([零壹贰叁肆伍陆柒捌玖])分)?(?:整)?", fraction)
    if not match or not fraction:
        raise ValueError("角分格式错误")
    jiao, fen = (DIGITS.index(c) if c else 0 for c in match.groups())
    return Decimal(total) + Decimal(jiao) / 10 + Decimal(fen) / 100


def amount_in_words(value):
    """Return financial Chinese RMB rounded to cents, for test/demo documents."""
    number = _number(value)
    if number is None:
        raise ValueError("金额须为非负有限数字")
    cents = int(_cent(number) * 100)
    integer, fraction = divmod(cents, 100)
    if integer >= 10000000000000000:
        raise ValueError("大写转换支持金额小于一亿亿元")

    def group(number):
        result, zero = "", False
        for place, unit in ((1000, "仟"), (100, "佰"), (10, "拾"), (1, "")):
            digit, number = divmod(number, place)
            if digit:
                result += ("零" if zero and result else "") + DIGITS[digit] + unit
                zero = False
            elif result:
                zero = True
        return result

    parts = []
    while integer:
        integer, part = divmod(integer, 10000)
        parts.append(part)
    result, zero = "", False
    for index in range(len(parts) - 1, -1, -1):
        part = parts[index]
        if part:
            if result and (zero or part < 1000):
                result += "零"
            result += group(part) + ("", "万", "亿", "万亿")[index]
            zero = False
        else:
            zero = True
    result = (result or "零") + "元"
    jiao, fen = divmod(fraction, 10)
    if jiao:
        result += DIGITS[jiao] + "角"
    if fen:
        result += ("零" if not jiao else "") + DIGITS[fen] + "分"
    return result + ("整" if not fen else "")


def audit(app: Application, policy: Policy) -> list[Check]:
    with localcontext() as ctx:
        ctx.prec = 64
        return _audit(app, policy)


def _audit(app: Application, policy: Policy) -> list[Check]:
    checks = []

    def add(code, title, status, actual="", expected="", reason="", source="申请汇总", field=""):
        checks.append(Check(code, title, status, str(actual), str(expected), reason, source, field))

    submitted = _date(app.submitted_on)
    requested = _number(app.requested_amount)
    for key, title, valid in (("applicant", "申请人", bool(_text(app.applicant))),
                              ("department", "部门", bool(_text(app.department))),
                              ("submitted_on", "提交日期", submitted is not None),
                              ("requested_amount", "申请金额", requested is not None)):
        add("required", title + "有效性", PASS if valid else FAIL, getattr(app, key),
            "有效日期 YYYY-MM-DD" if key == "submitted_on" else "非负有限数字" if key == "requested_amount" else "不能为空",
            "申请表由用户填写，缺失或无效时须补正" if not valid else "", field=key)

    try:
        max_days = int(policy.max_days)
        policy_valid = max_days >= 0 and bool(_text(policy.company_name)) and bool(_text(policy.company_tax_id))
    except (ValueError, TypeError, OverflowError):
        max_days, policy_valid = 90, False
    if not policy_valid:
        add("policy", "审核制度配置", REVIEW, "配置不完整或期限无效", "公司名称、税号非空且期限为非负整数")

    unparsed_pages = False
    known_categories = {"发票", "报销申请", "付款凭证", "其他"}
    for document in app.documents:
        if document.category not in known_categories:
            # 这条待复核必须自带原因：报告里若只有状态没有解释，读的人无从判断要做什么。
            add("attachments", "附件类别待确认", REVIEW, document.path, "人工确认附件类别",
                "该文件未自动识别为发票、报销申请或付款凭证，需先人工指定类别，才能判定必需附件是否齐全",
                source=document.path, field="attachments")
        if document.error:
            # 原因要能自己站住：报告与界面详情只看 reason，"实际值"里那行异常文本不算解释。
            add("document", "文件处理失败", REVIEW, document.error, "重新导入或人工核对原件",
                "该文件未能解析，可能是损坏、加密或不是单据，需重新导入或人工核对原件",
                source=document.path)
        if document.category == "发票" and not document.invoices:
            add("required", "发票尚未识别", REVIEW, document.path, "至少一张已提取发票",
                "该文件按发票归类，但没有提取到任何发票，不能据此判定金额与抬头",
                source=document.path)
        if document.category == "发票" and document.pages:
            missing_pages = set(range(1, document.pages + 1)) - {inv.page for inv in document.invoices}
            if missing_pages:
                pages_confirmed = any("pages" in inv.confirmed for inv in document.invoices)
                unparsed_pages = unparsed_pages or not pages_confirmed
                add("document", "全部页面覆盖", NA if pages_confirmed else REVIEW,
                    "未提取为发票的页：" + "、".join(map(str, sorted(missing_pages))),
                    "拆分附件，或人工核对并确认全部页面与明细", source=document.path, field="pages")

    unique_totals, seen, incomplete_total = [], {}, False
    for invoice in app.invoices:
        fields = invoice.fields
        source = f"{Path(invoice.source).name} 第{invoice.page}页"

        def field_value(key):
            return invoice.kind if key == "kind" else fields.get(key, "")

        def uncertain(keys):
            return any(k in invoice.uncertain and k not in invoice.confirmed for k in keys)

        def check(code, title, status, actual="", expected="", reason="", keys=()):
            if status != NA and uncertain(keys):
                status = REVIEW
                reason = (reason + "；" if reason else "") + "相关识别字段尚未人工确认"
            field_key = keys[0] if keys else ""
            if status in (FAIL, REVIEW):
                field_key = next((key for key in keys if key in required and not _text(field_value(key))),
                                 next((key for key in keys if uncertain((key,))), field_key))
            add(code, title, status, actual, expected, reason, source, field_key)

        def missing(keys):
            absent = [k for k in keys if not _text(field_value(k))]
            if absent:
                return FAIL if any(k in invoice.confirmed for k in absent) else REVIEW
            return None

        required = ("kind", "invoice_number", "invoice_date", "buyer_name", "buyer_tax_id",
                    "seller_name", "seller_tax_id", "amount", "tax_amount", "total", "uppercase_total")
        for key in required:
            absent = missing((key,))
            if absent or uncertain((key,)) or (key == "kind" and invoice.kind == "未知"):
                check("required", "必填字段完整性", absent or REVIEW, field_value(key), FIELD_LABELS.get(key, key) + " 已可靠识别或确认",
                      "已确认字段为空" if absent == FAIL else "字段缺失、未知或尚待确认", (key,))
        for key in sorted(set(invoice.uncertain) - set(invoice.confirmed) - set(required)):
            check("required", "识别字段待确认", REVIEW, field_value(key), "人工核对原票后确认", keys=(key,))

        number = re.sub(r"\s", "", _text(fields.get("invoice_number")))
        code = re.sub(r"\s", "", _text(fields.get("invoice_code")))
        number_keys = ("invoice_number", "invoice_code", "kind")
        known_kind = any(word in invoice.kind for word in ("普通", "专用", "数电", "全电"))
        electronic = any(word in invoice.kind for word in ("电子", "数电", "全电"))
        if missing(("invoice_number",)):
            number_status, expected = missing(("invoice_number",)), "发票号码不能为空"
            number_reason = "原票未识别到发票号码，无法唯一确定票据"
        elif not known_kind:
            number_status, expected = REVIEW, "确认发票类型后按对应格式校验"
            number_keys = ("kind", "invoice_number", "invoice_code")
            number_reason = f"发票类型“{invoice.kind}”无法判定，不能确定号码应采用的位数规则"
        elif code:
            number_status = PASS if re.fullmatch(r"\d{8}", number) and re.fullmatch(r"\d{10}|\d{12}", code) else FAIL
            expected = "传统发票：8位号码，10或12位代码"
            number_reason = "" if number_status == PASS else (
                f"号码“{number}”为 {len(number)} 位、代码“{code}”为 {len(code)} 位，"
                "传统发票须为 8 位号码配 10 或 12 位代码")
            if re.fullmatch(r"\d{8}", number) and not re.fullmatch(r"\d{10}|\d{12}", code):
                number_keys = ("invoice_code", "invoice_number", "kind")
        elif electronic:
            number_status = PASS if re.fullmatch(r"\d{20}", number) else FAIL
            expected = "无代码电子发票：20位号码"
            number_reason = "" if number_status == PASS else (
                f"号码“{number}”为 {len(number)} 位，无代码电子发票的号码须为 20 位")
        else:
            number_status, expected = (FAIL if "invoice_code" in invoice.confirmed else REVIEW), "传统发票须提供代码"
            number_keys = ("invoice_code", "invoice_number", "kind")
            number_reason = ("已确认该票无发票代码，但发票类型不属于无代码电子发票，无法按传统发票规则校验"
                             if number_status == FAIL else "原票缺少发票代码，请确认该票是否属于无代码电子发票")
        check("invoice_number", "发票号码与代码格式", number_status, f"号码 {number}；代码 {code or '无'}", expected,
              number_reason, keys=number_keys)

        duplicate = False
        if number:
            identity = (code, number)
            duplicate = identity in seen
            if duplicate:
                first_source, first_uncertain = seen[identity]
                check("duplicate", "重复发票", REVIEW if first_uncertain or uncertain(("invoice_number", "invoice_code")) else FAIL,
                      number, "每张发票只能报销一次", "与 " + first_source + " 重复；汇总金额不重复累加",
                      ("invoice_number", "invoice_code"))
            else:
                seen[identity] = source, uncertain(("invoice_number", "invoice_code"))

        for key, wanted, title in (("buyer_name", policy.company_name, "购买方抬头"),
                                   ("buyer_tax_id", policy.company_tax_id, "购买方税号")):
            actual = re.sub(r"\s", "", _text(fields.get(key))).upper()
            target = re.sub(r"\s", "", _text(wanted)).upper()
            present = fields.get(key, "")
            if not policy_valid:
                buyer_status, buyer_reason = REVIEW, "审核制度未配置公司名称或税号，无法比对购买方信息"
            elif missing((key,)):
                buyer_status = missing((key,))
                buyer_reason = (f"{title}已确认为空，无法证明票据抬头与报销主体一致"
                                if buyer_status == FAIL else f"原票未识别到{title}，需人工核对原票后确认")
            elif actual == target:
                buyer_status, buyer_reason = PASS, ""
            else:
                buyer_status = FAIL
                buyer_reason = f"{title}“{present}”与制度要求的“{wanted}”不一致，该票据抬头与报销主体不符"
            check(key, title, buyer_status, present, wanted, buyer_reason, keys=(key,))
        seller_missing = missing(("seller_name", "seller_tax_id"))
        seller_tax = re.sub(r"\s", "", _text(fields.get("seller_tax_id"))).upper()
        seller_valid = bool(re.fullmatch(r"[0-9A-Z]{15}|[0-9A-Z]{18}|[0-9A-Z]{20}", seller_tax))
        seller_keys = ("seller_tax_id", "seller_name") if not seller_valid else ("seller_name", "seller_tax_id")
        if seller_missing:
            seller_status = seller_missing
            seller_absent = "、".join(FIELD_LABELS.get(k, k) for k in ("seller_name", "seller_tax_id")
                                     if not _text(fields.get(k)))
            seller_reason = (f"销售方{seller_absent}已确认为空，无法确认开票主体"
                             if seller_status == FAIL else f"销售方{seller_absent}尚未识别，需人工核对原票")
        elif seller_valid:
            seller_status, seller_reason = PASS, ""
        else:
            seller_status = FAIL
            seller_reason = (f"销售方税号“{seller_tax}”为 {len(seller_tax)} 位，"
                             "不符合 15、18 或 20 位字母数字的编码规则")
        check("seller", "销售方信息", seller_status,
              f"{fields.get('seller_name', '')} / {seller_tax}", "名称非空，税号为15、18或20位字母数字",
              seller_reason, keys=seller_keys)

        issued = _date(fields.get("invoice_date"))
        delta = (submitted - issued).days if submitted and issued else None
        absent_date = missing(("invoice_date",))
        if absent_date:
            status = absent_date
            date_reason = (f"开票日期已确认为空，无法判定该票据是否在报销期限内"
                           if status == FAIL else "原票未识别到开票日期，需人工核对原件后确认")
        elif submitted is None:
            status, date_reason = REVIEW, "报销提交日期缺失或无效，无法计算报销期限"
        elif not policy_valid:
            status, date_reason = REVIEW, "审核制度未配置有效期，无法判断报销期限"
        elif issued is None:
            status = FAIL
            date_reason = f"开票日期“{fields.get('invoice_date', '')}”不是可识别的日期，无法判定是否在报销期限内"
        elif delta < 0:
            status = FAIL
            date_reason = f"开票日期 {issued} 晚于报销提交日期 {submitted}，票据开出时间在报销之后"
        elif delta > max_days:
            status = FAIL
            date_reason = f"开票日期 {issued} 距提交日期 {submitted} 已 {delta} 天，超过制度规定的 {max_days} 天"
        else:
            status, date_reason = PASS, ""
        # Compact wording: the results table column is narrow, and the full dates are
        # still spelled out in 期望值 and in the reason text below.
        check("date", "报销期限", status,
              f"开票 {fields.get('invoice_date', '')} → 提交 {app.submitted_on}，"
              f"{delta if delta is not None else '间隔未知'} 天",
              f"间隔 0 至 {max_days} 天（含边界）", date_reason, keys=("invoice_date",))

        amount, tax, total = (_number(fields.get(k)) for k in ("amount", "tax_amount", "total"))
        for key, numeric in (("amount", amount), ("tax_amount", tax), ("total", total)):
            if numeric is None and not missing((key,)):
                check("required", "金额字段有效性", FAIL, fields.get(key, ""), "非负有限数字",
                      f"{FIELD_LABELS.get(key, key)}“{fields.get(key, '')}”不是有效的非负数字，须核对原票后更正",
                      keys=(key,))
        arithmetic_status = missing(("amount", "tax_amount", "total")) or REVIEW
        if all(v is not None for v in (amount, tax, total)):
            arithmetic_status = PASS if _cent(amount) + _cent(tax) == _cent(total) else FAIL
        if arithmetic_status == FAIL and all(v is not None for v in (amount, tax, total)):
            difference = _cent(total) - _cent(amount) - _cent(tax)
            arithmetic_reason = (f"价税合计 {_cent(total)} 元，但金额 {_cent(amount)} + 税额 {_cent(tax)} = "
                                 f"{_cent(amount) + _cent(tax)} 元，相差 {abs(difference)} 元，等式不成立")
        elif arithmetic_status == FAIL:
            arithmetic_reason = "金额、税额或价税合计已确认为空，无法核对加总等式"
        elif arithmetic_status == REVIEW:
            arithmetic_reason = "金额、税额或价税合计未全部识别，无法核对加总等式"
        else:
            arithmetic_reason = ""
        check("total", "金额加税额等于价税合计", arithmetic_status,
              f"{fields.get('amount', '')} + {fields.get('tax_amount', '')} = {fields.get('total', '')}",
              str(_cent(amount) + _cent(tax)) if amount is not None and tax is not None else "有效金额 + 有效税额",
              arithmetic_reason, keys=("total", "amount", "tax_amount"))

        line_amounts, line_taxes = [], []
        for index, item in enumerate(invoice.items, 1):
            prefix = f"第{index}行 "
            quantity = _number(item.get("quantity"))
            price = _number(item.get("unit_price", item.get("price")))
            line_amount = _number(item.get("amount"))
            line_tax = _number(item.get("tax_amount", item.get("tax")))
            rate_value = item.get("tax_rate", item.get("rate", fields.get("tax_rate", "")))
            rate = _rate(rate_value)
            quantity_status = NA if not item.get("quantity") and not item.get("unit_price", item.get("price")) else REVIEW
            quantity_reason = "未提供数量与单价，无法核算行金额"
            if quantity is not None and price is not None and line_amount is not None:
                quantity_status = PASS if _cent(quantity * price) == _cent(line_amount) else FAIL
                quantity_reason = "" if quantity_status == PASS else (
                    f"{quantity} × {price} = {_cent(quantity * price)} 元，与行金额 {_cent(line_amount)} 元不一致")
            invalid = [FIELD_LABELS.get(k, k) for k, v in
                       (("quantity", item.get("quantity")),
                        ("unit_price", item.get("unit_price", item.get("price"))),
                        ("amount", item.get("amount"))) if _text(v) and _number(v) is None]
            if invalid:
                quantity_status = FAIL
                quantity_reason = "、".join(invalid) + "不是有效数字，无法核算行金额"
            if quantity_status == NA:
                quantity_reason = ""
            check("quantity", prefix + "数量乘单价", quantity_status,
                  f"{item.get('quantity', '')} × {item.get('unit_price', item.get('price', ''))}；金额 {item.get('amount', '')}",
                  str(_cent(quantity * price)) if quantity is not None and price is not None else "数量 × 单价 = 行金额（均无数量单价时不适用）",
                  quantity_reason, keys=("items", "quantity", "unit_price", "amount"))
            tax_status = REVIEW
            expected_tax = _cent(_cent(line_amount) * rate) if line_amount is not None and rate is not None else None
            tax_reason = "行金额、税率或税额未识别完整，无法核算本行税额"
            if expected_tax is not None and line_tax is not None:
                tax_status = PASS if abs(_cent(line_tax) - expected_tax) <= CENT else FAIL
                tax_reason = "" if tax_status == PASS else (
                    f"金额 {_cent(line_amount)} 元 × 税率 {rate_value} 应为 {expected_tax} 元，"
                    f"原票税额 {_cent(line_tax)} 元，相差 {abs(_cent(line_tax) - expected_tax)} 元")
            if (_text(item.get("tax_amount", item.get("tax"))) and line_tax is None) or (_text(rate_value) and rate is None):
                tax_status = FAIL
                tax_reason = "行金额、税率或税额不是有效数字，无法核算本行税额"
            check("tax", prefix + "税额计算", tax_status, f"金额 {item.get('amount', '')}；税率 {rate_value}；税额 {item.get('tax_amount', item.get('tax', ''))}",
                  f"{expected_tax}（容差 ±0.01）" if expected_tax is not None else "行金额 × 税率，按分四舍五入",
                  tax_reason, keys=("items", "tax_rate", "tax_amount", "amount"))
            line_amounts.append(line_amount)
            line_taxes.append(line_tax)

        for values, header, key, title in ((line_amounts, amount, "amount", "明细金额汇总"),
                                            (line_taxes, tax, "tax_amount", "明细税额汇总")):
            complete = bool(values) and all(v is not None for v in values) and header is not None
            expected_sum = sum((_cent(value) for value in values), Decimal(0)) if complete else None
            if not complete:
                summary_status, summary_reason = REVIEW, "本票无可用的完整明细行，无法用明细逐行相加核对汇总"
            elif expected_sum == _cent(header):
                summary_status, summary_reason = PASS, ""
            else:
                summary_status = FAIL
                summary_reason = (f"票面{title}为 {_cent(header)} 元，明细逐行相加为 {expected_sum} 元，"
                                  f"相差 {abs(_cent(header) - expected_sum)} 元")
            check("total", title, summary_status,
                  fields.get(key, ""), str(expected_sum) if complete else "完整明细之和",
                  summary_reason, (key, "items"))

        words = fields.get("uppercase_total", "")
        try:
            parsed = parse_chinese_amount(words)
            upper_status = PASS if total is not None and _cent(parsed) == _cent(total) else FAIL if total is not None else REVIEW
            expected_upper = f"大写折算 {parsed} 元；数字合计 {fields.get('total', '')} 元"
            if upper_status == FAIL:
                upper_reason = (f"大写“{words}”折算为 {_cent(parsed)} 元，"
                                f"与数字价税合计 {_cent(total)} 元不一致")
            elif upper_status == REVIEW:
                upper_reason = "数字价税合计未识别，无法与大写金额比对"
            else:
                upper_reason = ""
        except (ValueError, InvalidOperation):
            upper_status = missing(("uppercase_total",)) or (FAIL if "uppercase_total" in invoice.confirmed else REVIEW)
            expected_upper = "可解析的人民币大写金额，与价税合计一致"
            upper_reason = (f"大写金额“{words}”已确认为空或无法解析为人民币大写，无法与数字合计核对"
                            if upper_status == FAIL else
                            f"大写金额“{words}”无法解析为规范的人民币大写，需人工核对原件")
        check("uppercase", "大小写金额一致", upper_status, words, expected_upper, upper_reason,
              keys=("uppercase_total", "total"))
        for warning in invoice.warnings:
            resolved = (warning.startswith("OCR 存在低置信度") and
                        set(fields).union({"kind", "items"}).issubset(invoice.confirmed))
            resolved = resolved or (warning.startswith("未自动识别明细") and bool(invoice.items) and "items" in invoice.confirmed)
            resolved = resolved or ("未识别为发票的页面" in warning and "pages" in invoice.confirmed)
            # 提示本身就是要复核的理由，直接放进 reason，不然报告里只剩标题没有解释。
            add("extraction", "识别提示", NA if resolved else REVIEW, warning,
                "已人工确认" if resolved else "核对原页并补全识别结果", warning, source=source)

        if total is None or uncertain(("total", "invoice_number", "invoice_code")) or not number:
            incomplete_total = True
        elif not duplicate:
            unique_totals.append(_cent(total))

    if unparsed_pages or not app.invoices or any(d.category == "发票" and (d.error or not d.invoices) for d in app.documents):
        incomplete_total = True
    invoice_sum = sum(unique_totals, Decimal(0))
    total_status = REVIEW if incomplete_total or requested is None else PASS if _cent(requested) == invoice_sum else FAIL
    if incomplete_total:
        total_reason = "存在尚未可靠识别的发票或未归类文件，不能确认申请金额与发票合计是否一致"
    elif requested is None:
        total_reason = "报销申请金额缺失或无效，无法与发票合计比对"
    elif total_status == FAIL:
        difference = _cent(requested) - invoice_sum
        total_reason = (f"申请报销 {_cent(requested)} 元，唯一发票价税合计 {invoice_sum:.2f} 元，"
                        f"相差 {abs(difference)} 元")
    else:
        total_reason = ""
    add("application_total", "申请金额与发票合计", total_status, app.requested_amount,
        f"唯一发票价税合计 {invoice_sum:.2f} 元", total_reason, field="requested_amount")

    received = {d.category for d in app.documents if d.category in known_categories}
    missing_attachments = [name for name in policy.required_attachments if name not in received]
    pending = any(d.category not in known_categories for d in app.documents)
    attachment_status = REVIEW if pending else FAIL if missing_attachments else PASS
    if missing_attachments:
        attachment_reason = "缺少：" + "、".join(missing_attachments)
    elif pending:
        attachment_reason = "存在尚未确认类别的附件，需先归类后才能确认附件是否齐全"
    else:
        attachment_reason = ""
    add("attachments", "必需附件齐全", attachment_status, "、".join(sorted(received)) or "无",
        "、".join(policy.required_attachments), attachment_reason, field="attachments")
    app.checks = checks
    return checks
