"""Fail-safe 回归：读不出内容的单据，永远不能走到「预审通过」。

这类检查比"正常票能通过"更重要：合规工具最坏的失效不是漏判异常，而是
把看不清的东西当成合规放行。这些用例之前没有覆盖，抓的是同一条性质。
"""
import tempfile
import unittest
from pathlib import Path

from PIL import Image
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from extractor import extract_document
from models import FAIL, NA, PASS, ROOT, REVIEW, Application, Document, Invoice, Policy
from rules import audit

FONT = "AdversarialFixture"


def _font():
    if FONT not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(FONT, str(ROOT / "assets" / "font.ttf")))
    return FONT


def _pdf(folder, name, lines):
    path = Path(folder) / name
    c = canvas.Canvas(str(path), pagesize=(595, 420))
    c.setFont(_font(), 14)
    for index, line in enumerate(lines):
        c.drawString(40, 360 - index * 30, line)
    c.save()
    return path


class NeverApproveUnreadableTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.folder = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def assertNotApproved(self, document, label):
        """整笔不得为预审通过，且必须给出可读的原因。"""
        app = Application("ADV-1", "测试", "测试部", "2026-03-05", documents=[document])
        app.checks = audit(app, Policy())
        self.assertNotEqual(app.status, "预审通过", f"{label} 被误判为通过")
        problems = [c for c in app.checks if c.status in (FAIL, REVIEW)]
        self.assertTrue(problems, f"{label} 既没通过也没给出问题项")
        self.assertTrue(all(c.reason for c in problems), f"{label} 的问题项缺少原因")

    def test_unrelated_document_is_not_classified_as_invoice(self):
        memo = _pdf(self.folder, "会议纪要.pdf", ["部门周会纪要", "议题：季度预算调整", "参会人：张三、李四"])
        document = extract_document(memo)
        self.assertNotEqual(document.category, "发票")
        self.assertEqual(document.invoices, [])
        self.assertNotApproved(document, "无关文档")

    def test_blank_invoice_page_is_never_approved(self):
        blank = _pdf(self.folder, "空白票.pdf", ["增值税电子普通发票", "（本页无有效内容）"])
        document = extract_document(blank)
        self.assertNotApproved(document, "空白票")

    def test_noise_image_is_never_approved(self):
        path = Path(self.folder) / "噪声.png"
        Image.effect_noise((760, 520), 48).convert("RGB").save(path)
        self.assertNotApproved(extract_document(path), "噪声图片")

    def test_missing_amounts_never_pass_amount_checks(self):
        page = _pdf(self.folder, "缺金额票.pdf", [
            "电子发票（普通发票）", "发票号码：25112000000100999999", "开票日期：2026年03月01日",
            "名称：北京蓝海智能科技有限公司", "统一社会信用代码/纳税人识别号：91110108MA01XYZ7K9"])
        document = extract_document(page)
        app = Application("ADV-2", "测试", "测试部", "2026-03-05", documents=[document])
        app.checks = audit(app, Policy())
        amounts = [c for c in app.checks if c.code in ("total", "tax", "uppercase", "quantity")]
        self.assertTrue(amounts, "缺少金额类校验项")
        for check in amounts:
            self.assertNotEqual(check.status, PASS,
                                f"金额缺失时「{check.title}」不能判通过：{check.actual}")


class DuplicateInvoiceTests(unittest.TestCase):
    def test_same_invoice_twice_is_flagged_and_not_double_counted(self):
        invoice = Invoice("INV-01.pdf", kind="电子普通发票", fields={
            "invoice_number": "25112000000100012345", "invoice_date": "2026-03-01",
            "buyer_name": Policy().company_name, "buyer_tax_id": Policy().company_tax_id,
            "seller_name": "上海智慧云科技有限公司", "amount": "9900.99",
            "tax_rate": "1%", "tax_amount": "99.01", "total": "10000.00",
            "uppercase_total": "壹万圆整"})
        app = Application("ADV-3", "测试", "测试部", "2026-03-05", "10000.00", documents=[
            Document("INV-01.pdf", "发票", [invoice]),
            Document("INV-01-copy.pdf", "发票", [Invoice("INV-01-copy.pdf", kind=invoice.kind,
                                                         fields=dict(invoice.fields))])])
        app.checks = audit(app, Policy())
        duplicate = [c for c in app.checks if c.code == "duplicate"]
        self.assertTrue(duplicate, "缺少重复发票校验项")
        self.assertEqual(duplicate[0].status, FAIL)
        self.assertIn("重复", duplicate[0].reason)


class PolicyCoverageTests(unittest.TestCase):
    def test_every_check_has_a_status_and_explains_failures(self):
        """所有校验项都必须是四种状态之一，且不通过与待复核必须带原因。"""
        app = Application("ADV-4", "测试", "测试部", "2026-03-05", documents=[])
        app.checks = audit(app, Policy())
        self.assertTrue(app.checks)
        for check in app.checks:
            self.assertIn(check.status, (PASS, FAIL, REVIEW, NA), check.title)
            if check.status in (FAIL, REVIEW):
                self.assertTrue(check.reason.strip(), f"{check.title} 缺少原因")

    def test_demo_applications_explain_every_failure(self):
        """10 笔演示单逐笔过审：任何"不通过/待复核"都必须能读到原因。

        只跑规则、不做识别，所以几毫秒完成，适合当常驻回归。
        """
        from demo import load_demo

        policy = Policy()
        for app in load_demo():
            app.checks = audit(app, policy)
            for check in app.checks:
                if check.status in (FAIL, REVIEW):
                    self.assertTrue(check.reason.strip(), f"{app.id} 的「{check.title}」缺少原因")


if __name__ == "__main__":
    unittest.main()
