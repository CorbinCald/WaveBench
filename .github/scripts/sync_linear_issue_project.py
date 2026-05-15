#!/usr/bin/env python3
"""Assign a synced Linear issue to a project based on its GitHub issue attachment."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any

LINEAR_API_URL = "https://api.linear.app/graphql"

FIND_ISSUE_QUERY = """
query FindIssueByGitHubAttachment($teamKey: String!, $after: String) {
  issues(
    first: 100
    after: $after
    orderBy: createdAt
    filter: { team: { key: { eq: $teamKey } } }
  ) {
    pageInfo {
      hasNextPage
      endCursor
    }
    nodes {
      id
      identifier
      url
      project {
        id
        name
      }
      attachments {
        nodes {
          url
          sourceType
        }
      }
    }
  }
}
"""

UPDATE_ISSUE_PROJECT_MUTATION = """
mutation AssignIssueProject($issueId: String!, $projectId: String!) {
  issueUpdate(id: $issueId, input: { projectId: $projectId }) {
    success
    issue {
      identifier
      url
      project {
        id
        name
      }
    }
  }
}
"""


def normalize_url(url: str) -> str:
    return url.strip().rstrip("/")


class LinearClient:
    def __init__(self, api_key: str, api_url: str = LINEAR_API_URL) -> None:
        self.api_key = api_key
        self.api_url = api_url

    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        request_body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
        request = urllib.request.Request(
            self.api_url,
            data=request_body,
            headers={
                "Authorization": self.api_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                response_body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            response_body = exc.read().decode("utf-8", "replace")
            raise RuntimeError(f"Linear API HTTP {exc.code}: {response_body}") from exc

        payload = json.loads(response_body)
        if payload.get("errors"):
            errors = json.dumps(payload["errors"], indent=2)
            raise RuntimeError(f"Linear API errors: {errors}")

        return payload["data"]


def issue_has_github_attachment(issue: dict[str, Any], github_issue_url: str) -> bool:
    attachments = issue.get("attachments", {}).get("nodes", [])
    for attachment in attachments:
        if attachment.get("sourceType") != "github":
            continue
        if normalize_url(attachment.get("url", "")) == github_issue_url:
            return True
    return False


def find_synced_linear_issue(
    client: LinearClient,
    *,
    team_key: str,
    github_issue_url: str,
    max_pages: int,
) -> dict[str, Any] | None:
    after = None
    for _ in range(max_pages):
        data = client.graphql(FIND_ISSUE_QUERY, {"teamKey": team_key, "after": after})
        issues = data["issues"]

        for issue in issues["nodes"]:
            if issue_has_github_attachment(issue, github_issue_url):
                return issue

        page_info = issues["pageInfo"]
        if not page_info["hasNextPage"]:
            return None
        after = page_info["endCursor"]

    return None


def assign_issue_project(
    client: LinearClient,
    *,
    issue_id: str,
    project_id: str,
) -> dict[str, Any]:
    data = client.graphql(
        UPDATE_ISSUE_PROJECT_MUTATION,
        {"issueId": issue_id, "projectId": project_id},
    )
    result = data["issueUpdate"]
    if not result["success"]:
        raise RuntimeError("Linear issueUpdate returned success=false")
    return result["issue"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Assign a Linear issue synced from GitHub to a Linear project."
    )
    parser.add_argument("--issue-url", required=True, help="GitHub issue URL to find in Linear")
    parser.add_argument("--team-key", required=True, help="Linear team key to search")
    parser.add_argument("--project-id", required=True, help="Linear project ID to assign")
    parser.add_argument("--retries", type=int, default=12, help="Number of Linear lookup attempts")
    parser.add_argument("--delay", type=float, default=5.0, help="Seconds between lookup attempts")
    parser.add_argument("--max-pages", type=int, default=3, help="Linear issue pages to scan")
    parser.add_argument("--dry-run", action="store_true", help="Find the issue without updating it")
    parser.add_argument("--linear-api-url", default=LINEAR_API_URL, help=argparse.SUPPRESS)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    api_key = os.environ.get("LINEAR_API_KEY")
    if not api_key:
        print("LINEAR_API_KEY is required", file=sys.stderr)
        return 2

    github_issue_url = normalize_url(args.issue_url)
    client = LinearClient(api_key, args.linear_api_url)

    issue = None
    for attempt in range(1, args.retries + 1):
        issue = find_synced_linear_issue(
            client,
            team_key=args.team_key,
            github_issue_url=github_issue_url,
            max_pages=args.max_pages,
        )
        if issue:
            break

        if attempt < args.retries:
            print(f"No synced Linear issue found for {github_issue_url}; retrying...")
            time.sleep(args.delay)

    if not issue:
        print(f"No synced Linear issue found for {github_issue_url}", file=sys.stderr)
        return 1

    project = issue.get("project")
    if project and project["id"] == args.project_id:
        print(f"{issue['identifier']} is already assigned to project {project['name']}")
        return 0

    print(f"Found synced Linear issue {issue['identifier']}: {issue['url']}")
    if args.dry_run:
        print(f"Dry run: would assign project {args.project_id}")
        return 0

    updated_issue = assign_issue_project(
        client,
        issue_id=issue["id"],
        project_id=args.project_id,
    )
    updated_project = updated_issue["project"]
    print(
        f"Assigned {updated_issue['identifier']} to project "
        f"{updated_project['name']}: {updated_issue['url']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
