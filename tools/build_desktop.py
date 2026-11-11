"""Native host build, never claim Windows cross-compilation from macOS."""

import argparse
import platform
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=("pyinstaller", "nuitka"), default="pyinstaller")
    args = parser.parse_args()
    if platform.system() not in {"Darwin", "Windows"}:
        raise SystemExit("Only macOS/Windows native builds are currently planned")
    host = "macos" if sys.platform == "darwin" else "windows"
    output = ROOT / "dist" / (host if args.backend == "pyinstaller" else "nuitka-" + host)
    output.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "nuitka",
        "--standalone",
        "--enable-plugin=pyside6",
        "--include-qt-plugins=platforms,imageformats",
        "--include-package=stitchpress",
        "--include-data-dir=src/stitchpress/resources=stitchpress/resources",
        f"--output-dir={output}",
        "--output-filename=StitchPressDesktop",
        "--assume-yes-for-downloads",
    ]
    if sys.platform == "darwin":
        command += [
            "--macos-create-app-bundle",
            "--macos-app-name=StitchPress",
            "--macos-app-version=0.1.0",
        ]
    else:
        command += [
            "--windows-console-mode=disable",
            "--product-name=StitchPress",
            "--file-version=0.1.0.0",
            "--product-version=0.1.0.0",
        ]
    command += ["tools/StitchPress.py"]
    if args.backend == "pyinstaller":
        command = [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--onedir",
            "--windowed",
            "--name=StitchPress",
            f"--paths={ROOT / 'src'}",
            f"--add-data={ROOT / 'src' / 'stitchpress' / 'resources'}:stitchpress/resources",
            "--collect-submodules=stitchpress",
            f"--distpath={output}",
            f"--workpath={ROOT / 'build' / ('pyinstaller-' + host)}",
            f"--specpath={ROOT / 'build' / 'spec'}",
        ]
        if sys.platform == "darwin":
            command += ["--osx-bundle-identifier=com.stitchpress.desktop"]
        command += ["tools/StitchPress.py"]
    with (output / "build.log").open("w", encoding="utf-8") as log:
        result = subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    print(f"exit_code={result.returncode}; log={output / 'build.log'}")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
