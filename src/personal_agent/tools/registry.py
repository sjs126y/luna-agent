"""ToolRegistry — module-level singleton. Tools self-register on import.

get_definitions(enabled_toolsets, quiet_mode): resolves toolsets, returns
Anthropic-format schemas. LRU cache (8 entries) when quiet_mode=True.
On bridge mode: deferrable tools replaced with tool_search/describe/call.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from personal_agent.tools.entry import ToolEntry

logger = logging.getLogger(__name__)


_QUERY_ALIASES: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("后台", "长任务", "server", "watcher", "守护", "跑测试", "跑服务"),
        "process_start background long-running server watcher",
    ),
    (("看后台", "进程列表", "任务列表"), "process_list background process"),
    (("读后台输出", "日志", "poll", "进度"), "process_read since_last output log"),
    (("停止进程", "kill", "终止后台"), "process_kill stop process"),
    (("找文件", "文件名", "路径匹配"), "glob file pattern"),
    (("搜内容", "全文搜索", "grep", "包含文本"), "grep file content regex"),
    (("读文件", "打开文件"), "read file"),
    (("写文件", "覆盖文件"), "write file"),
    (("修改文件", "替换文本", "追加"), "edit file replace append"),
    (("网页", "搜索互联网", "最新"), "web_search network web"),
    (("打开网页", "抓页面", "fetch url"), "web_fetch network web"),
)


class ToolRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, ToolEntry] = {}
        self._generation: int = 0
        self._defs_cache: dict[tuple, list[dict]] = {}  # key → cached result
        self._catalog_cache: dict[tuple, list[dict]] = {}
        self._cache_maxsize = 8

    # ── registration ──────────────────────────────────

    def register(self, entry: ToolEntry) -> None:
        self._entries[entry.name] = entry
        self._generation += 1

    def unregister(self, name: str) -> None:
        if name in self._entries:
            del self._entries[name]
            self._generation += 1

    def invalidate(self) -> None:
        """Invalidate cached definitions when dynamic availability changes."""
        self._generation += 1

    def get(self, name: str) -> ToolEntry | None:
        return self._entries.get(name)

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def all_names(self) -> set[str]:
        return set(self._entries.keys())

    # ── definitions ────────────────────────────────────

    def get_definitions(
        self,
        enabled_toolsets: list[str] | None = None,
        *,
        quiet_mode: bool = True,
        skip_bridge: bool = False,
    ) -> list[dict]:
        """Return Anthropic-format tool schemas for resolved toolsets.

        quiet_mode=True → cache result (8-entry LRU). Used by Agent init.
        skip_bridge=True → return ALL schemas (used by tool_search index).
        """
        from personal_agent.tools.toolsets import resolve_toolsets, is_core_tool

        # Cache key: toolsets + generation + skip_bridge
        cache_key = (
            frozenset(enabled_toolsets or []),
            self._generation,
            skip_bridge,
        )

        if quiet_mode and cache_key in self._defs_cache:
            return list(self._defs_cache[cache_key])

        # Resolve which tools to include
        resolved = resolve_toolsets(enabled_toolsets, self.all_names)

        # Check dependencies
        active: list[ToolEntry] = []
        for name in sorted(resolved):
            entry = self._entries.get(name)
            if entry is None:
                continue
            if entry.check_fn and not entry.check_fn():
                logger.debug("Tool '%s' skipped: check_fn returned False", name)
                continue
            active.append(entry)

        # Build schemas
        if skip_bridge:
            result = [_entry_to_schema(e) for e in active]
        else:
            result = _assemble_with_bridge(active, is_core_tool)

        # Cache if quiet
        if quiet_mode:
            if len(self._defs_cache) >= self._cache_maxsize:
                oldest = next(iter(self._defs_cache))
                del self._defs_cache[oldest]
            self._defs_cache[cache_key] = result

        return result

    # ── dispatch ───────────────────────────────────────

    async def dispatch(self, name: str, args: dict) -> str:
        entry = self._entries.get(name)
        if entry is None:
            return f"Error: unknown tool '{name}'"
        try:
            return await entry.handler(**args)
        except Exception as exc:
            logger.exception("Tool '%s' failed", name)
            return f"Error: {exc}"

    # ── catalog ────────────────────────────────────────

    def catalog(self, enabled_toolsets: list[str] | None = None) -> list[dict]:
        """Return stable metadata for registered tools without executing them."""
        from personal_agent.tools.toolsets import TOOLSETS, is_core_tool, resolve_toolsets

        cache_key = (frozenset(enabled_toolsets or []), self._generation)
        if cache_key in self._catalog_cache:
            return [dict(item) for item in self._catalog_cache[cache_key]]

        resolved = resolve_toolsets(enabled_toolsets, self.all_names)
        result: list[dict] = []
        for name in sorted(resolved):
            entry = self._entries.get(name)
            if entry is None:
                continue
            available, unavailable_reason = _entry_availability(entry)
            result.append({
                "name": entry.name,
                "description": entry.description,
                "input_schema": entry.schema,
                "toolset": entry.toolset,
                "groups": _tool_groups(entry.name, entry.toolset, TOOLSETS),
                "permission_category": entry.permission_category,
                "tags": list(entry.tags),
                "risk_level": _normalize_risk_level(entry.risk_level, entry),
                "usage_hint": entry.usage_hint,
                "is_core": is_core_tool(entry.name),
                "is_parallel_safe": entry.is_parallel_safe,
                "is_destructive": entry.is_destructive,
                "approval_mode": entry.approval_mode,
                "idempotent": entry.idempotent,
                "has_resource_resolver": entry.resource_resolver is not None,
                "has_precheck": entry.precheck is not None,
                "has_check_fn": entry.check_fn is not None,
                "available": available,
                "unavailable_reason": unavailable_reason,
                "input_properties": sorted(
                    (entry.schema.get("properties") or {}).keys()
                    if isinstance(entry.schema, dict)
                    else []
                ),
            })
        if len(self._catalog_cache) >= self._cache_maxsize:
            oldest = next(iter(self._catalog_cache))
            del self._catalog_cache[oldest]
        self._catalog_cache[cache_key] = result
        return [dict(item) for item in result]

    def catalog_summary(self, enabled_toolsets: list[str] | None = None) -> dict:
        items = self.catalog(enabled_toolsets)
        by_toolset = Counter(str(item["toolset"]) for item in items)
        by_permission = Counter(str(item["permission_category"]) for item in items)
        by_risk = Counter(str(item["risk_level"]) for item in items)
        by_approval = Counter(str(item["approval_mode"]) for item in items)
        by_tag = Counter(tag for item in items for tag in item["tags"])
        high_risk_categories = {"write", "bash", "background", "network"}
        high_risk = [
            item["name"]
            for item in items
            if (
                item["is_destructive"]
                or item["permission_category"] in high_risk_categories
                or item["risk_level"] == "high"
            )
        ]
        unavailable = [
            {"name": item["name"], "reason": item["unavailable_reason"]}
            for item in items
            if not item["available"]
        ]
        return {
            "total": len(items),
            "available": sum(1 for item in items if item["available"]),
            "unavailable": len(unavailable),
            "core": sum(1 for item in items if item["is_core"]),
            "parallel_safe": sum(1 for item in items if item["is_parallel_safe"]),
            "destructive": sum(1 for item in items if item["is_destructive"]),
            "by_toolset": dict(sorted(by_toolset.items())),
            "by_permission": dict(sorted(by_permission.items())),
            "by_risk": dict(sorted(by_risk.items())),
            "by_approval": dict(sorted(by_approval.items())),
            "by_tag": dict(sorted(by_tag.items())),
            "high_risk": high_risk,
            "unavailable_tools": unavailable,
            "items": items,
        }


tool_registry = ToolRegistry()


# ── helpers ────────────────────────────────────────────

def _entry_to_schema(entry: ToolEntry) -> dict:
    return {
        "name": entry.name,
        "description": entry.description,
        "input_schema": entry.schema,
    }


def _entry_availability(entry: ToolEntry) -> tuple[bool, str]:
    if entry.check_fn is None:
        return True, ""
    try:
        if entry.check_fn():
            return True, ""
    except Exception as exc:
        return False, f"check_fn error: {type(exc).__name__}: {exc}"
    return False, "check_fn returned False"


def _normalize_risk_level(risk_level: str, entry: ToolEntry) -> str:
    value = str(risk_level or "").strip().lower()
    if entry.is_destructive or entry.permission_category in {"write", "bash"}:
        return "high"
    if entry.permission_category in {"background", "network"}:
        return "high" if value == "high" else "medium"
    if value in {"low", "medium", "high"}:
        return value
    return "low"


def _tool_groups(name: str, toolset: str, groups: dict[str, set[str]]) -> list[str]:
    result = sorted(group for group, names in groups.items() if name in names)
    if toolset == "mcp" and "mcp" not in result:
        result.append("mcp")
    return result


def _assemble_with_bridge(active: list[ToolEntry], is_core) -> list[dict]:
    """Split active tools: core → full schema, deferrable → bridge tools."""
    core: list[dict] = []
    deferrable: list[dict] = []

    for entry in active:
        if is_core(entry.name):
            core.append(_entry_to_schema(entry))
        else:
            deferrable.append(_entry_to_schema(entry))

    result = list(core)

    # Only add bridge tools if there are deferrable tools
    if deferrable:
        result.append({
            "name": "tool_search",
            "description": "Search for tools by keyword. Returns matching tools with name, description, and "
                          "full input_schema. After searching, call the matched tool DIRECTLY by name.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Keywords to search for in tool names and descriptions"},
                },
                "required": ["query"],
            },
        })
        result.append({
            "name": "tool_describe",
            "description": "Get the full parameter schema for a specific tool. Use after tool_search if you need more detail.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Exact tool name from tool_search results"},
                },
                "required": ["name"],
            },
        })
        result.append({
            "name": "tool_call",
            "description": "Execute a discovered tool by name through the same security and audit pipeline as a direct call.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Tool name to execute"},
                    "arguments": {"type": "object", "description": "Tool arguments as a JSON object"},
                },
                "required": ["name", "arguments"],
            },
        })

    return result


# ── bridge tool dispatch ──────────────────────────────

async def dispatch_tool_search(query: str) -> str:
    """BM25 search over the tool catalog."""
    import json

    catalog = [
        d for d in tool_registry.catalog()
        if d["name"] not in {"tool_search", "tool_describe", "tool_call"}
    ]
    if not catalog:
        return json.dumps({"hits": [], "message": "No tools available."}, ensure_ascii=False)

    hits = _bm25_search(catalog, query)
    return json.dumps({"hits": hits}, ensure_ascii=False)


async def dispatch_tool_describe(name: str) -> str:
    """Return full schema for a specific tool."""
    import json
    for d in tool_registry.catalog():
        if d["name"] == name:
            return json.dumps(d, ensure_ascii=False)
    return json.dumps({"error": f"Tool not found: {name}"}, ensure_ascii=False)


async def dispatch_tool_call(name: str, arguments: dict) -> object:
    """Execute a tool by name."""
    entry = tool_registry.get(name)
    if entry is None:
        return f"Error: unknown tool '{name}'"
    if name == "tool_call":
        return "Error: tool_call cannot call itself"
    from personal_agent.tools.executor import execute_tool_call_result, format_tool_result
    from personal_agent.tools.runtime_context import (
        current_tool_agent,
        current_tool_confirm,
        current_tool_event_sink,
        current_tool_hooks,
    )

    agent = current_tool_agent()
    confirm = current_tool_confirm()
    hooks = current_tool_hooks()
    event_sink = current_tool_event_sink()
    result = await execute_tool_call_result(
        {
            "id": f"tool_call:{name}",
            "name": name,
            "input": arguments or {},
        },
        agent=agent,
        confirm=confirm,
        hooks=hooks,
        event_sink=event_sink,
    )
    if agent is None and confirm is None and hooks is None and event_sink is None:
        return format_tool_result(result)
    return result


# ── BM25 ──────────────────────────────────────────────

def _tokenize(text: str) -> list[str]:
    """Simple 2-char n-gram tokenizer for CJK + space-split for Latin."""
    import re
    tokens = []
    # Split on whitespace for Latin text
    for word in text.lower().split():
        if re.search(r'[一-鿿]', word):
            # CJK: 2-char ngrams
            for i in range(len(word) - 1):
                tokens.append(word[i:i + 2])
            tokens.append(word[-1])  # last char solo
        else:
            tokens.append(word)
    return tokens


def _bm25_search(catalog: list[dict], query: str, top_k: int = 5) -> list[dict]:
    """BM25 search over catalog."""
    if not catalog:
        return []

    query_text, alias_matches = _expand_query(query)
    query_tokens = _tokenize(query_text)
    if not query_tokens:
        return [_search_hit(item, score=0.0, why_matched=[]) for item in catalog[:top_k]]

    # Build per-doc token frequencies
    docs_text = [_catalog_search_text(d) for d in catalog]
    docs_tokens = [_tokenize(text) for text in docs_text]

    k1, b = 1.5, 0.75
    avgdl = sum(len(t) for t in docs_tokens) / max(len(docs_tokens), 1)
    N = len(catalog)

    # IDF
    df: dict[str, int] = {}
    for tokens in docs_tokens:
        for tok in set(tokens):
            df[tok] = df.get(tok, 0) + 1

    scores = []
    for i, doc_tokens in enumerate(docs_tokens):
        score = 0.0
        doc_len = len(doc_tokens)
        tf: dict[str, int] = {}
        for tok in doc_tokens:
            tf[tok] = tf.get(tok, 0) + 1

        why_matched: list[str] = []
        lower_query = query.lower().strip()
        lower_query_text = query_text.lower().strip()
        lower_name = str(catalog[i].get("name") or "").lower()
        lower_usage_hint = str(catalog[i].get("usage_hint") or "").lower()
        lower_tags = [str(tag).lower() for tag in catalog[i].get("tags", []) or []]

        for tok in query_tokens:
            if tok not in tf:
                continue
            idf = max(0, __import__("math").log((N - df.get(tok, 0) + 0.5) / (df.get(tok, 0) + 0.5)) + 1)
            numerator = tf[tok] * (k1 + 1)
            denominator = tf[tok] + k1 * (1 - b + b * doc_len / max(avgdl, 1))
            score += idf * numerator / max(denominator, 0.001)
        if lower_query and lower_query == lower_name:
            score += 6.0
            why_matched.append("exact name")
        elif lower_query and lower_query in lower_name:
            score += 3.0
            why_matched.append("name substring")
        if lower_query and lower_query in docs_text[i].lower():
            score += 1.0
            why_matched.append("substring")
        matched_tags = [tag for tag in lower_tags if tag and tag in lower_query_text]
        if matched_tags:
            score += 2.0 * len(matched_tags)
            why_matched.extend(f"tag: {tag}" for tag in matched_tags[:2])
        if lower_query and lower_query in lower_usage_hint:
            score += 1.5
            why_matched.append("usage_hint")
        elif any(token in lower_usage_hint for token in query_tokens):
            score += 0.75
            why_matched.append("usage_hint")
        for alias_term, alias_expansion in alias_matches:
            alias_tokens = _tokenize(alias_expansion)
            if any(token in docs_tokens[i] for token in alias_tokens):
                score += 1.5
                why_matched.append(f"alias: {alias_term} -> {alias_expansion}")
        if not catalog[i].get("available", True):
            score *= 0.5
            why_matched.append("unavailable")

        scores.append({
            **catalog[i],
            "score": round(score, 3),
            "why_matched": _trim_reasons(why_matched),
        })

    scores.sort(key=lambda x: (-float(x["score"]), str(x["name"])))
    return [
        _search_hit(s, score=float(s["score"]), why_matched=s.get("why_matched", []))
        for s in scores[:top_k]
        if s["score"] > 0
    ]


def _search_hit(item: dict, *, score: float, why_matched: list[str]) -> dict:
    return {
        "name": item["name"],
        "description": item["description"],
        "input_schema": item.get("input_schema", {}),
        "toolset": item.get("toolset", ""),
        "permission_category": item.get("permission_category", "default"),
        "risk_level": item.get("risk_level", "low"),
        "tags": item.get("tags", []),
        "usage_hint": item.get("usage_hint", ""),
        "available": item.get("available", True),
        "unavailable_reason": item.get("unavailable_reason", ""),
        "why_matched": why_matched,
        "score": round(score, 3),
    }


def _catalog_search_text(item: dict) -> str:
    return " ".join([
        str(item.get("name") or ""),
        str(item.get("description") or ""),
        " ".join(str(tag) for tag in item.get("tags", []) or []),
        str(item.get("usage_hint") or ""),
    ])


def _expand_query(query: str) -> tuple[str, list[tuple[str, str]]]:
    raw = query or ""
    lowered = raw.lower()
    matches: list[tuple[str, str]] = []
    expansions: list[str] = [raw]
    for terms, expansion in _QUERY_ALIASES:
        for term in terms:
            if term.lower() in lowered:
                matches.append((term, expansion))
                expansions.append(expansion)
                break
    return " ".join(part for part in expansions if part).strip(), matches


def _trim_reasons(reasons: list[str], limit: int = 4) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for reason in reasons:
        if reason in seen:
            continue
        result.append(reason)
        seen.add(reason)
        if len(result) >= limit:
            break
    return result
