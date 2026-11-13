"""Compile only an isolated staging tree; never transform the maintainable source."""

import argparse
import ast
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    source = args.source.resolve()
    if source.name != "src" or not (source / "stitchpress/_edition.py").is_file():
        parser.error("Expected isolated src/stitchpress staging tree")
    # Build tooling calls this in build/protected-<host> only, never the repository src.
    if not source.parent.name.startswith("protected-") or source.parent.parent.name != "build":
        parser.error("Refusing to transform a non-staging source tree")
    from Cython.Build import cythonize
    from setuptools import Extension, setup

    os.chdir(source.parent)
    sources = sorted(p for p in (source / "stitchpress").rglob("*.py") if p.name != "__init__.py")
    hidden = {"cryptography.hazmat.bindings._rust"}
    for path in sources:
        for node in ast.walk(ast.parse(path.read_text("utf-8"))):
            if isinstance(node, ast.Import):
                hidden.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                hidden.add(node.module)
                # Native extensions hide imports from PyInstaller's bytecode analysis.
                # `from PIL import ImageDraw` imports the submodule, not just PIL.
                for alias in node.names:
                    candidate = node.module + "." + alias.name
                    try:
                        if importlib.util.find_spec(candidate) is not None:
                            hidden.add(candidate)
                    except (ImportError, AttributeError, ValueError):
                        pass  # Class/function imports and modules unavailable on this host.
    modules = [".".join(path.relative_to(source).with_suffix("").parts) for path in sources]
    extensions = [
        Extension(name, [path.relative_to(source.parent).as_posix()])
        for path, name in zip(sources, modules, strict=True)
    ]
    setup(
        name="stitchpress-protected",
        script_args=["build_ext", "--inplace", "--build-lib", "src"],
        package_dir={"": "src"},
        ext_modules=cythonize(
            extensions,
            compiler_directives={"language_level": 3, "binding": True, "annotation_typing": False},
            nthreads=min(4, os.cpu_count() or 1),
            build_dir="cython-c",
        ),
    )
    for path in sources:
        if not any(
            path.with_suffix(suffix).is_file() for suffix in importlib.machinery.EXTENSION_SUFFIXES
        ):
            raise RuntimeError(f"Missing native extension: {path.name}")
    for path in sources:
        path.unlink()
    # C sources and build intermediates stay outside src and therefore outside the bundle.
    (source.parent / "protection.json").write_text(
        json.dumps({"protected_modules": modules, "hidden_imports": sorted(hidden)}, indent=2),
        "utf-8",
    )


if __name__ == "__main__":
    main()
