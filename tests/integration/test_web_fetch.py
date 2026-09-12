"""Exercise source reads over HTTP, including hostile URLs and tool delivery."""

from __future__ import annotations

import asyncio
import json
import socket

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from wavebench import web_fetch as module
from wavebench.harness.commands import Dispatcher
from wavebench.harness.config import Limits
from wavebench.web_search import BraveSearch


@pytest.fixture
async def source_server(monkeypatch):
    state = {
        "requests": [],
        "text": "Source text",
        "type": "text/plain",
        "status": 200,
        "headers": {},
    }

    async def page(request):
        state["requests"].append(request)
        if state.get("delay"):
            await asyncio.sleep(state["delay"])
        if request.path == "/redirect":
            return web.Response(
                status=302,
                headers={
                    "Location": state.get("location", "/page"),
                    "Set-Cookie": "private=do-not-forward",
                },
            )
        return web.Response(
            text=state["text"],
            content_type=state["type"],
            status=state["status"],
            headers=state["headers"],
        )

    async def resolve(self, host, port=0, family=socket.AF_INET):
        # Only this fixture hostname is mapped to the local HTTP server.
        if host != "source.test":
            raise module.FetchError("Source URLs must resolve only to public Internet addresses")
        return [
            {
                "hostname": host,
                "host": "127.0.0.1",
                "port": port,
                "family": socket.AF_INET,
                "proto": 0,
                "flags": 0,
            }
        ]

    monkeypatch.setattr(module.PublicResolver, "resolve", resolve)
    app = web.Application()
    app.router.add_get("/{path:.*}", page)
    async with TestServer(app) as server:
        state["url"] = f"http://source.test:{server.port}"
        yield state


async def test_html_tables_links_dates_and_redirects_without_credentials(
    source_server, monkeypatch
):
    source_server.update(
        type="text/html",
        text="""<!doctype html><html><head>
      <title>Model &amp; price reference</title>
      <meta property="article:published_time" content="2026-09-12">
      <meta property="article:modified_time" content="2026-09-13">
      <script>secret script</script><style>secret style</style></head><body>
      <nav>secret navigation</nav><main><h1>Current models</h1>
      <table><tr><th>Model</th><th>Price</th></tr><tr><td>Astra</td><td>$5</td></tr></table>
      <p>Read <a href="/method">the method</a>.</p>
      <p hidden>secret hidden</p><p style="display: none">secret hidden style</p>
      <script>secret body script</script></main></body></html>""",
        headers={"Last-Modified": "Sat, 12 Sep 2026 10:00:00 GMT"},
    )
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "secret-brave")
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret-router")
    result = await module.WebFetch().fetch(source_server["url"] + "/redirect")
    assert result["final_url"] == source_server["url"] + "/page"
    assert result["title"] == "Model & price reference"
    assert result["published_at"] == "2026-09-12"
    assert result["modified_at"] == "2026-09-13"
    assert result["last_modified"] == source_server["headers"]["Last-Modified"]
    assert result["fetched_at"].endswith("+00:00")
    assert "Model | Price" in result["content"] and "Astra | $5" in result["content"]
    assert f"the method ({source_server['url']}/method)" in result["content"]
    assert "secret" not in json.dumps(result)
    for request in source_server["requests"]:
        assert not {"Authorization", "Cookie", "X-Subscription-Token"} & request.headers.keys()


@pytest.mark.parametrize(
    "mime,text",
    [
        ("text/plain", "Prices\n$5 per task"),
        ("text/markdown", "# Prices\n[Source](/docs)"),
        ("application/json", '{"price": 5}'),
        ("application/ld+json", '{"name": "Astra"}'),
    ],
)
async def test_text_and_json_data_endpoints(source_server, mime, text):
    source_server.update(type=mime, text=text)
    result = await module.WebFetch().fetch(source_server["url"] + "/data")
    assert result["content"] == text
    assert result["content_type"] == mime


async def test_long_source_continuation_uses_same_snapshot(source_server):
    source_server["text"] = "αβγ\n" * 5000
    fetcher = module.WebFetch()
    first = await fetcher.fetch(source_server["url"], max_chars=12000)
    source_server["text"] = "Site changed during reading"
    second = await fetcher.fetch(source_server["url"], start=first["next_start"])
    assert first["content"] + second["content"] == "αβγ\n" * 5000
    assert second["next_start"] is None and not second["truncated"]
    assert not first["cached"] and second["cached"]
    assert first["fetched_at"] == second["fetched_at"]
    assert len(source_server["requests"]) == 1
    with pytest.raises(module.FetchError, match="exceeds"):
        await fetcher.fetch(source_server["url"], start=20001)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com",
        "http://user:pass@example.com",
        "http://127.0.0.1",
        "http://169.254.169.254/latest/meta-data",
        "http://10.0.0.1",
        "http://100.100.100.200",
        "http://[::1]",
        "http://[::ffff:127.0.0.1]",
        "http://224.0.0.1",
        "http://[ff02::1]",
        "http://[fe80::1%25eth0]",
        "https://example.com\nheader",
        None,
        "https://example.com:99999",
        "https://example.com/" + "a" * 4096,
    ],
)
async def test_nonpublic_or_invalid_urls_never_make_requests(source_server, url):
    with pytest.raises(module.FetchError):
        await module.WebFetch().fetch(url)
    assert source_server["requests"] == []


@pytest.mark.parametrize(
    "location",
    [
        "http://127.0.0.1/private",
        "http://[::1]/private",
        "file:///etc/passwd",
        "http://localhost/private",
        "https://user:pass@example.com",
        "/redirect",
    ],
)
async def test_redirects_are_revalidated_and_bounded(source_server, location):
    source_server["location"] = location
    with pytest.raises(module.FetchError, match=r"redirect|public"):
        await module.WebFetch().fetch(source_server["url"] + "/redirect")
    assert all(request.path == "/redirect" for request in source_server["requests"])
    assert len(source_server["requests"]) <= 6


@pytest.mark.parametrize(
    "addresses", [["127.0.0.1"], ["8.8.8.8", "10.0.0.1"], ["::ffff:127.0.0.1"], []]
)
async def test_dns_validation_rejects_private_and_mixed_answers(monkeypatch, addresses):
    async def resolve(*args):
        return [{"host": address} for address in addresses]

    resolver = module.PublicResolver()
    monkeypatch.setattr(resolver._resolver, "resolve", resolve)
    try:
        with pytest.raises(module.FetchError, match="public"):
            await resolver.resolve("example.com")
    finally:
        await resolver.close()


async def test_dns_connector_receives_only_validated_answers(monkeypatch):
    answers = [{"host": "8.8.8.8"}, {"host": "2606:4700:4700::1111"}]

    async def resolve(*args):
        return answers

    resolver = module.PublicResolver()
    monkeypatch.setattr(resolver._resolver, "resolve", resolve)
    try:
        assert await resolver.resolve("example.com") is answers
    finally:
        await resolver.close()


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500])
async def test_http_failures_are_safe_and_not_retried(source_server, status):
    source_server.update(status=status, text="secret upstream error")
    with pytest.raises(module.FetchError, match=f"HTTP {status}") as error:
        await module.WebFetch().fetch(source_server["url"])
    assert "secret" not in str(error.value)
    assert len(source_server["requests"]) == 1


async def test_unsupported_files_empty_js_shell_and_size_limit(source_server, monkeypatch):
    source_server.update(type="application/pdf", text="%PDF")
    with pytest.raises(module.FetchError, match="PDF"):
        await module.WebFetch().fetch(source_server["url"])
    source_server.update(
        type="text/html", text='<html><div id="root"></div><script>load()</script></html>'
    )
    with pytest.raises(module.FetchError, match="JavaScript"):
        await module.WebFetch().fetch(source_server["url"])
    source_server.update(type="text/plain", text="x" * 1001)
    monkeypatch.setattr(module, "MAX_RESPONSE_BYTES", 1000)
    with pytest.raises(module.FetchError, match="size limit"):
        await module.WebFetch().fetch(source_server["url"])


async def test_timeout_and_cancellation(source_server, monkeypatch):
    source_server["delay"] = 0.1
    monkeypatch.setattr(module, "FETCH_TIMEOUT", 0.01)
    with pytest.raises(module.FetchError, match="timed out"):
        await module.WebFetch().fetch(source_server["url"])
    monkeypatch.setattr(module, "FETCH_TIMEOUT", 1)
    task = asyncio.create_task(module.WebFetch().fetch(source_server["url"]))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.parametrize(
    "arguments",
    [
        {"start": -1},
        {"start": True},
        {"start": "0"},
        {"max_chars": 0},
        {"max_chars": 12001},
        {"max_chars": True},
    ],
)
async def test_invalid_sections_never_make_requests(source_server, arguments):
    with pytest.raises(module.FetchError):
        await module.WebFetch().fetch(source_server["url"], **arguments)
    assert source_server["requests"] == []


async def test_dispatch_budget_repair_replay_and_structured_continuation(source_server, tmp_path):
    source_server["text"] = '\\"\n' * 9000
    call = {"id": "read-1", "name": "web_fetch", "arguments": {"url": source_server["url"]}}
    disabled = Dispatcher(None, None, tmp_path, Limits())
    assert module.WEB_FETCH_SCHEMA not in disabled.tools
    assert "disabled" in (await disabled.batch([call]))[0]["error"]
    dispatcher = Dispatcher(
        None,
        None,
        tmp_path,
        Limits(web_fetch_calls=3, output_chars=1500),
        web_search=BraveSearch("secret-brave"),
    )
    assert module.WEB_FETCH_SCHEMA in dispatcher.tools
    first = (await dispatcher.batch([call]))[0]
    assert first["ok"] and first["truncated"]
    assert first["next_start"] == len(first["content"]) > 0
    assert len(json.dumps(first, ensure_ascii=False)) <= 1500
    assert (await dispatcher.batch([call]))[0] == first
    dispatcher.reopen()
    second = (
        await dispatcher.batch(
            [
                {
                    **call,
                    "id": "read-2",
                    "arguments": {**call["arguments"], "start": first["next_start"]},
                }
            ]
        )
    )[0]
    assert second["ok"] and second["start"] == len(first["content"])
    assert first["content"] + second["content"] == source_server["text"][: second["next_start"]]
    bad = {**call, "id": "read-3", "arguments": {"url": source_server["url"], "headers": {}}}
    assert not (await dispatcher.batch([bad]))[0]["ok"]
    assert "budget" in (await dispatcher.batch([{**call, "id": "read-4"}]))[0]["error"]
    assert dispatcher.web_fetch_usage == {"calls": 3, "failures": 1}
    assert dispatcher.web_search_usage == {"calls": 0, "failures": 0}
    assert len(source_server["requests"]) == 1
    assert "secret-brave" not in "".join(p.read_text() for p in tmp_path.glob("*.json"))
