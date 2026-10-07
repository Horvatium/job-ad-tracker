"""
Samodejno delo pomočnika v ozadju (samo standardna knjižnica):

- enkrat na dan preveri, ali so oglasi »Za pregled« in »Zanimivo« še objavljeni, in ocenjen rok
  zamenja z natančnim, ko je na voljo;
- obvesti o rokih, ki se iztečejo v 3 dneh ali jutri (vsak opomnik samo enkrat);
- obvesti, ko oglas ni več objavljen;
- enkrat na dan naredi varnostno kopijo baze.

Branje oglasa in pošiljanje obvestil sta podana kot funkciji, zato se da vse preizkusiti brez omrežja.
"""
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

# Windows: obvestilo prek ikone v območju za obvestila (Shell_NotifyIconW) s ctypes; Windows 10/11 ga prikaže
# kot običajno obvestilo. Namenoma brez PowerShella: skrit zagon PowerShella iz drugega programa
# (z -EncodedCommand ali -ExecutionPolicy Bypass) protivirusni programi blokirajo kot sumljiv.
WM_USER = 0x0400
CALLBACK_MSG = WM_USER + 20
NIN_BALLOONHIDE, NIN_BALLOONTIMEOUT, NIN_BALLOONUSERCLICK = 0x0403, 0x0404, 0x0405
NIM_ADD, NIM_DELETE = 0, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x01, 0x02, 0x04, 0x10
NIIF_INFO = 0x01
IDI_INFORMATION = 32516
PM_REMOVE = 0x0001


def _notify_windows(title, body, url=None, max_seconds=15):
    import ctypes
    from ctypes import wintypes as w

    user32, shell32, kernel32 = ctypes.windll.user32, ctypes.windll.shell32, ctypes.windll.kernel32
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [("style", w.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int), ("hInstance", w.HINSTANCE), ("hIcon", w.HICON),
                    ("hCursor", w.HANDLE), ("hbrBackground", w.HBRUSH), ("lpszMenuName", w.LPCWSTR),
                    ("lpszClassName", w.LPCWSTR)]

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", w.DWORD), ("Data2", w.WORD), ("Data3", w.WORD), ("Data4", w.BYTE * 8)]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [("cbSize", w.DWORD), ("hWnd", w.HWND), ("uID", w.UINT), ("uFlags", w.UINT),
                    ("uCallbackMessage", w.UINT), ("hIcon", w.HICON), ("szTip", w.WCHAR * 128),
                    ("dwState", w.DWORD), ("dwStateMask", w.DWORD), ("szInfo", w.WCHAR * 256),
                    ("uTimeoutOrVersion", w.UINT), ("szInfoTitle", w.WCHAR * 64), ("dwInfoFlags", w.DWORD),
                    ("guidItem", GUID), ("hBalloonIcon", w.HICON)]

    # brez argtypes ctypes na 64-bitnem sistemu napačno prenaša ročaje in kazalce
    user32.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
    user32.DefWindowProcW.restype = LRESULT
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    user32.RegisterClassW.restype = w.ATOM
    user32.UnregisterClassW.argtypes = [w.LPCWSTR, w.HINSTANCE]
    user32.CreateWindowExW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID]
    user32.CreateWindowExW.restype = w.HWND
    user32.DestroyWindow.argtypes = [w.HWND]
    user32.LoadIconW.argtypes = [w.HINSTANCE, ctypes.c_void_p]
    user32.LoadIconW.restype = w.HICON
    user32.PeekMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT]
    user32.TranslateMessage.argtypes = [ctypes.POINTER(w.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(w.MSG)]
    shell32.Shell_NotifyIconW.argtypes = [w.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
    shell32.Shell_NotifyIconW.restype = w.BOOL
    kernel32.GetModuleHandleW.argtypes = [w.LPCWSTR]
    kernel32.GetModuleHandleW.restype = w.HMODULE

    state = {"done": False, "clicked": False}

    def proc(hwnd, msg, wparam, lparam):
        if msg == CALLBACK_MSG:
            event = lparam & 0xFFFF
            if event == NIN_BALLOONUSERCLICK:
                state["clicked"] = state["done"] = True
            elif event in (NIN_BALLOONTIMEOUT, NIN_BALLOONHIDE):
                state["done"] = True
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    wndproc = WNDPROC(proc)  # referenca mora živeti, dokler okno obstaja
    hinst = kernel32.GetModuleHandleW(None)
    cls_name = f"MojeDeloObvestilo{threading.get_ident()}{time.monotonic_ns()}"
    wc = WNDCLASSW(lpfnWndProc=wndproc, hInstance=hinst, lpszClassName=cls_name)
    if not user32.RegisterClassW(ctypes.byref(wc)):
        return False
    hwnd = user32.CreateWindowExW(0, cls_name, "Upravljalnik oglasov", 0, 0, 0, 0, 0, None, None, hinst, None)
    if not hwnd:
        user32.UnregisterClassW(cls_name, hinst)
        return False
    nid = NOTIFYICONDATAW()
    nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
    nid.hWnd, nid.uID = hwnd, 1
    nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_INFO
    nid.uCallbackMessage = CALLBACK_MSG
    nid.hIcon = user32.LoadIconW(None, IDI_INFORMATION)
    nid.szTip = "Upravljalnik oglasov"
    nid.szInfoTitle, nid.szInfo = title[:63], body[:255]
    nid.dwInfoFlags = NIIF_INFO
    try:
        if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            return False
        # počakaj, da obvestilo izgine ali ga uporabnik klikne (ikona mora do takrat ostati)
        msg = w.MSG()
        end = time.monotonic() + max_seconds
        while not state["done"] and time.monotonic() < end:
            while user32.PeekMessageW(ctypes.byref(msg), hwnd, 0, 0, PM_REMOVE):
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            time.sleep(0.1)
        if state["clicked"] and url:
            import webbrowser
            webbrowser.open(url)
        return True
    finally:
        shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
        user32.DestroyWindow(hwnd)
        user32.UnregisterClassW(cls_name, hinst)


def notify(title, body, url=None):
    """Obvestilo na namizju (Windows, macOS, Linux z notify-send). Vrne True, če je bilo poslano."""
    print(f"  [OBVESTILO] {title}: {body.replace(chr(10), ' | ')}")
    try:
        if sys.platform == "win32":
            return _notify_windows(title, body, url)
        if sys.platform == "darwin":
            script = ["-e", "on run argv", "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
                      "-e", "end run"]
            return subprocess.run(["osascript", *script, title, body], timeout=30).returncode == 0
        if shutil.which("notify-send"):
            return subprocess.run(["notify-send", title, body], timeout=30).returncode == 0
    except Exception as e:  # obvestilo ne sme nikoli ustaviti preverjanja
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
