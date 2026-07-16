---
name: upgrade-library
description: Analyze a library upgrade using current migration guidance, changed APIs, compatibility requirements, and verification steps
---

# Library Upgrade

Require the library plus current and target versions when possible.

1. Resolve the library in Context7 and query migration, release, deprecation, and compatibility documentation.
2. Inspect the user's relevant dependency and usage files when available.
3. Separate breaking changes, deprecated behavior, required configuration changes, and optional improvements.
4. Map documented changes to the user's actual usage; do not list unrelated release notes.
5. Propose focused edits and verification commands. Do not change dependencies unless requested.
