"""Argument shapes a model is likely to send. No network."""
import json

from test_plugin import Script, control, deps, load, status, view


def test_action_case_posts_pause(tmp_path):
    script = Script(view("Printing") + view("Printing") + [(204, None)] + view("Pausing") + view("Paused"))
    seen = []

    def approve(text, rule):
        seen.append(text)
        return True, ""

    out = load(control(deps(script, tmp_path, approver=approve, control_timeout=30)[0], {"action": "Pause"}))
    assert out["ok"] is True
    assert out["moved"] is True
    posted = [call for call in script.calls if call["method"] == "POST"]
    assert json.loads(posted[0]["body"]) == {"command": "pause", "action": "pause"}
    assert "This pauses the current print." in seen[0]


def test_string_booleans_follow_the_flag(tmp_path):
    skipped = load(status(deps(Script(view("Printing")), tmp_path)[0], {"include_snapshot": "false"}))
    assert skipped["ok"] is True
    assert skipped["snapshot"] is None
    honored = load(status(deps(Script(view("Printing")), tmp_path)[0], {"include_snapshot": "True"}))
    assert honored["ok"] is True
    assert honored["snapshot"]["ok"] is False
    assert "OCTOPRINT_SNAPSHOT_URL" in honored["snapshot"]["message"]


def test_extra_argument_is_refused_without_the_value(tmp_path):
    script = Script(view("Printing"))
    out = load(control(deps(script, tmp_path)[0], {"action": "pause", "temperature": 200}))
    text = json.dumps(out)
    assert out["ok"] is False
    assert out["error_type"] == "bad_args"
    assert "for example action pause" in out["message"]
    assert "temperature" not in text
    assert "200" not in text
    assert script.calls == []


def test_missing_action_names_the_three_actions(tmp_path):
    script = Script(view("Printing"))
    out = load(control(deps(script, tmp_path)[0], {}))
    assert out["ok"] is False
    assert out["error_type"] == "bad_args"
    assert "pause, resume, or cancel" in out["message"]
    assert "for example action pause" in out["message"]
    assert script.calls == []


def test_numeric_flags_are_refused_without_the_number(tmp_path):
    for bad in (10.0, "１０"):
        script = Script(view("Printing"))
        out = load(status(deps(script, tmp_path)[0], {"include_snapshot": bad}))
        text = json.dumps(out, ensure_ascii=False)
        assert out["ok"] is False
        assert out["error_type"] == "bad_args"
        assert "true or false" in out["message"]
        assert "for example include_snapshot false" in out["message"]
        assert "10.0" not in text
        assert "１０" not in text
        assert script.calls == []
