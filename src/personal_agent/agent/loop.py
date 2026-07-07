"""run_conversation — the core while loop: LLM call → parse → tools → continue.

Retry strategies (6 types, Hermes pattern):
  empty_content_retries   — LLM returned no text, no tool_calls → nudge "请继续。"
  invalid_tool_retries    — tool_call has empty input → nudge with bad tool names
  invalid_json_retries    — response failed to parse as JSON → nudge to retry
  post_tool_empty_retried — after tools ran, LLM returned empty → nudge once
  incomplete_scratchpad_retries — Anthropic-specific (reserved)
  thinking_prefill_retries      — thinking block prefill failed (reserved)
"""

from __future__ import annotations

import asyncio
import hashlib
import logging

from personal_agent.agent.report import TurnReportRecorder
from personal_agent.context_budget import compose_context_text, estimate_context_budget
from personal_agent.conversation.events import ConversationEvent, emit_delta, emit_event
from personal_agent.llm.base import LLMRequestPlan
from personal_agent.tools.executor import execute_tool_calls

logger = logging.getLogger(__name__)


async def run_conversation(agent, ctx, *, event_sink=None, confirm=None, steer=None, session_key: str = "") -> dict:
    """Execute the agent while loop. Returns final result dict."""
    just_executed_tools = False
    report_recorder = TurnReportRecorder(event_sink)

    def _with_turn_report(result: dict) -> dict:
        report_recorder.report.finish(result)
        result["turn_report"] = report_recorder.report.as_dict()
        return result

    await emit_event(
        report_recorder,
        "turn_start",
        "开始处理",
        turn_id=getattr(ctx, "turn_id", ""),
        user_message=getattr(ctx, "user_message", ""),
        message_count=len(getattr(ctx, "messages", []) or []),
        was_compressed=bool(getattr(ctx, "was_compressed", False)),
        attachments_count=len(getattr(ctx, "processed_attachments", []) or []),
        attachment_kinds=list((getattr(ctx, "multimodal_diagnostics", {}) or {}).get("attachment_kinds") or []),
        multimodal_diagnostics=dict(getattr(ctx, "multimodal_diagnostics", {}) or {}),
    )
    if getattr(ctx, "was_compressed", False):
        await emit_event(
            report_recorder,
            "compression",
            "历史消息已压缩",
            pre_message_count=getattr(ctx, "pre_compress_message_count", 0),
            post_message_count=len(ctx.messages),
        )

    while agent._iteration_budget > 0:
        if agent._interrupt_requested:
            logger.info("Agent interrupted by user")
            await _emit_stop(report_recorder, reason="user")
            return _with_turn_report({
                "final_response": "已停止。",
                "messages": ctx.messages,
                "api_calls": agent.session_api_calls,
                "completed": False,
                "status": "stopped",
                "error": "",
            })

        await _consume_steer(ctx, steer, session_key, report_recorder)

        # ── build api_messages (injections, NOT persisted) ──
        skill_injection_for_plan = getattr(ctx, "skill_injection", None)
        api_messages = await _build_api_messages(agent, ctx)

        # ── refresh system prompt if tools changed ──
        system_prompt = agent._cached_system_prompt or ""

        # ── hooks: on_before_llm_call ──
        hook_result = await agent.hooks.fire(
            "on_before_llm_call",
            api_messages, system_prompt, agent.tools,
        )
        hook_changed_messages = False
        if isinstance(hook_result, dict):
            hook_changed_messages = "messages" in hook_result
            api_messages = hook_result.get("messages", api_messages)
            system_prompt = hook_result.get("system_prompt", system_prompt)
        request_plan = (
            LLMRequestPlan.from_legacy(
                api_messages,
                system_prompt,
                agent.tools,
                metadata={"source": "hook" if hook_changed_messages else "legacy"},
            )
            if hook_changed_messages
            else _build_request_plan(agent, ctx, system_prompt, skill_injection_for_plan)
        )
        context_budget = _build_request_context_budget(
            agent,
            ctx,
            api_messages,
            system_prompt,
            skill_injection=skill_injection_for_plan,
            use_actual_messages=hook_changed_messages,
        )
        context_usage_payload = _context_usage_payload(context_budget)

        # ── LLM call (interruptible — polls _interrupt_requested every 5s) ──
        try:
            await emit_event(
                report_recorder,
                "llm_start",
                "请求模型",
                api_calls=agent.session_api_calls + 1,
                message_count=len(api_messages),
                tool_count=len(agent.tools),
                model=getattr(agent._provider, "model", ""),
                **context_usage_payload,
            )

            # Only stream token-by-token events when a renderer opts in. On the
            # platform path (event_sink=None or wants_deltas=False) on_delta stays
            # None, so the transport still streams but emits no per-token events.
            # Only stream token-by-token when a renderer opts in. On the platform
            # path (event_sink=None or wants_deltas=False) we omit the on_delta
            # kwarg entirely, so transports without streaming support keep working.
            call_kwargs = {
                "messages": api_messages,
                "system_prompt": system_prompt,
                "tools": agent.tools,
                "max_tokens": agent._provider.max_tokens,
            }
            if hasattr(agent._transport, "build_request_from_plan"):
                call_kwargs["request_plan"] = request_plan
            if getattr(report_recorder, "wants_deltas", False):
                async def on_delta(kind: str, chunk: str) -> None:
                    if kind == "thinking":
                        await emit_delta(report_recorder, "thinking_delta", chunk)
                    else:
                        await emit_delta(report_recorder, "assistant_delta", chunk)

                call_kwargs["on_delta"] = on_delta

            llm_task = asyncio.create_task(agent._transport.call(**call_kwargs))
            while not llm_task.done():
                done, _ = await asyncio.wait([llm_task], timeout=5.0)
                if llm_task.done():
                    break
                if agent._interrupt_requested:
                    llm_task.cancel()
                    logger.info("LLM call interrupted by /stop")
                    await _emit_stop(report_recorder, reason="user")
                    return _with_turn_report({
                        "final_response": "已停止。",
                        "messages": ctx.messages,
                        "api_calls": agent.session_api_calls,
                        "completed": False,
                        "status": "stopped",
                        "error": "",
                    })
            response = await llm_task
        except asyncio.CancelledError:
            await _emit_stop(report_recorder, reason="interrupt")
            return _with_turn_report({
                "final_response": "已停止。",
                "messages": ctx.messages,
                "api_calls": agent.session_api_calls,
                "completed": False,
                "status": "stopped",
                "error": "",
            })
        except Exception as exc:
            if _looks_like_image_unsupported_error(exc) and not getattr(ctx, "_image_retry_text_only", False):
                stripped = _strip_image_blocks(ctx.messages)
                if stripped:
                    ctx._image_retry_text_only = True
                    diagnostics = dict(getattr(ctx, "multimodal_diagnostics", {}) or {})
                    diagnostics["provider_rejected_images"] = True
                    diagnostics["native_retry_text_only"] = True
                    ctx.multimodal_diagnostics = diagnostics
                    await emit_event(
                        report_recorder,
                        "retry",
                        "模型不接受图片输入，已切换为纯文本重试",
                        category="multimodal_fallback",
                        attempt=1,
                        max_attempts=1,
                        error=f"{type(exc).__name__}: {exc}",
                        recoverable=True,
                    )
                    ctx.messages.append({
                        "role": "user",
                        "content": [{"type": "text", "text": "图片输入被 provider 拒绝，本轮已改为纯文本处理。"}],
                    })
                    continue
            # ── retry: invalid JSON / parse error ──
            if _looks_like_parse_error(exc) and \
               agent._retry.invalid_json_retries < agent._retry.MAX_INVALID_JSON:
                agent._retry.invalid_json_retries += 1
                await emit_event(
                    report_recorder,
                    "retry",
                    "模型返回格式异常，准备重试",
                    category="invalid_json",
                    attempt=agent._retry.invalid_json_retries,
                    max_attempts=agent._retry.MAX_INVALID_JSON,
                    error=f"{type(exc).__name__}: {exc}",
                    recoverable=True,
                )
                ctx.messages.append({
                    "role": "user",
                    "content": [{"type": "text", "text":
                        "Your previous response was malformed. Please retry with a valid response."}],
                })
                logger.debug("Invalid JSON retry %d: %s", agent._retry.invalid_json_retries, exc)
                continue
            logger.exception("LLM call failed (non-retryable)")
            await _emit_error(report_recorder, "模型调用失败", exc, category="llm")
            return _with_turn_report({
                "final_response": f"抱歉，模型调用出错了：{exc}",
                "messages": ctx.messages,
                "api_calls": agent.session_api_calls,
                "completed": False,
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            })

        agent.session_prompt_tokens += response.usage.get("input_tokens", 0)
        agent.session_completion_tokens += response.usage.get("output_tokens", 0)
        agent.session_api_calls += 1
        cache_diagnostics = (
            agent._transport.last_cache_diagnostics()
            if hasattr(agent._transport, "last_cache_diagnostics")
            else {}
        )
        await emit_event(
            report_recorder,
            "llm_end",
            "模型返回",
            input_tokens=response.usage.get("input_tokens", 0),
            output_tokens=response.usage.get("output_tokens", 0),
            cache_hit_tokens=response.usage.get("cache_hit_tokens", 0),
            cache_miss_tokens=response.usage.get("cache_miss_tokens", 0),
            cache_write_tokens=response.usage.get("cache_write_tokens", 0),
            cache_read_tokens=response.usage.get("cache_read_tokens", 0),
            cache_hit_rate=response.usage.get("cache_hit_rate", 0.0),
            cache_diagnostics=cache_diagnostics,
            tool_call_count=len(response.tool_calls or []),
            finish_reason=response.finish_reason,
            model=response.model or getattr(agent._provider, "model", ""),
            context_window=getattr(agent._provider, "context_window", 0),
            **context_usage_payload,
        )
        if agent._compressor is not None:
            try:
                agent._compressor.update_from_response(response)
            except Exception:
                logger.exception("Compressor usage update failed")

        # ── hooks: on_after_llm_call ──
        modified = await agent.hooks.fire("on_after_llm_call", response, response.usage)
        if isinstance(modified, dict):
            response.text = modified.get("text", response.text)

        # ── retry: empty response ──
        if not response.text and not response.tool_calls:
            # Post-tool empty: specific retry (different nudge)
            if just_executed_tools and not agent._retry.post_tool_empty_retried:
                agent._retry.post_tool_empty_retried = True
                await emit_event(
                    report_recorder,
                    "retry",
                    "工具执行后模型空回复，准备重试",
                    category="post_tool_empty",
                    attempt=1,
                    max_attempts=1,
                    recoverable=True,
                )
                ctx.messages.append({
                    "role": "user",
                    "content": [{"type": "text", "text":
                        "You just executed tools but gave no response. "
                        "Please provide a summary of what was done and the results."}],
                })
                logger.debug("Post-tool empty retry")
                just_executed_tools = False
                continue
            # Generic empty: retry with continue nudge
            if agent._retry.empty_content_retries < agent._retry.MAX_EMPTY_CONTENT:
                agent._retry.empty_content_retries += 1
                await emit_event(
                    report_recorder,
                    "retry",
                    "模型空回复，准备重试",
                    category="empty_response",
                    attempt=agent._retry.empty_content_retries,
                    max_attempts=agent._retry.MAX_EMPTY_CONTENT,
                    recoverable=True,
                )
                ctx.messages.append({
                    "role": "user",
                    "content": [{"type": "text", "text": "请继续。"}],
                })
                logger.debug("Empty response retry %d", agent._retry.empty_content_retries)
                just_executed_tools = False
                continue
            return _with_turn_report({
                "final_response": "(empty response from model)",
                "messages": ctx.messages,
                "api_calls": agent.session_api_calls,
                "completed": True,
                "status": "completed",
                "error": "",
            })

        # ── retry: invalid JSON in tool calls ──
        invalid_tools = [
            tc for tc in (response.tool_calls or [])
            if not tc.get("input") and tc.get("name")
        ]
        if invalid_tools and response.tool_calls:
            if agent._retry.invalid_tool_retries < agent._retry.MAX_INVALID_TOOL:
                agent._retry.invalid_tool_retries += 1
                bad_names = ", ".join(tc["name"] for tc in invalid_tools)
                await emit_event(
                    report_recorder,
                    "retry",
                    "工具参数格式异常，准备重试",
                    category="invalid_tool",
                    attempt=agent._retry.invalid_tool_retries,
                    max_attempts=agent._retry.MAX_INVALID_TOOL,
                    tool_names=bad_names,
                    recoverable=True,
                )
                ctx.messages.append({
                    "role": "user",
                    "content": [{"type": "text", "text":
                        f"Your previous tool call(s) had invalid JSON arguments: {bad_names}. "
                        f"Please retry with valid JSON arguments."}],
                })
                logger.debug("Invalid tool retry %d: %s", agent._retry.invalid_tool_retries, bad_names)
                just_executed_tools = False
                continue

        # ── no tool_calls → done ──
        if not response.tool_calls:
            if response.text:
                ctx.messages.append({
                    "role": "assistant",
                    "content": [{"type": "text", "text": response.text}],
                })
                await emit_event(report_recorder, "assistant_message", response.text)
            if await _consume_steer(ctx, steer, session_key, report_recorder):
                just_executed_tools = False
                continue
            if not response.text:
                ctx.messages.append({
                    "role": "assistant",
                    "content": [{"type": "text", "text": response.text}],
                })
                await emit_event(report_recorder, "assistant_message", response.text)
            break

        # ── has tool_calls → execute ──
        assistant_blocks = []
        if response.text:
            assistant_blocks.append({"type": "text", "text": response.text})
        for tc in response.tool_calls:
            assistant_blocks.append({
                "type": "tool_use",
                "id": tc["id"],
                "name": tc["name"],
                "input": tc["input"],
            })
        ctx.messages.append({"role": "assistant", "content": assistant_blocks})

        if response.text:
            await emit_event(report_recorder, "assistant_message", response.text)

        tool_results = await execute_tool_calls(
            response.tool_calls,
            ctx.messages,
            agent=agent,
            hooks=agent.hooks,
            event_sink=report_recorder,
            confirm=confirm,
        )
        just_executed_tools = True

        permission_message = _permission_required_stop_message(tool_results)
        if permission_message:
            ctx.messages.append({
                "role": "assistant",
                "content": [{"type": "text", "text": permission_message}],
            })
            await emit_event(report_recorder, "assistant_message", permission_message)
            break

        # ── iteration budget check ──
        agent._iteration_budget -= 1
        if agent._iteration_budget <= 0:
            ctx.messages.append({
                "role": "user",
                "content": [{"type": "text", "text": "请总结一下已完成的操作。"}],
            })
            await emit_event(
                report_recorder,
                "retry",
                "达到迭代上限，要求模型总结",
                category="iteration_budget",
                attempt=1,
                max_attempts=1,
                recoverable=True,
            )
            break

    # ── final response ──
    final_text = ""
    if ctx.messages and ctx.messages[-1]["role"] == "assistant":
        for block in ctx.messages[-1].get("content", []):
            if block.get("type") == "text":
                final_text += block["text"]

    result = {
        "final_response": final_text,
        "messages": ctx.messages,
        "api_calls": agent.session_api_calls,
        "completed": True,
        "should_review_memory": ctx.should_review_memory,
        "status": "completed",
        "error": "",
    }
    await emit_event(
        report_recorder,
        "turn_end",
        "处理完成",
        status=result["status"],
        completed=result["completed"],
        final_response=final_text,
        api_calls=agent.session_api_calls,
        should_review_memory=ctx.should_review_memory,
    )
    return _with_turn_report(result)


def _looks_like_parse_error(exc: Exception) -> bool:
    """Check if an exception is likely a JSON/parse error that should be retried."""
    msg = str(exc).lower()
    keywords = ("json", "parse", "decode", "malformed", "unexpected token",
                "invalid character", "expecting")
    return any(kw in msg for kw in keywords)


def _looks_like_image_unsupported_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    keywords = (
        "image",
        "vision",
        "multi-modal",
        "multimodal",
        "unsupported content",
        "invalid content type",
        "image_url",
    )
    return any(keyword in msg for keyword in keywords)


def _strip_image_blocks(messages: list[dict]) -> bool:
    stripped = False
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        filtered = [
            block for block in content
            if not (isinstance(block, dict) and block.get("type") in {"image_url", "image"})
        ]
        if len(filtered) != len(content):
            message["content"] = filtered or [{"type": "text", "text": "图片内容已移除。"}]
            stripped = True
    return stripped


async def _emit_stop(
    sink,
    *,
    reason: str,
    message: str = "已停止",
    stopped_tools: int = 0,
    stopped_agents: int = 0,
) -> None:
    if sink is None:
        return
    await sink.emit(ConversationEvent(
        type="stop",
        message=message,
        data={
            "reason": reason,
            "message": message,
            "stopped_tools": stopped_tools,
            "stopped_agents": stopped_agents,
        },
    ))


async def _emit_error(sink, message: str, exc: Exception, *, category: str) -> None:
    error = f"{type(exc).__name__}: {exc}"
    await emit_event(
        sink,
        "error",
        message,
        error=error,
        category=category,
        recoverable=False,
        detail_id=_event_detail_id(category, error),
    )


def _event_detail_id(category: str, detail: str) -> str:
    digest = hashlib.sha1(f"{category}:{detail}".encode("utf-8", errors="replace")).hexdigest()
    return f"err_{digest[:12]}"


async def _consume_steer(ctx, steer, session_key: str, sink) -> bool:
    if steer is None:
        return False
    turn_id = str(getattr(ctx, "turn_id", "") or "")
    if not turn_id:
        return False
    consume = getattr(steer, "consume", None)
    if consume is None:
        return False
    signals = consume(session_key, turn_id)
    if not signals:
        return False
    text = _format_steer_message(signals)
    ctx.messages.append({
        "role": "user",
        "content": [{"type": "text", "text": text}],
    })
    await emit_event(
        sink,
        "steer_consumed",
        "已应用运行中修正",
        count=len(signals),
        steer_ids=[signal.id for signal in signals],
        text_preview="; ".join(_signal_preview(signal.text) for signal in signals),
    )
    return True


def _format_steer_message(signals) -> str:
    lines = [
        "[高优先级运行中用户指令]",
        "以下内容是用户在当前任务执行过程中追加的最新指令，优先级高于本轮较早的用户请求。",
        "请立即根据这条最新指令调整接下来的回答和工具使用；如果它要求停止、返回结果或改变方向，应优先服从。",
        "用户最新指令：",
    ]
    if len(signals) == 1:
        lines.append(str(signals[0].text or "").strip())
    else:
        lines[2] = "请立即根据这些最新指令调整接下来的回答和工具使用；如果它们要求停止、返回结果或改变方向，应优先服从。"
        for index, signal in enumerate(signals, 1):
            lines.append(f"{index}. {str(signal.text or '').strip()}")
    return "\n".join(line for line in lines if line)


def _signal_preview(value: str, limit: int = 120) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


async def _build_api_messages(agent, ctx) -> list[dict]:
    """Build messages for LLM: messages + injections. NOT persisted."""
    msgs = list(ctx.messages)

    if ctx.skill_summaries:
        msgs.insert(0, {
            "role": "user",
            "content": [{"type": "text", "text": ctx.skill_summaries}],
        })

    # Skill injection: /skill-name content, injected ONCE then consumed
    if ctx.skill_injection:
        msgs.insert(0, {
            "role": "user",
            "content": [{"type": "text", "text": ctx.skill_injection}],
        })
        ctx.skill_injection = None  # consumed — won't inject again this turn

    # Memory prefetch: external provider results injected as prefix
    for message in reversed(ctx.memory_prefetch_messages):
        msgs.insert(0, message)

    return msgs


def _build_request_plan(agent, ctx, system_prompt: str, skill_injection: str | None) -> LLMRequestPlan:
    dynamic_context: list[dict] = []
    dynamic_context.extend(list(getattr(ctx, "memory_prefetch_messages", []) or []))
    if skill_injection:
        dynamic_context.append({
            "role": "user",
            "content": [{"type": "text", "text": skill_injection}],
        })
    if getattr(ctx, "skill_summaries", ""):
        dynamic_context.append({
            "role": "user",
            "content": [{"type": "text", "text": ctx.skill_summaries}],
        })

    messages = list(getattr(ctx, "messages", []) or [])
    current_idx = int(getattr(ctx, "current_turn_user_idx", max(0, len(messages) - 1)) or 0)
    current_user = messages[current_idx] if 0 <= current_idx < len(messages) else None
    history = [
        message
        for index, message in enumerate(messages)
        if index != current_idx
    ]
    return LLMRequestPlan(
        stable_system=system_prompt,
        stable_tools=list(agent.tools or []),
        stable_context=[],
        dynamic_context=dynamic_context,
        history=history,
        current_user=current_user,
        metadata={"source": "agent_context"},
    )


def _build_request_context_budget(
    agent,
    ctx,
    api_messages: list[dict],
    system_prompt: str,
    *,
    skill_injection: str | None,
    use_actual_messages: bool = False,
):
    provider = getattr(agent, "_provider", None)
    model = getattr(provider, "model", "") or getattr(agent, "model", "")
    context_limit = int(getattr(provider, "context_window", 0) or 0)

    if use_actual_messages:
        messages = api_messages
        skills_summary = ""
        memory_injections = ""
    else:
        messages = list(getattr(ctx, "messages", []) or [])
        skills_summary = compose_context_text(
            getattr(ctx, "skill_summaries", "") or "",
            skill_injection or "",
        )
        memory_injections = getattr(ctx, "memory_injections_text", "") or ""

    budget = estimate_context_budget(
        messages=messages,
        system_prompt=system_prompt,
        tools=list(getattr(agent, "tools", []) or []),
        skills_summary=skills_summary,
        memory_injections=memory_injections,
        context_limit=context_limit,
        model=model,
    )
    compressor = getattr(agent, "_compressor", None)
    threshold_tokens = int(getattr(compressor, "threshold_tokens", 0) or 0)
    if threshold_tokens:
        budget.compression_threshold = threshold_tokens
    return budget


def _context_usage_payload(budget) -> dict:
    return {
        "context_used_tokens": budget.used,
        "context_remaining_tokens": budget.remaining_context,
        "context_percent": budget.percent,
        "context_budget": budget.as_dict(),
    }


def _permission_required_stop_message(tool_results: list) -> str:
    if not tool_results or not all(_is_permission_required_denial(result) for result in tool_results):
        return ""
    categories = _permission_required_categories(tool_results)
    if categories == ["network"]:
        return "网络工具需要授权，本轮已停止。请发送 /allow network 后重试。"
    if len(categories) == 1:
        return f"工具需要授权，本轮已停止。请发送 /allow {categories[0]} 后重试。"
    return f"多个工具需要授权，本轮已停止。请发送 /allow all 或分别授权：{', '.join(categories)}。"


def _is_permission_required_denial(result) -> bool:
    return (
        getattr(result, "status", "") == "denied"
        and getattr(result, "category", "") == "authorization"
        and getattr(result, "reason_code", "") == "permission_required"
    )


def _permission_required_categories(tool_results: list) -> list[str]:
    categories: list[str] = []
    for result in tool_results:
        category = str(
            getattr(result, "required_allow", "")
            or getattr(result, "permission_category", "")
            or "tool"
        )
        if category and category not in categories:
            categories.append(category)
    return categories
