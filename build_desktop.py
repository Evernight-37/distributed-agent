"""Maintainer build: portable Windows app, including an isolated Python worker runtime."""
import hashlib
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

SOURCE = Path(__file__).resolve().parent
VERSION = '3.14.7'
URL = f'https://www.python.org/ftp/python/{VERSION}/python-{VERSION}-embed-amd64.zip'
# SHA-256 published on the Python.org release page; never run an unverified download.
SHA256 = 'd297e5ff019966817ad8502465176139f2d3d840fa4ed84b13bed399a6ab1f15'
CLIENT_FILES = ('common.py', 'pull_worker.py', 'local_panel.py', 'desktop_service.py',
                'worker.py', 'jobrunner.py', 'panel.py', 'broker.py', 'collaboration_store.py', 'coordinator.py', 'controller.py')


def main():
    if sys.platform != 'win32': raise RuntimeError('Build this Windows app on Windows')
    cache = SOURCE / 'build' / 'downloads'; cache.mkdir(parents=True, exist_ok=True)
    archive = cache / f'python-{VERSION}-embed-amd64.zip'
    if not archive.exists():
        with urllib.request.urlopen(URL, timeout=60) as response: archive.write_bytes(response.read())
    if hashlib.sha256(archive.read_bytes()).hexdigest() != SHA256: raise RuntimeError('Python runtime checksum mismatch')
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--windowed', '--onedir',
                    '--name', 'AgentHarness', str(SOURCE / 'desktop_app.py')], cwd=SOURCE, check=True)
    destination = SOURCE / 'dist' / 'AgentHarness'
    runtime = destination / 'runtime'; runtime.mkdir(exist_ok=True)
    with zipfile.ZipFile(archive) as bundle: bundle.extractall(runtime)
    (runtime / 'python314._pth').write_text('python314.zip\n.\n../client\n', encoding='utf-8')
    client = destination / 'client'; client.mkdir(exist_ok=True)
    for name in CLIENT_FILES: shutil.copyfile(SOURCE / name, client / name)
    shutil.copytree(SOURCE / 'web', client / 'web', dirs_exist_ok=True)
    (destination / '开始使用.txt').write_text('双击 AgentHarness.exe。\n创建或打开工作台，登录后生成机器配对码。\n输入服务地址、配对码，点击“配对并启动”。\n选择项目文件夹、模型与思考强度，勾选启用并保存。\n关闭窗口不会停止接收服务；停止接收不会终止已经启动的作业。\n首次使用前，每台机器需要已有可用的 Codex 登录。\n请保留整个目录，不要只复制 exe。\n', encoding='utf-8-sig')
    print(destination)


if __name__ == '__main__': main()
