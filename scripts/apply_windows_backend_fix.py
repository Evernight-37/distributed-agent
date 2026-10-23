"""Apply the verified 1.0.0 backend update without touching account data."""

import csv
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def apply_update(bundle, payload, manifest):
    bundle, payload = Path(bundle).resolve(), Path(payload).resolve()
    if not (bundle / "AgentHarness.exe").is_file() or not (bundle / "runtime/python.exe").is_file():
        raise ValueError("请选择包含 AgentHarness.exe、runtime 和 client 的完整 Windows 应用目录。")
    planned = []
    for record in manifest["files"]:
        relative = Path(record["path"])
        if relative.is_absolute() or ".." in relative.parts or relative.parts[0] != "client":
            raise ValueError("Invalid patch manifest path")
        target, source = bundle / relative, payload / relative
        if not target.resolve().is_relative_to(bundle) or target.is_symlink():
            raise ValueError("Refusing linked application files")
        if digest(source) != record["updated_sha256"]:
            raise ValueError("补丁文件校验失败，请重新解压补丁包。")
        current = digest(target)
        if current not in {record["original_sha256"], record["updated_sha256"]}:
            raise ValueError("应用文件版本不匹配，未修改任何文件：" + str(relative))
        if relative.suffix == ".py":
            compile(source.read_text(encoding="utf-8-sig"), str(relative), "exec")
        if current != record["updated_sha256"]:
            planned.append((relative, source, target))
    if not planned:
        return None
    backup = Path(tempfile.mkdtemp(prefix="group-fix-backup-" + datetime.now().strftime("%Y%m%d-"), dir=bundle))
    for relative, _, target in planned:
        saved = backup / relative
        saved.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, saved)
    with tempfile.TemporaryDirectory(prefix="group-fix-stage-", dir=bundle) as directory:
        staging = Path(directory)
        for relative, source, _ in planned:
            staged = staging / relative
            staged.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, staged)
        changed = []
        try:
            for relative, _, target in planned:
                os.replace(staging / relative, target)
                changed.append((relative, target))
        except Exception:
            for relative, target in reversed(changed):
                shutil.copy2(backup / relative, target)
            raise
    return backup


def main():
    if os.name != "nt":
        raise RuntimeError("此补丁仅用于 Windows 完整便携版，不能用于 Mac .app。")
    result = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq AgentHarness.exe", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
        errors="replace",
        check=True,
    )
    if any(row and row[0].lower() == "agentharness.exe" for row in csv.reader(result.stdout.splitlines())):
        raise RuntimeError("请先在 Agent Harness 中选择“完全退出”，再运行补丁。")
    with socket.socket() as probe:
        probe.settimeout(1)
        if probe.connect_ex(("127.0.0.1", 8765)) == 0:
            raise RuntimeError("本机 8765 端口仍有服务运行，请先完全退出协作服务后再更新。")
    root = Path(__file__).resolve().parent
    manifest = json.loads((root / "backend-fix-manifest.json").read_text(encoding="utf-8"))
    backup = apply_update(sys.argv[1], root / "backend-fix-payload", manifest)
    print("更新成功。原文件备份：" + str(backup) if backup else "已经是修复版，无需重复更新。")
    print("现在重新打开 AgentHarness.exe；账户、项目和历史记录保留。")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("更新未完成：" + str(error))
        sys.exit(1)
