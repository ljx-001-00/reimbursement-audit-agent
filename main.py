"""Desktop and verification entry points."""
import argparse
import os
import sys
from pathlib import Path
import traceback


def _record_error(exc_type, exc, tb, secret=""):
    from datetime import datetime
    detail = "".join(traceback.format_exception(exc_type, exc, tb))
    for value in (secret, os.environ.get("REIMBURSEMENT_API_KEY", ""), os.environ.get("GLM_API_KEY", "")):
        if value:
            detail = detail.replace(value, "[密钥已隐藏]")
    app_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    paths = [app_dir / "output" / "启动错误.log",
             Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ReimbursementAudit" / "logs" / "启动错误.log"]
    for path in paths:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as stream:
                stream.write(f"\n{datetime.now():%Y-%m-%d %H:%M:%S}\n{detail}")
            return path
        except OSError:
            if path == paths[-1]:
                raise


def main():
    parser = argparse.ArgumentParser(description="报销单据智能校验智能体")
    parser.add_argument("--self-test", metavar="输出目录", help="运行验收并生成报告，不打开窗口")
    parser.add_argument("--gui-smoke", metavar="截图前缀", help="打开演示窗口、截图并检查后退出")
    args = parser.parse_args()
    if args.self_test:
        from verification import run_verification
        result = run_verification(args.self_test)
        if result is not None:
            print(result)
        return
    if args.gui_smoke:
        from ui import ReimbursementApp
        window = ReimbursementApp()
        window.run_smoke(Path(args.gui_smoke))
        window.mainloop()
        return
    try:
        from ui import ReimbursementApp
        window = ReimbursementApp()
        def callback_error(exc_type, exc, tb):
            from tkinter import messagebox
            path = _record_error(exc_type, exc, tb, window.api_key.get())
            messagebox.showerror("操作未完成", f"本次操作未完成，错误已记录。\n请将日志提供给开发者：\n{path}", parent=window)
        window.report_callback_exception = callback_error
        window.mainloop()
    except Exception as exc:
        path = _record_error(type(exc), exc, exc.__traceback__)
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, f"程序暂时无法启动，请查看错误日志：\n{path}", "报销审核", 0x10)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
