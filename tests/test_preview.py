import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from PIL import Image

from models import ROOT, Application, Document, Invoice
from preview import DocumentPreview
from ui import ReimbursementApp


class OriginalPreviewTests(unittest.TestCase):
    def setUp(self):
        self.app = ReimbursementApp()
        self.app.withdraw()
        self.app.update_idletasks()
        self.viewer = None
        self.folder = TemporaryDirectory()

    def tearDown(self):
        if self.viewer is not None and self.viewer.winfo_exists():
            self.viewer.close()
        self.app.update_idletasks()
        self.app.destroy()
        self.folder.cleanup()

    def test_image_can_be_viewed_zoomed_and_closed_without_changing_original(self):
        path = Path(self.folder.name) / "中文原图.png"
        Image.new("RGB", (1600, 1000), "#1278ab").save(path)
        before = hashlib.sha256(path.read_bytes()).digest()
        self.viewer = DocumentPreview(self.app, path)
        self.assertEqual(self.viewer.image_size, (1600, 1000))
        self.assertGreater(self.viewer.photo.width(), 600)
        self.assertEqual(self.viewer.page_count, 1)
        self.assertEqual(str(self.viewer.next_button.cget("state")), "disabled")
        zoom = self.viewer.zoom
        self.viewer.change_zoom(1.25)
        self.assertGreater(self.viewer.zoom, zoom)
        self.viewer.fit_page()
        self.assertTrue(self.viewer.fitted)
        self.viewer.close()
        self.assertIsNone(self.viewer.original)
        self.assertIsNone(self.viewer.photo)
        self.assertEqual(hashlib.sha256(path.read_bytes()).digest(), before)

    def test_pdf_initial_page_navigation_and_invalid_page_preserve_current_image(self):
        path = ROOT / "data/supplemental/多页两张发票.pdf"
        self.viewer = DocumentPreview(self.app, path, initial_page=2)
        self.assertEqual(self.viewer.page_number, 2)
        self.assertEqual(self.viewer.page_count, 2)
        self.assertGreater(self.viewer.photo.width(), 600)
        self.viewer.show_page(1)
        self.assertEqual(self.viewer.page_number, 1)
        photo = self.viewer.photo
        with self.assertRaises(ValueError):
            self.viewer.show_page(3)
        self.assertEqual(self.viewer.page_number, 1)
        self.assertIs(self.viewer.photo, photo)
        self.viewer.change_zoom(1000)
        self.assertLessEqual(self.viewer.photo.width()*self.viewer.photo.height(), 24_000_000)
        self.viewer.close()
        self.assertIsNone(self.viewer.pdf)

    def test_confirmation_preview_uses_selected_invoice_not_stale_attachment_selection(self):
        pdf = ROOT / "data/supplemental/多页两张发票.pdf"
        attachment = ROOT / "data/supplemental/付款凭证.pdf"
        documents = [Document(str(attachment), "付款凭证"),
                     Document(str(pdf), "发票", [Invoice(str(pdf), page=1), Invoice(str(pdf), page=2)], pages=2)]
        app = Application("PREVIEW", documents=documents)
        self.app.apps = [app]
        self.app.current_id = app.id
        self.app._refresh_list()
        self.app._refresh_current()
        self.app.doc_tree.selection_set("0")
        self.app.invoice_combo.current(1)
        self.app.preview_original(from_invoice=True)
        self.viewer = self.app.preview_window
        self.assertEqual(self.viewer.path, pdf.resolve())
        self.assertEqual(self.viewer.page_number, 2)
        previous = self.viewer
        self.app.preview_original()
        self.viewer = self.app.preview_window
        self.assertEqual(self.viewer.path, attachment.resolve())
        self.assertEqual(self.viewer.page_number, 1)
        self.assertFalse(previous.winfo_exists())
        self.assertIsNone(previous.pdf)

    def test_missing_corrupt_and_unsupported_files_show_clear_errors(self):
        with self.assertRaises(FileNotFoundError):
            DocumentPreview(self.app, Path(self.folder.name)/"已删除.png")
        path = Path(self.folder.name)/"无效.txt"
        path.write_text("not an image", encoding="utf-8")
        with self.assertRaises(ValueError):
            DocumentPreview(self.app, path)
        path = Path(self.folder.name)/"损坏.png"
        path.write_bytes(b"not a PNG")
        with self.assertRaises(OSError):
            DocumentPreview(self.app, path)

    def test_oversized_image_is_rejected_before_decode(self):
        path = Path(self.folder.name)/"大图.png"
        path.write_bytes(b"header")
        with patch("preview.Image.open") as opening:
            source = opening.return_value.__enter__.return_value
            source.width, source.height = 10000, 9000
            with self.assertRaisesRegex(ValueError, "8500"):
                DocumentPreview(self.app, path)
            source.copy.assert_not_called()

    def test_empty_selection_explains_what_to_select(self):
        with patch("ui.messagebox.showinfo") as info:
            self.app.preview_original()
        info.assert_called_once()
        self.assertIsNone(self.app.preview_window)


if __name__ == "__main__":
    unittest.main()
