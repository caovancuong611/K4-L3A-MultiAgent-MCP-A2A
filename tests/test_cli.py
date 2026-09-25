from __future__ import annotations

from student_agent.cli import _is_transient_error


class MCPError(Exception):
    pass


def test_connection_mcp_errors_are_transient() -> None:
    assert _is_transient_error(MCPError("SSE stream ended without a response"))
    assert _is_transient_error(MCPError("Connection closed"))


def test_semantic_mcp_errors_are_not_transient() -> None:
    assert not _is_transient_error(MCPError("Invalid params"))


def test_nested_connection_error_is_transient() -> None:
    error = ExceptionGroup("transport", [MCPError("Connection closed")])
    assert _is_transient_error(error)
