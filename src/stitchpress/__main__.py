import os
import sys
from pathlib import Path


def main():
    # Windowed bootloaders can omit standard handles. Worker results/journals remain on disk.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
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
