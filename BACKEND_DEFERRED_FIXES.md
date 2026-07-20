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

Status: implemented; native Windows smoke validation remains pending.

Native Windows `auto` and `appcontainer` now create a per-plugin AppContainer
profile, grant only the package/environment read roots and generation data write
root, inherit only framed-RPC stdio handles, and assign the suspended Worker to a
kill-on-close Job Object before resuming it. Network capability is omitted unless
explicitly enabled. All setup failures remain fail-closed; `process-only` remains
development-only. This branch was completed under WSL, so the native launcher
still needs a Windows CI or physical-host smoke run before release certification.

### Installed Package Migration

Status: completed for the current installation on 2026-07-20.

`productivity/document-converter`, `external/markdown-structure-analyzer`, and
`integrations/workspace-watch` were rebuilt and reinstalled with SDK `>=0.3` and
complete `requires.python` declarations. Package data and old digests were
preserved. The pre-migration install state is backed up at
`data/plugins/install-state.pre-isolation-v022.json`, and the current local
configuration now enables external isolation.

### Worker Crash Recovery

Status: completed.

Unexpected Worker exit marks the generation failed/recovering, rejects new calls,
uses configurable bounded backoff, validates the replacement capability contract,
publishes a fresh snapshot, and restarts an enabled active runner. Repeated
failures open a circuit breaker. Info/doctor and the event journal expose state,
restart/failure counts, last exit/error, next retry, and circuit state. Normal
shutdown and Unix terminal signals do not enter recovery.

### Environment Garbage Collection

Status: completed.

Environment creation remains content-addressed. Cross-process file leases protect
live Workers, while installed versions and active generations contribute retained
references. `plugins environments` reports retained/removable environments and
size; `plugins gc-environments` is dry-run by default and requires `--apply` to
delete. Invalid installed manifests and invalid environment metadata are retained
conservatively, and `.staging` is never collected by this command.
