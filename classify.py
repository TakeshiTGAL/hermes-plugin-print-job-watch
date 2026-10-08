"""Print-state names for OctoPrint. A 409 from /api/printer is not a state."""
from __future__ import annotations

from typing import Any

ERROR_STATES = {"Error", "Offline after error"}
CLOSED_CONNECTIONS = {"Closed", "Offline"}
PRINTING_STATES = {"Printing", "Printing from SD"}
PAUSE_FROM = PRINTING_STATES | {"Pausing", "Resuming"}
COMPLETE_FROM = PRINTING_STATES | {"Pausing", "Paused", "Resuming", "Finishing"}
CANCEL_FROM = PRINTING_STATES | {"Pausing", "Paused", "Resuming", "Cancelling", "Finishing"}


def kind_of(job_state: str, connection_state: str) -> str:
    """One of error, disconnected, paused, printing, or other.

    Error wins over disconnect. A closed connection wins over a leftover Paused
    string. HTTP 409 is not an input here.
    """
    if job_state in ERROR_STATES or connection_state == "Error":
        return "error"
    if connection_state in CLOSED_CONNECTIONS or job_state == "Offline":
        return "disconnected"
    if job_state == "Paused":
        return "paused"
    if job_state in PRINTING_STATES:
        return "printing"
    return "other"


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def events_for(previous: dict[str, Any] | None, sample: dict[str, Any], now: float, stall_minutes: int) -> list[str]:
    """Events since ``previous``. The first stored sample is quiet unless the printer is already down."""
    kind = kind_of(str(sample.get("job_state") or ""), str(sample.get("connection_state") or ""))
    if previous is None:
        if kind == "disconnected":
            return ["printer_disconnected"]
        if kind == "error":
            return ["error"]
        return []

    prev_job = str(previous.get("job_state") or "")
    prev_kind = str(previous.get("kind") or "")
    job = str(sample.get("job_state") or "")
    found: list[str] = []
    if kind == "disconnected" and prev_kind != "disconnected":
        found.append("printer_disconnected")
        return found
    if kind == "error" and prev_kind != "error":
        found.append("error")
        return found
    if kind in {"disconnected", "error"}:
        return found

    reported = set(previous.get("reported") or [])
    if job == "Paused" and prev_job in PAUSE_FROM and "paused" not in reported:
        found.append("paused")
    if job == "Cancelling" and prev_job != "Cancelling" and prev_job in CANCEL_FROM and "cancelled" not in reported:
        found.append("cancelled")
    completion = _number(sample.get("completion"))
    if job == "Operational" and "cancelled" not in reported and "cancelled" not in found and "complete" not in reported:
        missed_cancel = prev_job == "Cancelling" or prev_job in COMPLETE_FROM
        if missed_cancel and prev_job in COMPLETE_FROM and completion is not None and completion >= 100:
            found.append("complete")
        elif missed_cancel and (completion is None or completion < 100):
            found.append("cancelled")
    if job in PRINTING_STATES and _stalled(previous, sample, now, stall_minutes) and "stalled" not in reported:
        found.append("stalled")
    return found


def _stalled(previous: dict[str, Any], sample: dict[str, Any], now: float, stall_minutes: int) -> bool:
    filepos = _number(sample.get("filepos"))
    if filepos is None:
        return False
    same = (
        previous.get("filepos") == sample.get("filepos")
        and previous.get("completion") == sample.get("completion")
        and previous.get("filename") == sample.get("filename")
        and str(previous.get("job_state") or "") in PRINTING_STATES
    )
    since = previous.get("stall_since")
    if not same or not isinstance(since, (int, float)):
        return False
    return float(now) - float(since) >= stall_minutes * 60


def next_record(previous: dict[str, Any] | None, sample: dict[str, Any], events: list[str], now: float) -> dict[str, Any]:
    """State file body. Does not copy the API key or a chat user name."""
    job = str(sample.get("job_state") or "")
    prev_job = "" if previous is None else str(previous.get("job_state") or "")
    new_print = previous is not None and job in PRINTING_STATES and prev_job not in PRINTING_STATES
    generation = 1 if previous is None else int(previous.get("generation") or 1)
    if new_print:
        generation += 1
    reported = set() if previous is None or new_print else set(previous.get("reported") or [])
    reported.update(events)
    filepos = sample.get("filepos")
    stalled_same = (
        previous is not None
        and previous.get("filepos") == filepos
        and previous.get("completion") == sample.get("completion")
        and previous.get("filename") == sample.get("filename")
        and prev_job in PRINTING_STATES
        and job in PRINTING_STATES
        and _number(filepos) is not None
    )
    if stalled_same and isinstance(previous.get("stall_since"), (int, float)):
        stall_since = previous.get("stall_since")
    elif job in PRINTING_STATES and _number(filepos) is not None:
        stall_since = now
    else:
        stall_since = None
    left = sample.get("print_time_left")
    if isinstance(left, bool) or not isinstance(left, (int, float)):
        left = None
    return {
        "version": 1,
        "job_state": job,
        "connection_state": str(sample.get("connection_state") or ""),
        "kind": kind_of(job, str(sample.get("connection_state") or "")),
        "filename": sample.get("filename"),
        "completion": sample.get("completion"),
        "filepos": filepos,
        "print_time_left": left,
        "generation": generation,
        "reported": sorted(reported),
        "stall_since": stall_since,
        "saved_at": now,
    }
