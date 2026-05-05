"""Planner phase: runs Claude with read-only tools and a terminal submit_plan
tool. Produces a validated Plan; never performs side effects."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable

from fda.organize import _fs
from fda.organize.models import Operation, OperationKind, Plan
from fda.organize.prompts import PLANNER_SYSTEM_PROMPT

logger = logging.getLogger(__name__)


PLANNER_MAX_ITERATIONS = 20


class PlannerDidNotSubmitError(Exception):
    """Raised when the planner loop ends without a successful submit_plan."""


_PLANNER_TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_directory",
        "description": "List entries in a directory with size and modified date.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "get_file_info",
        "description": "Get size, dates, MIME type, and git-repo status for a path.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a text file's contents (use sparingly).",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "submit_plan",
        "description": (
            "Submit the final organization plan. Call exactly once with a "
            "complete, validated set of operations. Validation is all-or-nothing."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "operations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {"enum": ["create_dir", "move", "delete"]},
                            "source": {"type": "string"},
                            "destination": {"type": "string"},
                            "reason": {"type": "string"},
                        },
                        "required": ["kind", "reason"],
                    },
                },
                "grouping_summary": {"type": "string"},
            },
            "required": ["operations", "grouping_summary"],
        },
    },
]


def _parse_operations(raw_ops: list[dict[str, Any]]) -> list[Operation]:
    parsed: list[Operation] = []
    for raw in raw_ops:
        kind = OperationKind(raw["kind"])
        parsed.append(Operation(
            kind=kind,
            source=raw.get("source"),
            destination=raw.get("destination"),
            reason=raw.get("reason", ""),
        ))
    return parsed


def build_plan(
    target: Path,
    instructions: str,
    *,
    backend,
    progress_callback: Callable[[str], None] | None = None,
) -> Plan:
    """Run the planner Claude loop and return a validated Plan.

    Raises PlannerDidNotSubmitError if the loop ends without a successful
    submit_plan call (e.g., iteration cap reached without submission).
    """
    state: dict[str, Any] = {"submitted": False, "plan": None}

    def _emit(msg: str) -> None:
        if progress_callback:
            try:
                progress_callback(f"planner: {msg}")
            except Exception:
                logger.debug("progress_callback raised", exc_info=True)

    def tool_executor(name: str, tinput: dict[str, Any]) -> str:
        if name == "list_directory":
            return _exec_list(target, tinput)
        if name == "get_file_info":
            return _exec_info(target, tinput)
        if name == "read_file":
            return _exec_read(target, tinput)
        if name == "submit_plan":
            return _exec_submit(target, tinput, state, instructions, _emit)
        return f"unknown tool: {name}"

    _emit(f"starting on {target}")
    backend.complete_with_tools(
        system=PLANNER_SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": (
                f"TARGET DIRECTORY: {target}\n\n"
                f"INSTRUCTIONS: {instructions or '(organize the files in this directory)'}\n\n"
                "Explore with the read-only tools, then call submit_plan exactly "
                "once with a complete, valid plan."
            ),
        }],
        tools=_PLANNER_TOOLS,
        tool_executor=tool_executor,
        max_iterations=PLANNER_MAX_ITERATIONS,
    )

    if not state["submitted"]:
        raise PlannerDidNotSubmitError(
            "Planner finished without successfully calling submit_plan"
        )

    return state["plan"]


# ---- read-only tool executors ------------------------------------------------

def _exec_list(target: Path, tinput: dict[str, Any]) -> str:
    rel = tinput.get("path", ".")
    full = (target / rel).resolve() if not Path(rel).is_absolute() else Path(rel).resolve()
    if not (full == target or full.is_relative_to(target)):
        return "error: path outside target"
    if not full.is_dir():
        return f"error: not a directory: {rel}"
    lines: list[str] = []
    for entry in sorted(full.iterdir()):
        if entry.name.startswith(".") and entry.name != ".git":
            continue
        suffix = "/" if entry.is_dir() else ""
        try:
            size = entry.stat().st_size
            lines.append(f"{entry.name}{suffix}\t{size} bytes")
        except OSError:
            lines.append(f"{entry.name}{suffix}\t(stat failed)")
    return "\n".join(lines) or "(empty)"


def _exec_info(target: Path, tinput: dict[str, Any]) -> str:
    p = Path(tinput.get("path", ""))
    if not p.is_absolute():
        p = (target / p).resolve()
    if not (p == target or p.is_relative_to(target)):
        return "error: path outside target"
    if not p.exists():
        return "error: not found"
    try:
        st = p.stat()
    except OSError as e:
        return f"error: {e}"
    info = {
        "name": p.name,
        "type": "directory" if p.is_dir() else "file",
        "size_bytes": st.st_size,
        "in_git_repo": _fs.is_inside_git_repo(p),
    }
    if p.is_dir():
        info["is_git_repo"] = (p / ".git").exists()
    return json.dumps(info)


def _exec_read(target: Path, tinput: dict[str, Any]) -> str:
    p = Path(tinput.get("path", ""))
    if not p.is_absolute():
        p = (target / p).resolve()
    if not (p == target or p.is_relative_to(target)):
        return "error: path outside target"
    if not p.is_file():
        return "error: not a file"
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"error: {e}"
    return text[:10000]


# ---- submit_plan executor ----------------------------------------------------

def _exec_submit(
    target: Path,
    tinput: dict[str, Any],
    state: dict[str, Any],
    instructions: str,
    emit: Callable[[str], None],
) -> str:
    raw_ops = tinput.get("operations", [])
    grouping = tinput.get("grouping_summary", "")
    try:
        operations = _parse_operations(raw_ops)
    except (KeyError, ValueError, TypeError) as e:
        return f"error: malformed operation: {e}"

    rejections: list[str] = []
    for idx, op in enumerate(operations):
        try:
            _fs.validate_operation(op, target)
        except ValueError as e:
            rejections.append(f"  [{idx}] {op.kind.value}: {e}")

    if rejections:
        emit(f"submit_plan rejected: {len(rejections)} invalid op(s)")
        return (
            "submit_plan rejected (validation is all-or-nothing). "
            "Fix every rejected op and resubmit:\n" + "\n".join(rejections)
        )

    state["submitted"] = True
    state["plan"] = Plan(
        target=str(target),
        instructions=instructions,
        operations=tuple(operations),
        grouping_summary=grouping,
    )
    emit(f"submit_plan accepted: {len(operations)} ops")
    return f"plan accepted ({len(operations)} operations)"
