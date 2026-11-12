import os
import sys
from pathlib import Path


def main():
    # Windowed bootloaders may omit handles or inherit a CP1252 pipe. Frozen
    # Python does not necessarily honor PYTHONUTF8 from its parent environment.
    # Keep Chinese progress/errors printable, including cancellation, so an
    # encoding exception cannot turn into a blocking Windows error dialog.
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if stream is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
        elif hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    if len(sys.argv) > 1 and sys.argv[1] == "--internal-worker":
        from stitchpress.app.worker import run

        return run(Path(sys.argv[2]))
    if len(sys.argv) > 1 and sys.argv[1] == "--internal-cli":
        from stitchpress.cli import main as cli

        return cli(sys.argv[2:])
    if len(sys.argv) > 2 and sys.argv[1] == "--internal-cli-report":
        # Windows GUI bootloaders have no standard handles. Return via an explicit file.
        import io
        from contextlib import redirect_stderr, redirect_stdout

        from stitchpress.cli import main as cli
        from stitchpress.core.identity import canonical_bytes
        from stitchpress.platform.files import atomic_write

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = cli(sys.argv[3:])
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 2
        atomic_write(
            Path(sys.argv[2]),
            canonical_bytes(
                {"exit_code": code, "stdout": out.getvalue(), "stderr": err.getvalue()}
            ),
        )
        return code
    from stitchpress.app.desktop import main as desktop

    return desktop()


if __name__ == "__main__":
    raise SystemExit(main())
