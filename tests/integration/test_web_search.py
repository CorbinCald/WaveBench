"""Brave HTTP contract, bounded failures, and actual native-tool dispatch."""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from wavebench import web_search as module
from wavebench.harness.commands import TOOL_SCHEMA, Dispatcher
from wavebench.harness.config import Limits


@pytest.fixture
async def brave_server(monkeypatch):
    state = {
        "requests": [],
        "status": 200,
        "body": {
            "web": {
                "results": [
                    {
                        "title": "Example docs",
                        "url": "https://example.com/docs",
                        "description": "Current documentation.",
                    }
                ]
            }
        },
    }

    async def search(request):
        state["requests"].append({"headers": dict(request.headers), "query": dict(request.query)})
        if state.get("delay"):
            await asyncio.sleep(state["delay"])
        return web.json_response(state["body"], status=state["status"])

    app = web.Application()
    app.router.add_get("/search", search)
    async with TestServer(app) as server:
        monkeypatch.setattr(module, "BRAVE_URL", str(server.make_url("/search")))
        monkeypatch.setattr(module, "SEARCH_INTERVAL", 0)
        yield state


async def test_brave_request_and_clean_results(brave_server):
    result = await module.BraveSearch("test-brave-key").search(" Python docs ", count=2)
    request = brave_server["requests"][0]
    assert request["headers"]["X-Subscription-Token"] == "test-brave-key"
    assert "Authorization" not in request["headers"]
    assert request["query"] == {
        "q": "Python docs",
        "count": "2",
        "result_filter": "web",
        "text_decorations": "false",
    }
    assert result == {
        "provider": "brave",
        "query": "Python docs",
        "results": [
            {
                "title": "Example docs",
                "url": "https://example.com/docs",
                "description": "Current documentation.",
            }
        ],
    }


@pytest.mark.parametrize(
    "status,message",
    [(401, "rejected"), (403, "rejected"), (429, "quota"), (500, "HTTP 500"), (302, "HTTP 302")],
)
async def test_provider_errors_are_safe_and_not_retried(brave_server, status, message):
    brave_server.update(status=status, body={"error": "test-secret-do-not-log"})
    with pytest.raises(module.SearchError, match=message) as error:
        await module.BraveSearch("test-secret-do-not-log").search("docs")
    assert "test-secret" not in str(error.value)
    assert len(brave_server["requests"]) == 1


@pytest.mark.parametrize(
    "body", [[], {"web": []}, {"web": {"results": "bad"}}, {"web": {"results": [{}]}}]
)
async def test_malformed_results(brave_server, body):
    brave_server["body"] = body
    with pytest.raises(module.SearchError, match="invalid response"):
        await module.BraveSearch("test-key").search("docs")


async def test_no_results_and_response_limits(brave_server, monkeypatch):
    brave_server["body"] = {"query": {"original": "missing"}}
    assert (await module.BraveSearch("test-key").search("missing"))["results"] == []
    brave_server["body"] = {
        "web": {"results": [{"url": "https://example.com", "title": "x" * 1000}]}
    }
    result = await module.BraveSearch("test-key").search("docs")
    assert len(result["results"][0]["title"]) == 300
    monkeypatch.setattr(module, "MAX_RESPONSE_BYTES", 100)
    with pytest.raises(module.SearchError, match="size limit"):
        await module.BraveSearch("test-key").search("docs")


async def test_timeout_and_cancellation(brave_server, monkeypatch):
    brave_server["delay"] = 0.1
    monkeypatch.setattr(module, "SEARCH_TIMEOUT", 0.01)
    with pytest.raises(module.SearchError, match="timed out"):
        await module.BraveSearch("test-key").search("docs")
    monkeypatch.setattr(module, "SEARCH_TIMEOUT", 1)
    task = asyncio.create_task(module.BraveSearch("test-key").search("docs"))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize(
    "query,count",
    [
        ("", 5),
        (None, 5),
        ("x" * 601, 5),
        ("x " * 76, 5),
        ("docs\nheader", 5),
        ("docs", 0),
        ("docs", 11),
        ("docs", True),
    ],
)
async def test_invalid_arguments_make_no_requests(brave_server, query, count):
    with pytest.raises(module.SearchError):
        await module.BraveSearch("test-key").search(query, count)
    assert brave_server["requests"] == []


async def test_optional_dispatch_budget_replay_and_safe_logs(brave_server, tmp_path):
    disabled = Dispatcher(None, None, tmp_path, Limits())
    call = {"id": "search-1", "name": "web_search", "arguments": {"query": "docs"}}
    assert disabled.tools == TOOL_SCHEMA
    assert "disabled" in (await disabled.batch([call]))[0]["error"]
    assert not brave_server["requests"]

    dispatcher = Dispatcher(
        None,
        None,
        tmp_path,
        Limits(web_search_calls=2),
        web_search=module.BraveSearch("test-private-key"),
    )
    assert dispatcher.tools[-1] == module.WEB_SEARCH_SCHEMA
    first = await dispatcher.batch([call])
    assert first[0]["ok"] and first[0]["results"][0]["url"] == "https://example.com/docs"
    assert await dispatcher.batch([call]) == first
    assert len(brave_server["requests"]) == 1
    # A different tool name cannot replay a previous search response.
    assert not (await dispatcher.batch([{**call, "name": "wb"}]))[0]["ok"]
    brave_server["status"] = 429
    assert not (await dispatcher.batch([{**call, "id": "search-2"}]))[0]["ok"]
    dispatcher.reopen()  # Repair shares the same budget.
    assert "budget" in (await dispatcher.batch([{**call, "id": "search-3"}]))[0]["error"]
    assert dispatcher.web_search_usage == {"calls": 2, "failures": 1}
    assert len(brave_server["requests"]) == 2
    logs = "".join(path.read_text() for path in tmp_path.glob("tool-*.json"))
    assert "test-private-key" not in logs
    assert json.loads((tmp_path / "tool-0001.json").read_text())["tool"] == "web_search"
