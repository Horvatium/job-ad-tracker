#!/usr/bin/env python3
"""
Lokalni pomočnik za stran mojedelo-oglasi.html

Zagon:   python mojedelo_pomocnik.py      (v Windows tudi:  py mojedelo_pomocnik.py)
Nato se samodejno odpre http://localhost:8765 in stran ob dodajanju URL-ja
sama izpolni naslov, podjetje, kraj, vrsto zaposlitve, rok prijave in zahteve.

Podprti portali: mojedelo.com, ZRSZ (ess.gov.si, Iskanje dela) in Optius (optius.com).
Potrebuje samo Python 3.8+ (brez dodatnih paketov). Program posluša samo na
tvojem računalniku (127.0.0.1) in bere samo strani na podprtih portalih.
"""
import html
import json
import os
import re
import sys
import webbrowser
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urljoin, urlparse
from urllib.request import Request, urlopen

PORT = 8765
HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "mojedelo-oglasi.html")
MAX_BYTES = 4 * 1024 * 1024

# ---------------------------------------------------------------- razbiranje


class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
             "tr", "td", "th", "section", "article", "header", "footer", "main",
             "nav", "table", "dd", "dt", "hr"}
    SKIP = {"script", "style", "noscript", "template", "svg"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def text_to_lines(text):
    lines = [re.sub(r"[ \t ]+", " ", l).strip() for l in text.split("\n")]
    return [l for l in lines if l]


def html_to_lines(markup):
    p = _TextExtractor()
    try:
        p.feed(markup)
    except Exception:
        pass
    return text_to_lines("".join(p.out))


DATE_LINE = re.compile(r"^(\d{1,2})\. ?(\d{1,2})\. ?(20\d{2})$")
GENDER = re.compile(r"^[žmd](/[žmd]){1,2}$")


def _iso(d, m, y):
    try:
        return date(int(y), int(m), int(d))
    except ValueError:
        return None


def parse_header(lines):
    """Glava oglasa: naslov / datum objave / podjetje / kraj / ž/m/d"""
    for i, l in enumerate(lines):
        m = DATE_LINE.match(l)
        if m and i >= 1 and i + 2 < len(lines):
            if any(GENDER.match(x) for x in lines[i + 1:i + 5]):
                return {
                    "title": lines[i - 1],
                    "company": lines[i + 1],
                    "location": lines[i + 2],
                    "posted": _iso(m.group(1), m.group(2), m.group(3)),
                }
    return {}


def find_explicit_deadline(text):
    pats = [
        r"(?:rok za prijavo|rok prijave|prijave zbiramo|prijave sprejemamo)[^\d\n]{0,25}(\d{1,2})\. ?(\d{1,2})\. ?(20\d{2})",
        r"\b(?:najkasneje )?do\s+(\d{1,2})\. ?(\d{1,2})\. ?(20\d{2})",
    ]
    for p in pats:
        m = re.search(p, text, re.I)
        if m:
            d = _iso(m.group(1), m.group(2), m.group(3))
            if d:
                return d
    return None


def find_days_left(text):
    m = re.search(r"za prijavo imate na voljo še\s+(\d+)\s+(?:dni|dan|dneva|dnevi)", text, re.I)
    return int(m.group(1)) if m else None


def find_employment_type(text):
    t = text.lower()
    rules = [
        (r"\bnedoločen čas", "nedoločen čas"),
        (r"\bdoločen čas", "določen čas"),
        (r"polni delovni čas|polni čas|polnim delovnim časom", "polni delovni čas"),
        (r"krajši delovni čas|krajšim delovnim časom|part[- ]time", "krajši delovni čas"),
        (r"tri ?izmensk|v treh izmenah|tri izmene|3 izmene", "tri izmene"),
        (r"dvo ?izmensk|v dveh izmenah|dve izmeni|2 izmeni", "dve izmeni"),
    ]
    found = []
    for pat, label in rules:
        m = re.search(pat, t)
        if m:
            found.append((m.start(), label))
    if any(l == "tri izmene" for _, l in found):
        found = [x for x in found if x[1] != "dve izmeni"]
    found.sort()  # po vrstnem redu v oglasu
    return ", ".join(l for _, l in found)


REQ_HEAD = re.compile(
    r"^(od (vas|kandidat\w*|vas kot kandidat\w*) pričakujemo|pričakujemo|kaj pričakujemo|zahteve|"
    r"zahtevana (izobrazba|znanja)|vaše spretnosti|vaše kvalifikacije|pogoji|veseli bomo vaše prijave|"
    r"iščemo sodelavca, ki|iščemo)", re.I)
STOP_HEAD = re.compile(
    r"^(nudimo|ponujamo|kaj ponujamo|kaj nudimo|prednost|ugodnosti|prijava|pošlji|bodočemu sodelavcu|"
    r"delo poteka|vaše naloge|delovne naloge|zainteresirani|pridruži)", re.I)


def find_requirements(lines, limit=320):
    for i, l in enumerate(lines):
        head = l.rstrip(":").strip()
        if len(head) < 60 and REQ_HEAD.match(head):
            chunk = []
            for x in lines[i + 1:i + 8]:
                if x.endswith(":") or STOP_HEAD.match(x.rstrip(":")):
                    break
                chunk.append(x.rstrip(";,. "))
                if len("; ".join(chunk)) > limit:
                    break
            s = "; ".join(chunk)
            if len(s) > 25:
                return (s[:limit].rsplit(" ", 1)[0] + "…") if len(s) > limit else s
    return ""


DUTY_HEAD = re.compile(
    r"^(vaš(i)? izziv(i)?( bo(do)?)?|vaše delo( bo obsegalo)?|vaše naloge|delovne naloge|"
    r"glavne naloge( in odgovornosti)?|naloge( in odgovornosti)?|opis dela( in nalog)?|"
    r"opis delovnega mesta|obseg dela|tvoji dnevni izzivi( bodo)?|kaj boste delali|"
    r"vaše odgovornosti|delo obsega|delo bo obsegalo|vaše delo)", re.I)


def find_duties(lines, limit=300, fallback=True):
    """Opis delovnih nalog: vrstice pod naslovom »Vaše naloge« ipd.; rezerva: kratke vrstice opisa."""
    chunk = []
    for i, l in enumerate(lines):
        head = l.rstrip(":").strip()
        if len(head) < 60 and DUTY_HEAD.match(head):
            for x in lines[i + 1:i + 11]:
                if x.endswith(":") or REQ_HEAD.match(x.rstrip(":")) or STOP_HEAD.match(x.rstrip(":")):
                    break
                chunk.append(x.rstrip(";,. "))
            if chunk:
                break
    if not chunk and fallback:
        for x in lines:
            if x.endswith(":") or REQ_HEAD.match(x.rstrip(":")) or STOP_HEAD.match(x.rstrip(":")):
                if chunk:
                    break
                continue
            if 3 <= len(x) <= 160:
                chunk.append(x.rstrip(";,. "))
            if len(chunk) >= 6:
                break
    s = "; ".join(chunk)
    if len(s) < 15:
        return ""
    return (s[:limit].rsplit(" ", 1)[0] + "…") if len(s) > limit else s


def _flatten_ld(obj):
    if isinstance(obj, list):
        for x in obj:
            yield from _flatten_ld(x)
    elif isinstance(obj, dict):
        if "@graph" in obj:
            yield from _flatten_ld(obj["@graph"])
        yield obj


def parse_json_ld(markup):
    for m in re.finditer(r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
                         markup, re.S | re.I):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:
            continue
        for o in _flatten_ld(data):
            t = o.get("@type")
            if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
                return o
    return None


EMP_MAP = {"FULL_TIME": "polni delovni čas", "PART_TIME": "krajši delovni čas",
           "CONTRACTOR": "pogodbeno delo", "TEMPORARY": "določen čas", "INTERN": "praksa",
           "VOLUNTEER": "prostovoljno", "PER_DIEM": "po potrebi", "OTHER": ""}


def from_json_ld(o):
    res = {}
    if not o:
        return res
    if o.get("title"):
        res["title"] = html.unescape(str(o["title"])).strip()
    org = o.get("hiringOrganization")
    if isinstance(org, dict) and org.get("name"):
        res["company"] = html.unescape(str(org["name"])).strip()
    elif isinstance(org, str):
        res["company"] = org.strip()
    loc = o.get("jobLocation")
    if isinstance(loc, list) and loc:
        loc = loc[0]
    if isinstance(loc, dict):
        a = loc.get("address")
        if isinstance(a, dict):
            res["location"] = ", ".join(
                str(a[k]).strip() for k in ("addressLocality", "addressRegion") if a.get(k))
        elif isinstance(a, str):
            res["location"] = a.strip()
    et = o.get("employmentType")
    if et:
        ets = et if isinstance(et, list) else [et]
        res["type"] = ", ".join(x for x in (EMP_MAP.get(str(e).upper(), str(e)) for e in ets) if x)
    vt = o.get("validThrough")
    if vt:
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(vt))
        if m:
            res["deadline_exact"] = _iso(m.group(3), m.group(2), m.group(1))
    dp = o.get("datePosted")
    if dp:
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(dp))
        if m:
            res["posted"] = _iso(m.group(3), m.group(2), m.group(1))
    bs = o.get("baseSalary")
    if isinstance(bs, dict):
        v = bs.get("value")
        cur = bs.get("currency", "")
        if isinstance(v, dict):
            lo, hi, one = v.get("minValue"), v.get("maxValue"), v.get("value")
            if lo and hi:
                res["salary"] = f"{lo}–{hi} {cur}".strip()
            elif one:
                res["salary"] = f"{one} {cur}".strip()
    desc = o.get("description")
    if desc:
        res["desc_lines"] = html_to_lines(html.unescape(str(desc)))
    return res


def meta_title(markup):
    m = re.search(r"<meta[^>]+property=[\"']og:title[\"'][^>]+content=[\"']([^\"']+)", markup, re.I)
    if not m:
        m = re.search(r"<title[^>]*>(.*?)</title>", markup, re.S | re.I)
    if not m:
        return ""
    t = html.unescape(m.group(1)).strip()
    t = re.sub(r"\s*[|\-–]\s*(MojeDelo\.com|Moje delo)\s*$", "", t, flags=re.I)
    return t if t.lower() not in ("moje delo", "mojedelo.com") else ""


def fmt_sl(d):
    return f"{d.day}. {d.month}. {d.year}"


def extract(markup, today=None):
    """Iz HTML-ja ali besedila strani izlušči podatke oglasa."""
    today = today or date.today()
    ld = from_json_ld(parse_json_ld(markup))
    lines = html_to_lines(markup) if "<" in markup else text_to_lines(markup)
    head = parse_header(lines)
    body_text = "\n".join(lines)
    body_all = body_text + "\n" + "\n".join(ld.get("desc_lines", []))

    out = {"title": "", "company": "", "location": "", "type": "", "salary": "",
           "deadline": "", "dlApprox": False, "notes": ""}
    out["title"] = head.get("title") or ld.get("title") or meta_title(markup)
    out["company"] = head.get("company") or ld.get("company", "")
    out["location"] = head.get("location") or ld.get("location", "")
    out["type"] = ld.get("type") or find_employment_type(body_all)
    if ld.get("type") and find_employment_type(body_all):
        # besedilo je običajno natančnejše (npr. »nedoločen čas s poskusno dobo«)
        out["type"] = find_employment_type(body_all)
    out["salary"] = ld.get("salary", "")

    explicit = find_explicit_deadline(body_all)
    days = find_days_left(body_all)
    if explicit:
        out["deadline"] = explicit.isoformat()
    elif ld.get("deadline_exact"):
        out["deadline"] = ld["deadline_exact"].isoformat()
    elif days is not None:
        out["deadline"] = (today + timedelta(days=max(0, days - 1))).isoformat()
        out["dlApprox"] = True

    posted = head.get("posted") or ld.get("posted")
    req = find_requirements(lines) or find_requirements(ld.get("desc_lines", []))
    duties = find_duties(lines, fallback=False) or find_duties(ld.get("desc_lines", []), fallback=False)
    parts = []
    if posted:
        parts.append(f"Objavljeno {fmt_sl(posted)}.")
    if duties:
        parts.append("Naloge: " + duties + ("" if duties.endswith("…") else "."))
    if req:
        parts.append("Zahteve: " + req)
    out["notes"] = " ".join(parts)

    warnings = []
    if re.search(r"oglas je potekel|ni več aktualen|oglas ni več", body_text, re.I):
        warnings.append("Oglas je morda potekel.")
    if not out["title"] and not out["company"]:
        warnings.append("V HTML-ju strani ni vsebine oglasa (stran jo morda naloži z JavaScriptom).")
    elif not out["deadline"]:
        warnings.append("Roka prijave ni bilo mogoče prebrati.")
    out["warnings"] = warnings
    out["ok"] = bool(out["title"] or out["company"])
    return out


# ---------------------------------------------------------------- strežnik


def host_allowed(url):
    try:
        u = urlparse(url)
    except Exception:
        return False
    h = (u.hostname or "").lower()
    return u.scheme in ("http", "https") and (
        h == "mojedelo.com" or h.endswith(".mojedelo.com") or h == "ess.gov.si" or h.endswith(".ess.gov.si")
        or h == "optius.com" or h.endswith(".optius.com"))


UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
SITE = "https://www.mojedelo.com/"
API_BASE = "https://api.mojedelo.com"
# Javni podatki, ki jih stran sama pošilja vmesniku (izvirajo iz njene javne nastavitve).
# Če se kdaj spremenijo, jih pomočnik poskusi sam prebrati s portala.
DEFAULT_CFG = {
    "tenantid": "5947a585-ad25-47dc-bff3-f08620d1ce17",
    "channelid": "8805c1b8-a0a9-4f57-ad42-329af3c92a61",
    "languageid": "db3c58e6-a083-4f72-b30b-39f2127bb18d",
}
_cfg = None
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def http_get(url, headers=None, timeout=25):
    """Vrne (status, besedilo). Napake HTTP vrne kot status, omrežne napake sprožijo izjemo."""
    h = {"User-Agent": UA, "Accept-Language": "sl-SI,sl;q=0.9,en;q=0.5"}
    h.update(headers or {})
    try:
        with urlopen(Request(url, headers=h), timeout=timeout) as r:
            raw = r.read(MAX_BYTES)
            cs = r.headers.get_content_charset() or "utf-8"
            return r.status, raw.decode(cs, errors="replace")
    except HTTPError as e:
        try:
            body = e.read(MAX_BYTES).decode("utf-8", "replace")
        except Exception:
            body = ""
        return e.code, body


def api_config(force=False):
    global _cfg
    if _cfg and not force:
        return _cfg
    cfg = dict(DEFAULT_CFG)
    if force:
        try:
            st, shell = http_get(SITE)
            m = re.search(r'<script[^>]+src="([^"]*jb\.globals\.js[^"]*)"', shell)
            if st == 200 and m:
                st2, js = http_get(urljoin(SITE, html.unescape(m.group(1))))
                if st2 == 200:
                    for key, pat in (("tenantid", r'tenantId\\?"\s*:\s*\\?"([0-9a-f-]{36})'),
                                     ("channelid", r'jbChannelId\\?"\s*:\s*\\?"([0-9a-f-]{36})'),
                                     ("languageid", r'defaultLanguageId\\?"\s*:\s*\\?"([0-9a-f-]{36})')):
                        mm = re.search(pat, js)
                        if mm:
                            cfg[key] = mm.group(1)
        except Exception:
            pass
    _cfg = cfg
    return cfg


def ad_id_from_url(url):
    found = UUID_RE.findall(urlparse(url).path)
    return found[-1].lower() if found else None


def fetch_ad_api(ad_id):
    last = (0, "")
    for attempt in (0, 1):
        cfg = api_config(force=(attempt == 1))
        hdrs = {"Accept": "application/json", "Origin": SITE.rstrip("/"), "Referer": SITE}
        hdrs.update(cfg)
        last = http_get(f"{API_BASE}/job-ads/{ad_id}", hdrs)
        if last[0] in (400, 401, 403) and attempt == 0 and "header" in last[1].lower():
            continue  # glave so se morda spremenile: osveži nastavitev in poskusi znova
        break
    return last


def _last_sunday(year, month):
    d = date(year, month, 31)  # marec in oktober imata 31 dni
    return d - timedelta(days=(d.weekday() + 1) % 7)


def to_local_date(iso):
    """ISO čas v UTC -> datum po slovenskem času (CET/CEST)."""
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(iso))
        return _iso(m.group(3), m.group(2), m.group(1)) if m else None
    start = datetime.combine(_last_sunday(dt.year, 3), datetime.min.time()) + timedelta(hours=1)
    end = datetime.combine(_last_sunday(dt.year, 10), datetime.min.time()) + timedelta(hours=1)
    return (dt + timedelta(hours=2 if start <= dt < end else 1)).date()


def _tr(x):
    if isinstance(x, dict):
        return str(x.get("translation") or x.get("name") or "").strip()
    return str(x).strip() if x else ""


def from_api(d, today=None):
    today = today or date.today()
    out = {"title": str(d.get("title") or "").strip(), "company": "", "location": "", "type": "",
           "salary": "", "deadline": "", "dlApprox": False, "notes": "", "warnings": []}

    comp = d.get("company")
    name = comp.get("name") if isinstance(comp, dict) else ""
    if not name:
        anon = d.get("anonymizedCompany")
        name = anon.get("name") if isinstance(anon, dict) else (anon if isinstance(anon, str) else "")
    out["company"] = str(name or "").strip()

    loc = _tr(d.get("town"))
    if not loc:
        loc = ", ".join(x for x in (_tr(r) for r in (d.get("regions") or [])) if x)
    if not loc and isinstance(d.get("jobLocationInput"), str):
        loc = d["jobLocationInput"].strip()
    out["location"] = loc

    expect_html = d.get("weExpect") or ""
    desc_html = d.get("jobDescription") or ""
    offer_html = d.get("weOffer") or ""

    parts = []
    for e in d.get("employmentTypes") or []:
        t = _tr(e).lower()
        parts.append("nedoločen čas" if "nedoločen" in t else "določen čas" if "določen" in t else t)
    wt = _tr(d.get("workTime")).lower()
    if wt:
        parts.append(wt)
    for m in d.get("workModes") or []:
        t = _tr(m).lower()
        if re.search(r"doma|hibrid|daljav|remote", t):
            parts.append(t)
    prob = d.get("probationPeriod")
    if isinstance(prob, (int, float)) and prob > 0:
        parts.append(f"poskusna doba {int(prob)} mes.")
    shifts = find_employment_type(" ".join(
        "\n".join(html_to_lines(h)) for h in (desc_html, expect_html, offer_html) if h))
    parts += [s for s in shifts.split(", ") if "izmen" in s]
    seen, uniq = set(), []
    for p in parts:
        if p and p not in seen:
            seen.add(p)
            uniq.append(p)
    out["type"] = ", ".join(uniq)

    sal = d.get("salary")
    if isinstance(sal, (int, float, str)) and sal:
        out["salary"] = str(sal)
    elif isinstance(sal, dict):
        lo = next((sal[k] for k in ("min", "minimum", "from", "salaryFrom") if sal.get(k)), None)
        hi = next((sal[k] for k in ("max", "maximum", "to", "salaryTo") if sal.get(k)), None)
        cur = sal.get("currency") or ""
        if lo and hi:
            out["salary"] = f"{lo}–{hi} {cur}".strip()
        elif lo or hi:
            out["salary"] = f"{lo or hi} {cur}".strip()

    end = to_local_date(d["endDate"]) if d.get("endDate") else None
    if end:
        out["deadline"] = end.isoformat()
        if end < today:
            out["warnings"].append("Rok prijave je potekel.")
    else:
        out["warnings"].append("Roka prijave ni bilo mogoče prebrati.")
    if d.get("status") and d["status"] != "published":
        out["warnings"].append(f"Oglas ni več objavljen (stanje: {d['status']}).")

    # opombe: datum objave, izkušnje, zahteve
    req_lines = [l for l in html_to_lines(expect_html)
                 if not (l.endswith(":") or REQ_HEAD.match(l.rstrip(":")) and len(l) < 60)]
    req = "; ".join(x.rstrip(";,. ") for x in req_lines)
    if len(req) < 25:
        req = find_requirements(html_to_lines(desc_html))
    if len(req) > 320:
        req = req[:320].rsplit(" ", 1)[0] + "…"
    notes = []
    posted = to_local_date(d["startDate"]) if d.get("startDate") else None
    if posted:
        notes.append(f"Objavljeno {fmt_sl(posted)}.")
    exp = _tr(d.get("totalWorkExperience"))
    if exp:
        notes.append(f"Izkušnje: {exp}.")
    duties = find_duties(html_to_lines(desc_html))
    if duties:
        notes.append("Naloge: " + duties + ("" if duties.endswith("…") else "."))
    if req:
        notes.append("Zahteve: " + req)
    out["notes"] = " ".join(notes)
    out["ok"] = bool(out["title"] or out["company"])
    return out


# ------------------------------------------------------------------ ZRSZ (ess.gov.si)

ZRSZ_SITE = "https://www.ess.gov.si/iskalci-zaposlitve/iskanje-zaposlitve/iskanje-dela/"
ZRSZ_API = ("https://apigateway-prod-www-prod.apps.ess.gov.si/iskalnik-po-pdm/v1/"
            "delovno-mesto/podrobnosti-prosto-delovno-mesto")
ZRSZ_KEY = "9b7dcbe8ec1855d14f0b2ec4f6335a91"  # javni ključ, ki ga stran ZRSZ sama pošilja
_zrsz_key = None


def is_zrsz(url):
    h = (urlparse(url).hostname or "").lower()
    return h == "ess.gov.si" or h.endswith(".ess.gov.si")


def zrsz_id_from_url(url):
    m = re.search(r"(?:pdm/|idp=|idDelovnoMesto=)(\d{4,})", url)
    return m.group(1) if m else None


def zrsz_key(force=False):
    global _zrsz_key
    if _zrsz_key and not force:
        return _zrsz_key
    key = ZRSZ_KEY
    if force:
        try:
            st, page = http_get(ZRSZ_SITE)
            texts = [page]
            for src in re.findall(r'<script[^>]+src="([^"]+\.js[^"]*)"', page)[:8]:
                s2, js = http_get(urljoin(ZRSZ_SITE, html.unescape(src)))
                if s2 == 200:
                    texts.append(js)
            for t in texts:
                m = re.search(r"user_key\W{1,6}([0-9a-f]{32})", t)
                if m:
                    key = m.group(1)
                    break
        except Exception:
            pass
    _zrsz_key = key
    return key


def _sentence(s):
    """VELIKE ČRKE -> Navadno besedilo (samo če je celotno besedilo z velikimi črkami)."""
    s = (s or "").strip()
    if s and any(c.isalpha() for c in s) and s == s.upper():
        s = s.lower()
    return s[:1].upper() + s[1:] if s else s


def _clean_zrsz_company(name):
    name = (name or "").strip()
    parts = [p.strip() for p in name.split(",")]
    # ZRSZ pripne sedež: »PODJETJE d.o.o., KRAJ« -> odstrani KRAJ
    if len(parts) > 1 and parts[-1] and parts[-1] == parts[-1].upper() \
            and not re.search(r"D\.?\s?O\.?\s?O|D\.?\s?D\.?|S\.?\s?P\.?", parts[-1]) \
            and re.fullmatch(r"[A-ZČŠŽĆĐ0-9 \-./]+", parts[-1]):
        parts = parts[:-1]
    return ", ".join(parts)


def _parse_sl_date(s):
    m = re.search(r"(\d{1,2})\. ?(\d{1,2})\. ?(20\d{2})", str(s or ""))
    return _iso(m.group(1), m.group(2), m.group(3)) if m else None


def from_zrsz(d, today=None):
    today = today or date.today()
    out = {"title": "", "company": "", "location": "", "type": "", "salary": "",
           "deadline": "", "dlApprox": False, "notes": "", "warnings": []}
    t = str(d.get("nazivDelovnegaMesta") or "").strip()
    suffix = re.search(r"\s*(?:[-–]\s*|\(\s*)M\s*/\s*Ž(\s*/\s*D)?\s*\)?\s*$", t, re.I)
    if suffix:
        t = t[:suffix.start()].rstrip()
    # pretvorba iz VELIKIH ČRK mora biti pred pripono, sicer »(m/ž)« pokvari preverjanje
    out["title"] = _sentence(t) + (" (m/ž)" if suffix else "")
    out["company"] = _clean_zrsz_company(d.get("delodajalec"))
    kraj = str(d.get("objavaKraj") or d.get("upravnaEnota") or "").strip()
    out["location"] = kraj.title() if kraj == kraj.upper() else kraj

    parts = []
    for k in ("trajanjeZaposlitve", "delovniCas", "urnikDela"):
        v = str(d.get(k) or "").strip()
        if v:
            parts.append(v[:1].lower() + v[1:] if v[:2] != v[:2].upper() else v)
    out["type"] = ", ".join(parts)
    out["salary"] = str(d.get("okvirnaPlaca") or "").strip()

    end = _parse_sl_date(d.get("prijavaDo"))
    if end:
        out["deadline"] = end.isoformat()
        if end < today:
            out["warnings"].append("Rok prijave je potekel.")
    else:
        out["warnings"].append("Roka prijave ni bilo mogoče prebrati.")

    notes = []
    posted = _parse_sl_date(d.get("datumObjave"))
    if posted:
        notes.append(f"Objavljeno {fmt_sl(posted)}.")
    duties = _sentence(str(d.get("opisDelInNalog") or "").strip().rstrip(",;. "))
    if duties:
        if len(duties) > 300:
            duties = duties[:300].rsplit(" ", 1)[0] + "…"
        notes.append("Naloge: " + duties + ("" if duties.endswith("…") else "."))
    req = []
    labels = (("izobrazba", "izobrazba"), ("alternativnaIzobrazba", "ali"), ("delovneIzkusnje", "izkušnje"),
              ("znanjeJezikov", "jeziki"), ("racunalniskaZnanja", "računalniška znanja"),
              ("vozniskoDovoljenje", "vozniško dovoljenje"), ("drugiPogoji", ""))
    for key, label in labels:
        v = str(d.get(key) or "").strip().rstrip(",;. ")
        if v:
            v = v.lower() if v == v.upper() else v
            req.append((label + " " if label else "") + v)
    if req:
        r = "; ".join(req)
        if len(r) > 300:
            r = r[:300].rsplit(" ", 1)[0] + "…"
        notes.append("Zahteve: " + r + ("" if r.endswith("…") else "."))
    apply = []
    how = str(d.get("nacinPrijaveKandidata") or "").strip().rstrip(",;. ")
    if how:
        apply.append(how)
    k = d.get("kontaktZaKandidata") or {}
    contact = ", ".join(x for x in (str(k.get("kontaktnaOseba") or "").strip().title(),
                                    str(k.get("telefon") or "").strip(),
                                    str(k.get("eNaslov") or "").strip()) if x)
    if contact:
        apply.append("kontakt: " + contact)
    if apply:
        notes.append("Prijava: " + "; ".join(apply) + ".")
    out["notes"] = " ".join(notes)
    out["ok"] = bool(out["title"])
    return out


def get_zrsz(url):
    pid = zrsz_id_from_url(url)
    if not pid:
        return {"ok": False, "error": "V povezavi ni številke oglasa ZRSZ (iščem …/pdm/<št.> ali ?idp=<št.>). "
                                      "Kopiraj povezavo, ko je oglas odprt."}
    status, body = 0, ""
    for attempt in (0, 1):
        key = zrsz_key(force=(attempt == 1))
        status, body = http_get(
            f"{ZRSZ_API}?idDelovnoMesto={pid}&user_key={key}",
            {"Accept": "application/json", "Origin": "https://www.ess.gov.si", "Referer": ZRSZ_SITE})
        if status in (401, 403) and attempt == 0:
            continue  # ključ se je morda zamenjal: osveži in poskusi znova
        break
    if status != 200:
        if status in (204, 404):
            return {"ok": False, "error": "Oglasa ni več na ZRSZ."}
        return {"ok": False, "error": f"Vmesnik ZRSZ je vrnil napako {status}."}
    try:
        data = json.loads(body)
    except ValueError:
        return {"ok": False, "error": "Nepričakovan odgovor ZRSZ."}
    if not isinstance(data, dict) or not data.get("nazivDelovnegaMesta"):
        return {"ok": False, "error": "Oglasa ni več na ZRSZ."}
    return from_zrsz(data)


# ------------------------------------------------------------------ Optius
# Optius vrača oglas kot običajen HTML (brez API-ja): glavni podatki so v JSON-LD (JobPosting),
# naloge in zahteve pa v razdelkih »Opis delovnega mesta« in »Pričakujemo«.

def is_optius(url):
    h = (urlparse(url).hostname or "").lower()
    return h == "optius.com" or h.endswith(".optius.com")


def _ld_job_posting(markup):
    """Kot parse_json_ld, a prenese presledke in nove vrstice znotraj nizov (Optius jih ne ubeži)."""
    for m in re.finditer(r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
                         markup, re.S | re.I):
        raw = m.group(1).strip()
        if "JobPosting" not in raw:
            continue
        try:
            data = json.loads(raw, strict=False)
        except Exception:
            continue
        for o in _flatten_ld(data):
            t = o.get("@type")
            if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
                return o
    return None


def _strip_tags(frag):
    return " ".join(html_to_lines(frag))


def _optius_sections(markup):
    """{naslov razdelka: [alineje]} iz blokov <h3 class="section-title">…</h3> + <div class="col-right">…</div>."""
    out = {}
    for m in re.finditer(r'<h3[^>]*class="[^"]*section-title[^"]*"[^>]*>(.*?)</h3>\s*</div>\s*'
                         r'<div[^>]*class="[^"]*col-right[^"]*"[^>]*>(.*?)</div>\s*</div>',
                         markup, re.S | re.I):
        head = _strip_tags(m.group(1)).strip()
        items = [i for i in html_to_lines(m.group(2)) if i]
        if head and items:
            out.setdefault(head.lower(), items)
    return out


def _join_items(items, limit=300):
    # vmesni podnaslovi (»Opis del in nalog:«, »Pogoji za zasedbo delovnega mesta:«) niso del vsebine
    items = [x for x in items if x.strip() and not (x.strip().endswith(":") and len(x) < 60)]
    txt = "; ".join(x.strip().rstrip(",;. ") for x in items)
    if len(txt) > limit:
        txt = txt[:limit].rsplit(" ", 1)[0] + "…"
    return txt


def from_optius(markup, today=None):
    today = today or date.today()
    out = {"title": "", "company": "", "location": "", "type": "", "salary": "",
           "deadline": "", "dlApprox": False, "notes": "", "warnings": []}
    ld = _ld_job_posting(markup) or {}
    ldv = from_json_ld(ld) if ld else {}

    h1 = re.search(r"<h1[^>]*>(.*?)</h1>", markup, re.S | re.I)
    out["title"] = ldv.get("title") or (_strip_tags(h1.group(1)) if h1 else "")
    out["company"] = ldv.get("company", "")
    out["location"] = ldv.get("location", "")
    if not out["location"]:
        m = re.search(r"Kraj dela\s*</strong>\s*<div[^>]*>(.*?)</div>", markup, re.S | re.I)
        if m:
            out["location"] = _strip_tags(m.group(1))

    parts = []
    for v in (ld.get("employmentType"), ld.get("workHours")):
        if isinstance(v, list):
            v = ", ".join(map(str, v))
        v = html.unescape(str(v or "")).strip()
        if v and v.lower() not in [p.lower() for p in parts]:
            parts.append(EMP_MAP.get(v.upper(), v))
    out["type"] = ", ".join(parts)
    out["salary"] = ldv.get("salary", "")

    posted = ldv.get("posted")
    end = ldv.get("deadline_exact")
    if not end:
        m = re.search(r"Prijave do:?\s*</strong>.*?(\d{1,2})\. ?(\d{1,2})\. ?(20\d{2})", markup, re.S | re.I)
        if m:
            end = _iso(m.group(1), m.group(2), m.group(3))
    if not posted:
        m = re.search(r"Datum objave:?\s*</strong>.*?(\d{1,2})\. ?(\d{1,2})\. ?(20\d{2})", markup, re.S | re.I)
        if m:
            posted = _iso(m.group(1), m.group(2), m.group(3))
    if end:
        out["deadline"] = end.isoformat()
        if end < today:
            out["warnings"].append("Rok prijave je potekel.")
    else:
        out["warnings"].append("Roka prijave ni bilo mogoče prebrati.")

    sec = _optius_sections(markup)

    def pick(*names):
        for k, v in sec.items():
            if any(n in k for n in names):
                return v
        return []

    notes = []
    if posted:
        notes.append(f"Objavljeno {fmt_sl(posted)}.")
    exp = html.unescape(str(ld.get("experienceRequirements") or "")).strip()
    if exp:
        notes.append(f"Izkušnje: {exp}.")
    # JSON-LD »responsibilities« vsebuje HTML entitete (&scaron; …), zato gre skozi html_to_lines
    duties = pick("opis delovnega mesta", "opis dela", "naloge") or html_to_lines(str(ld.get("responsibilities") or ""))
    if duties:
        d = _join_items(duties)
        notes.append("Naloge: " + _sentence(d) + ("" if d.endswith("…") else "."))
    req = pick("pričakujemo", "zahteve", "pogoji")
    edu = html.unescape(str(ld.get("educationRequirements") or "")).strip()
    reqs = ([f"izobrazba {edu}"] if edu else []) + req
    if reqs:
        r = _join_items(reqs)
        notes.append("Zahteve: " + r + ("" if r.endswith("…") else "."))
    ap = re.search(r"Prijava na delovno mesto</h3>(.*?)(?:<div class=\"ilu-benefits|</section>|$)", markup, re.S | re.I)
    if ap:
        mails = list(dict.fromkeys(re.findall(r'mailto:([^"\'>\s?]+)', ap.group(1))))
        if mails:
            notes.append("Prijava: " + ", ".join(mails) + ".")
    out["notes"] = " ".join(notes)
    out["ok"] = bool(out["title"]) and bool(ld or sec)
    if not out["ok"]:
        out["error"] = "Oglasa ni več na Optiusu (ali povezava ni oglas)."
    return out


def get_optius(url):
    status, markup = http_get(url)
    if status in (404, 410):
        return {"ok": False, "error": "Oglasa ni več na Optiusu."}
    if status != 200:
        return {"ok": False, "error": f"Optius je vrnil napako {status}."}
    return from_optius(markup)


def get_ad(url):
    """Prebere oglas: najprej prek podatkovnega vmesnika portala, rezerva je HTML strani."""
    if is_zrsz(url):
        try:
            return get_zrsz(url)
        except (URLError, TimeoutError, OSError) as e:
            return {"ok": False, "error": f"Povezava ni uspela: {e}"}
    if is_optius(url):
        try:
            return get_optius(url)
        except (URLError, TimeoutError, OSError) as e:
            return {"ok": False, "error": f"Povezava ni uspela: {e}"}
    ad_id = ad_id_from_url(url)
    api_err = ""
    if ad_id:
        try:
            status, body = fetch_ad_api(ad_id)
        except (URLError, TimeoutError, OSError) as e:
            return {"ok": False, "error": f"Povezava ni uspela: {e}"}
        if status == 200:
            try:
                data = json.loads(body).get("data")
                if isinstance(data, dict):
                    return from_api(data)
            except ValueError:
                pass
            api_err = "Nepričakovan odgovor portala."
        elif status in (404, 410):
            return {"ok": False, "error": "Oglasa ni več (404)."}
        else:
            api_err = f"Vmesnik portala je vrnil napako {status}."
    try:
        status, markup = http_get(url)
    except (URLError, TimeoutError, OSError) as e:
        return {"ok": False, "error": f"Povezava ni uspela: {e}"}
    if status != 200:
        return {"ok": False, "error": api_err or f"Portal je vrnil napako {status}."}
    res = extract(markup)
    if not res["ok"]:
        res["error"] = (api_err + " " if api_err else "") + (res["warnings"][0] if res["warnings"] else "")
    return res


LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]"}


def host_header_ok(value):
    """Zaščita pred DNS rebindingom: tuja stran, ki se razreši na 127.0.0.1, pošlje svoj Host."""
    host = (value or "").strip().lower()
    if host.startswith("["):
        host = host[:host.find("]") + 1]
    else:
        host = host.split(":", 1)[0]
    return host in LOCAL_HOSTS


class Handler(BaseHTTPRequestHandler):
    server_version = "MojeDeloPomocnik/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("  " + (fmt % args) + "\n")

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def do_GET(self):
        if not host_header_ok(self.headers.get("Host")):
            return self._send(403, "Forbidden", "text/plain; charset=utf-8")
        u = urlparse(self.path)
        if u.path in ("/", "/index.html", "/mojedelo-oglasi.html"):
            try:
                with open(PAGE, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                self._send(500, "Datoteke mojedelo-oglasi.html ni poleg programa.", "text/plain; charset=utf-8")
        elif u.path == "/api/ping":
            self._json(200, {"ok": True})
        elif u.path == "/api/fetch":
            url = (parse_qs(u.query).get("url") or [""])[0].strip()
            if not host_allowed(url):
                return self._json(400, {"ok": False, "error": "Podprti so mojedelo.com, ess.gov.si (ZRSZ) in optius.com."})
            try:
                res = get_ad(url)
            except Exception as e:  # nikoli ne podri strežnika zaradi enega oglasa
                res = {"ok": False, "error": f"Napaka pomočnika: {e}"}
            print(f"  [{'OK ' if res.get('ok') else 'NAPAKA'}] {url}")
            if res.get("ok"):
                for k in ("title", "company", "location", "type", "deadline"):
                    print(f"      {k:9}: {res.get(k) or '— (ni najdeno)'}")
                for w in res.get("warnings", []):
                    print("      ! " + w)
            else:
                print("      " + (res.get("error") or "Podatkov ni bilo mogoče prebrati."))
            self._json(200, res)
        else:
            self._send(404, "Not found", "text/plain; charset=utf-8")


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Lokalni pomočnik za mojedelo-oglasi.html")
    ap.add_argument("--port", type=int, default=PORT, help=f"začetna vrata (privzeto {PORT}, nato do +9)")
    ap.add_argument("--no-browser", action="store_true", help="ne odpri brskalnika ob zagonu")
    args = ap.parse_args(argv)
    if not os.path.exists(PAGE):
        print("Napaka: poleg tega programa mora biti datoteka mojedelo-oglasi.html")
        sys.exit(1)
    server = None
    port = args.port
    for p in range(args.port, args.port + 10):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            port = p
            break
        except OSError:
            continue
    if not server:
        print(f"Napaka: ni prostih vrat {args.port}–{args.port + 9}.")
        sys.exit(1)
    url = f"http://localhost:{port}/"
    print(f"Pomočnik teče: {url}")
    print("Za izhod pritisni Ctrl+C. To okno naj ostane odprto, dokler uporabljaš stran.\n")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nKonec.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
