"""Chinese, evidence-based financial pre-audit reports.

Layout follows the competition's reference result sheet:
一、单据基本信息 → 二、发票识别与信息提取 → 三、合规校验结果
→ 四、问题与不合规项 → 五、审核结论与处理建议 → 六、审核签章与人工复核
"""
from datetime import datetime
from pathlib import Path
import os
import tempfile
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    CondPageBreak, KeepTogether, LongTable, PageBreak, Paragraph, SimpleDocTemplate, Spacer, TableStyle,
)

from models import Application, FAIL, FIELD_LABELS, NA, PASS, Policy, PRESETS, REVIEW, ROOT

# The reference sheet is two-state. Anything not clearly passing stays "不通过" there.
REFERENCE_VERDICT = {PASS: "通过", FAIL: "不通过", REVIEW: "不通过", NA: "不通过", "预审通过": "通过"}
VERDICT_TEXT = {"通过": "审核通过，准予报销", "不通过": "审核不通过，退回补正", "待审核": "待审核"}
UNITS = {"次", "个", "台", "套", "件", "张", "箱", "米", "份", "人", "天", "月", "年", "项", "批",
         "只", "块", "本", "页", "吨", "小时", "人次", "平方米", "公斤", "千克", "升", "米", "组"}


def _filename(value):
    return str(value).replace("\\", "/").rsplit("/", 1)[-1]


def split_unit(item):
    """Separate a trailing unit from the item name, as the reference sheet shows 数量 / 单位."""
    name = str(item.get("name") or "").strip()
    unit = str(item.get("unit") or "").strip()
    if unit:
        return name, unit
    parts = name.rsplit(" ", 1)
    if len(parts) == 2 and parts[1] in UNITS and parts[0]:
        return parts[0], parts[1]
    return name, ""


def reference_verdict(conclusion):
    return REFERENCE_VERDICT.get(conclusion, "待审核")


def _money(value):
    text = str(value or "").strip()
    if not text:
        return "未提供"
    if text[0] in "¥￥" or not text.replace(",", "").replace(".", "").isdigit():
        return text
    return "¥" + text


def export_pdf(apps: list[Application], policy: Policy, path: str) -> str:
    """Export every application, replacing the destination only after success."""
    if not apps:
        raise ValueError("没有可导出的报销申请。")
    target = Path(path).expanduser().resolve()
    if target.suffix.lower() != ".pdf":
        raise ValueError("报告文件名必须以 .pdf 结尾。")
    for app in apps:
        for document in app.documents:
            if document.path and Path(document.path).resolve() == target:
                raise ValueError("报告不能覆盖原始单据，请选择其他文件名。")
    font_path = ROOT / "assets" / "font.ttf"
    if not font_path.is_file():
        raise FileNotFoundError("缺少随程序提供的中文字体 assets/font.ttf，请重新解压完整程序。")
    if "Chinese" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("Chinese", str(font_path)))

    ink = colors.HexColor("#203345")
    muted = colors.HexColor("#566575")
    red = colors.HexColor("#BC2635")
    orange = colors.HexColor("#A65300")
    green = colors.HexColor("#1A6B45")
    status_colors = {FAIL: red, REVIEW: orange}
    width = A4[0] - 84
    styles = {
        "body": ParagraphStyle("Body", fontName="Chinese", fontSize=9,
                               leading=14, textColor=ink, wordWrap="CJK"),
        "small": ParagraphStyle("Small", fontName="Chinese", fontSize=8,
                                leading=12, textColor=ink, wordWrap="CJK"),
        "english": ParagraphStyle("English", fontName="Chinese", fontSize=8,
                                  leading=12, textColor=muted, alignment=TA_CENTER),
        "heading": ParagraphStyle("Heading", fontName="Chinese", fontSize=12,
                                  leading=18, textColor=ink, spaceBefore=9,
                                  spaceAfter=5, wordWrap="CJK"),
        "title": ParagraphStyle("Title", fontName="Chinese", fontSize=19,
                                leading=27, alignment=TA_CENTER, textColor=ink,
                                spaceAfter=2, wordWrap="CJK"),
    }

    def paragraph(value, kind="body", status=None):
        text = str(value) if value is not None and str(value) else "未提供"
        text = escape(text).replace("\n", "<br/>")
        if status in status_colors:
            text = f'<font color="{status_colors[status].hexval()}">{text}</font>'
        return Paragraph(text, styles[kind])

    def table(rows, widths, header=True):
        data = [[cell if isinstance(cell, Paragraph) else paragraph(cell, "small")
                 for cell in row] for row in rows]
        result = LongTable(data, colWidths=widths, repeatRows=1 if header else 0,
                           hAlign="LEFT", splitByRow=1, splitInRow=1)
        commands = [
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#D2DCE4")),
        ]
        if header:
            commands.append(("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E7EEF4")))
        result.setStyle(TableStyle(commands))
        return result

    def heading(text, minimum=100):
        # Leave space for the heading, repeated table header and first data row.
        story.extend([CondPageBreak(minimum), paragraph(text, "heading")])

    story = []
    generated_on = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    audit_day = datetime.now().strftime("%Y-%m-%d")
    for position, app in enumerate(apps):
        if position:
            story.append(PageBreak())
        conclusion = app.status if app.checks else "待审核"
        verdict = reference_verdict(conclusion)
        story.append(paragraph("标准化报销审核结果单", "title"))
        story.append(paragraph("Standardized Reimbursement Audit Report", "english"))
        story.append(Spacer(1, 4))
        story.append(paragraph(f"生成时间：{generated_on}    运行模式：{app.mode}", "small"))

        # 一、单据基本信息
        heading("一、单据基本信息")
        numbers = "、".join(str(inv.fields.get("invoice_number") or "待确认") for inv in app.invoices) or "未识别"
        story.append(table([
            ["审核单编号", app.id, "关联发票号码", numbers],
            ["报销人", app.applicant, "所在部门", app.department],
            ["报销提交日期", app.submitted_on, "申请报销金额", _money(app.requested_amount)],
        ], [76, width / 2 - 76, 88, width / 2 - 88], header=False))
        story.append(Spacer(1, 4))
        story.append(paragraph(f"审核依据：演示购买方 {policy.company_name}（税号 {policy.company_tax_id}）；"
                               f"报销期限 {policy.max_days} 天；必需附件 {'、'.join(policy.required_attachments) or '无'}；"
                               f"审核口径 {policy.preset_label()}；金额与税额按十进制分位核对，税额容差 ±0.01 元。", "small"))
        preset = PRESETS.get(policy.preset_name())
        if preset:
            story.append(paragraph(f"口径说明：{preset['note']}", "small"))
        story.append(Spacer(1, 4))
        application_docs = [_filename(document.path) for document in app.documents
                            if document.category == "报销申请" and document.application_fields.get("requested_amount")]
        origin = ("申报金额取自报销申请单 " + "、".join(application_docs) if application_docs
                  else "申请信息来自当前表单或演示载入记录")
        story.append(paragraph(f"输入来源：{origin}；票据字段来自原单据逐页提取及人工修正。上述规则为项目演示制度。", "small"))
        received = sorted({document.category for document in app.documents
                           if document.category and document.category != "待确认"})
        missing = [category for category in policy.required_attachments if category not in received]
        story.append(paragraph(f"必需附件：{'、'.join(policy.required_attachments) or '无'}；已收到类别：{'、'.join(received) or '无'}；缺失类别：{'、'.join(missing) or '无'}。", "small",
                               FAIL if missing else None))
        story.append(Spacer(1, 4))
        rows = [["文件", "附件类别", "页数", "处理情况"]]
        for document in app.documents:
            state = document.error or ("待确认附件类别" if document.category == "待确认" else "已载入")
            rows.append([_filename(document.path), document.category, str(document.pages or "未识别"),
                         paragraph(state, "small", REVIEW if document.error or document.category == "待确认" else None)])
        if len(rows) == 1:
            rows.append(["未载入文件", "-", "-", "待导入"])
        story.append(table(rows, [width * .40, width * .16, width * .10, width * .34]))

        # 二、发票识别与信息提取结果
        heading("二、发票识别与信息提取结果（OCR/解析）", 140 if app.invoices else 80)
        if not app.invoices:
            story.append(paragraph("尚无已识别且归类为发票的单据，不能据此认定审核通过。", status=REVIEW))
        for number, invoice in enumerate(app.invoices, 1):
            source_label = f"{_filename(invoice.source)} 第{invoice.page}页"
            heading(f"发票 {number}：{source_label}（{invoice.kind}）", 120)
            values = invoice.fields
            items = invoice.items
            first_name, first_unit = split_unit(items[0]) if items else ("", "")
            item_label = first_name if len(items) <= 1 else f"{first_name} 等 {len(items)} 项"
            quantity = str(items[0].get("quantity", "")) if items else ""
            price = str(items[0].get("unit_price", items[0].get("price", ""))) if items else ""
            amount = str(items[0].get("amount", "")) if items else values.get("amount", "")
            rate = str(items[0].get("tax_rate", "")) if items else values.get("tax_rate", "")
            tax = str(items[0].get("tax_amount", "")) if items else values.get("tax_amount", "")
            rate_tax = f"{rate} / {tax}" if rate or tax else ""
            story.append(table([
                ["发票号码", values.get("invoice_number", ""), "开票日期", values.get("invoice_date", "")],
                ["购买方名称", values.get("buyer_name", ""), "购买方税号", values.get("buyer_tax_id", "")],
                ["销售方名称", values.get("seller_name", ""), "销售方税号", values.get("seller_tax_id", "")],
                ["项目名称", item_label, "数量 / 单位", f"{quantity} / {first_unit}".strip(" /")],
                ["单价(元)", price, "金额(元)", amount],
                ["税率 / 税额(元)", rate_tax, "价税合计(大写)", values.get("uppercase_total", "")],
                ["价税合计(小写)", _money(values.get("total", "")), "明细条数", str(len(items)) or "0"],
            ], [76, width / 2 - 76, 88, width / 2 - 88], header=False))
            story.append(Spacer(1, 5))
            rows = [["字段", "当前值与确认状态", "提取证据（来源同上）"]]
            for key, label in FIELD_LABELS.items():
                value = invoice.kind if key == "kind" else values.get(key, "")
                matching = [check for check in app.checks if check.field == key
                            and _filename(check.source) == source_label]
                state = FAIL if any(check.status == FAIL for check in matching) else (
                    REVIEW if key in invoice.uncertain or any(check.status == REVIEW for check in matching) else None)
                confirmation = "人工已确认" if key in invoice.confirmed else "待人工确认" if key in invoice.uncertain else ""
                display_value = str(value or "未提供") + ("\n" + confirmation if confirmation else "")
                rows.append([label, paragraph(display_value, "small", state),
                             invoice.evidence.get(key) or "无可引用文本证据，请对照原票复核"])
            story.append(table(rows, [76, 155, width - 231]))
            if invoice.items:
                item_keys = ("quantity", "unit_price", "amount", "tax_rate", "tax_amount")
                item_rows = [["序号", "项目名称", "数量", "单位", "单价", "金额", "税率", "税额"]]
                extras = [["明细序号", "补充字段", "值"]]
                labels = {"specification": "规格型号", "price": "单价（原字段）",
                          "rate": "税率（原字段）", "tax": "税额（原字段）"}
                for item_number, item in enumerate(invoice.items, 1):
                    item_name, item_unit = split_unit(item)
                    item_rows.append([str(item_number), item_name] +
                                     [str(item.get(key, "")) for key in item_keys[:1]] +
                                     [item_unit] + [str(item.get(key, "")) for key in item_keys[1:]])
                    for key, value in item.items():
                        if key not in item_keys + ("name", "unit"):
                            extras.append([str(item_number), labels.get(key, key), str(value)])
                item_table = table(item_rows, [25, width - 365, 40, 40, 70, 70, 50, 70])
                if len(invoice.items) < 8:
                    story.append(KeepTogether([Spacer(1, 6), item_table]))
                else:
                    story.extend([Spacer(1, 6), item_table])
                if len(extras) > 1:
                    story.extend([Spacer(1, 6), table(extras, [60, 110, width - 170])])
            for warning in invoice.warnings:
                story.append(paragraph(f"识别提示：{warning}", "small", REVIEW))
            if invoice.edits:
                heading("人工修改记录")
                changes = [["时间", "字段", "修改前", "修改后"]]
                for change in invoice.edits:
                    changes.append([change.get("time", ""), FIELD_LABELS.get(change.get("field"), change.get("field", "")),
                                    change.get("before", ""), change.get("after", "")])
                story.append(table(changes, [110, 75, (width - 185) / 2, (width - 185) / 2]))

        # 三、合规校验结果
        heading("三、合规校验结果（规则校验表）")
        if app.checks:
            # Five columns, same as the reference result sheet: the source file is folded into
            # the note so the 校验标准 column keeps a readable width.
            rows = [["序号", "校验项目", "校验标准", "结果", "备注"]]
            for number, check in enumerate(app.checks, 1):
                note = check.reason or (f"实际值：{check.actual}" if check.actual and check.actual != check.expected else "—")
                note = f"{note}（来源：{_filename(check.source)}）"
                rows.append([str(number), check.title, check.expected,
                             paragraph(check.status, "small", check.status),
                             paragraph(note, "small", check.status)])
            story.append(table(rows, [26, 104, 152, 44, width - 326]))
            story.append(paragraph(f"状态说明：{PASS} / {FAIL} / {REVIEW} / {NA}。不适用项不作为通过依据；本次共检查 {len(app.checks)} 项。", "small"))
        else:
            story.append(paragraph("待审核：尚未执行规则校验，本报告不包含通过结论。", status=REVIEW))

        # 四、问题与不合规项
        heading("四、问题与不合规项（问题清单）", 80)
        problems = [check for check in app.checks if check.status in (FAIL, REVIEW)]
        if problems:
            for number, check in enumerate(problems, 1):
                story.append(paragraph(f"{number}. [{check.status}] {check.title}；单据：{_filename(check.source)}；{check.reason or '请核对实际值与期望值。'}", status=check.status))
        elif app.checks:
            story.append(paragraph("无。所有校验项均通过。"))
        else:
            story.append(paragraph("尚未审核，问题清单尚未形成。"))

        # 五、审核结论与处理建议
        heading("五、审核结论与处理建议", 110)
        advice = ("请补正不通过项目，并对待人工复核项确认后重新审核。" if conclusion == FAIL else
                  "请核对不确定字段及附件，并重新审核。" if conclusion == REVIEW else
                  "请先执行审核，再提交人工复核。" if conclusion == "待审核" else
                  "经规则校验，发票信息完整、计算一致、抬头与日期符合当前演示制度，准予进入人工复核。")
        pending = [check for check in app.checks if check.status == REVIEW]
        # Red means "confirmed non-compliant"; a review-only outcome must not borrow it.
        verdict_status = FAIL if conclusion == FAIL else (REVIEW if conclusion in (REVIEW, "待审核") else None)
        # "待人工复核" is not a confirmed violation: wording stays distinct while the
        # two-state reference column still reads 不通过.
        verdict_text = ("待补正后复核 · 参考口径记为审核不通过" if conclusion == REVIEW else
                        VERDICT_TEXT.get(verdict, "待审核"))
        conclusion_rows = [
            ["最终审核结论", paragraph(verdict_text, "body", verdict_status)],
            ["处理建议", advice],
            ["参考口径（两态）", verdict if verdict != "待审核" else "待审核"],
        ]
        if pending:
            # The reference sheet is two-state, so a review case still reads 不通过 there;
            # state that explicitly so amber is never read as a confirmed failure.
            conclusion_rows.append(["待人工复核项", paragraph(
                f"共 {len(pending)} 项需人工确认后方可判定；最终结论按参考口径记为「{verdict}」。",
                "small", REVIEW)])
        story.append(table(conclusion_rows, [92, width - 92], header=False))
        if app.ai_summary:
            story.append(Spacer(1, 5))
            story.append(paragraph("审核摘要（根据规则工具结果生成）：", "small"))
            story.append(paragraph(app.ai_summary, "small"))
        if app.model_note:
            story.append(Spacer(1, 4))
            story.append(paragraph("模型说明（由所选模型生成，只解释问题，不改判结论）：", "small"))
            story.append(paragraph(app.model_note, "small"))

        # 六、审核签章与人工复核
        heading("六、审核签章与人工复核", 145)
        story.append(table([
            ["审核人", "AI 智能体（财务初审）", "审核时间", audit_day],
            ["复核人", "待人工复核", "复核时间", "—"],
        ], [76, width / 2 - 76, 88, width / 2 - 88], header=False))
        story.append(Spacer(1, 6))
        story.append(table([
            ["复核意见", Paragraph("<br/><br/>", styles["body"])],
            ["审批与签章", Paragraph("<br/><br/>", styles["body"])],
        ], [76, width - 76], header=False))
        story.append(Spacer(1, 5))
        story.append(paragraph("—— 本单据由报销单据智能校验智能体自动生成，关键校验项需人工复核签字后生效 ——", "small"))
        if app.events:
            heading("附录：执行记录", 70)
            for event in app.events:
                story.append(paragraph(event, "small"))
        for document in app.documents:
            story.append(paragraph("原文件索引（供追溯）：" + document.path, "small"))

    def page_marks(canvas, doc):
        canvas.saveState()
        canvas.setFont("Chinese", 8)
        canvas.setFillColor(muted)
        canvas.drawString(42, 26, "标准化报销审核结果单 | 报销单据智能校验智能体 | 财务预审")
        canvas.drawRightString(A4[0] - 42, 26, f"第 {doc.page} 页")
        canvas.restoreState()

    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".audit-", suffix=".pdf", dir=target.parent)
    os.close(handle)
    try:
        doc = SimpleDocTemplate(temporary, pagesize=A4, leftMargin=42, rightMargin=42,
                                topMargin=38, bottomMargin=44, title="标准化报销审核结果单",
                                author="报销单据智能校验智能体")
        doc.build(story, onFirstPage=page_marks, onLaterPages=page_marks)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return str(target)
