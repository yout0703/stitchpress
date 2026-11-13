"""Offline, signed, fixed-term licenses. No network and no issuer private keys."""

import base64
import hashlib
import json
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from stitchpress import _edition
from stitchpress.core.errors import ConflictError, StitchPressError
from stitchpress.core.identity import canonical_bytes
from stitchpress.platform.files import atomic_write, exclusive_lock
from stitchpress.platform.settings import os_keyring, settings_dir

TRIAL_SECONDS = 7 * 24 * 60 * 60
SCHEMA = "stitchpress-offline-license-v1"
REQUEST_SCHEMA = "stitchpress-device-request-v1"
MAX_LICENSE_BYTES = 16 * 1024
SERVICE = "StitchPress.Licensing"


@contextmanager
def clock_record_lock(path):
    # UI and multiple workers can start together. Brief contention is not an invalid license.
    for _ in range(20):
        lock = exclusive_lock(path)
        try:
            lock.__enter__()
            break
        except ConflictError:
            time.sleep(0.025)
    else:
        raise LicenseError("安全时间记录正被其他进程更新，请稍后重试")
    try:
        yield
    finally:
        lock.__exit__(None, None, None)


class LicenseError(StitchPressError):
    """Authorization failure; never damage the user's projects."""


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise LicenseError("授权文件包含重复字段")
        result[key] = value
    return result


def read_document(raw):
    if len(raw) > MAX_LICENSE_BYTES:
        raise LicenseError("授权文件过大")
    try:
        result = json.loads(raw, object_pairs_hook=_unique_pairs)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (ValueError, UnicodeError) as exc:
        raise LicenseError("授权文件格式损坏") from exc


def read_license_file(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_LICENSE_BYTES + 1)
    if len(raw) > MAX_LICENSE_BYTES:
        raise LicenseError("授权文件过大")
    return raw


@lru_cache(maxsize=1)
def device_id():
    """Hash one stable OS identifier; never expose its raw value in requests."""
    try:
        if sys.platform == "win32":
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Cryptography",
                0,
                winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
            ) as key:
                identity, _ = winreg.QueryValueEx(key, "MachineGuid")
        elif sys.platform == "darwin":
            output = subprocess.run(
                ["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                check=True,
                capture_output=True,
                timeout=5,
                text=True,
            ).stdout
            identity = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', output).group(1)
        else:
            raise ValueError()
        if not isinstance(identity, str) or not identity.strip():
            raise ValueError()
        return hashlib.sha256(
            ("StitchPress.device.v1:" + sys.platform + ":" + identity.strip().lower()).encode()
        ).hexdigest()
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError) as exc:
        raise LicenseError("无法读取本机设备标识，请检查系统权限；未开始试用") from exc


def device_request():
    return {"schema": REQUEST_SCHEMA, "product": "StitchPress", "device": device_id()}


def verify_license(raw, public_keys, device):
    envelope = read_document(raw)
    if set(envelope) != {"claims", "key_id", "signature"}:
        raise LicenseError("授权文件字段不完整")
    key_id = envelope["key_id"]
    if not isinstance(key_id, str) or key_id not in public_keys:
        raise LicenseError("授权签发方不受此版本信任")
    claims = envelope["claims"]
    fields = {
        "schema",
        "product",
        "kind",
        "license_id",
        "customer",
        "device",
        "issued_at",
        "expires_at",
    }
    if not isinstance(claims, dict) or set(claims) != fields:
        raise LicenseError("授权内容格式损坏")
    try:
        signature = base64.b64decode(envelope["signature"], validate=True)
        key = Ed25519PublicKey.from_public_bytes(
            base64.b64decode(public_keys[key_id], validate=True)
        )
        key.verify(signature, canonical_bytes(claims))
    except (InvalidSignature, TypeError, ValueError) as exc:
        raise LicenseError("授权签名无效，文件可能损坏或被修改") from exc
    if (
        claims["schema"] != SCHEMA
        or claims["product"] != "StitchPress"
        or claims["kind"] != "trial"
    ):
        raise LicenseError("授权产品或类型不匹配")
    if not isinstance(claims["device"], str) or claims["device"] != device:
        raise LicenseError("此授权属于另一台电脑，请提供本机机器码重新签发")
    if (
        not isinstance(claims["customer"], str)
        or not 1 <= len(claims["customer"].strip()) <= 120
        or not isinstance(claims["license_id"], str)
        or not re.fullmatch(r"[0-9a-f]{32}", claims["license_id"])
        or type(claims["issued_at"]) is not int
        or type(claims["expires_at"]) is not int
        or not 0 < claims["issued_at"] < 253402300799 - TRIAL_SECONDS
        or claims["expires_at"] - claims["issued_at"] != TRIAL_SECONDS
    ):
        raise LicenseError("试用授权必须为有效的连续七天期限")
    return claims


@dataclass(frozen=True)
class LicenseStatus:
    code: str
    message: str
    expires_at: int | None = None
    customer: str = ""

    @property
    def allowed(self):
        return self.code in {"development", "valid"}


class OfflineLicense:
    def __init__(
        self, directory=None, public_keys=None, device=None, clock=None, monotonic=None, vault=None
    ):
        self.directory = Path(directory) if directory is not None else settings_dir() / "license"
        self.public_keys = _edition.PUBLIC_KEYS if public_keys is None else public_keys
        self._device = device
        self.clock = clock or time.time
        self.monotonic = monotonic or time.monotonic
        self._vault = vault
        self._anchor = None
        self._last_persist = None
        self._maximum = 0

    @property
    def path(self):
        return self.directory / "trial.spl-license"

    @property
    def device(self):
        if self._device is None:
            self._device = device_id()
        return self._device

    def _trusted_now(self, *, force=False):
        wall, mono = self.clock(), self.monotonic()
        if self._anchor is None:
            self._anchor = (wall, mono)
        floor = self._anchor[0] + max(0, mono - self._anchor[1])
        now = max(wall, floor)
        if wall < max(self._maximum, floor) - 120:
            raise LicenseError("系统时间发生回拨，请校准时间后重新打开软件")
        if force or self._last_persist is None or mono - self._last_persist >= 60:
            try:
                vault = self._vault or os_keyring()
                # Coordinate UI and workers; stale concurrent writers must not lower the watermark.
                with clock_record_lock(self.directory / "clock.lock"):
                    account = "clock-v1-" + self.device
                    raw = vault.get_password(SERVICE, account)
                    stored = 0
                    if raw is not None:
                        record = read_document(raw)
                        if set(record) != {"maximum"} or type(record["maximum"]) is not int:
                            raise ValueError()
                        stored = record["maximum"]
                        if not 0 <= stored < 253402300799:
                            raise ValueError()
                    if wall < stored - 120:
                        raise LicenseError("系统时间早于上次使用记录，请校准时间后重试")
                    self._maximum = max(self._maximum, stored, int(now))
                    vault.set_password(SERVICE, account, json.dumps({"maximum": self._maximum}))
                self._last_persist = mono
            except LicenseError:
                raise
            except Exception as exc:
                raise LicenseError(
                    "无法验证安全时间记录，请检查钥匙串或凭据管理器权限后重试"
                ) from exc
        return max(now, self._maximum)

    def status(self):
        try:
            if not self.public_keys:
                raise LicenseError("试用版缺少发行公钥，请联系发行方获取正确安装包")
            if not self.path.exists():
                return LicenseStatus("unactivated", "尚未激活 · 请导出机器码并导入七天试用授权")
            claims = verify_license(read_license_file(self.path), self.public_keys, self.device)
            now = self._trusted_now()
            if now < claims["issued_at"]:
                return LicenseStatus("clock_error", "系统时间早于授权签发时间，请校准时间后重试")
            if now >= claims["expires_at"]:
                return LicenseStatus(
                    "expired",
                    "七天试用已到期，业务功能已锁定；已有文件保持原样",
                    claims["expires_at"],
                    claims["customer"],
                )
            return LicenseStatus("valid", "七天试用中", claims["expires_at"], claims["customer"])
        except (LicenseError, OSError) as exc:
            return LicenseStatus("invalid", str(exc))

    def install(self, raw):
        claims = verify_license(raw, self.public_keys, self.device)
        now = self._trusted_now(force=True)
        if now < claims["issued_at"]:
            raise LicenseError("授权尚未生效，请校准系统时间或联系发行方")
        if now >= claims["expires_at"]:
            raise LicenseError("此授权已到期，未替换已有授权")
        atomic_write(self.path, canonical_bytes(read_document(raw)))
        return self.status()


@lru_cache(maxsize=1)
def license_manager():
    return OfflineLicense()


def license_status():
    if _edition.EDITION == "development":
        return LicenseStatus("development", "内部开发版 · 无试用限制，不用于客户试用交付")
    if _edition.EDITION != "trial":
        return LicenseStatus("invalid", "发行类型无效，请重新获取安装包")
    return license_manager().status()


def require_license():
    status = license_status()
    if not status.allowed:
        raise LicenseError(status.message)
    return status
