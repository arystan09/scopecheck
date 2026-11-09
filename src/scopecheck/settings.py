"""Keyless configuration for the current milestone."""

from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SCOPECHECK_",
        extra="forbid",
        hide_input_in_errors=True,
        frozen=True,
    )

    # Reject provider mode until its explicitly approved milestone exists.
    mode: Literal["fake"] = "fake"

    database_url: SecretStr | None = Field(default=None, repr=False, exclude=True)
    code_revision: str = Field(
        default="unknown", min_length=1, max_length=100, exclude=True
    )

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            try:
                url = make_url(value.get_secret_value())
                if url.drivername != "postgresql+psycopg" or not url.database:
                    raise ValueError
            except Exception:
                raise ValueError(
                    "Use a postgresql+psycopg URL with a database"
                ) from None
        return value
