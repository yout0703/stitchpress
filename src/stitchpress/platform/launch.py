import sys
from pathlib import Path


def is_compiled() -> bool:
    # Attribute lookup also compiles with Cython; Nuitka populates this module global.
    return bool(globals().get("__compiled__", False) or getattr(sys, "frozen", False))


def worker_program() -> str:
    return str(Path(sys.argv[0]).resolve()) if is_compiled() else sys.executable


def worker_arguments(kind: str, arguments: list[str]) -> list[str]:
    # Nuitka marks each compiled module with __compiled__; PyInstaller sets sys.frozen.
    prefix = [] if is_compiled() else ["-m", "stitchpress"]
    return [*prefix, "--internal-worker" if kind == "worker" else "--internal-cli", *arguments]
