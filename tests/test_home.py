import json
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from models import Application, Document, Invoice
from ui import ReimbursementApp


class HomeNavigationTests(unittest.TestCase):
    def setUp(self):
        self.app = ReimbursementApp()
        self.app.withdraw()
        self.app.update_idletasks()
        self.folder = TemporaryDirectory()

    def tearDown(self):
        self.app.update_idletasks()
        self.app.destroy()
        self.folder.cleanup()

    def test_initial_home_shows_two_entries_and_hides_business_navigation(self):
        self.assertTrue(self.app.home_visible)
        self.assertIn("主页", self.app.nav_buttons)
        self.assertTrue(self.app.top_nav.winfo_manager())
        self.assertTrue(self.app.welcome.winfo_manager())
        self.assertFalse(self.app.work_area.winfo_manager())
        self.assertFalse(self.app.sidebar.winfo_manager())
        self.assertEqual(self.app.home_new_button.cget("text"), "开始")
        self.assertEqual(self.app.home_continue_button.cget("text"), "打开")
        self.assertFalse(self.app.home_open_button.winfo_manager())
        self.assertTrue(all(not button.winfo_manager() for button in self.app.nav_buttons.values()))

    def test_home_and_resume_keep_invoice_edits_current_application_and_step(self):
        self.app.new_application()
        self.assertFalse(self.app.home_visible)
        self.assertTrue(all(button.winfo_manager() for button in self.app.nav_buttons.values()))
        current = self.app.current
        invoice = Invoice("测试原票.pdf", fields={"buyer_name": "原始名称"})
        current.documents.append(Document(invoice.source, "发票", [invoice], pages=1))
        self.app._refresh_current()
        self.app.show_step(1)
        selected_tab = self.app.notebook.select()
        self.app.field_vars["buyer_name"].set("修改后名称")
        self.assertTrue(self.app.fields_dirty)

        self.app.show_home()

        self.assertTrue(self.app.home_visible)
        self.assertEqual(self.app.current_id, current.id)
        self.assertEqual(invoice.fields["buyer_name"], "修改后名称")
        self.assertEqual(invoice.edits[-1]["after"], "修改后名称")
        self.assertEqual(self.app.home_continue_button.cget("text"), "继续")
        self.assertTrue(self.app.home_open_button.winfo_manager())
        self.assertTrue(all(not button.winfo_manager() for button in self.app.nav_buttons.values()))
        history = list(invoice.edits)
        self.app.home_continue_button.invoke()
        self.assertFalse(self.app.home_visible)
        self.assertEqual(self.app.notebook.select(), selected_tab)
        self.assertEqual(self.app.field_vars["buyer_name"].get(), "修改后名称")
        self.assertEqual(invoice.edits, history)
        self.assertIs(self.app.current, current)

    def test_compact_nav_folds_secondary_entries_and_pins_model_chip(self):
        self.app.new_application()
        self.app._nav_density = "compact"
        self.app._layout_nav()
        for label in self.app.nav_collapsed:
            self.assertEqual(self.app.nav_buttons[label].winfo_manager(), "", f"{label} 应收进更多菜单")
        self.assertEqual(self.app.nav_overflow.winfo_manager(), "pack")
        self.assertEqual(self.app.model_chip.winfo_manager(), "pack", "接入方式必须始终可见")
        self.app._nav_density = "full"
        self.app._layout_nav()
        self.assertTrue(all(button.winfo_manager() for button in self.app.nav_buttons.values()))
        self.assertEqual(self.app.nav_overflow.winfo_manager(), "")

    def test_failed_flush_and_busy_work_do_not_navigate(self):
        self.app.new_application()
        with patch.object(self.app, "_flush_edits", return_value=False):
            self.app.show_home()
        self.assertFalse(self.app.home_visible)
        self.app.busy = True
        self.app.show_home()
        self.assertFalse(self.app.home_visible)
        self.app.busy = False
        self.app.show_home()
        self.app.busy = True
        self.app.resume_current()
        self.assertTrue(self.app.home_visible)
        self.app.busy = False

    def test_refreshing_current_does_not_leave_home(self):
        self.app.new_application()
        current_id = self.app.current_id
        self.app.show_home()
        self.app._refresh_current()
        self.app.update_idletasks()
        self.assertTrue(self.app.home_visible)
        self.assertEqual(self.app.current_id, current_id)
        self.assertTrue(self.app.welcome.winfo_manager())
        self.assertFalse(self.app.work_area.winfo_manager())

    def test_open_project_cancel_success_and_empty_project_route_correctly(self):
        self.app.new_application()
        current_id = self.app.current_id
        self.app.show_home()
        with patch("ui.filedialog.askopenfilename", return_value=""):
            self.app.open_project()
        self.assertTrue(self.app.home_visible)
        self.assertEqual(self.app.current_id, current_id)

        path = Path(self.folder.name) / "主页路由项目.json"
        payload = {"version": 1, "policy": asdict(self.app.policy),
                   "applications": [Application("HOME-OPEN", applicant="测试申请人").to_dict()]}
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        with patch("ui.filedialog.askopenfilename", return_value=str(path)), \
                patch("ui.messagebox.askyesno", return_value=True):
            self.app.open_project()
        self.assertFalse(self.app.home_visible)
        self.assertEqual(self.app.current_id, "HOME-OPEN")
        self.assertTrue(self.app.work_area.winfo_manager())

        payload["applications"] = []
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        with patch("ui.filedialog.askopenfilename", return_value=str(path)), \
                patch("ui.messagebox.askyesno", return_value=True):
            self.app.open_project()
        self.assertTrue(self.app.home_visible)
        self.assertIsNone(self.app.current)
        self.assertTrue(self.app.welcome.winfo_manager())
        self.assertFalse(self.app.work_area.winfo_manager())

    def test_show_step_returns_to_workspace_only_when_current_exists(self):
        self.app.show_step(2)
        self.assertTrue(self.app.home_visible)
        self.app.new_application()
        self.app.show_home()
        self.app.show_step(2)
        self.assertFalse(self.app.home_visible)
        self.assertEqual(self.app.notebook.index(self.app.notebook.select()), 2)
        self.assertTrue(self.app.work_area.winfo_manager())

    def test_continue_home_opens_project_or_resumes_current_step(self):
        with patch.object(self.app, "open_project") as open_project:
            self.app.continue_home()
        open_project.assert_called_once_with()
        self.assertTrue(self.app.home_visible)

        self.app.new_application()
        current_id = self.app.current_id
        self.app.show_step(2)
        selected_tab = self.app.notebook.select()
        self.app.show_home()
        with patch.object(self.app, "open_project") as open_project:
            self.app.continue_home()
        open_project.assert_not_called()
        self.assertFalse(self.app.home_visible)
        self.assertEqual(self.app.current_id, current_id)
        self.assertEqual(self.app.notebook.select(), selected_tab)

    def test_continue_home_busy_and_failed_flush_do_not_navigate_or_open(self):
        self.app.busy = True
        with patch.object(self.app, "open_project") as open_project:
            self.app.continue_home()
        open_project.assert_not_called()
        self.app.busy = False
        with patch.object(self.app, "_flush_edits", return_value=False), \
                patch.object(self.app, "open_project") as open_project:
            self.app.continue_home()
        open_project.assert_not_called()

        self.app.new_application()
        current_id = self.app.current_id
        self.app.show_home()
        self.app.busy = True
        self.app.continue_home()
        self.assertTrue(self.app.home_visible)
        self.assertEqual(self.app.current_id, current_id)
        self.app.busy = False
        with patch.object(self.app, "_flush_edits", return_value=False):
            self.app.continue_home()
        self.assertTrue(self.app.home_visible)
        self.assertEqual(self.app.current_id, current_id)

    def test_open_other_project_cancel_invalid_and_declined_replace_preserve_home(self):
        self.app.new_application()
        current = self.app.current
        self.app.show_step(2)
        selected_tab = self.app.notebook.select()
        self.app.show_home()
        path = Path(self.folder.name) / "待打开项目.json"
        payload = {"version": 1, "policy": asdict(self.app.policy),
                   "applications": [Application("OTHER-HOME").to_dict()]}
        for case in ("cancel", "invalid", "decline"):
            with self.subTest(case=case):
                path.write_text("{损坏项目" if case == "invalid" else json.dumps(payload), encoding="utf-8")
                with patch("ui.filedialog.askopenfilename", return_value="" if case == "cancel" else str(path)), \
                        patch("ui.messagebox.showerror") as showerror, \
                        patch("ui.messagebox.askyesno", return_value=False):
                    self.app.home_open_button.invoke()
                self.assertEqual(showerror.call_count, int(case == "invalid"))
                self.assertTrue(self.app.home_visible)
                self.assertIs(self.app.current, current)
                self.assertEqual(self.app.apps, [current])
                self.assertEqual(self.app.notebook.select(), selected_tab)
                self.assertEqual(self.app.home_continue_button.cget("text"), "继续")

    def test_demo_replaces_only_the_untouched_draft_started_from_home(self):
        self.app.home_new_button.invoke()
        with patch.object(self.app, "_start_job") as start_job:
            self.app.load_demo()
        demo = [Application(f"DEMO-{i:02}") for i in range(10)]
        start_job.call_args.args[2](demo)
        self.assertEqual(len(self.app.apps), 10)
        self.assertEqual(self.app.apps, demo)

    def test_demo_preserves_edited_drafts_and_blank_objects_from_old_projects(self):
        for case in ("applicant", "date", "attachment", "old_project"):
            with self.subTest(case=case):
                self.app.apps = []
                self.app.current_id = None
                self.app.new_application()
                draft = self.app.current
                if case == "applicant":
                    draft.applicant = "已填写申请人"
                elif case == "date":
                    draft.submitted_on = "2020-01-01"
                elif case == "attachment":
                    draft.documents.append(Document("付款凭证.pdf", category="付款凭证"))
                else:
                    draft = Application.from_dict(draft.to_dict())
                    self.app.apps = [draft]
                self.app._refresh_list()
                self.app._refresh_current()
                with patch.object(self.app, "_start_job") as start_job:
                    self.app.load_demo()
                start_job.call_args.args[2]([Application(f"DEMO-{i:02}") for i in range(10)])
                self.assertEqual(len(self.app.apps), 11)
                self.assertTrue(any(item is draft for item in self.app.apps))


if __name__ == "__main__":
    unittest.main()
