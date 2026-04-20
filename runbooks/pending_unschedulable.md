# Pending Unschedulable Runbook

## Symptoms

- Pod remains in `Pending`
- Events mention `0/3 nodes are available` or unschedulable resource requests

## Checks

1. Inspect recent scheduler events.
2. Compare pod CPU and memory requests against node capacity.
3. Confirm node selectors and taints are satisfiable.

## Fixes

- Reduce requests to a realistic baseline.
- Remove impossible node selectors.
- Add capacity only if the workload sizing is already correct.

