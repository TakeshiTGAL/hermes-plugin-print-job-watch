"""Exercise Hermes's real pre_tool_call dispatcher with this plugin's hook."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import gates
from service import control
from test_plugin import ORIGIN, Script, deps, load, view

ROOT = Path(__file__).resolve().parents[1]
HERMES_SRC = Path(os.environ.get("HERMES_SRC") or ROOT.parent / "hermes-agent-ref")


def _load_entry():
    import importlib.util
    spec = importlib.util.spec_from_file_location("print_job_watch_hook_plugin", ROOT / "__init__.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def hermes(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
    monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)
    monkeypatch.delenv("HERMES_YOLO", raising=False)
    monkeypatch.delenv("HERMES_PLUGIN_HOST_PROCESS", raising=False)
    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    monkeypatch.delenv("HERMES_SINGLE_QUERY_SESSION", raising=False)
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    if not (HERMES_SRC / "hermes_cli" / "plugins.py").is_file():
        pytest.skip("set HERMES_SRC to a Hermes source tree")
    src = str(HERMES_SRC)
    if src not in sys.path:
        sys.path.insert(0, src)
    try:
        import hermes_cli.plugins as hp
    except Exception as exc:
        pytest.skip("Hermes source needs a newer Python: " + str(exc))
    mgr = hp.PluginManager(scope_key=str(tmp_path))
    mgr._discovered = True
    mgr._hooks["pre_tool_call"] = [gates.on_pre_tool_call]
    monkeypatch.setattr(hp, "get_plugin_manager", lambda: mgr)
    monkeypatch.setattr(hp, "_plugin_manager", mgr)
    gates.set_origin_getter(lambda: ORIGIN)
    gates._STASH.clear()
    yield hp
    gates._STASH.clear()


def _assert_blocked_skips_handler(monkeypatch, tool_name, args):
    import agent.tool_executor as executor
    import model_tools

    called = []
    monkeypatch.setattr(model_tools, "_execute_tool", lambda *_a, **_k: called.append("handler") or "{}")
    blocked = model_tools.handle_function_call(tool_name, args, session_id="s1", task_id="t1")
    assert called == []
    assert "BLOCKED" in str(blocked)

    ref = executor._ToolCallRef(tool_name, dict(args), "t1", "c1", [])
    state = executor._ManagedToolResult(
        result=None, args=dict(args), middleware_trace=[], blocked=False, dispatched=False,
    )

    class _Agent:
        session_id = "s1"

    executor._dispatch_authorized_once(
        _Agent(),
        state,
        ref,
        execute=lambda next_args: called.append(next_args) or "ran",
        scope_block=None,
        display_index=None,
        begin_execution=None,
        authorization_gate=None,
    )
    assert called == []
    assert state.blocked is True


def test_other_room_preview_stays(hermes):
    gates.on_pre_tool_call("print_job_control", {"action": "pause"}, session_id="room-a", task_id="t1")
    assert any(key[2:] == ("room-a", "t1") for key in gates._STASH)
    gates.on_pre_tool_call("print_job_control", {"action": "cancel"}, session_id="room-b", task_id="t2")
    assert any(key[2:] == ("room-a", "t1") for key in gates._STASH)
    assert any(key[2:] == ("room-b", "t2") for key in gates._STASH)


def test_dispatcher_refusal_does_not_call_the_handler(hermes, monkeypatch):
    _assert_blocked_skips_handler(monkeypatch, "print_job_control", {"action": "pause"})


def test_write_returns_approve(hermes):
    details = hermes._get_pre_tool_call_directive_details("print_job_control", {"action": "pause"})
    assert details.action == "approve"
    assert details.message
    assert "\n" not in details.message
    assert "`" not in details.message
    assert "pause" in details.message
    assert ORIGIN in details.message


def test_same_plan_reuses_rule_key_and_session_does_not_cover_another(hermes):
    pause = hermes._get_pre_tool_call_directive_details("print_job_control", {"action": "pause"})
    again = hermes._get_pre_tool_call_directive_details("print_job_control", {"action": "pause"})
    cancel = hermes._get_pre_tool_call_directive_details("print_job_control", {"action": "cancel"})
    assert pause.rule_key == again.rule_key
    assert pause.rule_key != cancel.rule_key
    assert pause.rule_key.startswith("print_job_control:")
    from tools.approval import approve_session
    from tools.approval_context import get_current_session_key

    approve_session(get_current_session_key(), "plugin_rule:" + pause.rule_key)
    allowed, _modified = hermes._dispatch_pre_tool_call_hooks("print_job_control", {"action": "pause"})
    blocked, _modified = hermes._dispatch_pre_tool_call_hooks("print_job_control", {"action": "cancel"})
    assert allowed is None
    assert blocked and "BLOCKED" in blocked


def test_read_is_silent(hermes):
    status = hermes._get_pre_tool_call_directive_details("print_job_status", {})
    watch = hermes._get_pre_tool_call_directive_details("print_job_watch", {})
    snapshot = hermes._get_pre_tool_call_directive_details("print_job_status", {"include_snapshot": True})
    assert status.action is None
    assert watch.action is None
    assert snapshot.action is None


def test_denied_gate_posts_nothing(hermes, tmp_path):
    script = Script(view("Printing"))
    bare = load(control(deps(script, tmp_path, approver=None)[0], {"action": "pause"}))
    assert bare["moved"] is False
    assert not any(call["method"] == "POST" for call in script.calls)
    before = len(script.calls)
    block, _modified = hermes._dispatch_pre_tool_call_hooks("print_job_control", {"action": "cancel"})
    assert block
    assert "BLOCKED" in block
    assert len(script.calls) == before
    assert not any(call["method"] == "POST" for call in script.calls)


def test_mismatch_after_preview_posts_nothing(hermes, tmp_path):
    assert hermes._get_pre_tool_call_directive_details("print_job_control", {"action": "pause"}).action == "approve"
    script = Script(view("Printing") + view("Paused"))
    body = load(control(deps(script, tmp_path, approver=None)[0], {"action": "pause"}))
    assert body.get("isError") is True
    assert "changed" in body.get("message", "").lower() or body.get("error") == "changed"
    assert not any(call["method"] == "POST" for call in script.calls)


def test_no_private_imports():
    pattern = "|".join(
        [
            "tools\\." + "approval",
            "_" + "yolo" + "_active",
            "approval" + "_context",
            "approval" + "_prompt",
            "request_elicitation_consent",
            "prompt_dangerous_approval",
        ]
    )
    proc = subprocess.run(
        ["grep", "-rnE", pattern, "--include=*.py", "."],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    leaked = [line for line in proc.stdout.splitlines() if "/tests/" not in line and not line.startswith("tests/")]
    assert leaked == [], leaked


def _printing_script():
    return Script(view("Printing") + view("Printing") + view("Paused"))


def _posts(script):
    return [call for call in script.calls if call["method"] == "POST"]


def test_preview_for_one_spelling_does_not_cover_another(hermes, tmp_path):
    gates.on_pre_tool_call("print_job_control", {"action": "pause"})
    script = _printing_script()
    load(control(deps(script, tmp_path, approver=None)[0], {"action": "PAUSE"}))
    assert _posts(script) == []


def test_preview_from_another_session_is_not_used(hermes, tmp_path):
    gates.on_pre_tool_call("print_job_control", {"action": "pause"}, session_id="other", task_id="t1")
    script = _printing_script()
    load(control(deps(script, tmp_path, approver=None)[0], {"action": "pause"}))
    assert _posts(script) == []


def test_preview_from_another_task_is_not_used(hermes, tmp_path):
    gates.on_pre_tool_call("print_job_control", {"action": "pause"}, session_id="s1", task_id="other")
    script = _printing_script()
    load(control(deps(script, tmp_path, approver=None)[0], {"action": "pause"}))
    assert _posts(script) == []


def test_silent_call_rewritten_to_a_write_finds_no_preview(hermes, tmp_path):
    gates.on_pre_tool_call("print_job_control", {"action": "pause"})
    assert gates.on_pre_tool_call("print_job_control", {"action": "pause", "extra": 1}) is None
    script = _printing_script()
    load(control(deps(script, tmp_path, approver=None)[0], {"action": "pause"}))
    assert _posts(script) == []


def test_old_preview_is_refused(hermes, tmp_path, monkeypatch):
    import time
    gates.on_pre_tool_call("print_job_control", {"action": "pause"})
    real = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: real + 601)
    script = _printing_script()
    load(control(deps(script, tmp_path, approver=None)[0], {"action": "pause"}))
    assert _posts(script) == []


def test_register_installs_the_hook():
    plugin = _load_entry()

    class Ctx:
        def __init__(self):
            self.hooks = {}

        def register_tool(self, **kwargs):
            return None

        def register_command(self, *args, **kwargs):
            return None

        def register_cli_command(self, **kwargs):
            return None

        def register_skill(self, *args, **kwargs):
            return None

        def register_hook(self, name, callback):
            self.hooks.setdefault(name, []).append(callback)

        def current_cron_execution(self):
            return None

        def get_config(self):
            return {}

    ctx = Ctx()
    plugin.register(ctx)
    hooked = ctx.hooks.get("pre_tool_call") or []
    assert len(hooked) == 1
    assert hooked[0].__name__ == "on_pre_tool_call"


def test_source_does_not_import_cron_jobs():
    text = (ROOT / "service.py").read_text(encoding="utf-8")
    assert "cron.jobs" not in text
    assert "from cron import jobs" not in text
    assert 'ROOT.parent / "hermes-agent-ref"' in Path(__file__).read_text(encoding="utf-8")


def test_cron_mark_refuses_without_the_cron_env(hermes, monkeypatch):
    from cron.execution_identity import enter_cron_execution, exit_cron_execution

    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    token = enter_cron_execution(
        {"id": "job", "name": "print-job-watch"},
        "exec",
        {"source": "builtin"},
    )
    try:
        block, _modified = hermes._dispatch_pre_tool_call_hooks(
            "print_job_control", {"action": "pause"}, session_id="s1", task_id="t1",
        )
    finally:
        exit_cron_execution(token)
    assert block and "including after always" in block


def test_always_then_cron_context_does_not_post(hermes, tmp_path, monkeypatch):
    from gateway.session_context import reset_session_vars, set_session_vars
    from tools.approval import approve_permanent
    import model_tools

    monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
    details = hermes._get_pre_tool_call_directive_details("print_job_control", {"action": "pause"})
    approve_permanent("plugin_rule:" + details.rule_key)
    set_session_vars(cron_session="1")
    script = Script(view("Printing") + view("Printing") + [(204, None)] + view("Paused"))
    called = []

    def execute(*_args, **_kwargs):
        called.append("handler")
        control(
            deps(script, tmp_path, approver=None)[0],
            {"action": "pause"},
            session_id="s1",
            task_id="t1",
        )
        return "{}"

    monkeypatch.setattr(model_tools, "_execute_tool", execute)
    try:
        result = model_tools.handle_function_call(
            "print_job_control", {"action": "pause"}, session_id="s1", task_id="t1",
        )
    finally:
        reset_session_vars()
    assert called == []
    assert "including after always" in str(result)
    assert _posts(script) == []


def test_always_then_webhook_context_is_blocked_without_process_env(hermes, monkeypatch):
    from gateway.session_context import reset_session_vars, set_session_vars
    from tools.approval import approve_permanent

    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    details = hermes._get_pre_tool_call_directive_details("print_job_control", {"action": "pause"})
    approve_permanent("plugin_rule:" + details.rule_key)
    set_session_vars(platform="webhook")
    try:
        assert os.environ.get("HERMES_SESSION_PLATFORM", "") == ""
        block, _modified = hermes._dispatch_pre_tool_call_hooks(
            "print_job_control", {"action": "pause"}, session_id="s1", task_id="t1",
        )
    finally:
        reset_session_vars()
    assert block and "including after always" in block


def test_watch_cron_blocks_other_tools_and_leaves_other_jobs_alone(hermes):
    from cron.execution_identity import enter_cron_execution, exit_cron_execution

    token = enter_cron_execution(
        {"id": "watch-job", "name": "print-job-watch"},
        "exec-watch",
        {"source": "builtin"},
    )
    try:
        terminal, _modified = hermes._dispatch_pre_tool_call_hooks(
            "terminal", {"command": "id"}, session_id="s1", task_id="t1",
        )
        status, _modified = hermes._dispatch_pre_tool_call_hooks(
            "print_job_status", {}, session_id="s1", task_id="t1",
        )
        watch = hermes._get_pre_tool_call_directive_details("print_job_watch", {})
        control, _modified = hermes._dispatch_pre_tool_call_hooks(
            "print_job_control", {"action": "pause"}, session_id="s1", task_id="t1",
        )
    finally:
        exit_cron_execution(token)
    assert terminal and "print_job_watch" in terminal
    assert status and "BLOCKED" in status
    assert watch.action is None
    assert control and "including after always" in control

    other = enter_cron_execution(
        {"id": "note-job", "name": "daily-note"},
        "exec-other",
        {"source": "builtin"},
    )
    try:
        left, _modified = hermes._dispatch_pre_tool_call_hooks(
            "terminal", {"command": "id"}, session_id="s1", task_id="t1",
        )
        still = hermes._get_pre_tool_call_directive_details("print_job_status", {})
    finally:
        exit_cron_execution(other)
    assert left is None
    assert still.action is None

    chat, _modified = hermes._dispatch_pre_tool_call_hooks(
        "terminal", {"command": "id"}, session_id="s1", task_id="t1",
    )
    assert chat is None


def test_cancel_always_does_not_cover_the_next_call(hermes):
    from tools.approval import approve_permanent

    first = hermes._get_pre_tool_call_directive_details("print_job_control", {"action": "cancel"})
    second = hermes._get_pre_tool_call_directive_details("print_job_control", {"action": "cancel"})
    assert first.action == "approve"
    assert second.action == "approve"
    assert first.rule_key != second.rule_key
    assert first.rule_key.startswith("print_job_control:")
    assert "Each cancel asks again" in first.message
    approve_permanent("plugin_rule:" + first.rule_key)
    outcome, _modified = hermes._dispatch_pre_tool_call_hooks(
        "print_job_control", {"action": "cancel"}, session_id="s1", task_id="t1",
    )
    assert outcome is not None
    pause = hermes._get_pre_tool_call_directive_details("print_job_control", {"action": "pause"})
    again = hermes._get_pre_tool_call_directive_details("print_job_control", {"action": "pause"})
    assert pause.rule_key == again.rule_key


def test_yolo_lets_hermes_skip_the_prompt_and_post(hermes, tmp_path, monkeypatch):
    import model_tools
    import tools.approval as approval

    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", True)
    script = Script(view("Printing") + view("Printing") + [(204, None)] + view("Paused"))
    called = []

    def execute(*_args, **_kwargs):
        called.append("handler")
        return control(
            deps(script, tmp_path, approver=None)[0],
            {"action": "pause"},
            session_id="s1",
            task_id="t1",
        )

    monkeypatch.setattr(model_tools, "_execute_tool", execute)
    result = model_tools.handle_function_call(
        "print_job_control", {"action": "pause"}, session_id="s1", task_id="t1",
    )
    assert called == ["handler"]
    assert _posts(script)
    assert "BLOCKED" not in str(result)
