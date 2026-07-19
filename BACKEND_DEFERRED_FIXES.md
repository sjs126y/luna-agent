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
