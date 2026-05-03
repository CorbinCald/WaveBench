"""Linear GraphQL tracker adapter for Symphony."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from typing import Any

import aiohttp

from symphony.errors import TrackerError
from symphony.models import BlockerRef, Issue, IssueComment, TrackerConfig

_COMMENT_LIMIT = 12

_CANDIDATE_QUERY = """
query SymphonyCandidateIssues($projectSlug: String!, $states: [String!], $after: String) {
  issues(
    first: 50
    after: $after
    filter: {
      project: { slugId: { eq: $projectSlug } }
      state: { name: { in: $states } }
    }
  ) {
    nodes {
      id
      identifier
      title
      description
      priority
      branchName
      url
      createdAt
      updatedAt
      state { name }
      labels { nodes { name } }
      comments(first: 12, orderBy: createdAt) {
        nodes {
          id
          body
          url
          createdAt
          user { displayName name }
          botActor { name }
        }
      }
      inverseRelations { nodes { type issue { id identifier state { name } } } }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

_ISSUES_BY_STATE_QUERY = _CANDIDATE_QUERY

_ISSUES_BY_ID_QUERY = """
query SymphonyIssuesByIds($ids: [ID!]!) {
  issues(first: 100, filter: { id: { in: $ids } }) {
    nodes {
      id
      identifier
      title
      description
      priority
      branchName
      url
      createdAt
      updatedAt
      state { name }
      labels { nodes { name } }
      comments(first: 12, orderBy: createdAt) {
        nodes {
          id
          body
          url
          createdAt
          user { displayName name }
          botActor { name }
        }
      }
      inverseRelations { nodes { type issue { id identifier state { name } } } }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

_ISSUE_TEAM_QUERY = """
query SymphonyIssueTeam($id: String!) {
  issue(id: $id) {
    id
    team { id }
  }
}
"""

_WORKFLOW_STATES_QUERY = """
query SymphonyWorkflowStates($teamId: ID!) {
  workflowStates(first: 100, filter: { team: { id: { eq: $teamId } } }) {
    nodes { id name }
  }
}
"""

_ISSUE_UPDATE_MUTATION = """
mutation SymphonyIssueUpdate($id: String!, $input: IssueUpdateInput!) {
  issueUpdate(id: $id, input: $input) {
    success
    issue { id identifier state { name } }
  }
}
"""

_COMMENT_CREATE_MUTATION = """
mutation SymphonyCommentCreate($input: CommentCreateInput!) {
  commentCreate(input: $input) {
    success
    comment { id url }
  }
}
"""

_ATTACHMENT_CREATE_MUTATION = """
mutation SymphonyAttachmentCreate($input: AttachmentCreateInput!) {
  attachmentCreate(input: $input) {
    success
    attachment { id url }
  }
}
"""


class LinearClient:
    """Read Linear issues and expose raw GraphQL for optional tool use."""

    def __init__(self, config: TrackerConfig, timeout_ms: int = 30_000):
        self.config = config
        self.timeout = aiohttp.ClientTimeout(total=timeout_ms / 1000)
        self._state_id_cache: dict[tuple[str, str], str] = {}

    async def fetch_candidate_issues(self) -> list[Issue]:
        return await self.fetch_issues_by_states(self.config.active_states)

    async def fetch_issues_by_states(self, state_names: list[str]) -> list[Issue]:
        if not state_names:
            return []
        if not self.config.project_slug:
            raise TrackerError("missing_tracker_project_slug", "Linear project_slug is required")
        issues: list[Issue] = []
        after: str | None = None
        while True:
            payload = await self.graphql(
                _ISSUES_BY_STATE_QUERY,
                {
                    "projectSlug": self.config.project_slug,
                    "states": state_names,
                    "after": after,
                },
            )
            page = _extract_issues_page(payload)
            issues.extend(_normalize_issue(node) for node in page["nodes"])
            page_info = page.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            after = page_info.get("endCursor")
            if not after:
                raise TrackerError(
                    "linear_missing_end_cursor", "Linear pageInfo.hasNextPage was true without endCursor"
                )
        return issues

    async def fetch_issue_states_by_ids(self, issue_ids: list[str]) -> list[Issue]:
        if not issue_ids:
            return []
        payload = await self.graphql(_ISSUES_BY_ID_QUERY, {"ids": issue_ids})
        page = _extract_issues_page(payload)
        return [_normalize_issue(node) for node in page["nodes"]]

    async def update_issue_state(self, issue_id: str, state_name: str) -> bool:
        """Move a Linear issue to a workflow state by name."""
        state_id = await self._workflow_state_id_for_issue(issue_id, state_name)
        payload = await self.graphql(
            _ISSUE_UPDATE_MUTATION,
            {"id": issue_id, "input": {"stateId": state_id}},
        )
        return _mutation_success(payload, "issueUpdate")

    async def create_comment(self, issue_id: str, body: str) -> bool:
        """Post a Linear issue comment."""
        payload = await self.graphql(
            _COMMENT_CREATE_MUTATION,
            {"input": {"issueId": issue_id, "body": body}},
        )
        return _mutation_success(payload, "commentCreate")

    async def update_issue_description(self, issue_id: str, description: str) -> bool:
        """Replace a Linear issue description."""
        payload = await self.graphql(
            _ISSUE_UPDATE_MUTATION,
            {"id": issue_id, "input": {"description": description}},
        )
        return _mutation_success(payload, "issueUpdate")

    async def create_attachment(
        self, issue_id: str, title: str, url: str, subtitle: str | None = None
    ) -> bool:
        """Attach a URL artifact to a Linear issue."""
        attachment: dict[str, Any] = {"issueId": issue_id, "title": title, "url": url}
        if subtitle:
            attachment["subtitle"] = subtitle
        payload = await self.graphql(_ATTACHMENT_CREATE_MUTATION, {"input": attachment})
        return _mutation_success(payload, "attachmentCreate")

    async def _workflow_state_id_for_issue(self, issue_id: str, state_name: str) -> str:
        team_id = await self._issue_team_id(issue_id)
        cache_key = (team_id, state_name.lower())
        cached = self._state_id_cache.get(cache_key)
        if cached:
            return cached
        payload = await self.graphql(_WORKFLOW_STATES_QUERY, {"teamId": team_id})
        try:
            states = payload["data"]["workflowStates"]["nodes"]
        except (KeyError, TypeError) as exc:
            raise TrackerError(
                "linear_unknown_payload", "Linear payload missing workflowStates.nodes"
            ) from exc
        for state in states:
            if not isinstance(state, dict):
                continue
            name = str(state.get("name") or "")
            state_id = str(state.get("id") or "")
            if name.lower() == state_name.lower() and state_id:
                self._state_id_cache[cache_key] = state_id
                return state_id
        raise TrackerError(
            "linear_state_not_found",
            f"Linear workflow state not found for issue {issue_id}: {state_name}",
        )

    async def _issue_team_id(self, issue_id: str) -> str:
        payload = await self.graphql(_ISSUE_TEAM_QUERY, {"id": issue_id})
        try:
            team_id = payload["data"]["issue"]["team"]["id"]
        except (KeyError, TypeError) as exc:
            raise TrackerError("linear_unknown_payload", "Linear payload missing issue.team.id") from exc
        if not team_id:
            raise TrackerError("linear_unknown_payload", "Linear issue team id was empty")
        return str(team_id)

    async def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.config.api_key:
            raise TrackerError("missing_tracker_api_key", "Linear API key is required")
        body = {"query": query, "variables": variables or {}}
        headers = {
            "Authorization": self.config.api_key,
            "Content-Type": "application/json",
        }
        try:
            async with aiohttp.ClientSession(timeout=self.timeout) as session, session.post(
                self.config.endpoint, json=body, headers=headers
            ) as response:
                text = await response.text()
                if response.status != 200:
                    raise TrackerError(
                        "linear_api_status", f"Linear returned HTTP {response.status}: {text[:500]}"
                    )
                try:
                    payload = await response.json()
                except Exception as exc:
                    raise TrackerError("linear_unknown_payload", "Linear returned invalid JSON") from exc
        except asyncio.TimeoutError as exc:
            raise TrackerError("linear_api_request", "Linear request timed out") from exc
        except aiohttp.ClientError as exc:
            raise TrackerError("linear_api_request", f"Linear request failed: {exc}") from exc
        if payload.get("errors"):
            raise TrackerError("linear_graphql_errors", str(payload["errors"]))
        if not isinstance(payload, dict):
            raise TrackerError("linear_unknown_payload", "Linear payload was not an object")
        return payload


async def linear_graphql_tool(
    client: LinearClient, query_or_payload: str | dict[str, Any]
) -> dict[str, Any]:
    """Optional client-side tool contract for raw Linear GraphQL calls."""

    if isinstance(query_or_payload, str):
        query = query_or_payload
        variables: dict[str, Any] = {}
    elif isinstance(query_or_payload, dict):
        query = query_or_payload.get("query")
        variables = query_or_payload.get("variables") or {}
    else:
        return {"success": False, "error": "invalid input"}
    if not isinstance(query, str) or not query.strip():
        return {"success": False, "error": "query must be a non-empty string"}
    if not isinstance(variables, dict):
        return {"success": False, "error": "variables must be an object"}
    if _count_graphql_operations(query) != 1:
        return {"success": False, "error": "query must contain exactly one GraphQL operation"}
    try:
        payload = await client.graphql(query, variables)
    except TrackerError as exc:
        return {"success": False, "error": {"code": exc.code, "message": exc.message}}
    return {"success": True, "response": payload}


def _extract_issues_page(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        page = payload["data"]["issues"]
        nodes = page["nodes"]
    except (KeyError, TypeError) as exc:
        raise TrackerError("linear_unknown_payload", "Linear payload missing data.issues.nodes") from exc
    if not isinstance(nodes, list):
        raise TrackerError("linear_unknown_payload", "Linear data.issues.nodes was not a list")
    return page


def _mutation_success(payload: dict[str, Any], key: str) -> bool:
    try:
        success = payload["data"][key]["success"]
    except (KeyError, TypeError) as exc:
        raise TrackerError("linear_unknown_payload", f"Linear payload missing {key}.success") from exc
    return bool(success)


def _normalize_issue(node: dict[str, Any]) -> Issue:
    state = ((node.get("state") or {}).get("name")) or ""
    labels = [
        str(label.get("name", "")).lower()
        for label in ((node.get("labels") or {}).get("nodes") or [])
        if label.get("name")
    ]
    blockers: list[BlockerRef] = []
    for relation in ((node.get("inverseRelations") or {}).get("nodes") or []):
        if str(relation.get("type", "")).lower() != "blocks":
            continue
        blocker = relation.get("issue") or {}
        blockers.append(
            BlockerRef(
                id=blocker.get("id"),
                identifier=blocker.get("identifier"),
                state=(blocker.get("state") or {}).get("name"),
            )
        )
    return Issue(
        id=str(node.get("id") or ""),
        identifier=str(node.get("identifier") or ""),
        title=str(node.get("title") or ""),
        description=node.get("description"),
        priority=_normalize_priority(node.get("priority")),
        state=str(state),
        branch_name=node.get("branchName"),
        url=node.get("url"),
        labels=labels,
        blocked_by=blockers,
        comments=_normalize_comments(node),
        created_at=_parse_iso_datetime(node.get("createdAt")),
        updated_at=_parse_iso_datetime(node.get("updatedAt")),
    )


def _normalize_comments(node: dict[str, Any]) -> list[IssueComment]:
    comments: list[IssueComment] = []
    for raw_comment in ((node.get("comments") or {}).get("nodes") or []):
        if not isinstance(raw_comment, dict):
            continue
        user = raw_comment.get("user") or {}
        bot_actor = raw_comment.get("botActor") or {}
        author = (
            user.get("displayName")
            or user.get("name")
            or bot_actor.get("name")
            or None
        )
        comments.append(
            IssueComment(
                id=str(raw_comment.get("id") or ""),
                body=str(raw_comment.get("body") or ""),
                author=str(author) if author else None,
                url=raw_comment.get("url"),
                created_at=_parse_iso_datetime(raw_comment.get("createdAt")),
            )
        )
    comments.sort(
        key=lambda comment: comment.created_at or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    return comments[:_COMMENT_LIMIT]


def _normalize_priority(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_iso_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _count_graphql_operations(query: str) -> int:
    stripped_lines = []
    for line in query.splitlines():
        line = line.split("#", 1)[0]
        if line.strip():
            stripped_lines.append(line)
    stripped = "\n".join(stripped_lines)
    return len(re.findall(r"\b(query|mutation|subscription)\b", stripped))
