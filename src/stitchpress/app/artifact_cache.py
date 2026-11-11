"""Content-checked reusable outputs. Cache failure never changes a project or old delivery."""

import json
import os
import shutil
import tempfile
from pathlib import Path

from stitchpress.core.identity import canonical_bytes, sha256_file
from stitchpress.platform.files import atomic_write, exclusive_lock


class ArtifactCache:
    def __init__(self, directory: Path):
        self.directory = directory

    def restore(self, fingerprint: str, target: Path):
        folder = self.directory / fingerprint
        try:
            metadata = json.loads((folder / "receipt.json").read_bytes())
            source = folder / "artifact.bin"
            if (
                metadata["fingerprint"] != fingerprint
                or source.stat().st_size != metadata["bytes"]
                or sha256_file(source) != metadata["sha256"]
            ):
                return None
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            return metadata["checks"]
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def put(self, fingerprint: str, source: Path, checks: dict):
        folder = self.directory / fingerprint
        folder.mkdir(parents=True, exist_ok=True)
        with exclusive_lock(folder / "write.lock"):
            fd, name = tempfile.mkstemp(prefix="artifact-", suffix=".tmp", dir=folder)
            try:
                with source.open("rb") as incoming, os.fdopen(fd, "wb") as output:
                    shutil.copyfileobj(incoming, output)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(name, folder / "artifact.bin")
                atomic_write(
                    folder / "receipt.json",
                    canonical_bytes(
                        {
                            "fingerprint": fingerprint,
                            "sha256": sha256_file(source),
                            "bytes": source.stat().st_size,
                            "checks": checks,
                        }
                    ),
                )
            finally:
                Path(name).unlink(missing_ok=True)
