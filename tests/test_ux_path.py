"""First request, a status read with no approval, and one pause approval."""
from __future__ import annotations

import json
import re
from pathlib import Path

from service import Deps, control, status

ROOT = Path(__file__).resolve().parents[1]
PHRASES = ("温度は", "進捗は", "印刷の様子", "一時停止", "見張りは")


def _deps(tmp_path, transport, asked) -> Deps:
    return Deps(
        url="http://printer.example:5000",
        api_key="test-api-key-value",
        snapshot_url="",
        data_dir=tmp_path,
        transport=transport,
        now=lambda: 1_000_000.0,
        sleep=lambda _seconds: None,
        approver=lambda text, rule: asked.append(text) or (True, ""),
        blocker=lambda action: None,
        write_guard=lambda path: None,
        control_timeout=0,
        stall_minutes=10,
        snapshot_max_bytes=2_000_000,
    )


def _transport(posted: dict):
    def transport(method, url, headers, body, timeout, cap):
        if method == "POST":
            posted["n"] += 1
            return 204, {"content-type": "application/json"}, b""
        if url.endswith("/api/connection"):
            payload = {
                "current": {"state": "Printing", "port": "VIRTUAL", "baudrate": 115200, "printerProfile": "_default"},
                "options": {},
            }
        elif url.endswith("/api/job"):
            state = "Paused" if posted["n"] else "Printing"
            payload = {
                "job": {"file": {"name": "tiny.gcode"}, "estimatedPrintTime": 6.7, "user": "_api"},
                "progress": {"completion": 10, "filepos": 100, "printTimeLeft": 5, "printTimeLeftOrigin": "linear"},
                "state": state,
            }
        else:
            payload = {
                "temperature": {"tool0": {"actual": 200.0, "target": 210.0}, "bed": {"actual": 60.0, "target": 60.0}},
                "state": {"text": "Printing", "flags": {"printing": True}},
            }
        return 200, {"content-type": "application/json"}, json.dumps(payload).encode()
    return transport


def test_skill_puts_each_japanese_request_on_an_english_search_line():
    text = (ROOT / "skills" / "octoprint-job" / "SKILL.md").read_text(encoding="utf-8")
    assert "tool_search" in text and "English" in text
    for phrase in PHRASES:
        line = next(row for row in text.splitlines() if phrase in row)
        assert "tool_search" in line and "OctoPrint" in line


def test_readme_first_request_has_five_steps_and_names_the_interface():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    plugin = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    section = text.split("## First request", 1)[1].split("\n## ", 1)[0]
    assert len(re.findall(r"(?m)^\d+\. ", section)) == 5
    assert "print_job_status" in section and "no arguments" in section
    assert "OctoPrint is a web interface for 3D printers" in plugin
    assert "can be used with OctoPrint" in plugin


def test_status_asks_nothing_and_a_pause_asks_once(tmp_path):
    asked = []
    deps = _deps(tmp_path, _transport({"n": 0}), asked)
    read = json.loads(status(deps, {}))
    assert read["ok"] is True and asked == []
    missing = json.loads(control(deps, {}))
    assert missing["ok"] is False and "action pause" in missing["message"] and asked == []
    moved = json.loads(control(deps, {"action": "pause"}))
    assert moved["ok"] is True and moved["moved"] is True and len(asked) == 1
