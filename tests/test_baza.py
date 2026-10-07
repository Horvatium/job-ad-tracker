import os
import sqlite3
from datetime import date

import pytest

import mojedelo_baza as baza


def job(i="a1", **kw):
    j = {"id": i, "url": f"https://www.mojedelo.com/oglas/x/{i}", "title": "Skladiščnik", "company": "Podjetje d.o.o.",
         "location": "Lendava", "type": "", "deadline": "2026-10-11", "dlApprox": False, "salary": "",
         "status": "new", "star": False, "notes": "Naloge: …", "added": 1000}
    j.update(kw)
    return j


@pytest.fixture
def store(tmp_path):
    s = baza.Store(str(tmp_path / "oglasi.db"))
    yield s
    s.close()


def test_roundtrip(store):
    store.apply_batch([job(autoNotes="Naloge: …", star=True)])
    [j] = store.list_jobs()
    assert j["title"] == "Skladiščnik" and j["star"] is True and j["dlApprox"] is False
    assert j["autoNotes"] == "Naloge: …"
    assert j["statusAt"] and j["checkState"] is None


def test_auto_notes_key_absent_when_not_set(store):
    # stran razlikuje »ni ključa« (ročna opomba) od »autoNotes === notes« (opomba programa)
    store.apply_batch([job()])
    assert "autoNotes" not in store.list_jobs()[0]


def test_status_history_and_status_at(store, monkeypatch):
    t = iter([1000, 2000, 3000])
    monkeypatch.setattr(baza, "now_ms", lambda: next(t))
    store.apply_batch([job()])
    store.apply_batch([job(notes="sprememba opombe")])       # status ostane, zgodovina ne raste
    store.apply_batch([job(status="applied")])
    assert store.history("a1") == [("new", 1000), ("applied", 3000)]
    assert store.get_job("a1")["statusAt"] == 3000


def test_page_cannot_overwrite_server_fields(store):
    store.apply_batch([job()])
    store.set_check("a1", "gone", "Oglasa ni več (404).")
    store.apply_batch([job(checkState="ok", checkMsg="", statusAt=1, title="Nov naslov")])
    j = store.get_job("a1")
    assert j["title"] == "Nov naslov"
    assert j["checkState"] == "gone" and j["checkMsg"] == "Oglasa ni več (404)."


def test_delete_removes_history_and_notifications(store):
    store.apply_batch([job(), job("b2")])
    store.mark_notified("a1", "d1:2026-10-11")
    store.apply_batch(delete=["a1"])
    assert [j["id"] for j in store.list_jobs()] == ["b2"]
    assert store.history("a1") == [] and not store.was_notified("a1", "d1:2026-10-11")


def test_invalid_batch_saves_nothing(store):
    with pytest.raises(ValueError):
        store.apply_batch([job("ok1"), job("ne veljaven id")])
    assert store.list_jobs() == []


@pytest.mark.parametrize("bad", [None, "niz", {"id": "a1"}, {"id": "", "url": "https://x"}])
def test_clean_job_rejects(bad):
    with pytest.raises(ValueError):
        baza.clean_job(bad)


def test_clean_job_normalises():
    j = baza.clean_job(job(status="hacked", deadline="11. 10. 2026", title="x" * 900, added="abc"))
    assert j["status"] == "new" and j["deadline"] == "" and len(j["title"]) == 500 and j["added"] == 0


def test_initialized_flag(store):
    assert not store.initialized()
    store.apply_batch([], init=True)          # tudi prazen seznam je lahko namerna izbira
    assert store.initialized()


def test_set_check_replaces_only_missing_or_estimated_deadline(store):
    store.apply_batch([job("est", deadline="2026-10-10", dlApprox=True), job("man", deadline="2026-10-09")])
    store.set_check("est", "ok", deadline="2026-10-11")
    store.set_check("man", "ok", deadline="2026-10-11")
    assert (store.get_job("est")["deadline"], store.get_job("est")["dlApprox"]) == ("2026-10-11", False)
    assert store.get_job("man")["deadline"] == "2026-10-09"   # ročno vnesenega roka ne povozi


def test_jobs_for_check(store):
    store.apply_batch([job("a", status="new"), job("b", status="applied"), job("c", demo=True),
                       job("d", deadline="2026-10-01"), job("e", deadline="", status="interesting")])
    assert [j["id"] for j in store.jobs_for_check("2026-10-07")] == ["a", "e"]


def test_schema_version(store):
    assert store.db.execute("PRAGMA user_version").fetchone()[0] == baza.SCHEMA_VERSION


def test_reopen_keeps_data(tmp_path):
    p = str(tmp_path / "oglasi.db")
    s = baza.Store(p)
    s.apply_batch([job()], init=True)
    s.close()
    s2 = baza.Store(p)
    assert s2.initialized() and s2.get_job("a1")["title"] == "Skladiščnik"
    s2.close()


def test_backup_once_per_day_and_rotation(store, tmp_path):
    store.apply_batch([job()])
    folder = str(tmp_path / "backups")
    first = store.backup(folder, keep=3, today=date(2026, 10, 1))
    assert first and store.backup(folder, keep=3, today=date(2026, 10, 1)) is None
    for d in range(2, 6):
        store.backup(folder, keep=3, today=date(2026, 10, d))
    assert sorted(os.listdir(folder)) == ["oglasi-2026-10-03.db", "oglasi-2026-10-04.db", "oglasi-2026-10-05.db"]
    copy = sqlite3.connect(os.path.join(folder, "oglasi-2026-10-05.db"))
    assert copy.execute("SELECT title FROM jobs").fetchone()[0] == "Skladiščnik"
    copy.close()
