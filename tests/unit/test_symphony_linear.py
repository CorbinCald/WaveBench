from __future__ import annotations

import pytest

from symphony.linear import LinearClient, _normalize_issue, linear_graphql_tool
from symphony.models import TrackerConfig


def test_normalize_issue_labels_priority_and_blockers() -> None:
    issue = _normalize_issue(
        {
            "id": "abc",
            "identifier": "WB-1",
            "title": "Title",
            "description": "Body",
            "priority": "2",
            "state": {"name": "Todo"},
            "labels": {"nodes": [{"name": "Bug"}, {"name": "CLI"}]},
            "comments": {
                "nodes": [
                    {
                        "id": "comment-old",
                        "body": "Older note",
                        "url": "https://linear.app/comment-old",
                        "createdAt": "2026-05-01T12:00:00.000Z",
                        "user": {"displayName": "Alice", "name": "alice"},
                    },
                    {
                        "id": "comment-new",
                        "body": "Latest note",
                        "url": "https://linear.app/comment-new",
                        "createdAt": "2026-05-02T12:00:00.000Z",
                        "botActor": {"name": "Symphony"},
                    },
                ]
            },
            "inverseRelations": {
                "nodes": [
                    {
                        "type": "blocks",
                        "issue": {"id": "b", "identifier": "WB-0", "state": {"name": "Done"}},
                    }
                ]
            },
        }
    )

    assert issue.labels == ["bug", "cli"]
    assert issue.priority == 2
    assert issue.blocked_by[0].identifier == "WB-0"
    assert [comment.body for comment in issue.comments] == ["Latest note", "Older note"]
    assert issue.comments[0].author == "Symphony"
    assert issue.comments[1].author == "Alice"


@pytest.mark.asyncio
async def test_fetch_issues_by_empty_states_returns_without_api_call() -> None:
    client = LinearClient(
        TrackerConfig("linear", "https://example.invalid", "secret", "demo", [], [])
    )

    assert await client.fetch_issues_by_states([]) == []


@pytest.mark.asyncio
async def test_linear_graphql_tool_rejects_multiple_operations() -> None:
    client = LinearClient(
        TrackerConfig("linear", "https://example.invalid", "secret", "demo", [], [])
    )

    result = await linear_graphql_tool(client, "query A { viewer { id } } query B { viewer { id } }")

    assert result["success"] is False
    assert "exactly one" in result["error"]


class WritebackClient(LinearClient):
    def __init__(self) -> None:
        super().__init__(TrackerConfig("linear", "https://example.invalid", "secret", "demo", [], []))
        self.calls: list[dict] = []

    async def graphql(self, query: str, variables: dict | None = None) -> dict:
        self.calls.append({"query": query, "variables": variables or {}})
        if "SymphonyIssueTeam" in query:
            return {"data": {"issue": {"team": {"id": "team-1"}}}}
        if "SymphonyWorkflowStates" in query:
            return {
                "data": {
                    "workflowStates": {
                        "nodes": [
                            {"id": "state-review", "name": "Human Review"},
                            {"id": "state-progress", "name": "In Progress"},
                        ]
                    }
                }
            }
        if "SymphonyIssueUpdate" in query:
            return {"data": {"issueUpdate": {"success": True}}}
        if "SymphonyCommentCreate" in query:
            return {"data": {"commentCreate": {"success": True}}}
        if "SymphonyAttachmentCreate" in query:
            return {"data": {"attachmentCreate": {"success": True}}}
        raise AssertionError(f"unexpected query: {query}")


@pytest.mark.asyncio
async def test_update_issue_state_resolves_state_name_and_updates_issue() -> None:
    client = WritebackClient()

    assert await client.update_issue_state("issue-1", "Human Review") is True

    assert client.calls[0]["variables"] == {"id": "issue-1"}
    assert client.calls[1]["variables"] == {"teamId": "team-1"}
    assert client.calls[2]["variables"] == {
        "id": "issue-1",
        "input": {"stateId": "state-review"},
    }


@pytest.mark.asyncio
async def test_linear_writeback_helpers_create_comment_description_and_attachment() -> None:
    client = WritebackClient()

    assert await client.create_comment("issue-1", "Ready for review") is True
    assert await client.update_issue_description("issue-1", "New description") is True
    assert await client.create_attachment("issue-1", "Video", "https://example.com/video.webm") is True

    assert client.calls[0]["variables"] == {
        "input": {"issueId": "issue-1", "body": "Ready for review"}
    }
    assert client.calls[1]["variables"] == {
        "id": "issue-1",
        "input": {"description": "New description"},
    }
    assert client.calls[2]["variables"] == {
        "input": {"issueId": "issue-1", "title": "Video", "url": "https://example.com/video.webm"}
    }
