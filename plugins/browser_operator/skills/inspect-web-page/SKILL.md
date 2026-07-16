---
name: inspect-web-page
description: Inspect a live web page with Playwright and report verified content, structure, state, and browser-visible problems
---

# Inspect Web Page

1. Require or infer one explicit URL, then navigate once.
2. Inspect the accessibility snapshot before interacting. Use screenshots only when visual state matters.
3. Report visible content, controls, navigation, errors, and relevant page state from browser evidence.
4. Do not sign in, submit forms, upload files, or change persistent state during inspection.
5. Distinguish observed behavior from likely implementation causes.
