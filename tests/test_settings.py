import pytest
from pydantic import ValidationError

from scopecheck.settings import Settings

pytestmark = pytest.mark.unit


def test_default_is_keyless_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCOPECHECK_MODE", raising=False)
    settings = Settings()
    assert settings.model_dump() == {"mode": "fake"}


@pytest.mark.parametrize("mode", ["provider", "", "FAKE"])
def test_invalid_environment_mode(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setenv("SCOPECHECK_MODE", mode)
    with pytest.raises(ValidationError, match="Input should be 'fake'"):
        Settings()


def test_unknown_configuration_is_rejected_without_echoing_secret() -> None:
    secret = "synthetic-secret-for-test-only"
    with pytest.raises(ValidationError) as caught:
        Settings(provider_key=secret)
    assert "Extra inputs are not permitted" in str(caught.value)
    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)


def test_invalid_mode_does_not_echo_input(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "synthetic-accidentally-pasted-secret"
    monkeypatch.setenv("SCOPECHECK_MODE", secret)
    with pytest.raises(ValidationError) as caught:
        Settings()
    assert secret not in str(caught.value)


def test_unrelated_credentials_are_not_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCOPECHECK_MODE", "fake")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-unused-key")
    settings = Settings()
    assert "synthetic-unused-key" not in repr(settings)
    assert settings.model_dump_json() == '{"mode":"fake"}'
