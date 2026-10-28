"""Move plans and the public pre_tool_call line. Hermes asks the person."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable

PLUGIN_NAME = "print-job-watch"
WATCH_JOB_NAME = "print-job-watch"
ACTIONS = ("pause", "resume", "cancel")
WATCH_ONLY = (
    "BLOCKED: this cron run is the print-job-watch job. "
    "Only print_job_watch may run. tool_search, tool_describe, and tool_call may run "
    "so the model can reach it. Other tools are blocked before they run."
)
# print_job_control is listed so the unattended refusal below still speaks.
# The three bridge names are listed because a cron turn asks this hook about them.
_WATCH_PASS = frozenset({
    "print_job_watch",
    "print_job_control",
    "tool_search",
    "tool_describe",
    "tool_call",
})
HOST_PROCESS_ENV = "HERMES_PLUGIN_HOST_PROCESS"
REASON_BUDGET = 300
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_UNATTENDED_PLATFORMS = frozenset({"webhook", "msgraph_webhook", "api_server"})
UNATTENDED_MOTION = (
    "BLOCKED: pause, resume, and cancel are refused in cron, hermes -q, webhook, "
    "and other unattended sessions, including after always. Nothing was sent."
)

BODIES = {
    "pause": {"command": "pause", "action": "pause"},
    "resume": {"command": "pause", "action": "resume"},
    "cancel": {"command": "cancel"},
}


def in_plugin_host_process() -> bool:
    """True unless the variable is unset, empty, or 0. Hermes itself treats only 1 as the host."""
    return os.environ.get(HOST_PROCESS_ENV, "").strip() not in {"", "0"}


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
        scope = "always is only this action at this origin while a person is present, not cron, -q, or webhook."
    elif action == "resume":
        meaning = "This resumes the paused print from where it stopped. It does not restart from the beginning."
        scope = "always is only this action at this origin while a person is present, not cron, -q, or webhook."
    else:
        meaning = "Cancel ends the print; it cannot be resumed afterwards."
        scope = (
            "Each cancel asks again while a person is present. "
            "Answering always does not cover the next cancel. cron, -q, and webhook refuse the move."
        )
    body = json.dumps(BODIES[action], separators=(",", ":"), sort_keys=True)
    return f"OctoPrint at {where}: POST /api/job {body}. {meaning} {scope}"


def session_value(name: str) -> str:
    """Session value Hermes itself reads: the context var, then the process environment.

    The module is already imported when a Hermes turn is running. This does not
    import it. If it is absent, the process environment is the fallback.
    """
    module = sys.modules.get("gateway.session_context")
    reader = getattr(module, "get_session_env", None) if module is not None else None
    if callable(reader):
        try:
            return str(reader(name, "") or "")
        except Exception:
            pass
    return os.environ.get(name, "") or ""


def _truthy(value: str) -> bool:
    return value.strip().lower() in _TRUTHY


def _current_cron():
    """Public cron mark, or None when it is missing or cannot be read. Import failure is not cron."""
    try:
        from cron.execution_identity import current_cron_execution
    except Exception:
        return None
    try:
        return current_cron_execution()
    except Exception:
        return None


def cron_mark_active() -> bool:
    """True when the public cron execution mark is bound. Import failure is not cron."""
    return _current_cron() is not None


def our_watch_cron() -> bool:
    """True only when the running cron job is the one this plugin names print-job-watch.

    A chat has no cron mark. Another cron job has a different job_name. Both leave
    every other tool call untouched.
    """
    current = _current_cron()
    if current is None:
        return False
    return getattr(current, "job_name", None) == WATCH_JOB_NAME


def motion_is_unattended() -> bool:
    """True for cron, hermes -q, and webhook-style platforms. The plugin host is separate."""
    if cron_mark_active():
        return True
    if _truthy(session_value("HERMES_CRON_SESSION")):
        return True
    if _truthy(session_value("HERMES_SINGLE_QUERY_SESSION")):
        return True
    platform = session_value("HERMES_SESSION_PLATFORM").strip().lower()
    return platform in _UNATTENDED_PLATFORMS


def plan_digest(action: str, origin: str) -> str:
    """Hermes stores session and always under this key. The same plan matches; another does not."""
    body = json.dumps({"action": action, "origin": origin}, sort_keys=True, separators=(",", ":"))
    return "print_job_control:" + hashlib.sha256(body.encode()).hexdigest()[:16]


def rule_key_for(action: str, origin: str) -> str:
    """Pause and resume keep the plan digest. Cancel is a new key on every call, so always cannot stick."""
    if action == "cancel":
        nonce = secrets.token_hex(8)
        body = json.dumps(
            {"action": action, "origin": origin, "call": nonce},
            sort_keys=True,
            separators=(",", ":"),
        )
        return "print_job_control:" + hashlib.sha256(body.encode()).hexdigest()[:16]
    return plan_digest(action, origin)


def fold_action(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = unicodedata.normalize("NFKC", value).strip().lower()
    if text in ACTIONS:
        return text
    return None


def motion_block_reason(action: str) -> str | None:
    """Local refusals. yolo and approvals off stay Hermes's gate. Unattended motion stops here.

    Hermes checks a saved always line before its own cron, -q, and unattended deny,
    so this refusal has to happen in the hook, before that line is consulted.
    """
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
    if motion_is_unattended():
        return UNATTENDED_MOTION
    return None


_LOCK = threading.Lock()
_STASH_TTL = 600
_STASH: dict[tuple, tuple[tuple[str, str, str], float]] = {}
_origin_getter: Callable[[], str] | None = None


def _ids(kwargs: dict) -> tuple[str, str]:
    return (str(kwargs.get("session_id") or ""), str(kwargs.get("task_id") or ""))


def _stash_key(payload: dict, ids: tuple[str, str]) -> tuple:
    # The whole call, plus the session and task. One action does not cover another spelling,
    # another session, or another task.
    fields = {str(key): payload[key] for key in payload}
    return ("print_job_control", json.dumps(fields, sort_keys=True, default=str), ids[0], ids[1])


def set_origin_getter(getter: Callable[[], str] | None) -> None:
    global _origin_getter
    _origin_getter = getter


def _origin() -> str:
    getter = _origin_getter
    if getter is None:
        return ""
    try:
        value = getter()
    except Exception:
        return ""
    return value if isinstance(value, str) else ""


def _plan(action: str, origin: str) -> str:
    return approval_text(action, origin)


def remember_move(payload: dict, ids: tuple[str, str], plan: tuple[str, str, str]) -> None:
    with _LOCK:
        _STASH[_stash_key(payload, ids)] = (plan, time.monotonic())


def take_move(payload: dict, ids: tuple[str, str], plan: tuple[str, str, str]) -> tuple[bool, str]:
    """Pop this call's plan. Missing, stale, or different means do not post."""
    with _LOCK:
        found = _STASH.pop(_stash_key(payload, ids), None)
    if found is None:
        return False, "BLOCKED: the printer move was not approved. Nothing was sent."
    stored, made = found
    if time.monotonic() - made > _STASH_TTL:
        return False, "BLOCKED: the approval is too old. Nothing was sent."
    if stored != plan:
        return False, "The target changed after the preview. Nothing was sent."
    return True, ""


def on_pre_tool_call(tool_name: str = "", args: dict | None = None, **kwargs: Any):
    """Approve one pause, resume, or cancel. Reads and watches stay silent.

    On this plugin's own watch cron job, print_job_watch and the three bridge tools
    pass. print_job_control still falls through to the unattended refusal below.
    A cron turn peels a tool_call that names a real deferred tool down to that
    inner name before this hook, so another tool is blocked under its own name.
    Any other cron job, and any chat, keeps the previous return for that tool.
    """
    if our_watch_cron() and tool_name not in _WATCH_PASS:
        return {"action": "block", "message": WATCH_ONLY}
    if tool_name != "print_job_control":
        return None
    # Every writer call in this session and task starts clean. A preview kept after a
    # refusal, a block, or a later modify must not authorize a different call.
    # Other rooms keep their own previews while they wait for an answer.
    room = _ids(kwargs)
    with _LOCK:
        for key in [k for k in _STASH if k[2:] == room]:
            del _STASH[key]
    payload = dict(args) if isinstance(args, dict) else {}
    if set(payload) - {"action"}:
        return None
    if "action" not in payload or payload.get("action") in (None, ""):
        return None
    action = fold_action(payload.get("action"))
    if action is None:
        return None
    blocked = motion_block_reason(action)
    if blocked:
        return {"action": "block", "message": blocked}
    origin = _origin()
    text = _plan(action, origin)
    if not reason_fits(text) or "\n" in text or "`" in text:
        return {
            "action": "block",
            "message": "BLOCKED: the approval question would not fit in one message, so nothing was sent.",
        }
    remember_move(payload, _ids(kwargs), (action, origin, text))
    return {"action": "approve", "message": text, "rule_key": rule_key_for(action, origin)}


def write_guard_error(path: str) -> str | None:
    """None only when ``path`` stays inside this plugin's data directory."""
    try:
        root = plugin_data_dir().resolve()
        target = Path(path).resolve()
        target.relative_to(root)
    except Exception:
        return "BLOCKED: the file is outside the plugin data directory, so it was not saved."
    return None


def plugin_data_dir() -> Path:
    try:
        from plugins.plugin_storage import plugin_data_dir as read_dir
    except Exception as exc:
        raise RuntimeError("plugin_data_dir is not available") from exc
    path = read_dir(PLUGIN_NAME)
    if not isinstance(path, Path):
        path = Path(path)
    return path


Guard = Callable[[str], str | None]
Approver = Callable[[str, str], tuple[bool, str]]
