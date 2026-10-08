import socket
from datetime import date

import pytest

import mojedelo_pomocnik as m
from conftest import TODAY, read_fixture

MERCATOR = "https://www.mercatorgroup.si/sl/kariera/prosta-delovna-mesta/komisionar-mz-v-cash-and-carry-murska-sobota-kgq21my-11629/"


def test_mercator_page_without_json_ld():
    res = m.from_generic(read_fixture("mercator_11629.html"), MERCATOR, today=TODAY)
    assert res["ok"] and res["warnings"] == []
    assert res["title"] == "Komisionar (m/ž) v Cash & Carry Murska Sobota"
    assert res["company"] == "Mercator d.o.o."
    assert res["location"] == "Murska Sobota"
    assert res["type"] == "nedoločen čas"
    # »Trajanje razpisa: 29.09.2026 - 13.10.2026« in »Prijave pričakujemo do 13.10.2026«
    assert res["deadline"] == "2026-10-13"
    assert res["notes"].startswith("Objavljeno 29. 9. 2026. Naloge: pripravo blaga na podlagi delovne dokumentacije")
    assert "Zahteve: dokončano IV. stopnjo izobrazbe" in res["notes"]


JSON_LD_PAGE = """<html><head><title>Careers</title>
<script type="application/ld+json">{"@context": "https://schema.org", "@type": "JobPosting",
 "title": "Backend razvijalec (m/ž)", "datePosted": "2026-10-01", "validThrough": "2026-10-31T23:59",
 "employmentType": "FULL_TIME",
 "hiringOrganization": {"@type": "Organization", "name": "Primer Tech d.o.o."},
 "jobLocation": {"@type": "Place", "address": {"addressLocality": "Ljubljana", "addressCountry": "SI"}},
 "baseSalary": {"@type": "MonetaryAmount", "currency": "EUR", "value": {"minValue": 2500, "maxValue": 3200}},
 "description": "<h3>Vaše naloge:</h3><ul><li>razvoj API-jev</li><li>pregled kode</li></ul><h3>Pričakujemo:</h3><ul><li>3 leta izkušenj s Pythonom</li><li>SQL</li></ul>"}
</script></head><body><div id="app"></div></body></html>"""


def test_json_ld_job_posting():
    res = m.from_generic(JSON_LD_PAGE, "https://kariera.primer.si/oglas/42", today=TODAY)
    assert res["ok"] and res["warnings"] == []
    assert (res["title"], res["company"], res["location"]) == ("Backend razvijalec (m/ž)", "Primer Tech d.o.o.", "Ljubljana")
    assert res["type"] == "polni delovni čas"
    assert res["salary"] == "2500–3200 EUR"
    assert res["deadline"] == "2026-10-31"
    assert res["notes"] == ("Objavljeno 1. 10. 2026. Naloge: razvoj API-jev; pregled kode. "
                            "Zahteve: 3 leta izkušenj s Pythonom; SQL.")


def test_javascript_page_still_adds_with_warning():
    res = m.from_generic("<html><head><title>Senior Developer | Podjetje d.o.o.</title></head>"
                         "<body><div id='root'></div></body></html>", "https://jobs.podjetje.si/1", today=TODAY)
    assert res["ok"] and res["title"] == "Senior Developer" and res["company"] == "Podjetje d.o.o."
    assert "JavaScriptom" in res["warnings"][0]


def test_company_falls_back_to_domain():
    res = m.from_generic("<title>Skladiščnik</title><p>Prijave do 20. 10. 2026</p>", "https://www.podjetje.si/x", today=TODAY)
    assert res["company"] == "podjetje.si" and res["deadline"] == "2026-10-20"


@pytest.mark.parametrize("text, expected", [
    ("13.10.2026", [date(2026, 10, 13)]),
    ("13. 10. 2026 in 1.11.2026", [date(2026, 10, 13), date(2026, 11, 1)]),
    ("do 13. oktobra 2026", [date(2026, 10, 13)]),
    ("5. marec 2027", [date(2027, 3, 5)]),
    ("31. 2. 2026", []),          # neobstoječ datum
])
def test_find_dates(text, expected):
    assert [d for _, d in m.find_dates(text)] == expected


@pytest.mark.parametrize("lines, expected", [
    (["Trajanje razpisa: 29.09.2026 - 13.10.2026"], date(2026, 10, 13)),
    (["Prijave sprejemamo do 13. oktobra 2026, začetek dela 1. 11. 2026."], date(2026, 10, 13)),
    (["Rok za prijavo:", "20.10.2026"], date(2026, 10, 20)),
    (["Zaposlitev za določen čas do 31. 12. 2027.", "Prijave do 15. 10. 2026"], date(2026, 10, 15)),
    (["Zaposlitev za določen čas do 31. 12. 2027."], None),     # to ni rok prijave
])
def test_generic_deadline(lines, expected):
    assert m.find_generic_deadline(lines) == expected


@pytest.mark.parametrize("raw, fixed", [
    ("KOMISIONAR (m/ž) v Cash & Carry", "Komisionar (m/ž) v Cash & Carry"),
    ("SKLADIŠČNI DELAVEC", "Skladiščni delavec"),
    ("IT svetovalec za SAP", "IT svetovalec za SAP"),
])
def test_fix_caps(raw, fixed):
    assert m.fix_caps(raw) == fixed


@pytest.mark.parametrize("title, parts", [
    ("Komisionar » Mercator d.o.o.", ["Komisionar", "Mercator d.o.o."]),
    ("Razvijalec | Kariera | Podjetje", ["Razvijalec", "Kariera", "Podjetje"]),
    ("Razvijalec – Podjetje d.o.o.", ["Razvijalec", "Podjetje d.o.o."]),
    ("Razvijalec – hibridno | Podjetje", ["Razvijalec – hibridno", "Podjetje"]),
])
def test_split_page_title(title, parts):
    assert m.split_page_title(title) == parts


# ---- varnost: pomočnik ne sme brati tega računalnika ali lokalnega omrežja

def fake_dns(ip):
    return lambda host, port, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]


@pytest.mark.parametrize("ip, ok", [
    ("93.184.216.34", True), ("127.0.0.1", False), ("10.0.0.5", False), ("192.168.1.1", False),
    ("169.254.169.254", False), ("::1", False), ("fd00::1", False), ("0.0.0.0", False),
])
def test_check_public_url_by_ip(monkeypatch, ip, ok):
    monkeypatch.setattr(m.socket, "getaddrinfo", fake_dns(ip))
    if ok:
        m.check_public_url("https://kariera.primer.si/oglas")
    else:
        with pytest.raises(m.UnsafeURL):
            m.check_public_url("https://kariera.primer.si/oglas")


@pytest.mark.parametrize("url", ["file:///C:/Windows/win.ini", "ftp://primer.si/", "javascript:alert(1)", "https:///nic"])
def test_check_public_url_scheme(url):
    with pytest.raises(m.UnsafeURL):
        m.check_public_url(url)


def test_redirect_to_local_address_is_blocked():
    with pytest.raises(m.UnsafeURL):
        m._PublicRedirects().redirect_request(None, None, 302, "Found", {}, "http://127.0.0.1:8765/api/jobs")


def test_get_generic_results(monkeypatch):
    responses = {
        MERCATOR: (200, read_fixture("mercator_11629.html"), MERCATOR, "text/html"),
        "https://a.si/oglas/gone": (404, "", "https://a.si/oglas/gone", ""),
        "https://a.si/oglas/pdf": (200, "%PDF", "https://a.si/oglas/pdf", "application/pdf"),
        "https://a.si/kariera/oglas-5": (200, "<title>Kariera</title>", "https://a.si/kariera/", "text/html"),
    }
    monkeypatch.setattr(m, "fetch_public", lambda url, timeout=25: responses[url])
    assert m.get_ad(MERCATOR)["company"] == "Mercator d.o.o."      # stran podjetja gre v splošni bralnik
    assert m.get_ad("https://a.si/oglas/gone") == {"ok": False, "error": "Oglasa ni več (404)."}
    assert "PDF" in m.get_ad("https://a.si/oglas/pdf")["error"]
    assert m.get_ad("https://a.si/kariera/oglas-5")["warnings"][0].startswith("Oglas ni več objavljen")
