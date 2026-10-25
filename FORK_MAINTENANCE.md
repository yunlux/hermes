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

## Migrated customization contract

The integration branch now carries selectively ported workstation behavior on the
upstream base, rather than replaying old files over rewritten upstream modules:

- Codex grants are pinned to their auth-store authority, exact row ID and token
  pair. Refresh/rotation, reset markers and early quota reopen stay in that
  authority; same-principal grants are not interchangeable.
- Profile provider plugins can own runtime resolution, credential-pool recovery
  and account status; opted-out providers cannot silently fail over to another
  account or paid lane.
- Public model menus use the local free catalog. Account-scoped ChatGPT and Go
  routes retain native catalogs. **Menu filtering is not a request firewall.**
- The optional local request-rule policy and usage ledger retain existing behavior.
  Disabled/missing request rules are permissive; no blanket billing guarantee is
  implied. No configuration is enabled or changed merely by installing this fork.
- Upstream vision defaults, OpenRouter free-lane routing, streaming hooks and
  quota-reopen behavior are preserved. Usage bookkeeping does not consume the
  streaming timing clock.
- The 900k context variant for `gpt-6.1-sol` comes from current upstream metadata;
  it is a local variant, not a separate remote model ID.

### Test layering

Upstream transport/catalog-cache tests exercise the retained `_unfiltered_*`
catalog layer; `test_subscription_menu_filter.py` and the other local menu tests
exercise the public policy overlay. Fallback/composition fixtures explicitly
mark their synthetic model IDs as free. Browser auth tests isolate the built-in
login path from global plugin-mirroring state and use independent fake grants.
No test authorizes a live paid request.

### Private precheck settings

Cron change gates read `<profile-home>/cron/prechecks/config.json`:

```json
{"jobs": {"example-job-id": "vault"}, "vault": "/private/local/vault"}
```

The mapping and path are operator data, not repository defaults. Missing settings
mean no job commits its baseline; missing vault input fails closed. Existing
schedules, model choices and credentials are not carried in source commits.

## Deployment boundary

Updating this repository does not restart a running gateway. Deployments must
record the exact tested commit, interpreter, profile scope, rollback source and
activation status separately. A staged next-launch service override is **not**
evidence that a live process has switched code.

No scheduled synchronization or unattended deployment is enabled by this document.
