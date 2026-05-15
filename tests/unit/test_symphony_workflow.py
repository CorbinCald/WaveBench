from __future__ import annotations

from pathlib import Path

import pytest

from symphony.config import load_dotenv, resolve_config
from symphony.errors import TemplateError, WorkflowError
from symphony.models import Issue
from symphony.workflow import load_workflow, render_prompt


def test_load_workflow_parses_front_matter_and_prompt(tmp_path: Path) -> None:
    workflow_path = tmp_path / "WORKFLOW.md"
    workflow_path.write_text(
        """---
tracker:
  kind: linear
  api_key: $LINEAR_API_KEY
  project_slug: demo-project
  active_states:
    - Todo
    - In Progress
  working_state: In Progress
  review_state: Human Review
  merging_state: Merging
  auto_transition: true
  post_status_comments: true
workspace:
  root: ./workspaces
git:
  enabled: true
  repo: $GIT_REPO_URL
  remote: upstream
  base_branch: trunk
  branch_prefix: auto
  rebase_policy: never
  push_on_merging: true
  pr_on_merging: true
hooks:
  after_create: |
    echo created
    pwd
agent:
  max_concurrent_agents_by_state:
    Todo: 1
    Bad: 0
pi:
  ingest_linear_images: true
  max_linear_images: 4
  max_linear_image_bytes: 12345
---
Hello {{ issue.identifier }} attempt={{ attempt }}
""",
        encoding="utf-8",
    )

    workflow = load_workflow(workflow_path)
    config = resolve_config(
        workflow,
        env={"LINEAR_API_KEY": "secret", "GIT_REPO_URL": "https://example.invalid/repo.git"},
    )

    assert workflow.config["tracker"]["active_states"] == ["Todo", "In Progress"]
    assert workflow.config["hooks"]["after_create"] == "echo created\npwd"
    assert workflow.prompt_template == "Hello {{ issue.identifier }} attempt={{ attempt }}"
    assert config.workspace_root == (tmp_path / "workspaces").resolve()
    assert config.tracker.api_key == "secret"
    assert config.tracker.working_state == "In Progress"
    assert config.tracker.review_state == "Human Review"
    assert config.tracker.merging_state == "Merging"
    assert config.tracker.auto_transition is True
    assert config.tracker.post_status_comments is True
    assert config.pi.command == "pi --mode rpc --no-session"
    assert config.pi.ingest_linear_images is True
    assert config.pi.max_linear_images == 4
    assert config.pi.max_linear_image_bytes == 12345
    assert config.git.enabled is True
    assert config.git.repo == "https://example.invalid/repo.git"
    assert config.git.remote == "upstream"
    assert config.git.base_branch == "trunk"
    assert config.git.branch_prefix == "auto"
    assert config.git.rebase_policy == "never"
    assert config.git.push_on_merging is True
    assert config.git.pr_on_merging is True
    assert config.agent.max_concurrent_agents_by_state == {"todo": 1}


def test_resolve_config_loads_workflow_adjacent_dotenv(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    workflow_path = tmp_path / "WORKFLOW.md"
    workflow_path.write_text(
        """---
tracker:
  kind: linear
  api_key: $LINEAR_API_KEY
  project_slug: demo-project
---
Prompt
""",
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text("LINEAR_API_KEY=secret-from-dotenv\n", encoding="utf-8")

    config = resolve_config(load_workflow(workflow_path))

    assert config.tracker.api_key == "secret-from-dotenv"


def test_load_dotenv_does_not_override_existing_env_values(tmp_path: Path) -> None:
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        """# local operator secrets
LINEAR_API_KEY=secret-from-file
export OTHER_KEY='other value'
""",
        encoding="utf-8",
    )
    env = {"LINEAR_API_KEY": "secret-from-env"}

    loaded = load_dotenv(dotenv_path, env)

    assert loaded == ["OTHER_KEY"]
    assert env["LINEAR_API_KEY"] == "secret-from-env"
    assert env["OTHER_KEY"] == "other value"


def test_load_workflow_without_front_matter_uses_empty_config(tmp_path: Path) -> None:
    workflow_path = tmp_path / "WORKFLOW.md"
    workflow_path.write_text("Just a prompt", encoding="utf-8")

    workflow = load_workflow(workflow_path)

    assert workflow.config == {}
    assert workflow.prompt_template == "Just a prompt"


def test_load_workflow_rejects_non_map_front_matter(tmp_path: Path) -> None:
    workflow_path = tmp_path / "WORKFLOW.md"
    workflow_path.write_text("---\n- nope\n---\nPrompt", encoding="utf-8")

    with pytest.raises(WorkflowError) as excinfo:
        load_workflow(workflow_path)

    assert excinfo.value.code == "workflow_front_matter_not_a_map"


def test_render_prompt_is_strict_and_supports_if_for() -> None:
    issue = Issue(
        id="1",
        identifier="WB-1",
        title="Add thing",
        state="Todo",
        description=None,
        labels=["bug", "cli"],
    )
    rendered = render_prompt(
        "{{ issue.identifier }} {% if issue.description %}{{ issue.description }}{% else %}none{% endif %}"
        "{% for label in issue.labels %} {{ label }}{% endfor %}",
        issue,
        attempt=2,
    )

    assert rendered == "WB-1 none bug cli"


def test_render_prompt_unknown_variable_fails() -> None:
    issue = Issue(id="1", identifier="WB-1", title="Add thing", state="Todo")

    with pytest.raises(TemplateError) as excinfo:
        render_prompt("{{ issue.missing }}", issue)

    assert excinfo.value.code == "template_render_error"
