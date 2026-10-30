import tempfile
import unittest
from pathlib import Path

import report
from models import Application, Document, Invoice, Policy
from reportlab.lib.colors import HexColor
from rules import audit

RED = HexColor("#BC2635").hexval()
ORANGE = HexColor("#A65300").hexval()

REAL_PARAGRAPH = report.Paragraph


class _RecordingParagraph(REAL_PARAGRAPH):
    """记录每一格的最终文本（含内联 <font color=...> 标记）。

    report.paragraph() 是 export_pdf 内部的闭包，无法直接替换，只能替换模块级
    Paragraph 类，这样捕获到的就是真实导出路径上的内容。
    """

    captured = []

    def __init__(self, text, style=None, *args, **kwargs):
        type(self).captured.append(str(text))
        super().__init__(text, style, *args, **kwargs)


def example():
    policy = Policy()
    invoice = Invoice("测试发票.pdf", kind="电子普通发票", fields={
        "invoice_number": "12345678901234567890", "invoice_code": "", "invoice_date": "2026-07-17",
        "buyer_name": policy.company_name, "buyer_tax_id": policy.company_tax_id,
        "seller_name": "测试销售公司", "seller_tax_id": "91110108MA01XYZ7K9",
        "amount": "100.00", "tax_rate": "13%", "tax_amount": "13.00", "total": "113.00",
        "uppercase_total": "壹佰壹拾叁元整",
    }, items=[{"name": "测试商品", "quantity": "2", "unit_price": "50", "amount": "100.00",
               "tax_rate": "13%", "tax_amount": "13.00"}])
    app = Application("TEST-1", "测试申请人", "测试部", "2026-10-15", "113.00", [
        Document(invoice.source, "发票", [invoice]),
        Document("申请.pdf", "报销申请"),
        Document("付款.pdf", "付款凭证")])
    return app, policy, invoice


class ReportVerdictColourTests(unittest.TestCase):
    """D3：结论着色必须区分「真不通过」（红）与「待人工复核」（橙）。

    参考结果单只有两态口径，待人工复核在参考口径里同样写作「不通过」；只靠文字
    无法区分，因此结论单元格的颜色就是这两种情况唯一的视觉区别。
    """

    def setUp(self):
        _RecordingParagraph.captured = []
        report.Paragraph = _RecordingParagraph
        self.addCleanup(self._restore)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.app, self.policy, self.invoice = example()

    def _restore(self):
        report.Paragraph = REAL_PARAGRAPH

    def export(self):
        audit(self.app, self.policy)
        path = report.export_pdf([self.app], self.policy, str(Path(self.directory.name) / "结果.pdf"))
        self.assertTrue(Path(path).is_file())
        self.assertGreater(Path(path).stat().st_size, 1024)
        return path

    def verdict_cell(self):
        """最后一格含 审核通过/审核不通过 的单元格就是「最终审核结论」。"""
        cells = [text for text in _RecordingParagraph.captured
                 if "审核通过" in text or "审核不通过" in text]
        self.assertTrue(cells, "没有捕获到结论单元格")
        return cells[-1]

    def test_confirmed_failure_is_red(self):
        self.invoice.fields.update(
            invoice_date="2026-01-01", buyer_name="错误公司", buyer_tax_id="91110108MA01WRONG1")
        self.export()
        self.assertEqual(self.app.status, "不通过")
        cell = self.verdict_cell()
        self.assertIn(RED, cell)
        self.assertNotIn(ORANGE, cell)

    def test_review_only_outcome_is_amber_with_pending_row(self):
        self.invoice.uncertain.extend(["buyer_name", "invoice_number"])
        self.export()
        self.assertEqual(self.app.status, "待人工复核")
        cell = self.verdict_cell()
        self.assertIn(ORANGE, cell)
        self.assertNotIn(RED, cell)
        self.assertTrue(any("待人工复核项" in text for text in _RecordingParagraph.captured),
                        "待人工复核的单据必须在结论表里明示待复核项")

    def test_passing_application_has_no_warning_colour(self):
        self.export()
        self.assertEqual(self.app.status, "预审通过")
        cell = self.verdict_cell()
        self.assertNotIn(RED, cell)
        self.assertNotIn(ORANGE, cell)


if __name__ == "__main__":
    unittest.main()
