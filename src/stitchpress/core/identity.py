import hashlib
import json
import re
import unicodedata
from typing import Any


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def image_pixel_sha(image):
    result = hashlib.sha256()
    for top in range(0, image.height, 128):
        band = image.crop((0, top, image.width, min(image.height, top + 128)))
        result.update(band.tobytes())
        band.close()
    return result.hexdigest()


def sha256_file(path) -> str:
    result = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def symbol_key(external_id: int | str) -> str:
    if type(external_id) is int:
        return f"int:{external_id}"
    if type(external_id) is str and external_id:
        return f"str:{external_id}"
    raise ValueError("符号ID必须为整数或非空字符串，不能使用浮点或布尔值")


def safe_filename(value: str) -> str:
    """Original SKU stays untouched. Suffix avoids case/Unicode/sanitization collisions."""
    name = unicodedata.normalize("NFC", value)
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")[:72].rstrip(" .")
    if not name or name.split(".")[0].upper() in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        name = f"_{name or 'untitled'}"
    return f"{name}-{sha256(value.encode('utf-8'))[:8]}"


def valid_directory_name(value: str) -> bool:
    """Validate a user-visible single directory name without appending a hash."""
    return bool(
        value
        and len(value.encode("utf-8")) <= 240
        and value == value.strip(" .")
        and not re.search(r'[<>:"/\\|?*\x00-\x1f]', value)
        and value.split(".")[0].upper()
        not in {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10)),
        }
    )
