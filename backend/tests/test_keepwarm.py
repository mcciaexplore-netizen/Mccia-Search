"""The keep-warm pings: what they call, that they never follow a redirect, and that a failure is reported once, not every four minutes.

No database and no outside network: the only server is a throwaway one on this machine.
"""

import http.server
import threading
import time

import pytest

import keepwarm
from keepwarm import KeepWarm, fetch_status, parse_urls


class _Server:
    """A local web server that records what was asked of it. GET /redirect answers 303 to /landing; /gone answers 404."""

    def __init__(self):
        self.hits: list[str] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - the name http.server looks for
                outer.hits.append(self.path)
                if self.path == "/redirect":
                    self.send_response(303)
                    self.send_header("Location", "/landing")
                elif self.path == "/gone":
                    self.send_response(404)
                elif self.path == "/broken":
                    self.send_response(503)
                else:
                    self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):  # keep the test output quiet
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    s = _Server()
    yield s
    s.close()


def test_the_setting_is_read_as_a_list_of_https_addresses():
    assert parse_urls("https://a.example/x, https://b.example/y") == ["https://a.example/x", "https://b.example/y"]


@pytest.mark.parametrize("word", ["off", "OFF", " none ", "false", "0", "no"])
def test_off_turns_it_off(word):
    assert parse_urls(word) == []


def test_only_https_addresses_are_pinged_plus_http_to_this_machine(capsys):
    got = parse_urls("http://evil.example/x, ftp://x.example/, https://ok.example/, http://localhost:3000/p, javascript:alert(1), https://, ")
    assert got == ["https://ok.example/", "http://localhost:3000/p"]
    assert "ignoring" in capsys.readouterr().out


def test_the_default_address_is_the_sign_in_function_of_the_live_web_app():
    assert keepwarm.KEEP_WARM_URLS == "https://mccia-search.vercel.app/api/sso/callback"
    assert keepwarm.KEEP_WARM_INTERVAL_SECONDS == 240


def test_a_redirect_is_an_answer_and_is_not_followed(server):
    assert fetch_status(f"{server.url}/redirect") == 303
    assert server.hits == ["/redirect"]  # /landing (and anything behind it, like the hub) was never asked


def test_an_error_status_is_an_answer_too(server):
    assert fetch_status(f"{server.url}/gone") == 404
    assert fetch_status(f"{server.url}/broken") == 503


def test_no_answer_at_all_raises(server):
    server.close()
    with pytest.raises(OSError):
        fetch_status(f"{server.url}/x")


def test_each_address_is_asked_once_per_round_and_a_healthy_ping_says_nothing(capsys):
    asked = []
    pinger = KeepWarm(["https://a.example/", "https://b.example/"], fetch=lambda url: asked.append(url) or 303)
    pinger.ping_once()
    pinger.ping_once()
    assert asked == ["https://a.example/", "https://b.example/"] * 2
    assert capsys.readouterr().out == ""


def test_a_failure_is_reported_once_and_so_is_the_recovery(capsys):
    answers = iter([200, 503, 503, 502, 200, 200])
    pinger = KeepWarm(["https://a.example/"], fetch=lambda url: next(answers))
    for _ in range(6):
        pinger.ping_once()
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 2
    assert "not answering well: answered 503" in lines[0]
    assert "answering again" in lines[1]


def test_an_exception_is_reported_not_raised(capsys):
    def boom(url):
        raise OSError("network is down")

    pinger = KeepWarm(["https://a.example/"], fetch=boom)
    pinger.ping_once()
    pinger.ping_once()
    out = capsys.readouterr().out
    assert out.count("no answer (OSError)") == 1


def test_the_background_thread_keeps_pinging_and_stops_when_asked():
    calls = []
    pinger = KeepWarm(["https://a.example/"], interval=0.05, first_delay=0.01, fetch=lambda url: calls.append(url) or 303)
    pinger.start()
    deadline = time.monotonic() + 3
    while len(calls) < 3 and time.monotonic() < deadline:
        time.sleep(0.02)
    pinger.stop()
    assert len(calls) >= 3
    settled = len(calls)
    time.sleep(0.2)
    assert len(calls) == settled  # nothing after stop
    assert not any(t.name == "keepwarm" and t.is_alive() for t in threading.enumerate())


def test_without_addresses_there_is_no_thread():
    pinger = KeepWarm([])
    pinger.start()
    assert pinger._thread is None


def test_start_and_stop_follow_the_configuration(monkeypatch):
    monkeypatch.setattr(keepwarm, "KEEP_WARM_URLS", "off")
    keepwarm.start()
    assert keepwarm._instance is not None and keepwarm._instance._thread is None
    keepwarm.stop()
    assert keepwarm._instance is None
