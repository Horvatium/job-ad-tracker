import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

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


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(m, "get_ad", lambda url: {"ok": True, "title": "Testni oglas", "warnings": []})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), m.Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def get(port, path, host=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.putrequest("GET", path, skip_host=True)
    c.putheader("Host", host or f"localhost:{port}")
    c.endheaders()
    r = c.getresponse()
    body = r.read()
    c.close()
    return r.status, body


def test_ping(server):
    status, body = get(server, "/api/ping")
    assert status == 200 and json.loads(body) == {"ok": True}


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
