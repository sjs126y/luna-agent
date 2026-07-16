---
name: release-notes
description: Draft user-facing release notes from verified GitHub commits, pull requests, tags, and issue references
---

# Release Notes

Require `owner/repo` and a tag, commit range, milestone, or bounded time range.

1. Resolve the requested range and collect merged pull requests and significant commits.
2. Group changes into features, fixes, performance, security, and maintenance only when supported.
3. Prefer PR descriptions and changed behavior over raw commit-message wording.
4. Mention breaking changes, migrations, and configuration impact prominently.
5. Link referenced PRs or issues. Exclude internal churn unless it affects users or maintainers.
6. Produce a draft only; do not create a GitHub release.
