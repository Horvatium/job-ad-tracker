"""
Samodejno delo pomočnika v ozadju (samo standardna knjižnica):

- enkrat na dan preveri, ali so oglasi »Za pregled« in »Zanimivo« še objavljeni, in ocenjen rok
  zamenja z natančnim, ko je na voljo;
- obvesti o rokih, ki se iztečejo v 3 dneh ali jutri (vsak opomnik samo enkrat);
- obvesti, ko oglas ni več objavljen;
- enkrat na dan naredi varnostno kopijo baze.

Branje oglasa in pošiljanje obvestil sta podana kot funkciji, zato se da vse preizkusiti brez omrežja.
"""
import base64
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import date

ACTIVE = ("new", "interesting")


def classify(res, today):
    """Izid branja oglasa -> (stanje, sporočilo). Stanja: ok, gone, expired, error."""
    if not res.get("ok"):
        err = res.get("error") or "Branja ni bilo mogoče dokončati."
        return ("gone" if "ni več" in err.lower() else "error"), err
    for w in res.get("warnings") or []:
        if "ni več objavljen" in w:
            return "gone", w
    dl = res.get("deadline")
    if dl and dl < today.isoformat():
        return "expired", "Rok prijave je potekel."
    return "ok", ""


def run_check(store, fetch, today=None, pause=2.0, supported=lambda url: True, progress=None):
    """Preveri aktivne oglase. Vrne seznam oglasov, ki so pravkar postali neobjavljeni."""
    today = today or date.today()
    jobs = [j for j in store.jobs_for_check(today.isoformat()) if supported(j["url"])]
    gone = []
    for i, j in enumerate(jobs):
        if progress:
            progress(i, len(jobs))
        try:
            res = fetch(j["url"])
        except Exception as e:  # en oglas ne sme ustaviti preverjanja ostalih
            res = {"ok": False, "error": f"Napaka: {e}"}
        state, msg = classify(res, today)
        exact = res.get("deadline") if res.get("ok") and not res.get("dlApprox") else None
        store.set_check(j["id"], state, msg, exact)
        if state == "gone" and j.get("checkState") != "gone":
            gone.append(j)
        if pause and i < len(jobs) - 1:
            time.sleep(pause)  # vljudno do portalov: en oglas na nekaj sekund
    if progress:
        progress(len(jobs), len(jobs))
    store.set_meta("last_check", today.isoformat())
    return gone


def due_reminders(store, today=None):
    """Oglasi, o katerih je treba danes opomniti: [(oglas, dni do roka)]. Vsak opomnik le enkrat."""
    today = today or date.today()
    out = []
    for j in store.list_jobs():
        if j["demo"] or j["status"] not in ACTIVE or not j["deadline"] or j.get("checkState") == "gone":
            continue
        days = (date.fromisoformat(j["deadline"]) - today).days
        kind = "d1" if 0 <= days <= 1 else "d3" if 2 <= days <= 3 else None
        if not kind:
            continue
        key = f"{kind}:{j['deadline']}"  # nov rok = nov opomnik
        if store.was_notified(j["id"], key):
            continue
        store.mark_notified(j["id"], key)
        out.append((j, days))
    return sorted(out, key=lambda x: x[1])


def when(days):
    return "danes" if days == 0 else "jutri" if days == 1 else f"čez {days} dni"


def name(j):
    return j["title"] + (f" ({j['company']})" if j.get("company") else "")


def summarize(lines, limit=4):
    return "\n".join(lines[:limit] + ([f"… in še {len(lines) - limit}"] if len(lines) > limit else []))


def reminder_text(items):
    title = "Rok prijave se izteka" if len(items) == 1 else f"Roki prijave se iztekajo ({len(items)})"
    return title, summarize([f"{name(j)} – {when(d)}" for j, d in items])


def gone_text(jobs):
    title = "Oglas ni več objavljen" if len(jobs) == 1 else f"Oglasi niso več objavljeni ({len(jobs)})"
    return title, summarize([name(j) for j in jobs])


# ------------------------------------------------------------------ obvestila

# PowerShell skripta za obvestilo Windows 10/11. Besedilo pride prek okolja (base64), zato ga ni treba ubežati,
# v XML pa se vstavi kot besedilni vozel. AppId je PowerShell, ki je v sistemu že registriran za obvestila.
TOAST_PS = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
function D($v) { [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($v)) }
$x = New-Object Windows.Data.Xml.Dom.XmlDocument
$x.LoadXml('<toast activationType="protocol"><visual><binding template="ToastGeneric"><text/><text/></binding></visual></toast>')
if ($env:MDP_URL) { $x.DocumentElement.SetAttribute('launch', (D $env:MDP_URL)) }
$t = $x.GetElementsByTagName('text')
$t.Item(0).AppendChild($x.CreateTextNode((D $env:MDP_TITLE))) > $null
$t.Item(1).AppendChild($x.CreateTextNode((D $env:MDP_BODY))) > $null
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show(
    [Windows.UI.Notifications.ToastNotification]::new($x))
"""


def _b64(s):
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


def notify(title, body, url=None):
    """Obvestilo na namizju (Windows, macOS, Linux z notify-send). Vrne True, če je bilo poslano."""
    print(f"  [OBVESTILO] {title}: {body.replace(chr(10), ' | ')}")
    try:
        if sys.platform == "win32":
            env = dict(os.environ, MDP_TITLE=_b64(title), MDP_BODY=_b64(body), MDP_URL=_b64(url or ""))
            cmd = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                   "-EncodedCommand", base64.b64encode(TOAST_PS.encode("utf-16-le")).decode("ascii")]
            r = subprocess.run(cmd, env=env, capture_output=True, timeout=30,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return r.returncode == 0
        if sys.platform == "darwin":
            script = ["-e", "on run argv", "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
                      "-e", "end run"]
            return subprocess.run(["osascript", *script, title, body], timeout=30).returncode == 0
        if shutil.which("notify-send"):
            return subprocess.run(["notify-send", title, body], timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError) as e:
        print(f"  Obvestila ni bilo mogoče prikazati: {e}")
    return False


# ------------------------------------------------------------------ delo v ozadju

class Checker:
    def __init__(self, store, fetch, notify=notify, supported=lambda url: True, backup_dir=None,
                 page_url=None, interval=15 * 60, pause=2.0):
        self.store, self.fetch, self.notify, self.supported = store, fetch, notify, supported
        self.backup_dir, self.page_url, self.interval, self.pause = backup_dir, page_url, interval, pause
        self._lock = threading.Lock()
        self.running = False
        self.done = self.total = 0

    def status(self):
        return {"running": self.running, "done": self.done, "total": self.total,
                "lastCheck": self.store.get_meta("last_check")}

    def _progress(self, done, total):
        self.done, self.total = done, total

    def check(self, today=None):
        """Preveri oglase in pošlje obvestila. Vrne False, če preverjanje že teče."""
        if not self._lock.acquire(blocking=False):
            return False
        self.running, self.done, self.total = True, 0, 0
        try:
            gone = run_check(self.store, self.fetch, today, self.pause, self.supported, self._progress)
            if gone:
                self.notify(*gone_text(gone), self.page_url)
            self.remind(today)
        finally:
            self.running = False
            self._lock.release()
        return True

    def remind(self, today=None):
        items = due_reminders(self.store, today)
        if items:
            self.notify(*reminder_text(items), self.page_url)

    def tick(self, today=None):
        """Ena ura v zanki: kopija, dnevno preverjanje (če danes še ni bilo) in opomniki."""
        today = today or date.today()
        if self.backup_dir:
            self.store.backup(self.backup_dir, today=today)
        if self.store.get_meta("last_check") != today.isoformat():
            self.check(today)
        else:
            self.remind(today)

    def check_in_background(self):
        if self.running:
            return False
        threading.Thread(target=self.check, daemon=True).start()
        return True

    def start(self, delay=20):
        def loop():
            time.sleep(delay)  # naj se pomočnik najprej v miru zažene
            while True:
                try:
                    self.tick()
                except Exception as e:  # zanka mora preživeti tudi nepričakovano napako
                    print(f"  Napaka pri preverjanju v ozadju: {e}")
                time.sleep(self.interval)
        threading.Thread(target=loop, daemon=True).start()
