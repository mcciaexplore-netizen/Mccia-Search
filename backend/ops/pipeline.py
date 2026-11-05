"""Runs long jobs one after another, unattended, on the server that also hosts the live site, and keeps them from hurting it.

  - Each step is a command. A step that fails is retried with a growing pause; one that cannot be made to work is recorded and the next step still runs.
  - Finished steps are remembered in STATE_DIR/state.json, so a restarted container carries on instead of starting over (every step is also safe to repeat).
  - The live site comes first. Every few seconds the guard asks the live backend's /health how fast it answers, and reads the server's load. If the site is
    slow or down, or the machine is overloaded, three times running, the running job is FROZEN (SIGSTOP: it keeps its memory and uses no CPU); three good
    readings in a row and it is thawed. Creating STATE_DIR/PAUSE freezes it by hand, deleting it thaws it, creating STATE_DIR/STOP ends the pipeline.
"""

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

LIVE_URL = os.environ.get("PIPELINE_LIVE_URL", "http://host.docker.internal:8002/health")
INTERVAL = 20.0  # seconds between readings of the live site
SLOW_SECONDS = 3.0  # a /health slower than this counts as "slow" (it normally answers in a few milliseconds)
LOAD_LIMIT = 4.0  # one-minute load average that counts as overloaded (the server has 2 CPUs)
BAD_TO_PAUSE = 3
GOOD_TO_RESUME = 3
STOP_GRACE_SECONDS = 30


def log(message: str) -> None:
    print(f"[pipeline] {time.strftime('%Y-%m-%d %H:%M:%S')} {message}", flush=True)


@dataclass
class Step:
    name: str
    command: list[str]
    env: dict[str, str] = field(default_factory=dict)
    retries: int = 5  # attempts before the step is given up on
    backoff_start: float = 30.0
    backoff_cap: float = 600.0


# ---- watching the live site ---------------------------------------------------------------------------------------------------------


def probe_live(url: str = LIVE_URL, timeout: float = 6.0) -> tuple[bool, float]:
    """(did /health answer ok, how many seconds it took)."""
    started = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            ok = response.status == 200 and b"ok" in response.read(200)
    except Exception:
        ok = False
    return ok, time.monotonic() - started


def read_load() -> float:
    try:
        return os.getloadavg()[0]
    except OSError:
        return 0.0


class Guard:
    """Decides from a stream of readings when to freeze and thaw a job. update() takes one reading and returns "pause", "resume" or None."""

    def __init__(self, probe: Callable[[], tuple[bool, float]] = probe_live, load: Callable[[], float] = read_load, slow: float = SLOW_SECONDS,
                 load_limit: float = LOAD_LIMIT, bad_needed: int = BAD_TO_PAUSE, good_needed: int = GOOD_TO_RESUME):
        self.probe, self.load, self.slow, self.load_limit = probe, load, slow, load_limit
        self.bad_needed, self.good_needed = bad_needed, good_needed
        self.paused = False
        self.disabled = False
        self._bad = self._good = 0
        self.last_reason = ""

    def calibrate(self, attempts: int = 3) -> bool:
        """Is the live site reachable from here at all? If it never answers, the guard would freeze every job for ever, so it switches itself off (loudly)."""
        for _ in range(attempts):
            if self.probe()[0]:
                return True
            time.sleep(0.2)
        self.disabled = True
        return False

    def update(self) -> str | None:
        if self.disabled:
            return None
        ok, seconds = self.probe()
        load = self.load()
        reason = "the live site did not answer" if not ok else f"the live site took {seconds:.1f}s" if seconds > self.slow else f"load is {load:.1f}" if load > self.load_limit else ""
        if reason:
            self._bad, self._good, self.last_reason = self._bad + 1, 0, reason
            if not self.paused and self._bad >= self.bad_needed:
                self.paused = True
                return "pause"
        else:
            self._good, self._bad = self._good + 1, 0
            if self.paused and self._good >= self.good_needed:
                self.paused = False
                return "resume"
        return None


# ---- running the steps ---------------------------------------------------------------------------------------------------------------


class Stopped(Exception):
    """STATE_DIR/STOP exists, or the container was told to stop."""


class State:
    """What has been done, kept in a file so that a restarted container knows."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / "state.json"
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {}

    def update(self, step: str, **fields) -> None:
        self.data.setdefault(step, {}).update(fields, updated=time.strftime("%Y-%m-%d %H:%M:%S"))
        self.directory.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1))
        tmp.replace(self.path)

    def is_done(self, step: str) -> bool:
        return self.data.get(step, {}).get("state") == "done"


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError):
        pass


def run_once(step: Step, guard: Guard, directory: Path, interval: float) -> int:
    """Run the step's command until it ends and return its exit code, freezing it while the live site is in trouble. Raises Stopped on request."""
    proc = subprocess.Popen(step.command, env={**os.environ, **step.env}, start_new_session=True)
    frozen_by_guard = frozen_by_hand = False
    try:
        while True:
            try:
                return proc.wait(timeout=interval)
            except subprocess.TimeoutExpired:
                pass
            if (directory / "STOP").exists() or _STOP_REQUESTED:
                raise Stopped()
            decision = guard.update()
            if decision == "pause":
                frozen_by_guard = True
                log(f"{step.name}: FROZEN to protect the live site ({guard.last_reason})")
            elif decision == "resume":
                frozen_by_guard = False
                log(f"{step.name}: live site is fine again, thawing")
            by_hand = (directory / "PAUSE").exists()
            if by_hand != frozen_by_hand:
                frozen_by_hand = by_hand
                log(f"{step.name}: {'frozen by hand (PAUSE file)' if by_hand else 'thawed by hand (PAUSE file removed)'}")
            _signal_group(proc, signal.SIGSTOP if (frozen_by_guard or frozen_by_hand) else signal.SIGCONT)
    except BaseException:
        _signal_group(proc, signal.SIGCONT)  # a frozen process cannot hear SIGTERM
        _signal_group(proc, signal.SIGTERM)
        try:
            proc.wait(timeout=STOP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            _signal_group(proc, signal.SIGKILL)
        raise


def run_step(step: Step, guard: Guard, state: State, interval: float = INTERVAL, sleep: Callable[[float], None] = time.sleep) -> bool:
    for attempt in range(1, step.retries + 1):
        state.update(step.name, state="running", attempt=attempt)
        log(f"step {step.name}: started (attempt {attempt} of {step.retries})")
        code = run_once(step, guard, state.directory, interval)
        if code == 0:
            state.update(step.name, state="done", exit=0)
            log(f"step {step.name}: DONE")
            return True
        wait = min(step.backoff_cap, step.backoff_start * 2 ** (attempt - 1))
        state.update(step.name, state="retrying", exit=code)
        log(f"step {step.name}: exited with {code}" + (f"; trying again in {wait:.0f}s" if attempt < step.retries else "; giving up"))
        if attempt < step.retries:
            waited = 0.0
            while waited < wait:  # in small pieces, so that STOP is noticed
                if (state.directory / "STOP").exists() or _STOP_REQUESTED:
                    raise Stopped()
                sleep(min(5.0, wait - waited))
                waited += 5.0
    state.update(step.name, state="failed")
    return False


def run_pipeline(steps: list[Step], guard: Guard, directory: Path, interval: float = INTERVAL, sleep: Callable[[float], None] = time.sleep) -> dict[str, str]:
    """Run every step that is not done yet, in order; a step that fails for good does not stop the ones after it. Returns {step: state}."""
    state = State(directory)
    try:
        for step in steps:
            if state.is_done(step.name):
                log(f"step {step.name}: already done, skipping")
                continue
            run_step(step, guard, state, interval, sleep)
    except Stopped:
        log("stop requested: ending the pipeline (finished steps stay finished)")
        for step in steps:
            if not state.is_done(step.name):
                state.update(step.name, state="stopped")
    return {step.name: state.data.get(step.name, {}).get("state", "not started") for step in steps}


_STOP_REQUESTED = False


def _request_stop(signum, frame) -> None:
    global _STOP_REQUESTED
    _STOP_REQUESTED = True


# ---- the steps of this deployment ------------------------------------------------------------------------------------------------------------

PILOT_PDFS = [
    "2011-Jan.PDF:all",
    "1961-April To1962-March Part-1.pdf:every:6,27,44,95,146,214,248,350,367",
    "1952 April.PDF:every:8",
    "1945 July.PDF:every:6",
]


def default_steps(state_dir: str = "/state") -> list[Step]:
    common = {"OCR_CACHE_DIR": f"{state_dir}/ocr_cache", "PYTHONUNBUFFERED": "1", "HF_HUB_OFFLINE": "1"}
    return [
        # 1. read the rest of the 1970 volume (its first 123 pages were read on the Mac and copied here)
        Step("ocr-1970", ["python", "-m", "ingest.parallel_ingest", "--engine", "tesseract", "--start-year", "1961", "--end-year", "1970"],
             env={**common, "OCR_ENGINE": "tesseract", "OCR_CONCURRENCY": "2", "HF_HUB_OFFLINE": "0"}, retries=5),
        # 2. Gemini vectors for 1966 and 1970, so the live site can search them
        Step("embed-gemini", ["python", "-m", "ingest.embed_pending"], env={**common, "EMBEDDING_ENGINE": "gemini", "HF_HUB_OFFLINE": "0"}, retries=8),
        # 3. the bge-m3 vectors for every chunk (days on this CPU); resumes from what the database already holds
        Step("bge-fill", ["python", "-m", "ingest.reembed_all", "--yes"], env={**common, "EMBEDDING_ENGINE": "bge"}, retries=200, backoff_cap=900),
        # 4. the 200-page photo pilot: writes only to STATE_DIR/photo-pilot, nothing to the database or to storage
        Step("photo-pilot", ["python", "-m", "photos.pilot", "--out", f"{state_dir}/photo-pilot", "--cache", f"{state_dir}/pdfs", *PILOT_PDFS],
             env={**common, "OCR_ENGINE": "tesseract", "YUNET_MODEL": f"{state_dir}/yunet.onnx"}, retries=3),
    ]


def main() -> int:
    state_dir = Path(os.environ.get("PIPELINE_STATE", "/state"))
    state_dir.mkdir(parents=True, exist_ok=True)
    signal.signal(signal.SIGTERM, _request_stop)
    guard = Guard()
    if guard.calibrate():
        log(f"guard on: watching {LIVE_URL} every {INTERVAL:.0f}s")
    else:
        log(f"WARNING: {LIVE_URL} does not answer from here, so the live-site guard is OFF (only the CPU and memory limits protect the site)")
    results = run_pipeline(default_steps(str(state_dir)), guard, state_dir)
    log("all steps ended: " + ", ".join(f"{name}={outcome}" for name, outcome in results.items()))
    while not _STOP_REQUESTED:  # nothing more to do, and exiting would only make Docker start the container again
        time.sleep(60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
