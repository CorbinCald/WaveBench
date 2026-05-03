#!/usr/bin/env python3
"""Upload a local evidence file to Linear and post it on an issue.

Uses LINEAR_API_KEY. The issue argument may be a UUID or an identifier like COR-5.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any

LINEAR_API = "https://api.linear.app/graphql"


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload a file to Linear and comment on an issue.")
    parser.add_argument("--issue", required=True, help="Linear issue UUID or identifier, e.g. COR-5")
    parser.add_argument("--file", required=True, type=Path, help="File to upload")
    parser.add_argument("--title", default="Interactive verification", help="Link title in Linear")
    parser.add_argument("--body", default="", help="Optional comment body before the file link")
    args = parser.parse_args()

    api_key = os.environ.get("LINEAR_API_KEY")
    if not api_key:
        print("LINEAR_API_KEY is required", file=sys.stderr)
        return 2

    file_path = args.file
    if not file_path.is_file():
        print(f"file not found: {file_path}", file=sys.stderr)
        return 2

    content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    issue_id = resolve_issue_id(api_key, args.issue)
    asset_url = upload_file(api_key, file_path, content_type)
    body = build_comment(args.body, args.title, asset_url, content_type)
    comment_url = create_comment(api_key, issue_id, body)
    print(comment_url or asset_url)
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


def upload_file(api_key: str, file_path: Path, content_type: str) -> str:
    size = file_path.stat().st_size
    payload = graphql(
        api_key,
        """
        mutation Upload($size: Int!, $contentType: String!, $filename: String!) {
          fileUpload(size: $size, contentType: $contentType, filename: $filename) {
            success
            uploadFile {
              uploadUrl
              assetUrl
              headers { key value }
            }
          }
        }
        """,
        {"size": size, "contentType": content_type, "filename": file_path.name},
    )
    upload = payload["data"]["fileUpload"]
    if not upload.get("success") or not upload.get("uploadFile"):
        raise RuntimeError("Linear fileUpload did not return an upload target")
    upload_file = upload["uploadFile"]
    headers = {header["key"]: header["value"] for header in upload_file.get("headers") or []}
    headers.setdefault("Content-Type", content_type)
    headers.setdefault("Cache-Control", "public, max-age=31536000")
    request = urllib.request.Request(
        upload_file["uploadUrl"],
        data=file_path.read_bytes(),
        headers=headers,
        method="PUT",
    )
    with urllib.request.urlopen(request, timeout=120):
        pass
    return str(upload_file["assetUrl"])


def build_comment(prefix: str, title: str, asset_url: str, content_type: str) -> str:
    if content_type.startswith("image/"):
        link = f"![{title}]({asset_url})"
    elif content_type.startswith("video/"):
        link = f"[{title}]({asset_url})"
    else:
        link = f"[{title}]({asset_url})"
    return f"{prefix.strip()}\n\n{link}".strip()


def create_comment(api_key: str, issue: str, body: str) -> str | None:
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
        {"input": {"issueId": issue, "body": body}},
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
