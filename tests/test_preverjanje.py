from datetime import date

import pytest

import mojedelo_baza as baza
import mojedelo_preverjanje as pr

TODAY = date(2026, 10, 7)


def job(i, **kw):
    j = {"id": i, "url": f"https://www.mojedelo.com/oglas/x/{i}", "title": f"Oglas {i}", "company": "Podjetje d.o.o.",
         "deadline": "2026-10-20", "status": "new"}
    j.update(kw)
    return j


@pytest.fixture
def store(tmp_path):
    s = baza.Store(str(tmp_path / "oglasi.db"))
    yield s
    s.close()


def fake_fetch(results):
    calls = []

    def fetch(url):
        calls.append(url)
        r = results[url.rsplit("/", 1)[1]]
        if isinstance(r, Exception):
            raise r
        return r
    fetch.calls = calls
    return fetch


@pytest.mark.parametrize("res, state", [
    ({"ok": True, "deadline": "2026-10-20", "warnings": []}, "ok"),
    ({"ok": False, "error": "Oglasa ni več (404)."}, "gone"),
    ({"ok": False, "error": "Oglasa ni več na ZRSZ."}, "gone"),
    ({"ok": True, "warnings": ["Oglas ni več objavljen (stanje: archived)."]}, "gone"),
    ({"ok": True, "deadline": "2026-10-01", "warnings": ["Rok prijave je potekel."]}, "expired"),
    ({"ok": False, "error": "Povezava ni uspela: timed out"}, "error"),
])
def test_classify(res, state):
    assert pr.classify(res, TODAY)[0] == state


def test_run_check(store):
    store.apply_batch([job("a"), job("b", deadline="2026-10-10", dlApprox=True), job("c"), job("d"),
                       job("skip1", status="applied"), job("skip2", demo=True), job("skip3", deadline="2026-10-01"),
                       job("other", url="https://example.com/oglas/other")])
    fetch = fake_fetch({
        "a": {"ok": True, "deadline": "2026-10-20"},
        "b": {"ok": True, "deadline": "2026-10-11", "dlApprox": False},   # natančen rok zamenja ocenjenega
        "c": {"ok": False, "error": "Oglasa ni več (404)."},
        "d": OSError("omrežje ne dela"),
    })
    gone = pr.run_check(store, fetch, TODAY, pause=0, supported=lambda u: "mojedelo.com" in u)
    assert sorted(u.rsplit("/", 1)[1] for u in fetch.calls) == ["a", "b", "c", "d"]
    assert [j["id"] for j in gone] == ["c"]
    s = {j["id"]: j for j in store.list_jobs()}
    assert s["a"]["checkState"] == "ok" and s["a"]["checkedAt"]
    assert (s["b"]["deadline"], s["b"]["dlApprox"]) == ("2026-10-11", False)
    assert s["c"]["checkState"] == "gone"
    assert s["d"]["checkState"] == "error" and "omrežje" in s["d"]["checkMsg"]
    assert s["skip1"]["checkState"] is None
    assert store.get_meta("last_check") == "2026-10-07"


def test_gone_reported_only_once(store):
    store.apply_batch([job("c")])
    fetch = fake_fetch({"c": {"ok": False, "error": "Oglasa ni več (404)."}})
    assert len(pr.run_check(store, fetch, TODAY, pause=0)) == 1
    assert pr.run_check(store, fetch, TODAY, pause=0) == []


def test_due_reminders(store):
    store.apply_batch([job("today", deadline="2026-10-07"), job("tomorrow", deadline="2026-10-08"),
                       job("in3", deadline="2026-10-10"), job("in4", deadline="2026-10-11"),
                       job("applied", deadline="2026-10-08", status="applied"), job("demo", deadline="2026-10-08", demo=True),
                       job("nodl", deadline="")])
    store.apply_batch([job("gone", deadline="2026-10-08")])
    store.set_check("gone", "gone")
    items = pr.due_reminders(store, TODAY)
    assert [(j["id"], d) for j, d in items] == [("today", 0), ("tomorrow", 1), ("in3", 3)]
    assert pr.due_reminders(store, TODAY) == []                       # isti dan nič novega
    # naslednji dan: »in3« je zdaj čez 2 dni (isti opomnik d3, zato nič), »in4« čez 3 dni (nov)
    assert [j["id"] for j, _ in pr.due_reminders(store, date(2026, 10, 8))] == ["in4"]
    # dva dni pozneje: »in3« je jutri -> opomnik d1
    assert [j["id"] for j, _ in pr.due_reminders(store, date(2026, 10, 9))] == ["in3"]


def test_new_deadline_means_new_reminder(store):
    store.apply_batch([job("a", deadline="2026-10-08")])
    assert len(pr.due_reminders(store, TODAY)) == 1
    store.apply_batch([job("a", deadline="2026-10-09")])
    assert len(pr.due_reminders(store, TODAY)) == 1


def test_texts():
    items = [({"title": f"Oglas {i}", "company": "X"}, i % 3) for i in range(6)]
    title, body = pr.reminder_text(items)
    assert title == "Roki prijave se iztekajo (6)"
    assert body.splitlines()[:2] == ["Oglas 0 (X) – danes", "Oglas 1 (X) – jutri"]
    assert body.splitlines()[-1] == "… in še 2"
    assert pr.gone_text([{"title": "A", "company": ""}]) == ("Oglas ni več objavljen", "A")


class FakeNotify:
    def __init__(self):
        self.sent = []

    def __call__(self, title, body, url=None):
        self.sent.append((title, body, url))
        return True


def test_checker_tick_runs_once_per_day(store, tmp_path):
    store.apply_batch([job("a", deadline="2026-10-08"), job("c")])
    fetch = fake_fetch({"a": {"ok": True, "deadline": "2026-10-08"}, "c": {"ok": False, "error": "Oglasa ni več (404)."}})
    note = FakeNotify()
    ch = pr.Checker(store, fetch, notify=note, backup_dir=str(tmp_path / "b"), page_url="http://localhost:8765/", pause=0)
    ch.tick(TODAY)
    assert len(fetch.calls) == 2
    assert [t for t, _, _ in note.sent] == ["Oglas ni več objavljen", "Rok prijave se izteka"]
    assert all(u == "http://localhost:8765/" for _, _, u in note.sent)
    assert (tmp_path / "b" / "oglasi-2026-10-07.db").exists()
    ch.tick(TODAY)                                   # isti dan: brez novega branja in brez ponovnih obvestil
    assert len(fetch.calls) == 2 and len(note.sent) == 2
    assert ch.status() == {"running": False, "done": 2, "total": 2, "lastCheck": "2026-10-07"}


def test_checker_refuses_parallel_run(store):
    ch = pr.Checker(store, fake_fetch({}), notify=FakeNotify(), pause=0)
    ch._lock.acquire()
    assert ch.check(TODAY) is False
    ch._lock.release()
    assert ch.check(TODAY) is True


def test_windows_notification_does_not_spawn_processes(monkeypatch):
    # PowerShell, skrito zagnan iz drugega programa, protivirusni programi blokirajo; Windows gre prek ctypes
    seen = []
    monkeypatch.setattr(pr.sys, "platform", "win32")
    monkeypatch.setattr(pr, "_notify_windows", lambda *a: seen.append(a) or True)
    monkeypatch.setattr(pr.subprocess, "run", lambda *a, **k: pytest.fail("ne sme zagnati procesa"))
    assert pr.notify("Rok prijave", "Skladiščnik – jutri", "http://localhost:8765/")
    assert seen == [("Rok prijave", "Skladiščnik – jutri", "http://localhost:8765/")]


def test_notification_failure_is_not_fatal(monkeypatch):
    def boom(*a):
        raise RuntimeError("ni namizja")
    monkeypatch.setattr(pr.sys, "platform", "win32")
    monkeypatch.setattr(pr, "_notify_windows", boom)
    assert pr.notify("a", "b") is False
