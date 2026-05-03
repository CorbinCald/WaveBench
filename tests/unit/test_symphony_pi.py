from __future__ import annotations

import shlex
import sys
from pathlib import Path

import pytest

from symphony.agent import PiRpcClient
from symphony.models import Issue, PiConfig


@pytest.mark.asyncio
async def test_pi_rpc_client_runs_prompt_and_cancels_ui_dialog(tmp_path: Path) -> None:
    fake_pi = tmp_path / "fake_pi.py"
    fake_pi.write_text(
        r'''
import json
import sys


def send(payload):
    print(json.dumps(payload), flush=True)


for line in sys.stdin:
    message = json.loads(line)
    if message.get("type") == "get_state":
        send({
            "id": message.get("id"),
            "type": "response",
            "command": "get_state",
            "success": True,
            "data": {"sessionId": "session-1"},
        })
    elif message.get("type") == "prompt":
        send({
            "id": message.get("id"),
            "type": "response",
            "command": "prompt",
            "success": True,
        })
        send({"type": "extension_ui_request", "id": "ui-1", "method": "confirm", "title": "Continue?"})
        response = json.loads(sys.stdin.readline())
        assert response == {"type": "extension_ui_response", "id": "ui-1", "cancelled": True}
        send({"type": "agent_start"})
        send({"type": "turn_start"})
        send({
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": "done"},
        })
        send({
            "type": "agent_end",
            "messages": [
                {
                    "role": "assistant",
                    "usage": {"input": 10, "output": 5, "cacheRead": 1, "cacheWrite": 0},
                }
            ],
        })
    elif message.get("type") == "abort":
        send({"type": "response", "command": "abort", "success": True})
        break
''',
        encoding="utf-8",
    )
    root = tmp_path / "root"
    workspace = root / "WB-1"
    workspace.mkdir(parents=True)
    events: list[tuple[str, dict]] = []

    def on_event(name: str, payload: dict) -> None:
        events.append((name, payload))

    command = f"{shlex.quote(sys.executable)} {shlex.quote(str(fake_pi))}"
    client = PiRpcClient(
        PiConfig(command=command, read_timeout_ms=5000, turn_timeout_ms=5000),
        root,
        on_event,
    )
    issue = Issue(id="1", identifier="WB-1", title="Test", state="Todo")

    await client.start(workspace, issue)
    result = await client.run_turn("Implement the issue", workspace, issue)
    await client.stop()

    assert result.success is True
    assert [name for name, _payload in events] == [
        "session_started",
        "extension_ui_request",
        "agent_start",
        "turn_started",
        "message_update",
        "agent_end",
    ]
    assert events[-1][1]["usage"] == {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 16,
    }
