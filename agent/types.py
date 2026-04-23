from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class ToolResult:
    name: str
    command: str
    ok: bool
    output: str
    started_at: str
    finished_at: str


@dataclass
class AgentStep:
    stage: str
    summary: str
    details: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())


@dataclass
class ChatTurn:
    user_message: str
    assistant_message: str
    namespace: str
    tool_calls: list[ToolResult]
    reasoning_steps: list[AgentStep]
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())


@dataclass
class ClusterWorkload:
    name: str
    namespace: str
    ready: str
    status: str
    restarts: str
    age: str


@dataclass
class ClusterSnapshot:
    namespace: str
    workloads: list[ClusterWorkload]
    fetched_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())


@dataclass
class SuggestedFix:
    title: str
    description: str
    resource: str
    namespace: str
    command_preview: str
    patch: dict[str, Any]


@dataclass
class PlannedToolCall:
    name: str
    params: dict[str, str]
    rationale: str


@dataclass
class AgentState:
    session_id: str
    question: str
    namespace: str
    intent: str = ""
    memory_summary: str = ""
    prefetched_context: str = ""
    runbook_matches: list[dict[str, Any]] = field(default_factory=list)
    tool_plan: list[PlannedToolCall] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    reasoning_steps: list[AgentStep] = field(default_factory=list)
    suggested_fixes: list[dict[str, Any]] = field(default_factory=list)
    response: str = ""
