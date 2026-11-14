import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


load("backup")
restore = load("restore_check")


@pytest.mark.parametrize(
    "name,confirmation",
    [
        ("scopecheck", "scopecheck"),
        ("postgres", "postgres"),
        ("scopecheck_restore_" + "a" * 32, "wrong"),
        ("scopecheck_restore_../../db", "scopecheck_restore_../../db"),
    ],
)
def test_restore_refuses_non_disposable_before_connect(monkeypatch, name, confirmation):
    def forbidden(*args, **kwargs):
        pytest.fail("Must validate destination before any database connection")

    monkeypatch.setattr(restore.psycopg, "connect", forbidden)
    with pytest.raises(ValueError):
        restore.restore(Path("none"), Path("none"), name, confirmation)


def test_restore_accepts_explicit_disposable_name():
    name = "scopecheck_restore_" + "a" * 32
    restore.validate_destination(name, name)
