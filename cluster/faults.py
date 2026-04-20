from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class FaultAction:
    description: str
    patch: dict


def build_faults(target: str) -> dict[str, FaultAction]:
    return {
        "crashloop": FaultAction(
            description="Force an OOMKill by constraining memory limits to 1Mi.",
            patch={
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": target,
                                    "resources": {
                                        "limits": {"memory": "1Mi"},
                                        "requests": {"memory": "1Mi"},
                                    },
                                }
                            ]
                        }
                    }
                }
            },
        ),
        "imagepull": FaultAction(
            description="Swap the image tag with a nonexistent tag to trigger ImagePullBackOff.",
            patch={
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": target,
                                    "image": "gcr.io/google-samples/microservices-demo/nonexistent:broken",
                                }
                            ]
                        }
                    }
                }
            },
        ),
        "pending_pod": FaultAction(
            description="Add an impossible nodeSelector so the pod remains Pending.",
            patch={
                "spec": {
                    "template": {
                        "spec": {
                            "nodeSelector": {"kubeops.dev/impossible": "true"},
                        }
                    }
                }
            },
        ),
        "svc_mismatch": FaultAction(
            description="Break a service selector so it resolves to zero endpoints.",
            patch={
                "spec": {
                    "selector": {"app": "definitely-not-the-right-workload"},
                }
            },
        ),
        "resource_hog": FaultAction(
            description="Request CPU beyond cluster capacity to make the pod unschedulable.",
            patch={
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": target,
                                    "resources": {
                                        "requests": {"cpu": "64"},
                                        "limits": {"cpu": "64"},
                                    },
                                }
                            ]
                        }
                    }
                }
            },
        ),
    }


def run_kubectl(command: list[str], dry_run: bool) -> None:
    print("kubectl " + " ".join(command))
    if dry_run:
        return
    subprocess.run(["kubectl", *command], check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Inject demo faults into the local kind cluster.")
    parser.add_argument("--fault", required=True, choices=["crashloop", "imagepull", "pending_pod", "svc_mismatch", "resource_hog"])
    parser.add_argument("--namespace", default="default")
    parser.add_argument("--target", required=True, help="Deployment or service name, for example deployment/cartservice")
    parser.add_argument("--container", help="Container name when patching a deployment. Defaults to the target resource name suffix.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    resource_name = args.target.split("/")[-1]
    patch_target = args.container or resource_name
    fault = build_faults(patch_target)[args.fault]
    patch = json.dumps(fault.patch, separators=(",", ":"))

    print(f"Injecting '{args.fault}' into {args.target} in namespace '{args.namespace}'")
    print(fault.description)
    run_kubectl(["-n", args.namespace, "patch", args.target, "--type", "merge", "-p", patch], dry_run=args.dry_run)


if __name__ == "__main__":
    main()

