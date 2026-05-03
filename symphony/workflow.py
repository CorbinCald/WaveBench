"""WORKFLOW.md loading and strict prompt rendering."""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from symphony.errors import TemplateError, WorkflowError
from symphony.models import Issue, WorkflowDefinition

_TOKEN_RE = re.compile(r"({{.*?}}|{%.*?%})", re.DOTALL)
_VAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")


def select_workflow_path(path: str | Path | None = None) -> Path:
    """Select the explicit workflow path or the cwd default."""

    if path is not None:
        return Path(path)
    return Path.cwd() / "WORKFLOW.md"


def load_workflow(path: str | Path | None = None) -> WorkflowDefinition:
    """Load WORKFLOW.md, parse optional YAML front matter, and return the prompt body."""

    workflow_path = select_workflow_path(path)
    try:
        text = workflow_path.read_text(encoding="utf-8")
        mtime_ns = workflow_path.stat().st_mtime_ns
    except OSError as exc:
        raise WorkflowError(
            "missing_workflow_file", f"could not read workflow file {workflow_path}"
        ) from exc

    config: dict[str, Any] = {}
    body = text
    if text.startswith("---"):
        lines = text.splitlines()
        end_index = None
        for index, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                end_index = index
                break
        if end_index is None:
            raise WorkflowError("workflow_parse_error", "front matter is missing closing ---")
        front_matter = "\n".join(lines[1:end_index])
        parsed = _parse_yaml_subset(front_matter)
        if parsed is None:
            parsed = {}
        if not isinstance(parsed, dict):
            raise WorkflowError(
                "workflow_front_matter_not_a_map", "front matter must decode to a map/object"
            )
        config = parsed
        body = "\n".join(lines[end_index + 1 :])

    return WorkflowDefinition(
        path=workflow_path,
        config=config,
        prompt_template=body.strip(),
        mtime_ns=mtime_ns,
    )


class WorkflowRuntime:
    """Keep the last known-good workflow/config and reload when WORKFLOW.md changes."""

    def __init__(self, path: str | Path | None = None):
        self.path = select_workflow_path(path)
        self.workflow: WorkflowDefinition | None = None
        self.last_error: Exception | None = None

    def load_initial(self) -> WorkflowDefinition:
        self.workflow = load_workflow(self.path)
        self.last_error = None
        return self.workflow

    def reload_if_changed(self) -> WorkflowDefinition | None:
        """Return a new workflow if mtime changed; keep old state on invalid reload."""

        previous = self.workflow
        try:
            mtime_ns = self.path.stat().st_mtime_ns
        except OSError:
            self.last_error = WorkflowError(
                "missing_workflow_file", f"could not stat workflow file {self.path}"
            )
            return None
        if previous is not None and previous.mtime_ns == mtime_ns:
            return None
        try:
            reloaded = load_workflow(self.path)
        except Exception as exc:
            self.last_error = exc
            return None
        self.workflow = reloaded
        self.last_error = None
        return reloaded


def render_prompt(template: str, issue: Issue, attempt: int | None = None) -> str:
    """Render a strict Liquid-like subset used by Symphony prompts."""

    if not template.strip():
        template = "You are working on an issue from Linear."
    context = {"issue": issue.to_template_data(), "attempt": attempt}
    tokens = _tokenize(template)
    return _render_tokens(tokens, context, 0, len(tokens))


def _parse_yaml_subset(text: str) -> Any:
    """Parse the YAML subset used by Symphony workflow front matter.

    This intentionally avoids adding a PyYAML dependency to WaveBench. It supports the
    structures used by Symphony config: nested maps, string lists, quoted/unquoted
    scalars, integers, booleans/null, inline lists, and literal blocks (``|``).
    """

    lines = text.splitlines()
    if not any(line.strip() and not line.lstrip().startswith("#") for line in lines):
        return {}
    try:
        value, index = _parse_yaml_block(lines, _next_content(lines, 0), _line_indent(lines[_next_content(lines, 0)]))
    except WorkflowError:
        raise
    except Exception as exc:  # defensive: present parser bugs as workflow parse errors
        raise WorkflowError("workflow_parse_error", str(exc)) from exc
    trailing = _next_content(lines, index)
    if trailing < len(lines):
        raise WorkflowError("workflow_parse_error", f"unexpected trailing YAML at line {trailing + 1}")
    return value


def _parse_yaml_block(lines: list[str], index: int, indent: int) -> tuple[Any, int]:
    if index >= len(lines):
        return {}, index
    stripped = lines[index].strip()
    if stripped.startswith("- "):
        return _parse_yaml_list(lines, index, indent)
    return _parse_yaml_map(lines, index, indent)


def _parse_yaml_map(lines: list[str], index: int, indent: int) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    while index < len(lines):
        index = _next_content(lines, index)
        if index >= len(lines):
            break
        line = lines[index]
        current_indent = _line_indent(line)
        if current_indent < indent:
            break
        if current_indent > indent:
            raise WorkflowError("workflow_parse_error", f"unexpected indentation at line {index + 1}")
        stripped = line.strip()
        if stripped.startswith("- "):
            break
        key, value_text = _split_yaml_key_value(stripped, index)
        if value_text in {"|", "|-", "|+"}:
            value, index = _parse_literal_block(lines, index + 1, indent)
        elif value_text == "":
            next_index = _next_content(lines, index + 1)
            if next_index >= len(lines) or _line_indent(lines[next_index]) <= indent:
                value, index = {}, index + 1
            else:
                value, index = _parse_yaml_block(lines, next_index, _line_indent(lines[next_index]))
        else:
            value, index = _parse_scalar(value_text), index + 1
        result[key] = value
    return result, index


def _parse_yaml_list(lines: list[str], index: int, indent: int) -> tuple[list[Any], int]:
    result: list[Any] = []
    while index < len(lines):
        index = _next_content(lines, index)
        if index >= len(lines):
            break
        line = lines[index]
        current_indent = _line_indent(line)
        if current_indent < indent:
            break
        if current_indent > indent:
            raise WorkflowError("workflow_parse_error", f"unexpected list indentation at line {index + 1}")
        stripped = line.strip()
        if not stripped.startswith("- "):
            break
        item_text = stripped[2:].strip()
        if item_text == "":
            next_index = _next_content(lines, index + 1)
            if next_index >= len(lines) or _line_indent(lines[next_index]) <= indent:
                value, index = None, index + 1
            else:
                value, index = _parse_yaml_block(lines, next_index, _line_indent(lines[next_index]))
        elif ":" in item_text and not item_text.startswith(("'", '"')):
            key, value_text = _split_yaml_key_value(item_text, index)
            value = {key: _parse_scalar(value_text) if value_text else {}}
            index += 1
            next_index = _next_content(lines, index)
            if next_index < len(lines) and _line_indent(lines[next_index]) > indent:
                extra, index = _parse_yaml_map(lines, next_index, _line_indent(lines[next_index]))
                if isinstance(extra, dict):
                    value.update(extra)
            result.append(value)
            continue
        else:
            value, index = _parse_scalar(item_text), index + 1
        result.append(value)
    return result, index


def _parse_literal_block(lines: list[str], index: int, parent_indent: int) -> tuple[str, int]:
    block_indent: int | None = None
    pieces: list[str] = []
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            pieces.append("")
            index += 1
            continue
        current_indent = _line_indent(line)
        if current_indent <= parent_indent:
            break
        if block_indent is None:
            block_indent = current_indent
        pieces.append(line[min(block_indent, len(line)) :])
        index += 1
    return "\n".join(pieces).rstrip("\n"), index


def _split_yaml_key_value(stripped_line: str, index: int) -> tuple[str, str]:
    if ":" not in stripped_line:
        raise WorkflowError("workflow_parse_error", f"expected key/value at line {index + 1}")
    key, value = stripped_line.split(":", 1)
    key = key.strip().strip("'\"")
    if not key:
        raise WorkflowError("workflow_parse_error", f"empty key at line {index + 1}")
    return key, value.strip()


def _parse_scalar(value: str) -> Any:
    value = _strip_inline_comment(value.strip())
    if value == "":
        return ""
    lowered = value.lower()
    if lowered in {"null", "nil", "~"}:
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if re.fullmatch(r"[-+]?\d+", value):
        return int(value)
    if (value.startswith("'") and value.endswith("'")) or (
        value.startswith('"') and value.endswith('"')
    ):
        try:
            return ast.literal_eval(value)
        except (SyntaxError, ValueError):
            return value[1:-1]
    if value.startswith("[") and value.endswith("]"):
        try:
            parsed = ast.literal_eval(value)
        except (SyntaxError, ValueError) as exc:
            raise WorkflowError("workflow_parse_error", f"invalid inline list: {value}") from exc
        if not isinstance(parsed, list):
            raise WorkflowError("workflow_parse_error", f"expected inline list: {value}")
        return parsed
    if value.startswith("{") and value.endswith("}"):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            try:
                return ast.literal_eval(value)
            except (SyntaxError, ValueError) as exc:
                raise WorkflowError("workflow_parse_error", f"invalid inline object: {value}") from exc
    return value


def _strip_inline_comment(value: str) -> str:
    in_single = False
    in_double = False
    for index, char in enumerate(value):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double and (index == 0 or value[index - 1].isspace()):
            return value[:index].rstrip()
    return value


def _next_content(lines: list[str], index: int) -> int:
    while index < len(lines):
        stripped = lines[index].strip()
        if stripped and not stripped.startswith("#"):
            return index
        index += 1
    return index


def _line_indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _tokenize(template: str) -> list[tuple[str, str]]:
    parts = _TOKEN_RE.split(template)
    tokens: list[tuple[str, str]] = []
    for part in parts:
        if not part:
            continue
        if part.startswith("{{"):
            tokens.append(("var", part[2:-2].strip()))
        elif part.startswith("{%"):
            tokens.append(("tag", part[2:-2].strip()))
        else:
            tokens.append(("text", part))
    return tokens


def _render_tokens(
    tokens: list[tuple[str, str]], context: dict[str, Any], start: int, end: int
) -> str:
    output: list[str] = []
    index = start
    while index < end:
        kind, value = tokens[index]
        if kind == "text":
            output.append(value)
            index += 1
        elif kind == "var":
            output.append(_stringify(_eval_expr(value, context)))
            index += 1
        elif value.startswith("if "):
            else_index, endif_index = _find_if_bounds(tokens, index, end)
            condition = bool(_eval_expr(value[3:].strip(), context))
            if condition:
                output.append(_render_tokens(tokens, context, index + 1, else_index or endif_index))
            elif else_index is not None:
                output.append(_render_tokens(tokens, context, else_index + 1, endif_index))
            index = endif_index + 1
        elif value.startswith("for "):
            var_name, expr = _parse_for_tag(value)
            endfor_index = _find_matching_end(tokens, index, end, "for ", "endfor")
            collection = _eval_expr(expr, context)
            if collection is None:
                collection = []
            if not isinstance(collection, list | tuple):
                raise TemplateError("template_render_error", f"for target is not a list: {expr}")
            for item in collection:
                child = dict(context)
                child[var_name] = item
                output.append(_render_tokens(tokens, child, index + 1, endfor_index))
            index = endfor_index + 1
        elif value in {"else", "endif", "endfor"}:
            raise TemplateError("template_parse_error", f"unexpected tag: {value}")
        else:
            raise TemplateError("template_parse_error", f"unsupported template tag: {value}")
    return "".join(output)


def _find_if_bounds(
    tokens: list[tuple[str, str]], start: int, end: int
) -> tuple[int | None, int]:
    depth = 0
    else_index: int | None = None
    for index in range(start + 1, end):
        kind, value = tokens[index]
        if kind != "tag":
            continue
        if value.startswith("if "):
            depth += 1
        elif value == "endif":
            if depth == 0:
                return else_index, index
            depth -= 1
        elif value == "else" and depth == 0:
            else_index = index
    raise TemplateError("template_parse_error", "if tag is missing endif")


def _find_matching_end(
    tokens: list[tuple[str, str]], start: int, end: int, opener: str, closer: str
) -> int:
    depth = 0
    for index in range(start + 1, end):
        kind, value = tokens[index]
        if kind != "tag":
            continue
        if value.startswith(opener):
            depth += 1
        elif value == closer:
            if depth == 0:
                return index
            depth -= 1
    raise TemplateError("template_parse_error", f"{opener.strip()} tag is missing {closer}")


def _parse_for_tag(tag: str) -> tuple[str, str]:
    match = re.fullmatch(r"for\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\s+(.+)", tag)
    if not match:
        raise TemplateError("template_parse_error", f"invalid for tag: {tag}")
    return match.group(1), match.group(2).strip()


def _eval_expr(expr: str, context: Mapping[str, Any]) -> Any:
    expr = expr.strip()
    if "|" in expr:
        raise TemplateError("template_render_error", f"unknown filter in expression: {expr}")
    if expr.startswith("not "):
        return not bool(_eval_expr(expr[4:].strip(), context))
    for operator in ("==", "!="):
        if operator in expr:
            left, right = expr.split(operator, 1)
            result = _eval_expr(left.strip(), context) == _eval_expr(right.strip(), context)
            return result if operator == "==" else not result
    if expr in {"null", "nil", "None"}:
        return None
    if expr in {"true", "True"}:
        return True
    if expr in {"false", "False"}:
        return False
    if (expr.startswith("'") and expr.endswith("'")) or (
        expr.startswith('"') and expr.endswith('"')
    ):
        return ast.literal_eval(expr)
    if re.fullmatch(r"[-+]?\d+", expr):
        return int(expr)
    if not _VAR_RE.fullmatch(expr):
        raise TemplateError("template_render_error", f"invalid expression: {expr}")
    current: Any = context
    for part in expr.split("."):
        if isinstance(current, Mapping):
            if part not in current:
                raise TemplateError("template_render_error", f"unknown variable: {expr}")
            current = current[part]
        else:
            if not hasattr(current, part):
                raise TemplateError("template_render_error", f"unknown variable: {expr}")
            current = getattr(current, part)
    return current


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool | int | float):
        return str(value)
    return json.dumps(value, default=str)
