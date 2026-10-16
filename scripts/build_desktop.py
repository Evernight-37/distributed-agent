"""Maintainer build: portable Windows app, including an isolated Python worker runtime."""

import hashlib
import shutil
import subprocess
import sys
import urllib.request
import zipfile
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = PROJECT_ROOT / "src" / "agent_harness"
WEB_SOURCE = PROJECT_ROOT / "assets" / "web"
PYTHON_VERSION = "3.14.7"
URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip"
# SHA-256 published on the Python.org release page; never run an unverified download.
SHA256 = "d297e5ff019966817ad8502465176139f2d3d840fa4ed84b13bed399a6ab1f15"
CLIENT_FILES = (
    "common.py",
    "pull_worker.py",
    "local_panel.py",
    "desktop_service.py",
    "desktop_runtime.py",
    "service_install.py",
    "host_control.py",
    "app_lifecycle.py",
    "worker.py",
    "jobrunner.py",
    "warm_worker.py",
    "appserver_client.py",
    "release_info.py",
    "panel.py",
    "broker.py",
    "collaboration_store.py",
    "coordinator.py",
    "project_memory.py",
    "runtime_paths.py",
    "account_identity.py",
    "account_backend.py",
    "owner_backend.py",
    "owner_worker.py",
    "app_bridge.py",
    "production_app.py",
    "team_service.py",
    "mcp_server.py",
    "mcp_adapter.py",
)
CRYPTOGRAPHY_VERSION = "46.0.3"


def application_version(source=PROJECT_ROOT):
    version = (Path(source) / "VERSION").read_text(encoding="utf-8").strip()
    if not version or any(character.isspace() for character in version):
        raise RuntimeError("VERSION must contain one non-empty version token")
    return version


def write_release_metadata(destination, source=PROJECT_ROOT):
    destination = Path(destination)
    version = application_version(source)
    (destination / "VERSION").write_text(version + "\n", encoding="utf-8")
    (destination / "app-manifest.json").write_text(
        json.dumps(
            {
                "format": 1,
                "name": "AgentHarness",
                "version": version,
                "python_runtime": PYTHON_VERSION,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return version


def getting_started_text(version):
    return f"Agent Harness {version}\n双击 AgentHarness.exe。\n账户、接收设置、MCP 授权和团队数据库统一保存在 %LOCALAPPDATA%\\AgentHarness\\data；团队数据库位于其中的 team-service。\n首次使用点击“创建账户”；自己的另一台电脑点击“关联我的另一台电脑”，由已登录设备批准。\n控制台管理所有关联机器上的员工。凭服务地址和项目邀请代码加入其他团队，不用重新创建身份。\n进入项目后派遣自己的员工，群主设置管理员与邀请成员。\n设置页管理设备、后台接收和服务连接。\n在“设置 → 在 Codex 中使用协作”点“连接到 Codex”后，可以在普通 Codex 对话里查看 Harness 项目、发消息和给自己的员工派异步任务。\nCodex 使用单独的受限身份，不能邀请成员、管理别人的机器或批准命令；断开不会删除项目。\n首次运行 agent 或连接 Codex 前，每台机器需要已有可用的 Codex 登录。\n账户身份保存在当前 Windows 用户目录；项目文件不会自动同步。\n升级或移动应用目录不会改变持久数据位置；卸载前如需保留数据，请备份 %LOCALAPPDATA%\\AgentHarness\\data。\n请保留整个发布目录，不要只复制 exe。\n"


def main(output_root=None):
    if sys.platform != "win32":
        raise RuntimeError("Build this Windows app on Windows")
    output_root = Path(output_root).resolve() if output_root else PROJECT_ROOT / "dist"
    missing = [name for name in CLIENT_FILES if not (APP_SOURCE / name).is_file()]
    if missing:
        raise RuntimeError("Missing production sources: " + ", ".join(missing))
    cache = PROJECT_ROOT / "build" / "downloads"
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / f"python-{PYTHON_VERSION}-embed-amd64.zip"
    if not archive.exists():
        with urllib.request.urlopen(URL, timeout=60) as response:
            archive.write_bytes(response.read())
    if hashlib.sha256(archive.read_bytes()).hexdigest() != SHA256:
        raise RuntimeError("Python runtime checksum mismatch")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--windowed",
            "--onedir",
            "--distpath",
            str(output_root),
            "--paths",
            str(APP_SOURCE),
            "--collect-all",
            "webview",
            "--collect-all",
            "pythonnet",
            "--collect-all",
            "clr_loader",
            "--collect-all",
            "cryptography",
            "--hidden-import",
            "webview.platforms.edgechromium",
            "--hidden-import",
            "clr",
            "--hidden-import",
            "webview.platforms.winforms",
            "--add-data",
            f"{WEB_SOURCE};web",
            "--name",
            "AgentHarness",
            str(APP_SOURCE / "production_app.py"),
        ],
        cwd=PROJECT_ROOT,
        check=True,
    )
    destination = output_root / "AgentHarness"
    runtime = destination / "runtime"
    runtime.mkdir(exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(runtime)
    packages = runtime / "site-packages"
    packages.mkdir(exist_ok=True)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--target",
            str(packages),
            "--only-binary=:all:",
            "--python-version",
            "3.14",
            "--implementation",
            "cp",
            "--abi",
            "cp314",
            f"cryptography=={CRYPTOGRAPHY_VERSION}",
        ],
        cwd=PROJECT_ROOT,
        check=True,
    )
    (runtime / "python314._pth").write_text(
        "python314.zip\n.\n../client\n./site-packages\nimport site\n", encoding="utf-8"
    )
    subprocess.run(
        [
            str(runtime / "python.exe"),
            "-c",
            "from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey; "
            'key=Ed25519PrivateKey.generate(); key.public_key().verify(key.sign(b"runtime-check"),b"runtime-check"); '
            'print("Worker runtime cryptography verified")',
        ],
        cwd=PROJECT_ROOT,
        check=True,
    )
    client = destination / "client"
    client.mkdir(exist_ok=True)
    for name in CLIENT_FILES:
        shutil.copyfile(APP_SOURCE / name, client / name)
    shutil.copyfile(PROJECT_ROOT / "VERSION", client / "VERSION")
    shutil.copytree(WEB_SOURCE, client / "web", dirs_exist_ok=True)
    app_version = write_release_metadata(destination)
    (destination / "开始使用.txt").write_text(getting_started_text(app_version), encoding="utf-8-sig")
    print(destination)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Build the portable Windows app")
    parser.add_argument("--output-root", help="Destination containing AgentHarness (default: dist)")
    args = parser.parse_args()
    main(args.output_root)
