from __future__ import annotations

import asyncio
import json
import os
import shlex
from dataclasses import dataclass
from datetime import datetime

import httpx

from agent.types import ClusterSnapshot, ClusterWorkload, ToolResult


def utcnow() -> str:
    return datetime.utcnow().isoformat()


@dataclass
class ToolSpec:
    name: str
    description: str


class ToolRegistry:
    def __init__(self) -> None:
        self.prometheus_url = os.getenv("PROMETHEUS_URL", "http://localhost:9090")
        self.specs = [
            ToolSpec("get_pods", "kubectl get pods -n <namespace> -o wide"),
            ToolSpec("describe_pod", "kubectl describe pod <pod_name> -n <namespace>"),
            ToolSpec("get_logs", "kubectl logs <pod_name> -n <namespace> --tail=50"),
            ToolSpec("get_previous_logs", "kubectl logs <pod_name> -n <namespace> --previous --tail=50"),
            ToolSpec("get_events", "kubectl get events -n <namespace> [--field-selector ...] --sort-by=.metadata.creationTimestamp"),
            ToolSpec("get_metrics", "kubectl top pods -n <namespace> [--sort-by=memory]"),
            ToolSpec("get_resource_usage", "kubectl top pods -n <namespace> --sort-by=memory"),
            ToolSpec("exec_kubectl", "kubectl <command>"),
            ToolSpec("get_metrics_range", "Prometheus query_range"),
        ]

    async def run(self, name: str, **kwargs: str) -> ToolResult:
        if name == "get_metrics_range":
            return await self._query_prometheus(**kwargs)
        return await self._run_kubectl(name, **kwargs)

    async def _run_kubectl(self, name: str, **kwargs: str) -> ToolResult:
        namespace = kwargs.get("namespace", "default")
        pod_name = kwargs.get("pod_name", "")
        extra_command = kwargs.get("command", "")
        field_selector = kwargs.get("field_selector", "")
        sort_by = kwargs.get("sort_by", "")
        if name in {"describe_pod", "get_logs", "get_previous_logs"} and pod_name:
            pod_name = await self._resolve_pod_name(namespace, pod_name)

        get_events_command = ["kubectl", "get", "events", "-n", namespace]
        if field_selector:
            get_events_command.extend(["--field-selector", field_selector])
        get_events_command.append("--sort-by=.metadata.creationTimestamp")

        get_metrics_command = ["kubectl", "top", "pods", "-n", namespace]
        if sort_by:
            get_metrics_command.append(f"--sort-by={sort_by}")

        command_map: dict[str, list[str]] = {
            "get_pods": ["kubectl", "get", "pods", "-n", namespace, "-o", "wide"],
            "describe_pod": ["kubectl", "describe", "pod", pod_name, "-n", namespace],
            "get_logs": ["kubectl", "logs", pod_name, "-n", namespace, "--tail=50"],
            "get_previous_logs": ["kubectl", "logs", pod_name, "-n", namespace, "--previous", "--tail=50"],
            "get_events": get_events_command,
            "get_metrics": get_metrics_command,
            "get_resource_usage": get_metrics_command,
            "exec_kubectl": ["kubectl", *shlex.split(extra_command)],
        }
        command = command_map[name]
        start = utcnow()
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            output = (stdout or b"").decode() + (stderr or b"").decode()
            return ToolResult(
                name=name,
                command=" ".join(command),
                ok=proc.returncode == 0,
                output=output.strip(),
                started_at=start,
                finished_at=utcnow(),
            )
        except FileNotFoundError:
            return ToolResult(
                name=name,
                command=" ".join(command),
                ok=False,
                output="kubectl is not installed or not available on PATH.",
                started_at=start,
                finished_at=utcnow(),
            )

    async def _resolve_pod_name(self, namespace: str, pod_hint: str) -> str:
        command = ["kubectl", "get", "pods", "-n", namespace, "-o", "json"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            payload = json.loads((stdout or b"{}").decode())
        except Exception:
            return pod_hint

        items = payload.get("items", [])
        exact = next((item["metadata"]["name"] for item in items if item["metadata"]["name"] == pod_hint), None)
        if exact:
            return exact

        lowered = pod_hint.lower().replace("-", "")
        partial = next(
            (
                item["metadata"]["name"]
                for item in items
                if lowered in item["metadata"]["name"].lower().replace("-", "")
            ),
            None,
        )
        return partial or pod_hint

    async def _query_prometheus(self, **kwargs: str) -> ToolResult:
        query = kwargs.get("query", "up")
        start = kwargs.get("start", "")
        end = kwargs.get("end", "")
        step = kwargs.get("step", "60s")
        started_at = utcnow()
        url = f"{self.prometheus_url}/api/v1/query_range"
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.get(url, params={"query": query, "start": start, "end": end, "step": step})
                response.raise_for_status()
                output = json.dumps(response.json(), indent=2)
                ok = True
        except Exception as exc:
            output = f"Prometheus query failed: {exc}"
            ok = False
        return ToolResult(
            name="get_metrics_range",
            command=f"GET {url}",
            ok=ok,
            output=output,
            started_at=started_at,
            finished_at=utcnow(),
        )

    async def snapshot(self, namespace: str = "default") -> ClusterSnapshot:
        command = ["kubectl", "get", "pods", "-n", namespace, "--no-headers"]
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            rows = (stdout or b"").decode().splitlines()
        except FileNotFoundError:
            rows = []

        workloads: list[ClusterWorkload] = []
        for row in rows:
            parts = row.split()
            if len(parts) < 5:
                continue
            workloads.append(
                ClusterWorkload(
                    name=parts[0],
                    namespace=namespace,
                    ready=parts[1],
                    status=parts[2],
                    restarts=parts[3],
                    age=parts[4],
                )
            )
        return ClusterSnapshot(namespace=namespace, workloads=workloads)
