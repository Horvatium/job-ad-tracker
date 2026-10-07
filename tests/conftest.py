import json
from datetime import date
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"

# Fiksture so resnični odgovori portalov, shranjeni 7. 10. 2026.
TODAY = date(2026, 10, 7)


def read_fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def json_fixture(name):
    return json.loads(read_fixture(name))


@pytest.fixture
def no_network(monkeypatch):
    """Vsak nenačrtovan klic na splet naj test podre, ne pa ga tiho upočasni."""
    import mojedelo_pomocnik

    def fail(*args, **kwargs):
        raise AssertionError(f"nepričakovan omrežni klic: {args!r}")

    monkeypatch.setattr(mojedelo_pomocnik, "http_get", fail)
    return monkeypatch
