# Backend Deferred Fixes

This list records confirmed backend follow-ups that are intentionally outside
the active repair scope. It is not a Memory or identity-model backlog.

## Read-Only Additional Roots

Status: deferred pending worktree comparison and a dedicated security pass.

The benchmark reported that positive read operations under `sandbox.read_roots`
were denied in Read Only mode. The expected policy is:

- `sandbox.roots`: read access
- `sandbox.read_roots`: read access
- no write access from either root

When scheduled, compare the benchmark worktree and the target branch before
editing `security/session.py` and `security/evaluator.py`. Add coverage for
both session-backed and isolated security contexts, then run the focused
security suite. Do not change Memory scope, session keys, or user identity as
part of this item.

## External Plugin Runtime Follow-ups

### Native Windows AppContainer

Status: implemented as an explicit fail-closed boundary, backend deferred.

Linux/WSL uses Bubblewrap. Native Windows `auto` and `appcontainer` currently
reject external workers until the launcher can create an AppContainer token,
pass only the required stdio handles, and apply a Job Object. `process-only` is
available only for local development and must not be presented as a production
sandbox.

### Installed Package Migration

Status: required before enabling `plugins.runtime.isolate_external: true` in an
existing installation.

Reinstall installed packages from their current source so their manifest carries
`requires.python`, `ToolResourceBinding`, and the SDK `>=0.3` contract. Packages
created before the Worker boundary may otherwise fail with missing dependencies
or unsupported host callbacks. The host should preserve package data and old
versions while publishing the new generation.

### Worker Crash Recovery

Status: diagnostics implemented; automatic passive-worker recovery deferred.

`plugins info/doctor` reports Worker PID, return code, stderr tail, and last RPC
error. A later change should mark a crashed generation unhealthy, apply bounded
restart backoff and circuit breaking, then publish a fresh capability snapshot;
it must not silently keep routing new calls to a dead Worker.

### Environment Garbage Collection

Status: deferred.

Environment creation is content-addressed and old environments are retained for
rollback. A future maintenance command can remove environments that are not
referenced by an installed package, active generation, or retained rollback
revision, after lease and process cleanup complete.
