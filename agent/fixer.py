from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass


@dataclass
class FixAction:
    title: str
    description: str
    resource: str
    namespace: str
    patch: dict
    command_preview: str


class FixApplicator:
    def suggest(self, question: str, namespace: str = "default") -> list[FixAction]:
        lower = question.lower()
        actions: list[FixAction] = []

        if "memory" in lower or "oom" in lower or "crashloop" in lower:
            patch = {
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": "cartservice",
                                    "resources": {
                                        "requests": {"memory": "128Mi"},
                                        "limits": {"memory": "256Mi"},
                                    },
                                }
                            ]
                        }
                    }
                }
            }
            actions.append(self._build_action("Raise memory limit", "Increase cartservice memory to recover from OOM pressure.", namespace, "deployment/cartservice", patch))

        if "image" in lower or "imagepull" in lower or "pull" in lower:
            patch = {
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": "productcatalogservice",
                                    "image": "gcr.io/google-samples/microservices-demo/productcatalogservice:v0.10.2",
                                }
                            ]
                        }
                    }
                }
            }
            actions.append(self._build_action("Restore image tag", "Reset the deployment image to a known-good demo tag.", namespace, "deployment/productcatalogservice", patch))

        if "service" in lower or "endpoint" in lower or "selector" in lower:
            patch = {"spec": {"selector": {"app": "cartservice"}}}
            actions.append(self._build_action("Repair service selector", "Restore the service selector so it points back to cartservice pods.", namespace, "service/cartservice", patch))

        if "cpu" in lower or "resource" in lower or "unschedulable" in lower:
            patch = {
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": "checkoutservice",
                                    "resources": {
                                        "requests": {"cpu": "250m"},
                                        "limits": {"cpu": "500m"},
                                    },
                                }
                            ]
                        }
                    }
                }
            }
            actions.append(self._build_action("Right-size CPU requests", "Reduce CPU requests so the scheduler can place the workload again.", namespace, "deployment/checkoutservice", patch))

        return actions

    def apply(self, resource: str, namespace: str, patch: dict, dry_run: bool = True) -> dict:
        patch_json = json.dumps(patch, separators=(",", ":"))
        command = ["kubectl", "-n", namespace, "patch", resource, "--type", "merge", "-p", patch_json]
        if dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "phase": "preview",
                "command": " ".join(command),
                "output": patch_json,
                "message": "Preview ready. Confirm to apply this patch to the cluster.",
            }

        try:
            completed = subprocess.run(command, check=False, capture_output=True, text=True)
            return {
                "ok": completed.returncode == 0,
                "dry_run": False,
                "phase": "apply",
                "command": " ".join(command),
                "output": (completed.stdout + completed.stderr).strip(),
                "message": "Patch applied successfully." if completed.returncode == 0 else "Patch command failed.",
            }
        except FileNotFoundError:
            return {
                "ok": False,
                "dry_run": False,
                "phase": "apply",
                "command": " ".join(command),
                "output": "kubectl is not installed or not available on PATH.",
                "message": "Could not apply patch because kubectl is unavailable.",
            }

    def verify(self, resource: str, namespace: str) -> dict:
        commands: list[list[str]] = []
        resource_name = resource.split("/", 1)[-1]
        if resource.startswith("deployment/"):
            commands.append(["kubectl", "-n", namespace, "rollout", "status", resource, "--timeout=45s"])
            commands.append(["kubectl", "-n", namespace, "get", resource, "-o", "wide"])
            if resource_name.endswith("service"):
                commands.append(["kubectl", "-n", namespace, "get", "endpoints", resource_name, "-o", "wide"])
        if resource.startswith("service/"):
            commands.append(["kubectl", "-n", namespace, "get", "endpoints", resource_name, "-o", "wide"])
        commands.append(["kubectl", "-n", namespace, "get", "pods", "-o", "wide"])

        outputs: list[str] = []
        ok = True
        for command in commands:
            try:
                completed = subprocess.run(command, check=False, capture_output=True, text=True)
            except FileNotFoundError:
                return {
                    "ok": False,
                    "phase": "verify",
                    "command": " | ".join(" ".join(item) for item in commands),
                    "output": "kubectl is not installed or not available on PATH.",
                    "message": "Could not verify recovery because kubectl is unavailable.",
                }

            output = (completed.stdout + completed.stderr).strip()
            outputs.append(f"$ {' '.join(command)}\n{output}")
            if completed.returncode != 0:
                ok = False

        return {
            "ok": ok,
            "phase": "verify",
            "command": " | ".join(" ".join(item) for item in commands),
            "output": "\n\n".join(outputs),
            "message": "Verification completed." if ok else "Verification found remaining issues.",
        }

    def _build_action(self, title: str, description: str, namespace: str, resource: str, patch: dict) -> FixAction:
        patch_json = json.dumps(patch, separators=(",", ":"))
        preview = f"kubectl -n {namespace} patch {resource} --type merge -p '{patch_json}'"
        return FixAction(
            title=title,
            description=description,
            resource=resource,
            namespace=namespace,
            patch=patch,
            command_preview=preview,
        )

    @staticmethod
    def serialize(actions: list[FixAction]) -> list[dict]:
        return [asdict(action) for action in actions]
