from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import prepare_submission


class SourceSubmissionTests(unittest.TestCase):
    def test_source_has_required_resources_but_no_environment_or_credentials(self):
        included = {"main.py", "requirements.txt", "安装环境.ps1", ".gitignore",
                    "assets/ocr/rec.onnx", "assets/font.ttf", "data/demo.json", "tests/test_rules.py"}
        excluded = {".venv/Scripts/python.exe", "output/项目.json", "tmp/cache.txt", "报销审核.lnk",
                    ".env", ".env.local", "model_profiles.json", "assets/private.key", "data/.env",
                    "tests/__pycache__/test_rules.pyc"}
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for name in included | excluded:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("example", encoding="utf-8")
            with patch.object(prepare_submission, "ROOT", root):
                actual = {path.relative_to(root).as_posix() for path in prepare_submission.submission_files()}
            self.assertEqual(actual, included)
