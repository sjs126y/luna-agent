"""Code review workflow — multi-dimensional review with adversarial verify.

Pattern: parallel finders → pipeline verify → synthesize

Usage:
  LLM calls workflow_run("review", {
    "files": ["main.py", "gateway.py"],
    "dimensions": ["security", "performance", "bugs"]
  })
"""

from __future__ import annotations

from typing import Any

from personal_agent.workflow.primitives import agent, parallel, pipeline, phase, log
from personal_agent.workflow.registry import workflow_registry, WorkflowDef

# ── Dimension prompts ──────────────────────────────────

DIMENSIONS = {
    "security": (
        "Review for security vulnerabilities: hardcoded secrets, missing auth checks, "
        "SQL injection, path traversal, XSS, insecure deserialization, unsafe eval/exec. "
        "For each finding, provide: title, file, line hint, severity (high/medium/low), "
        "and a one-sentence description."
    ),
    "performance": (
        "Review for performance issues: N+1 queries, unnecessary allocations, "
        "blocking I/O in async code, missing caching opportunities, O(n^2) patterns, "
        "large memory footprint. For each finding, provide: title, file, line hint, "
        "and a one-sentence description."
    ),
    "bugs": (
        "Review for logic bugs: off-by-one errors, null/None handling, race conditions, "
        "incorrect error handling, type mismatches, wrong assumptions. "
        "For each finding, provide: title, file, line hint, severity (high/medium/low), "
        "and a one-sentence description."
    ),
}

FINDING_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "file": {"type": "string"},
                    "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                    "description": {"type": "string"},
                },
                "required": ["title", "file", "severity", "description"],
            },
        },
    },
    "required": ["findings"],
}

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "is_real": {"type": "boolean", "description": "Is this finding a real issue?"},
        "reason": {"type": "string", "description": "Why it is or isn't real"},
    },
    "required": ["is_real", "reason"],
}


# ── Workflow function ──────────────────────────────────

async def review_workflow(args: dict | None = None) -> dict:
    """Multi-dimensional code review with adversarial verification.

    Args:
        args: {
            "files": ["path/to/file.py", ...] or None for recent changes,
            "dimensions": ["security", "performance", "bugs"] (default: all),
        }
    """
    if args is None:
        args = {}
    target_files = args.get("files", ["(all recent changes)"])
    dimensions = args.get("dimensions", list(DIMENSIONS.keys()))
    dimensions = [d for d in dimensions if d in DIMENSIONS]

    if not dimensions:
        return {"error": "No valid dimensions specified"}

    files_str = ", ".join(target_files)

    # ── Phase 1: Parallel finders ──
    phase("Find")
    log(f"Reviewing {files_str} across {len(dimensions)} dimensions...")

    finder_tasks = []
    for dim_key in dimensions:
        prompt = (
            f"Review these files for {dim_key} issues: {files_str}\n\n"
            f"{DIMENSIONS[dim_key]}"
        )
        finder_tasks.append(lambda p=prompt: agent(
            p, schema=FINDING_SCHEMA,
            system_prompt=f"You are a {dim_key} code reviewer. Be thorough and specific.",
        ))

    finder_results = await parallel(finder_tasks)
    finder_results = [r for r in finder_results if r is not None]

    # Flatten findings
    all_findings = []
    for r in finder_results:
        all_findings.extend(r.get("findings", []))

    log(f"Found {len(all_findings)} potential issues across {len(dimensions)} dimensions")

    if not all_findings:
        return {
            "total_findings": 0,
            "confirmed": [],
            "summary": "No issues found.",
        }

    # Dedup by file+title
    seen = set()
    unique = []
    for f in all_findings:
        key = (f.get("file", ""), f.get("title", ""))
        if key not in seen:
            seen.add(key)
            unique.append(f)
    log(f"After dedup: {len(unique)} unique findings")

    # ── Phase 2: Pipeline verify ──
    phase("Verify")

    async def verify_stage(finding, _original, _idx):
        dim_label = finding.get("file", "?")
        prompt = (
            f"Adversarially verify this finding. Try to REFUTE it — if you're not "
            f"confident it's real, mark is_real=false.\n\n"
            f"Finding: {finding['title']}\n"
            f"File: {finding.get('file', 'unknown')}\n"
            f"Severity: {finding.get('severity', '?')}\n"
            f"Description: {finding.get('description', '')}"
        )
        verdict = await agent(
            prompt, schema=VERDICT_SCHEMA,
            system_prompt="You are a skeptical code reviewer. Default to refuting unless the evidence is clear.",
        )
        if verdict:
            finding["verdict"] = verdict
        return finding

    verified = await pipeline(unique, verify_stage)
    verified = [v for v in verified if v is not None]

    # Filter confirmed
    confirmed = [v for v in verified if v.get("verdict", {}).get("is_real")]
    dismissed = len(verified) - len(confirmed)
    log(f"Confirmed: {len(confirmed)}, dismissed: {dismissed}")

    # ── Phase 3: Synthesize ──
    phase("Report")
    high = [c for c in confirmed if c.get("severity") == "high"]
    medium = [c for c in confirmed if c.get("severity") == "medium"]
    low = [c for c in confirmed if c.get("severity") == "low"]

    return {
        "total_findings": len(all_findings),
        "confirmed": len(confirmed),
        "dismissed": dismissed,
        "by_severity": {"high": len(high), "medium": len(medium), "low": len(low)},
        "issues": [
            {
                "title": c["title"],
                "file": c.get("file", ""),
                "severity": c.get("severity", ""),
                "description": c.get("description", ""),
            }
            for c in confirmed
        ],
    }


# ── Register ───────────────────────────────────────────

workflow_registry.register(WorkflowDef(
    name="review",
    description="Multi-dimensional code review with adversarial verification. Finds issues across security/performance/bugs dimensions, then verifies each finding by trying to refute it.",
    fn=review_workflow,
    phases=["Find", "Verify", "Report"],
    when_to_use="When the user asks to review code, check for bugs, audit security, or analyze performance.",
))
