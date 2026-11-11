import json
import os
import sys
from pathlib import Path

from stitchpress.core.errors import StitchPressError
from stitchpress.platform.files import atomic_write


def settings_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "StitchPress"
    if os.name == "nt":
        return (
            Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "StitchPress"
        )
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "stitchpress"


def os_keyring():
    # Select secure OS backends explicitly; never fall back to a plaintext plugin.
    if sys.platform == "darwin":
        from keyring.backends.macOS import Keyring

        return Keyring()
    if os.name == "nt":
        from keyring.backends.Windows import WinVaultKeyring

        return WinVaultKeyring()
    raise StitchPressError("当前只支持macOS钥匙串和Windows凭据管理器")


def save_secret(reference: str, secret: str):
    try:
        os_keyring().set_password("StitchPress.AI", reference, secret)
    except Exception as exc:
        raise StitchPressError(
            "无法写入系统安全存储；密钥没有写入普通配置，请检查系统权限"
        ) from exc


def load_secret(reference: str) -> str | None:
    try:
        return os_keyring().get_password("StitchPress.AI", reference)
    except Exception as exc:
        raise StitchPressError("无法读取系统安全存储，请检查钥匙串/凭据管理器权限") from exc


def save_settings(data: dict, directory: Path | None = None):
    forbidden = {"api_key", "password", "token", "secret", "authorization"}

    def check(value):
        if isinstance(value, dict):
            if any(str(k).lower() in forbidden for k in value):
                raise StitchPressError("普通配置不能包含密钥字段")
            for v in value.values():
                check(v)
        elif isinstance(value, list):
            for v in value:
                check(v)

    check(data)
    atomic_write(
        (directory or settings_dir()) / "settings.json",
        json.dumps(data, ensure_ascii=False, indent=2).encode(),
    )


def load_settings(directory: Path | None = None) -> dict:
    path = (directory or settings_dir()) / "settings.json"
    return json.loads(path.read_bytes()) if path.exists() else {}
