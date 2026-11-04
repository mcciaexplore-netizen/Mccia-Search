"""Keep the web app's sign-in function warm.

Vercel shuts an idle function down within minutes, and the free plan runs crons once a day at most, so the first sign-in after a quiet
spell waits for a cold start (about 2.5 s). This sends a cheap GET to the web app every few minutes from the backend, which is always on.

What it calls (config.KEEP_WARM_URLS, by default the web app's /api/sso/callback) must be an open address that needs no session and does
nothing: a GET to the callback only redirects to the front door. The redirect is NOT followed, so a ping never reaches the hub or a page.
Nothing here touches the database, and nothing secret is sent or kept.
"""

import threading
import urllib.error
import urllib.request
from urllib.parse import urlparse

from config import KEEP_WARM_INTERVAL_SECONDS, KEEP_WARM_URLS

TIMEOUT_SECONDS = 10
FIRST_PING_DELAY_SECONDS = 5  # shortly after the backend starts, so a deploy leaves the function warm too
OFF_WORDS = {"off", "none", "false", "0", "no"}


class _DoNotFollow(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # a 3xx is an answer, not an invitation
        return None


_opener = urllib.request.build_opener(_DoNotFollow)


def fetch_status(url: str) -> int:
    """The HTTP status of a GET to `url`, without following redirects. Raises OSError if there is no answer at all."""
    request = urllib.request.Request(url, headers={"User-Agent": "mccia-keepwarm/1"})
    try:
        with _opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status
    except urllib.error.HTTPError as answer:  # 3xx, 4xx and 5xx arrive as errors; they are still answers
        return answer.code


def parse_urls(value: str) -> list[str]:
    """The addresses in a comma-separated setting: https (or http to this machine) only, anything else is dropped."""
    if value.strip().lower() in OFF_WORDS:
        return []
    urls = []
    for part in value.split(","):
        url = part.strip()
        parsed = urlparse(url)
        local = parsed.hostname in ("localhost", "127.0.0.1")
        if (parsed.scheme == "https" and parsed.hostname) or (parsed.scheme == "http" and local):
            urls.append(url)
        elif url:
            print(f"[keepwarm] ignoring {url!r}: only https addresses are pinged")
    return urls


class KeepWarm:
    """Pings each address every `interval` seconds on a background thread, and says so only when an address starts or stops answering."""

    def __init__(self, urls: list[str], interval: float = KEEP_WARM_INTERVAL_SECONDS, fetch=fetch_status, first_delay: float = FIRST_PING_DELAY_SECONDS):
        self.urls, self.interval, self.first_delay, self._fetch = urls, interval, first_delay, fetch
        self._down: set[str] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def ping_once(self) -> None:
        for url in self.urls:
            try:
                status = self._fetch(url)
                failed = status >= 500
                detail = f"answered {status}"
            except Exception as exc:  # noqa: BLE001 - a ping must never take the backend down
                failed, detail = True, f"no answer ({type(exc).__name__})"
            if failed and url not in self._down:
                self._down.add(url)
                print(f"[keepwarm] {url} is not answering well: {detail}")
            elif not failed and url in self._down:
                self._down.discard(url)
                print(f"[keepwarm] {url} is answering again")

    def _run(self) -> None:
        delay = self.first_delay
        while not self._stop.wait(delay):
            self.ping_once()
            delay = self.interval

    def start(self) -> None:
        if not self.urls or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="keepwarm", daemon=True)
        self._thread.start()
        print(f"[keepwarm] pinging {len(self.urls)} address(es) every {int(self.interval)} s")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=TIMEOUT_SECONDS + 2)
            self._thread = None


_instance: KeepWarm | None = None


def start() -> None:
    """Start the pings the configuration asks for (none if it says "off")."""
    global _instance
    _instance = KeepWarm(parse_urls(KEEP_WARM_URLS))
    _instance.start()


def stop() -> None:
    global _instance
    if _instance is not None:
        _instance.stop()
        _instance = None
