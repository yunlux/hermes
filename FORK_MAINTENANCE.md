# Fork maintenance

## Branch contract

- `main` is an unmodified mirror of `NousResearch/hermes-agent:main`.
- `yunlux/custom` is the integration branch for reviewed local customizations.
- Merge upstream into the custom branch; never force-push either shared branch.
- Pin deployments to a tested commit. Updating this repository does not deploy or restart a gateway.

## Upstream synchronization

From a clean checkout, verify identity and remotes before writing:

```sh
gh api user --jq .login
git remote -v
git fetch origin main
git fetch upstream main
git switch main
git merge --ff-only origin/main
git merge --ff-only upstream/main
git push origin main
git switch yunlux/custom
git merge --no-edit main
```

If fast-forward fails on `main`, stop and inspect divergence. If the integration merge conflicts, resolve and test in an isolated checkout; never overwrite a live runtime to resolve a conflict. Verify remote branch SHAs after pushing.

## Customization admission

1. Inventory local changes and classify each as upstream-equivalent, still required, obsolete, or unresolved.
2. Preserve account/provider isolation, billing protections and per-profile scope. Do not drop protections just to make the merge succeed.
3. Commit related behavior and regression tests separately. Prefer upstream behavior or a supported external plugin over permanent core patches.
4. Run `scripts/run_tests.sh` for affected areas and the repository's required checks. Use isolated test homes and no paid model calls.
5. Push only reviewed source and tests. Never upload profile configuration, credentials, databases, logs, local backup files, or raw conversation exports.
6. Do not switch production until dependencies and profile/plugin compatibility are verified. Keep a rollback commit and preserve operator configuration.

## Bootstrap status

The initial `yunlux/custom` branch contains this maintenance contract only. Existing workstation patches are **not** implicitly included, validated, or deployed. Their migration must pass the admission process above before this branch is treated as a production replacement.

No scheduled synchronization or unattended deployment is enabled by this document.
