# macOS 与 Linux 发布运行环境

2.1.0 的应用源码与本机兼容安装使用相同的业务代码；差别在 Python 运行环境。原 macOS 发布包中的后台解释器引用了构建机的 `/Library/Frameworks/Python.framework/Versions/3.12/Python`，在未安装该 Framework 的 Mac 上无法启动。原 Linux x64 包的 `libpython` 要求 GLIBC_2.38，不能在 Ubuntu 22.04 / glibc 2.35 上启动。

发布构建现在使用 Python Build Standalone 的 CPython 3.12.16、20261026 版本，为 macOS 与 Linux 的 x64 / ARM64 分别固定下载地址和 SHA-256。摘要在解压与运行前校验，解压使用 `tarfile` 的 data 过滤器。来源：[Python Build Standalone 20261026](https://github.com/astral-sh/python-build-standalone/releases/tag/20261026)。

用 Python 3.12 启动下载器，然后使用下载的解释器创建构建环境。macOS 示例：

```bash
python3.12 scripts/prepare_python_runtime.py --destination build/standalone-python
build/standalone-python/bin/python3 -m venv build/release-env
build/release-env/bin/python -m pip install -e . -r requirements-macos.txt
build/release-env/bin/python scripts/build_macos.py \
  --output-root dist/macos --python-runtime build/standalone-python
build/release-env/bin/python tests/cleanroom_release.py \
  dist/macos/AgentHarness.app build/cleanroom-macos
```

Linux 使用相同的前两步，依赖改为 `-e . -r requirements-linux-build.txt`，再执行 `build/release-env/bin/python scripts/build_linux_receiver.py`。应在与目标相同的 CPU 架构上构建。

构建器拒绝把 venv 本身作为服务运行环境，也拒绝 Framework Python 或与构建解释器来自不同安装的运行环境。Linux 服务包复制所选独立运行环境中的解释器，不再隐式复制调用者的解释器。

CI 在 macOS 上实际构建并运行净室检查；Linux 的两个架构均在 Ubuntu 22.04 容器中运行打包后的 CLI 和后台解释器，验证 SSL、SQLite、cryptography 与服务模块导入。构建机自己能运行产物，不能代替这项最低系统版本检查。

这项修复不修改账户、聊天、项目工作目录或运行权限。已发布的 v2.1.0 附件不会因合并源码自动替换，需要后续正式发布。不要上传本机数据目录、设备身份、SSH 配置或代理设置。
