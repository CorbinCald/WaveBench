"""Exercise source reads over HTTP, including hostile URLs and tool delivery."""

from __future__ import annotations

import asyncio
import gzip
import json
import socket

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from yarl import URL

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
            **({"body": state["body"]} if "body" in state else {"text": state["text"]}),
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


async def test_large_compressed_hydration_keeps_table_and_bounds_extracted_text(source_server):
    html = (
        "<main><h1>Measured throughput</h1><table><tr><th>Model</th><th>tok/s</th></tr><tr><td>Astra</td><td>53.9</td></tr></table></main><script>"
        + "x" * 3_500_000
        + "</script>"
    )
    source_server.update(
        type="text/html", body=gzip.compress(html.encode()), headers={"Content-Encoding": "gzip"}
    )
    result = await module.WebFetch().fetch(source_server["url"])
    assert result["downloaded_bytes"] == len(html)
    assert "Astra | 53.9" in result["content"]
    assert result["total_chars"] < 100


async def test_compressed_bomb_and_incomplete_stream_are_rejected(source_server, monkeypatch):
    monkeypatch.setattr(module, "MAX_RESPONSE_BYTES", 100_000)
    body = gzip.compress(b"<script>" + b"x" * 1_000_000 + b"</script>")
    source_server.update(type="text/html", body=body, headers={"Content-Encoding": "gzip"})
    with pytest.raises(module.FetchError, match="size limit"):
        await module.WebFetch().fetch(source_server["url"])
    source_server["body"] = gzip.compress(b"<main>Evidence</main>")[:-5]
    with pytest.raises(module.FetchError, match="incomplete"):
        await module.WebFetch().fetch(source_server["url"])


async def test_html_preferred_over_incomplete_markdown_mirror(source_server):
    result = await module.WebFetch().fetch(source_server["url"])
    assert result["content"] == "Source text"
    accept = source_server["requests"][0].headers["Accept"]
    assert accept.startswith("text/html") and "text/markdown" not in accept


@pytest.mark.parametrize(
    "mime,text",
    [
        (
            "text/markdown",
            "# Runtime snapshot\n\n| Model | tok/s |\n| --- | --- |\n\n## Latency\n| Model | seconds |\n| --- | --- |\n",
        ),
        (
            "text/html",
            "<main><h1>Runtime snapshot</h1><table><tr><th>Model</th><th>tok/s</th></tr><tbody></tbody></table></main>",
        ),
        (
            "text/html",
            '<div><a href="/one">Documentation</a><a href="/two">API reference</a></div><iframe name="viewer"></iframe>',
        ),
    ],
)
async def test_empty_tables_and_navigation_are_insufficient_evidence(source_server, mime, text):
    source_server.update(type=mime, text=text)
    with pytest.raises(module.FetchError, match="Insufficient source evidence"):
        await module.WebFetch().fetch(source_server["url"])


async def test_main_region_and_anchor_select_requested_instructions(source_server):
    source_server.update(
        type="text/html",
        text="""<div class="sidebar">Irrelevant sidebar</div><main><h1>Guide</h1><h2 id="install">Installation</h2><pre>npm install three\nimport * as THREE from 'three';</pre><h2 id="advanced">Advanced</h2><p>Other instructions</p></main><div>Unrelated site text</div>""",
    )
    reader = module.WebFetch()
    full = await reader.fetch(source_server["url"])
    assert "Unrelated" not in full["content"] and "Irrelevant" not in full["content"]
    section = await reader.fetch(source_server["url"] + "#install", max_chars=20)
    rest = await reader.fetch(source_server["url"] + "#install", start=section["next_start"])
    assert "npm install three" in section["content"] + rest["content"]
    assert "Advanced" not in section["content"] + rest["content"]
    assert rest["cached"] and rest["next_start"] is None
    with pytest.raises(module.FetchError, match="section was not found"):
        await reader.fetch(source_server["url"] + "#manual/missing")


@pytest.mark.parametrize(
    "url,expected",
    [
        (
            "https://threejs.org/docs/#manual/en/introduction/Installation",
            "https://threejs.org/manual/pages/installation.html",
        ),
        (
            "https://threejs.org/manual/#en/installation",
            "https://threejs.org/manual/pages/installation.html",
        ),
        (
            "https://threejs.org/manual/#installation",
            "https://threejs.org/manual/pages/installation.html",
        ),
        (
            "https://example.org/docs/#manual/en/introduction/Installation",
            "https://example.org/docs/#manual/en/introduction/Installation",
        ),
    ],
)
def test_known_document_routes_preserve_provenance(url, expected):
    assert str(module.document_url(module.source_url(url))) == expected
    assert str(module.source_url(url)) == str(URL(url))


async def test_extracted_text_has_independent_limit(source_server, monkeypatch):
    monkeypatch.setattr(module, "MAX_TEXT_CHARS", 100)
    source_server.update(type="text/html", text="<main>" + "evidence " * 30 + "</main>")
    with pytest.raises(module.FetchError, match="Extracted source text"):
        await module.WebFetch().fetch(source_server["url"])


async def test_populated_markdown_table_remains_readable(source_server):
    source_server.update(
        type="text/markdown", text="| Model | tok/s |\n| --- | --- |\n| Astra | 53.9 |\n"
    )
    result = await module.WebFetch().fetch(source_server["url"])
    assert result["content"] == source_server["text"]


async def test_evidence_checks_apply_to_selected_main_and_section(source_server):
    source_server.update(
        type="text/html",
        text='<div><a href="/docs">Navigation</a></div>' * 100
        + '<main><h1 id="install">Installation</h1><pre>npm install three</pre><h1 id="data">Data</h1><table><tr><th>Model</th></tr></table></main>',
    )
    result = await module.WebFetch().fetch(source_server["url"] + "#install")
    assert "npm install three" in result["content"]
    assert "Navigation" not in result["content"]
    source_server["text"] = (
        '<main><a href="/docs">Documentation</a><a href="/api">API reference</a></main>'
    )
    with pytest.raises(module.FetchError, match="mostly navigation"):
        await module.WebFetch().fetch(source_server["url"])


async def test_large_element_count_is_bounded_before_extraction(source_server, monkeypatch):
    monkeypatch.setattr(module, "MAX_HTML_ELEMENTS", 100)
    source_server.update(type="text/html", text="<article><table></table></article>" * 101)
    with pytest.raises(module.FetchError, match="element limit"):
        await module.WebFetch().fetch(source_server["url"])
