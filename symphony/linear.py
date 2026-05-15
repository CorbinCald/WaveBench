"""Linear GraphQL tracker adapter for Symphony."""

from __future__ import annotations

import asyncio
import base64
import binascii
import html
import logging
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import unquote, urlparse

import aiohttp

from symphony.errors import TrackerError
from symphony.models import (
    BlockerRef,
    Issue,
    IssueComment,
    IssueImageRef,
    PromptImage,
    TrackerConfig,
)

_COMMENT_LIMIT = 12
_LOG = logging.getLogger(__name__)

_ALLOWED_IMAGE_MIME_TYPES = {"image/gif", "image/jpeg", "image/png", "image/webp"}
_IMAGE_EXTENSIONS = {".gif", ".jpeg", ".jpg", ".png", ".webp"}
_LINEAR_IMAGE_HOSTS = {"uploads.linear.app", "linearusercontent.com"}
_MARKDOWN_IMAGE_RE = re.compile(
    r"!\[(?P<alt>[^\]]*)\]\(\s*(?P<url><[^>]+>|[^\s)]+)(?:\s+(?:\"[^\"]*\"|'[^']*'))?\s*\)",
    re.IGNORECASE,
)
_HTML_IMG_RE = re.compile(r"<img\b(?P<attrs>[^>]*)>", re.IGNORECASE)
_HTML_ATTR_RE = re.compile(
    r"(?P<name>src|alt)\s*=\s*(?:\"(?P<double>[^\"]*)\"|'(?P<single>[^']*)'|(?P<bare>[^\s>]+))",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://[^\s<>()\"']+", re.IGNORECASE)
_DATA_IMAGE_RE = re.compile(
    r"^data:(?P<mime>image/[A-Za-z0-9.+-]+);base64,(?P<data>.+)$",
    re.IGNORECASE | re.DOTALL,
)

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
      attachments(first: 25) {
        nodes { id title subtitle url }
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
      attachments(first: 25) {
        nodes { id title subtitle url }
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

    async def fetch_issue_images(
        self, issue: Issue, max_images: int = 6, max_bytes: int = 8_000_000
    ) -> list[PromptImage]:
        """Download image URLs discovered in a Linear issue for Pi RPC."""
        if not issue.image_refs or max_images <= 0 or max_bytes <= 0:
            _LOG.info(
                "linear_image_ingestion_skipped issue_id=%s issue_identifier=%s refs=%s max_images=%s max_bytes=%s",
                issue.id,
                issue.identifier,
                len(issue.image_refs),
                max_images,
                max_bytes,
            )
            return []

        _LOG.info(
            "linear_image_ingestion_started issue_id=%s issue_identifier=%s refs=%s max_images=%s max_bytes=%s",
            issue.id,
            issue.identifier,
            len(issue.image_refs),
            max_images,
            max_bytes,
        )
        images: list[PromptImage] = []
        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            for image_ref in issue.image_refs:
                if len(images) >= max_images:
                    break
                image = await self._download_issue_image(session, image_ref, max_bytes)
                if image is not None:
                    images.append(image)
        _LOG.info(
            "linear_image_ingestion_completed issue_id=%s issue_identifier=%s refs=%s images=%s",
            issue.id,
            issue.identifier,
            len(issue.image_refs),
            len(images),
        )
        return images

    async def _download_issue_image(
        self, session: aiohttp.ClientSession, image_ref: IssueImageRef, max_bytes: int
    ) -> PromptImage | None:
        url = image_ref.url.strip()
        data_url = _prompt_image_from_data_url(url, image_ref, max_bytes)
        if data_url is not None:
            return data_url
        if not _is_http_url(url):
            return None

        headers = _image_request_headers(url, self.config.api_key)
        try:
            async with session.get(url, headers=headers) as response:
                if response.status != 200:
                    _LOG.info(
                        "linear_image_download_skipped status=%s source=%s url=%s",
                        response.status,
                        image_ref.source,
                        _safe_url_for_log(url),
                    )
                    return None
                content_type = response.headers.get("Content-Type")
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > max_bytes:
                    _LOG.info(
                        "linear_image_download_skipped reason=too_large content_length=%s source=%s url=%s",
                        content_length,
                        image_ref.source,
                        _safe_url_for_log(url),
                    )
                    return None
                data = await response.content.read(max_bytes + 1)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            _LOG.info(
                "linear_image_download_failed source=%s url=%s error=%s",
                image_ref.source,
                _safe_url_for_log(url),
                exc,
            )
            return None

        if len(data) > max_bytes:
            _LOG.info(
                "linear_image_download_skipped reason=too_large source=%s url=%s",
                image_ref.source,
                _safe_url_for_log(url),
            )
            return None
        mime_type = _normalize_image_mime(content_type, data)
        if mime_type is None:
            _LOG.info(
                "linear_image_download_skipped reason=not_supported_image source=%s url=%s",
                image_ref.source,
                _safe_url_for_log(url),
            )
            return None
        return PromptImage(
            data=base64.b64encode(data).decode("ascii"),
            mime_type=mime_type,
            url=url,
            alt=image_ref.alt,
            source=image_ref.source,
        )

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


def _extract_issue_image_refs(
    node: dict[str, Any], comments: list[IssueComment]
) -> list[IssueImageRef]:
    refs: list[IssueImageRef] = []
    seen: set[str] = set()

    description = node.get("description")
    if isinstance(description, str):
        _extract_text_image_refs(description, "description", refs, seen)

    for comment in comments:
        source = f"comment:{comment.id}" if comment.id else "comment"
        _extract_text_image_refs(comment.body, source, refs, seen)

    for attachment in ((node.get("attachments") or {}).get("nodes") or []):
        if not isinstance(attachment, dict):
            continue
        url = attachment.get("url")
        title = attachment.get("title") or attachment.get("subtitle")
        attachment_id = str(attachment.get("id") or "")
        source = f"attachment:{attachment_id}" if attachment_id else "attachment"
        _add_image_ref(refs, seen, url, _optional_text(title), source, explicit_image=False)

    return refs


def _extract_text_image_refs(
    text: str, source: str, refs: list[IssueImageRef], seen: set[str]
) -> None:
    for match in _MARKDOWN_IMAGE_RE.finditer(text):
        _add_image_ref(
            refs,
            seen,
            match.group("url"),
            _optional_text(match.group("alt")),
            source,
            explicit_image=True,
        )

    for match in _HTML_IMG_RE.finditer(text):
        attrs = _parse_img_attrs(match.group("attrs"))
        _add_image_ref(
            refs,
            seen,
            attrs.get("src"),
            attrs.get("alt"),
            source,
            explicit_image=True,
        )

    for match in _URL_RE.finditer(text):
        _add_image_ref(
            refs,
            seen,
            match.group(0),
            None,
            source,
            explicit_image=False,
        )


def _add_image_ref(
    refs: list[IssueImageRef],
    seen: set[str],
    raw_url: Any,
    alt: str | None,
    source: str,
    *,
    explicit_image: bool,
) -> None:
    url = _clean_image_url(raw_url)
    if url is None:
        return
    if not explicit_image and not _is_likely_image_url(url):
        return
    key = _dedupe_url_key(url)
    if key in seen:
        return
    seen.add(key)
    refs.append(IssueImageRef(url=url, alt=alt, source=source))


def _parse_img_attrs(attrs: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for match in _HTML_ATTR_RE.finditer(attrs):
        name = match.group("name").lower()
        value = match.group("double") or match.group("single") or match.group("bare") or ""
        text = _optional_text(html.unescape(value))
        if text is not None:
            parsed[name] = text
    return parsed


def _clean_image_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    url = html.unescape(value).strip().strip("<>").strip()
    url = url.rstrip(".,;:!?")
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme.lower() == "data":
        return url if _DATA_IMAGE_RE.match(url) else None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return None
    return url


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _dedupe_url_key(url: str) -> str:
    if url.lower().startswith("data:"):
        return url[:128]
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    host = parsed.netloc.lower()
    return parsed._replace(scheme=scheme, netloc=host).geturl()


def _is_http_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


def _is_likely_image_url(url: str) -> bool:
    if _DATA_IMAGE_RE.match(url):
        return True
    parsed = urlparse(url)
    path = unquote(parsed.path).lower()
    if any(path.endswith(extension) for extension in _IMAGE_EXTENSIONS):
        return True
    return _is_linear_image_host(parsed.netloc)


def _is_linear_image_host(host: str) -> bool:
    normalized = host.lower().split(":", 1)[0]
    return normalized in _LINEAR_IMAGE_HOSTS or any(
        normalized.endswith(f".{suffix}") for suffix in _LINEAR_IMAGE_HOSTS
    )


def _image_request_headers(url: str, api_key: str | None) -> dict[str, str]:
    headers = {"User-Agent": "WaveBench-Symphony/1.0"}
    if api_key and _is_linear_image_host(urlparse(url).netloc):
        headers["Authorization"] = api_key
    return headers


def _safe_url_for_log(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.netloc:
        return "<non-http-url>"
    return parsed._replace(query="", fragment="").geturl()


def _prompt_image_from_data_url(
    url: str, image_ref: IssueImageRef, max_bytes: int
) -> PromptImage | None:
    match = _DATA_IMAGE_RE.match(url)
    if match is None:
        return None
    mime_type = _normalize_image_mime(match.group("mime"), b"")
    if mime_type is None:
        return None
    try:
        data = base64.b64decode(match.group("data"), validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(data) > max_bytes:
        return None
    return PromptImage(
        data=base64.b64encode(data).decode("ascii"),
        mime_type=mime_type,
        url=url[:128],
        alt=image_ref.alt,
        source=image_ref.source,
    )


def _normalize_image_mime(content_type: str | None, data: bytes) -> str | None:
    raw = (content_type or "").split(";", 1)[0].strip().lower()
    if raw == "image/jpg":
        raw = "image/jpeg"
    sniffed = _sniff_image_mime(data)
    mime_type = sniffed or raw
    if mime_type in _ALLOWED_IMAGE_MIME_TYPES:
        return mime_type
    return None


def _sniff_image_mime(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _normalize_issue(node: dict[str, Any]) -> Issue:
    state = ((node.get("state") or {}).get("name")) or ""
    labels = [
        str(label.get("name", "")).lower()
        for label in ((node.get("labels") or {}).get("nodes") or [])
        if label.get("name")
    ]
    comments = _normalize_comments(node)
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
        comments=comments,
        created_at=_parse_iso_datetime(node.get("createdAt")),
        updated_at=_parse_iso_datetime(node.get("updatedAt")),
        image_refs=_extract_issue_image_refs(node, comments),
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
