"""Native host build, never claim Windows cross-compilation from macOS."""

import argparse
import base64
import json
import os
import platform
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

from stitchpress import __version__

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=("pyinstaller", "nuitka"), default="pyinstaller")
    parser.add_argument("--edition", choices=("development", "trial"), default="development")
    parser.add_argument("--public-keys", type=Path, help="试用版发行公钥JSON；绝不传入私钥")
    parser.add_argument("--protect", action="store_true", help="Cython编译自有模块后再打包")
    args = parser.parse_args()
    if args.edition == "trial" and (not args.public_keys or not args.protect):
        parser.error("客户试用版必须同时提供 --public-keys 和 --protect")
    if args.protect and args.backend != "pyinstaller":
        parser.error("--protect 使用已隔离的 Cython + PyInstaller 链路")
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    if declared != __version__:
        raise SystemExit(f"Version mismatch: project={declared}, runtime={__version__}")
    if platform.system() not in {"Darwin", "Windows"}:
        raise SystemExit("Only macOS/Windows native builds are currently planned")
    host = "macos" if sys.platform == "darwin" else "windows"
    output_name = host if args.backend == "pyinstaller" else "nuitka-" + host
    if args.edition == "trial":
        output_name += "-trial"
    elif args.protect:
        output_name += "-protected"
    output = ROOT / "dist" / output_name
    output.mkdir(parents=True, exist_ok=True)
    source = ROOT / "src"
    protection = None
    if args.protect:
        public_keys = {}
        if args.public_keys:
            public_keys = json.loads(args.public_keys.read_bytes())
            if not isinstance(public_keys, dict) or not public_keys:
                parser.error("公钥JSON必须包含至少一个发行密钥标识")
            for key_id, public_key in public_keys.items():
                if (
                    not isinstance(key_id, str)
                    or not 1 <= len(key_id) <= 80
                    or not isinstance(public_key, str)
                    or len(base64.b64decode(public_key, validate=True)) != 32
                ):
                    parser.error("发行公钥必须为32字节Ed25519公钥的Base64编码")
        stage = ROOT / "build" / ("protected-" + host)
        if stage.exists():
            shutil.rmtree(stage)
        source = stage / "src"
        shutil.copytree(ROOT / "src", source, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (source / "stitchpress/_edition.py").write_text(
            f"EDITION = {args.edition!r}\nPUBLIC_KEYS = {public_keys!r}\n", "utf-8"
        )
        with (output / "protection-build.log").open("w", encoding="utf-8") as log:
            protected = subprocess.run(
                [sys.executable, str(ROOT / "tools/protect_source.py"), str(source)],
                cwd=stage,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        if protected.returncode:
            print(f"Protection build failed; log={output / 'protection-build.log'}")
            return protected.returncode
        protection = json.loads((stage / "protection.json").read_bytes())
        shutil.copy2(stage / "protection.json", output / "protection.json")
    app_icon = (
        ROOT
        / "src/stitchpress/resources/brand"
        / ("stitchpress.icns" if sys.platform == "darwin" else "stitchpress.ico")
    )
    command = [
        sys.executable,
        "-m",
        "nuitka",
        "--standalone",
        "--enable-plugin=pyside6",
        "--include-qt-plugins=platforms,imageformats",
        "--include-package=stitchpress",
        "--include-package-data=qtawesome",
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
            f"--macos-app-icon={ROOT / 'src/stitchpress/resources/brand/stitchpress.icns'}",
        ]
    else:
        command += [
            "--windows-console-mode=disable",
            "--product-name=StitchPress",
            "--file-version=0.1.0.0",
            "--product-version=0.1.0.0",
            f"--windows-icon-from-ico={ROOT / 'src/stitchpress/resources/brand/stitchpress.ico'}",
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
            f"--icon={app_icon}",
            f"--paths={source}",
            f"--add-data={source / 'stitchpress' / 'resources'}:stitchpress/resources",
            "--collect-submodules=stitchpress",
            "--collect-data=qtawesome",
            "--collect-data=imagecodecs",
            "--hidden-import=imagecodecs._imcd",
            "--hidden-import=imagecodecs._shared",
            "--hidden-import=imagecodecs._shared_cython",
            f"--distpath={output}",
            f"--workpath={ROOT / 'build' / ('pyinstaller-' + host)}",
            f"--specpath={ROOT / 'build' / 'spec'}",
        ]
        if sys.platform == "darwin":
            command += ["--osx-bundle-identifier=com.stitchpress.desktop"]
        if protection:
            command += [f"--hidden-import={name}" for name in protection["hidden_imports"]]
        command += ["tools/StitchPress.py"]
    environment = dict(os.environ)
    if protection:
        environment["PYTHONPATH"] = str(source)
    with (output / "build.log").open("w", encoding="utf-8") as log:
        result = subprocess.run(
            command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, env=environment
        )
    print(f"exit_code={result.returncode}; log={output / 'build.log'}")
    if result.returncode == 0 and protection:
        executable = (
            output / "StitchPress.app/Contents/MacOS/StitchPress"
            if host == "macos"
            else output / "StitchPress/StitchPress.exe"
        )
        audit = subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools/audit_protected_bundle.py"),
                str(executable),
                str(output / "protection.json"),
                "--output",
                str(output / "protection-audit.json"),
            ],
            cwd=ROOT,
        )
        return audit.returncode
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
