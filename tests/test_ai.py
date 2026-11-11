import io
import json

import pytest
from PIL import Image

from stitchpress.app.ai import (
    BudgetLedger,
    ImageServiceProfile,
    RemoteUnknown,
    ServiceProfile,
    analyze,
)
from stitchpress.core.errors import StitchPressError
from stitchpress.platform.settings import load_settings, save_settings


def successful_response():
    return {
        "id": "request:1",
        "model": "deepseek-flash",
        "usage": {"total_tokens": 10},
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "content": json.dumps(
                        {
                            "summary": "白花蓝图",
                            "key_objects": ["white petals"],
                            "risks": [],
                            "route": "direct_blueprint",
                        }
                    )
                },
            }
        ],
    }


def test_default_profile_and_text_vision_contract():
    profile = ServiceProfile()
    assert profile.model == "deepseek-flash"
    assert not profile.image_generation and not profile.image_editing
    calls = []

    def transport(url, headers, body, timeout):
        calls.append((url, headers, json.loads(body), timeout))
        return successful_response()

    image = io.BytesIO()
    Image.new("RGB", (2, 3)).save(image, "PNG")
    result = analyze(profile, "test-not-a-real-key", "识别主体", image.getvalue(), transport)
    url, headers, body, _ = calls[0]
    assert url == "https://api.deepseek.com/chat/completions"
    assert body["model"] == "deepseek-flash"
    assert body["messages"][1]["content"][1]["type"] == "image_url"
    assert result["usage"]["total_tokens"] == 10
    assert "test-not-a-real-key" not in json.dumps(result)
    assert (
        ServiceProfile(endpoint="https://different-provider.example").credential_key
        != profile.credential_key
    )


def test_capability_failure_never_silently_drops_image():
    with pytest.raises(StitchPressError, match="图片"):
        analyze(ServiceProfile(vision=False), "x", "test", b"image")
    with pytest.raises(StitchPressError, match="生成"):
        ServiceProfile(image_generation=True)


def test_semantically_invalid_ai_result_is_rejected_without_retry():
    calls = []

    def invalid(*args):
        calls.append(1)
        result = successful_response()
        result["choices"][0]["message"]["content"] = '{"summary":"pass", "route":"run_shell"}'
        return result

    with pytest.raises(RemoteUnknown):
        analyze(ServiceProfile(), "not-a-key", "test", transport=invalid)
    assert len(calls) == 1


def test_s16_budget_reservations_unknown_cost_and_limits():
    ledger = BudgetLedger("mac:1", "10", 2)
    first = ledger.reserve("6")
    ledger.finish(first, actual=None, state="remote_unknown")
    assert str(ledger.committed) == "6"
    with pytest.raises(StitchPressError, match="预算"):
        ledger.reserve("5")
    second = ledger.reserve("4")
    ledger.finish(second, actual="2", state="completed")
    assert ledger.committed == 8
    with pytest.raises(StitchPressError):
        ledger.reserve("1")


def test_s17_cross_device_import_retains_cost_and_execution_owner():
    original = BudgetLedger("mac:1", "10", 10)
    task = original.reserve("4")
    copied = BudgetLedger("windows:2", "10", 10)
    copied.import_records(original.tasks)
    copied.import_records(original.tasks)
    assert copied.committed == 4
    assert not copied.can_schedule(task)
    with pytest.raises(StitchPressError, match="另一设备"):
        copied.finish(task, actual="0", state="cancelled")
    new_task = copied.reserve("4", parent=task)
    assert copied.tasks[new_task]["parent"] == task
    assert copied.committed == 8


def test_configuration_never_persists_secret_fields(tmp_path):
    save_settings({"ai_profile": ServiceProfile().to_dict()}, tmp_path)
    assert load_settings(tmp_path)["ai_profile"]["model"] == "deepseek-flash"
    with pytest.raises(StitchPressError, match="密钥"):
        save_settings({"nested": {"api_key": "do-not-save"}}, tmp_path)
    assert "do-not-save" not in (tmp_path / "settings.json").read_text()


def test_image_provider_configuration_is_separate_and_honestly_pending():
    image = ImageServiceProfile(endpoint="https://images.example", generation_model="future-model")
    assert image.protocol == "pending-supplier-contract"
    assert image.credential_key != ServiceProfile().credential_key
    assert ImageServiceProfile(**image.to_dict()) == image


@pytest.mark.parametrize(
    "url",
    [
        "http://api.deepseek.com",
        "https://user:pw@example.com",
        "https://example.com?api_key=unsafe",
    ],
)
def test_secret_cannot_be_embedded_in_endpoint(url):
    with pytest.raises(StitchPressError):
        ServiceProfile(endpoint=url)
