"""Typed errors used by the Symphony implementation."""

from __future__ import annotations


class SymphonyError(Exception):
    """Base error with a stable machine-readable code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class WorkflowError(SymphonyError):
    """Raised for WORKFLOW.md load and parse failures."""


class ConfigError(SymphonyError):
    """Raised when workflow config cannot be resolved or validated."""


class TemplateError(SymphonyError):
    """Raised when strict prompt template parsing/rendering fails."""


class WorkspaceError(SymphonyError):
    """Raised for workspace safety, creation, or hook failures."""


class TrackerError(SymphonyError):
    """Raised for tracker integration failures."""


class AgentError(SymphonyError):
    """Raised for coding-agent worker failures."""
