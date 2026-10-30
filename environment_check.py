"""Check the source-release runtime without reading any API credentials."""
import argparse
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import struct
import sys

ROOT = Path(__file__).resolve().parent

# onnxruntime 的扩展模块依赖 VC++ 运行库。缺它时 pip 会显示安装成功、import 却失败，
# 而重装依赖并不能修好，所以这里给出确切的下一步。
VC_RUNTIME_HINT = (
    "onnxruntime 需要 Microsoft Visual C++ 2015–2022 x64 运行库（msvcp140.dll、vcruntime140.dll）。"
    "到 https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist 下载 x64 版本安装后重跑本检查；"
    "重装依赖不能解决这个问题。"
)


def check(quick=False):
    results = []

    def record(name, passed, detail):
        results.append(dict(name=name, passed=bool(passed), detail=str(detail)))

    # 依赖按 CPython 3.10 的 wheel 锁定，3.10～3.12 的 64 位解释器都能装齐。只认 3.12 会把
    # 与便携版同版本的 3.10 误判为不达标，让源码复现卡在第一步。
    record("Windows 64 位 Python 3.10～3.12", os.name == "nt"
           and (3, 10) <= sys.version_info[:2] <= (3, 12)
           and struct.calcsize("P") == 8, f"{sys.version.split()[0]}, {struct.calcsize('P') * 8} bit, {sys.platform}")
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        if "==" not in line or line.lstrip().startswith("#"):
            continue
        name, expected = line.strip().split("==", 1)
        try:
            actual = importlib.metadata.version(name)
            record(name, actual == expected, f"已安装 {actual}，要求 {expected}")
        except importlib.metadata.PackageNotFoundError:
            record(name, False, f"未安装，要求 {expected}")
    for relative in ("assets/font.ttf", "assets/FONT-LICENSE.txt", "assets/ocr/det.onnx",
                     "assets/ocr/rec.onnx", "assets/ocr/cls.onnx", "data/demo.json"):
        path = ROOT / relative
        record(relative, path.is_file() and path.stat().st_size > 0, "存在且非空" if path.is_file() else "缺失")
    modules = ("tkinter",) if quick else ("tkinter", "PIL.ImageTk", "pypdfium2", "rapidocr", "onnxruntime", "reportlab")
    dll_failure = False
    for name in modules:
        try:
            importlib.import_module(name)
            record(f"导入 {name}", True, "成功")
        except Exception as exc:
            record(f"导入 {name}", False, f"{type(exc).__name__}: {exc}")
            # 单独的 import 失败可能只是没装；带 DLL 字样的才是运行库问题，处置方式完全不同。
            if isinstance(exc, ImportError) and re.search(r"DLL load failed|DLL 初始化|找不到指定的模块", str(exc)):
                dll_failure = True
    if dll_failure:
        record("Microsoft Visual C++ 运行库", False, VC_RUNTIME_HINT)
    if not quick and all(item["passed"] for item in results):
        try:
            import tkinter as tk
            window = tk.Tk()
            window.withdraw()
            window.update_idletasks()
            window.destroy()
            record("Tk 桌面窗口", True, "成功创建与关闭测试窗口")
            import onnxruntime as ort
            for model in ("det", "rec", "cls"):
                session = ort.InferenceSession(str(ROOT / "assets" / "ocr" / f"{model}.onnx"),
                                               providers=["CPUExecutionProvider"])
                record(f"本地 OCR {model}", bool(session.get_inputs()), "CPU 模型加载成功")
                del session
        except Exception as exc:
            record("本地运行检查", False, f"{type(exc).__name__}: {exc}")
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="检查 Python、依赖、Tk、字体及本地 OCR 模型")
    parser.add_argument("--quick", action="store_true", help="仅检查启动所需版本、资源与 Tk 导入")
    parser.add_argument("--output", type=Path, help="另存不含密钥的 JSON 检查结果")
    args = parser.parse_args()
    checks = check(args.quick)
    passed = all(item["passed"] for item in checks)
    for item in checks:
        print(f"[{'通过' if item['passed'] else '失败'}] {item['name']}: {item['detail']}")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(dict(passed=passed, checks=checks), ensure_ascii=False, indent=2), encoding="utf-8")
    if passed:
        print("环境检查通过。")
    elif any(item["name"] == "Microsoft Visual C++ 运行库" for item in checks):
        print("环境检查未通过：依赖已装齐，缺的是 VC++ 运行库。请按上面的链接安装后重跑；"
              "此时重复执行安装脚本不会改善。")
    else:
        print("环境检查失败，请双击“安装环境.cmd”修复，或按上面的提示检查。")
    sys.exit(0 if passed else 1)
