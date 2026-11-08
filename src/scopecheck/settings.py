"""Keyless configuration for the current milestone."""

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SCOPECHECK_",
        extra="forbid",
        hide_input_in_errors=True,
        frozen=True,
    )

    # Reject provider mode until its explicitly approved milestone exists.
    mode: Literal["fake"] = "fake"
