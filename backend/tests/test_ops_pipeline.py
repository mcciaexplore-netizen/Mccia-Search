"""The unattended supervisor (ops/pipeline.py): ordering, retries, restarts, and above all protecting the live site by freezing the job."""

import json
import subprocess
import sys
import threading
import time

import pytest

from ops import pipeline as P
from ops.pipeline import Guard, Step, run_pipeline


def py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def healthy_guard():
    return Guard(probe=lambda: (True, 0.01), load=lambda: 0.1)


def run(steps, tmp_path, guard=None, **kw):
    return run_pipeline(steps, guard or healthy_guard(), tmp_path, interval=kw.pop("interval", 0.05), sleep=kw.pop("sleep", lambda s: None))


def states(tmp_path):
    return json.loads((tmp_path / "state.json").read_text())


# ---- order, retries, restarts --------------------------------------------------------------------------------------------------------------


def test_steps_run_in_order_and_are_recorded(tmp_path):
    log = tmp_path / "log"
    steps = [Step(n, py(f"open(r'{log}', 'a').write('{n}\\n')")) for n in ("one", "two", "three")]
    assert run(steps, tmp_path) == {"one": "done", "two": "done", "three": "done"}
    assert log.read_text().split() == ["one", "two", "three"] and states(tmp_path)["two"]["state"] == "done"


def test_a_step_that_fails_is_tried_again_until_it_works(tmp_path):
    counter = tmp_path / "count"
    code = f"import pathlib,sys; p=pathlib.Path(r'{counter}'); n=int(p.read_text()) if p.exists() else 0; p.write_text(str(n+1)); sys.exit(0 if n>=2 else 1)"
    pauses = []
    assert run([Step("flaky", py(code), retries=5, backoff_start=10)], tmp_path, sleep=pauses.append) == {"flaky": "done"}
    assert counter.read_text() == "3" and states(tmp_path)["flaky"]["attempt"] == 3
    assert sum(pauses) >= 10 + 20  # the pause grew: 10 s, then 20 s


def test_the_pause_between_attempts_grows_but_has_a_ceiling(tmp_path):
    pauses = []
    run([Step("bad", py("import sys; sys.exit(3)"), retries=6, backoff_start=100, backoff_cap=250)], tmp_path, sleep=pauses.append)
    waits = [round(sum(pauses[i:i + 60]), 0) for i in range(0, len(pauses), 60)]  # sleeps are made in pieces of at most 5 s
    assert sum(pauses) == pytest.approx(100 + 200 + 250 + 250 + 250, abs=25)  # 5 pauses between 6 attempts, none above 250 s


def test_a_step_that_never_works_is_given_up_on_and_the_next_still_runs(tmp_path):
    marker = tmp_path / "ran"
    steps = [Step("bad", py("import sys; sys.exit(1)"), retries=2, backoff_start=1), Step("good", py(f"open(r'{marker}', 'w').write('x')"))]
    assert run(steps, tmp_path) == {"bad": "failed", "good": "done"} and marker.exists()


def test_a_restarted_container_skips_what_is_done_and_does_the_rest(tmp_path):
    log = tmp_path / "log"
    make = lambda n: Step(n, py(f"open(r'{log}', 'a').write('{n}\\n')"))
    run([make("one")], tmp_path)
    run([make("one"), make("two")], tmp_path)
    assert log.read_text().split() == ["one", "two"]  # "one" was not run a second time


def test_the_state_file_is_never_half_written(tmp_path):
    state = P.State(tmp_path)
    state.update("a", state="running")
    assert json.loads((tmp_path / "state.json").read_text())["a"]["state"] == "running" and not list(tmp_path.glob("*.tmp"))


def test_the_steps_of_this_deployment_are_the_four_asked_for_in_order():
    steps = P.default_steps("/state")
    assert [s.name for s in steps] == ["ocr-1970", "embed-gemini", "bge-fill", "photo-pilot"]
    ocr, embed, fill, pilot = steps
    assert ocr.command[-4:] == ["--start-year", "1961", "--end-year", "1970"] and ocr.env["OCR_ENGINE"] == "tesseract"
    assert embed.env["EMBEDDING_ENGINE"] == "gemini" and fill.env["EMBEDDING_ENGINE"] == "bge" and fill.command[-2:] == ["ingest.reembed_all", "--yes"][-2:]
    assert pilot.command[:4] == ["python", "-m", "photos.pilot", "--out"] and "/state/photo-pilot" in pilot.command  # nothing to the database
    assert all(s.env["OCR_CACHE_DIR"] == "/state/ocr_cache" for s in steps)  # the page cache is on the mounted folder, so it survives a restart
    assert fill.retries >= 100


# ---- the guard's decisions -----------------------------------------------------------------------------------------------------------


def scripted(readings, load=0.1):
    it = iter(readings)
    return Guard(probe=lambda: next(it), load=lambda: load)


def test_three_bad_readings_in_a_row_pause_and_three_good_ones_resume():
    g = scripted([(True, 0.01), (False, 6), (False, 6), (False, 6), (True, 0.01), (True, 0.01), (True, 0.01)])
    assert [g.update() for _ in range(7)] == [None, None, None, "pause", None, None, "resume"]


def test_one_good_reading_in_between_starts_the_count_again():
    g = scripted([(False, 6), (False, 6), (True, 0.01), (False, 6), (False, 6)])
    assert [g.update() for _ in range(5)] == [None] * 5


def test_a_slow_answer_counts_as_bad_even_if_it_is_ok():
    g = scripted([(True, 5.0)] * 3)
    assert [g.update() for _ in range(3)] == [None, None, "pause"] and "took 5.0s" in g.last_reason


def test_a_heavily_loaded_machine_counts_as_bad():
    g = scripted([(True, 0.01)] * 3, load=9.0)
    assert [g.update() for _ in range(3)] == [None, None, "pause"] and "load is 9.0" in g.last_reason


def test_a_paused_guard_stays_paused_while_things_stay_bad():
    g = scripted([(False, 6)] * 6)
    assert [g.update() for _ in range(6)] == [None, None, "pause", None, None, None]


def test_if_the_live_site_never_answers_from_here_the_guard_switches_itself_off_instead_of_freezing_everything_for_ever(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    g = scripted([(False, 6)] * 10)
    assert g.calibrate() is False and g.disabled and g.update() is None
    assert scripted([(False, 6), (True, 0.01)]).calibrate() is True


# ---- the guard really freezes the job -------------------------------------------------------------------------------------------------------


def process_state(pid: int) -> str:
    return subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()[:1]


def wait_for(condition, seconds=10.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.05)
    return False


def test_the_job_is_frozen_while_the_live_site_is_slow_and_continues_when_it_recovers(tmp_path):
    ticks, pidfile = tmp_path / "ticks", tmp_path / "pid"
    code = (f"import os,time,pathlib; pathlib.Path(r'{pidfile}').write_text(str(os.getpid()))\n"
            f"for i in range(400):\n open(r'{ticks}','a').write('x'); time.sleep(0.02)")
    readings = iter([(True, 0.01)] + [(False, 6)] * 3 + [(False, 6)] * 20 + [(True, 0.01)] * 200)
    guard = Guard(probe=lambda: next(readings), load=lambda: 0.1)
    result = {}
    thread = threading.Thread(target=lambda: result.update(run([Step("job", py(code))], tmp_path, guard, interval=0.1)))
    thread.start()
    assert wait_for(pidfile.exists)
    pid = int(pidfile.read_text())
    assert wait_for(lambda: process_state(pid) == "T"), "the job should be frozen while the site is in trouble"
    frozen_at = len(ticks.read_text())
    time.sleep(0.5)
    assert len(ticks.read_text()) == frozen_at  # no progress at all while frozen
    assert wait_for(lambda: process_state(pid) in ("S", "R") or not process_state(pid), 15.0) and wait_for(lambda: len(ticks.read_text()) > frozen_at, 15.0)
    thread.join(timeout=30)
    assert result == {"job": "done"}


def test_the_pause_file_freezes_the_job_by_hand_and_removing_it_thaws_it(tmp_path):
    pidfile = tmp_path / "pid"
    code = f"import os,time,pathlib; pathlib.Path(r'{pidfile}').write_text(str(os.getpid())); time.sleep(3)"
    (tmp_path / "PAUSE").write_text("")
    result = {}
    thread = threading.Thread(target=lambda: result.update(run([Step("job", py(code))], tmp_path, interval=0.1)))
    thread.start()
    assert wait_for(pidfile.exists)
    pid = int(pidfile.read_text())
    assert wait_for(lambda: process_state(pid) == "T")
    (tmp_path / "PAUSE").unlink()
    thread.join(timeout=30)
    assert result == {"job": "done"}


def test_the_stop_file_ends_the_job_and_the_pipeline_and_leaves_finished_steps_finished(tmp_path):
    done_marker = tmp_path / "first"
    steps = [Step("first", py(f"open(r'{done_marker}', 'w').write('x')")), Step("long", py("import time; time.sleep(60)")), Step("never", py("pass"))]
    result = {}
    thread = threading.Thread(target=lambda: result.update(run(steps, tmp_path, interval=0.1)))
    thread.start()
    assert wait_for(done_marker.exists)
    time.sleep(0.5)
    (tmp_path / "STOP").write_text("")
    thread.join(timeout=30)
    assert not thread.is_alive() and result == {"first": "done", "long": "stopped", "never": "stopped"}


def test_a_frozen_job_can_still_be_stopped(tmp_path):
    pidfile = tmp_path / "pid"
    code = f"import os,time,pathlib; pathlib.Path(r'{pidfile}').write_text(str(os.getpid())); time.sleep(60)"
    (tmp_path / "PAUSE").write_text("")
    result = {}
    thread = threading.Thread(target=lambda: result.update(run([Step("job", py(code))], tmp_path, interval=0.1)))
    thread.start()
    assert wait_for(pidfile.exists)
    pid = int(pidfile.read_text())
    assert wait_for(lambda: process_state(pid) == "T")
    (tmp_path / "STOP").write_text("")
    thread.join(timeout=30)
    assert not thread.is_alive() and wait_for(lambda: process_state(pid) in ("", "Z"))  # gone, although it was frozen
