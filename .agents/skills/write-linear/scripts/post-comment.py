#!/usr/bin/env python3
"""Post a comment to a Linear issue.

Uses LINEAR_API_KEY. The issue argument may be a UUID or an identifier like COR-5.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any

LINEAR_API = "https://api.linear.app/graphql"


def main() -> int:
    parser = argparse.ArgumentParser(description="Post a comment to a Linear issue.")
    parser.add_argument("--issue", required=True, help="Linear issue UUID or identifier, e.g. COR-5")
    body_group = parser.add_mutually_exclusive_group(required=True)
    body_group.add_argument("--body", help="Comment body")
    body_group.add_argument("--body-file", type=Path, help="Path to a markdown/text comment body")
    args = parser.parse_args()

    api_key = os.environ.get("LINEAR_API_KEY")
    if not api_key:
        print("LINEAR_API_KEY is required", file=sys.stderr)
        return 2

    body = args.body if args.body is not None else args.body_file.read_text(encoding="utf-8")
    issue_id = resolve_issue_id(api_key, args.issue)
    comment_url = create_comment(api_key, issue_id, body)
    print(comment_url or "comment posted")
    return 0


def resolve_issue_id(api_key: str, issue: str) -> str:
    payload = graphql(
        api_key,
        """
        query Issue($id: String!) {
          issue(id: $id) { id }
        }
        """,
        {"id": issue},
    )
    node = payload.get("data", {}).get("issue")
    if not node or not node.get("id"):
        raise RuntimeError(f"Linear issue not found: {issue}")
    return str(node["id"])


def create_comment(api_key: str, issue_id: str, body: str) -> str | None:
    payload = graphql(
        api_key,
        """
        mutation Comment($input: CommentCreateInput!) {
          commentCreate(input: $input) {
            success
            comment { url }
          }
        }
        """,
        {"input": {"issueId": issue_id, "body": body}},
    )
    result = payload["data"]["commentCreate"]
    if not result.get("success"):
        raise RuntimeError("Linear commentCreate failed")
    comment = result.get("comment") or {}
    return comment.get("url")


def graphql(api_key: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        LINEAR_API,
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Authorization": api_key, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(response.read().decode())
    if payload.get("errors"):
        raise RuntimeError(payload["errors"])
    return payload


if __name__ == "__main__":
    raise SystemExit(main())
