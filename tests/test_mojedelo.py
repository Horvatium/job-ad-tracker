import json
from datetime import date, timedelta

import pytest

import mojedelo_pomocnik as m
from conftest import TODAY, json_fixture, read_fixture


def test_api_merkur():
    res = m.from_api(json_fixture("mojedelo_merkur.json")["data"], today=TODAY)
    assert res["ok"]
    assert res["title"] == "Skladiščnik (m/ž) TC MERKUR Murska Sobota"
    assert res["company"] == "MERKUR trgovina, d.o.o."
    assert res["location"] == "Murska Sobota"
    assert res["type"] == "nedoločen čas, polni delovni čas, poskusna doba 6 mes., dve izmeni"
    assert res["deadline"] == "2026-10-11"
    assert res["dlApprox"] is False
    assert res["warnings"] == []
    assert res["notes"].startswith("Objavljeno 25. 9. 2026. Izkušnje: 0 do 1 leto. Naloge: prevzem")
    assert "Zahteve: opravljen izpit za voznika viličarja je prednost" in res["notes"]


def test_api_novum():
    res = m.from_api(json_fixture("mojedelo_novum.json")["data"], today=TODAY)
    assert res["company"] == "Novum-RGI Germany GmbH Podružnica Maribor"
    assert res["location"] == "Maribor"
    assert res["type"] == "nedoločen čas, polni delovni čas, poskusna doba 3 mes."
    assert res["deadline"] == "2026-10-22"
    assert "Java EE" in res["notes"]


def test_api_notes_are_shortened():
    res = m.from_api(json_fixture("mojedelo_novum.json")["data"], today=TODAY)
    req = res["notes"].split("Zahteve: ", 1)[1]
    assert len(req) <= 321 and req.endswith("…")


def test_api_expired_deadline_warns():
    res = m.from_api(json_fixture("mojedelo_merkur.json")["data"], today=date(2026, 11, 1))
    assert "Rok prijave je potekel." in res["warnings"]


def test_api_unpublished_warns():
    data = dict(json_fixture("mojedelo_merkur.json")["data"], status="archived")
    res = m.from_api(data, today=TODAY)
    assert any("ni več objavljen" in w for w in res["warnings"])


@pytest.mark.parametrize("utc, local", [
    ("2026-10-11T21:59:59Z", date(2026, 10, 11)),   # poletni čas: +2 h
    ("2026-10-10T22:00:00Z", date(2026, 10, 11)),
    ("2026-12-01T22:30:00Z", date(2026, 12, 1)),    # zimski čas: +1 h
    ("2026-12-01T23:30:00Z", date(2026, 12, 2)),
    ("2026-10-25T00:30:00Z", date(2026, 10, 25)),   # tik pred prehodom na zimski čas
    ("2026-10-25T23:30:00Z", date(2026, 10, 26)),   # po prehodu
    ("2026-03-29T00:30:00Z", date(2026, 3, 29)),    # pred prehodom na poletni čas
    ("2026-03-29T22:30:00Z", date(2026, 3, 30)),
])
def test_to_local_date(utc, local):
    assert m.to_local_date(utc) == local


def test_ad_id_from_url():
    url = "https://www.mojedelo.com/oglas/komisionar-mz/C9D763CF-3277-4ead-a81c-c7bf0909886a"
    assert m.ad_id_from_url(url) == "c9d763cf-3277-4ead-a81c-c7bf0909886a"
    assert m.ad_id_from_url("https://www.mojedelo.com/iskanje") is None


# ---- rezerva: razčlenjevanje HTML/besedila, ko API ne deluje

def test_html_shell_has_no_content():
    res = m.extract(read_fixture("mojedelo_shell.html"), today=TODAY)
    assert not res["ok"]
    assert "JavaScriptom" in res["warnings"][0]


AD_TEXT = """Skladiščnik
25. 9. 2026
Podjetje d.o.o.
Murska Sobota
m/ž
Vaše naloge:
prevzem in izdaja blaga
delo z viličarjem
Pričakujemo:
IV. stopnja izobrazbe tehnične smeri
izpit za viličarja je prednost
Nudimo:
zaposlitev za nedoločen čas, delo v dveh izmenah
Za prijavo imate na voljo še 5 dni
"""


def test_text_fallback_header_and_estimated_deadline():
    res = m.extract(AD_TEXT, today=TODAY)
    assert res["ok"]
    assert (res["title"], res["company"], res["location"]) == ("Skladiščnik", "Podjetje d.o.o.", "Murska Sobota")
    assert res["type"] == "nedoločen čas, dve izmeni"
    # »še 5 dni« = danes + 4
    assert res["deadline"] == (TODAY + timedelta(days=4)).isoformat()
    assert res["dlApprox"] is True
    assert "Naloge: prevzem in izdaja blaga; delo z viličarjem." in res["notes"]
    assert "Zahteve: IV. stopnja izobrazbe tehnične smeri; izpit za viličarja je prednost" in res["notes"]


def test_text_fallback_explicit_deadline_wins():
    res = m.extract(AD_TEXT + "Rok za prijavo: 20. 10. 2026\n", today=TODAY)
    assert res["deadline"] == "2026-10-20"
    assert res["dlApprox"] is False


# ---- usmerjanje v get_ad (brez omrežja)

URL = "https://www.mojedelo.com/oglas/skladiscnik-mz-tc-merkur-murska-sobota/14416613-0c78-4ff3-a127-6c1ce12d5088"


def test_get_ad_uses_api(no_network):
    calls = []

    def fake_get(url, headers=None, timeout=25):
        calls.append((url, headers))
        return 200, read_fixture("mojedelo_merkur.json")

    no_network.setattr(m, "http_get", fake_get)
    res = m.get_ad(URL)
    assert res["company"] == "MERKUR trgovina, d.o.o."
    assert calls[0][0] == "https://api.mojedelo.com/job-ads/14416613-0c78-4ff3-a127-6c1ce12d5088"
    assert {"tenantid", "channelid", "languageid"} <= set(calls[0][1])


def test_get_ad_404(no_network):
    no_network.setattr(m, "http_get", lambda *a, **k: (404, ""))
    res = m.get_ad(URL)
    assert res == {"ok": False, "error": "Oglasa ni več (404)."}


def test_get_ad_falls_back_to_html(no_network):
    def fake_get(url, headers=None, timeout=25):
        if url.startswith(m.API_BASE):
            return 500, json.dumps({"error": "boom"})
        return 200, "<html><body>" + AD_TEXT.replace("\n", "<br>") + "</body></html>"

    no_network.setattr(m, "http_get", fake_get)
    res = m.get_ad(URL)
    assert res["ok"]
    assert res["company"] == "Podjetje d.o.o."
