import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from main import _record_error


class PackagedLogTests(unittest.TestCase):
    def test_frozen_logs_use_executable_folder_and_redact_key(self):
        with TemporaryDirectory() as folder:
            exe = Path(folder) / "程序" / "报销审核智能体.exe"
            with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", str(exe)):
                path = _record_error(ValueError, ValueError("key=PACKAGED_TEST_KEY"), None, "PACKAGED_TEST_KEY")
            self.assertEqual(path, exe.parent / "output" / "启动错误.log")
            self.assertNotIn("PACKAGED_TEST_KEY", path.read_text(encoding="utf-8"))

    def test_readonly_application_uses_local_appdata(self):
        with TemporaryDirectory() as folder:
            exe = Path(folder) / "只读程序" / "报销审核智能体.exe"
            local = Path(folder) / "用户数据"
            mkdir = Path.mkdir
            def readonly(path, *args, **kwargs):
                if path == exe.parent / "output":
                    raise PermissionError("readonly")
                return mkdir(path, *args, **kwargs)
            with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", str(exe)), \
                    patch.dict(os.environ, {"LOCALAPPDATA": str(local)}), patch.object(Path, "mkdir", readonly):
                path = _record_error(ValueError, ValueError("test"), None)
            self.assertEqual(path, local / "ReimbursementAudit" / "logs" / "启动错误.log")
            self.assertTrue(path.is_file())


if __name__ == "__main__":
    unittest.main()
