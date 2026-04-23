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
        async for event in self._prefetch_node(state):
            yield event
        if state.intent in {"conceptual", "general"}:
            async for event in self._general_reason_node(state):
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
                        "plan": [],
                        "runbooks": [],
                        "fixes": [],
                    },
                    "turn": asdict(turn),
                },
            )
            yield {"type": "final", "payload": {"message": state.response}}
            return
        if state.intent == "mixed":
            for event in self._retrieve_node(state):
                yield event
            for event in self._plan_node(state):
                yield event
            async for event in self._execute_node(state):
                yield event
            async for event in self._mixed_reason_node(state):
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
                        "plan": [],
                        "runbooks": [],
                        "fixes": [],
                    },
                    "turn": asdict(turn),
                },
            )
            yield {"type": "final", "payload": {"message": state.response}}
            return
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
        completed = {(result.name, result.command) for result in state.tool_results}
        filtered_plan: list[PlannedToolCall] = []
        for item in state.tool_plan:
            command = self._planned_call_command(item)
            if (item.name, command) in completed:
                continue
            filtered_plan.append(item)
        state.tool_plan = filtered_plan
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
            state.prefetched_context,
            observations,
            [f"{match['title']}: {match['excerpt']}" for match in state.runbook_matches],
            [fix["command_preview"] for fix in state.suggested_fixes],
        )
        llm_result = await self.llm.reason(llm_prompt, prefer_fast=state.intent == "inventory")
        step = AgentStep(stage="reason", summary=f"Synthesized using {llm_result.provider}", details={"provider": llm_result.provider, "mode": "diagnostic"})
        state.reasoning_steps.append(step)
        state.suggested_fixes = self._filter_suggested_fixes(state.tool_results, state.suggested_fixes)
        state.response = self._format_response(state.question, llm_result.content, state.tool_results, state.runbook_matches, state.suggested_fixes, state.namespace)
        yield {"type": "step", "payload": asdict(step)}
        yield {"type": "graph", "payload": {"node": "reason", "status": "completed", "provider": llm_result.provider, "mode": "diagnostic"}}

    async def _general_reason_node(self, state: AgentState) -> AsyncIterator[dict]:
        if state.intent == "conceptual":
            llm_prompt = self.llm.format_concept_prompt(state.question, state.memory_summary)
        elif state.intent == "query":
            llm_prompt = self.llm.format_query_prompt(state.question, state.memory_summary)
        else:
            llm_prompt = self.llm.format_general_prompt(state.question, state.memory_summary)
        llm_result = await self.llm.reason(llm_prompt, prefer_fast=False)
        step = AgentStep(stage="reason", summary=f"Answered {state.intent} question using {llm_result.provider}", details={"provider": llm_result.provider, "mode": state.intent})
        state.reasoning_steps.append(step)
        if llm_result.provider == "heuristic":
            state.response = self._direct_answer_fallback(state.question, state.intent)
        else:
            state.response = llm_result.content.strip() or self._direct_answer_fallback(state.question, state.intent)
        yield {"type": "step", "payload": asdict(step)}
        yield {"type": "graph", "payload": {"node": "reason", "status": "completed", "provider": llm_result.provider, "mode": state.intent}}

    async def _mixed_reason_node(self, state: AgentState) -> AsyncIterator[dict]:
        observations = [f"{result.name}: {result.output[:2000]}" for result in state.tool_results if result.ok]
        llm_prompt = self.llm.format_mixed_prompt(
            state.question,
            state.memory_summary,
            state.prefetched_context,
            observations,
            [f"{match['title']}: {match['excerpt']}" for match in state.runbook_matches],
            [fix["command_preview"] for fix in state.suggested_fixes],
        )
        llm_result = await self.llm.reason(llm_prompt, prefer_fast=False)
        step = AgentStep(stage="reason", summary=f"Answered in mixed mode using {llm_result.provider}", details={"provider": llm_result.provider, "mode": "mixed"})
        state.reasoning_steps.append(step)
        state.suggested_fixes = self._filter_suggested_fixes(state.tool_results, state.suggested_fixes)
        diagnostic_block = self._format_response(
            state.question,
            llm_result.content,
            state.tool_results,
            state.runbook_matches,
            state.suggested_fixes,
            state.namespace,
        )
        concept_block = self._direct_answer_fallback(state.question, "conceptual")
        state.response = f"## Concept\n{concept_block}\n\n## Live Diagnosis\n{diagnostic_block}"
        yield {"type": "step", "payload": asdict(step)}
        yield {"type": "graph", "payload": {"node": "reason", "status": "completed", "provider": llm_result.provider, "mode": "mixed"}}

    async def _prefetch_node(self, state: AgentState) -> AsyncIterator[dict]:
        if state.intent in {"conceptual", "general"}:
            return
        forced_calls = self._get_forced_first_tools(state.question, state.namespace)
        if not forced_calls:
            return

        step = AgentStep(
            stage="prefetch",
            summary=f"Running {len(forced_calls)} forced first tool(s) before reasoning",
            details={"intent": state.intent, "tools": [call.name for call in forced_calls]},
        )
        state.reasoning_steps.append(step)
        yield {"type": "step", "payload": asdict(step)}
        yield {"type": "graph", "payload": {"node": "prefetch", "status": "running", "tool_count": len(forced_calls)}}

        seen_commands = {(result.name, result.command) for result in state.tool_results}
        prefetched_blocks: list[str] = []
        for planned_call in forced_calls:
            command = self._planned_call_command(planned_call)
            if (planned_call.name, command) in seen_commands:
                continue
            tool_step = AgentStep(
                stage="execute",
                summary=f"Forced first tool '{planned_call.name}'",
                details=planned_call.params | {"rationale": planned_call.rationale, "forced": True},
            )
            state.reasoning_steps.append(tool_step)
            yield {"type": "step", "payload": asdict(tool_step)}
            result = await self.tools.run(planned_call.name, **planned_call.params)
            state.tool_results.append(result)
            seen_commands.add((result.name, result.command))
            prefetched_blocks.append(f"=== {planned_call.name} ===\n{result.output}")
            observe_step = AgentStep(
                stage="observe",
                summary=f"Observed output from forced tool '{planned_call.name}'",
                details={"ok": result.ok, "command": result.command, "preview": result.output[:1200], "forced": True},
            )
            state.reasoning_steps.append(observe_step)
            yield {"type": "tool_result", "payload": asdict(result)}
            yield {"type": "step", "payload": asdict(observe_step)}
            if planned_call.name == "get_pods" and result.ok:
                extra_calls = self._build_drilldown_calls(state, result.output, set())
                extra_seen = set()
                for extra_call in extra_calls:
                    extra_key = (extra_call.name, self._planned_call_command(extra_call))
                    if extra_key in seen_commands or extra_key in extra_seen:
                        continue
                    extra_seen.add(extra_key)
                    detail_step = AgentStep(
                        stage="execute",
                        summary=f"Forced follow-up tool '{extra_call.name}'",
                        details=extra_call.params | {"rationale": extra_call.rationale, "forced": True},
                    )
                    state.reasoning_steps.append(detail_step)
                    yield {"type": "step", "payload": asdict(detail_step)}
                    detail_result = await self.tools.run(extra_call.name, **extra_call.params)
                    state.tool_results.append(detail_result)
                    seen_commands.add((detail_result.name, detail_result.command))
                    prefetched_blocks.append(f"=== {extra_call.name} ===\n{detail_result.output}")
                    detail_observe = AgentStep(
                        stage="observe",
                        summary=f"Observed output from forced follow-up '{extra_call.name}'",
                        details={"ok": detail_result.ok, "command": detail_result.command, "preview": detail_result.output[:1200], "forced": True},
                    )
                    state.reasoning_steps.append(detail_observe)
                    yield {"type": "tool_result", "payload": asdict(detail_result)}
                    yield {"type": "step", "payload": asdict(detail_observe)}
        if prefetched_blocks:
            state.prefetched_context = (
                "[Kuberon pre-fetched cluster data for you:]\n"
                + "\n\n".join(prefetched_blocks)
                + "\n\nUse this as your primary source. Do not ignore pod status rows. "
                "Any STATUS that is not Running is a problem. Any READY value starting with 0/ means the pod is down. "
                "If restarts are high, investigate further with describe output and logs."
            )
        yield {"type": "graph", "payload": {"node": "prefetch", "status": "completed", "tool_count": len(state.tool_results)}} 

    async def _respond_node(self, state: AgentState) -> AsyncIterator[dict]:
        for chunk in self._chunk_text(state.response, 140):
            yield {"type": "token", "payload": chunk}
        if state.intent not in {"conceptual", "general"} and state.suggested_fixes:
            yield {"type": "fixes", "payload": state.suggested_fixes}
        if state.intent not in {"conceptual", "general"}:
            snapshot = await self.tools.snapshot(namespace=state.namespace)
            yield {"type": "snapshot", "payload": asdict(snapshot)}
        yield {"type": "graph", "payload": {"node": "respond", "status": "completed"}}

    def _parse_intent(self, question: str) -> str:
        lower = question.lower()
        conceptual_leads = [
            "what is",
            "what are",
            "what does",
            "how does",
            "how do i",
            "why do",
            "why is",
            "define",
            "explain",
            "tell me about",
            "when to use",
            "what happens when",
            "is ",
            "difference between",
        ]
        query_leads = [
            "which",
            "how many",
            "show all",
            "analyze",
            "list",
        ]
        diagnostic_keywords = [
            "pod is",
            "app is",
            "crashing",
            "crash",
            "not running",
            "not reachable",
            "unreachable",
            "cannot connect",
            "service not working",
            "not accessible",
            "error",
            "fix",
            "debug",
            "issue",
            "problem",
            "failing",
            "show me",
            "check",
            "diagnose",
            "down",
            "oom",
            "memory issue",
            "pending",
            "imagepullbackoff",
            "crashloopbackoff",
            "restarts",
            "stuck",
            "keeps",
            "connection refused",
            "503",
            "502",
            "404",
        ]
        query_keywords = [
            "which",
            "how many",
            "show all",
            "analyze",
            "restarted",
            "usage",
            "trend",
            "trends",
            "metrics",
            "cpu",
            "memory",
            "top pods",
        ]
        kubernetes_concept_keywords = [
            "cluster",
            "node",
            "kubernetes",
            "pod",
            "deployment",
            "statefulset",
            "service",
            "namespace",
            "configmap",
            "secret",
            "ingress",
            "helm",
            "resource limits",
            "oomkilled",
            "crashloopbackoff",
        ]
        has_conceptual_shape = any(lower.startswith(lead) for lead in conceptual_leads)
        has_diagnostic_shape = any(keyword in lower for keyword in diagnostic_keywords)
        has_query_shape = any(lower.startswith(lead) for lead in query_leads) or any(keyword in lower for keyword in query_keywords)
        references_kubernetes_concept = any(keyword in lower for keyword in kubernetes_concept_keywords)
        describes_bad_state = any(
            phrase in lower
            for phrase in [
                "is not",
                "cannot",
                "can't",
                "keeps",
                "is stuck",
                "is failing",
                "is down",
                "is pending",
                "not reachable",
                "not working",
                "unreachable",
                "connection refused",
            ]
        )
        asks_broad_health = any(phrase in lower for phrase in ["any issues", "what is wrong", "cluster health"])

        if asks_broad_health:
            return "query"
        if describes_bad_state or has_diagnostic_shape:
            return "diagnose"
        if has_query_shape:
            return "query"
        if has_conceptual_shape and references_kubernetes_concept:
            return "conceptual"
        if has_conceptual_shape:
            return "general"
        if references_kubernetes_concept and any(phrase in lower for phrase in ["and how do i fix", "my pod", "my cluster", "keeps dying", "in my cluster"]):
            return "mixed"
        if "what pods" in lower or "not running" in lower:
            return "query"
        if "memory" in lower or "oom" in lower or "high memory" in lower:
            return "memory"
        if "logs" in lower or "crash" in lower or "down" in lower or "failing" in lower:
            return "diagnose"
        if "fix" in lower or "suggest" in lower or "recover" in lower:
            return "recommend_fix"
        if "memory" in lower or "cpu" in lower or "usage" in lower:
            return "query"
        return "general"

    def _get_forced_first_tools(self, question: str, namespace: str) -> list[PlannedToolCall]:
        lower = question.lower()
        crash_keywords = [
            "crash",
            "crashing",
            "crashloop",
            "not running",
            "down",
            "failing",
            "broken",
            "unhealthy",
            "restarts",
        ]
        memory_keywords = [
            "memory",
            "oom",
            "oomkilled",
            "out of memory",
            "ram",
            "mem",
        ]
        service_keywords = [
            "service",
            "endpoint",
            "connection",
            "unreachable",
            "timeout",
            "502",
            "503",
        ]

        if any(keyword in lower for keyword in crash_keywords):
            return [
                PlannedToolCall("get_pods", {"namespace": namespace}, "Crash and down queries must start with current pod status."),
                PlannedToolCall("get_events", {"namespace": namespace}, "Events provide secondary evidence after pod state is known."),
            ]
        if any(keyword in lower for keyword in memory_keywords):
            return [
                PlannedToolCall("get_pods", {"namespace": namespace}, "Memory investigations should first identify which pods are unhealthy."),
                PlannedToolCall("get_resource_usage", {"namespace": namespace, "sort_by": "memory"}, "Live memory usage is the second signal for OOM-style queries."),
            ]
        if any(keyword in lower for keyword in service_keywords):
            return [
                PlannedToolCall("get_pods", {"namespace": namespace}, "Service issues should still start with the health of backing pods."),
                PlannedToolCall("exec_kubectl", {"command": f"get svc -n {namespace}"}, "List services because this codebase does not expose a dedicated get_services tool."),
            ]
        return [
            PlannedToolCall("get_pods", {"namespace": namespace}, "Default prefetch always starts with the current pod overview."),
        ]

    def _build_tool_plan(self, intent: str, question: str, namespace: str) -> list[PlannedToolCall]:
        lower = question.lower()
        pod_hint = self._extract_workload_hint(lower)
        requested_issue = self._detect_requested_issue(lower)
        now = datetime.now(timezone.utc)
        one_hour_ago = now - timedelta(hours=1)

        if intent == "inventory":
            return [
                PlannedToolCall("get_pods", {"namespace": namespace}, "Inventory questions start with current pod status."),
                PlannedToolCall("get_events", {"namespace": namespace}, "Recent events explain why workloads are not running."),
            ]
        if intent == "query":
            if any(term in lower for term in ["usage", "cpu", "memory", "top pods"]):
                return [
                    PlannedToolCall("get_pods", {"namespace": namespace}, "Analysis questions still need current pod inventory."),
                    PlannedToolCall("get_metrics", {"namespace": namespace, "sort_by": "memory"}, "Usage analysis needs live metrics."),
                    PlannedToolCall("get_events", {"namespace": namespace}, "Events add context when interpreting workload health."),
                ]
            return [
                PlannedToolCall("get_pods", {"namespace": namespace}, "Query mode should analyze the current pod inventory."),
                PlannedToolCall("get_events", {"namespace": namespace}, "Recent events help explain restart counts and failures."),
            ]
        if intent == "memory":
            return [
                PlannedToolCall(
                    "get_pods",
                    {"namespace": namespace},
                    "Memory and OOM questions must start with pod state before reading event history.",
                ),
                PlannedToolCall(
                    "get_resource_usage",
                    {"namespace": namespace, "sort_by": "memory"},
                    "Current pod memory usage confirms whether any workload is near its limit right now.",
                ),
                PlannedToolCall(
                    "get_events",
                    {"namespace": namespace, "field_selector": "reason=OOMKilling"},
                    "OOMKilling events are useful after the pod inventory shows which workloads need attention.",
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
        if intent == "conceptual":
            return []
        if requested_issue == "PVC Pending":
            return [
                PlannedToolCall("exec_kubectl", {"command": f"get pvc -n {namespace}"}, "PVC questions must inspect claims directly."),
                PlannedToolCall("get_pods", {"namespace": namespace}, "Pod state confirms whether storage issues are blocking workloads."),
                PlannedToolCall("get_events", {"namespace": namespace}, "Events provide storage binding clues after PVC state is known."),
            ]
        plan: list[PlannedToolCall] = [
            PlannedToolCall("get_pods", {"namespace": namespace}, "Pod status is the fastest first signal for service incidents."),
            PlannedToolCall("get_events", {"namespace": namespace}, "Events often reveal scheduling, image, or probe failures."),
        ]
        return plan

    @staticmethod
    def _direct_answer_fallback(question: str, intent: str) -> str:
        lower = question.strip().lower()
        if "what is cluster" in lower or "what is a cluster" in lower or "what is kubernetes cluster" in lower:
            return (
                "📘 **Kubernetes Cluster**\n\n"
                "**What is it?**\n"
                "A Kubernetes cluster is a group of machines that work together to run containerized applications.\n\n"
                "**The simple analogy**\n"
                "Think of a cluster like a managed warehouse system. One team coordinates the work, and many worker spaces actually handle the packages.\n\n"
                "**How it works**\n"
                "A cluster usually has a control plane and one or more worker nodes. The control plane stores the desired state, decides where workloads should run, and keeps the system healthy.\n\n"
                "Worker nodes run the actual Pods. When you create a Deployment, Kubernetes schedules Pods onto nodes, monitors them, and recreates them if they fail.\n\n"
                "**Real example**\n"
                "```bash\n"
                "kubectl cluster-info\n"
                "kubectl get nodes\n"
                "```\n\n"
                "**When to use it**\n"
                "- Use a cluster when you want to run multiple containerized apps reliably.\n"
                "- Use it when you need self-healing, scaling, and service discovery.\n"
                "- Use it when teams or environments need workload isolation and automation.\n\n"
                "**Common mistakes**\n"
                "- Thinking a cluster is just one machine.\n"
                "- Confusing nodes, pods, and deployments.\n"
                "- Ignoring the difference between the control plane and worker nodes.\n\n"
                "**Related topics to learn next**\n"
                "- Node: a single machine inside the cluster.\n"
                "- Pod: the smallest runtime unit Kubernetes manages.\n"
                "- Deployment: the controller that manages replicated Pods.\n\n"
                "---\n"
                "*Ask me anything else — I answer concepts, cluster issues, and general tech ↗*"
            )
        if "what is node" in lower or "what is a node" in lower:
            return (
                "📘 **Node**\n\n"
                "**What is it?**\n"
                "A node is a single machine in a Kubernetes cluster that runs workloads or cluster control components.\n\n"
                "**The simple analogy**\n"
                "If a cluster is a warehouse system, a node is one building inside that warehouse where work actually happens.\n\n"
                "**How it works**\n"
                "Worker nodes run your application Pods. Each node has a kubelet agent that talks to the control plane and makes sure the right containers are running.\n\n"
                "Some clusters also have control plane nodes, which run the API server, scheduler, and controller manager. In smaller local clusters, one node may do both jobs.\n\n"
                "**Real example**\n"
                "```bash\n"
                "kubectl get nodes\n"
                "kubectl describe node <node-name>\n"
                "```\n\n"
                "**When to use it**\n"
                "- Learn about nodes when debugging scheduling or capacity issues.\n"
                "- Check nodes when Pods are Pending.\n"
                "- Inspect nodes when workloads seem slow or resource constrained.\n\n"
                "**Common mistakes**\n"
                "- Confusing a node with a pod.\n"
                "- Assuming all nodes are identical.\n"
                "- Ignoring node labels, taints, and resource limits.\n\n"
                "**Related topics to learn next**\n"
                "- Cluster: the full group of nodes managed by Kubernetes.\n"
                "- Scheduler: decides which node runs a Pod.\n"
                "- DaemonSet: runs one Pod on every node.\n\n"
                "---\n"
                "*Ask me anything else — I answer concepts, cluster issues, and general tech ↗*"
            )
        if "what is a pod" in lower or "what is pod" in lower or lower == "pod":
            return (
                "📘 **Pod**\n\n"
                "**What is it?**\n"
                "A Pod is the smallest unit Kubernetes runs, and it wraps one or more containers together.\n\n"
                "**The simple analogy**\n"
                "Think of a pod like a small apartment. The containers inside share the same address and can use the same local storage.\n\n"
                "**How it works**\n"
                "Kubernetes does not usually manage single containers directly. Instead, it schedules Pods onto nodes. A Pod gives the containers inside a shared network identity, so they can talk to each other over `localhost`.\n\n"
                "Pods are temporary. If a Pod dies, Kubernetes can replace it with a new one, often with a different IP. That is why important state should not live only inside the Pod filesystem.\n\n"
                "**Real example**\n"
                "```bash\n"
                "kubectl get pods -n default\n"
                "kubectl describe pod <pod-name> -n default\n"
                "```\n\n"
                "**When to use it**\n"
                "- Use Pods as the runtime unit for your application containers.\n"
                "- Use a single Pod when sidecar containers need to share the same network and storage.\n"
                "- In production, create Pods through Deployments or StatefulSets instead of by hand.\n\n"
                "**Common mistakes**\n"
                "- Treating Pods like permanent servers.\n"
                "- Storing important data only inside a Pod.\n"
                "- Creating raw Pods directly instead of using a controller.\n\n"
                "**Related topics to learn next**\n"
                "- Deployment: manages replicated Pods and rollouts.\n"
                "- Service: gives Pods a stable network address.\n"
                "- StatefulSet: manages stateful Pods with stable identity.\n\n"
                "---\n"
                "*Ask me anything else — I answer concepts, cluster issues, and general tech ↗*"
            )
        if "what is docker" in lower or lower == "docker":
            return (
                "📘 **Docker**\n\n"
                "**What is it?**\n"
                "Docker packages an application and its dependencies into a portable container image so it runs the same way on different machines.\n\n"
                "**The simple analogy**\n"
                "Docker is like a shipping container: the same box can move between different trucks, ships, and ports without changing what is inside.\n\n"
                "**How it works**\n"
                "You define how to build an image with a Dockerfile. That image includes your app code, runtime, libraries, and system dependencies.\n\n"
                "Once built, you can run the same image on your laptop, in CI, or on a server. That consistency is why Docker became so useful in modern development.\n\n"
                "**Real example**\n"
                "```bash\n"
                "docker build -t myapp:v1 .\n"
                "docker run -p 3000:3000 myapp:v1\n"
                "```\n\n"
                "**When to use it**\n"
                "- Use Docker for consistent development and deployment.\n"
                "- Use it when your app depends on specific runtimes or libraries.\n"
                "- Use it when you want a portable image artifact.\n\n"
                "**Common mistakes**\n"
                "- Putting secrets directly into the image.\n"
                "- Building very large images.\n"
                "- Confusing Docker with Kubernetes.\n\n"
                "**Related topics to learn next**\n"
                "- Dockerfile: defines how the image is built.\n"
                "- Container image: the packaged artifact Docker produces.\n"
                "- Kubernetes: runs containers at scale.\n\n"
                "---\n"
                "*Ask me anything else — I answer concepts, cluster issues, and general tech ↗*"
            )
        if "what is service" in lower or "what is a service" in lower:
            return (
                "📘 **Service**\n\n"
                "**What is it?**\n"
                "A Service is a stable network address in Kubernetes that routes traffic to Pods.\n\n"
                "**The simple analogy**\n"
                "Think of a Service like a receptionist desk. The desk stays in the same place even if the staff behind it changes.\n\n"
                "**How it works**\n"
                "Pod IPs can change whenever Pods restart or get replaced. A Service gives you one stable endpoint so other apps do not need to track individual Pod IPs.\n\n"
                "The Service chooses backend Pods using label selectors. If the selector does not match the Pod labels, the Service gets zero endpoints and traffic will fail.\n\n"
                "**Real example**\n"
                "```bash\n"
                "kubectl expose deployment myapp --port=80 --type=ClusterIP\n"
                "kubectl get svc myapp\n"
                "kubectl get endpoints myapp\n"
                "```\n\n"
                "**When to use it**\n"
                "- Use a Service when clients need a stable address for Pods.\n"
                "- Use it for internal service-to-service communication.\n"
                "- Use it when a Deployment may replace Pods often.\n\n"
                "**Common mistakes**\n"
                "- Forgetting that selectors must match Pod labels.\n"
                "- Assuming Pod IPs are stable enough to use directly.\n"
                "- Confusing Service exposure with Ingress routing.\n\n"
                "**Related topics to learn next**\n"
                "- Pod: the workload unit behind a Service.\n"
                "- Endpoints: the actual backend Pod targets for a Service.\n"
                "- Ingress: routes external HTTP traffic to Services.\n\n"
                "---\n"
                "*Ask me anything else — I answer concepts, cluster issues, and general tech ↗*"
            )
        if "what is python" in lower or lower == "python":
            return (
                "**Python**\n\n"
                "Python is a programming language known for readable syntax and fast development.\n\n"
                "It is widely used for automation, APIs, scripting, data analysis, and machine learning because it lets you write useful programs quickly without a lot of boilerplate.\n\n"
                "```python\n"
                "for name in [\"pod-a\", \"pod-b\"]:\n"
                "    print(name)\n"
                "```\n\n"
                "Ask me anything else — I answer concepts, cluster issues, and general tech ↗"
            )
        if intent == "diagnose":
            if "service" in lower and any(
                phrase in lower
                for phrase in ["not reachable", "unreachable", "not accessible", "not working", "cannot connect"]
            ):
                return (
                    "🔴 **Severity:** Medium\n\n"
                    "🔍 **Findings**\n"
                    "- The question describes a Service reachability problem, which usually points to unhealthy backing Pods or a selector mismatch.\n"
                    "- The most common production cause is a Service with zero endpoints because its selector does not match the running Pod labels.\n\n"
                    "🎯 **Root Cause**\n"
                    "A Kubernetes Service can be present and still fail if it has no healthy endpoints behind it. That usually happens when the selector labels are wrong or when the targeted Pods are not Ready.\n\n"
                    "🛠️ **Fix**\n"
                    "```bash\n"
                    "kubectl patch service <service-name> -n default -p '{\"spec\":{\"selector\":{\"app\":\"<correct-label>\"}}}'\n"
                    "kubectl rollout undo deployment/<deployment-name> -n default\n"
                    "```\n\n"
                    "💬 **Follow-ups**\n"
                    "- Show me the service endpoints\n"
                    "- Check the backing pods for this service\n"
                    "- Compare the service selector with pod labels\n"
                )
            return (
                "🔴 **Severity:** Low\n\n"
                "🔍 **Findings**\n"
                "- A live cluster issue was described, but no concrete failing pod, event, or log evidence is available in this fallback path.\n\n"
                "🎯 **Root Cause**\n"
                "There is not enough runtime evidence in the current response path to confirm the exact root cause yet. The next step is to inspect pods, service endpoints, and events from the cluster.\n\n"
                "🛠️ **Fix**\n"
                "```bash\n"
                "kubectl get endpoints <service-name> -n default\n"
                "kubectl patch service <service-name> -n default -p '{\"spec\":{\"selector\":{\"app\":\"<correct-label>\"}}}'\n"
                "```\n\n"
                "💬 **Follow-ups**\n"
                "- Show me all non-running pods\n"
                "- Check the service endpoints too\n"
                "- Inspect the related deployment labels\n"
            )
        if intent == "general":
            return (
                "**General Tech**\n\n"
                "Kuberon can answer broader engineering questions directly, including programming, networking, DevOps, cloud, databases, and system design.\n\n"
                "Ask a topic like `what is Python`, `how does DNS work`, or `what is REST API`, and it will answer with a practical explanation and examples.\n\n"
                "Ask me anything else — I answer concepts, cluster issues, and general tech ↗"
            )
        return (
            "📘 **Technical Topic**\n\n"
            "**What is it?**\n"
            "This is a technical question, so Kuberon answers it directly with the best practical interpretation available.\n\n"
            "**The simple analogy**\n"
            "It works like asking a senior engineer for a quick explanation: short answer first, practical details after that.\n\n"
            "**How it works**\n"
            "Kuberon supports concept questions, cluster diagnosis, and broader technical help. When a question is broad, it still aims to give a useful direct answer instead of stopping.\n\n"
            "If you ask a more specific topic name, the explanation becomes more precise and example-driven.\n\n"
            "**Real example**\n"
            "```text\n"
            "what is a pod\n"
            "what is cluster\n"
            "how does DNS work\n"
            "```\n\n"
            "**When to use it**\n"
            "- Use concept questions when you want explanations.\n"
            "- Use cluster issue questions when something is broken.\n"
            "- Use general tech questions for programming, networking, or DevOps topics.\n\n"
            "**Common mistakes**\n"
            "- Asking something broad and expecting a very specific answer.\n"
            "- Mixing concept and incident questions in one sentence.\n"
            "- Leaving out the main topic name.\n\n"
            "**Related topics to learn next**\n"
            "- Pod: the smallest Kubernetes runtime unit.\n"
            "- Cluster: the full Kubernetes environment.\n"
            "- Service: the stable network layer for Pods.\n\n"
            "---\n"
            "*Ask me anything else — I answer concepts, cluster issues, and general tech ↗*"
        )

    @staticmethod
    def _planned_call_command(call: PlannedToolCall) -> str:
        namespace = call.params.get("namespace", "default")
        if call.name == "get_pods":
            return f"kubectl get pods -n {namespace} -o wide"
        if call.name == "get_events":
            field_selector = call.params.get("field_selector", "")
            selector_part = f" --field-selector {field_selector}" if field_selector else ""
            return f"kubectl get events -n {namespace}{selector_part} --sort-by=.metadata.creationTimestamp"
        if call.name in {"get_metrics", "get_resource_usage"}:
            sort_by = call.params.get("sort_by", "")
            sort_part = f" --sort-by={sort_by}" if sort_by else ""
            return f"kubectl top pods -n {namespace}{sort_part}"
        if call.name == "describe_pod":
            return f"kubectl describe pod {call.params.get('pod_name', '')} -n {namespace}"
        if call.name == "get_logs":
            return f"kubectl logs {call.params.get('pod_name', '')} -n {namespace} --tail=50"
        if call.name == "get_previous_logs":
            return f"kubectl logs {call.params.get('pod_name', '')} -n {namespace} --previous --tail=50"
        if call.name == "exec_kubectl":
            return f"kubectl {call.params.get('command', '')}".strip()
        if call.name == "get_metrics_range":
            return "prometheus query_range"
        return call.name

    @staticmethod
    def _general_fallback_answer(question: str) -> str:
        return OpsAssistant._direct_answer_fallback(question, "general")

        lower = question.strip().lower()
        if "what is a pod" in lower or "what is pod" in lower or lower == "pod":
            return (
                "📘 Pod\n\n"
                "**What is it?**\n"
                "A Pod is the smallest unit Kubernetes runs, and it wraps one or more containers together.\n\n"
                "**Simple analogy**\n"
                "Think of a pod like a small apartment. The containers inside share the same address and can use the same local storage.\n\n"
                "**How it works**\n"
                "Kubernetes does not usually manage single containers directly. Instead, it schedules Pods onto nodes. A Pod gives the containers inside a shared network identity, so they can talk to each other over `localhost`.\n\n"
                "Pods are temporary. If a Pod dies, Kubernetes can replace it with a new one, often with a different IP. That is why important state should not live only inside the Pod filesystem.\n\n"
                "**Real example**\n"
                "```bash\n"
                "kubectl get pods -n default\n"
                "kubectl describe pod <pod-name> -n default\n"
                "```\n\n"
                "**When to use it**\n"
                "- Use Pods as the runtime unit for your application containers.\n"
                "- Use a single Pod when sidecar containers need to share the same network and storage.\n"
                "- In production, create Pods through Deployments or StatefulSets instead of by hand.\n\n"
                "**Common mistakes**\n"
                "- Treating Pods like permanent servers.\n"
                "- Storing important data only inside a Pod.\n"
                "- Creating raw Pods directly instead of using a controller.\n\n"
                "**Related topics**\n"
                "- Deployment: manages replicated Pods and rollouts.\n"
                "- Service: gives Pods a stable network address.\n"
                "- StatefulSet: manages stateful Pods with stable identity.\n\n"
                "---\n"
                "*Ask me anything else — concepts, cluster issues, or general tech ↗*"
            )
        if "what is docker" in lower or lower == "docker":
            return (
                "CONCEPT: Docker\n\n"
                "SIMPLE ANSWER\n"
                "Docker packages an application and its dependencies into a container so it runs the same way everywhere.\n\n"
                "DETAILED EXPLANATION\n"
                "Docker solves the 'works on my machine' problem by bundling your app, runtime, libraries, and system dependencies into one portable image.\n\n"
                "A good analogy is a shipping container. No matter which truck, port, or ship carries it, the box stays the same. Docker does that for software.\n\n"
                "REAL EXAMPLE\n"
                "```bash\n"
                "docker build -t myapp:latest .\n"
                "docker run -p 3000:3000 myapp:latest\n"
                "```\n\n"
                "WHEN TO USE / WHEN NOT TO USE\n"
                "- Use Docker for consistent development, testing, and deployment.\n"
                "- Use Docker when your app depends on a specific runtime or library set.\n"
                "- Do not treat Docker alone as a full multi-service production orchestrator.\n\n"
                "COMMON MISTAKES\n"
                "- Baking secrets into the image.\n"
                "- Building very large images with unnecessary files.\n"
                "- Confusing Docker with Kubernetes.\n\n"
                "RELATED CONCEPTS\n"
                "- Dockerfile\n"
                "- Container image\n"
                "- Kubernetes\n\n"
                "Ask me anything else about this topic ?"
            )
        if "what is kubernetes" in lower or lower == "kubernetes":
            return (
                "CONCEPT: Kubernetes\n\n"
                "SIMPLE ANSWER\n"
                "Kubernetes is a platform that deploys, scales, heals, and connects containers across a cluster.\n\n"
                "DETAILED EXPLANATION\n"
                "If Docker gives you the container, Kubernetes manages lots of containers in production. It decides where they run, how many copies should exist, when to restart them, and how traffic should reach them.\n\n"
                "A simple analogy is air traffic control. The planes are your containers, and Kubernetes coordinates where they land, when they move, and how the system stays stable.\n\n"
                "REAL EXAMPLE\n"
                "```bash\n"
                "kubectl get pods -n default\n"
                "kubectl rollout status deployment/cartservice -n default\n"
                "```\n\n"
                "WHEN TO USE / WHEN NOT TO USE\n"
                "- Use Kubernetes when you run multiple services and need scaling, rollouts, and self-healing.\n"
                "- Use Kubernetes when you want declarative infrastructure for containerized apps.\n"
                "- Do not start with Kubernetes for a tiny single-container app unless you really need the complexity.\n\n"
                "COMMON MISTAKES\n"
                "- Treating pods like permanent servers.\n"
                "- Skipping resource requests and limits.\n"
                "- Storing important state directly inside pods.\n\n"
                "RELATED CONCEPTS\n"
                "- Pod\n"
                "- Deployment\n"
                "- Service\n\n"
                "Ask me anything else about this topic ?"
            )
        if "what is api" in lower or "what is an api" in lower:
            return (
                "CONCEPT: API\n\n"
                "SIMPLE ANSWER\n"
                "An API is a defined way for one piece of software to request data or actions from another.\n\n"
                "DETAILED EXPLANATION\n"
                "An API is like a restaurant menu. It tells you what you can ask for, how to ask for it, and what comes back, without exposing how the kitchen works internally.\n\n"
                "In real projects, a frontend uses an API to talk to a backend, fetch data, create records, or trigger workflows.\n\n"
                "REAL EXAMPLE\n"
                "```bash\n"
                "curl http://127.0.0.1:8000/health\n"
                "```\n\n"
                "WHEN TO USE / WHEN NOT TO USE\n"
                "- Use an API when two systems need a stable contract.\n"
                "- Use an API when frontend and backend should evolve independently.\n"
                "- Do not overengineer a formal API for a tiny one-off local script.\n\n"
                "COMMON MISTAKES\n"
                "- Changing response shapes without versioning.\n"
                "- Not documenting request and response formats.\n"
                "- Exposing internal implementation details as public contract.\n\n"
                "RELATED CONCEPTS\n"
                "- HTTP\n"
                "- REST\n"
                "- JSON\n\n"
                "Ask me anything else about this topic ?"
            )
        if "what is react" in lower:
            return (
                "CONCEPT: React\n\n"
                "SIMPLE ANSWER\n"
                "React is a JavaScript library for building reusable user interfaces from components.\n\n"
                "DETAILED EXPLANATION\n"
                "React helps you split a UI into small reusable pieces like buttons, forms, cards, and layouts. Instead of manually updating the whole page, you describe what the UI should look like for the current state.\n\n"
                "A useful analogy is LEGO blocks. Small pieces combine into bigger structures, and you can reuse the same block in many places.\n\n"
                "REAL EXAMPLE\n"
                "```jsx\n"
                "function Welcome() {\n"
                "  return <h1>Welcome to Kuberon</h1>;\n"
                "}\n"
                "```\n\n"
                "WHEN TO USE / WHEN NOT TO USE\n"
                "- Use React for interactive web apps with changing state.\n"
                "- Use React when you want a component-based frontend.\n"
                "- Do not add React to a page that only needs simple static HTML.\n\n"
                "COMMON MISTAKES\n"
                "- Keeping too much logic in one component.\n"
                "- Misunderstanding how state updates flow.\n"
                "- Repeating UI instead of extracting components.\n\n"
                "RELATED CONCEPTS\n"
                "- Components\n"
                "- State\n"
                "- Props\n\n"
                "Ask me anything else about this topic ?"
            )
        if "what is python" in lower:
            return (
                "CONCEPT: Python\n\n"
                "SIMPLE ANSWER\n"
                "Python is a programming language known for readable syntax and fast development.\n\n"
                "DETAILED EXPLANATION\n"
                "Python is popular because it is easy to read and quick to write. Teams use it for automation, backend APIs, scripting, data analysis, and machine learning.\n\n"
                "It is often a strong first language because you can focus on solving the problem instead of fighting complicated syntax.\n\n"
                "REAL EXAMPLE\n"
                "```python\n"
                "for name in [\"pod-a\", \"pod-b\"]:\n"
                "    print(name)\n"
                "```\n\n"
                "WHEN TO USE / WHEN NOT TO USE\n"
                "- Use Python for automation, APIs, data work, and scripting.\n"
                "- Use Python when developer speed matters.\n"
                "- Do not assume it is always the best choice for low-level systems programming.\n\n"
                "COMMON MISTAKES\n"
                "- Ignoring virtual environments.\n"
                "- Mixing tabs and spaces.\n"
                "- Writing large scripts without organizing them into functions and modules.\n\n"
                "RELATED CONCEPTS\n"
                "- Virtual environment\n"
                "- FastAPI\n"
                "- Automation\n\n"
                "Ask me anything else about this topic ?"
            )
        return (
            "**Answer**\n\n"
            "I do not want to guess what topic you meant.\n\n"
            "If you want a concept explanation, ask something like `what is a pod`, `what is docker`, or `explain ingress`.\n"
            "If you want cluster help, ask something like `show crashing pods` or `why is cartservice down`.\n"
            "If you want general tech help, ask things like `what is Python` or `how does DNS work`.\n\n"
            "Ask me another topic and I will answer it directly ↗"
        )

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
    def _usable_model_output(model_output: str) -> str:
        text = (model_output or "").strip()
        if not text:
            return ""
        if "External models are not configured" in text:
            return ""
        return text

    @staticmethod
    def _detect_requested_issue(question_lower: str) -> str:
        if "pvc" in question_lower or "persistentvolumeclaim" in question_lower:
            return "PVC Pending"
        if (
            "imagepullbackoff" in question_lower
            or "errimagepull" in question_lower
            or "image pull" in question_lower
            or "cannot pull container image" in question_lower
            or "can't pull container image" in question_lower
            or "cannot pull image" in question_lower
            or "can't pull image" in question_lower
            or "pod cannot pull container image" in question_lower
            or "container image pull" in question_lower
        ):
            return "ImagePullBackOff"
        if "crashloopbackoff" in question_lower or "crashloop" in question_lower:
            return "CrashLoopBackOff"
        if "oomkilled" in question_lower or "oom" in question_lower:
            return "OOMKilled"
        if "pending" in question_lower:
            return "FailedScheduling"
        if "service" in question_lower and any(term in question_lower for term in ["down", "unreachable", "not accessible", "not reachable"]):
            return "Service Not Accessible"
        return ""

    @staticmethod
    def _parse_pvc_rows(output: str) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for raw_line in output.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("NAME "):
                continue
            parts = re.split(r"\s{2,}|\t+|\s+", line)
            if len(parts) < 2:
                continue
            rows.append(
                {
                    "name": parts[0],
                    "status": parts[1],
                    "volume": parts[2] if len(parts) > 2 else "",
                    "capacity": parts[3] if len(parts) > 3 else "",
                }
            )
        return rows

    @staticmethod
    def _issue_exists(requested_issue: str, unhealthy_rows: list[dict[str, str]], pvc_rows: list[dict[str, str]], outputs: str) -> bool:
        if not requested_issue:
            return False
        if requested_issue == "PVC Pending":
            return any(row["status"].lower() == "pending" for row in pvc_rows)
        if requested_issue == "ImagePullBackOff":
            return "imagepullbackoff" in outputs or "errimagepull" in outputs or any(row["status"].lower() in {"imagepullbackoff", "errimagepull"} for row in unhealthy_rows)
        if requested_issue == "CrashLoopBackOff":
            return "crashloopbackoff" in outputs or any(row["status"].lower() == "crashloopbackoff" for row in unhealthy_rows)
        if requested_issue == "OOMKilled":
            return "oomkilled" in outputs or any("oomkilled" in row["status"].lower() for row in unhealthy_rows)
        if requested_issue == "FailedScheduling":
            return "failedscheduling" in outputs or "unschedulable" in outputs or any(row["status"].lower() == "pending" for row in unhealthy_rows)
        if requested_issue == "Service Not Accessible":
            return "0 endpoints" in outputs or "no endpoints" in outputs or "connection refused" in outputs
        return False

    @staticmethod
    def _build_issue_not_found_response(requested_issue: str, namespace: str) -> str:
        debug_map = {
            "PVC Pending": [f"kubectl get pvc -n {namespace}"],
            "ImagePullBackOff": [f"kubectl get pods -n {namespace}"],
            "CrashLoopBackOff": [f"kubectl get pods -n {namespace}"],
            "OOMKilled": [f"kubectl get pods -n {namespace}", f"kubectl get events -n {namespace} --sort-by=.metadata.creationTimestamp"],
            "FailedScheduling": [f"kubectl get pods -n {namespace}", f"kubectl get events -n {namespace} --sort-by=.metadata.creationTimestamp"],
            "Service Not Accessible": [f"kubectl get svc -n {namespace}", f"kubectl get endpoints -n {namespace}"],
        }
        problem_text = {
            "PVC Pending": f"No PVC in Pending state found in namespace {namespace}.",
            "ImagePullBackOff": f"No pods are in ImagePullBackOff state in namespace {namespace}.",
            "CrashLoopBackOff": f"No pods are in CrashLoopBackOff state in namespace {namespace}.",
            "OOMKilled": f"No OOMKilled workload was found in namespace {namespace}.",
            "FailedScheduling": f"No Pending or unschedulable workload was found in namespace {namespace}.",
            "Service Not Accessible": f"No service reachability failure matching the request was confirmed in namespace {namespace}.",
        }
        return OpsAssistant._render_diagnostic_response(
            requested_issue,
            [problem_text.get(requested_issue, f"No {requested_issue} issue found in namespace {namespace}.")],
            "None",
            ["Requested issue not present in current cluster state."],
            debug_map.get(requested_issue, [f"kubectl get pods -n {namespace}"]),
            "Requested issue not present in current cluster state.",
            ["No action required."],
        )

    @staticmethod
    def _render_query_response(
        issue_name: str,
        severity: str,
        analysis_lines: list[str],
        debug_commands: list[str],
        fix_lines: list[str],
    ) -> str:
        rendered_analysis = "\n".join(f"- {item}" for item in analysis_lines) if analysis_lines else "- No analysis details available."
        rendered_debug = "\n".join(debug_commands) if debug_commands else "kubectl get pods -n default"
        rendered_fix = "\n".join(f"- {item}" for item in fix_lines) if fix_lines else "- No action required."
        return (
            "Issue\n"
            + issue_name
            + "\n\nSeverity: "
            + severity
            + "\n\nAnalysis\n"
            + rendered_analysis
            + "\n\nDebug\n```bash\n"
            + rendered_debug
            + "\n```\n\nFix\n"
            + rendered_fix
        )

    @staticmethod
    def _format_query_response(question: str, tool_results: list, namespace: str = "default") -> str:
        successful_results = [result for result in tool_results if result.ok]
        pod_rows: list[dict[str, str]] = []
        metrics_preview: list[str] = []
        events_present = False
        for result in successful_results:
            if result.name == "get_pods":
                pod_rows = OpsAssistant._parse_pod_rows(result.output)
            elif result.name in {"get_metrics", "get_resource_usage"}:
                metrics_preview = [line.strip() for line in result.output.splitlines() if line.strip()][:6]
            elif result.name == "get_events" and result.output.strip() and not result.output.lower().startswith("no resources found"):
                events_present = True

        question_lower = question.lower()
        high_restart_rows = [row for row in pod_rows if row["restarts"].isdigit() and int(row["restarts"]) > 0]
        not_running_rows = [row for row in pod_rows if row["status"].lower() != "running" or row["ready"].startswith("0/")]

        if "restarted" in question_lower:
            if not high_restart_rows:
                return OpsAssistant._render_query_response(
                    "Restart Analysis",
                    "None",
                    [
                        f"Namespace checked: {namespace}",
                        "Restarted workloads: 0",
                    ],
                    [f"kubectl get pods -n {namespace}"],
                    ["No action required."],
                )
            lines = [
                f"{row['name']} restarted {row['restarts']} time(s) and is currently {row['status']}."
                for row in high_restart_rows
            ]
            return OpsAssistant._render_query_response(
                "Restart Analysis",
                "Medium" if any(int(row["restarts"]) > 2 for row in high_restart_rows if row["restarts"].isdigit()) else "Low",
                lines,
                [
                    f"kubectl get pods -n {namespace}",
                    f"kubectl get events -n {namespace} --sort-by=.metadata.creationTimestamp",
                ],
                ["Inspect restarted workloads if the restart count keeps increasing."],
            )

        if "how many" in question_lower or "show all" in question_lower or "which" in question_lower:
            lines = [
                f"Total pods checked: {len(pod_rows)}",
                f"Not running or not ready: {len(not_running_rows)}",
                f"Restarted workloads: {len(high_restart_rows)}",
                f"Recent error events present: {'yes' if events_present else 'no'}",
            ]
            if not_running_rows:
                lines.extend(f"{row['name']} -> {row['status']} ({row['restarts']} restarts)" for row in not_running_rows[:6])
            return OpsAssistant._render_query_response(
                "Cluster Query",
                "Low" if not not_running_rows else "Medium",
                lines,
                [
                    f"kubectl get pods -n {namespace}",
                    f"kubectl get events -n {namespace} --sort-by=.metadata.creationTimestamp",
                ],
                ["No action required unless the analysis reveals a workload you want to investigate."],
            )

        if any(term in question_lower for term in ["usage", "cpu", "memory", "analyze"]):
            metric_lines = metrics_preview if metrics_preview else ["No live metrics were returned.", "Metrics data is unavailable from the cluster right now."]
            return OpsAssistant._render_query_response(
                "Resource Usage Analysis",
                "None" if metrics_preview else "Medium",
                metric_lines,
                [
                    f"kubectl top pods -n {namespace}",
                    f"kubectl get pods -n {namespace}",
                ],
                ["Install or verify metrics-server if live usage data is required." if not metrics_preview else "No action required."],
            )

        return OpsAssistant._render_query_response(
            "Cluster Query",
            "Low",
            [
                f"Pods checked: {len(pod_rows)}",
                f"Not running or not ready: {len(not_running_rows)}",
                f"Restarted workloads: {len(high_restart_rows)}",
            ],
            [f"kubectl get pods -n {namespace}"],
            ["No action required."],
        )

    @staticmethod
    def _render_diagnostic_response(
        issue_name: str,
        problem_lines: list[str],
        severity: str,
        possible_causes: list[str],
        debug_commands: list[str],
        root_cause: str,
        fix_lines: list[str],
    ) -> str:
        rendered_problem = "\n".join(f"- {item}" for item in problem_lines) if problem_lines else "- No concrete runtime facts were captured."
        rendered_causes = "\n".join(f"- {item}" for item in possible_causes) if possible_causes else "- No additional likely causes identified from current evidence."
        rendered_debug = "\n".join(debug_commands) if debug_commands else "kubectl get pods -n default"
        rendered_fix = "\n".join(f"- {item}" for item in fix_lines) if fix_lines else "- No action required"
        return (
            "Issue\n"
            + issue_name
            + "\n\nCluster evidence summary\n"
            + rendered_problem
            + "\n\nSeverity: "
            + severity
            + "\n\nLikely causes\n"
            + rendered_causes
            + "\n\nVerify with\n```bash\n"
            + rendered_debug
            + "\n```\n\nRoot cause\n"
            + root_cause
            + "\n\nSuggested fixes\n"
            + rendered_fix
        )

    @staticmethod
    def _derive_possible_causes(issue_name: str, question_lower: str, has_confirmed_issue: bool, unhealthy_rows: list[dict[str, str]]) -> list[str]:
        if issue_name == "CrashLoopBackOff":
            return [
                "Application startup failure.",
                "Missing environment variables or secrets.",
                "Dependency not reachable during startup, such as a database or API.",
                "Invalid configuration or container command.",
            ]
        if issue_name == "ImagePullBackOff":
            return [
                "Wrong image name or tag.",
                "Registry credentials are missing or invalid.",
                "The image was deleted or is not accessible from the cluster.",
            ]
        if issue_name == "OOMKilled":
            return [
                "Container memory limit is too low.",
                "Application has a memory spike or memory leak.",
                "Resource requests and limits do not match real workload usage.",
            ]
        if issue_name == "FailedScheduling":
            return [
                "Resource requests are higher than cluster capacity.",
                "nodeSelector or affinity rules cannot be satisfied.",
                "Node taints are blocking placement.",
            ]
        if issue_name == "PVC Pending":
            return [
                "No matching PersistentVolume exists.",
                "StorageClass or access mode does not match the claim.",
                "Dynamic provisioner is missing or unhealthy.",
            ]
        if issue_name == "Service Not Accessible" or ("service" in question_lower and any(term in question_lower for term in ["down", "unreachable", "not accessible", "not reachable"])):
            return [
                "Service selector does not match pod labels.",
                "Backing pods are not Ready, so endpoints stay empty.",
                "Port mapping between Service and container is incorrect.",
            ]
        if unhealthy_rows or has_confirmed_issue:
            return [
                "Application startup failure.",
                "Missing environment variables or configuration.",
                "Dependency or networking issue between services.",
            ]
        return [
            "No active failure signals are present in the current cluster data."
        ]

    @staticmethod
    def _derive_root_cause(issue_name: str, unhealthy_rows: list[dict[str, str]], successful_results: list, model_output: str) -> str:
        usable_model_output = OpsAssistant._usable_model_output(model_output)
        if usable_model_output:
            return usable_model_output[:560] if len(usable_model_output) > 560 else usable_model_output
        primary_row = unhealthy_rows[0] if unhealthy_rows else None
        combined_output = "\n".join(result.output for result in successful_results if result.ok).lower()
        pod_fact = ""
        if primary_row:
            pod_fact = (
                f"Pod `{primary_row['name']}` is currently `{primary_row['status']}` "
                f"with ready state `{primary_row['ready']}` and `{primary_row['restarts']}` restarts."
            )
        if issue_name == "CrashLoopBackOff":
            evidence = "The cluster evidence shows a repeated startup crash loop."
            if pod_fact:
                evidence = pod_fact
            if "exit code: 1" in combined_output or "exit code 1" in combined_output:
                return evidence + " Describe output also reports exit code 1, which confirms the container process is starting and then crashing."
            if "oomkilled" in combined_output or "exit code: 137" in combined_output:
                return evidence + " Describe output shows OOMKilled or exit code 137, so the restart loop is being caused by memory exhaustion."
            if "back-off restarting failed container" in combined_output:
                return evidence + " Events show Kubernetes is backing off after repeated failed restarts, which confirms the application is not staying up long enough to become healthy."
            return evidence + " That confirms the application is failing during startup and Kubernetes is retrying it."
        if issue_name == "RunContainerError":
            return (pod_fact + " " if pod_fact else "") + "Kubernetes created the container but could not run its configured command successfully, so the failure is at container start time rather than steady-state runtime."
        if issue_name == "ImagePullBackOff":
            return (pod_fact + " " if pod_fact else "") + "The pod cannot download its container image, so the workload never reaches a running state."
        if issue_name == "OOMKilled":
            return (pod_fact + " " if pod_fact else "") + "Describe output or status evidence shows OOMKilled, which means the container exceeded its memory limit and the kernel terminated it."
        if issue_name == "FailedScheduling":
            return (pod_fact + " " if pod_fact else "") + "The scheduler could not place the pod on any node, so the workload is blocked before it can even start."
        if issue_name == "PVC Pending":
            return (pod_fact + " " if pod_fact else "") + "The workload is waiting on storage binding, so the pod cannot start until the PVC is satisfied."
        if issue_name == "Service Not Accessible":
            return "The Service exists, but the current cluster evidence does not show healthy reachable backends for that request path."
        if issue_name == "High Restart Count":
            return (pod_fact + " " if pod_fact else "") + "The workload is currently running, but restart counts prove that it was unstable recently."
        if issue_name == "No Issue Detected":
            return "No root cause was identified because the current pod status and recent events do not show an active failure."
        if pod_fact:
            return pod_fact + " The cluster data confirms a workload problem, but the exact failure mechanism is still incomplete from the current evidence."
        return "Insufficient evidence is available to determine the exact root cause."

    @staticmethod
    def _derive_fix_lines(issue_name: str, namespace: str, unhealthy_rows: list[dict[str, str]], suggested_fixes: list[dict], question_lower: str) -> list[str]:
        if suggested_fixes:
            commands = [fix["command_preview"] for fix in suggested_fixes[:3] if fix.get("command_preview")]
            if commands:
                return commands
        pod_name = unhealthy_rows[0]["name"] if unhealthy_rows else ""
        workload = OpsAssistant._derive_workload_name(pod_name) if pod_name else ""
        if issue_name in {"CrashLoopBackOff", "RunContainerError", "Workload Failure"} and workload:
            return [
                f"kubectl rollout undo deployment/{workload} -n {namespace}",
                f"kubectl delete pod {pod_name} -n {namespace}",
            ]
        if issue_name == "ImagePullBackOff" and workload:
            return [
                f"kubectl rollout undo deployment/{workload} -n {namespace}",
                f"kubectl set image deployment/{workload} <container-name>=<working-image> -n {namespace}",
            ]
        if issue_name == "OOMKilled" and pod_name:
            return OpsAssistant._build_memory_fix_commands(pod_name, namespace)
        if issue_name == "FailedScheduling" and workload:
            return [
                f"kubectl scale deployment/{workload} --replicas=0 -n {namespace}",
                f"kubectl scale deployment/{workload} --replicas=1 -n {namespace}",
            ]
        if issue_name == "Service Not Accessible":
            service_name = OpsAssistant._extract_workload_hint(question_lower) or "<service-name>"
            return [
                f"kubectl patch service {service_name} -n {namespace} -p '{{\"spec\":{{\"selector\":{{\"app\":\"<correct-label>\"}}}}}}'",
            ]
        if issue_name == "PVC Pending":
            return [
                "Create a matching PersistentVolume or fix the StorageClass configuration.",
            ]
        if issue_name == "No Issue Detected":
            return ["No action required."]
        return ["Review the failing workload configuration and apply a rollback or corrected deployment spec."]

    @staticmethod
    def _format_response(question: str, model_output: str, tool_results: list, runbook_matches: list, suggested_fixes: list, namespace: str = "default") -> str:
        usable_model_output = OpsAssistant._usable_model_output(model_output)
        if "which" in question.lower() or "how many" in question.lower() or "show all" in question.lower() or "analyze" in question.lower() or "restarted" in question.lower() or "usage" in question.lower() or "cluster health" in question.lower() or "any issues" in question.lower() or "what is wrong" in question.lower():
            if usable_model_output:
                return usable_model_output
            return OpsAssistant._format_query_response(question, tool_results, namespace)
        if usable_model_output:
            return usable_model_output
        if "memory" in question.lower() or "oom" in question.lower() or "high memory" in question.lower():
            return OpsAssistant._format_memory_response(question, model_output, tool_results, namespace)

        question_lower = question.lower()
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
            return OpsAssistant._render_diagnostic_response(
                "Diagnostic Tooling Unavailable",
                [
                    "Kuberon could not collect trustworthy cluster evidence.",
                    "No service-specific conclusion was generated because every diagnostic tool failed.",
                    missing.replace("`", ""),
                ],
                "Medium",
                [
                    "kubectl cannot reach the cluster or required tooling is unavailable.",
                    "The API server, kubeconfig, or local cluster may not be reachable.",
                ],
                [
                    "kubectl cluster-info",
                    "kubectl get pods -n default",
                    "kubectl get events -n default --sort-by=.metadata.creationTimestamp",
                ],
                "Insufficient data to determine root cause because the diagnostic tools did not return usable cluster evidence.",
                [
                    "Restore kubectl or cluster connectivity.",
                    "Provide pod logs.",
                    "Provide describe output.",
                    "Provide events.",
                ],
            )

        severity = "Low"
        if any(marker in outputs for marker in ["crashloopbackoff", "panic", "fatal", "oomkilled", "imagepullbackoff"]):
            severity = "High"
        elif any(marker in outputs for marker in ["failing", "back-off", "unhealthy", "failed", "timeout", "unschedulable"]):
            severity = "Medium"
        elif has_confirmed_issue:
            severity = "Low"

        findings: list[str] = []
        pod_rows = []
        unhealthy_rows: list[dict[str, str]] = []
        restarted_rows: list[dict[str, str]] = []
        pvc_rows: list[dict[str, str]] = []
        events_preview = ""
        for result in successful_results:
            if result.name == "get_pods":
                pod_rows = OpsAssistant._parse_pod_rows(result.output)
                if pod_rows:
                    unhealthy_rows = [
                        row for row in pod_rows if row["status"].lower() != "running" or row["ready"].startswith("0/")
                    ]
                    restarted_rows = [
                        row for row in pod_rows if row["restarts"].isdigit() and int(row["restarts"]) > 0
                    ]
                    if unhealthy_rows:
                        findings.append(
                            f"Checked {len(pod_rows)} pod rows in namespace {namespace}; {len(unhealthy_rows)} pod(s) are not healthy."
                        )
                    else:
                        findings.append(
                            f"Checked {len(pod_rows)} pod rows in namespace {namespace}; all currently show Running status and ready containers."
                        )
                for row in pod_rows:
                    if row["status"].lower() != "running" or row["ready"].startswith("0/"):
                        findings.append(f"{row['name']} -> {row['status']} ({row['restarts']} restarts, ready {row['ready']})")
                for row in restarted_rows:
                    findings.append(f"{row['name']} has restarted {row['restarts']} time(s) while currently showing {row['status']}.")
            if result.name == "get_events":
                events_preview = result.output.strip()
            if result.name == "exec_kubectl" and "get pvc" in result.command:
                pvc_rows = OpsAssistant._parse_pvc_rows(result.output)
                if pvc_rows:
                    pending_pvcs = [row for row in pvc_rows if row["status"].lower() == "pending"]
                    if pending_pvcs:
                        for row in pending_pvcs:
                            findings.append(f"PVC {row['name']} is Pending.")
                    else:
                        findings.append(f"Checked {len(pvc_rows)} PVC row(s) in namespace {namespace}; none are Pending.")
        for result in successful_results[:4]:
            if result.name == "get_pods":
                continue
            if result.name == "exec_kubectl" and "get pvc" in result.command:
                continue
            preview = result.output.strip().replace("\r", " ").replace("\n", " ")
            if preview:
                findings.append(f"{result.name}: {preview[:220]}")
        if not findings:
            findings.append("Diagnostics returned limited evidence, so Kuberon is keeping the conclusion conservative.")

        no_recent_events = events_preview.lower().startswith("no resources found")
        requested_issue = OpsAssistant._detect_requested_issue(question_lower)
        asks_for_pending = "pending" in question_lower
        asks_for_crash = any(term in question_lower for term in ["crash", "crashing", "crashloop", "down", "failing", "not running"])
        asks_for_inventory = "what pods" in question_lower or "show me all" in question_lower
        high_restart_rows = [row for row in restarted_rows if row["restarts"].isdigit() and int(row["restarts"]) > 2]
        issue_name = OpsAssistant._derive_issue_name(outputs, unhealthy_rows, high_restart_rows, question_lower, no_recent_events)
        debug_commands = OpsAssistant._build_debug_commands(question_lower, namespace, unhealthy_rows)

        if requested_issue and not OpsAssistant._issue_exists(requested_issue, unhealthy_rows, pvc_rows, outputs):
            return OpsAssistant._build_issue_not_found_response(requested_issue, namespace)

        if pod_rows and not unhealthy_rows and not restarted_rows and no_recent_events and (asks_for_pending or asks_for_crash or asks_for_inventory):
            return OpsAssistant._render_diagnostic_response(
                "No Issue Detected",
                [
                    f"Checked {len(pod_rows)} pod rows in namespace {namespace}.",
                    "All pods are healthy.",
                    "No failures detected.",
                ],
                "None",
                ["No active failure signals are present in pod status or events."],
                debug_commands,
                "No root cause, system is healthy.",
                ["No action required."],
            )

        if pod_rows and not unhealthy_rows and restarted_rows and no_recent_events:
            return OpsAssistant._render_diagnostic_response(
                "High Restart Count" if high_restart_rows else "Transient Issue Resolved",
                findings,
                "Medium" if high_restart_rows else "Low",
                [
                    "The workload had a recent runtime failure but is currently recovering.",
                    "A dependency, configuration, or startup issue may have existed earlier.",
                ],
                debug_commands,
                (
                    "No active outage is visible right now because pods are currently Running and Ready, but restart history is concrete evidence that at least one workload was unstable recently."
                    if high_restart_rows
                    else "A prior issue likely occurred, but the workload is currently healthy. Only restart history remains as evidence, so the failure appears transient and resolved."
                ),
                (
                    ["Investigate the restarted workload with `kubectl describe pod` and `kubectl logs` if restarts continue."]
                    if high_restart_rows
                    else ["No action required unless the restarts continue or new error events appear."]
                ),
            )

        root_cause = OpsAssistant._derive_root_cause(issue_name, unhealthy_rows, successful_results, model_output)
        fix_commands = OpsAssistant._derive_fix_lines(issue_name, namespace, unhealthy_rows, suggested_fixes, question_lower)
        uncertain_issue = not has_confirmed_issue and (bool(unhealthy_rows) or bool(restarted_rows) or (events_preview and not no_recent_events))
        if uncertain_issue:
            return OpsAssistant._render_diagnostic_response(
                issue_name,
                findings,
                "Medium",
                [
                    "Application startup or configuration issue.",
                    "Dependency or service connectivity problem.",
                    "Resource or environment mismatch.",
                ],
                debug_commands,
                "Insufficient data to determine root cause from the currently collected evidence.",
                [
                    "Provide pod logs.",
                    "Provide `kubectl describe pod <pod-name>` output.",
                    "Provide `kubectl get events` output.",
                ],
            )

        possible_causes = OpsAssistant._derive_possible_causes(issue_name, question_lower, has_confirmed_issue, unhealthy_rows)
        rendered_fixes = fix_commands
        return (
            OpsAssistant._render_diagnostic_response(
                issue_name,
                findings,
                severity,
                possible_causes,
                debug_commands,
                root_cause,
                rendered_fixes,
            )
        )

    @staticmethod
    def _derive_issue_name(outputs: str, unhealthy_rows: list[dict[str, str]], high_restart_rows: list[dict[str, str]], question_lower: str, no_recent_events: bool) -> str:
        if "crashloopbackoff" in outputs:
            return "CrashLoopBackOff"
        if "runcontainererror" in outputs:
            return "RunContainerError"
        if "imagepullbackoff" in outputs or "errimagepull" in outputs:
            return "ImagePullBackOff"
        if "oomkilled" in outputs:
            return "OOMKilled"
        if "unschedulable" in outputs or "failedscheduling" in outputs:
            return "FailedScheduling"
        if "persistentvolumeclaim" in outputs or ("pvc" in question_lower and "pending" in outputs):
            return "PVC Pending"
        if "service" in question_lower and any(term in question_lower for term in ["down", "unreachable", "not accessible", "not reachable"]):
            return "Service Not Accessible"
        if any(row["status"].lower() == "pending" for row in unhealthy_rows):
            return "FailedScheduling"
        if any(row["status"].lower() in {"runcontainererror", "error"} for row in unhealthy_rows):
            return "RunContainerError"
        if high_restart_rows:
            return "High Restart Count"
        if not unhealthy_rows and not high_restart_rows and no_recent_events:
            return "No Issue Detected"
        if unhealthy_rows:
            return "Workload Failure"
        return "No Issue Detected"

    @staticmethod
    def _build_debug_commands(question_lower: str, namespace: str, unhealthy_rows: list[dict[str, str]]) -> list[str]:
        commands = [f"kubectl get pods -n {namespace}", f"kubectl get events -n {namespace} --sort-by=.metadata.creationTimestamp"]
        if unhealthy_rows:
            pod_name = unhealthy_rows[0]["name"]
            commands.append(f"kubectl describe pod {pod_name} -n {namespace}")
            commands.append(f"kubectl logs {pod_name} -n {namespace} --tail=50")
        elif "service" in question_lower:
            commands.append(f"kubectl get svc -n {namespace}")
            commands.append(f"kubectl get endpoints -n {namespace}")
        return commands

    @staticmethod
    def _format_memory_response(question: str, model_output: str, tool_results: list, namespace: str = "default") -> str:
        usable_model_output = OpsAssistant._usable_model_output(model_output)
        if usable_model_output:
            return usable_model_output
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
            return OpsAssistant._render_diagnostic_response(
                "Memory Diagnostics Unavailable",
                ["Kuberon could not retrieve memory diagnostics from the cluster."],
                "Medium",
                ["Metrics data is unavailable."],
                manual_checks.splitlines(),
                "Could not retrieve metrics. Please verify metrics-server and Prometheus are installed.",
                ["No remediation command is justified until the tooling gap is fixed."],
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
            return OpsAssistant._render_diagnostic_response(
                "No OOMKilled Issue Detected",
                findings,
                "None",
                ["No OOMKilled evidence is present in the current cluster state."],
                [
                    f"kubectl get events -n {namespace} --field-selector reason=OOMKilling --sort-by=.metadata.creationTimestamp",
                    f"kubectl top pods -n {namespace} --sort-by=memory",
                ],
                "No OOM events were found and live usage does not show workloads near a memory limit.",
                ["No action required."],
            )

        if confirmed_oom_pods:
            target_pod = confirmed_oom_pods[0]["pod_name"]
            fix_commands = OpsAssistant._build_memory_fix_commands(target_pod, namespace)
            root_cause = model_output.strip() or (
                f"{target_pod} is repeatedly terminating with OOMKilled exit code 137, which means the container exceeded its memory limit and Kubernetes restarted it."
            )
            return OpsAssistant._render_diagnostic_response(
                "OOMKilled",
                findings,
                "High",
                [
                    "Container memory limit is too low.",
                    "Application has a memory spike or memory leak.",
                ],
                [
                    f"kubectl get events -n {namespace} --field-selector reason=OOMKilling --sort-by=.metadata.creationTimestamp",
                    f"kubectl describe pod {target_pod} -n {namespace}",
                ],
                root_cause,
                fix_commands,
            )

        if failed_results:
            return OpsAssistant._render_diagnostic_response(
                "OOMKilled Investigation",
                findings,
                "Medium",
                [
                    "Memory metrics are incomplete.",
                    "The cluster may still have OOM evidence in events or describe output.",
                ],
                [
                    f"kubectl get events -n {namespace} --field-selector reason=OOMKilling --sort-by=.metadata.creationTimestamp",
                    f"kubectl get pods -n {namespace}",
                    f"kubectl top pods -n {namespace} --sort-by=memory",
                ],
                "Insufficient data. Metrics could not be retrieved, so the memory investigation is incomplete.",
                ["No remediation command is justified until memory diagnostics succeed."],
            )

        return OpsAssistant._render_diagnostic_response(
            "OOMKilled Investigation",
            findings,
            "Medium",
            [
                "Current evidence does not confirm an OOMKilled failure.",
                "More memory telemetry may be required.",
            ],
            [
                f"kubectl get events -n {namespace} --field-selector reason=OOMKilling --sort-by=.metadata.creationTimestamp",
                f"kubectl top pods -n {namespace} --sort-by=memory",
            ],
            "Kuberon checked events, pod status, live memory usage, and describe output, but there is still no confirmed OOMKilled root cause.",
            ["No remediation command is justified until OOMKilled evidence is confirmed."],
        )

    @staticmethod
    def _chunk_text(text: str, size: int) -> list[str]:
        return [text[index : index + size] for index in range(0, len(text), size)] or [""]

    @staticmethod
    def _env(key: str) -> str | None:
        import os

        return os.getenv(key)

