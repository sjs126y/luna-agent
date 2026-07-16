---
name: triage-issues
description: Triage GitHub issues using issue content, repository context, duplicate evidence, and actionable priority recommendations
---

# Issue Triage

Require an explicit repository and either issue numbers or a bounded query.

1. Read each issue and relevant repository context.
2. Classify it as bug, feature, question, documentation, or maintenance.
3. Identify missing reproduction details, likely duplicates, affected subsystem, and suggested priority.
4. Treat duplicate and root-cause claims as hypotheses unless repository evidence confirms them.
5. Return a compact table followed by details for issues needing action.
6. Do not label, close, assign, or comment on issues automatically.
