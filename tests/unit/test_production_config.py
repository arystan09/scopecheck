import copy
import runpy
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
validate = runpy.run_path(
    str(Path(__file__).resolve().parents[2] / "scripts/check-production.py")
)["validate"]


def config():
    return {
        "services": {
            **{
                name: {
                    "image": "ghcr.io/owner/scopecheck@sha256:" + "a" * 64,
                    "environment": {
                        "SCOPECHECK_PUBLIC_DEMO": "true",
                        "SCOPECHECK_MODE": "fake",
                    },
                    "networks": {"private": {}},
                }
                for name in ("app", "worker", "migrate")
            },
            "db": {},
        },
        "networks": {"private": {"internal": True}},
    }


def test_production_private_fake_digest():
    validate(config())


@pytest.mark.parametrize(
    "change", ["tag", "different", "key", "mode", "port", "network"]
)
def test_production_rejects_unsafe_configuration(change):
    value = copy.deepcopy(config())
    app = value["services"]["app"]
    if change == "tag":
        for name in ("app", "worker", "migrate"):
            value["services"][name]["image"] = "ghcr.io/owner/scopecheck:latest"
    elif change == "different":
        app["image"] = "ghcr.io/owner/scopecheck@sha256:" + "b" * 64
    elif change == "key":
        app["environment"]["OPENAI_API_KEY"] = "forbidden"
    elif change == "mode":
        app["environment"]["SCOPECHECK_PUBLIC_DEMO"] = "false"
    elif change == "port":
        value["services"]["db"]["ports"] = ["5432:5432"]
    else:
        value["networks"]["private"]["internal"] = False
    with pytest.raises(ValueError):
        validate(value)
