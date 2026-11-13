"""Verify protected modules are native extensions, not fallback bytecode in PYZ."""

import argparse
import json
from pathlib import Path

from PyInstaller.archive.readers import CArchiveReader


def audit(executable, manifest):
    reader = CArchiveReader(str(executable))
    pyz_name = next(name for name in reader.toc if name.endswith(".pyz"))
    pyz = reader.open_embedded_archive(pyz_name)
    expected = manifest["protected_modules"]
    leaked = sorted(set(expected) & set(pyz.toc))
    if leaked:
        raise ValueError(f"Protected modules unexpectedly present as bytecode: {leaked}")
    root = executable.parent.parent if executable.parent.name == "MacOS" else executable.parent
    binaries = list(root.rglob("*.so")) + list(root.rglob("*.pyd"))
    missing = []
    for name in expected:
        parent, stem = name.rsplit(".", 1)
        suffix = "/" + parent.replace(".", "/") + "/"
        if not any(
            suffix in ("/" + path.as_posix())
            and (path.name.startswith(stem + ".") or path.stem == stem)
            for path in binaries
        ):
            missing.append(name)
    if missing:
        raise ValueError(f"Missing native modules: {missing}")
    return {"native_modules_verified": len(expected), "protected_bytecode_leaks": leaked}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("executable", type=Path)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.executable, json.loads(args.manifest.read_bytes()))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), "utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
