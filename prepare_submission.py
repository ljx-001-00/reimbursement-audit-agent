"""Create the competition source ZIP from a deliberate allowlist, never a .venv."""
import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parent
ROOT_EXTENSIONS = {".py", ".md", ".txt", ".html", ".cmd", ".ps1", ".manifest"}
RESOURCE_DIRECTORIES = {"assets", "data", "tests"}


def submission_files():
    candidates = [path for path in ROOT.iterdir() if path.is_file()]
    for directory in RESOURCE_DIRECTORIES:
        candidates.extend((ROOT / directory).rglob("*"))
    for path in sorted(candidates):
        if not path.is_file():
            continue
        relative = path.relative_to(ROOT)
        if ("__pycache__" in relative.parts or path.suffix.lower() in {".pyc", ".lnk", ".log", ".key", ".pem"}
                or path.name == "model_profiles.json" or path.name.startswith(".env")):
            continue
        if len(relative.parts) == 1:
            if path.suffix.lower() in ROOT_EXTENSIONS or path.name == ".gitignore":
                yield path
        elif relative.parts[0] in RESOURCE_DIRECTORIES:
            if relative.parts[0] != "tests" or path.suffix == ".py":
                yield path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="生成比赛平台提交的源码 ZIP，不生成 exe、不包含密钥或运行环境")
    parser.add_argument("--output", type=Path, default=ROOT / "output" / "报销审核_比赛源码.zip")
    args = parser.parse_args()
    files = list(submission_files())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(args.output, "w", compression=ZIP_DEFLATED, compresslevel=6) as archive:
        for path in files:
            archive.write(path, Path("报销审核源码") / path.relative_to(ROOT))
    print(f"已生成源码资料包：{args.output.resolve()}")
    print(f"共 {len(files)} 个文件，{args.output.stat().st_size / 1024 / 1024:.1f} MB。")
    print("包含源码、锁定依赖、安装启动脚本、OCR 模型、字体、样例和测试。")
    print("不包含 .venv、output、tmp、快捷方式、用户模型配置或 API 密钥。")
