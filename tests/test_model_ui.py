import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import patch

from model_settings import DEFAULT_BASE_URL, DEFAULT_MODEL
from tests.test_model_config import model_server, completion
from ui import ReimbursementApp


class ModelSettingsUITests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.path = Path(self.folder.name) / "model_profiles.json"
        self.path_patch = patch("model_settings.profile_path", return_value=self.path)
        self.env_patch = patch.dict(os.environ, {"REIMBURSEMENT_BASE_URL": DEFAULT_BASE_URL,
            "REIMBURSEMENT_MODEL": DEFAULT_MODEL, "REIMBURSEMENT_API_KEY": "", "GLM_API_KEY": ""})
        self.path_patch.start()
        self.env_patch.start()
        self.app = ReimbursementApp()
        self.app.withdraw()

    def tearDown(self):
        self.app.update_idletasks()
        self.app.destroy()
        self.path_patch.stop()
        self.env_patch.stop()
        self.folder.cleanup()

    @staticmethod
    def children(widget):
        for child in widget.winfo_children():
            yield child
            yield from ModelSettingsUITests.children(child)

    def dialog(self):
        self.app.advanced_dialog()
        popup = next(w for w in self.app.winfo_children()
                     if isinstance(w, tk.Toplevel) and not getattr(w, "assistant_window", False))
        widgets = list(self.children(popup))
        buttons = {w.cget("text"): w for w in widgets if isinstance(w, ttk.Button)}
        entries = sorted([w for w in widgets if w.winfo_class() == "TEntry"], key=lambda w: int(w.grid_info()["row"]))
        return popup, widgets, buttons, entries

    @staticmethod
    def fill(entries, values):
        for entry, value in zip(entries, values):
            entry.delete(0, "end")
            entry.insert(0, value)

    def test_add_save_apply_and_switch_profiles_keep_keys_out_of_files(self):
        _popup, widgets, buttons, entries = self.dialog()
        buttons["新增"].invoke()
        secret = "MODEL_UI_TEST_SECRET"
        self.fill(entries, ["自建模型", "http://127.0.0.1:11434/v1", "custom-local-model", secret])
        next(w for w in widgets if isinstance(w, ttk.Checkbutton)).invoke()
        buttons["保存配置"].invoke()
        payload = self.path.read_text(encoding="utf-8")
        self.assertNotIn(secret, payload)
        self.assertNotIn("api_key", payload)
        self.assertEqual(json.loads(payload)["active"], "自建模型")
        buttons["完成"].invoke()
        self.assertEqual(self.app.model_name.get(), "custom-local-model")
        self.assertEqual(self.app.api_key.get(), secret)
        self.assertTrue(self.app.use_ai.get())

        _popup, widgets, buttons, entries = self.dialog()
        choices = next(w for w in widgets if isinstance(w, ttk.Combobox))
        choices.set("智谱默认")
        choices.event_generate("<<ComboboxSelected>>")
        self.assertEqual(entries[-1].get(), "")
        buttons["完成"].invoke()
        self.assertEqual(self.app.model_name.get(), DEFAULT_MODEL)
        self.assertEqual(self.app.api_key.get(), "")

    def test_selected_model_reaches_audit_worker(self):
        self.app.new_application()
        self.app.model_base_url.set("http://localhost:11434/v1")
        self.app.model_name.set("local-model")
        self.app.use_ai.set(True)
        def complete(_title, worker, done):
            done(worker(lambda _line: None))
        with patch.object(self.app, "_start_job", side_effect=complete), patch("agent.run_audit") as audited:
            self.app.audit_current()
        self.assertEqual(audited.call_args.kwargs["base_url"], "http://localhost:11434/v1")
        self.assertEqual(audited.call_args.kwargs["model"], "local-model")
        self.assertTrue(audited.call_args.kwargs["use_ai"])

    def test_connection_button_uses_custom_endpoint_without_documents(self):
        _popup, _widgets, buttons, entries = self.dialog()
        self.app.new_application()
        self.app.current.applicant = "PRIVATE_APPLICATION_MARKER"
        with model_server([completion({"role": "assistant", "content": "连接成功"})]) as (base, requests):
            self.fill(entries, ["连接测试", base + "/v1", "custom-model", ""])
            def complete(_title, worker, done):
                done(worker(lambda _line: None))
            with patch.object(self.app, "_start_job", side_effect=complete):
                buttons["测试连接"].invoke()
        self.assertIn("已收到模型响应", self.app.status_text.get())
        self.assertEqual(len(requests), 1)
        self.assertNotIn("PRIVATE_APPLICATION_MARKER", requests[0]["body"].decode("utf-8"))
        self.assertEqual(json.loads(requests[0]["body"])["model"], "custom-model")


if __name__ == "__main__":
    unittest.main()
