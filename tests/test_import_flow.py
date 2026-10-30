import unittest
from unittest.mock import patch

from models import Document
from ui import ReimbursementApp


class FileImportFlowTests(unittest.TestCase):
    def setUp(self):
        self.app = ReimbursementApp()
        self.app.withdraw()
        self.app.new_application()

    def tearDown(self):
        self.app.update_idletasks()
        self.app.destroy()

    @staticmethod
    def complete_job(_title, worker, done):
        done(worker(lambda _message: None))

    def test_multiple_batches_and_partial_failures_keep_import_step_and_prior_files(self):
        original = Document("原有发票.pdf", category="发票")
        self.app.current.documents.append(original)
        self.app._refresh_current()
        with patch("ui.filedialog.askopenfilenames", side_effect=[("新增一.png", "损坏.jpg"), ("新增二.jpg",)]), \
                patch("extractor.extract_document", side_effect=[Document("新增一.png"), ValueError("图片损坏"), Document("新增二.jpg")]), \
                patch.object(self.app, "_start_job", side_effect=self.complete_job):
            for count in (3, 4):
                self.app.import_documents()
                self.assertEqual(self.app.notebook.index(self.app.notebook.select()), 0)
                self.assertEqual(len(self.app.current.documents), count)
                self.assertIs(self.app.current.documents[0], original)
        self.assertEqual([doc.path for doc in self.app.current.documents],
                         ["原有发票.pdf", "新增一.png", "损坏.jpg", "新增二.jpg"])
        self.assertEqual(self.app.current.documents[2].error, "图片损坏")
        self.assertEqual(len(self.app.doc_tree.get_children()), 4)
        self.app.next_step()
        self.assertEqual(self.app.notebook.index(self.app.notebook.select()), 1)

    def test_import_from_other_steps_preserves_current_step(self):
        with patch("ui.filedialog.askopenfilenames", return_value=("附件.png",)), \
                patch("extractor.extract_document", side_effect=lambda path: Document(path, error="识别失败，需人工确认")), \
                patch.object(self.app, "_start_job", side_effect=self.complete_job):
            for index in (1, 2, 3):
                with self.subTest(step=index):
                    self.app.show_step(index)
                    self.app.import_documents()
                    self.assertEqual(self.app.notebook.index(self.app.notebook.select()), index)
        self.assertEqual(len(self.app.current.documents), 3)

    def test_cancel_file_selection_keeps_files_and_step(self):
        original = Document("已添加图片.png")
        self.app.current.documents.append(original)
        self.app._refresh_current()
        self.app.show_step(2)
        with patch("ui.filedialog.askopenfilenames", return_value=()), \
                patch.object(self.app, "_start_job") as start_job:
            self.app.import_documents()
        start_job.assert_not_called()
        self.assertEqual(self.app.current.documents, [original])
        self.assertEqual(self.app.notebook.index(self.app.notebook.select()), 2)


if __name__ == "__main__":
    unittest.main()
