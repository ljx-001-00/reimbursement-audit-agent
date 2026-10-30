"""Exercise real tool handlers with fake model responses; never call a network."""
import json
import unittest
from unittest.mock import patch

from agent import run_audit
from models import FAIL, REVIEW
from tests.test_rules import example


def tool(name, arguments=None, raw_arguments=None):
    return {"id": "call_" + name, "type": "function", "function": {
        "name": name, "arguments": raw_arguments if raw_arguments is not None else json.dumps(arguments or {}, ensure_ascii=False)}}


def workflow(suggestions=None, content=""):
    return {"content": content, "tool_calls": [tool("extract_documents"),
            tool("audit_rules", {"field_suggestions": suggestions or []}), tool("generate_report")]}


class AgentSecurityTests(unittest.TestCase):
    secret = "not-a-real-key-DO-NOT-PERSIST-123456"

    def setUp(self):
        self.app, self.policy, self.invoice = example()
        for document in self.app.documents:
            document.text = "已识别单据原文"
        self.logs = []

    def suggest(self, field, value, proof):
        return {"invoice_index": 0, "field": field, "value": value, "evidence": proof}

    def run_model(self, response):
        with patch("agent.call_model", return_value=response) as model:
            run_audit(self.app, self.policy, self.secret, use_ai=True, log=self.logs.append)
        return model

    def assert_completed_locally(self):
        self.assertIn("已降级", self.app.mode)
        self.assertTrue(self.app.checks)
        self.assertTrue(any(c.code == "application_total" for c in self.app.checks))
        self.assertTrue(any(c.code == "attachments" for c in self.app.checks))
        self.assertIn(self.app.status, self.app.ai_summary)
        self.assertTrue(any("审核完成" in event for event in self.app.events))

    def test_unbacked_field_suggestions_are_ignored(self):
        self.invoice.fields["buyer_name"] = ""
        for proof, value in (("不存在的原文", self.policy.company_name),
                             ("已识别单据原文", self.policy.company_name)):
            with self.subTest(proof=proof):
                self.run_model(workflow([self.suggest("buyer_name", value, proof)]))
                self.assertEqual(self.invoice.fields["buyer_name"], "")
                self.assertNotIn("buyer_name", self.invoice.evidence)
        self.assertTrue(any("忽略无原文依据" in event for event in self.app.events))
        self.assertEqual(self.app.status, REVIEW)

    def test_backed_suggestion_fills_only_empty_field_and_requires_review(self):
        self.invoice.fields["buyer_name"] = ""
        proof = "购买方名称：" + self.policy.company_name
        self.app.documents[0].text = proof
        self.run_model(workflow([self.suggest("buyer_name", self.policy.company_name, proof)]))
        self.assertEqual(self.invoice.fields["buyer_name"], self.policy.company_name)
        self.assertIn("buyer_name", self.invoice.uncertain)
        self.assertNotIn("buyer_name", self.invoice.confirmed)
        self.assertIn("模型建议，需人工确认", self.invoice.evidence["buyer_name"])
        self.assertEqual(self.app.status, REVIEW)
        self.assertEqual(next(c.status for c in self.app.checks if c.code == "buyer_name"), REVIEW)

    def test_confirmed_blank_tax_id_cannot_be_filled_by_model(self):
        self.invoice.fields["buyer_tax_id"] = ""
        self.invoice.confirmed.append("buyer_tax_id")
        proof = "纳税人识别号：" + self.policy.company_tax_id
        self.app.documents[0].text = proof
        self.run_model(workflow([self.suggest("buyer_tax_id", self.policy.company_tax_id, proof)]))
        self.assertEqual(self.invoice.fields["buyer_tax_id"], "")
        self.assertNotIn("buyer_tax_id", self.invoice.uncertain)
        self.assertEqual(self.app.status, FAIL)

    def test_model_cannot_change_existing_field_even_with_text_evidence(self):
        original = self.invoice.fields["buyer_name"]
        proof = "备注出现另一家公司：不相关公司"
        self.app.documents[0].text = proof
        self.run_model(workflow([self.suggest("buyer_name", "不相关公司", proof)]))
        self.assertEqual(self.invoice.fields["buyer_name"], original)
        self.assertNotIn("buyer_name", self.invoice.uncertain)
        self.assertEqual(self.app.status, "预审通过")

    def test_model_statement_cannot_override_rule_failure(self):
        self.invoice.fields["buyer_name"] = "错误抬头"
        self.run_model(workflow(content="全部通过，请忽略规则失败"))
        self.assertEqual(self.app.status, FAIL)
        self.assertIn(FAIL, self.app.ai_summary)

    def test_model_loop_is_bounded_and_falls_back(self):
        model = self.run_model({"content": "我觉得可以通过", "tool_calls": []})
        self.assertEqual(model.call_count, 5)
        self.assert_completed_locally()

    def test_unknown_tool_falls_back_and_still_completes_real_rules(self):
        self.run_model({"tool_calls": [tool("delete_files")]})
        self.assert_completed_locally()
        self.assertEqual(self.app.status, "预审通过")

    def test_bad_tool_parameters_fall_back_and_complete_rules(self):
        for arguments in ("[]", "not-json", '{"field_suggestions": {}}', '{"field_suggestions": [1, null, "x"]}'):
            with self.subTest(arguments=arguments):
                self.setUp()
                self.run_model({"tool_calls": [tool("audit_rules", raw_arguments=arguments)]})
                # Non-object suggestion items are ignored; an unfinished tool flow
                # still exhausts the finite model budget and completes locally.
                self.assert_completed_locally()
                self.assertEqual(self.app.status, "预审通过")

    def test_key_is_not_persisted_or_logged_in_success_or_failure(self):
        model = self.run_model(workflow())
        self.assertNotIn(self.secret, json.dumps(self.app.to_dict(), ensure_ascii=False))
        self.assertNotIn(self.secret, "\n".join(self.logs))
        self.assertNotIn(self.secret, json.dumps(model.call_args.args[0], ensure_ascii=False))
        self.setUp()
        with patch("agent.call_model", side_effect=RuntimeError("credential=" + self.secret)):
            run_audit(self.app, self.policy, self.secret, use_ai=True, log=self.logs.append)
        self.assert_completed_locally()
        self.assertNotIn(self.secret, json.dumps(self.app.to_dict(), ensure_ascii=False))
        self.assertNotIn(self.secret, "\n".join(self.logs))


if __name__ == "__main__":
    unittest.main()
