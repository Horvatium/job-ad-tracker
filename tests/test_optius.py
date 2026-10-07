import mojedelo_pomocnik as m
from conftest import TODAY, read_fixture


def test_optius_ad():
    res = m.from_optius(read_fixture("optius_962969.html"), today=TODAY)
    assert res["ok"]
    assert res["title"] == "Arhitekt in razvijalec informacijskih rešitev (m/ž)"
    assert res["company"] == "POŠTA SLOVENIJE d.o.o."
    assert res["location"] == "Maribor"
    assert res["deadline"] == "2026-10-21"
    assert res["warnings"] == []
    assert res["notes"].startswith("Objavljeno 7. 10. 2026. Izkušnje: 1 - 3 let. Naloge: Analiziranje in zasnova")


def test_optius_notes_have_no_html_entities_or_subheadings():
    notes = m.from_optius(read_fixture("optius_962969.html"), today=TODAY)["notes"]
    assert "&" not in notes
    assert "Opis del in nalog" not in notes
    assert "Pogoji za zasedbo" not in notes
    assert "rešitev" in notes


def test_optius_sections():
    sec = m._optius_sections(read_fixture("optius_962969.html"))
    assert {"opis delovnega mesta", "pričakujemo", "ponujamo"} <= set(sec)


def test_optius_expired_ad_redirects_to_listing():
    # Ko oglasa ni več, Optius preusmeri na seznam: ni JobPosting in ni razdelkov.
    res = m.from_optius("<html><h1>Prosta delovna mesta</h1></html>", today=TODAY)
    assert not res["ok"]
    assert "ni več" in res["error"]


def test_optius_ld_with_raw_newlines():
    markup = ('<script type="application/ld+json">{"@type":"JobPosting","title":"Vrstica 1\nVrstica 2",'
              '"hiringOrganization":{"name":"X d.o.o."}}</script>')
    assert m._ld_job_posting(markup)["hiringOrganization"]["name"] == "X d.o.o."
