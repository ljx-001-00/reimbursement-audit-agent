"""口径预设与报销申请单抽取：验证"换口径不改规则"和"申报金额可自动读取"。"""
import unittest

from extractor import parse_application
from models import Application, Document


class PresetTests(unittest.TestCase):
    def test_default_policy_is_strict(self):
        from models import Policy
        policy = Policy()
        self.assertEqual(policy.preset_name(), "strict")
        self.assertEqual(policy.preset_label(), "严格口径")

    def test_reference_preset_only_changes_parameters(self):
        from models import PRESETS, Policy
        strict, reference = Policy(), Policy().with_preset("reference")
        self.assertEqual(reference.preset_name(), "reference")
        self.assertEqual(reference.max_days, PRESETS["reference"]["max_days"])
        self.assertEqual(reference.required_attachments, ["发票"])
        # 公司抬头等业务参数必须原样保留，口径切换只动期限与附件清单。
        self.assertEqual((reference.company_name, reference.company_tax_id),
                         (strict.company_name, strict.company_tax_id))

    def test_hand_edited_policy_is_custom(self):
        from models import Policy
        self.assertEqual(Policy(max_days=120).preset_name(), "custom")
        self.assertEqual(Policy(required_attachments=["发票"]).preset_name(), "custom")
        self.assertEqual(Policy(max_days=180, required_attachments=["发票"]).preset_name(), "reference")
        self.assertEqual(Policy().preset_label(), "严格口径")
        self.assertEqual(Policy(max_days=120).preset_label(), "自定义口径")

    def test_unknown_preset_is_rejected(self):
        from models import Policy
        with self.assertRaises(ValueError):
            Policy().with_preset("lenient")


class ApplicationPrefillTests(unittest.TestCase):
    def document(self, **fields):
        return Document("报销申请.pdf", "报销申请", application_fields=fields)

    def test_blank_form_is_filled_and_logged(self):
        app = Application("TEST-PREFILL", documents=[self.document(
            requested_amount="325.00", submitted_on="2026-10-15",
            applicant="测试用户", department="测试部门")])
        filled = app.prefill_from_documents()
        self.assertEqual((app.applicant, app.department, app.submitted_on, app.requested_amount),
                         ("测试用户", "测试部门", "2026-10-15", "325.00"))
        self.assertEqual(len(filled), 4)
        self.assertIn("报销申请.pdf", app.events[-1])
        self.assertIn("申请金额=325.00", app.events[-1])

    def test_typed_values_win(self):
        app = Application("TEST-KEEP", "手工填写人", "手工部门", "2026-09-01", "999.00",
                          [self.document(requested_amount="325.00", submitted_on="2026-10-15",
                                         applicant="测试用户", department="测试部门")])
        self.assertEqual(app.prefill_from_documents(), [])
        self.assertEqual((app.applicant, app.department, app.submitted_on, app.requested_amount),
                         ("手工填写人", "手工部门", "2026-09-01", "999.00"))

    def test_today_placeholder_is_replaced_by_application_date(self):
        from datetime import date
        app = Application("TEST-TODAY", submitted_on=date.today().isoformat(),
                          documents=[self.document(submitted_on="2026-10-15")])
        app.prefill_from_documents()
        self.assertEqual(app.submitted_on, "2026-10-15")

    def test_non_application_document_is_ignored(self):
        invoice_doc = Document("发票.pdf", "发票", application_fields={"requested_amount": "1.00"})
        app = Application("TEST-IGNORE", documents=[invoice_doc])
        self.assertEqual(app.prefill_from_documents(), [])
        self.assertEqual(app.requested_amount, "")


class ParseApplicationTests(unittest.TestCase):
    def test_full_width_colon_and_currency_symbol(self):
        fields = parse_application("报销申请\n申请人：张三  部门：技术部\n申请日期：2026-10-15\n"
                                   "申请金额：¥1,325.50 元\n事由：办公用品采购")
        self.assertEqual(fields["applicant"], "张三")
        self.assertEqual(fields["department"], "技术部")
        self.assertEqual(fields["submitted_on"], "2026-10-15")
        self.assertEqual(fields["requested_amount"], "1325.50")
        self.assertEqual(fields["purpose"], "办公用品采购")

    def test_half_width_and_spaced_layout(self):
        fields = parse_application("Reimbursement\n申请人: 测试用户    部门: 测试部门\n"
                                   "申请金额: 325.00\n报销日期: 2026/10/15")
        self.assertEqual(fields["applicant"], "测试用户")
        self.assertEqual(fields["department"], "测试部门")
        self.assertEqual(fields["requested_amount"], "325.00")
        self.assertEqual(fields["submitted_on"], "2026-10-15")

    def test_chinese_date_is_normalised(self):
        fields = parse_application("申请日期: 2026年10月15日\n合计金额: 88")
        self.assertEqual(fields["submitted_on"], "2026-10-15")
        self.assertEqual(fields["requested_amount"], "88.00")

    def test_label_without_number_is_not_invented(self):
        # 现有测试用申请单写的是"申请金额由程序表单填写"，不能凭空编一个金额出来。
        fields = parse_application("测试数据 报销申请\n申请人: 测试用户  部门: 测试部门\n"
                                   "申请金额由程序表单填写，以表单本次金额为准。")
        self.assertNotIn("requested_amount", fields)
        self.assertEqual(fields["applicant"], "测试用户")

    def test_out_of_range_amount_is_dropped(self):
        self.assertNotIn("requested_amount", parse_application("申请金额: 0"))
        self.assertNotIn("requested_amount", parse_application("申请金额: -5"))
        self.assertNotIn("requested_amount", parse_application("申请金额: 999999999999999999"))

    def test_malformed_input_returns_empty_dict(self):
        self.assertEqual(parse_application(""), {})
        self.assertEqual(parse_application(None), {})


if __name__ == "__main__":
    unittest.main()
