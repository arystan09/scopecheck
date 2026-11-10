import subprocess
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from scopecheck.providers.config import LiveConfig
from scopecheck.providers.openai import TOOL_SCHEMA_HASH, generation_settings
from scopecheck.runs.service import digest

pytestmark = pytest.mark.unit


def test_conservative_rounded_charge_and_full_settings(live_config):
    assert live_config.maximum_charge() == 102001
    settings = generation_settings(live_config)
    assert settings["store"] is False and settings["stream"] is False
    assert len(TOOL_SCHEMA_HASH) == 64
    altered = live_config.model_dump(mode="json")
    altered["generation"]["temperature"] = 0.5
    assert digest(altered) != digest(live_config.model_dump(mode="json"))
    assert generation_settings(LiveConfig.model_validate(altered))["temperature"] == 0.5


@pytest.mark.parametrize(
    "field,value",
    [
        ("model", ""),
        ("model", "https://attacker.invalid"),
        ("model", "another-model"),
        ("api_key", "must-not-be-stored"),
        ("base_url", "https://attacker.invalid"),
    ],
)
def test_invalid_config_fields(live_config, field, value):
    body = live_config.model_dump(mode="json")
    body[field] = value
    with pytest.raises(ValidationError):
        LiveConfig.model_validate(body)


@pytest.mark.parametrize(
    "field,value",
    [
        ("input_microusd_per_million", 0),
        ("output_microusd_per_million", -1),
        ("input_microusd_per_million", 1.2),
        ("input_microusd_per_million", True),
        ("version", ""),
        ("owner_verified", False),
        ("owner_verified", 1),
        ("currency", "EUR"),
        ("valid_until", "2100-01-01"),
        ("context_window_tokens", 10),
    ],
)
def test_invalid_prices(live_config, field, value):
    body = live_config.model_dump(mode="json")
    body["price"][field] = value
    with pytest.raises(ValidationError):
        LiveConfig.model_validate(body)


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_input_tokens", 99999),
        ("max_output_tokens", 4001),
        ("max_output_tokens", 0),
        ("max_output_tokens", True),
        ("max_output_tokens", 1.1),
        ("temperature", float("nan")),
        ("top_p", 0),
        ("reasoning_effort", "invented"),
    ],
)
def test_invalid_generation_bounds(live_config, field, value):
    body = live_config.model_dump(mode="json")
    body["generation"][field] = value
    with pytest.raises(ValidationError):
        LiveConfig.model_validate(body)


def test_expired_price_refused(live_config):
    live_config.price.valid_until = datetime(2000, 1, 1, tzinfo=UTC)
    with pytest.raises(ValueError, match="expired"):
        live_config.price.require_current()


def test_cli_help_and_owner_authorization_required():
    result = subprocess.run(
        ["scopecheck", "run", "live", "--help"], capture_output=True, text=True
    )
    assert result.returncode == 0
    for flag in ("--owner-authorized", "--budget-microusd", "--config", "--dataset-id"):
        assert flag in result.stdout
    refused = subprocess.run(
        ["scopecheck", "run", "live"], capture_output=True, text=True
    )
    assert refused.returncode == 2
    assert "--owner-authorized" in refused.stderr
