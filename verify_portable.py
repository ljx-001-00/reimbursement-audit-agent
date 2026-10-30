"""Build-side checks for a relocated Windows folder release; never uses PATH for DLL lookup."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys


VC_DLLS = {"msvcp140.dll", "msvcp140_1.dll", "vcruntime140.dll", "vcruntime140_1.dll"}


def inspect_release(folder):
    import pefile  # Build dependency; not needed by the delivered executable.

    folder = Path(folder).resolve()
    files = [path for path in folder.rglob("*") if path.is_file()]
    by_name = {}
    for path in files:
        by_name.setdefault(path.name.lower(), []).append(path)
    # The bundled interpreter version is a build detail; accept any python3xx.dll.
    bundled_python = sorted(path.name for path in files
                            if path.parent.name == "_internal" and re.fullmatch(r"python3\d+\.dll", path.name, re.I))
    if not bundled_python:
        missing_python = ["python3xx.dll"]
    else:
        missing_python = []
    required = ["_tkinter.pyd", "tcl86t.dll", "tk86t.dll", "pdfium.dll",
                "onnxruntime.dll", "onnxruntime_pybind11_state.pyd", *sorted(VC_DLLS),
                "font.ttf", "FONT-LICENSE.txt", "det.onnx", "rec.onnx", "cls.onnx",
                "demo.json", "config.yaml", "default_models.yaml", "init.tcl", "tk.tcl"]
    missing_resources = [name for name in required if not by_name.get(name.lower())] + missing_python
    system = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    native = [path for path in files if path.suffix.lower() in {".exe", ".dll", ".pyd"}]
    missing_dlls, wrong_architecture, invalid_pe = [], [], []
    system_dlls, bundled_dlls = set(), set()
    for path in native:
        try:
            with pefile.PE(str(path), fast_load=True) as binary:
                if binary.FILE_HEADER.Machine != 0x8664:
                    wrong_architecture.append(str(path.relative_to(folder)))
                binary.parse_data_directories(directories=[
                    pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
                    pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"],
                ])
                imports = [*getattr(binary, "DIRECTORY_ENTRY_IMPORT", []),
                           *getattr(binary, "DIRECTORY_ENTRY_DELAY_IMPORT", [])]
                for imported in imports:
                    name = imported.dll.decode("ascii").lower()
                    if by_name.get(name):
                        bundled_dlls.add(name)
                    elif (name.startswith(("api-ms-", "ext-ms-")) or
                          (name not in VC_DLLS and (system / name).is_file())):
                        system_dlls.add(name)
                    else:
                        missing_dlls.append({"binary": str(path.relative_to(folder)), "dll": name})
        except (pefile.PEFormatError, OSError) as exc:
            invalid_pe.append({"binary": str(path.relative_to(folder)), "error": str(exc)})
    return {"passed": not any((missing_resources, missing_dlls, wrong_architecture, invalid_pe)),
            "native_files": len(native), "required_resources": len(required),
            "missing_resources": missing_resources, "missing_dlls": missing_dlls,
            "wrong_architecture": wrong_architecture, "invalid_pe": invalid_pe,
            "bundled_imports": sorted(bundled_dlls), "windows_imports": sorted(system_dlls),
            "lookup": "Only release files, Windows System32, and Windows API-set contracts; no PATH search."}


def clean_environment(output):
    env = dict(os.environ)
    for key in list(env):
        if (key.upper().startswith(("PYTHON", "REIMBURSEMENT_")) or
                key.upper() in {"VIRTUAL_ENV", "CONDA_PREFIX", "GLM_API_KEY", "TCL_LIBRARY", "TK_LIBRARY"}):
            del env[key]
    system = Path(env.get("SystemRoot", r"C:\Windows"))
    env["PATH"] = os.pathsep.join(str(path) for path in (system / "System32", system))
    for variable in ("APPDATA", "LOCALAPPDATA"):
        path = output / "isolated-profile" / variable
        path.mkdir(parents=True, exist_ok=True)
        env[variable] = str(path)
    return env


def run_release(folder, output, gui=False):
    executables = list(folder.glob("*.exe"))
    if len(executables) != 1:
        raise ValueError("分发目录顶层应恰有一个程序 exe")
    executable = executables[0]
    environment = clean_environment(output)
    work = output / "independent-working-directory"
    work.mkdir(parents=True, exist_ok=True)
    result = {"executable": str(executable), "working_directory": str(work),
              "path": environment["PATH"], "external_python_environment": False}
    with (output / "self-test-process.log").open("w", encoding="utf-8") as log:
        completed = subprocess.run([str(executable), "--self-test", str(output / "business")],
                                   cwd=work, env=environment, stdout=log, stderr=log,
                                   timeout=240, creationflags=subprocess.CREATE_NO_WINDOW)
    evidence = json.loads((output / "business" / "验收结果.json").read_text(encoding="utf-8"))
    result.update(exit_code=completed.returncode, frozen=evidence.get("frozen"),
                  passed_cases=evidence.get("passed"), total_cases=evidence.get("total"))
    result["passed"] = completed.returncode == 0 and evidence.get("frozen") is True and evidence.get("all_passed") is True
    if gui:
        with (output / "gui-smoke-process.log").open("w", encoding="utf-8") as log:
            completed = subprocess.run([str(executable), "--gui-smoke", str(output / "gui")],
                                       cwd=work, env=environment, stdout=log, stderr=log,
                                       timeout=240, creationflags=subprocess.CREATE_NO_WINDOW)
        gui_evidence = json.loads((output / "gui.json").read_text(encoding="utf-8"))
        result["gui_exit_code"] = completed.returncode
        result["gui"] = gui_evidence
        checks = gui_evidence.get("checks", {})
        result["passed"] = (result["passed"] and completed.returncode == 0 and bool(checks)
                            and all(value is True for value in checks.values()))
    return result


def main():
    parser = argparse.ArgumentParser(description="验证便携程序资源、DLL 及脱离开发环境后的真实业务流程")
    parser.add_argument("folder", type=Path, help="已迁移的完整程序目录")
    parser.add_argument("--output", type=Path, required=True, help="检查结果目录")
    parser.add_argument("--run", action="store_true", help="清理 Python 环境后执行 exe 业务验收")
    parser.add_argument("--gui", action="store_true", help="同时执行 exe GUI 冒烟验收")
    args = parser.parse_args()
    folder, output = args.folder.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    evidence = {"time": datetime.now().isoformat(timespec="seconds"), "folder": str(folder),
                "native": inspect_release(folder),
                "limits": "Runs on the current Windows host; not a clean virtual machine or a real external model connection test."}
    try:
        if args.run or args.gui:
            evidence["runtime"] = run_release(folder, output, args.gui)
        evidence["all_passed"] = evidence["native"]["passed"] and evidence.get("runtime", {"passed": True})["passed"]
    except Exception as exc:
        evidence.update(all_passed=False, runtime_error=f"{type(exc).__name__}: {exc}")
    path = output / "便携验收.json"
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(path)
    print("全部通过" if evidence["all_passed"] else "检查失败，请查看结果")
    return 0 if evidence["all_passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
