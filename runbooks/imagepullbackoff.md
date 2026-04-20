# ImagePullBackOff Runbook

## Symptoms

- Pod remains in `ImagePullBackOff` or `ErrImagePull`
- Events mention manifest not found or registry authentication failure

## Checks

1. Inspect the image name and tag on the deployment.
2. Verify the image exists in the target registry.
3. Confirm image pull secrets and registry reachability.

## Fixes

- Restore the correct image tag.
- Recreate or rebind the image pull secret if credentials expired.
- Roll the deployment after the image reference is corrected.

