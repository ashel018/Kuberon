# OOMKill Runbook

## Symptoms

- Pod cycles into `CrashLoopBackOff`
- `kubectl describe pod` reports `OOMKilled`
- Recent changes increased memory pressure or reduced limits

## Checks

1. Inspect pod events and restart count.
2. Check memory requests and limits on the deployment.
3. Confirm memory usage trend in Prometheus over the last hour.

## Fixes

- Raise the memory limit to a sane baseline.
- Right-size requests so the scheduler still places the pod.
- Investigate the release that increased working set size.

