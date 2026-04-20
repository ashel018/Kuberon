# Service Selector Runbook

## Symptoms

- Service is reachable, but traffic never reaches healthy pods
- Endpoint list is empty
- Labels or selectors drifted during a deployment change

## Checks

1. Inspect `kubectl get endpoints` for the service.
2. Compare service selectors with pod labels.
3. Confirm the deployment still publishes the expected app label.

## Fixes

- Restore the correct selector labels on the service.
- Reconcile deployment labels if they drifted.
- Recheck endpoints after the selector change.
