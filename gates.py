"""Fail-closed gates. A move is refused when Hermes cannot ask a person."""
from __future__ import annotations

import importlib
import json
import os
import uuid
from pathlib import Path
from typing import Any, Callable

PLUGIN_NAME = "print-job-watch"
ACTIONS = ("pause", "resume", "cancel")
HOST_PROCESS_ENV = "HERMES_PLUGIN_HOST_PROCESS"
REASON_BUDGET = 300

BODIES = {
    "pause": {"command": "pause", "action": "pause"},
    "resume": {"command": "pause", "action": "resume"},
    "cancel": {"command": "cancel"},
}


def in_plugin_host_process() -> bool:
    """True unless the variable is unset, empty, or 0. Hermes itself treats only 1 as the host."""
    return os.environ.get(HOST_PROCESS_ENV, "").strip() not in {"", "0"}


def _load(module: str, name: str) -> tuple[str, Any]:
    try:
        mod = importlib.import_module(module)
    except Exception:
        return "failed", None
    if not hasattr(mod, name):
        return "missing", None
    try:
        return "ok", getattr(mod, name)
    except Exception:
        return "failed", None


def reason_fits(text: str) -> bool:
    """False when Discord would cut the reason, or HTML escaping would push it over 300."""
    if len(text) > REASON_BUDGET:
        return False
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return len(escaped) <= REASON_BUDGET


def approval_text(action: str, origin: str) -> str:
    where = origin or "(OCTOPRINT_URL not set)"
    if action == "pause":
        meaning = "This pauses the current print."
    elif action == "resume":
        meaning = "This resumes the paused print from where it stopped. It does not restart from the beginning."
    else:
        meaning = "Cancel ends the print; it cannot be resumed afterwards."
    body = json.dumps(BODIES[action], separators=(",", ":"), sort_keys=True)
    return (
        f"OctoPrint at {where}: POST /api/job {body}. {meaning} "
        "This one call only; choose once."
    )


def one_call_rule_key(action: str) -> str:
    return f"print_job_control:{action}:{uuid.uuid4().hex}"


def motion_block_reason(action: str) -> str | None:
    if action not in ACTIONS:
        return (
            f"'{action}' is not allowed. The only actions are pause, resume, and cancel. "
            "Temperature changes, G-code, start, restart, and toggle are not in this plugin."
        )
    if in_plugin_host_process():
        return (
            "BLOCKED: this plugin is running in a separate plugin-host process (plugins.isolation: host). "
            "Pause, resume, and cancel are refused there. Use plugins.isolation: in_process to move the printer."
        )
    checks = (
        ("tools.approval_context", "_is_cron_approval_context", "a cron job", "BLOCKED: cron cannot pause, resume, or cancel the printer. The watch only reports."),
        ("tools.approval", "_yolo_active", "yolo", "BLOCKED: Hermes yolo is on, so the approval gate would not ask a person. Turn yolo off and try again."),
        ("tools.approval_context", "_get_approval_mode", "approvals.mode", "BLOCKED: Hermes approvals are off, so nobody would be asked. Turn approvals on and try again."),
        ("tools.approval_context", "_is_single_query_approval_context", "a single-query session", "BLOCKED: a single-query session cannot move the printer."),
        ("tools.approval_context", "_is_unattended_platform_approval_context", "an unattended session", "BLOCKED: an unattended session cannot move the printer."),
    )
    for module, name, label, blocked in checks:
        status, fn = _load(module, name)
        if status != "ok":
            return f"BLOCKED: could not check {label}, so the printer was not moved."
        try:
            value = fn()
        except Exception:
            return f"BLOCKED: the check for {label} failed, so the printer was not moved."
        if name == "_get_approval_mode":
            if value == "off":
                return blocked
        elif value:
            return blocked
    return None


def request_motion_approval(action: str, origin: str) -> tuple[bool, str, str]:
    """Returns approved, message, rule_key. No Hermes call when the move is already blocked."""
    reason = motion_block_reason(action)
    if reason:
        return False, reason, ""
    text = approval_text(action, origin)
    if not reason_fits(text):
        return False, (
            "BLOCKED: the approval question would not fit in one message, so it was not sent and the printer was not moved."
        ), ""
    status, fn = _load("tools.approval", "request_tool_approval")
    if status != "ok":
        return False, "BLOCKED: Hermes approval could not be loaded, so the printer was not moved.", ""
    rule_key = one_call_rule_key(action)
    try:
        result = fn("print_job_control", text, rule_key=rule_key)
    except Exception:
        return False, "BLOCKED: the Hermes approval request failed, so the printer was not moved.", rule_key
    if not isinstance(result, dict) or result.get("approved") is not True:
        message = str(result.get("message") or "") if isinstance(result, dict) else ""
        return False, message or "BLOCKED: the printer move was not approved.", rule_key
    return True, "", rule_key


def write_guard_error(path: str) -> str | None:
    status, fn = _load("agent.file_safety", "get_write_denied_error")
    if status != "ok":
        return "BLOCKED: Hermes's write guard could not be loaded, so the file was not saved."
    try:
        denied = fn(path)
    except Exception:
        return "BLOCKED: Hermes's write guard raised, so the file was not saved."
    if denied:
        return str(denied)
    return None


def plugin_data_dir() -> Path:
    status, fn = _load("plugins.plugin_storage", "plugin_data_dir")
    if status != "ok":
        raise RuntimeError("plugin_data_dir is not available")
    path = fn(PLUGIN_NAME)
    if not isinstance(path, Path):
        path = Path(path)
    return path


Guard = Callable[[str], str | None]
Approver = Callable[[str, str], tuple[bool, str]]
