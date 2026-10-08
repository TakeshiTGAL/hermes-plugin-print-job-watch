"""Offline tests. Bodies are the ones saved from OctoPrint 1.11.8 Virtual Printer."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import gates
from client import PERMISSION_ERROR, ApiError, parse_origin
from client import OctoPrint
from service import CANNOT_WATCH, Deps, canonical_deliver, control, deliver_looks_like_schedule, schedule, status, unschedule, watch


SECRET = "test-api-key-value"
ORIGIN = "http://printer.example:5000"


def job(state, completion=10, filepos=100, left=0, name="tiny.gcode", error=None):
    body = {
        "job": {"file": {"name": name}, "estimatedPrintTime": 6.7, "user": "_api"},
        "progress": {
            "completion": completion,
            "filepos": filepos,
            "printTimeLeft": left,
            "printTimeLeftOrigin": "linear",
        },
        "state": state,
    }
    if error is not None:
        body["error"] = error
    return body


def connection(state="Operational"):
    return {"current": {"state": state, "port": "VIRTUAL", "baudrate": 115200, "printerProfile": "_default"}, "options": {}}


def printer(text="Operational"):
    return {
        "temperature": {"tool0": {"actual": 21.3, "target": 0.0}, "bed": {"actual": 21.3, "target": 0.0}},
        "state": {"text": text, "flags": {"operational": text == "Operational", "paused": text == "Paused", "printing": text == "Printing", "error": False}},
    }


NOT_OPERATIONAL = {"error": "Printer is not operational"}
NOT_PRINTING = {"error": "Printer is neither printing nor paused, 'cancel' command cannot be performed"}
BAD_COMMAND = {"error": "command is invalid"}
FORBIDDEN = {"error": PERMISSION_ERROR}


class Script:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, method, url, headers, body, timeout, cap):
        self.calls.append({"method": method, "url": url, "body": body, "header_names": sorted(headers)})
        status_code, payload = self.replies.pop(0)
        if payload is None:
            raw = b""
        elif isinstance(payload, bytes):
            raw = payload
        else:
            raw = json.dumps(payload).encode()
        return status_code, {"content-type": "application/json"}, raw


def view(state, connection_state="Operational", printer_status=200, **kwargs):
    printer_body = printer(state) if printer_status == 200 else NOT_OPERATIONAL
    return [
        (200, connection(connection_state)),
        (200, job(state, **kwargs)),
        (printer_status, printer_body),
    ]


def deps(script, tmp_path, **kwargs):
    clock = {"now": 1_000_000.0}

    def now():
        return clock["now"]

    def sleep(seconds):
        clock["now"] += seconds

    base = dict(
        url=ORIGIN,
        api_key=SECRET,
        data_dir=tmp_path,
        transport=script,
        now=now,
        sleep=sleep,
        approver=lambda text, rule: (True, ""),
        blocker=lambda action: None,
        write_guard=lambda path: None,
        control_timeout=0,
    )
    base.update(kwargs)
    return Deps(**base), clock


def load(text):
    return json.loads(text)


def test_bad_key_and_conflict_bodies():
    assert "permission" in PERMISSION_ERROR
    assert NOT_OPERATIONAL["error"] == "Printer is not operational"
    assert "neither printing nor paused" in NOT_PRINTING["error"]


def test_origin_rejects_userinfo_path_and_query():
    assert parse_origin(ORIGIN).value == ORIGIN
    with pytest.raises(ApiError):
        parse_origin("http://user:pw@printer.example:5000")
    with pytest.raises(ApiError):
        parse_origin("http://printer.example:5000/octoprint")
    with pytest.raises(ApiError):
        parse_origin("http://printer.example:5000?apikey=secret")


def test_status_disconnected_is_not_paused(tmp_path):
    script = Script(view("Offline", connection_state="Closed", printer_status=409, completion=None, filepos=None, left=None, name=None))
    result = load(status(deps(script, tmp_path)[0], {}))
    assert result["kind"] == "disconnected"
    assert result["temperatures"] is None
    assert "not a pause" in result["message"]
    assert all("/api/settings" not in call["url"] for call in script.calls)


def test_error_with_printer_409_is_not_disconnect(tmp_path):
    script = Script(view("Error", connection_state="Error", printer_status=409, error="Thermal runaway"))
    result = load(status(deps(script, tmp_path)[0], {}))
    assert result["kind"] == "error"
    assert result["kind"] != "disconnected"
    assert "Thermal runaway" in result["message"]


def test_offline_after_error_is_error(tmp_path):
    script = Script(view("Offline after error", connection_state="Operational", printer_status=409, error="min temp"))
    result = load(status(deps(script, tmp_path)[0], {}))
    assert result["kind"] == "error"


def test_print_time_left_zero_stays_zero(tmp_path):
    script = Script(view("Printing", left=0, completion=82.9, filepos=2211))
    result = load(status(deps(script, tmp_path)[0], {}))
    assert result["print_time_left"] == 0
    assert result["temperatures"]["tool0"]["actual"] == 21.3


def test_forbidden_key(tmp_path):
    script = Script([(403, FORBIDDEN)])
    result = load(status(deps(script, tmp_path)[0], {}))
    assert result["error"] == "unauthorized"
    assert SECRET not in result["message"]


def test_pause_sends_action_and_waits_for_paused(tmp_path):
    script = Script(view("Printing") + view("Printing") + [(204, None)] + view("Pausing") + view("Paused"))
    seen = []

    def approve(text, rule):
        seen.append((text, rule))
        return True, ""

    result = load(control(deps(script, tmp_path, approver=approve, control_timeout=30)[0], {"action": "pause"}))
    assert result["moved"] is True
    assert result["job_state"] == "Paused"
    posted = [call for call in script.calls if call["method"] == "POST"]
    assert len(posted) == 1
    assert json.loads(posted[0]["body"]) == {"command": "pause", "action": "pause"}
    assert "cannot be resumed" not in seen[0][0]
    assert "pause" in seen[0][0]


def test_pausing_is_not_a_failure_and_does_not_invite_resume(tmp_path):
    script = Script(view("Printing") + view("Printing") + [(204, None)] + view("Pausing"))
    result = load(control(deps(script, tmp_path, control_timeout=0)[0], {"action": "pause"}))
    assert result["ok"] is True
    assert result["moved"] is False
    assert result["accepted"] is True
    assert "not a failure" in result["message"]
    assert "do not send resume" in result["next_step"].lower()
    assert len([call for call in script.calls if call["method"] == "POST"]) == 1


def test_resume_during_pausing_is_not_sent(tmp_path):
    script = Script(view("Pausing"))
    result = load(control(deps(script, tmp_path)[0], {"action": "resume"}))
    assert result["moved"] is False
    assert script.calls[0]["method"] == "GET"
    assert not any(call["method"] == "POST" for call in script.calls)
    assert "does not continue the print" in result["message"]


def test_cancel_text_says_it_cannot_be_resumed(tmp_path):
    script = Script(view("Printing") + view("Printing") + [(204, None)] + view("Operational", completion=40, filepos=None, left=None))
    seen = []
    result = load(control(
        deps(script, tmp_path, approver=lambda text, rule: seen.append(text) or (True, ""), control_timeout=5)[0],
        {"action": "cancel"},
    ))
    assert result["moved"] is True
    assert "cannot be resumed" in seen[0]
    assert json.loads([call for call in script.calls if call["method"] == "POST"][0]["body"]) == {"command": "cancel"}


def test_second_call_uses_a_new_rule_key(tmp_path):
    keys = []

    def approve(text, rule):
        keys.append(rule)
        return True, ""

    script = Script(view("Printing") + view("Printing") + [(204, None)] + view("Paused") + view("Paused"))
    base, _clock = deps(script, tmp_path, approver=approve, control_timeout=5)
    control(base, {"action": "pause"})
    control(base, {"action": "pause"})
    assert len(keys) == 1
    script.replies = view("Printing") + view("Printing") + [(204, None)] + view("Paused")
    control(base, {"action": "pause"})
    assert keys[0] != keys[1]
    assert keys[0].startswith("print_job_control:pause:")


def test_approval_over_300_chars_is_not_sent(tmp_path):
    long_origin = "http://" + ("a" * 240) + ".example:5000"
    script = Script(view("Printing"))
    result = load(control(deps(script, tmp_path, url=long_origin)[0], {"action": "pause"}))
    assert result["error"] == "approval_cut"
    assert not any(call["method"] == "POST" for call in script.calls)


def test_timeout_says_it_may_have_arrived(tmp_path):
    script = Script(view("Printing") + view("Printing"))

    def explode(method, url, headers, body, timeout, cap):
        if method == "POST":
            raise ApiError(
                "timeout",
                "OctoPrint did not answer before the time limit.",
                "The command may still have reached the printer and take effect later. Read the state before sending it again.",
            )
        return script(method, url, headers, body, timeout, cap)

    result = load(control(deps(script, tmp_path, transport=explode, control_timeout=5)[0], {"action": "pause"}))
    assert result["moved"] is False
    assert "may still have reached" in result["next_step"]
    assert "again" in result["next_step"]


def test_blocked_contexts_do_not_call_http(tmp_path, monkeypatch):
    def loader(module, name):
        if name == "_is_cron_approval_context":
            return "ok", lambda: True
        if name == "_get_approval_mode":
            return "ok", lambda: "manual"
        return "ok", lambda: False

    monkeypatch.setattr(gates, "_load", loader)
    assert "cron" in gates.motion_block_reason("pause")
    monkeypatch.setattr(gates, "_load", lambda module, name: ("missing", None))
    assert gates.motion_block_reason("pause").startswith("BLOCKED")
    monkeypatch.delenv("HERMES_PLUGIN_HOST_PROCESS", raising=False)
    monkeypatch.setattr(gates, "_load", lambda module, name: ("ok", (lambda: "manual") if name == "_get_approval_mode" else (lambda: False)))
    assert gates.motion_block_reason("pause") is None
    monkeypatch.setenv("HERMES_PLUGIN_HOST_PROCESS", "0")
    assert gates.motion_block_reason("pause") is None
    monkeypatch.setenv("HERMES_PLUGIN_HOST_PROCESS", "1")
    assert "plugin-host" in gates.motion_block_reason("pause")
    script = Script(view("Printing"))
    result = load(control(deps(script, tmp_path, blocker=lambda action: "BLOCKED: cron")[0], {"action": "pause"}))
    assert result["moved"] is False
    assert script.calls == []


def test_watch_disconnect_paused_complete_and_finishing(tmp_path):
    folder = tmp_path
    base, clock = deps(Script(view("Printing", completion=10, filepos=10)), folder)
    first = load(watch(base, {}, record=True))
    assert first["events"] == []
    saved_first = json.loads((folder / "watch_state.json").read_text())
    assert saved_first["print_time_left"] == 0
    base.transport = Script(view("Paused", completion=10, filepos=10))
    paused = load(watch(base, {}, record=True))
    assert paused["events"] == ["paused"]
    base.transport = Script(view("Finishing", completion=99, filepos=20))
    assert load(watch(base, {}, record=True))["events"] == []
    base.transport = Script(view("Operational", completion=100, filepos=30, left=0))
    done = load(watch(base, {}, record=True))
    assert done["events"] == ["complete"]
    base.transport = Script(view("Offline", connection_state="Closed", printer_status=409, completion=None, filepos=None, left=None))
    down = load(watch(base, {}, record=True))
    assert down["events"] == ["printer_disconnected"]
    assert "not paused" in down["message"]
    saved = json.loads((folder / "watch_state.json").read_text())
    assert SECRET not in json.dumps(saved)
    assert "user" not in saved
    assert saved["print_time_left"] is None


def test_cancelling_is_cancelled_once(tmp_path):
    base, _clock = deps(Script(view("Printing")), tmp_path)
    watch(base, {}, record=True)
    base.transport = Script(view("Cancelling", completion=None, filepos=None, left=None))
    assert load(watch(base, {}, record=True))["events"] == ["cancelled"]
    base.transport = Script(view("Operational", completion=40, filepos=None, left=None))
    assert load(watch(base, {}, record=True))["events"] == []
    finished, _clock = deps(Script(view("Printing")), tmp_path / "done")
    watch(finished, {}, record=True)
    finished.transport = Script(view("Cancelling", completion=None, filepos=None, left=None))
    watch(finished, {}, record=True)
    finished.transport = Script(view("Operational", completion=100, filepos=None, left=0))
    assert load(watch(finished, {}, record=True))["events"] == []


def test_watch_previous_states_match_the_public_lists(tmp_path):
    paused_from = ("Printing", "Printing from SD", "Pausing", "Resuming")
    for previous in paused_from:
        folder = tmp_path / f"paused-{previous.replace(' ', '-')}"
        folder.mkdir()
        base, _clock = deps(Script(view(previous, completion=10, filepos=10)), folder)
        assert load(watch(base, {}, record=True))["events"] == []
        base.transport = Script(view("Paused", completion=10, filepos=10))
        got = load(watch(base, {}, record=True))
        assert got["events"] == ["paused"], previous
        assert "Printing, Printing from SD, Pausing, or Resuming" in got["message"]

    first = tmp_path / "first-paused"
    first.mkdir()
    opened, _clock = deps(Script(view("Paused")), first)
    assert load(watch(opened, {}, record=True))["events"] == []

    for previous in ("Printing", "Printing from SD", "Pausing", "Paused", "Resuming", "Finishing"):
        folder = tmp_path / f"complete-{previous.replace(' ', '-')}"
        folder.mkdir()
        base, _clock = deps(Script(view(previous, completion=90, filepos=10)), folder)
        watch(base, {}, record=True)
        base.transport = Script(view("Operational", completion=100, filepos=20, left=0))
        assert load(watch(base, {}, record=True))["events"] == ["complete"], previous

    stall = tmp_path / "sd-stall"
    stall.mkdir()
    base, clock = deps(Script(view("Printing from SD", filepos=10, completion=10)), stall, stall_minutes=10)
    watch(base, {}, record=True)
    clock["now"] += 11 * 60
    base.transport = Script(view("Printing from SD", filepos=10, completion=10))
    assert "stalled" in load(watch(base, {}, record=True))["events"]


def test_stall_needs_a_numeric_filepos(tmp_path):
    base, clock = deps(Script(view("Printing", filepos=10, completion=10)), tmp_path, stall_minutes=10)
    watch(base, {}, record=True)
    clock["now"] += 11 * 60
    base.transport = Script(view("Printing", filepos=10, completion=10))
    assert "stalled" in load(watch(base, {}, record=True))["events"]
    base.transport = Script(view("Printing", filepos=None, completion=10))
    fresh = tmp_path / "other"
    fresh.mkdir()
    other, other_clock = deps(Script(view("Printing", filepos=None, completion=10)), fresh, stall_minutes=10)
    watch(other, {}, record=True)
    other_clock["now"] += 11 * 60
    other.transport = Script(view("Printing", filepos=None, completion=10))
    assert load(watch(other, {}, record=True))["events"] == []


def test_repeated_failure_is_quiet_until_cause_changes_or_a_day_passes(tmp_path):
    script = Script([(403, FORBIDDEN)])
    base, clock = deps(script, tmp_path)
    first = load(watch(base, {}, record=True))
    assert first["notify"] is True
    base.transport = Script([(403, FORBIDDEN)])
    second = load(watch(base, {}, record=True))
    assert second["notify"] is False
    base.transport = Script([])

    def boom(method, url, headers, body, timeout, cap):
        raise ApiError("network", "Could not reach OctoPrint (URLError).", "Check OCTOPRINT_URL.")

    base.transport = boom
    changed = load(watch(base, {}, record=True))
    assert changed["notify"] is True
    clock["now"] += 24 * 3600 + 1
    again = load(watch(base, {}, record=True))
    assert again["notify"] is True
    base.transport = Script(view("Operational", completion=None, filepos=None, left=None, name=None))
    recovered = load(watch(base, {}, record=True))
    assert recovered["notify"] is True
    assert recovered["recovered"] is True
    base.transport = Script(view("Operational", completion=None, filepos=None, left=None, name=None))
    assert load(watch(base, {}, record=True))["notify"] is False


def test_watch_arguments_are_not_recorded(tmp_path):
    script = Script(view("Printing"))
    result = load(watch(deps(script, tmp_path)[0], {"nope": True}))
    assert result["notify"] is True
    assert result["error"] == "bad_args"
    assert script.calls == []
    assert not (tmp_path / "watch_failure.json").exists()


def test_corrupt_state_is_not_overwritten(tmp_path):
    path = tmp_path / "watch_state.json"
    path.write_text("{", encoding="utf-8")
    base, _clock = deps(Script(view("Printing")), tmp_path)
    result = load(watch(base, {}, record=True))
    assert result["error"] == "corrupt_state"
    assert path.read_text(encoding="utf-8") == "{"


def test_schedule_refuses_a_schedule_word_as_deliver(tmp_path):
    class Cron:
        def __init__(self):
            self.jobs = [{"id": "old", "name": "print-job-watch", "deliver": "telegram"}]

        def list_jobs(self, include_disabled=True):
            return list(self.jobs)

        def create_job(self, **kwargs):
            raise AssertionError("should not create")

        def remove_job(self, job_id):
            raise AssertionError("should not remove")

    cron = Cron()
    for word in ("every", "5m", "weekdays", "weekends", "monday", "2026-10-08T00:00:00"):
        result = load(schedule(deps(Script([]), tmp_path, cron_module=cron)[0], "5m", word))
        assert result["error"] == "deliver_looks_like_schedule"
    assert cron.jobs[0]["deliver"] == "telegram"
    assert deliver_looks_like_schedule("telegram:123") is False
    assert deliver_looks_like_schedule("slack:C1,telegram") is False
    for word in ("cli", "cron", "api_server", "not-a-platform"):
        refused = load(schedule(deps(Script([]), tmp_path, cron_module=cron)[0], "*/5 * * * *", word))
        assert refused["error"] == "bad_deliver", word
        assert "left as it is" in refused["message"]
    assert cron.jobs[0]["deliver"] == "telegram"
    assert canonical_deliver("origin,all") == "origin,all"
    assert canonical_deliver("bot-chat:desk") == "bot-chat:desk"
    assert canonical_deliver("cli") is None


def test_schedule_names_the_previous_deliver(tmp_path):
    class Cron:
        def __init__(self):
            self.jobs = [{"id": "old", "name": "print-job-watch", "deliver": "telegram"}]

        def list_jobs(self, include_disabled=True):
            return list(self.jobs)

        def create_job(self, **kwargs):
            job = {"id": "new", "name": kwargs["name"], "deliver": kwargs["deliver"], "schedule_display": kwargs["schedule"]}
            self.jobs.append(job)
            return job

        def remove_job(self, job_id):
            self.jobs = [job for job in self.jobs if job["id"] != job_id]

    cron = Cron()
    result = load(schedule(deps(Script([]), tmp_path, cron_module=cron)[0], "*/5 * * * *", "discord"))
    assert result["previous_deliver"] == "telegram"
    assert "telegram" in result["message"]
    assert cron.jobs == [{"id": "new", "name": "print-job-watch", "deliver": "discord", "schedule_display": "*/5 * * * *"}]


def test_fast_schedule_is_refused(tmp_path):
    class Cron:
        def __init__(self):
            self.created = 0

        def create_job(self, **kwargs):
            self.created += 1
            return {"id": "job", "deliver": kwargs["deliver"], "schedule_display": kwargs["schedule"]}

        def list_jobs(self, include_disabled=True):
            return []

    cron = Cron()
    base = deps(Script([]), tmp_path, cron_module=cron)[0]
    assert load(schedule(base, "* * * * *", "local"))["error"] == "schedule_too_fast"
    assert load(schedule(base, "every 1m", "local"))["error"] == "schedule_too_fast"
    assert load(schedule(base, "every monday 9am", "local"))["error"] == "schedule_too_fast"
    impossible = load(schedule(base, "0 0 31 2 *", "local"))
    assert impossible["error"] == "schedule_too_fast"
    assert "could not prove" in impossible["message"]
    assert cron.created == 0
    assert load(schedule(base, "0 0 1 1 *", "local"))["ok"] is True
    assert load(schedule(base, "*/5 * * * *", "local"))["ok"] is True
    assert cron.created == 2


def test_snapshot_rejects_another_host_and_settings(tmp_path):
    script = Script(view("Printing") + [(200, b"\xff\xd8\xff\xd9")])
    result = load(status(deps(script, tmp_path, snapshot_url="http://evil.example/snap.jpg")[0], {"include_snapshot": True}))
    assert result["snapshot"]["ok"] is False
    assert not any("evil.example" in call["url"] for call in script.calls)
    assert all("/api/settings" not in call["url"] for call in script.calls)


def test_snapshot_allows_same_host_other_port(tmp_path):
    script = Script(view("Printing") + [(200, b"\xff\xd8\xffhello")])
    result = load(status(deps(script, tmp_path, snapshot_url="http://printer.example:8080/?action=snapshot")[0], {"include_snapshot": True}))
    assert result["snapshot"]["ok"] is True
    snaps = [call for call in script.calls if ":8080/" in call["url"]]
    assert snaps
    assert "X-Api-Key" not in snaps[0]["header_names"]


def test_gcode_action_is_refused_without_http(tmp_path):
    for action in ("gcode", "start", "toggle", "restart"):
        script = Script(view("Printing"))
        result = load(control(deps(script, tmp_path, blocker=gates.motion_block_reason)[0], {"action": action}))
        assert result["moved"] is False, action
        assert script.calls == [], action
    script = Script(view("Printing"))
    result = load(control(deps(script, tmp_path)[0], {"action": "pause", "temperature": 200}))
    assert result["error"] == "bad_args"
    assert script.calls == []


def test_reason_fits_boundary():
    assert gates.reason_fits("a" * 300)
    assert not gates.reason_fits("a" * 301)
    assert not gates.reason_fits("&" * 61)


def test_null_completion_is_written_as_null(tmp_path):
    script = Script(view("Operational", completion=None, filepos=None, left=None, name=None))
    result = load(status(deps(script, tmp_path)[0], {}))
    assert "completion null" in result["message"]
    assert "None" not in result["message"]


def test_pause_during_resuming_is_not_sent(tmp_path):
    for state in ("Resuming", "Starting", "Starting print from SD", "Starting to send file to SD"):
        script = Script(view(state))
        result = load(control(deps(script, tmp_path)[0], {"action": "pause"}))
        assert result["moved"] is False, state
        assert "still going" in result["message"], state
        assert "does not pause" in result["message"], state
        assert not any(call["method"] == "POST" for call in script.calls)


def test_resume_to_printing_from_sd_is_not_called_finished(tmp_path):
    script = Script(view("Paused") + view("Paused") + [(204, None)] + view("Printing from SD"))
    result = load(control(deps(script, tmp_path, control_timeout=0)[0], {"action": "resume"}))
    assert result["accepted"] is True
    assert result["moved"] is False
    assert "does not call that a finished resume" in result["message"]
    assert "may already be going" in result["message"]


def test_printing_from_sd_can_be_paused(tmp_path):
    script = Script(view("Printing from SD") + view("Printing from SD") + [(204, None)] + view("Paused"))
    result = load(control(deps(script, tmp_path, control_timeout=5)[0], {"action": "pause"}))
    assert result["moved"] is True
    assert len([call for call in script.calls if call["method"] == "POST"]) == 1


def test_state_change_after_approval_is_not_posted(tmp_path):
    script = Script(view("Printing") + view("Paused"))
    result = load(control(deps(script, tmp_path)[0], {"action": "pause"}))
    assert result["moved"] is False
    assert not any(call["method"] == "POST" for call in script.calls)


def test_watch_that_starts_during_cancelling_reports_the_settle(tmp_path):
    base, _clock = deps(Script(view("Cancelling", completion=None, filepos=None, left=None)), tmp_path)
    assert load(watch(base, {}, record=True))["events"] == []
    base.transport = Script(view("Operational", completion=40, filepos=None, left=None))
    assert load(watch(base, {}, record=True))["events"] == ["cancelled"]
    other, _clock = deps(Script(view("Cancelling", completion=None, filepos=None, left=None)), tmp_path / "null")
    assert load(watch(other, {}, record=True))["events"] == []
    other.transport = Script(view("Operational", completion=None, filepos=None, left=None, name=None))
    assert load(watch(other, {}, record=True))["events"] == ["cancelled"]


def test_same_origin_snapshot_sends_the_key_and_another_port_does_not(tmp_path):
    script = Script(view("Printing") + [(200, b"\xff\xd8\xff\xd9")])
    result = load(status(deps(script, tmp_path, snapshot_url="http://printer.example:5000/snap.jpg")[0], {"include_snapshot": True}))
    assert result["snapshot"]["ok"] is True
    snaps = [call for call in script.calls if call["url"].endswith("/snap.jpg")]
    assert snaps and "X-Api-Key" in snaps[0]["header_names"]


_HELPERS = (
    ("tools.approval_context", "_is_cron_approval_context"),
    ("tools.approval", "_yolo_active"),
    ("tools.approval_context", "_get_approval_mode"),
    ("tools.approval_context", "_is_single_query_approval_context"),
    ("tools.approval_context", "_is_unattended_platform_approval_context"),
)


def _quiet_loader(module, name):
    if name == "_get_approval_mode":
        return "ok", lambda: "manual"
    return "ok", lambda: False


def test_each_motion_helper_fails_closed(monkeypatch):
    monkeypatch.delenv("HERMES_PLUGIN_HOST_PROCESS", raising=False)

    def boom():
        raise RuntimeError("boom")

    for module, name in _HELPERS:
        for status_name in ("missing", "failed"):
            def loader(found_module, found_name, module=module, name=name, status_name=status_name):
                if (found_module, found_name) == (module, name):
                    return status_name, None
                return _quiet_loader(found_module, found_name)

            monkeypatch.setattr(gates, "_load", loader)
            reason = gates.motion_block_reason("pause")
            assert reason is not None and reason.startswith("BLOCKED"), (module, name, status_name, reason)

        def raising(found_module, found_name, module=module, name=name):
            if (found_module, found_name) == (module, name):
                return "ok", boom
            return _quiet_loader(found_module, found_name)

        monkeypatch.setattr(gates, "_load", raising)
        reason = gates.motion_block_reason("pause")
        assert reason is not None and reason.startswith("BLOCKED"), (module, name, reason)


def test_approval_and_write_guard_fail_closed(monkeypatch):
    monkeypatch.delenv("HERMES_PLUGIN_HOST_PROCESS", raising=False)

    def loader(module, name):
        if name == "request_tool_approval":
            return "missing", None
        return _quiet_loader(module, name)

    monkeypatch.setattr(gates, "_load", loader)
    approved, message, _rule = gates.request_motion_approval("pause", ORIGIN)
    assert approved is False
    assert "could not be loaded" in message

    def explode(*_args, **_kwargs):
        raise RuntimeError("boom")

    def raising(module, name):
        if name == "request_tool_approval":
            return "ok", explode
        return _quiet_loader(module, name)

    monkeypatch.setattr(gates, "_load", raising)
    approved, message, _rule = gates.request_motion_approval("pause", ORIGIN)
    assert approved is False
    assert "failed" in message

    monkeypatch.setattr(gates, "_load", lambda module, name: ("missing", None))
    assert gates.write_guard_error("/tmp/still.jpg").startswith("BLOCKED")

    def guard_boom(_path):
        raise RuntimeError("boom")

    monkeypatch.setattr(gates, "_load", lambda module, name: ("ok", guard_boom))
    assert gates.write_guard_error("/tmp/still.jpg").startswith("BLOCKED")


def test_bad_port_is_bad_url(tmp_path):
    for url in ("http://h:99999", "http://h:abc", "http://[::1", "http://[zz]:80"):
        with pytest.raises(ApiError) as caught:
            parse_origin(url)
        assert caught.value.code == "bad_url"
    base, _clock = deps(Script([]), tmp_path, url="http://h:99999")
    first = load(watch(base, {}, record=True))
    assert first["error"] == "bad_url"
    assert first["notify"] is True
    second = load(watch(base, {}, record=True))
    assert second["error"] == "bad_url"
    assert second["notify"] is False


def test_approval_text_is_json_not_a_python_dict():
    text = gates.approval_text("pause", ORIGIN)
    assert '{"action":"pause","command":"pause"}' in text
    assert "{'command'" not in text


def test_post_disconnect_says_the_command_may_have_arrived(tmp_path):
    import http.client

    class Drop(Script):
        def __call__(self, method, url, headers, body, timeout, cap):
            if method == "POST":
                raise http.client.RemoteDisconnected("closed")
            return super().__call__(method, url, headers, body, timeout, cap)

    script = Drop(view("Printing") + view("Printing"))
    result = load(control(deps(script, tmp_path)[0], {"action": "pause"}))
    assert result["error"] == "disconnected"
    assert "may still have reached" in result["next_step"]
    assert "Could not reach" not in result["message"]


def test_default_watch_records_only_on_a_cron_turn(tmp_path, monkeypatch):
    """The tool path: cron, then a non-cron call, then cron. The middle call must not consume the notice."""
    base, _clock = deps(Script(view("Printing")), tmp_path)
    monkeypatch.setattr("service._is_cron_turn", lambda: True)
    assert load(watch(base, {}))["events"] == []
    saved = (tmp_path / "watch_state.json").read_text()
    down = view("Offline", connection_state="Closed", printer_status=409, completion=None, filepos=None, left=None)
    base.transport = Script(down)
    monkeypatch.setattr("service._is_cron_turn", lambda: False)
    manual = load(watch(base, {}))
    assert manual["events"] == ["printer_disconnected"]
    assert "did not update" in manual["message"]
    assert "Marked reported" not in manual["message"]
    assert (tmp_path / "watch_state.json").read_text() == saved
    base.transport = Script(down)
    monkeypatch.setattr("service._is_cron_turn", lambda: True)
    again = load(watch(base, {}))
    assert again["events"] == ["printer_disconnected"]
    assert "Marked reported" in again["message"]
    assert "did not update" not in again["message"]


def test_manual_watch_leaves_the_cron_event(tmp_path):
    """Order is cron, then a manual check, then cron. The manual check must not consume the notice."""
    base, _clock = deps(Script(view("Printing")), tmp_path)
    assert load(watch(base, {}, record=True))["events"] == []
    saved = (tmp_path / "watch_state.json").read_text()
    down = view("Offline", connection_state="Closed", printer_status=409, completion=None, filepos=None, left=None)
    base.transport = Script(down)
    manual = load(watch(base, {}, record=False))
    assert manual["events"] == ["printer_disconnected"]
    assert manual["recorded"] is False
    assert "did not update" in manual["message"]
    assert "Marked reported" not in manual["message"]
    assert (tmp_path / "watch_state.json").read_text() == saved
    assert not (tmp_path / "watch_failure.json").exists()
    base.transport = Script(down)
    again = load(watch(base, {}, record=True))
    assert again["events"] == ["printer_disconnected"]
    assert again["notify"] is True
    assert "Marked reported" in again["message"]
    assert "did not update" not in again["message"]


def test_redirect_to_another_host_does_not_receive_the_key():
    import socket
    import threading
    import http.server

    try:
        socket.create_connection(("127.0.0.1", 1), timeout=0.2)
    except RuntimeError:
        pytest.skip("socket connect is blocked")
    except OSError:
        pass

    seen: list[str | None] = []

    class Steal(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers.get("X-Api-Key"))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"stolen": true}')

        def log_message(self, *_args):
            pass

    class Bounce(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", Bounce.location)
            self.end_headers()

        def log_message(self, *_args):
            pass

    steal = http.server.HTTPServer(("127.0.0.1", 0), Steal)
    Bounce.location = f"http://localhost:{steal.server_address[1]}/stolen"
    bounce = http.server.HTTPServer(("127.0.0.1", 0), Bounce)
    for server in (steal, bounce):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        from client import OctoPrint

        client = OctoPrint(parse_origin(f"http://127.0.0.1:{bounce.server_address[1]}"), SECRET)
        with pytest.raises(ApiError) as caught:
            client.get_json("/api/job")
        assert caught.value.code == "redirect"
        assert seen == []
    finally:
        bounce.shutdown()
        steal.shutdown()
        bounce.server_close()
        steal.server_close()


def test_printer_409_while_printing_is_not_disconnect_or_error(tmp_path):
    script = Script(view("Printing", printer_status=409))
    result = load(status(deps(script, tmp_path)[0], {}))
    assert result["kind"] == "printing"
    assert result["kind"] not in {"disconnected", "error"}
    assert result["temperatures"] is None
    assert result["ok"] is True


def test_empty_key_settings_and_key_are_not_sent(tmp_path):
    script = Script(view("Printing"))
    result = load(status(deps(script, tmp_path, api_key="  ")[0], {}))
    assert result["error"] == "no_key"
    assert script.calls == []
    client = OctoPrint(parse_origin(ORIGIN), SECRET, Script([]))
    with pytest.raises(ApiError) as caught:
        client.get_json("/api/settings")
    assert caught.value.code == "settings_blocked"
    assert client.transport.calls == []
    script = Script([(400, {"error": f"refused {SECRET}"})])
    result = load(status(deps(script, tmp_path)[0], {}))
    assert SECRET not in json.dumps(result)
    assert all(SECRET not in call["url"] for call in script.calls)
    assert "X-Api-Key" in script.calls[0]["header_names"]


def test_forbidden_failure_file_keeps_only_the_code(tmp_path):
    script = Script([(403, {"error": PERMISSION_ERROR + " " + SECRET})])
    result = load(watch(deps(script, tmp_path)[0], {}, record=True))
    assert result["error"] == "unauthorized"
    assert SECRET not in json.dumps(result)
    saved = json.loads((tmp_path / "watch_failure.json").read_text(encoding="utf-8"))
    assert saved["code"] == "unauthorized"
    assert "job_state" not in saved
    assert SECRET not in json.dumps(saved)
    assert "Printing" not in json.dumps(saved)


def test_html_502_keeps_the_status(tmp_path):
    html = b"<html>bad gateway</html>"
    script = Script([(502, html)])
    got = load(status(deps(script, tmp_path)[0], {}))
    assert got["error"] == "server"
    assert "502" in got["message"]
    assert "If this was a move" not in got["message"]
    assert "If this was a move" not in got["next_step"]
    assert "may still have reached" not in got["next_step"]
    posted = Script(view("Printing") + view("Printing") + [(502, html)])
    moved = load(control(deps(posted, tmp_path)[0], {"action": "pause"}))
    assert moved["error"] == "server"
    assert "502" in moved["message"]
    assert "may still have reached" in moved["next_step"]
    assert "Could not reach" not in moved["message"]
    assert "bad_body" != moved["error"]


def test_post_cut_off_on_a_real_server_may_have_reached(tmp_path):
    """POST faults from a real http.server, not a stand-in transport."""
    import socket
    import threading
    import http.server

    try:
        socket.create_connection(("127.0.0.1", 1), timeout=0.2)
    except RuntimeError:
        pytest.skip("socket connect is blocked")
    except OSError:
        pass

    pages = {
        "/api/connection": b'{"current":{"state":"Operational"}}',
        "/api/job": (
            b'{"job":{"file":{"name":"tiny.gcode"}},'
            b'"progress":{"completion":10,"filepos":100,"printTimeLeft":0},"state":"Printing"}'
        ),
        "/api/printer": b'{"temperature":{"tool0":{"actual":21.0,"target":0.0}},"state":{"text":"Printing"}}',
    }

    def handler_for(mode):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = pages.get(self.path.split("?", 1)[0], b"{}")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0") or 0)
                if length:
                    self.rfile.read(length)
                if mode == "short_5xx":
                    raw = (
                        b"HTTP/1.1 502 Bad Gateway\r\nContent-Type: text/html\r\n"
                        b"Content-Length: 100\r\nConnection: close\r\n\r\n<html>502</html>"
                    )
                elif mode == "short_length":
                    raw = (
                        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                        b"Content-Length: 40\r\nConnection: close\r\n\r\n{}"
                    )
                elif mode == "chunk_cut":
                    raw = (
                        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n"
                        b"Connection: close\r\n\r\n5\r\nhello\r\n"
                    )
                else:
                    self.close_connection = True
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    return
                self.connection.sendall(raw)
                self.close_connection = True
                try:
                    self.connection.shutdown(socket.SHUT_WR)
                except OSError:
                    pass

            def log_message(self, *_args):
                pass

        return Handler

    def run(mode):
        server = http.server.HTTPServer(("127.0.0.1", 0), handler_for(mode))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            origin = f"http://127.0.0.1:{server.server_address[1]}"
            base, _clock = deps(Script([]), tmp_path / mode, url=origin, transport=None, control_timeout=5)
            return load(control(base, {"action": "pause"}))
        finally:
            server.shutdown()
            server.server_close()

    short_5xx = run("short_5xx")
    assert short_5xx["error"] == "server"
    assert "502" in short_5xx["message"]
    assert short_5xx["error"] != "short_body"
    short_length = run("short_length")
    assert short_length["error"] == "short_body"
    chunk_cut = run("chunk_cut")
    assert chunk_cut["error"] == "disconnected"
    dropped = run("drop_after_send")
    assert dropped["error"] == "disconnected"
    for name, result in (
        ("short_5xx", short_5xx),
        ("short_length", short_length),
        ("chunk_cut", chunk_cut),
        ("drop_after_send", dropped),
    ):
        assert "may still have reached" in result["next_step"], name
        assert "Nothing was saved." not in result["next_step"], name
        assert "Nothing was saved." not in result["message"], name
        assert "Could not reach" not in result["message"], name
        assert "Check OCTOPRINT_URL" not in result["message"], name
        assert "Check OCTOPRINT_URL" not in result["next_step"], name


def test_read_timeout_on_a_real_server_does_not_say_the_command_arrived():
    """A hang before any status line. A read saved nothing. A control POST may have."""
    import socket
    import threading
    import http.server

    try:
        socket.create_connection(("127.0.0.1", 1), timeout=0.2)
    except RuntimeError:
        pytest.skip("socket connect is blocked")
    except OSError:
        pass

    def hang(method):
        release = threading.Event()

        class Handler(http.server.BaseHTTPRequestHandler):
            def _hold(self):
                length = int(self.headers.get("Content-Length", "0") or 0)
                if length:
                    self.rfile.read(length)
                release.wait(5)

            do_GET = _hold
            do_POST = _hold

            def log_message(self, *_args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            origin = parse_origin(f"http://127.0.0.1:{server.server_address[1]}")
            client = OctoPrint(origin, SECRET)
            with pytest.raises(ApiError) as caught:
                if method == "GET":
                    client.get_json("/api/job", timeout=0.4)
                elif method == "STILL":
                    client.get_bytes(origin.value + "/webcam/?action=snapshot", 1000, timeout=0.4)
                else:
                    client.post_json("/api/job", {"command": "pause", "action": "pause"}, 0.4)
            return caught.value
        finally:
            release.set()
            server.shutdown()
            server.server_close()

    read = hang("GET")
    assert read.code == "timeout"
    assert "may still have reached" not in read.next_step
    assert "Nothing was saved." in read.next_step
    assert "Could not reach" not in read.message
    still = hang("STILL")
    assert still.code == "timeout"
    assert "may still have reached" not in still.next_step
    assert "Nothing was saved." in still.next_step
    posted = hang("POST")
    assert posted.code == "timeout"
    assert "may still have reached" in posted.next_step
    assert "Could not reach" not in posted.message


def test_accept_waits_until_60_seconds(tmp_path):
    hold = view("Pausing") * 80
    script = Script(view("Printing") + view("Printing") + [(204, None)] + hold)
    base, clock = deps(script, tmp_path, control_timeout=60)
    result = load(control(base, {"action": "pause"}))
    assert result["accepted"] is True
    assert result["moved"] is False
    assert result["job_state"] == "Pausing"
    assert "not a failure" in result["message"]
    assert clock["now"] - 1_000_000 >= 60
    posts = [call for call in script.calls if call["method"] == "POST"]
    assert len(posts) == 1


def test_sd_transfer_pause_is_not_already(tmp_path):
    for state in ("Sending file to SD", "Transferring file to SD"):
        script = Script(view(state))
        result = load(control(deps(script, tmp_path)[0], {"action": "pause"}))
        assert result["moved"] is False, state
        assert not any(call["method"] == "POST" for call in script.calls), state
        assert "already" not in result["message"], state
        assert "Nothing was sent" in result["message"], state
        assert "transfer" in result["message"].lower(), state


def test_idle_pause_resume_and_starting_cancel(tmp_path):
    for action in ("pause", "resume"):
        script = Script(view("Operational", completion=None, filepos=None, left=None, name=None))
        result = load(control(deps(script, tmp_path)[0], {"action": action}))
        assert not any(call["method"] == "POST" for call in script.calls), action
        assert "not printing" in result["message"], action
        assert "Nothing was sent" in result["message"], action
        assert "already" not in result["message"], action
    script = Script(view("Operational", completion=None, filepos=None, left=None, name=None))
    result = load(control(deps(script, tmp_path)[0], {"action": "cancel"}))
    assert result["message"] == "State is already Operational. Nothing was sent."
    assert not any(call["method"] == "POST" for call in script.calls)
    script = Script(view("Starting print from SD"))
    result = load(control(deps(script, tmp_path)[0], {"action": "cancel"}))
    assert not any(call["method"] == "POST" for call in script.calls)
    assert "cancel" in result["message"].lower()
    assert "already" not in result["message"]
    assert "pause" not in result["message"].lower()
    assert "pause" not in result["next_step"].lower()


def test_first_sample_disconnect_is_not_paused(tmp_path):
    script = Script(view("Offline", connection_state="Closed", printer_status=409, completion=None, filepos=None, left=None, name=None))
    result = load(watch(deps(script, tmp_path)[0], {}, record=True))
    assert result["events"] == ["printer_disconnected"]
    assert "paused" not in result["events"]
    saved = json.loads((tmp_path / "watch_state.json").read_text(encoding="utf-8"))
    assert SECRET not in json.dumps(saved)


def test_host_and_unreadable_cron_helper_cannot_watch(tmp_path, monkeypatch):
    import sys
    import types

    monkeypatch.delenv("HERMES_PLUGIN_HOST_PROCESS", raising=False)
    script = Script(view("Paused"))
    monkeypatch.setenv("HERMES_PLUGIN_HOST_PROCESS", "1")
    result = load(watch(deps(script, tmp_path)[0], {}))
    assert result["notify"] is True
    assert result["message"] == CANNOT_WATCH
    assert "Nothing new" not in result["message"]
    assert result["events"] == []
    assert script.calls == []
    assert not (tmp_path / "watch_state.json").exists()

    monkeypatch.setenv("HERMES_PLUGIN_HOST_PROCESS", "0")
    mod = types.ModuleType("tools.approval_context")
    mod._is_cron_approval_context = lambda: False
    monkeypatch.setitem(sys.modules, "tools.approval_context", mod)
    script = Script(view("Printing"))
    manual = load(watch(deps(script, tmp_path / "manual")[0], {}))
    assert manual["ok"] is True
    assert manual.get("error") is None
    assert script.calls

    monkeypatch.delenv("HERMES_PLUGIN_HOST_PROCESS", raising=False)
    missing = types.ModuleType("tools.approval_context")
    monkeypatch.setitem(sys.modules, "tools.approval_context", missing)
    script = Script(view("Paused"))
    renamed = load(watch(deps(script, tmp_path / "renamed")[0], {}))
    assert renamed["message"] == CANNOT_WATCH
    assert renamed["notify"] is True
    assert "Nothing new" not in renamed["message"]
    assert script.calls == []
    assert not (tmp_path / "renamed" / "watch_state.json").exists()

    def boom():
        raise RuntimeError("boom")

    broken = types.ModuleType("tools.approval_context")
    broken._is_cron_approval_context = boom
    monkeypatch.setitem(sys.modules, "tools.approval_context", broken)
    script = Script(view("Paused"))
    failed = load(watch(deps(script, tmp_path / "boom")[0], {}))
    assert failed["message"] == CANNOT_WATCH
    assert script.calls == []


def test_non_default_profile_prints_p_flag(tmp_path, monkeypatch):
    monkeypatch.setattr("service.active_profile_name", lambda: "desk")

    class Jobs:
        def __init__(self):
            self.jobs = []

        def list_jobs(self, include_disabled=True):
            return list(self.jobs)

        def create_job(self, **kwargs):
            job = {"id": "new", "name": "print-job-watch", "deliver": kwargs["deliver"], "schedule_display": kwargs["schedule"]}
            self.jobs = [job]
            return job

        def remove_job(self, job_id):
            self.jobs = [job for job in self.jobs if job["id"] != job_id]

    base, _clock = deps(Script([]), tmp_path, cron_module=Jobs())
    made = load(schedule(base, "*/5 * * * *", "local"))
    assert "hermes -p desk cron list" in made["message"]
    assert "hermes -p desk print-job-watch unschedule" in made["message"]
    assert "--profile" not in made["message"]
    refused = load(schedule(base, "*/5 * * * *", "5m"))
    assert "hermes -p desk print-job-watch schedule --deliver" in refused["next_step"]
    assert "Put the delivery target first" not in refused["message"]
    assert "Put the delivery target first" not in refused["next_step"]
    removed = load(unschedule(base))
    assert removed["ok"] is True
    assert "hermes -p desk plugins remove" in removed["message"]
    assert "--profile" not in removed["message"]
    bare = Deps(url=ORIGIN, api_key=SECRET, data_dir=tmp_path / "bare", now=lambda: 0, sleep=lambda _s: None, write_guard=lambda _p: None)
    missing = load(schedule(bare, "*/5 * * * *", "local"))
    assert "hermes -p desk cron create" in missing["next_step"]


def test_register_does_not_import_tests():
    text = Path(__file__).resolve().parents[1].joinpath("__init__.py").read_text(encoding="utf-8")
    assert "import tests" not in text
    assert "from tests" not in text


def test_public_text_uses_the_same_sentences():
    root = Path(__file__).resolve().parents[1]
    files = [
        root / "README.md",
        root / "plugin.yaml",
        root / "__init__.py",
    ]
    ship = None
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "ship" / "octoprint-print-watch"
        if (candidate / "pr_body.md").is_file() and (candidate / "print-job-watch.yaml").is_file():
            ship = candidate
            break
    if ship is not None:
        files.extend([
            ship / "pr_body.md",
            ship / "print-job-watch.yaml",
        ])
    complete = "Printing, Printing from SD, Pausing, Paused, Resuming, or Finishing"
    paused = "Printing, Printing from SD, Pausing, or Resuming"
    watch_job = "The watch is a cron job (a model turn every 5 minutes by default) that stays after uninstall until you run unschedule."
    for path in files:
        text = " ".join(path.read_text(encoding="utf-8").split())
        assert CANNOT_WATCH in text, path.name
        assert "can be used with OctoPrint" in text or path.name == "__init__.py"
        assert "Each move asks and adds one unused line" not in text
        assert "reported on every" not in text
        assert "reported every run" not in text
        assert "Put the delivery target first" not in text
        assert "model-free" not in text
        if path.name in {"README.md", "plugin.yaml", "pr_body.md", "__init__.py", "print-job-watch.yaml"}:
            assert complete in text, path.name
            assert paused in text, path.name
        if path.name == "print-job-watch.yaml":
            raw = path.read_text(encoding="utf-8")
            start = raw.index('description: "') + len('description: "')
            description = raw[start:raw.index('"\n', start)]
            assert watch_job in description
            assert "Complete is after" not in description
            assert len(description) <= 1500
        if path.name == "README.md":
            assert "not sent again" in text or "does not send a cron event again" in text
            assert "Printing or Printing from SD" in text
            assert "not loaded by `register()`" in text
        if path.name == "__init__.py":
            assert "single-query" in text
