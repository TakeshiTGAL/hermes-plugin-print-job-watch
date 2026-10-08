"""Status, approved pause/resume/cancel, and a watch that does not move the printer."""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

if __package__:
    from .classify import PRINTING_STATES, events_for, kind_of, next_record
    from .client import (
        CONTROL_TIMEOUT_SECONDS,
        JSON_MAX_BYTES,
        JSON_TIMEOUT_SECONDS,
        MAY_HAVE_REACHED,
        SNAPSHOT_TIMEOUT_SECONDS,
        ApiError,
        OctoPrint,
        parse_origin,
    )
    from .gates import (
        BODIES,
        approval_text,
        in_plugin_host_process,
        motion_block_reason,
        plugin_data_dir,
        reason_fits,
        request_motion_approval,
        write_guard_error,
    )
else:
    from classify import PRINTING_STATES, events_for, kind_of, next_record
    from client import (
        CONTROL_TIMEOUT_SECONDS,
        JSON_MAX_BYTES,
        JSON_TIMEOUT_SECONDS,
        MAY_HAVE_REACHED,
        SNAPSHOT_TIMEOUT_SECONDS,
        ApiError,
        OctoPrint,
        parse_origin,
    )
    from gates import (
        BODIES,
        approval_text,
        in_plugin_host_process,
        motion_block_reason,
        plugin_data_dir,
        reason_fits,
        request_motion_approval,
        write_guard_error,
    )

TOOLSET = "print_job_watch"
JOB_NAME = "print-job-watch"
DEFAULT_SCHEDULE = "*/5 * * * *"
STATE_FILE = "watch_state.json"
FAILURE_FILE = "watch_failure.json"
SNAPSHOT_KEEP = 20
STALL_LO, STALL_HI = 1, 240
SNAPSHOT_LO, SNAPSHOT_HI = 1, 5_000_000
FAILURE_REMIND_SECONDS = 24 * 3600
STILL_NAME = re.compile(r"still-[0-9]+-[0-9]+\.(jpg|png)\Z")
CANNOT_WATCH = "Under plugins.isolation: host, this form cannot watch the printer. Set plugins.isolation to in_process."
_PROFILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
CRON_PROMPT = (
    "Call the print_job_watch tool with no arguments. "
    "Do not call print_job_control. Do not pause, resume, cancel, change a temperature, or send G-code. "
    "If the tool result has notify true, reply with its message field and nothing else. "
    "If notify is false, reply with [SILENT]. "
    "If the tool cannot be called or returns no notify field, reply with: "
    "print_job_watch did not run, so the printer was not checked."
)


def dumps(body: dict[str, Any]) -> str:
    return json.dumps(body, ensure_ascii=True, sort_keys=True)


def active_profile_name() -> str:
    """Hermes profile for printed commands. default when the helper cannot be read."""
    try:
        from hermes_cli.profiles import get_active_profile_name
        name = get_active_profile_name()
    except Exception:
        return "default"
    if not isinstance(name, str) or not _PROFILE_NAME.fullmatch(name.strip()):
        return "default"
    return name.strip() or "default"


def hermes_line(command: str) -> str:
    """A hermes command. Adds -p when the profile is not default. Never uses --profile."""
    name = active_profile_name()
    if name != "default":
        return f"hermes -p {name} {command}"
    return f"hermes {command}"


def fail(code: str, message: str, next_step: str, **extra: Any) -> str:
    body = {"ok": False, "error": code, "message": message, "next_step": next_step, "moved": False}
    body.update(extra)
    return dumps(body)


@dataclass
class Deps:
    url: str
    api_key: str = ""
    snapshot_url: str = ""
    data_dir: Path | None = None
    stall_minutes: int = 10
    snapshot_max_bytes: int = 2_000_000
    now: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    transport: Any = None
    approver: Callable[[str, str], tuple[bool, str]] | None = None
    blocker: Callable[[str], str | None] | None = None
    write_guard: Callable[[str], str | None] | None = None
    cron_module: Any = None
    control_timeout: float = CONTROL_TIMEOUT_SECONDS


def _clamp(value: Any, lo: int, hi: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, number))


def deps_from_config(get_config: Callable[[], Any], url: str, api_key: str, snapshot_url: str = "") -> Deps:
    raw = {}
    try:
        loaded = get_config() or {}
        if isinstance(loaded, dict):
            raw = loaded
    except Exception:
        raw = {}
    try:
        data = plugin_data_dir()
    except Exception:
        data = None
    return Deps(
        url=url,
        api_key=api_key,
        snapshot_url=snapshot_url,
        data_dir=data,
        stall_minutes=_clamp(raw.get("stall_minutes", 10), STALL_LO, STALL_HI, 10),
        snapshot_max_bytes=_clamp(raw.get("snapshot_max_bytes", 2_000_000), SNAPSHOT_LO, SNAPSHOT_HI, 2_000_000),
    )


def _client(deps: Deps) -> OctoPrint:
    if not deps.url.strip():
        raise ApiError(
            "bad_url",
            "OCTOPRINT_URL is empty, so nothing was requested.",
            "Set OCTOPRINT_URL to one origin, for example http://printer.example:5000.",
        )
    return OctoPrint(parse_origin(deps.url), deps.api_key, deps.transport)


def _sample_from(connection: Any, job: Any, temperatures: Any) -> dict[str, Any]:
    if not isinstance(connection, dict) or not isinstance(connection.get("current"), dict):
        raise ApiError("bad_body", "OctoPrint connection JSON had no current state.", "Nothing was saved.")
    if not isinstance(job, dict) or "state" not in job or "progress" not in job or "job" not in job:
        raise ApiError("bad_body", "OctoPrint job JSON had no job, progress, and state.", "Nothing was saved.")
    progress = job.get("progress") if isinstance(job.get("progress"), dict) else {}
    info = job.get("job") if isinstance(job.get("job"), dict) else {}
    fileinfo = info.get("file") if isinstance(info.get("file"), dict) else {}
    left = progress.get("printTimeLeft")
    if isinstance(left, bool) or not isinstance(left, (int, float)):
        left = None
    completion = progress.get("completion")
    if isinstance(completion, bool) or not isinstance(completion, (int, float)):
        completion = None
    filepos = progress.get("filepos")
    if isinstance(filepos, bool) or not isinstance(filepos, (int, float)):
        filepos = None
    error_text = job.get("error") if isinstance(job.get("error"), str) else ""
    temps = temperatures if isinstance(temperatures, dict) else None
    return {
        "job_state": str(job.get("state") or ""),
        "connection_state": str(connection["current"].get("state") or ""),
        "filename": fileinfo.get("name") if isinstance(fileinfo.get("name"), str) else None,
        "completion": completion,
        "filepos": filepos,
        "print_time_left": left,
        "error_text": error_text,
        "temperatures": _temps(temps),
    }


def _temps(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    if not raw:
        return None
    picked = {}
    for name in ("tool0", "bed"):
        item = raw.get(name)
        if isinstance(item, dict):
            picked[name] = {"actual": item.get("actual"), "target": item.get("target")}
    return picked or None


def read_sample(client: OctoPrint) -> dict[str, Any]:
    _status, connection = client.get_json("/api/connection")
    _status, job = client.get_json("/api/job")
    temperatures = None
    try:
        _status, printer = client.get_json("/api/printer")
    except ApiError as error:
        if error.code != "conflict":
            raise
        printer = None
    if isinstance(printer, dict):
        temperatures = printer.get("temperature")
    sample = _sample_from(connection, job, temperatures)
    sample["kind"] = kind_of(sample["job_state"], sample["connection_state"])
    return sample


def _guard(deps: Deps) -> Callable[[str], str | None]:
    return deps.write_guard or write_guard_error


def status(deps: Deps, args: dict[str, Any] | None) -> str:
    args = args or {}
    if any(key not in {"include_snapshot"} for key in args):
        return fail("bad_args", "print_job_status accepts only include_snapshot.", "Drop the other arguments. The URL cannot be changed from here.")
    try:
        sample = read_sample(_client(deps))
    except ApiError as error:
        return fail(error.code, error.message, error.next_step)
    snapshot = None
    if args.get("include_snapshot") is True:
        snapshot = _save_snapshot(deps, sample)
    message = _status_message(sample)
    return dumps({
        "ok": True,
        "moved": False,
        "message": message,
        "next_step": "Pause, resume, and cancel go through print_job_control, which asks for approval.",
        "kind": sample["kind"],
        "job_state": sample["job_state"],
        "connection_state": sample["connection_state"],
        "filename": sample["filename"],
        "completion": sample["completion"],
        "filepos": sample["filepos"],
        "print_time_left": sample["print_time_left"],
        "temperatures": sample["temperatures"],
        "error_text": sample["error_text"],
        "snapshot": snapshot,
        "limits": {"json_timeout_seconds": JSON_TIMEOUT_SECONDS, "json_max_bytes": JSON_MAX_BYTES, "retries": 0},
    })


def _status_message(sample: dict[str, Any]) -> str:
    kind = sample["kind"]
    if kind == "disconnected":
        return (
            f"The printer is disconnected (connection {sample['connection_state']}, job {sample['job_state']}). "
            "This is not a pause. Temperatures were not read."
        )
    if kind == "error":
        detail = sample["error_text"] or "no error text"
        return f"The printer is in error ({sample['job_state']}: {detail}). This is not a disconnect and not a pause."
    left = sample["print_time_left"]
    left_text = "null" if left is None else str(left)
    return (
        f"Job state {sample['job_state']}, connection {sample['connection_state']}, "
        f"completion {'null' if sample['completion'] is None else sample['completion']}, printTimeLeft {left_text} seconds. "
        "printTimeLeft is OctoPrint's estimate, including 0. A state string is not proof the toolhead moved."
    )


def _send_decision(action: str, sample: dict[str, Any]) -> str:
    job = sample["job_state"]
    kind = sample["kind"]
    if kind in {"disconnected", "error"}:
        return "down"
    if action == "pause":
        if job in PRINTING_STATES:
            return "send"
        if job == "Pausing":
            return "hold"
        if job == "Paused":
            return "already"
        return "refuse"
    if action == "resume":
        if job == "Paused":
            return "send"
        if job == "Pausing":
            return "hold"
        if job in PRINTING_STATES:
            return "already"
        return "refuse"
    if job in PRINTING_STATES or job in {"Paused", "Pausing", "Resuming"}:
        return "send"
    if job == "Cancelling":
        return "hold"
    return "already"


def _not_sent_message(action: str, decision: str, sample: dict[str, Any]) -> str:
    job = sample["job_state"]
    if decision == "down":
        return (
            f"The printer is {sample['kind']} (job {job}, connection {sample['connection_state']}). "
            "Nothing was sent. A 409 from /api/printer is not itself a pause."
        )
    if job == "Pausing":
        return (
            "State is Pausing. Nothing was sent. Pausing is a pause in progress, not a failure. "
            "Do not send pause or resume until a later read shows Paused or Printing. "
            "On OctoPrint 1.11.8, resume runs only when the job is already paused, so resume during Pausing does not continue the print."
        )
    if job == "Cancelling":
        return (
            "State is Cancelling. Nothing was sent. Cancel was already accepted. "
            "Do not send cancel or resume until a later read leaves Cancelling."
        )
    if action == "cancel" and job == "Operational":
        return "State is already Operational. Nothing was sent."
    if action == "cancel" and job.startswith("Starting"):
        return (
            f"State is {job}. Nothing was sent. Cancel was not posted while the job is still starting. "
            "Read the state again before asking for cancel."
        )
    if action == "pause" and (job == "Resuming" or job.startswith("Starting")):
        return (
            f"State is {job}. Nothing was sent. The print is still going. "
            "This does not pause the print."
        )
    if action == "pause" and job in {"Sending file to SD", "Transferring file to SD"}:
        return (
            f"State is {job}. The file transfer is still going, and pause was not sent. Nothing was sent."
        )
    if action in {"pause", "resume"} and job == "Operational":
        return (
            f"State is Operational. The printer is not printing, so {action} was not sent. Nothing was sent."
        )
    if action == "pause" and job == "Paused":
        return "State is already Paused. Nothing was sent."
    return f"State is {job}. Nothing was sent."


def _not_sent(action: str, decision: str, sample: dict[str, Any]) -> str:
    if action == "cancel":
        next_step = "Read print_job_status before asking for cancel again. Nothing was posted."
    elif sample["job_state"] == "Pausing":
        next_step = "Read print_job_status before sending a move. If the state is Pausing, do not send resume."
    else:
        next_step = "Read print_job_status before sending a move."
    return dumps({
        "ok": True,
        "moved": False,
        "accepted": False,
        "message": _not_sent_message(action, decision, sample),
        "next_step": next_step,
        "job_state": sample["job_state"],
        "kind": sample["kind"],
    })


def control(deps: Deps, args: dict[str, Any] | None) -> str:
    args = args or {}
    action = args.get("action")
    if not isinstance(action, str) or set(args) - {"action"}:
        return fail("bad_args", "print_job_control requires action pause, resume, or cancel and no other arguments.", "Do not send G-code or a temperature.")
    blocker = deps.blocker or motion_block_reason
    blocked = blocker(action)
    if blocked:
        return fail("blocked", blocked, "Nothing was sent.")
    try:
        client = _client(deps)
        sample = read_sample(client)
    except ApiError as error:
        return fail(error.code, error.message, error.next_step or "Nothing was sent.")
    decision = _send_decision(action, sample)
    if decision != "send":
        return _not_sent(action, decision, sample)
    origin = ""
    try:
        origin = parse_origin(deps.url).value
    except ApiError:
        origin = deps.url
    text = approval_text(action, origin)
    if not reason_fits(text):
        return fail(
            "approval_cut",
            "The approval question would not fit in one message, so it was not sent and the printer was not moved.",
            "Shorten OCTOPRINT_URL. Nothing was sent.",
        )
    if deps.approver is None:
        approved, message, _rule_key = request_motion_approval(action, origin)
        if not approved:
            return fail("not_approved", message, "Nothing was sent.")
    else:
        rule_key = f"print_job_control:{action}:{uuid.uuid4().hex}"
        try:
            approved, message = deps.approver(text, rule_key)
        except Exception:
            return fail("not_approved", "BLOCKED: the approval request failed, so the printer was not moved.", "Nothing was sent.")
        if not approved:
            return fail("not_approved", message or "BLOCKED: the printer move was not approved.", "Nothing was sent.")
    try:
        sample = read_sample(client)
    except ApiError as error:
        return fail(
            error.code,
            "The move was approved, then the state could not be read again: " + error.message,
            "Nothing was sent. Read the state before sending it again.",
        )
    decision = _send_decision(action, sample)
    if decision != "send":
        return _not_sent(action, decision, sample)
    try:
        status_code, _body = client.post_json("/api/job", BODIES[action], deps.control_timeout)
    except ApiError as error:
        return fail(error.code, error.message, error.next_step or MAY_HAVE_REACHED)
    if status_code != 204:
        return fail("http", f"OctoPrint returned HTTP {status_code} instead of 204, so this was not accepted.", "Read the state before sending it again.")
    return _after_accept(deps, client, action)


def _after_accept(deps: Deps, client: OctoPrint, action: str) -> str:
    deadline = deps.now() + deps.control_timeout
    last = None
    reads = 0
    while reads < 70:
        reads += 1
        try:
            last = read_sample(client)
        except ApiError as error:
            return fail(
                error.code,
                "OctoPrint accepted the command (HTTP 204). A later read failed: " + error.message,
                "The command may already have taken effect. Read the state before sending it again.",
                accepted=True,
            )
        reached = _reached(action, last)
        if reached:
            return dumps({
                "ok": True,
                "moved": True,
                "accepted": True,
                "message": _moved_message(action, last),
                "next_step": "A changed state string is not proof the toolhead followed the command. Read print_job_status if you need to check.",
                "job_state": last["job_state"],
                "kind": last["kind"],
                "completion": last["completion"],
            })
        if last["job_state"] == "Operational" and action == "resume" and isinstance(last.get("completion"), (int, float)) and last["completion"] >= 100:
            return fail(
                "finished",
                "OctoPrint accepted resume (HTTP 204), then the job was Operational with completion 100. The print finished. This is not a successful resume.",
                "Do not send resume again.",
                accepted=True,
                job_state=last["job_state"],
            )
        if deps.now() >= deadline:
            break
        deps.sleep(1)
    return dumps({
        "ok": True,
        "moved": False,
        "accepted": True,
        "message": _progress_message(action, last or {"job_state": "unknown"}),
        "next_step": "Do not send the command again yet. Read the state first. If the state is Pausing, do not send resume.",
        "job_state": None if last is None else last.get("job_state"),
        "kind": None if last is None else last.get("kind"),
    })


def _reached(action: str, sample: dict[str, Any]) -> bool:
    job = sample["job_state"]
    if sample["kind"] in {"disconnected", "error"}:
        return False
    if action == "pause":
        return job == "Paused"
    if action == "resume":
        return job == "Printing"
    return job == "Operational"


def _moved_message(action: str, sample: dict[str, Any]) -> str:
    if action == "cancel":
        return "Cancel was accepted and a later read shows Operational. The print cannot be resumed. This does not prove where the toolhead stopped."
    if action == "pause":
        return "Pause was accepted and a later read shows Paused. HTTP 204 alone was not treated as success."
    return "Resume was accepted and a later read shows Printing. It continues; it was not restarted from the beginning."


def _progress_message(action: str, sample: dict[str, Any]) -> str:
    job = sample["job_state"]
    if job == "Pausing":
        return (
            "OctoPrint accepted the command (HTTP 204). State is still Pausing, which is normal and not a failure. "
            "Do not send pause or resume until a later read shows Paused or Printing."
        )
    if job == "Cancelling":
        return (
            "OctoPrint accepted cancel (HTTP 204). State is still Cancelling, which is not a finished cancel and not a failure. "
            "Do not send cancel or resume until a later read leaves Cancelling."
        )
    if action == "resume" and job == "Printing from SD":
        return (
            "OctoPrint accepted resume (HTTP 204). A later read is Printing from SD, not Printing. "
            "This plugin does not call that a finished resume. The print may already be going. "
            "Read the state before sending resume again."
        )
    return (
        f"OctoPrint accepted the command (HTTP 204) but the state is still {job}. "
        "Do not send it again until you have read the state."
    )


def _is_cron_turn() -> bool | None:
    """True on a visible cron turn, False on a visible manual turn, None when the turn cannot be judged.

    None covers a host child and a cron helper that cannot be imported, raises, or was renamed.
    A readable helper that returns False is manual, not None.
    """
    if in_plugin_host_process():
        return None
    try:
        import importlib
        mod = importlib.import_module("tools.approval_context")
    except Exception:
        return None
    fn = getattr(mod, "_is_cron_approval_context", None)
    if not callable(fn):
        return None
    try:
        value = fn()
    except Exception:
        return None
    if value is True:
        return True
    if value is False:
        return False
    return None


def _cannot_watch() -> str:
    return dumps({
        "ok": False,
        "error": "cannot_watch",
        "moved": False,
        "notify": True,
        "recorded": False,
        "events": [],
        "message": CANNOT_WATCH,
        "next_step": "Set plugins.isolation to in_process. The watch was not saved and the printer was not moved.",
    })


def watch(deps: Deps, args: dict[str, Any] | None, *, record: bool | None = None) -> str:
    if args:
        return fail(
            "bad_args",
            "print_job_watch takes no arguments. This was a wrong call, not a printer failure.",
            "Call print_job_watch with an empty object. Nothing was checked and nothing was recorded.",
            notify=True,
        )
    if record is None:
        cron = _is_cron_turn()
        if cron is None:
            return _cannot_watch()
        record = cron is True
    try:
        sample = read_sample(_client(deps))
    except ApiError as error:
        return _watch_failed(
            deps, error.code, error.message,
            error.next_step or "The printer was not saved as a new state.",
            record=record,
        )
    path = _state_path(deps)
    if path is None:
        return fail(
            "no_data_dir",
            "plugin_data_dir is not available, so the watch was not saved.",
            "Fix the Hermes profile data directory. The printer was not moved.",
            notify=True,
        )
    previous, problem = _load_record(path)
    if problem:
        return _watch_failed(
            deps,
            "corrupt_state",
            "watch_state.json could not be read, so it was left in place.",
            "Delete watch_state.json in the plugin data directory, then run the watch again. It was not overwritten.",
            record=record,
        )
    now = deps.now()
    found = events_for(previous, sample, now, deps.stall_minutes)
    if not record:
        message = _watch_message(sample, found, None, recorded=False)
        if found:
            message += " This check did not update the cron watch, so the owner's next cron run can still report it."
        return dumps({
            "ok": True,
            "moved": False,
            "notify": bool(found),
            "recorded": False,
            "events": found,
            "message": message,
            "next_step": "The watch does not move the printer. Only a cron run updates the saved watch.",
            "kind": sample["kind"],
            "job_state": sample["job_state"],
            "connection_state": sample["connection_state"],
            "filename": sample["filename"],
            "completion": sample["completion"],
            "print_time_left": sample["print_time_left"],
            "recovered": False,
        })
    saved = next_record(previous, sample, found, now)
    write_error = _dump_json(path, saved, _guard(deps), deps.api_key)
    if write_error:
        return fail("not_saved", write_error, "The watch result was not saved. The printer was not moved.", notify=True, events=found)
    failure_path = path.with_name(FAILURE_FILE)
    recovered = _clear_failure(deps, failure_path)
    notify = bool(found) or recovered is not None
    message = _watch_message(sample, found, recovered, recorded=True)
    return dumps({
        "ok": True,
        "moved": False,
        "notify": notify,
        "events": found,
        "message": message,
        "next_step": "The watch does not move the printer.",
        "kind": sample["kind"],
        "job_state": sample["job_state"],
        "connection_state": sample["connection_state"],
        "filename": sample["filename"],
        "completion": sample["completion"],
        "print_time_left": sample["print_time_left"],
        "recovered": recovered is not None,
    })


def _watch_message(sample: dict[str, Any], found: list[str], recovered: str | None, *, recorded: bool) -> str:
    parts = []
    if recovered:
        parts.append(recovered)
    if not found:
        parts.append("Nothing new to report.")
    else:
        parts.append("Events: " + ", ".join(found) + ".")
        if recorded:
            parts.append("Marked reported when this tool returns; a later chat failure does not send it again.")
        if "printer_disconnected" in found:
            parts.append("The printer is disconnected, not paused.")
        if "error" in found:
            detail = sample.get("error_text") or sample.get("job_state")
            parts.append(f"Printer error: {detail}.")
        if "paused" in found:
            parts.append(
                "Paused after Printing, Printing from SD, Pausing, or Resuming, with the connection still up."
            )
    return " ".join(parts)


def _state_path(deps: Deps) -> Path | None:
    if deps.data_dir is None:
        return None
    return Path(deps.data_dir) / STATE_FILE


def _load_record(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.exists():
        return None, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, "corrupt"
    if not isinstance(data, dict) or data.get("version") != 1:
        return None, "corrupt"
    return data, None


def _dump_json(path: Path, data: dict[str, Any], guard: Callable[[str], str | None], secret: str = "") -> str | None:
    denied = guard(str(path))
    if denied:
        return denied
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        text = dumps(data)
        if secret:
            text = text.replace(secret, "[redacted]")
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    except OSError as error:
        return f"Could not write {path.name} ({type(error).__name__})."
    return None


def _watch_failed(deps: Deps, code: str, message: str, next_step: str, *, record: bool = True) -> str:
    if not record:
        return fail(code, message + " This check did not update the cron watch.", next_step, notify=True, recorded=False)
    path = _state_path(deps)
    notify = True
    if path is None:
        text = message + " The failure could not be recorded, so it will be reported on every check."
        return fail(code, text, next_step, notify=True, failure_reported_before=False)
    failure = path.with_name(FAILURE_FILE)
    previous, problem = _load_record(failure)
    now = deps.now()
    if problem:
        text = message + " watch_failure.json could not be read and was not overwritten. Delete it."
        return fail(code, text, next_step, notify=True, failure_record_unreadable=True)
    report = previous is None or previous.get("code") != code or now - float(previous.get("reported_at") or 0) >= FAILURE_REMIND_SECONDS
    body = {"version": 1, "code": code, "reported_at": now if report else previous.get("reported_at"), "saved_at": now}
    written = _dump_json(failure, body, _guard(deps), deps.api_key)
    if written:
        text = message + " The failure could not be recorded, so it will be reported on every check."
        return fail(code, text, next_step, notify=True)
    if report:
        text = message
    else:
        text = message + " The same failure was already reported."
        notify = False
    return fail(code, text, next_step, notify=notify, failure_reported_before=not report)


def _clear_failure(deps: Deps, path: Path) -> str | None:
    previous, problem = _load_record(path)
    if problem:
        return "The printer can be checked again, but watch_failure.json is unreadable and was left in place. Delete it."
    if previous is None:
        return None
    try:
        path.unlink()
    except OSError:
        return "The printer can be checked again. watch_failure.json could not be removed."
    return "The printer can be checked again."


def _save_snapshot(deps: Deps, sample: dict[str, Any]) -> dict[str, Any]:
    if not deps.snapshot_url.strip():
        return {"ok": False, "message": "OCTOPRINT_SNAPSHOT_URL is not set, so no still was downloaded."}
    if deps.data_dir is None:
        return {"ok": False, "message": "plugin_data_dir is not available, so no still was saved."}
    try:
        origin = parse_origin(deps.url)
        client = _client(deps)
        url = deps.snapshot_url
        if url.startswith("/"):
            url = origin.value + url
        status_code, headers, payload = client.get_bytes(url, deps.snapshot_max_bytes)
    except ApiError as error:
        return {"ok": False, "message": error.message}
    if status_code != 200:
        return {"ok": False, "message": f"The snapshot request returned HTTP {status_code}, so nothing was saved."}
    kind = _image_kind(payload)
    if kind is None:
        return {"ok": False, "message": "The snapshot was not a JPEG or PNG, so it was not saved."}
    directory = Path(deps.data_dir) / "stills"
    name = f"still-{int(deps.now() * 1000)}-1.{kind}"
    if not STILL_NAME.fullmatch(name):
        return {"ok": False, "message": "The still name was refused."}
    path = directory / name
    denied = _guard(deps)(str(path))
    if denied:
        return {"ok": False, "message": denied}
    try:
        directory.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_bytes(payload)
        os.replace(temporary, path)
        _prune_stills(directory)
    except OSError as error:
        return {"ok": False, "message": f"The still was not saved ({type(error).__name__})."}
    return {"ok": True, "path": str(path), "bytes": len(payload), "content_type": headers.get("content-type", "")}


def _image_kind(payload: bytes) -> str | None:
    if payload.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    return None


def _prune_stills(directory: Path) -> None:
    files = [item for item in directory.iterdir() if item.is_file() and STILL_NAME.fullmatch(item.name)]
    files.sort(key=lambda item: item.name)
    for item in files[:-SNAPSHOT_KEEP]:
        try:
            item.unlink()
        except OSError:
            continue


_MONTHS = {name: index for index, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1,
)}
_DOW = {name: index for index, name in enumerate(("sun", "mon", "tue", "wed", "thu", "fri", "sat"))}
_DURATION = re.compile(r"(\d*)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)\Z", re.IGNORECASE)
_DURATION_MINUTES = {"m": 1, "h": 60, "d": 1440}
_SCHEDULE_WORDS = {
    "every", "in", "at", "on", "daily", "hourly", "weekly", "monthly", "yearly", "annually",
    "once", "now", "today", "tonight", "tomorrow", "midnight", "noon", "minute", "minutes",
    "hour", "hours", "day", "days", "week", "weeks", "weekdays", "weekends", "weekday", "weekend",
}
_WEEKDAYS = {
    "mon", "tue", "tues", "wed", "thu", "thur", "thurs", "fri", "sat", "sun",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
}


def _duration_minutes(text: str) -> int | None:
    match = _DURATION.fullmatch(text.strip())
    if not match:
        return None
    number = int(match.group(1)) if match.group(1) else 1
    return number * _DURATION_MINUTES[match.group(2)[0].lower()]


def deliver_looks_like_schedule(deliver: str) -> bool:
    return any(_part_looks_like_schedule(part) for part in deliver.split(","))


# The same set as Hermes cron.scheduler_delivery._KNOWN_DELIVERY_PLATFORMS.
# cli, cron, and api_server are surfaces cron accepts as words and then does not deliver to.
_DELIVER_PLATFORMS = frozenset({
    "telegram", "discord", "slack", "whatsapp", "signal",
    "matrix", "mattermost", "dingtalk", "feishu",
    "wecom", "wecom_callback", "weixin", "sms", "email", "webhook", "bluebubbles",
    "qqbot", "yuanbao",
})
_NEVER_DELIVER = frozenset({"cli", "cron", "api_server"})
_DELIVER_EXACT = frozenset({"origin", "local", "all"})


def _known_deliver_platforms() -> set[str]:
    names = set(_DELIVER_PLATFORMS)
    try:
        from cron.scheduler_delivery import _KNOWN_DELIVERY_PLATFORMS
        names.update(str(name).strip().lower() for name in _KNOWN_DELIVERY_PLATFORMS)
    except Exception:
        pass
    try:
        from hermes_cli.plugins import discover_plugins
        discover_plugins()
        from gateway.platform_registry import platform_registry
        for name in platform_registry.registered_names():
            text = str(name).strip().lower()
            entry = platform_registry.get(text)
            if text and entry is not None and getattr(entry, "cron_deliver_env_var", None):
                names.add(text)
    except Exception:
        pass
    return names - _NEVER_DELIVER


def canonical_deliver(deliver: str) -> str | None:
    """A delivery target cron can actually send to, or None. Comma combinations are kept."""
    names = _known_deliver_platforms()
    canon: list[str] = []
    for part in deliver.split(","):
        piece = part.strip()
        if not piece or any(ch.isspace() for ch in piece):
            return None
        low = piece.lower()
        if low in _NEVER_DELIVER:
            return None
        if low in _DELIVER_EXACT or low == "bot-chat":
            canon.append(low)
            continue
        if low.startswith("bot-chat:"):
            chat = piece.split(":", 1)[1].strip()
            if not chat or any(ch.isspace() for ch in chat):
                return None
            canon.append(f"bot-chat:{chat}")
            continue
        platform, sep, chat = piece.partition(":")
        key = platform.lower()
        if key not in names:
            return None
        if not sep:
            canon.append(key)
            continue
        if not chat or any(ch.isspace() for ch in chat):
            return None
        canon.append(f"{key}:{chat}")
    return ",".join(canon) if canon else None


def _part_looks_like_schedule(target: str) -> bool:
    word = target.strip().lower()
    if not word:
        return False
    if word in _SCHEDULE_WORDS or word in _WEEKDAYS or word.rstrip("s") in _WEEKDAYS:
        return True
    if word[0].isdigit() or word[0] in "*@":
        return True
    if "/" in word or _duration_minutes(word) is not None:
        return True
    try:
        datetime.fromisoformat(word.replace("z", "+00:00"))
    except ValueError:
        return False
    return True


def _schedule_refusal(expr: str) -> str | None:
    text = expr.strip()
    if not text:
        return "That schedule is empty, so no job was created."
    lower = text.lower()
    if lower.startswith("every "):
        minutes = _duration_minutes(text[6:])
        if minutes is None:
            return "This plugin could not prove that schedule waits at least 2 minutes, so no job was created."
        if minutes < 2:
            return "That schedule is faster than every 2 minutes, so no job was created."
        return None
    if lower.startswith("in "):
        if _duration_minutes(text[3:]) is None:
            return "This plugin could not prove that one-shot delay, so no job was created."
        return None
    minutes = _duration_minutes(text)
    if minutes is not None:
        if minutes < 2:
            return "That schedule is faster than every 2 minutes, so no job was created."
        return None
    if "T" in text or re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", text):
        try:
            datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return "This plugin could not prove that timestamp, so no job was created."
        return None
    parts = text.split()
    if len(parts) in {5, 6} and all(re.fullmatch(r"[A-Za-z0-9*,/-]+", part) for part in parts):
        gap = _cron_min_gap_seconds(parts)
        if gap is None:
            return "This plugin could not prove that cron expression waits at least 2 minutes, so no job was created."
        if gap < 120:
            return "That schedule is faster than every 2 minutes, so no job was created."
        return None
    return "This plugin could not prove that schedule waits at least 2 minutes, so no job was created."


def _cron_token(token: str, names: dict[str, int] | None) -> int | None:
    if token.isdigit():
        return int(token)
    if names is not None and token.lower() in names:
        return names[token.lower()]
    return None


def _cron_values(field: str, lo: int, hi: int, names: dict[str, int] | None = None) -> set[int] | None:
    values: set[int] = set()
    for part in field.split(","):
        step = 1
        chunk = part
        if "/" in part:
            chunk, raw_step = part.split("/", 1)
            if not raw_step.isdigit() or int(raw_step) < 1:
                return None
            step = int(raw_step)
        if chunk in {"*", ""}:
            start, end = lo, hi
        elif "-" in chunk:
            left, right = chunk.split("-", 1)
            start = _cron_token(left, names)
            end = _cron_token(right, names)
            if start is None or end is None or start > end or start < lo or end > hi:
                return None
        else:
            start = _cron_token(chunk, names)
            if start is None or start < lo or start > hi:
                return None
            end = start
        for number in range(start, end + 1, step):
            values.add(number)
    return values or None


def _cron_min_gap_seconds(parts: list[str]) -> int | None:
    if len(parts) == 6:
        seconds = _cron_values(parts[0], 0, 59)
        if not seconds or len(seconds) != 1:
            return 0 if seconds else None
        fields = parts[1:]
    else:
        fields = parts
    minute = _cron_values(fields[0], 0, 59)
    hour = _cron_values(fields[1], 0, 23)
    day = _cron_values(fields[2], 1, 31)
    month = _cron_values(fields[3], 1, 12, _MONTHS)
    dow = _cron_values(fields[4], 0, 7, _DOW)
    if not all((minute, hour, day, month, dow)):
        return None
    assert minute and hour and day and month and dow
    if 7 in dow:
        dow = (dow - {7}) | {0}
    dom_star = fields[2] == "*"
    dow_star = fields[4] == "*"
    start = datetime(2024, 1, 1)
    every_month = month == set(range(1, 13))
    if dom_star and dow_star and every_month:
        window_days = 2
    elif dom_star and every_month:
        window_days = 14
    elif dow_star and every_month:
        window_days = 62
    else:
        window_days = 366
    end = start + timedelta(days=window_days)
    previous: datetime | None = None
    smallest: int | None = None
    matched_any = False
    cursor = start
    while cursor < end:
        cron_dow = (cursor.weekday() + 1) % 7
        if cursor.month in month and cursor.hour in hour and cursor.minute in minute:
            dom_ok = cursor.day in day
            dow_ok = cron_dow in dow
            if dom_star and dow_star:
                matched = True
            elif dom_star:
                matched = dow_ok
            elif dow_star:
                matched = dom_ok
            else:
                matched = dom_ok or dow_ok
            if matched:
                matched_any = True
                if previous is not None:
                    gap = int((cursor - previous).total_seconds())
                    if smallest is None or gap < smallest:
                        smallest = gap
                    if smallest < 120:
                        return smallest
                previous = cursor
        cursor += timedelta(minutes=1)
    if not matched_any:
        return None
    return 366 * 24 * 3600 if smallest is None else smallest


def _cron(deps: Deps) -> Any:
    if deps.cron_module is not None:
        return deps.cron_module
    try:
        from cron import jobs
        return jobs
    except Exception as error:
        raise ApiError(
            "no_cron",
            f"Hermes cron is not available ({type(error).__name__}).",
            f"Create the job with {hermes_line('cron create')}. Do not point it at print_job_control.",
        ) from None


def schedule(deps: Deps, when: str = DEFAULT_SCHEDULE, deliver: str = "") -> str:
    if not deliver.strip():
        return fail(
            "no_deliver",
            "No delivery target was given, so no cron job was created.",
            "Pass a target such as telegram, discord, slack, or local. local is not sent to a chat.",
        )
    if deliver_looks_like_schedule(deliver):
        example = hermes_line('print-job-watch schedule --deliver telegram --schedule "*/5 * * * *"')
        return fail(
            "deliver_looks_like_schedule",
            f"The delivery target {deliver.strip()!r} looks like part of a schedule, so no cron job was created "
            "and any existing print-job-watch job was left as it is.",
            f"Use `{example}`.",
        )
    target = canonical_deliver(deliver)
    if target is None:
        return fail(
            "bad_deliver",
            f"{deliver.strip()!r} is not a delivery target cron can send to, so no cron job was created "
            "and any existing print-job-watch job was left as it is.",
            "Use origin, local, all, bot-chat, bot-chat:<profile>, a platform such as telegram, "
            "or platform:chat_id, including a comma combination such as origin,all. "
            "cli, cron, and api_server are not delivery targets. "
            "bot-chat spends one model turn: the agent reads the report and acts on it. "
            "local is not sent to a chat.",
        )
    deliver = target
    refusal = _schedule_refusal(when)
    if refusal:
        return fail("schedule_too_fast", refusal, "Use a schedule this plugin can prove is at least 2 minutes. Nothing was created.")
    try:
        jobs = _cron(deps)
        listed = jobs.list_jobs(include_disabled=True) or []
        old = next((job for job in listed if job.get("name") == JOB_NAME), None)
        created = jobs.create_job(
            prompt=CRON_PROMPT, schedule=when, name=JOB_NAME, deliver=deliver.strip(),
            enabled_toolsets=[TOOLSET],
        )
        if old and old.get("id") and old.get("id") != created.get("id"):
            try:
                jobs.remove_job(old["id"])
            except Exception:
                return dumps({
                    "ok": True,
                    "moved": False,
                    "message": (
                        f"Scheduled new job {created.get('id')}, but the previous job {old.get('id')} "
                        f"which delivered to {old.get('deliver') or '(none)'} is still there. "
                        f"Remove the previous one with `{hermes_line('cron list')}`."
                    ),
                    "job_id": created.get("id"),
                    "previous_deliver": old.get("deliver"),
                })
    except ApiError as error:
        return fail(error.code, error.message, error.next_step)
    except Exception as error:
        return fail("no_cron", f"Could not schedule ({type(error).__name__}: {error}).", "No printer call was made.")
    target = deliver.strip()
    replaced = ""
    previous_deliver = None
    if old and old.get("id") and old.get("id") != created.get("id"):
        previous_deliver = old.get("deliver") or "(none)"
        replaced = f" It replaced job {old.get('id')}, which delivered to {previous_deliver}; results now go to {target}."
    where = (
        f"saved on this machine only (`{hermes_line('cron list')}`). It is not sent to a chat."
        if target == "local"
        else f"marked for delivery to {target}. This plugin does not check that the chat exists."
    )
    return dumps({
        "ok": True,
        "moved": False,
        "message": (
            f"Scheduled {JOB_NAME} ({created.get('schedule_display') or when}). Results are {where}{replaced} "
            "Each run is at least one model turn; a turn that calls a tool makes two or more model requests. "
            "If the saved schedule is every 5 minutes, that is 288 runs a day. "
            f"Removing this plugin does not remove the job; run `{hermes_line('print-job-watch unschedule')}` first."
        ),
        "job_id": created.get("id"),
        "deliver": target,
        "previous_deliver": previous_deliver,
        "agent_turns_per_run": 1,
    })


def unschedule(deps: Deps) -> str:
    try:
        jobs = _cron(deps)
        listed = jobs.list_jobs(include_disabled=True) or []
        old = next((job for job in listed if job.get("name") == JOB_NAME), None)
        if not old:
            return dumps({"ok": True, "moved": False, "message": "print-job-watch is not scheduled. Watch state was left in place."})
        jobs.remove_job(old["id"])
    except ApiError as error:
        return fail(error.code, error.message, error.next_step)
    except Exception as error:
        return fail("no_cron", f"Could not remove the job ({type(error).__name__}).", f"Use `{hermes_line('cron list')}` and remove print-job-watch there.")
    return dumps({
        "ok": True,
        "moved": False,
        "message": f"Removed cron job {old['id']}. Watch state was kept. `{hermes_line('plugins remove')}` does not do this by itself.",
    })
