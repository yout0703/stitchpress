import base64
import importlib.util
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from stitchpress import _edition
from stitchpress.core.identity import canonical_bytes
from stitchpress.platform import licensing
from stitchpress.platform.licensing import (
    SCHEMA,
    TRIAL_SECONDS,
    LicenseError,
    LicenseStatus,
    OfflineLicense,
    verify_license,
)


class Vault:
    def __init__(self):
        self.records = {}

    def get_password(self, service, account):
        return self.records.get((service, account))

    def set_password(self, service, account, value):
        self.records[service, account] = value


@pytest.fixture
def trial(tmp_path):
    key = Ed25519PrivateKey.generate()
    public = {"test": base64.b64encode(key.public_key().public_bytes_raw()).decode()}
    clock = {"wall": 1800000000, "mono": 100}
    claims = dict(
        schema=SCHEMA,
        product="StitchPress",
        kind="trial",
        license_id="a" * 32,
        customer="测试客户",
        device="b" * 64,
        issued_at=clock["wall"],
        expires_at=clock["wall"] + TRIAL_SECONDS,
    )
    vault = Vault()

    def manager(directory=None, device=None):
        return OfflineLicense(
            directory=directory or tmp_path,
            public_keys=public,
            device=device or claims["device"],
            clock=lambda: clock["wall"],
            monotonic=lambda: clock["mono"],
            vault=vault,
        )

    def sign(document=None):
        body = claims if document is None else document
        return canonical_bytes(
            {
                "claims": body,
                "key_id": "test",
                "signature": base64.b64encode(key.sign(canonical_bytes(body))).decode(),
            }
        )

    return manager, sign, claims, clock, key, public, vault


def test_exact_168_hours_and_reinstall_does_not_restart(trial, tmp_path):
    factory, sign, claims, clock, *_ = trial
    manager = factory()
    assert manager.status().code == "unactivated"
    manager.install(sign())
    clock["wall"] = claims["expires_at"] - 1
    assert manager.status().allowed
    clock["wall"] += 1
    assert manager.status().code == "expired"
    manager.path.unlink()
    # Reimport into a fresh application directory still has the signed original deadline.
    with pytest.raises(LicenseError, match="已到期"):
        factory(tmp_path / "reinstall").install(sign())


def test_wrong_device_and_tampering_preserve_installed_license(trial):
    factory, sign, _, _, _, public, _ = trial
    manager = factory()
    manager.install(sign())
    original = manager.path.read_bytes()
    tampered = json.loads(sign())
    tampered["claims"]["expires_at"] += TRIAL_SECONDS
    with pytest.raises(LicenseError, match="签名"):
        manager.install(canonical_bytes(tampered))
    with pytest.raises(LicenseError, match="另一台"):
        verify_license(original, public, "c" * 64)
    assert manager.path.read_bytes() == original


@pytest.mark.parametrize(
    "change",
    [
        {"product": "Other"},
        {"kind": "full"},
        {"schema": "wrong"},
        {"expires_at": 1800000000 + TRIAL_SECONDS + 1},
        {"issued_at": True},
        {"customer": ""},
        {"license_id": "bad"},
    ],
)
def test_rejects_even_signed_invalid_claims(trial, change):
    factory, sign, claims, *_ = trial
    with pytest.raises(LicenseError):
        factory().install(sign({**claims, **change}))


@pytest.mark.parametrize("raw", [b"[]", b"{", b"{}", b"{}" * 9000, b'{"claims":{},"claims":{}}'])
def test_malformed_license_is_not_activation(trial, raw):
    factory, *_ = trial
    with pytest.raises(LicenseError):
        factory().install(raw)
    assert not factory().path.exists()


def test_rollback_detected_across_restart(trial):
    factory, sign, _, clock, *_ = trial
    manager = factory()
    manager.install(sign())
    clock["wall"] += 3600
    clock["mono"] += 3600
    assert manager.status().allowed
    clock["wall"] -= 600
    assert not factory().status().allowed


def test_monotonic_detects_frozen_wall_and_future_license(trial):
    factory, sign, claims, clock, *_ = trial
    manager = factory()
    with pytest.raises(LicenseError, match="尚未生效"):
        manager.install(
            sign(
                {
                    **claims,
                    "issued_at": clock["wall"] + 500,
                    "expires_at": clock["wall"] + 500 + TRIAL_SECONDS,
                }
            )
        )
    manager.install(sign())
    clock["mono"] += TRIAL_SECONDS
    assert not manager.status().allowed


def test_vault_failure_is_closed_and_bad_install_does_not_overwrite(trial, monkeypatch):
    factory, sign, *_ = trial
    manager = factory()
    manager.install(sign())
    original = manager.path.read_bytes()

    def denied(*args):
        raise OSError("access denied")

    monkeypatch.setattr(manager._vault, "get_password", denied)
    manager._last_persist = None
    assert not manager.status().allowed
    with pytest.raises(LicenseError, match="安全时间"):
        manager.install(sign())
    assert manager.path.read_bytes() == original


def test_short_worker_contention_does_not_falsely_lock_ui(trial):
    import threading
    import time

    from stitchpress.platform.files import exclusive_lock

    factory, sign, *_ = trial
    manager = factory()
    manager.install(sign())
    acquired = threading.Event()

    def hold():
        with exclusive_lock(manager.directory / "clock.lock"):
            acquired.set()
            time.sleep(0.1)

    thread = threading.Thread(target=hold)
    thread.start()
    assert acquired.wait(2)
    manager._last_persist = None
    assert manager.status().allowed
    thread.join(2)


def test_issuer_fixed_duration_and_no_private_material(trial):
    _, _, claims, _, key, public, _ = trial
    spec = importlib.util.spec_from_file_location(
        "issuer_test", Path(__file__).parents[1] / "tools/issue_trial.py"
    )
    issuer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(issuer)
    request = {
        "schema": licensing.REQUEST_SCHEMA,
        "product": "StitchPress",
        "device": claims["device"],
    }
    document = issuer.issue(key, "test", request, "客户", now=claims["issued_at"])
    verified = verify_license(canonical_bytes(document), public, claims["device"])
    assert verified["expires_at"] - verified["issued_at"] == 604800
    assert set(document) == {"claims", "key_id", "signature"}
    assert key.private_bytes_raw() not in canonical_bytes(document)


def test_cli_and_worker_cannot_bypass_expiry_with_demo_or_internal_flags(
    tmp_path, monkeypatch, capsys
):
    from stitchpress.app.worker import run
    from stitchpress.cli import main

    monkeypatch.setattr(_edition, "EDITION", "trial")
    monkeypatch.setattr(licensing, "license_status", lambda: LicenseStatus("expired", "试用到期"))
    assert main(["demo", str(tmp_path / "demo.stitchpress")]) == 2
    assert "到期" in capsys.readouterr().out
    assert not (tmp_path / "demo.stitchpress").exists()
    assert (
        main(
            [
                "reference-export",
                "missing",
                str(tmp_path / "export"),
                "--work",
                str(tmp_path / "work"),
                "--demo",
            ]
        )
        == 2
    )
    result, job = tmp_path / "result.json", tmp_path / "job.json"
    job.write_bytes(canonical_bytes({"operation": "post-preview", "output": str(result)}))
    assert run(job) == 2
    assert json.loads(result.read_bytes())["status"] == "failed"
    assert main(["license-status"]) == 0


def test_expiry_at_final_publish_never_leaves_zip(tmp_path, monkeypatch):
    from stitchpress.app.export import zip_package

    source = tmp_path / "source"
    source.mkdir()
    (source / "already-generated.txt").write_text("preserve")
    calls = []

    def guard():
        calls.append(1)
        if len(calls) > 1:
            raise LicenseError("试用到期")

    monkeypatch.setattr(licensing, "require_license", guard)
    with pytest.raises(LicenseError, match="到期"):
        zip_package(source, tmp_path / "new.zip")
    assert not (tmp_path / "new.zip").exists()
    assert not list(tmp_path.glob(".stitchpress-zip-*.tmp"))
    assert (source / "already-generated.txt").read_text() == "preserve"


def test_live_ui_lock_preserves_pending_invalid_forms_and_recovers(tmp_path, qtbot, monkeypatch):
    import stitchpress.app.desktop as desktop
    import stitchpress.app.license_ui as ui
    import stitchpress.app.local_features as local_features
    from stitchpress.app.store import Project

    monkeypatch.setattr(desktop, "settings_dir", lambda: tmp_path)
    monkeypatch.setattr(local_features, "settings_dir", lambda: tmp_path)
    monkeypatch.setattr(ui, "settings_dir", lambda: tmp_path)
    monkeypatch.setattr(desktop, "load_settings", lambda: {})
    window = desktop.Workbench()
    qtbot.addWidget(window)
    window.confirm_discard = lambda: True
    status = [LicenseStatus("valid", "试用中", 1800604800, "测试客户")]
    monkeypatch.setattr(ui, "license_status", lambda: status[0])
    gate = ui.TrialController(window)
    window.trial_controller = gate
    window.title_edit.setText("到期前未保存的名称")
    window.spec_panel.editor.fabric_fields["nominal_ct"].setText("尚未填写完整")
    window.add_thread_row(
        plan={"ct": "14", "stitches_per_metre": "尚未填写", "source_note": "客户"}
    )
    status[0] = LicenseStatus("expired", "试用到期", 1800604800, "测试客户")
    gate.refresh()
    assert gate.locked
    assert not window.centralWidget().isEnabled()
    assert gate.action.isEnabled()
    assert gate.recovery_path.is_file()
    saved = Project.decode(gate.recovery_path.read_bytes())
    assert saved.entities["recovery_form"]["title"] == "到期前未保存的名称"
    window.spec_panel.editor.fabric_fields["nominal_ct"].setText("14")
    ui.restore_pending_forms(window, saved.entities["trial_pending_forms"])
    assert window.spec_panel.editor.fabric_fields["nominal_ct"].text() == "尚未填写完整"
    assert window.thread_table.item(0, 1).text() == "尚未填写"
    status[0] = LicenseStatus("valid", "续期后试用中", 1800604800, "测试客户")
    gate.refresh()
    assert window.centralWidget().isEnabled()
    assert not gate.locked
    window.dirty = False
    window.confirm_discard = lambda: True


def test_license_dialog_stops_polling_after_close(qtbot):
    from stitchpress.app.license_ui import LicenseDialog

    dialog = LicenseDialog()
    qtbot.addWidget(dialog)
    assert dialog.timer.isActive()
    dialog.reject()
    assert not dialog.timer.isActive()
