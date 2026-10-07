"""
Shramba oglasov v SQLite za mojedelo_pomocnik.py (samo standardna knjižnica).

Stran pošilja oglase v isti obliki, kot jih hrani v brskalniku (JSON s polji id, url, title …).
Polja, ki jih nastavlja samo pomočnik (statusAt, checkedAt, checkState, checkMsg), stran
ne more prepisati, zato se samodejno preverjanje v ozadju in urejanje na strani ne motita.
"""
import os
import re
import sqlite3
import threading
import time
from datetime import date

STATUSES = ("new", "interesting", "applied", "waiting", "rejected")
SCHEMA_VERSION = 1
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TEXT_FIELDS = ("url", "title", "company", "location", "type", "deadline", "salary", "notes")
MAX_LEN = {"url": 2000, "notes": 20000}  # ostala besedila do 500 znakov
BACKUP_RE = re.compile(r"^oglasi-\d{4}-\d{2}-\d{2}\.db$")

SCHEMA = """
CREATE TABLE jobs (
    id          TEXT PRIMARY KEY,
    url         TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    company     TEXT NOT NULL DEFAULT '',
    location    TEXT NOT NULL DEFAULT '',
    type        TEXT NOT NULL DEFAULT '',
    deadline    TEXT NOT NULL DEFAULT '',     -- YYYY-MM-DD ali prazno
    dl_approx   INTEGER NOT NULL DEFAULT 0,
    salary      TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'new',
    star        INTEGER NOT NULL DEFAULT 0,
    notes       TEXT NOT NULL DEFAULT '',
    auto_notes  TEXT,                         -- opomba, ki jo je napisal program
    demo        INTEGER NOT NULL DEFAULT 0,
    added       INTEGER NOT NULL DEFAULT 0,   -- ms od 1970, kot Date.now()
    updated     INTEGER NOT NULL DEFAULT 0,
    status_at   INTEGER,                      -- zadnja sprememba statusa
    checked_at  INTEGER,                      -- zadnje samodejno preverjanje
    check_state TEXT,                         -- ok | gone | expired | error
    check_msg   TEXT
);
CREATE TABLE status_history (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    status TEXT NOT NULL,
    at     INTEGER NOT NULL
);
CREATE INDEX status_history_job ON status_history(job_id);
CREATE TABLE notifications (
    job_id TEXT NOT NULL,
    key    TEXT NOT NULL,                     -- npr. »d3:2026-10-11«, da se opomnik ne ponovi
    at     INTEGER NOT NULL,
    PRIMARY KEY (job_id, key)
);
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def now_ms():
    return int(time.time() * 1000)


def clean_job(raw):
    """Preveri in očisti oglas, ki ga pošlje stran. Ob neveljavnem vnosu sproži ValueError."""
    if not isinstance(raw, dict):
        raise ValueError("oglas mora biti objekt")
    jid = str(raw.get("id") or "")
    if not ID_RE.match(jid):
        raise ValueError(f"neveljaven id oglasa: {jid[:70]!r}")
    out = {"id": jid}
    for k in TEXT_FIELDS:
        v = raw.get(k)
        out[k] = ("" if v is None else str(v))[:MAX_LEN.get(k, 500)]
    if not out["url"]:
        raise ValueError(f"oglas {jid} nima povezave")
    if out["deadline"] and not DATE_RE.match(out["deadline"]):
        out["deadline"] = ""
    out["status"] = raw.get("status") if raw.get("status") in STATUSES else "new"
    out["dlApprox"] = bool(raw.get("dlApprox"))
    out["star"] = bool(raw.get("star"))
    out["demo"] = bool(raw.get("demo"))
    an = raw.get("autoNotes")
    out["autoNotes"] = None if an is None else str(an)[:MAX_LEN["notes"]]
    try:
        out["added"] = int(raw.get("added") or 0)
    except (TypeError, ValueError):
        out["added"] = 0
    return out


def row_to_job(r):
    j = {k: r[k] for k in TEXT_FIELDS}
    j.update(id=r["id"], dlApprox=bool(r["dl_approx"]), status=r["status"], star=bool(r["star"]),
             demo=bool(r["demo"]), added=r["added"], statusAt=r["status_at"], checkedAt=r["checked_at"],
             checkState=r["check_state"], checkMsg=r["check_msg"])
    if r["auto_notes"] is not None:  # brez ključa, kot v brskalniku: stran preverja »autoNotes!==undefined«
        j["autoNotes"] = r["auto_notes"]
    return j


class Store:
    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()
        # ena povezava za vse niti (strežnik in preverjanje v ozadju), dostop varuje _lock
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._migrate()

    def _migrate(self):
        with self._lock, self.db:
            ver = self.db.execute("PRAGMA user_version").fetchone()[0]
            if ver < 1:
                self.db.executescript(SCHEMA)
            # tu bodo nadaljnje spremembe sheme: if ver < 2: ALTER TABLE …
            self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self):
        with self._lock:
            self.db.close()

    # ---- meta

    def get_meta(self, key, default=None):
        with self._lock:
            r = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return r[0] if r else default

    def set_meta(self, key, value):
        with self._lock, self.db:
            self.db.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                            "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))

    def initialized(self):
        """Ali je stran bazo že napolnila (iz brskalnika ali s primeri). Prazna baza je lahko tudi namerna."""
        return self.get_meta("initialized") == "1"

    # ---- oglasi

    def list_jobs(self):
        with self._lock:
            rows = self.db.execute("SELECT * FROM jobs ORDER BY added DESC").fetchall()
        return [row_to_job(r) for r in rows]

    def get_job(self, job_id):
        with self._lock:
            r = self.db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return row_to_job(r) if r else None

    def apply_batch(self, upsert=(), delete=(), init=False):
        """Shrani spremenjene in izbriše odstranjene oglase v eni transakciji. Vrne shranjene oglase."""
        jobs = [clean_job(x) for x in upsert]  # najprej preveri vse, da se ob napaki ne shrani pol paketa
        delete = [str(x) for x in delete if ID_RE.match(str(x))]
        t = now_ms()
        with self._lock, self.db:
            for j in jobs:
                vals = (j["url"], j["title"], j["company"], j["location"], j["type"], j["deadline"],
                        int(j["dlApprox"]), j["salary"], j["status"], int(j["star"]), j["notes"],
                        j["autoNotes"], int(j["demo"]), j["added"], t)
                old = self.db.execute("SELECT status FROM jobs WHERE id = ?", (j["id"],)).fetchone()
                if old is None:
                    self.db.execute(
                        "INSERT INTO jobs (url, title, company, location, type, deadline, dl_approx, salary,"
                        " status, star, notes, auto_notes, demo, added, updated, id, status_at)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", vals + (j["id"], t))
                    self._history(j["id"], j["status"], t)
                else:
                    self.db.execute(
                        "UPDATE jobs SET url = ?, title = ?, company = ?, location = ?, type = ?, deadline = ?,"
                        " dl_approx = ?, salary = ?, status = ?, star = ?, notes = ?, auto_notes = ?, demo = ?,"
                        " added = ?, updated = ? WHERE id = ?", vals + (j["id"],))
                    if old["status"] != j["status"]:
                        self.db.execute("UPDATE jobs SET status_at = ? WHERE id = ?", (t, j["id"]))
                        self._history(j["id"], j["status"], t)
            for jid in delete:
                for table, col in (("jobs", "id"), ("status_history", "job_id"), ("notifications", "job_id")):
                    self.db.execute(f"DELETE FROM {table} WHERE {col} = ?", (jid,))
            if init:
                self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('initialized', '1')")
        return [self.get_job(j["id"]) for j in jobs]

    def _history(self, job_id, status, t):
        self.db.execute("INSERT INTO status_history (job_id, status, at) VALUES (?, ?, ?)", (job_id, status, t))

    def history(self, job_id):
        with self._lock:
            rows = self.db.execute("SELECT status, at FROM status_history WHERE job_id = ? ORDER BY id",
                                   (job_id,)).fetchall()
        return [(r["status"], r["at"]) for r in rows]

    # ---- samodejno preverjanje

    def jobs_for_check(self, today_iso):
        """Oglasi, pri katerih je smiselno preveriti, ali so še objavljeni."""
        with self._lock:
            rows = self.db.execute(
                "SELECT * FROM jobs WHERE demo = 0 AND status IN ('new', 'interesting')"
                " AND (deadline = '' OR deadline >= ?) ORDER BY deadline = '', deadline", (today_iso,)).fetchall()
        return [row_to_job(r) for r in rows]

    def set_check(self, job_id, state, msg="", deadline=None):
        """Zapiše izid preverjanja; natančen rok s portala zamenja prazen ali ocenjen rok."""
        with self._lock, self.db:
            self.db.execute("UPDATE jobs SET checked_at = ?, check_state = ?, check_msg = ? WHERE id = ?",
                            (now_ms(), state, msg or "", job_id))
            if deadline:
                self.db.execute("UPDATE jobs SET deadline = ?, dl_approx = 0 WHERE id = ?"
                                " AND (deadline = '' OR dl_approx = 1)", (deadline, job_id))

    def was_notified(self, job_id, key):
        with self._lock:
            return self.db.execute("SELECT 1 FROM notifications WHERE job_id = ? AND key = ?",
                                   (job_id, key)).fetchone() is not None

    def mark_notified(self, job_id, key):
        with self._lock, self.db:
            self.db.execute("INSERT OR IGNORE INTO notifications (job_id, key, at) VALUES (?, ?, ?)",
                            (job_id, key, now_ms()))

    # ---- varnostne kopije

    def backup(self, folder, keep=14, today=None):
        """Ena kopija na dan (oglasi-YYYY-MM-DD.db); obdrži zadnjih `keep`. Vrne pot nove kopije ali None."""
        today = today or date.today()
        os.makedirs(folder, exist_ok=True)
        dest = os.path.join(folder, f"oglasi-{today.isoformat()}.db")
        if os.path.exists(dest):
            return None
        tmp = dest + ".tmp"
        out = sqlite3.connect(tmp)
        try:
            with self._lock:
                self.db.backup(out)  # dosledna kopija tudi med pisanjem
        finally:
            out.close()
        os.replace(tmp, dest)
        old = sorted(f for f in os.listdir(folder) if BACKUP_RE.match(f))[:-keep]
        for f in old:
            os.remove(os.path.join(folder, f))
        return dest
