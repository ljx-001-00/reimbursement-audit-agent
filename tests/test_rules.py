import copy
from decimal import Decimal
import unittest

from models import Application, Document, Invoice, Policy, PASS, FAIL, REVIEW, NA
from rules import audit, amount_in_words, parse_chinese_amount


def example():
    policy = Policy()
    invoice = Invoice("测试发票.pdf", kind="电子普通发票", fields={
        "invoice_number": "12345678901234567890", "invoice_code": "", "invoice_date": "2026-07-17",
        "buyer_name": policy.company_name, "buyer_tax_id": policy.company_tax_id,
        "seller_name": "测试销售公司", "seller_tax_id": "91110108MA01XYZ7K9",
        "amount": "100.00", "tax_rate": "13%", "tax_amount": "13.00", "total": "113.00",
        "uppercase_total": "壹佰壹拾叁元整",
    }, items=[{"name": "测试商品", "quantity": "2", "unit_price": "50", "amount": "100.00", "tax_rate": "13%", "tax_amount": "13.00"}])
    app = Application("TEST-1", "测试申请人", "测试部", "2026-10-15", "113.00", [
        Document(invoice.source, "发票", [invoice]), Document("申请.pdf", "报销申请"), Document("付款.pdf", "付款凭证")])
    return app, policy, invoice


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.app, self.policy, self.invoice = example()

    def checks(self, code):
        return [c for c in audit(self.app, self.policy) if c.code == code]

    def test_valid_and_mutates_application_checks(self):
        result = audit(self.app, self.policy)
        self.assertIs(result, self.app.checks)
        self.assertEqual(self.app.status, "预审通过")
        self.assertTrue(all(c.status in (PASS, NA) for c in result))

    def test_every_failure_explains_itself(self):
        """D2：任何 FAIL/REVIEW 都必须给出可读原因，不能只留空备注。"""
        self.invoice.fields.update(
            invoice_date="2026-01-01", buyer_name="错误公司", buyer_tax_id="91110108MA01WRONG1",
            seller_tax_id="123", amount="90.00", uppercase_total="壹佰元整")
        self.invoice.items[0].update(quantity="3", amount="90.00")
        self.app.documents = [d for d in self.app.documents if d.category == "发票"]
        self.app.requested_amount = "500.00"
        result = audit(self.app, self.policy)
        self.assertTrue(any(c.status in (FAIL, REVIEW) for c in result))
        for item in result:
            if item.status in (FAIL, REVIEW):
                self.assertTrue(item.reason.strip(),
                                f"{item.code}/{item.title} 状态为 {item.status} 却没有说明原因")

    def test_uncertain_and_missing_checks_explain_themselves(self):
        """D2：字段缺失或待确认时同样必须说明原因。"""
        self.invoice.fields.update(
            invoice_date="", amount="", tax_amount="", total="", uppercase_total="",
            seller_name="", seller_tax_id="")
        self.invoice.items[0].update(quantity="", unit_price="")
        self.invoice.uncertain.extend(["buyer_name", "invoice_number"])
        self.app.requested_amount = ""
        result = audit(self.app, self.policy)
        self.assertTrue(any(c.status in (FAIL, REVIEW) for c in result))
        for item in result:
            if item.status in (FAIL, REVIEW):
                self.assertTrue(item.reason.strip(),
                                f"{item.code}/{item.title} 状态为 {item.status} 却没有说明原因")

    def test_date_inclusive_boundary_and_future(self):
        self.assertEqual(self.checks("date")[0].status, PASS)  # exactly 90 days
        self.invoice.fields["invoice_date"] = "2026-07-16"
        self.assertEqual(self.checks("date")[0].status, FAIL)
        self.invoice.fields["invoice_date"] = "2026-10-16"
        self.assertEqual(self.checks("date")[0].status, FAIL)
        self.invoice.fields["invoice_date"] = "2026年10月15日"
        self.assertEqual(self.checks("date")[0].status, PASS)

    def test_traditional_and_electronic_number_formats(self):
        self.invoice.kind = "增值税专用发票"
        self.invoice.fields.update(invoice_code="1234567890", invoice_number="12345678")
        self.assertEqual(self.checks("invoice_number")[0].status, PASS)
        self.invoice.fields["invoice_code"] = "123456789012"
        self.assertEqual(self.checks("invoice_number")[0].status, PASS)
        self.invoice.kind = "电子普通发票"
        self.assertEqual(self.checks("invoice_number")[0].status, PASS)
        self.invoice.fields["invoice_code"] = ""
        self.assertEqual(self.checks("invoice_number")[0].status, FAIL)
        self.invoice.kind = "未知"
        self.assertEqual(self.checks("invoice_number")[0].status, REVIEW)

    def test_uncertain_mismatch_requires_review_then_confirmed_fails(self):
        self.invoice.fields["buyer_name"] = "错误公司"
        self.invoice.uncertain.append("buyer_name")
        self.assertEqual(self.checks("buyer_name")[0].status, REVIEW)
        self.assertEqual(self.app.status, REVIEW)
        self.invoice.edit("buyer_name", "错误公司")
        self.assertEqual(self.checks("buyer_name")[0].status, FAIL)

    def test_missing_field_review_and_confirmed_missing_fail(self):
        self.invoice.fields["seller_name"] = ""
        self.assertEqual(self.checks("seller")[0].status, REVIEW)
        self.invoice.edit("seller_name", "")
        self.assertEqual(self.checks("seller")[0].status, FAIL)

    def test_direct_application_values_must_be_valid(self):
        for invalid in ("", "NaN", "Infinity", "-1", "1e1000000"):
            self.app.requested_amount = invalid
            self.checks("required")
            self.assertEqual(self.app.status, FAIL, invalid)
        self.app.requested_amount = "113"
        self.app.submitted_on = "2026-02-30"
        self.checks("required")
        self.assertEqual(self.app.status, FAIL)

    def test_invalid_invoice_numbers_never_pass(self):
        for invalid in ("NaN", "Infinity", "-1"):
            self.invoice.fields["amount"] = invalid
            audit(self.app, self.policy)
            self.assertEqual(self.app.status, FAIL, invalid)

    def test_quantity_and_half_up(self):
        self.invoice.items[0].update(quantity="3", unit_price="0.335", amount="1.01")
        self.assertEqual(self.checks("quantity")[0].status, PASS)
        self.invoice.items[0]["amount"] = "1.00"
        self.assertEqual(self.checks("quantity")[0].status, FAIL)
        self.invoice.items[0].update(quantity="", unit_price="")
        self.assertEqual(self.checks("quantity")[0].status, NA)

    def test_tax_tolerance_is_inclusive_one_cent(self):
        self.invoice.items[0]["tax_amount"] = "13.01"
        self.assertEqual(self.checks("tax")[0].status, PASS)
        self.invoice.items[0]["tax_amount"] = "12.99"
        self.assertEqual(self.checks("tax")[0].status, PASS)
        self.invoice.items[0]["tax_amount"] = "13.02"
        self.assertEqual(self.checks("tax")[0].status, FAIL)

    def test_multiple_tax_rates_and_line_sum(self):
        self.invoice.fields.update(amount="150", tax_amount="19.50", total="169.50", tax_rate="多税率", uppercase_total="壹佰陆拾玖元伍角整")
        self.invoice.items = [
            {"quantity": "1", "unit_price": "50", "amount": "50", "tax_rate": "13%", "tax_amount": "6.50"},
            {"quantity": "2", "unit_price": "50", "amount": "100", "tax_rate": "13%", "tax_amount": "13.00"},
        ]
        self.app.requested_amount = "169.50"
        audit(self.app, self.policy)
        self.assertEqual(self.app.status, "预审通过")
        self.invoice.items[1].update(tax_rate="6%", tax_amount="6")
        self.assertTrue(all(c.status == PASS for c in self.checks("tax")))
        self.assertTrue(any(c.status == FAIL for c in self.checks("total")))

    def test_uppercase_disagreement(self):
        self.invoice.fields["uppercase_total"] = "壹佰壹拾贰元整"
        self.assertEqual(self.checks("uppercase")[0].status, FAIL)
        self.invoice.fields["uppercase_total"] = "识别乱码"
        self.assertEqual(self.checks("uppercase")[0].status, REVIEW)
        self.invoice.confirmed.append("uppercase_total")
        self.assertEqual(self.checks("uppercase")[0].status, FAIL)

    def test_multiple_invoices_and_duplicate_not_summed(self):
        second = copy.deepcopy(self.invoice)
        second.source = "第二票.pdf"
        second.fields["invoice_number"] = "22345678901234567890"
        self.app.documents.append(Document(second.source, "发票", [second]))
        self.app.requested_amount = "226.00"
        self.assertEqual(self.checks("application_total")[0].status, PASS)
        second.fields["invoice_number"] = self.invoice.fields["invoice_number"]
        self.assertEqual(self.checks("duplicate")[0].status, FAIL)
        total = self.checks("application_total")[0]
        self.assertEqual(total.status, FAIL)
        self.assertIn("113.00", total.expected)
        self.invoice.uncertain.append("invoice_number")
        self.assertEqual(self.checks("duplicate")[0].status, REVIEW)

    def test_missing_and_pending_attachments(self):
        self.app.documents.pop()
        self.assertEqual(self.checks("attachments")[-1].status, FAIL)
        self.app.documents.append(Document("未知.pdf", "待确认"))
        self.assertEqual(self.checks("attachments")[-1].status, REVIEW)
        self.app.documents[-1].category = "付款凭证"
        self.assertEqual(self.checks("attachments")[-1].status, PASS)

    def test_processing_failure_and_unrecognized_pages_block_pass(self):
        self.app.documents.append(Document("损坏.pdf", "发票", error="无法打开文件"))
        self.assertEqual(self.checks("document")[0].status, REVIEW)
        self.assertEqual(self.checks("application_total")[0].status, REVIEW)
        self.app.documents.pop()
        self.invoice.warnings.append("第2页未识别到发票")
        self.assertEqual(self.checks("extraction")[0].status, REVIEW)
        self.assertEqual(self.app.status, REVIEW)

    def test_edit_then_reaudit_clears_stale_failure(self):
        self.invoice.fields["buyer_name"] = "错误公司"
        self.checks("buyer_name")
        self.assertEqual(self.app.status, FAIL)
        self.invoice.edit("buyer_name", self.policy.company_name)
        self.checks("buyer_name")
        self.assertEqual(self.app.status, "预审通过")
        self.assertEqual(len(self.invoice.edits), 1)

    def test_optional_uncertain_field_also_blocks_pass(self):
        self.invoice.uncertain.append("item_name")
        audit(self.app, self.policy)
        self.assertEqual(self.app.status, REVIEW)

    def test_ocr_warning_resolved_only_after_fields_and_items_confirmed(self):
        self.invoice.warnings.append("OCR 存在低置信度文字，请对照原票确认字段及明细")
        for key, value in self.invoice.fields.items():
            self.invoice.edit(key, value)
        self.invoice.edit("kind", self.invoice.kind)
        self.assertEqual(self.checks("extraction")[0].status, REVIEW)
        self.invoice.confirmed.append("items")
        self.assertEqual(self.checks("extraction")[0].status, NA)
        self.assertEqual(self.app.status, "预审通过")
        self.assertTrue(self.invoice.warnings)  # retain original extraction history

    def test_unrecognized_page_cannot_be_bypassed_by_clearing_warning(self):
        self.app.documents[0].pages = 2
        self.invoice.warnings.clear()
        self.assertEqual(self.checks("document")[0].status, REVIEW)
        self.assertEqual(self.checks("application_total")[0].status, REVIEW)
        self.invoice.confirmed.append("items")
        self.assertEqual(self.checks("document")[0].status, REVIEW)
        self.invoice.confirmed.append("pages")
        self.assertEqual(self.checks("document")[0].status, NA)
        self.assertEqual(self.app.status, "预审通过")

    def test_invalid_attachment_category_remains_pending(self):
        self.app.documents.append(Document("未知资料.pdf", ""))
        self.assertEqual(self.checks("attachments")[-1].status, REVIEW)
        self.assertEqual(self.app.status, REVIEW)

    def test_invalid_code_and_tax_id_point_to_the_invalid_field(self):
        self.invoice.fields.update(invoice_number="12345678", invoice_code="123")
        self.assertEqual(self.checks("invoice_number")[0].field, "invoice_code")
        self.invoice.fields["seller_tax_id"] = "BAD"
        self.assertEqual(self.checks("seller")[0].field, "seller_tax_id")
        self.invoice.fields["seller_tax_id"] = ""
        self.assertEqual(self.checks("seller")[0].field, "seller_tax_id")
        self.invoice.fields["seller_tax_id"] = self.policy.company_tax_id
        self.invoice.uncertain.append("seller_tax_id")
        self.assertEqual(self.checks("seller")[0].field, "seller_tax_id")

    def test_currency_values_rounded_individually_before_summing(self):
        self.invoice.fields.update(amount="100.004", tax_amount="13.004", total="113.008")
        total = [c for c in self.checks("total") if c.title == "金额加税额等于价税合计"][0]
        self.assertEqual(total.status, FAIL)
        self.assertEqual(total.expected, "113.00")
        self.invoice.items = [{"amount": "50.004", "tax_amount": "0", "tax_rate": "免税"},
                              {"amount": "50.004", "tax_amount": "0", "tax_rate": "免税"}]
        self.invoice.fields.update(amount="100.01", tax_amount="0", total="100.01")
        total = [c for c in self.checks("total") if c.title == "明细金额汇总"][0]
        self.assertEqual(total.status, FAIL)
        self.assertEqual(total.expected, "100.00")


class ChineseMoneyTests(unittest.TestCase):
    def test_roundtrip_and_traditional_variants(self):
        for value in ("0", "0.01", "0.10", "0.50", "1.05", "10", "1001", "10000", "10001", "100000001", "100010001", "1234567890123.45", "9999999999999999.99"):
            with self.subTest(value=value):
                self.assertEqual(parse_chinese_amount(amount_in_words(value)), Decimal(value))
        self.assertEqual(parse_chinese_amount("人民幣壹佰元整".replace("幣", "币")), Decimal(100))
        self.assertEqual(parse_chinese_amount("貳萬參仟陸佰圓伍角正"), Decimal("23600.50"))
        self.assertEqual(amount_in_words("1.005"), "壹元零壹分")
        self.assertEqual(amount_in_words(Decimal("0")), "零元整")

    def test_malformed_and_nonfinite_rejected(self):
        for value in ("", "壹贰元整", "壹元壹角壹角", "负壹元整", "壹元", "garbage", "壹拾拾元整", "零拾元整"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_chinese_amount(value)
        for value in ("NaN", "Infinity", "-1"):
            with self.assertRaises(ValueError):
                amount_in_words(value)


if __name__ == "__main__":
    unittest.main()
