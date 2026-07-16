---
name: review-pr
description: Review a GitHub pull request for correctness, regressions, security risks, and missing tests using repository evidence
---

# Pull Request Review

Require `owner/repo` and a pull request number.

1. Read PR metadata, changed files, diff, review state, checks, and linked issue when available.
2. Fetch surrounding source only where the diff needs context. Do not review from the PR description alone.
3. Prioritize concrete bugs, security issues, behavioral regressions, and missing tests.
4. For every finding, identify the affected file and explain the failure scenario. Do not invent line numbers.
5. Put findings first, ordered by severity. Then give open questions and a short summary.
6. Do not submit a GitHub review or comment unless the user separately requests it and approves the write action.
