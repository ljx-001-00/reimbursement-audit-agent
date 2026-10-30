import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from models import Check, Document, Invoice, PASS
from ui import ReimbursementApp


class AIImportUITests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.profile_patch = patch("model_settings.profile_path", return_value=Path(self.folder.name) / "profiles.json")
        self.env_patch = patch.dict(os.environ, {"REIMBURSEMENT_API_KEY": "", "GLM_API_KEY": "",
            "REIMBURSEMENT_BASE_URL": "https://models.example.test/v1", "REIMBURSEMENT_MODEL": "import-model"})
        self.profile_patch.start()
        self.env_patch.start()
        self.app = ReimbursementApp()
        self.app.withdraw()
        self.app.new_application()
        self.app.api_key.set("AI_IMPORT_UI_SECRET")

    def tearDown(self):
        self.app.progress.stop()
        self.app.update_idletasks()
        self.app.destroy()
        self.env_patch.stop()
        self.profile_patch.stop()
        self.folder.cleanup()

    def complete_job(self, _title, worker, done):
        done(worker(lambda line: self.app.queue.put(("log", line))))

    @staticmethod
    def organized(doc, *_args, **_kwargs):
        doc.ai_import = {"status": "AI 已完成", "model": "import-model", "summary": "发现购买方名称差异，保留原值供人工核对。",
            "fields": [{"page": 1, "field": "buyer_name", "label": "购买方名称", "original": "本地原值公司",
                "suggested": "模型建议公司", "evidence": "购买方名称：模型建议公司", "action": "保留原值，差异待复核"}], "reason": ""}
        return doc

    def test_missing_configuration_opens_settings_before_file_selection_or_request(self):
        self.app.api_key.set("")
        with patch("ui.filedialog.askopenfilenames") as choose, \
                patch("ai_import.organize_document") as organize, \
                patch("ui.messagebox.showinfo") as info, \
                patch.object(self.app, "advanced_dialog") as settings:
            self.app.import_documents(use_ai=True)
        choose.assert_not_called()
        organize.assert_not_called()
        info.assert_called_once()
        settings.assert_called_once()
        self.assertEqual(self.app.current.documents, [])

    def test_ai_batches_extract_first_use_selected_model_and_keep_current_step_and_history(self):
        original = Document("原有材料.pdf")
        self.app.current.documents.append(original)
        self.app._refresh_current()
        self.app.show_step(1)
        order = []

        def extract(path):
            order.append(("extract", path))
            return Document(path, text="已识别原文")

        def organize(doc, *args, **kwargs):
            order.append(("organize", doc.path))
            return self.organized(doc, *args, **kwargs)

        with patch("ui.filedialog.askopenfilenames", side_effect=[("第一批.png",), ("第二批.jpg",)]), \
                patch("extractor.extract_document", side_effect=extract), \
                patch("ai_import.organize_document", side_effect=organize) as organize_mock, \
                patch.object(self.app, "_start_job", side_effect=self.complete_job):
            self.app.import_documents(use_ai=True)
            first_result = self.app.current.documents[1].ai_import
            self.app.import_documents(use_ai=True)
        self.assertEqual(order, [("extract", "第一批.png"), ("organize", "第一批.png"),
                                 ("extract", "第二批.jpg"), ("organize", "第二批.jpg")])
        self.assertEqual([doc.path for doc in self.app.current.documents], ["原有材料.pdf", "第一批.png", "第二批.jpg"])
        self.assertIs(self.app.current.documents[0], original)
        self.assertEqual(self.app.current.documents[1].ai_import, first_result)
        self.assertEqual(self.app.notebook.index(self.app.notebook.select()), 1)
        for call in organize_mock.call_args_list:
            self.assertEqual(call.kwargs["base_url"], "https://models.example.test/v1")
            self.assertEqual(call.kwargs["model"], "import-model")
            self.assertEqual(call.kwargs.get("api_key", call.args[1] if len(call.args) > 1 else None), "AI_IMPORT_UI_SECRET")
        self.assertIn("AI 已完成", self.app.ai_import_state.get())
        self.assertIn("开始整理", self.app.ai_import_log.get("1.0", "end"))
        self.assertNotIn("AI_IMPORT_UI_SECRET", self.app.ai_import_log.get("1.0", "end"))

    def test_organize_existing_material_uses_saved_fields_and_invalidates_old_audit(self):
        invoice = Invoice("已有发票.pdf", fields={"buyer_name": "初始值", "total": "100.00"})
        invoice.edit("buyer_name", "人工确认公司")
        doc = Document(invoice.source, category="发票", invoices=[invoice], text="可读取的原文")
        self.app.current.documents.append(doc)
        self.app.current.checks = [Check("old", "旧审核", PASS)]
        self.app.current.ai_summary = "旧审核摘要"
        self.app._refresh_current()
        with patch("ui.filedialog.askopenfilenames") as choose, \
                patch("extractor.extract_document") as extract, \
                patch("ai_import.organize_document", side_effect=self.organized) as organize, \
                patch.object(self.app, "_start_job", side_effect=self.complete_job):
            self.app.organize_existing_documents()
        choose.assert_not_called()
        extract.assert_not_called()
        self.assertIs(organize.call_args.args[0], doc)
        self.assertEqual(invoice.fields["buyer_name"], "人工确认公司")
        self.assertEqual(len(invoice.edits), 1)
        self.assertEqual(self.app.current.checks, [])
        self.assertEqual(self.app.current.ai_summary, "")
        self.assertEqual(self.app.current.documents, [doc])

    def test_backend_failure_and_empty_result_keep_material_and_show_incomplete_state(self):
        for case in ("unexpected_error", "empty_model_response"):
            with self.subTest(case=case):
                self.app.current.documents.clear()
                self.app._refresh_current()
                target = "ai_import.organize_document" if case == "unexpected_error" else "ai_import.call_model"
                effect = {"side_effect": RuntimeError("模型暂不可用")} if case == "unexpected_error" else {"return_value": {}}
                doc = Document("未完成.png", category="发票", text="购买方名称：模型建议公司", pages=1,
                               invoices=[Invoice("未完成.png", fields={"buyer_name": "本地原值公司"})])
                with patch("ui.filedialog.askopenfilenames", return_value=("未完成.png",)), \
                        patch("extractor.extract_document", return_value=doc), \
                        patch(target, **effect), \
                        patch.object(self.app, "_start_job", side_effect=self.complete_job):
                    self.app.import_documents(use_ai=True)
                self.assertEqual(len(self.app.current.documents), 1)
                self.assertEqual(self.app.current.documents[0].path, "未完成.png")
                self.assertEqual(self.app.current.documents[0].text, "购买方名称：模型建议公司")
                self.assertEqual(self.app.current.documents[0].invoices[0].fields["buyer_name"], "本地原值公司")
                self.assertNotEqual(self.app.current.documents[0].ai_import.get("status"), "AI 已完成")
                self.assertRegex(self.app.ai_import_state.get(), "未完成|失败")

    def test_workbench_shows_model_evidence_and_project_roundtrip_preserves_it_without_key(self):
        doc = self.organized(Document("有差异的发票.pdf", category="发票", text="购买方名称：模型建议公司"))
        self.app.current.documents.append(doc)
        self.app._refresh_current()
        rows = self.app.ai_import_tree.get_children()
        self.assertTrue(rows)
        self.app.ai_import_tree.selection_set(rows[0])
        self.app._show_ai_import_detail()
        shown = " ".join(str(self.app.ai_import_tree.item(row, "values")) for row in rows)
        shown += self.app.ai_import_detail.get("1.0", "end")
        for value in ("本地原值公司", "模型建议公司", "购买方名称：模型建议公司"):
            self.assertIn(value, shown)
        project = Path(self.folder.name) / "AI 整理项目.json"
        with patch("ui.filedialog.asksaveasfilename", return_value=str(project)):
            self.app.save_project()
        payload = project.read_text(encoding="utf-8")
        self.assertNotIn("AI_IMPORT_UI_SECRET", payload)
        self.assertNotIn("api_key", payload)
        self.assertEqual(json.loads(payload)["applications"][0]["documents"][0]["ai_import"], doc.ai_import)
        with patch("ui.filedialog.askopenfilename", return_value=str(project)), \
                patch("ui.messagebox.askyesno", return_value=True):
            self.app.open_project()
        self.assertEqual(self.app.current.documents[0].ai_import, doc.ai_import)
        self.assertTrue(self.app.ai_import_tree.get_children())

    def test_busy_disables_ai_actions_and_does_not_start_or_open_dialogs(self):
        self.app._set_busy(True)
        self.assertTrue(self.app.ai_import_button.instate(["disabled"]))
        self.assertTrue(self.app.ai_existing_button.instate(["disabled"]))
        with patch("ui.filedialog.askopenfilenames") as choose, \
                patch("ai_import.organize_document") as organize, \
                patch.object(self.app, "_start_job") as start, \
                patch.object(self.app, "advanced_dialog") as settings:
            self.app.import_documents(use_ai=True)
            self.app.organize_existing_documents()
        choose.assert_not_called()
        organize.assert_not_called()
        start.assert_not_called()
        settings.assert_not_called()

    def test_regular_import_stays_local_even_with_valid_model_configuration(self):
        self.app.use_ai.set(True)
        with patch("ui.filedialog.askopenfilenames", return_value=("普通导入.png",)), \
                patch("extractor.extract_document", return_value=Document("普通导入.png", text="本地识别原文")) as extract, \
                patch("ai_import.organize_document") as organize, \
                patch.object(self.app, "_start_job", side_effect=self.complete_job):
            self.app.import_documents()
        extract.assert_called_once_with("普通导入.png")
        organize.assert_not_called()
        self.assertEqual(self.app.current.documents[0].text, "本地识别原文")


if __name__ == "__main__":
    unittest.main()
