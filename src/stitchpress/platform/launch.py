import sys
from pathlib import Path


def is_compiled() -> bool:
    try:
        return bool(__compiled__)  # noqa: F821 - Nuitka supplies this special name.
    except NameError:
        return bool(getattr(sys, "frozen", False))


def worker_program() -> str:
    return str(Path(sys.argv[0]).resolve()) if is_compiled() else sys.executable


def worker_arguments(kind: str, arguments: list[str]) -> list[str]:
    # Nuitka marks each compiled module with __compiled__; PyInstaller sets sys.frozen.
    prefix = [] if is_compiled() else ["-m", "stitchpress"]
    return [*prefix, "--internal-worker" if kind == "worker" else "--internal-cli", *arguments]
