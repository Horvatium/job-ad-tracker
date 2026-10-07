from datetime import date

import pytest

import mojedelo_pomocnik as m
from conftest import TODAY, json_fixture


def test_zrsz_ad():
    res = m.from_zrsz(json_fixture("zrsz_3489278.json"), today=TODAY)
    assert res["ok"]
    assert res["title"] == "Natakar (m/ž)"
    # sedež, ki ga ZRSZ pripne imenu podjetja (», ŠEMPETER PRI GORICI«), je odstranjen
    assert res["company"] == "ALEKSANDRO, restavracije in gostilne, d.o.o."
    assert res["location"] == "Nova Gorica"
    assert res["salary"] == "1.481,88 EUR bruto mesečno"
    assert res["deadline"] == "2026-10-16"
    assert res["warnings"] == []
    assert res["notes"].startswith("Objavljeno 6. 10. 2026. Naloge: Pripravljalna dela v jedilnici")
    assert "Zahteve: izobrazba srednja poklicna" in res["notes"]
    assert "Prijava: kandidati naj pokličejo za razgovor" in res["notes"]


def test_zrsz_expired():
    res = m.from_zrsz(json_fixture("zrsz_3489278.json"), today=date(2026, 10, 17))
    assert "Rok prijave je potekel." in res["warnings"]


@pytest.mark.parametrize("raw, title", [
    ("NATAKAR - M/Ž", "Natakar (m/ž)"),
    ("KUHAR – M/Ž/D", "Kuhar (m/ž)"),
    ("VOZNIK (M/Ž)", "Voznik (m/ž)"),
    ("Prodajalec v trgovini", "Prodajalec v trgovini"),
])
def test_zrsz_title(raw, title):
    assert m.from_zrsz({"nazivDelovnegaMesta": raw}, today=TODAY)["title"] == title


@pytest.mark.parametrize("raw, clean", [
    ("ALEKSANDRO, restavracije in gostilne, d.o.o., ŠEMPETER PRI GORICI",
     "ALEKSANDRO, restavracije in gostilne, d.o.o."),
    ("PODJETJE D.O.O.", "PODJETJE D.O.O."),
    ("MIZARSTVO NOVAK, S.P.", "MIZARSTVO NOVAK, S.P."),
])
def test_clean_zrsz_company(raw, clean):
    assert m._clean_zrsz_company(raw) == clean


@pytest.mark.parametrize("url, pid", [
    ("https://www.ess.gov.si/iskalci-zaposlitve/iskanje-zaposlitve/iskanje-dela/?idp=3489278/#/pdm/3489278", "3489278"),
    ("https://www.ess.gov.si/iskalci-zaposlitve/iskanje-zaposlitve/iskanje-dela/#/pdm/3489278", "3489278"),
    ("https://www.ess.gov.si/iskalci-zaposlitve/iskanje-zaposlitve/iskanje-dela/", None),
])
def test_zrsz_id_from_url(url, pid):
    assert m.zrsz_id_from_url(url) == pid


def test_get_zrsz_without_id(no_network):
    res = m.get_zrsz("https://www.ess.gov.si/iskalci-zaposlitve/")
    assert not res["ok"] and "številke oglasa" in res["error"]


def test_get_zrsz_refreshes_key_on_401(no_network):
    seen = []

    def fake_get(url, headers=None, timeout=25):
        seen.append(url)
        if "user_key=" in url and len([u for u in seen if "user_key=" in u]) == 1:
            return 401, ""
        if "user_key=" in url:
            from conftest import read_fixture
            return 200, read_fixture("zrsz_3489278.json")
        return 200, "<script>var cfg={user_key:'0123456789abcdef0123456789abcdef'}</script>"

    no_network.setattr(m, "http_get", fake_get)
    no_network.setattr(m, "_zrsz_key", None)
    res = m.get_zrsz("https://www.ess.gov.si/x/#/pdm/3489278")
    assert res["ok"]
    assert seen[-1].endswith("user_key=0123456789abcdef0123456789abcdef")
