from __future__ import annotations

import json
import os
from dataclasses import dataclass

import httpx


@dataclass
class RoutedModelResponse:
    provider: str
    content: str


class LLMRouter:
    def __init__(self) -> None:
        self.anthropic_api_key = os.getenv("ANTHROPIC_API_KEY")
        self.ollama_base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        self.anthropic_model = os.getenv("ANTHROPIC_MODEL", "claude-3-7-sonnet-latest")
        self.ollama_model = os.getenv("OLLAMA_MODEL", "mistral")

    async def reason(self, prompt: str, prefer_fast: bool = False) -> RoutedModelResponse:
        if not prefer_fast and self.anthropic_api_key:
            response = await self._call_anthropic(prompt)
            if response:
                return response
        response = await self._call_ollama(prompt)
        if response:
            return response
        return RoutedModelResponse(
            provider="heuristic",
            content="External models are not configured, so the assistant is using local deterministic reasoning.",
        )

    async def _call_anthropic(self, prompt: str) -> RoutedModelResponse | None:
        headers = {
            "x-api-key": self.anthropic_api_key or "",
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": self.anthropic_model,
            "max_tokens": 700,
            "messages": [{"role": "user", "content": prompt}],
        }
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.post("https://api.anthropic.com/v1/messages", headers=headers, json=payload)
                response.raise_for_status()
                body = response.json()
                text_parts = [item.get("text", "") for item in body.get("content", []) if item.get("type") == "text"]
                return RoutedModelResponse(provider="anthropic", content="\n".join(text_parts).strip())
        except Exception:
            return None

    async def _call_ollama(self, prompt: str) -> RoutedModelResponse | None:
        payload = {"model": self.ollama_model, "stream": False, "prompt": prompt}
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.post(f"{self.ollama_base_url}/api/generate", json=payload)
                response.raise_for_status()
                body = response.json()
                return RoutedModelResponse(provider="ollama", content=body.get("response", "").strip())
        except Exception:
            return None

    @staticmethod
    def format_reasoning_prompt(
        question: str,
        memory_summary: str,
        observations: list[str],
        runbook_context: list[str],
        candidate_fixes: list[str],
    ) -> str:
        return json.dumps(
            {
                "identity": "You are Kuberon, an expert Kubernetes SRE AI assistant. Be precise, evidence-based, and never guess.",
                "task": "Diagnose the Kubernetes issue from real tool output only.",
                "user_question": question,
                "memory_summary": memory_summary,
                "observations": observations,
                "runbook_context": runbook_context,
                "candidate_fixes": candidate_fixes,
                "rules": [
                    "Never invent findings or fixes when evidence is missing.",
                    "Only explain root cause when the observations support it.",
                    "If evidence is partial, say so explicitly and stay conservative.",
                    "Do not mention a specific service unless it appears in tool output.",
                    "RULE 8 — MEMORY QUERY SPECIFIC FALLBACK CHAIN: When the user asks about memory issues, OOM, or high memory, follow this order before writing Findings or Root Cause: Step 1 get_events with OOMKilling filter using kubectl get events -n default --field-selector reason=OOMKilling --sort-by=.metadata.creationTimestamp. Step 2 get_pods and look for OOMKilled status or exit 137 evidence; if any pod shows OOMKilled, call describe_pod on it immediately. Step 3 get_resource_usage with kubectl top pods -n default --sort-by=memory. Step 4 describe_pod for each OOMKilled pod and confirm Last State Terminated, Reason OOMKilled, Exit Code 137, plus timing when possible. The Fix section must contain remediation commands only, such as kubectl patch or kubectl rollout undo. Never put diagnostic kubectl get or kubectl describe commands in the Fix section.",
                    "RULE 9 — KEEP GOING WHEN DATA IS MISSING: If Root Cause cannot be confirmed yet, call more tools and do not stop early. Only stop when all fallback tools are exhausted. When truly stuck on a memory query, say exactly: No OOM events found in the last hour. kubectl top pods also shows no pods near their limit. Your cluster appears memory-healthy right now.",
                ],
                "required_output": {
                    "format": "markdown",
                    "sections": ["Tool Calls", "Severity", "Findings", "Root Cause", "Fix", "Follow-ups"],
                },
            },
            indent=2,
        )
