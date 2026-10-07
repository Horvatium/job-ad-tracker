import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

import mojedelo_baza as baza
import mojedelo_pomocnik as m


@pytest.mark.parametrize("url, ok", [
    ("https://www.mojedelo.com/oglas/x/1", True),
    ("https://mojedelo.com/oglas/x/1", True),
    ("https://www.ess.gov.si/iskalci-zaposlitve/", True),
    ("https://www.optius.com/iskalci/", True),
    ("https://evil-mojedelo.com/", False),
    ("https://mojedelo.com.evil.com/", False),
    ("ftp://www.mojedelo.com/", False),
    ("file:///C:/Windows/win.ini", False),
    ("http://127.0.0.1:8765/", False),
])
def test_host_allowed(url, ok):
    assert m.host_allowed(url) is ok


@pytest.mark.parametrize("host, ok", [
    ("localhost:8765", True),
    ("127.0.0.1:8765", True),
    ("[::1]:8765", True),
    ("LOCALHOST", True),
    ("evil.example:8765", False),
    ("localhost.evil.example", False),
    ("", False),
    (None, False),
])
def test_host_header_ok(host, ok):
    assert m.host_header_ok(host) is ok


def start(srv):
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1]


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "get_ad", lambda url: {"ok": True, "title": "Testni oglas", "warnings": []})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), m.Handler)
    srv.store = baza.Store(str(tmp_path / "oglasi.db"))
    yield start(srv)
    srv.shutdown()
    srv.server_close()
    srv.store.close()


@pytest.fixture
def server_without_db():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), m.Handler)
    yield start(srv)
    srv.shutdown()
    srv.server_close()


def request(port, method, path, body=None, host=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.putrequest(method, path, skip_host=True)
    c.putheader("Host", host or f"localhost:{port}")
    data = body.encode("utf-8") if isinstance(body, str) else body
    for k, v in (headers or {}).items():
        c.putheader(k, v)
    if data is not None:
        c.putheader("Content-Length", str(len(data)))
    c.endheaders(data)
    r = c.getresponse()
    out = r.read()
    c.close()
    return r.status, out


def get(port, path, host=None):
    return request(port, "GET", path, host=host)


def post_json(port, obj, headers=None):
    h = {"Content-Type": "application/json", "Origin": f"http://localhost:{port}"}
    h.update(headers or {})
    status, body = request(port, "POST", "/api/jobs/batch", json.dumps(obj), headers=h)
    return status, json.loads(body)


JOB = {"id": "a1", "url": "https://www.mojedelo.com/oglas/x/1", "title": "Skladiščnik", "status": "new"}


def test_ping(server, server_without_db):
    assert json.loads(get(server, "/api/ping")[1]) == {"ok": True, "storage": "sqlite", "check": False}
    assert json.loads(get(server_without_db, "/api/ping")[1]) == {"ok": True, "storage": "none", "check": False}


def test_check_endpoint_disabled(server):
    assert get(server, "/api/check")[0] == 404


def test_check_endpoint(tmp_path):
    class FakeChecker:
        def check_in_background(self):
            return True

        def status(self):
            return {"running": True, "done": 0, "total": 3, "lastCheck": None}

    srv = ThreadingHTTPServer(("127.0.0.1", 0), m.Handler)
    srv.checker = FakeChecker()
    port = start(srv)
    try:
        status, body = request(port, "POST", "/api/check", "{}", headers={"Content-Type": "application/json"})
        assert status == 200 and json.loads(body)["started"] is True
        assert json.loads(get(port, "/api/check")[1])["total"] == 3
        # tudi zagon preverjanja je zaščiten pred tujimi stranmi
        assert request(port, "POST", "/api/check", "{}", headers={"Content-Type": "text/plain"})[0] == 415
    finally:
        srv.shutdown()
        srv.server_close()


def test_jobs_roundtrip(server):
    status, body = get(server, "/api/jobs")
    assert status == 200 and json.loads(body) == {"ok": True, "initialized": False, "jobs": []}
    status, res = post_json(server, {"upsert": [JOB], "delete": [], "init": True})
    assert status == 200 and res["jobs"][0]["title"] == "Skladiščnik" and res["jobs"][0]["statusAt"]
    data = json.loads(get(server, "/api/jobs")[1])
    assert data["initialized"] and [j["id"] for j in data["jobs"]] == ["a1"]
    post_json(server, {"upsert": [], "delete": ["a1"]})
    assert json.loads(get(server, "/api/jobs")[1])["jobs"] == []


def test_batch_rejects_invalid_job(server):
    status, res = post_json(server, {"upsert": [{"id": "a b", "url": "x"}]})
    assert status == 400 and "id" in res["error"]


def test_batch_requires_json_content_type(server):
    # obrazec ali fetch z »text/plain« s tuje strani ne sproži CORS preverjanja, zato ga zavrnemo
    status, _ = request(server, "POST", "/api/jobs/batch", json.dumps({"delete": ["a1"]}),
                        headers={"Content-Type": "text/plain"})
    assert status == 415


def test_batch_rejects_foreign_origin(server):
    status, res = post_json(server, {"delete": ["a1"]}, headers={"Origin": "https://evil.example"})
    assert status == 403


def test_batch_rejects_empty_or_huge_body(server):
    h = {"Content-Type": "application/json"}
    assert request(server, "POST", "/api/jobs/batch", "", headers=h)[0] == 413
    assert request(server, "POST", "/api/jobs/batch", "x", headers=dict(h, **{"Content-Length": str(10**9)}))[0] == 413


def test_batch_bad_json(server):
    assert request(server, "POST", "/api/jobs/batch", "{nope", headers={"Content-Type": "application/json"})[0] == 400


def test_jobs_without_db(server_without_db):
    assert get(server_without_db, "/api/jobs")[0] == 404


def test_serves_page(server):
    status, body = get(server, "/")
    assert status == 200 and b"<title>Moja delovna mesta</title>" in body


def test_rejects_foreign_host_header(server):
    # DNS rebinding: tuja domena, ki kaže na 127.0.0.1
    assert get(server, "/api/ping", host="evil.example")[0] == 403


def test_fetch_rejects_other_hosts(server):
    status, body = get(server, "/api/fetch?url=https%3A%2F%2Fevil.example%2F")
    assert status == 400 and not json.loads(body)["ok"]


def test_fetch_ok(server):
    status, body = get(server, "/api/fetch?url=https%3A%2F%2Fwww.mojedelo.com%2Foglas%2Fx%2F1")
    assert status == 200 and json.loads(body)["title"] == "Testni oglas"
