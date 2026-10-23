"""Install only inside this receiver directory, including offline servers."""

import subprocess
import venv
import zipfile
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent
    if not (root / "client/linux_receiver.py").is_file():
        raise RuntimeError("Run install.py from an unpacked Linux receiver release")
    destination = root / ".venv"
    if destination.exists():
        raise RuntimeError(".venv already exists; preserve this environment or install to a new release directory")
    wheels = root / "wheelhouse"
    pip_wheels = sorted(wheels.glob("pip-*.whl"))
    venv.EnvBuilder(with_pip=not bool(pip_wheels)).create(destination)
    python = destination / "bin/python"
    if pip_wheels:
        purelib = subprocess.check_output(
            [str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True
        ).strip()
        with zipfile.ZipFile(pip_wheels[-1]) as archive:
            for entry in archive.infolist():
                target = Path(purelib) / entry.filename
                if not target.resolve().is_relative_to(Path(purelib).resolve()):
                    raise RuntimeError("Invalid wheel entry")
                if not entry.is_dir():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.read(entry))
    command = [str(python), "-m", "pip", "install", "--disable-pip-version-check"]
    if wheels.is_dir():
        command.extend(["--no-index", "--find-links", str(wheels)])
    subprocess.run([*command, "-r", str(root / "requirements.txt")], check=True)
    print("Installed. Run ./harness link --help")


if __name__ == "__main__":
    main()
