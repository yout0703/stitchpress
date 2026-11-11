"""Allowlisted process protocol. Workers write only their task result, never project files."""

import json
from pathlib import Path

from stitchpress.core.identity import canonical_bytes
from stitchpress.platform.files import atomic_write


def run(job_path: Path) -> int:
    job = json.loads(job_path.read_bytes())
    output = Path(job["output"])
    try:
        if job["operation"] == "convert":
            from stitchpress.core.conversion import ConversionOptions, convert_image
            from stitchpress.core.resources import load_catalog

            design, record = convert_image(
                Path(job["image"]).read_bytes(),
                load_catalog(Path(job["catalog"]).read_bytes()),
                ConversionOptions(**job["options"]),
            )
            result = {"design": design.to_dict(), "conversion": record}
        elif job["operation"] == "analyze":
            from stitchpress.platform.settings import load_secret

            from .ai import ServiceProfile, analyze

            profile = ServiceProfile(**job["profile"])
            result = analyze(
                profile,
                load_secret(profile.credential_key) or "",
                job["prompt"],
                Path(job["image"]).read_bytes() if job.get("image") else None,
            )
        else:
            raise ValueError("未知工作进程操作")
        atomic_write(
            output,
            canonical_bytes(
                {"status": "completed", "result": result, "base_sha": job.get("base_sha")}
            ),
        )
        print(json.dumps({"stage": "completed"}), flush=True)
        return 0
    except Exception as exc:
        from .ai import RemoteUnknown

        atomic_write(
            output,
            canonical_bytes(
                {
                    "status": "remote_unknown" if isinstance(exc, RemoteUnknown) else "failed",
                    "error": str(exc),
                    "base_sha": job.get("base_sha"),
                }
            ),
        )
        print(json.dumps({"stage": "failed", "error": str(exc)}, ensure_ascii=False), flush=True)
        return 2


if __name__ == "__main__":
    import sys

    raise SystemExit(run(Path(sys.argv[1])))
