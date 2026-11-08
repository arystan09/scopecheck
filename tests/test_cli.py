import subprocess

import pytest

pytestmark = pytest.mark.unit


def test_installed_cli_help() -> None:
    result = subprocess.run(
        ["scopecheck", "--help"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0
    assert "usage: scopecheck" in result.stdout
    assert "synthetic offline evaluation" in result.stdout
    assert "dataset" in result.stdout
    assert "replay" in result.stdout


def test_cli_rejects_unknown_arguments() -> None:
    result = subprocess.run(
        ["scopecheck", "--unknown"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 2
