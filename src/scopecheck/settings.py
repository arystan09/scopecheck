"""Public app settings; the worker reads its provider key separately."""

import os
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SCOPECHECK_",
        extra="forbid",
        hide_input_in_errors=True,
        frozen=True,
    )

    # Public API creation stays fake-only; live runs require local owner authority.
    mode: Literal["fake"] = "fake"
    public_demo: bool = Field(default=False, exclude=True)

    @model_validator(mode="after")
    def public_has_no_key(self) -> "Settings":
        if self.public_demo and any(
            name in os.environ
            for name in ("SCOPECHECK_OPENAI_API_KEY", "OPENAI_API_KEY")
        ):
            raise ValueError(
                "Public demo requires no provider credential in the environment"
            )
        return self

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
