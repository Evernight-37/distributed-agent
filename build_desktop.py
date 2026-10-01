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
CLIENT_FILES = ('common.py', 'pull_worker.py', 'local_panel.py', 'desktop_service.py', 'desktop_runtime.py',
                'worker.py', 'jobrunner.py', 'panel.py', 'broker.py', 'collaboration_store.py', 'coordinator.py', 'controller.py',
                'account_identity.py', 'account_backend.py', 'owner_backend.py', 'owner_worker.py',
                'app_bridge.py', 'production_app.py', 'team_service.py')
CRYPTOGRAPHY_VERSION = '46.0.3'


def main():
    if sys.platform != 'win32': raise RuntimeError('Build this Windows app on Windows')
    missing = [name for name in CLIENT_FILES if not (SOURCE / name).is_file()]
    if missing: raise RuntimeError('Missing production sources: ' + ', '.join(missing))
    cache = SOURCE / 'build' / 'downloads'; cache.mkdir(parents=True, exist_ok=True)
    archive = cache / f'python-{VERSION}-embed-amd64.zip'
    if not archive.exists():
        with urllib.request.urlopen(URL, timeout=60) as response: archive.write_bytes(response.read())
    if hashlib.sha256(archive.read_bytes()).hexdigest() != SHA256: raise RuntimeError('Python runtime checksum mismatch')
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--windowed', '--onedir',
                    '--collect-all', 'webview', '--collect-all', 'pythonnet', '--collect-all', 'clr_loader',
                    '--collect-all', 'cryptography', '--hidden-import', 'webview.platforms.edgechromium',
                    '--hidden-import', 'clr', '--hidden-import', 'webview.platforms.winforms',
                    '--add-data', f'{SOURCE / "web"};web',
                    '--name', 'AgentHarness', str(SOURCE / 'production_app.py')], cwd=SOURCE, check=True)
    destination = SOURCE / 'dist' / 'AgentHarness'
    runtime = destination / 'runtime'; runtime.mkdir(exist_ok=True)
    with zipfile.ZipFile(archive) as bundle: bundle.extractall(runtime)
    packages = runtime / 'site-packages'
    packages.mkdir(exist_ok=True)
    subprocess.run([sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check',
                    '--target', str(packages), '--only-binary=:all:', '--python-version', '3.14',
                    '--implementation', 'cp', '--abi', 'cp314', f'cryptography=={CRYPTOGRAPHY_VERSION}'],
                   cwd=SOURCE, check=True)
    (runtime / 'python314._pth').write_text('python314.zip\n.\n../client\n./site-packages\nimport site\n', encoding='utf-8')
    subprocess.run([str(runtime / 'python.exe'), '-c',
                    'from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey; '
                    'key=Ed25519PrivateKey.generate(); key.public_key().verify(key.sign(b"runtime-check"),b"runtime-check"); '
                    'print("Worker runtime cryptography verified")'], cwd=SOURCE, check=True)
    client = destination / 'client'; client.mkdir(exist_ok=True)
    for name in CLIENT_FILES: shutil.copyfile(SOURCE / name, client / name)
    shutil.copytree(SOURCE / 'web', client / 'web', dirs_exist_ok=True)
    (destination / '开始使用.txt').write_text('双击 AgentHarness.exe。\n首次使用点击“创建账户”；另一台电脑点击“加入已有账户”，由已登录设备批准。\n控制台管理所有关联机器上的员工，创建项目或凭邀请加入项目。\n进入项目后派遣自己的员工，群主设置管理员与邀请成员。\n设置页管理设备、后台接收和连接地址。\n首次运行 agent 前，每台机器需要已有可用的 Codex 登录。\n账户身份保存在本机；项目文件不会自动同步。\n请保留整个目录，不要只复制 exe。\n', encoding='utf-8-sig')
    print(destination)


if __name__ == '__main__': main()
