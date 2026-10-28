"""Script stdout is empty when nothing is new, and present when there is news.

The scheduled script does not share the scheduler's cron identity.
watch_stdout must still fetch and record. print_job_watch must still refuse
to record when the cron mark cannot be read.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import service
from service import CANNOT_WATCH, watch
from test_plugin import Script, deps, load, view


def test_watch_stdout_is_empty_when_nothing_is_new_and_prints_an_event(tmp_path):
    script = Script(view("Printing") + view("Operational", completion=100, filepos=None, left=None, name=None))
    base, _clock = deps(script, tmp_path)
    refused = load(watch(base, {}))
    assert refused["message"] == CANNOT_WATCH
    assert script.calls == []
    assert service.watch_stdout(base) == ""
    assert script.calls
    assert not any(call["method"] == "POST" for call in script.calls)
    assert (tmp_path / "watch_state.json").is_file()
    text = service.watch_stdout(base)
    assert "Events: complete." in text
    assert base.api_key not in text
    saved = json.loads((tmp_path / "watch_state.json").read_text(encoding="utf-8"))
    assert base.api_key not in json.dumps(saved)


def test_schedule_writes_the_script_and_unschedule_removes_it(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))

    class Cron:
        def __init__(self):
            self.jobs = []

        def list_jobs(self, include_disabled=True):
            return list(self.jobs)

        def create_job(self, **kwargs):
            job = {
                "id": "job1",
                "name": kwargs["name"],
                "deliver": kwargs["deliver"],
                "schedule_display": kwargs["schedule"],
                "script": kwargs["script"],
                "no_agent": kwargs["no_agent"],
            }
            self.jobs = [job]
            return job

        def remove_job(self, job_id):
            self.jobs = [job for job in self.jobs if job["id"] != job_id]

    cron = Cron()
    base, _clock = deps(Script([]), tmp_path, cron_module=cron)
    made = load(service.schedule(base, "*/5 * * * *", "local"))
    assert made["ok"] is True, made
    script = home / "scripts" / service.SCRIPT_NAME
    assert script.is_file()
    body = script.read_text(encoding="utf-8")
    assert "watch_stdout" in body
    assert "build_profile_secret_scope" in body
    assert "cron.jobs" not in body
    assert "[SILENT]" not in body
    assert cron.jobs[0]["no_agent"] is True
    assert cron.jobs[0]["script"] == service.SCRIPT_NAME
    gone = load(service.unschedule(base))
    assert gone["ok"] is True, gone
    assert not script.exists()
    assert cron.jobs == []


def _python_with_hermes() -> str | None:
    done = subprocess.run(
        [sys.executable, "-c", "import hermes_constants"],
        capture_output=True,
        text=True,
    )
    if done.returncode == 0:
        return sys.executable
    return None


def test_script_stdout_is_empty_or_the_watch_text(tmp_path, monkeypatch):
    python = _python_with_hermes()
    if python is None:
        pytest.skip("this interpreter cannot import hermes_constants")
    home = tmp_path / "home"
    plugin = home / "plugins" / "print-job-watch"
    plugin.mkdir(parents=True)
    (plugin / "service.py").write_text("def watch_stdout():\n    return ''\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    assert service.write_watch_script() is None
    script = home / "scripts" / service.SCRIPT_NAME
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    quiet = subprocess.run([python, str(script)], capture_output=True, text=True, env=env)
    assert quiet.returncode == 0, quiet.stderr
    assert quiet.stdout == ""
    (plugin / "service.py").write_text("def watch_stdout():\n    return 'hello post'\n", encoding="utf-8")
    loud = subprocess.run([python, str(script)], capture_output=True, text=True, env=env)
    assert loud.returncode == 0, loud.stderr
    assert loud.stdout == "hello post\n"
    assert "test-api-key" not in loud.stdout
    (plugin / "service.py").unlink()
    missing = subprocess.run([python, str(script)], capture_output=True, text=True, env=env)
    assert missing.returncode != 0
    assert missing.stdout == ""


def test_hermes_bin_prefers_the_interpreter_sibling(tmp_path, monkeypatch):
    sibling = tmp_path / "hermes"
    sibling.write_text("#!/bin/sh\n", encoding="utf-8")
    sibling.chmod(0o755)
    monkeypatch.setattr(service.sys, "executable", str(tmp_path / "python"))
    monkeypatch.setattr(service.shutil, "which", lambda _name: "/somewhere/else/hermes")
    assert service._hermes_bin() == str(sibling)


def test_default_home_schedule_lists_a_no_agent_job_and_unschedule_removes_it(tmp_path, monkeypatch):
    user = tmp_path / "user"
    home = user / ".hermes"
    data = home / "plugin-data" / "print-job-watch"
    data.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(user))
    monkeypatch.setenv("HERMES_HOME", str(home))
    hermes = Path(sys.executable).parent / "hermes"
    if not hermes.is_file():
        pytest.skip("hermes is not next to this interpreter")
    base, _clock = deps(Script([]), data)
    out = json.loads(service.schedule(base, "*/5 * * * *", "local"))
    assert out["ok"] is True, out
    script = home / "scripts" / service.SCRIPT_NAME
    assert script.is_file()
    env = os.environ.copy()
    listed = subprocess.run(
        [str(hermes), "cron", "list"],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        stdin=subprocess.DEVNULL,
    )
    assert listed.returncode == 0, listed.stderr
    shown = listed.stdout + listed.stderr
    assert "print-job-watch" in shown
    assert "no-agent" in shown
    gone = json.loads(service.unschedule(base))
    assert gone["ok"] is True, gone
    assert not script.exists()
    after = subprocess.run(
        [str(hermes), "cron", "list"],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        stdin=subprocess.DEVNULL,
    )
    assert after.returncode == 0, after.stderr
    quiet = after.stdout + after.stderr
    assert "print-job-watch" not in quiet
    assert "No scheduled jobs" in quiet
