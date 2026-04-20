from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AsyncIterator
import re

from agent.db import Database
from agent.fixer import FixApplicator
from agent.llm import LLMRouter
from agent.logging import StructuredRunLogger
from agent.memory import ConversationMemory
from agent.runbooks import RunbookLibrary
from agent.tools import ToolRegistry
from agent.types import AgentState, AgentStep, ChatTurn, PlannedToolCall


class OpsAssistant:
    def __init__(self, project_root: str | Path) -> None:
        self.project_root = Path(project_root)
        self.db = Database(self.project_root / "data" / "kuberon.db")
        self.tools = ToolRegistry()
        self.memory = ConversationMemory(
            redis_url=None if not self._env("REDIS_URL") else self._env("REDIS_URL"),
            sqlite_path=self.project_root / "data" / "kuberon.db",
        )
        self.llm = LLMRouter()
        self.logger = StructuredRunLogger(self.project_root / "logs")
        self.runbooks = RunbookLibrary(self.project_root / "runbooks")
        self.fixer = FixApplicator()

    async def stream_chat(self, session_id: str, question: str, namespace: str = "default") -> AsyncIterator[dict]:
        turns = await self.memory.get_turns(session_id, limit=6)
        state = AgentState(
            session_id=session_id,
            question=question,
            namespace=namespace,
            memory_summary=self.memory.summarize(turns),
        )

        for event in self._intent_node(state):
            yield event
        for event in self._retrieve_node(state):
            yield event
        for event in self._plan_node(state):
            yield event
        async for event in self._execute_node(state):
            yield event
        async for event in self._reason_node(state):
            yield event
        async for event in self._respond_node(state):
            yield event

        turn = ChatTurn(
            user_message=question,
            assistant_message=state.response,
            namespace=namespace,
            tool_calls=state.tool_results,
            reasoning_steps=state.reasoning_steps,
        )
        await self.memory.append_turn(session_id, turn)
        self.logger.append(
            session_id,
            {
                "created_at": turn.created_at,
                "session_id": session_id,
                "graph_state": {
                    "intent": state.intent,
                    "plan": [asdict(item) for item in state.tool_plan],
                    "runbooks": state.runbook_matches,
                    "fixes": state.suggested_fixes,
                },
                "turn": asdict(turn),
            },
        )
        yield {"type": "final", "payload": {"message": state.response}}

    def _intent_node(self, state: AgentState) -> list[dict]:
        state.intent = self._parse_intent(state.question)
        step = AgentStep(stage="intent", summary=f"Interpreted intent as '{state.intent}'", details={"namespace": state.namespace})
        state.reasoning_steps.append(step)
        return [{"type": "step", "payload": asdict(step)}, {"type": "graph", "payload": {"node": "intent", "status": "completed"}}]

    def _retrieve_node(self, state: AgentState) -> list[dict]:
        matches = self.runbooks.search(state.question)
        state.runbook_matches = [asdict(match) for match in matches]
        events = [{"type": "graph", "payload": {"node": "retrieve", "status": "completed", "match_count": len(matches)}}]
        if matches:
            step = AgentStep(
                stage="retrieve",
                summary=f"Matched {len(matches)} runbook(s) for context",
                details={"titles": [match.title for match in matches]},
            )
            state.reasoning_steps.append(step)
            events.append({"type": "step", "payload": asdict(step)})
            events.append({"type": "runbooks", "payload": state.runbook_matches})
        return events

    def _plan_node(self, state: AgentState) -> list[dict]:
        state.suggested_fixes = self.fixer.serialize(self.fixer.suggest(state.question, namespace=state.namespace))
        state.tool_plan = self._build_tool_plan(state.intent, state.question, state.namespace)
        step = AgentStep(
            stage="plan",
            summary=f"Prepared a {len(state.tool_plan)}-step tool plan",
            details={"intent": state.intent},
        )
        state.reasoning_steps.append(step)
        return [
            {"type": "step", "payload": asdict(step)},
            {"type": "plan", "payload": [asdict(item) for item in state.tool_plan]},
            {"type": "graph", "payload": {"node": "plan", "status": "completed", "tool_count": len(state.tool_plan)}},
        ]

    async def _execute_node(self, state: AgentState) -> AsyncIterator[dict]:
        attempted_tools = {planned_call.name for planned_call in state.tool_plan}
        failed_any = False
        dynamic_calls_added = False
        index = 0
        while index < len(state.tool_plan):
            planned_call = state.tool_plan[index]
            step = AgentStep(stage="execute", summary=f"Running tool '{planned_call.name}'", details=planned_call.params | {"rationale": planned_call.rationale})
            state.reasoning_steps.append(step)
            yield {"type": "step", "payload": asdict(step)}
            yield {"type": "graph", "payload": {"node": "execute", "status": "running", "tool": planned_call.name}}

            result = await self.tools.run(planned_call.name, **planned_call.params)
            state.tool_results.append(result)
            failed_any = failed_any or not result.ok
            observe_step = AgentStep(
                stage="observe",
                summary=f"Observed output from '{planned_call.name}'",
                details={"ok": result.ok, "command": result.command, "preview": result.output[:1200]},
            )
            state.reasoning_steps.append(observe_step)
            yield {"type": "tool_result", "payload": asdict(result)}
            yield {"type": "step", "payload": asdict(observe_step)}
            if planned_call.name == "get_events" and result.ok and state.intent == "memory" and not dynamic_calls_added:
                oom_event_calls: list[PlannedToolCall] = []
                for event in self._extract_oom_event_pods(result.output):
                    oom_event_calls.append(
                        PlannedToolCall(
                            "describe_pod",
                            {"namespace": state.namespace, "pod_name": event["pod_name"]},
                            f"OOMKilling events mentioned {event['pod_name']}, so describe it to confirm exit code 137 and termination state.",
                        )
                    )
                if oom_event_calls:
                    state.tool_plan.extend(oom_event_calls)
                    attempted_tools.update(call.name for call in oom_event_calls)
            if planned_call.name == "get_pods" and result.ok and not dynamic_calls_added:
                extra_calls = self._build_drilldown_calls(state, result.output, attempted_tools)
                if extra_calls:
                    state.tool_plan.extend(extra_calls)
                    attempted_tools.update(call.name for call in extra_calls)
                dynamic_calls_added = True
            index += 1

        if failed_any:
            fallback_limit = 4 if state.intent == "memory" else 2
            fallback_plan = self._build_fallback_plan(state, attempted_tools, limit=fallback_limit)
            for planned_call in fallback_plan:
                attempted_tools.add(planned_call.name)
                step = AgentStep(
                    stage="execute",
                    summary=f"Running fallback tool '{planned_call.name}'",
                    details=planned_call.params | {"rationale": planned_call.rationale, "fallback": True},
                )
                state.reasoning_steps.append(step)
                yield {"type": "step", "payload": asdict(step)}
                yield {"type": "graph", "payload": {"node": "execute", "status": "running", "tool": planned_call.name, "fallback": True}}

                result = await self.tools.run(planned_call.name, **planned_call.params)
                state.tool_results.append(result)
                observe_step = AgentStep(
                    stage="observe",
                    summary=f"Observed output from fallback tool '{planned_call.name}'",
                    details={"ok": result.ok, "command": result.command, "preview": result.output[:1200], "fallback": True},
                )
                state.reasoning_steps.append(observe_step)
                yield {"type": "tool_result", "payload": asdict(result)}
                yield {"type": "step", "payload": asdict(observe_step)}
        yield {"type": "graph", "payload": {"node": "execute", "status": "completed", "tool_count": len(state.tool_results)}}

    async def _reason_node(self, state: AgentState) -> AsyncIterator[dict]:
        observations = [f"{result.name}: {result.output[:2000]}" for result in state.tool_results if result.ok]
        llm_prompt = self.llm.format_reasoning_prompt(
            state.question,
            state.memory_summary,
            observations,
            [f"{match['title']}: {match['excerpt']}" for match in state.runbook_matches],
            [fix["command_preview"] for fix in state.suggested_fixes],
        )
        llm_result = await self.llm.reason(llm_prompt, prefer_fast=state.intent == "inventory")
        step = AgentStep(stage="reason", summary=f"Synthesized using {llm_result.provider}", details={"provider": llm_result.provider})
        state.reasoning_steps.append(step)
        state.suggested_fixes = self._filter_suggested_fixes(state.tool_results, state.suggested_fixes)
        state.response = self._format_response(state.question, llm_result.content, state.tool_results, state.runbook_matches, state.suggested_fixes, state.namespace)
        yield {"type": "step", "payload": asdict(step)}
        yield {"type": "graph", "payload": {"node": "reason", "status": "completed", "provider": llm_result.provider}}

    async def _respond_node(self, state: AgentState) -> AsyncIterator[dict]:
        for chunk in self._chunk_text(state.response, 140):
            yield {"type": "token", "payload": chunk}
        if state.suggested_fixes:
            yield {"type": "fixes", "payload": state.suggested_fixes}
        snapshot = await self.tools.snapshot(namespace=state.namespace)
        yield {"type": "snapshot", "payload": asdict(snapshot)}
        yield {"type": "graph", "payload": {"node": "respond", "status": "completed"}}

    def _parse_intent(self, question: str) -> str:
        lower = question.lower()
        if "what pods" in lower or "not running" in lower:
            return "inventory"
        if "memory" in lower or "oom" in lower or "high memory" in lower:
            return "memory"
        if "logs" in lower or "crash" in lower or "down" in lower or "failing" in lower:
            return "diagnose"
        if "fix" in lower or "suggest" in lower or "recover" in lower:
            return "recommend_fix"
        if "memory" in lower or "cpu" in lower or "usage" in lower:
            return "metrics"
        return "diagnose"

    def _build_tool_plan(self, intent: str, question: str, namespace: str) -> list[PlannedToolCall]:
        lower = question.lower()
        pod_hint = self._extract_workload_hint(lower)
        now = datetime.now(timezone.utc)
        one_hour_ago = now - timedelta(hours=1)

        if intent == "inventory":
            return [
                PlannedToolCall("get_pods", {"namespace": namespace}, "Inventory questions start with current pod status."),
                PlannedToolCall("get_events", {"namespace": namespace}, "Recent events explain why workloads are not running."),
            ]
        if intent == "memory":
            return [
                PlannedToolCall(
                    "get_events",
                    {"namespace": namespace, "field_selector": "reason=OOMKilling"},
                    "Memory and OOM questions must start with OOMKilling events from the target namespace.",
                ),
                PlannedToolCall(
                    "get_pods",
                    {"namespace": namespace},
                    "Next inspect pod status rows for OOMKilled, restart spikes, and workloads to drill into.",
                ),
                PlannedToolCall(
                    "get_resource_usage",
                    {"namespace": namespace, "sort_by": "memory"},
                    "Current pod memory usage confirms whether any workload is near its limit right now.",
                ),
            ]
        if intent == "metrics":
            return [
                PlannedToolCall("get_metrics", {"namespace": namespace}, "Live pod metrics reveal current resource hotspots."),
                PlannedToolCall(
                    "get_metrics_range",
                    {
                        "query": "sum(rate(container_cpu_usage_seconds_total[5m]))",
                        "start": f"{one_hour_ago.timestamp():.0f}",
                        "end": f"{now.timestamp():.0f}",
                        "step": "60s",
                    },
                    "Historical Prometheus data confirms whether the spike is sustained.",
                ),
            ]
        plan: list[PlannedToolCall] = [
            PlannedToolCall("get_pods", {"namespace": namespace}, "Pod status is the fastest first signal for service incidents."),
            PlannedToolCall("get_events", {"namespace": namespace}, "Events often reveal scheduling, image, or probe failures."),
        ]
        return plan

    @staticmethod
    def _extract_workload_hint(question: str) -> str:
        tokens = [token.strip(" ?.,!") for token in question.split()]
        for token in tokens:
            if token.endswith("service"):
                return token
        return ""

    @staticmethod
    def _parse_pod_rows(output: str) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for raw_line in output.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("NAME "):
                continue
            parts = re.split(r"\s{2,}|\t+|\s+", line)
            if len(parts) < 5:
                continue
            rows.append(
                {
                    "name": parts[0],
                    "ready": parts[1],
                    "status": parts[2],
                    "restarts": parts[3],
                    "age": parts[4],
                }
            )
        return rows

    def _build_drilldown_calls(self, state: AgentState, pod_output: str, attempted_tools: set[str]) -> list[PlannedToolCall]:
        service_hint = self._extract_workload_hint(state.question.lower())
        pod_rows = self._parse_pod_rows(pod_output)
        if service_hint:
            pod_rows = [row for row in pod_rows if service_hint.replace("-", "") in row["name"].lower().replace("-", "")]
        if state.intent == "memory":
            failing_rows = [
                row
                for row in pod_rows
                if "oomkilled" in row["status"].lower()
            ]
        else:
            failing_rows = [
                row
                for row in pod_rows
                if row["status"].lower() != "running" or row["ready"].startswith("0/")
            ]
        calls: list[PlannedToolCall] = []
        for row in failing_rows:
            pod_name = row["name"]
            calls.append(
                PlannedToolCall(
                    "describe_pod",
                    {"namespace": state.namespace, "pod_name": pod_name},
                    f"Drill down into failing pod {pod_name} for events, exit codes, and last state.",
                )
            )
            calls.append(
                PlannedToolCall(
                    "get_logs",
                    {"namespace": state.namespace, "pod_name": pod_name},
                    f"Inspect current logs for failing pod {pod_name}.",
                )
            )
            crash_like = row["status"].lower() in {"crashloopbackoff", "error"} or int(row["restarts"]) > 0
            if crash_like:
                calls.append(
                    PlannedToolCall(
                        "get_previous_logs",
                        {"namespace": state.namespace, "pod_name": pod_name},
                        f"Inspect previous logs for restarting pod {pod_name}.",
                    )
                )
        unique_calls: list[PlannedToolCall] = []
        seen = set()
        for call in calls:
            key = (call.name, call.params.get("pod_name", ""), call.params.get("namespace", ""))
            if key in seen:
                continue
            seen.add(key)
            unique_calls.append(call)
        return unique_calls

    @staticmethod
    def _build_fallback_plan(state: AgentState, attempted_tools: set[str], limit: int = 2) -> list[PlannedToolCall]:
        pod_hint = OpsAssistant._extract_workload_hint(state.question.lower())
        if state.intent == "memory":
            candidates = [
                PlannedToolCall(
                    "get_events",
                    {"namespace": state.namespace, "field_selector": "reason=OOMKilling"},
                    "Fallback 1 for memory questions is OOMKilling events in the namespace.",
                ),
                PlannedToolCall(
                    "get_pods",
                    {"namespace": state.namespace},
                    "Fallback 2 for memory questions is pod inventory to find OOMKilled workloads.",
                ),
                PlannedToolCall(
                    "get_resource_usage",
                    {"namespace": state.namespace, "sort_by": "memory"},
                    "Fallback 3 for memory questions is live memory usage by pod.",
                ),
            ]
            if pod_hint:
                candidates.append(
                    PlannedToolCall(
                        "describe_pod",
                        {"namespace": state.namespace, "pod_name": pod_hint},
                        "Fallback 4 for memory questions is describe output to confirm OOMKilled exit code 137.",
                    )
                )
            plan: list[PlannedToolCall] = []
            seen = set()
            for candidate in candidates:
                key = (candidate.name, tuple(sorted(candidate.params.items())))
                if key in seen or candidate.name in attempted_tools:
                    continue
                seen.add(key)
                plan.append(candidate)
                if len(plan) >= limit:
                    break
            return plan
        candidates = [
            PlannedToolCall("get_pods", {"namespace": state.namespace}, "Fallback to broad pod inventory when another diagnostic tool fails."),
            PlannedToolCall("get_events", {"namespace": state.namespace}, "Fallback to recent events to capture scheduling, image, or probe failures."),
            PlannedToolCall("get_metrics", {"namespace": state.namespace}, "Fallback to live metrics to validate whether resource pressure is involved."),
        ]
        if pod_hint:
            candidates.extend(
                [
                    PlannedToolCall(
                        "describe_pod",
                        {"namespace": state.namespace, "pod_name": pod_hint},
                        "Fallback to describe output for lifecycle evidence tied to the referenced workload.",
                    ),
                    PlannedToolCall(
                        "get_logs",
                        {"namespace": state.namespace, "pod_name": pod_hint},
                        "Fallback to logs so Kuberon can confirm startup or runtime failures before suggesting a fix.",
                    ),
                    PlannedToolCall(
                        "get_previous_logs",
                        {"namespace": state.namespace, "pod_name": pod_hint},
                        "Fallback to previous logs for restart loops and crash evidence.",
                    ),
                ]
            )
        plan: list[PlannedToolCall] = []
        for candidate in candidates:
            if candidate.name in attempted_tools:
                continue
            plan.append(candidate)
            if len(plan) >= limit:
                break
        return plan

    @staticmethod
    def _extract_oom_event_pods(output: str) -> list[dict[str, str]]:
        pods: list[dict[str, str]] = []
        for raw_line in output.splitlines():
            line = raw_line.strip()
            if not line or line.lower().startswith("last seen"):
                continue
            match = re.search(r"pod/([a-z0-9][a-z0-9-]*)", line, re.IGNORECASE)
            if not match:
                continue
            last_seen = line.split()[0]
            pods.append({"pod_name": match.group(1), "last_seen": last_seen, "line": line})
        return pods

    @staticmethod
    def _parse_metrics_rows(output: str) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for raw_line in output.splitlines():
            line = raw_line.strip()
            if not line or line.lower().startswith("name"):
                continue
            parts = re.split(r"\s{2,}|\t+|\s+", line)
            if len(parts) < 3:
                continue
            rows.append({"name": parts[0], "cpu": parts[1], "memory": parts[2]})
        return rows

    @staticmethod
    def _memory_to_mib(value: str) -> float | None:
        match = re.match(r"(?i)^\s*(\d+(?:\.\d+)?)(ki|mi|gi)?\s*$", value.strip())
        if not match:
            return None
        amount = float(match.group(1))
        unit = (match.group(2) or "mi").lower()
        factors = {"ki": 1 / 1024, "mi": 1, "gi": 1024}
        return amount * factors.get(unit, 1)

    @staticmethod
    def _parse_describe_oom(output: str) -> dict[str, str | bool]:
        lowered = output.lower()
        has_oom = "oomkilled" in lowered and re.search(r"Exit Code:\s*137", output) is not None
        finished_match = re.search(r"Finished:\s+(.+)", output)
        return {
            "confirmed": has_oom,
            "finished": finished_match.group(1).strip() if finished_match else "",
        }

    @staticmethod
    def _derive_workload_name(pod_name: str) -> str:
        parts = pod_name.split("-")
        if len(parts) >= 3:
            return "-".join(parts[:-2])
        if len(parts) >= 2:
            return "-".join(parts[:-1])
        return pod_name

    @staticmethod
    def _build_memory_fix_commands(pod_name: str, namespace: str) -> list[str]:
        workload = OpsAssistant._derive_workload_name(pod_name)
        patch = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": workload,
                                "resources": {
                                    "requests": {"memory": "256Mi"},
                                    "limits": {"memory": "512Mi"},
                                },
                            }
                        ]
                    }
                }
            }
        }
        patch_json = str(patch).replace("'", '"')
        return [
            f"kubectl -n {namespace} patch deployment/{workload} --type merge -p '{patch_json}'",
            f"kubectl -n {namespace} rollout undo deployment/{workload}",
        ]

    @staticmethod
    def _filter_suggested_fixes(tool_results: list, suggested_fixes: list[dict]) -> list[dict]:
        observed_text = "\n".join(result.output.lower() for result in tool_results if result.ok)
        filtered: list[dict] = []
        for fix in suggested_fixes:
            resource = (fix.get("resource") or "").split("/")[-1].lower()
            if resource and resource not in observed_text:
                continue
            filtered.append(fix)
        return filtered

    @staticmethod
    def _format_response(question: str, model_output: str, tool_results: list, runbook_matches: list, suggested_fixes: list, namespace: str = "default") -> str:
        if "memory" in question.lower() or "oom" in question.lower() or "high memory" in question.lower():
            return OpsAssistant._format_memory_response(question, model_output, tool_results, namespace)

        successful_results = [result for result in tool_results if result.ok]
        failed_tools = [result for result in tool_results if not result.ok]
        outputs = "\n".join(result.output for result in successful_results[:4]).lower()
        confirmed_markers = [
            "crashloopbackoff",
            "oomkilled",
            "imagepullbackoff",
            "errimagepull",
            "panic",
            "fatal",
            "back-off",
            "liveness probe failed",
            "readiness probe failed",
            "failed",
            "unhealthy",
            "deadline exceeded",
            "timeout",
            "unschedulable",
            "evicted",
        ]
        has_confirmed_issue = any(marker in outputs for marker in confirmed_markers)

        if not successful_results:
            missing = "\n".join(f"- `{result.name}` failed: {result.output[:220]}" for result in failed_tools[:4]) or "- No diagnostic tools could run."
            return (
                "## Tool Calls\n"
                f"{missing}\n\n"
                "## Severity\n"
                "Investigating\n\n"
                "## Findings\n"
                "- Kuberon could not collect trustworthy cluster evidence.\n"
                "- No service-specific conclusion was generated because every diagnostic tool failed.\n\n"
                "## Root Cause\n"
                "The current blocker is missing or unreachable tooling such as kubectl, cluster access, or metrics endpoints. Kuberon will not guess at workload health without real evidence.\n\n"
                "## Fix\n```bash\n"
                "# Check kubectl access\n"
                "kubectl cluster-info\n"
                "kubectl get pods -n default\n"
                "```\n\n"
                "## Follow-ups\n"
                "- Is kubectl configured against the right cluster?\n"
                "- Can you run kubectl get pods -n default manually?\n"
                "- Do you want me to help verify metrics-server or Prometheus?\n"
            )

        severity = "Investigating"
        if any(marker in outputs for marker in ["crashloopbackoff", "panic", "fatal", "oomkilled", "imagepullbackoff"]):
            severity = "Critical"
        elif any(marker in outputs for marker in ["failing", "back-off", "unhealthy", "failed", "timeout"]):
            severity = "High"
        elif has_confirmed_issue:
            severity = "Medium"

        findings: list[str] = []
        pod_rows = []
        for result in successful_results:
            if result.name == "get_pods":
                pod_rows = OpsAssistant._parse_pod_rows(result.output)
                for row in pod_rows:
                    if row["status"].lower() != "running" or row["ready"].startswith("0/"):
                        findings.append(f"{row['name']} -> {row['status']} ({row['restarts']} restarts, ready {row['ready']})")
        for result in successful_results[:4]:
            if result.name == "get_pods":
                continue
            preview = result.output.strip().replace("\r", " ").replace("\n", " ")
            if preview:
                findings.append(f"{result.name}: {preview[:220]}")
        if not findings:
            findings.append("Diagnostics returned limited evidence, so Kuberon is keeping the conclusion conservative.")

        if has_confirmed_issue:
            root_cause = model_output.strip() if model_output.strip() else "The captured evidence points to a workload startup, dependency readiness, or health-check problem, but the root cause remains partially inconclusive."
            if len(root_cause) > 560:
                root_cause = f"{root_cause[:557].rstrip()}..."
        else:
            root_cause = "Kuberon collected some cluster data, but not enough confirmed evidence to name a root cause yet. It is still investigating and needs stronger signals from describe output, events, logs, or metrics."

        fix_commands = [fix["command_preview"] for fix in suggested_fixes[:2] if fix.get("command_preview")] if has_confirmed_issue else []
        if not fix_commands:
            fix_commands = [
                "kubectl get pods -n default",
                "kubectl get events -n default --sort-by=.metadata.creationTimestamp",
            ]
            if has_confirmed_issue:
                fix_commands = ["Inspect the failing workload with describe/logs and confirm the deployment spec before applying a change."]

        next_questions = [
            "Show me all crashing pods",
            "What caused this?",
            "Check the payment service too",
        ]
        if "memory" in question.lower() or "oom" in outputs:
            next_questions = [
                "Show memory-related issues from the last hour",
                "Which pods restarted most recently?",
                "Check node pressure too",
            ]

        tool_call_lines = []
        for result in tool_results:
            status = "ok" if result.ok else "failed"
            tool_call_lines.append(f"- {result.name} ({status}) -> {result.command}")

        return (
            "## Tool Calls\n"
            + "\n".join(tool_call_lines)
            + "\n\n## Severity\n"
            + severity
            + "\n\n## Findings\n"
            + "\n".join(f"- {item}" for item in findings)
            + "\n\n## Root Cause\n"
            + root_cause
            + "\n\n## Fix\n```bash\n"
            + "\n".join(fix_commands)
            + "\n```\n\n## Follow-ups\n"
            + "\n".join(f"- {item}" for item in next_questions)
        )

    @staticmethod
    def _format_memory_response(question: str, model_output: str, tool_results: list, namespace: str = "default") -> str:
        tool_call_lines = [f"- {result.name} ({'ok' if result.ok else 'failed'}) -> {result.command}" for result in tool_results]
        successful_results = [result for result in tool_results if result.ok]
        failed_results = [result for result in tool_results if not result.ok]

        events_result = next((result for result in tool_results if result.name == "get_events"), None)
        pods_result = next((result for result in tool_results if result.name == "get_pods"), None)
        usage_result = next((result for result in tool_results if result.name in {"get_resource_usage", "get_metrics"}), None)
        describe_results = [result for result in tool_results if result.name == "describe_pod" and result.ok]

        findings: list[str] = []
        follow_ups = [
            "Show me all OOMKilled pods",
            "Which workloads restarted in the last hour?",
            "Check node memory pressure too",
        ]

        if not successful_results:
            manual_checks = "\n".join(
                [
                    "kubectl get events -n default --field-selector reason=OOMKilling --sort-by=.metadata.creationTimestamp",
                    "kubectl get pods -n default",
                    "kubectl top pods -n default --sort-by=memory",
                ]
            )
            return (
                "## Tool Calls\n"
                + ("\n".join(tool_call_lines) if tool_call_lines else "- No tool calls ran.")
                + "\n\n## Severity\nInvestigating\n\n## Findings\n"
                "- Kuberon could not retrieve memory diagnostics from the cluster.\n"
                + "\n## Root Cause\nCould not retrieve metrics. Please verify metrics-server and Prometheus are installed. Manual check:\n"
                f"```bash\n{manual_checks}\n```"
                + "\n\n## Fix\nNo remediation command is justified until the tooling gap is fixed.\n\n## Follow-ups\n"
                + "\n".join(f"- {item}" for item in follow_ups)
            )

        oom_events = OpsAssistant._extract_oom_event_pods(events_result.output) if events_result and events_result.ok else []
        if events_result and events_result.ok:
            if oom_events:
                for event in oom_events:
                    findings.append(f"OOMKilling event for {event['pod_name']} last seen {event['last_seen']}.")
            else:
                findings.append("No OOM events found in the namespace event stream.")

        pod_rows = OpsAssistant._parse_pod_rows(pods_result.output) if pods_result and pods_result.ok else []
        oom_rows = [row for row in pod_rows if "oomkilled" in row["status"].lower()]
        if pod_rows and oom_rows:
            for row in oom_rows:
                findings.append(f"{row['name']} -> {row['status']} ({row['restarts']} restarts, ready {row['ready']}).")
        elif pod_rows:
            findings.append("kubectl get pods does not show any pod currently in OOMKilled state.")

        usage_rows = OpsAssistant._parse_metrics_rows(usage_result.output) if usage_result and usage_result.ok else []
        if usage_rows:
            top_rows = usage_rows[:3]
            for row in top_rows:
                findings.append(f"{row['name']} is currently using {row['memory']} memory.")

        confirmed_oom_pods: list[dict[str, str]] = []
        for result in describe_results:
            pod_name = result.command.split("describe pod ", 1)[-1].split(" -n ", 1)[0].strip()
            parsed = OpsAssistant._parse_describe_oom(result.output)
            if parsed["confirmed"]:
                confirmed_oom_pods.append({"pod_name": pod_name, "finished": str(parsed["finished"])})
                finished = f" Finished at {parsed['finished']}." if parsed["finished"] else ""
                findings.append(f"{pod_name} last terminated with Reason OOMKilled and Exit Code 137.{finished}")

        usage_high = any((OpsAssistant._memory_to_mib(row["memory"]) or 0) >= 400 for row in usage_rows)
        no_oom_signals = not oom_events and not oom_rows and not confirmed_oom_pods

        if no_oom_signals and usage_rows and not usage_high:
            return (
                "## Tool Calls\n"
                + "\n".join(tool_call_lines)
                + "\n\n## Severity\nLow\n\n## Findings\n"
                + "\n".join(f"- {item}" for item in findings)
                + "\n\n## Root Cause\nNo OOM events found in the last hour. \nkubectl top pods also shows no pods near their limit.\nYour cluster appears memory-healthy right now."
                + "\n\n## Fix\nNo remediation needed right now.\n\n## Follow-ups\n"
                + "\n".join(f"- {item}" for item in follow_ups)
            )

        if confirmed_oom_pods:
            target_pod = confirmed_oom_pods[0]["pod_name"]
            fix_commands = OpsAssistant._build_memory_fix_commands(target_pod, namespace)
            root_cause = model_output.strip() or (
                f"{target_pod} is repeatedly terminating with OOMKilled exit code 137, which means the container exceeded its memory limit and Kubernetes restarted it."
            )
            return (
                "## Tool Calls\n"
                + "\n".join(tool_call_lines)
                + "\n\n## Severity\nHigh\n\n## Findings\n"
                + "\n".join(f"- {item}" for item in findings)
                + "\n\n## Root Cause\n"
                + root_cause
                + "\n\n## Fix\n```bash\n"
                + "\n".join(fix_commands)
                + "\n```\n\n## Follow-ups\n"
                + "\n".join(f"- {item}" for item in follow_ups)
            )

        if failed_results:
            return (
                "## Tool Calls\n"
                + "\n".join(tool_call_lines)
                + "\n\n## Severity\nInvestigating\n\n## Findings\n"
                + "\n".join(f"- {item}" for item in findings)
                + "\n\n## Root Cause\nCould not retrieve metrics. Please verify metrics-server and Prometheus are installed. Manual check:\n```bash\n"
                + "kubectl get events -n default --field-selector reason=OOMKilling --sort-by=.metadata.creationTimestamp\n"
                + "kubectl get pods -n default\n"
                + "kubectl top pods -n default --sort-by=memory\n"
                + "```\n\n## Fix\nNo remediation command is justified until memory diagnostics succeed.\n\n## Follow-ups\n"
                + "\n".join(f"- {item}" for item in follow_ups)
            )

        return (
            "## Tool Calls\n"
            + "\n".join(tool_call_lines)
            + "\n\n## Severity\nInvestigating\n\n## Findings\n"
            + "\n".join(f"- {item}" for item in findings)
            + "\n\n## Root Cause\nKuberon exhausted the OOM fallback chain without confirming an OOMKilled root cause. It kept investigating with events, pod status, live memory usage, and describe output, but there still is no confirmed memory failure."
            + "\n\n## Fix\nNo remediation command is justified until OOMKilled evidence is confirmed.\n\n## Follow-ups\n"
            + "\n".join(f"- {item}" for item in follow_ups)
        )

    @staticmethod
    def _chunk_text(text: str, size: int) -> list[str]:
        return [text[index : index + size] for index in range(0, len(text), size)] or [""]

    @staticmethod
    def _env(key: str) -> str | None:
        import os

        return os.getenv(key)
