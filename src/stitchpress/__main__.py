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
    from stitchpress.app.desktop import main as desktop

    return desktop()


if __name__ == "__main__":
    raise SystemExit(main())
