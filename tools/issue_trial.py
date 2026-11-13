"""Issuer-only tool. Never packaged; private key stays in the operator's OS vault."""

import argparse
import base64
import json
import time
from pathlib import Path
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from stitchpress.core.errors import StitchPressError
from stitchpress.core.identity import canonical_bytes
from stitchpress.platform.files import atomic_write
from stitchpress.platform.licensing import REQUEST_SCHEMA, SCHEMA, TRIAL_SECONDS, read_document
from stitchpress.platform.settings import os_keyring

ISSUER_SERVICE = "StitchPress.Issuer"


def public_manifest(key, key_id):
    return {key_id: base64.b64encode(key.public_key().public_bytes_raw()).decode("ascii")}


def issue(key, key_id, request, customer, *, now=None):
    import re

    if (
        set(request) != {"schema", "product", "device"}
        or request["schema"] != REQUEST_SCHEMA
        or request["product"] != "StitchPress"
        or not isinstance(request["device"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", request["device"])
    ):
        raise StitchPressError("机器码请求格式不正确")
    if not 1 <= len(customer.strip()) <= 120:
        raise StitchPressError("客户名称须为1至120个字符")
    issued = int(time.time()) if now is None else now
    claims = dict(
        schema=SCHEMA,
        product="StitchPress",
        kind="trial",
        license_id=uuid4().hex,
        customer=customer.strip(),
        device=request["device"],
        issued_at=issued,
        expires_at=issued + TRIAL_SECONDS,
    )
    return {
        "claims": claims,
        "key_id": key_id,
        "signature": base64.b64encode(key.sign(canonical_bytes(claims))).decode("ascii"),
    }


def main():
    parser = argparse.ArgumentParser(description="发行方离线七天试用签发；私钥仅存系统安全存储")
    parser.add_argument("command", choices=("init-key", "public-key", "issue"))
    parser.add_argument("--key-id", required=True, help="发行密钥标识，不是密钥内容")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--customer")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("输出已存在，请选择新路径；不覆盖旧授权或公钥")
    if args.command == "issue" and (not args.request or not args.customer):
        parser.error("签发需要 --request 和 --customer")
    vault = os_keyring()
    raw = vault.get_password(ISSUER_SERVICE, args.key_id)
    if args.command == "init-key":
        if raw is not None:
            parser.error("该密钥标识已存在；使用 public-key 导出公钥")
        key = Ed25519PrivateKey.generate()
        vault.set_password(
            ISSUER_SERVICE, args.key_id, base64.b64encode(key.private_bytes_raw()).decode("ascii")
        )
    else:
        if raw is None:
            parser.error("发行密钥不存在，请先在本机 init-key")
        key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(raw, validate=True))
    document = (
        issue(key, args.key_id, read_document(args.request.read_bytes()), args.customer)
        if args.command == "issue"
        else public_manifest(key, args.key_id)
    )
    atomic_write(args.output, canonical_bytes(document))
    print(
        json.dumps({"saved": str(args.output), "private_key_exported": False}, ensure_ascii=False)
    )


if __name__ == "__main__":
    main()
