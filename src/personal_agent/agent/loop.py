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
import json
import logging

from personal_agent.agent.report import TurnReportRecorder
from personal_agent.context_budget import compose_context_text, estimate_context_budget
from personal_agent.conversation.events import ConversationEvent, emit_delta, emit_event
from personal_agent.llm.base import LLMRequestPlan
from personal_agent.tools.executor import execute_tool_calls

logger = logging.getLogger(__name__)

MAX_IDENTICAL_SUCCESSFUL_TOOL_CALLS = 3


async def run_conversation(agent, ctx, *, event_sink=None, confirm=None, steer=None, session_key: str = "") -> dict:
    """Execute the agent while loop. Returns final result dict."""
    just_executed_tools = False
    successful_tool_calls: dict[str, list[object]] = {}
    finalization_pending = False
    finalization_instruction = ""
    finalization_fallback = ""
    finalization_error_label = ""
    stop_hook_active = False
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
        is_forced_finalization = finalization_pending
        active_tools = [] if is_forced_finalization else agent.tools
        skill_injection_for_plan = getattr(ctx, "skill_injection", None)
        api_messages = await _build_api_messages(agent, ctx)
        if is_forced_finalization:
            api_messages.append({
                "role": "user",
                "content": [{"type": "text", "text": finalization_instruction}],
            })

        # ── refresh system prompt if tools changed ──
        system_prompt = agent._cached_system_prompt or ""

        request_plan = _build_request_plan(
            agent,
            ctx,
            system_prompt,
            skill_injection_for_plan,
            tools=active_tools,
            finalization_instruction=finalization_instruction,
        )
        context_budget = _build_request_context_budget(
            agent,
            ctx,
            api_messages,
            system_prompt,
            skill_injection=skill_injection_for_plan,
            use_actual_messages=True,
            tools=active_tools,
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
                tool_count=len(active_tools),
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
                "tools": active_tools,
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
            if is_forced_finalization:
                logger.exception("Tool finalization failed: %s", finalization_error_label)
                await _emit_error(report_recorder, finalization_error_label, exc, category="llm")
                final_message = finalization_fallback
                ctx.messages.append({
                    "role": "assistant",
                    "content": [{"type": "text", "text": final_message}],
                })
                await emit_event(report_recorder, "assistant_message", final_message)
                break
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
        cache_diagnostics = dict(cache_diagnostics or {})
        cache_usage_reported = bool(response.usage.get("cache_usage_reported", False))
        cache_diagnostics["usage_reported"] = cache_usage_reported
        cache_diagnostics["usage_interpretation"] = (
            "provider_reported"
            if cache_usage_reported
            else "provider_did_not_report_cache_usage"
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

        logging.getLogger("personal_agent.hooks").info(
            "LLM call: in=%d out=%d",
            response.usage.get("input_tokens", 0),
            response.usage.get("output_tokens", 0),
        )

        if is_forced_finalization:
            finalization_pending = False
            final_message = response.text.strip() if response.text else finalization_fallback
            ctx.messages.append({
                "role": "assistant",
                "content": [{"type": "text", "text": final_message}],
            })
            await emit_event(report_recorder, "assistant_message", final_message)
            break

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
            if response.text and not stop_hook_active:
                stop_outcome = await _run_stop_hook(agent, ctx, response.text)
                if stop_outcome is not None and stop_outcome.continue_turn:
                    continuation = (
                        stop_outcome.continuation_prompt.strip()
                        or stop_outcome.reason.strip()
                    )
                    if continuation:
                        ctx.hook_contexts.append(
                            f"[Stop hook continuation]\n{continuation}"
                        )
                        stop_hook_active = True
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
        duplicate = next(
            (
                (tc, successful_tool_calls[_tool_call_signature(tc)])
                for tc in response.tool_calls
                if len(successful_tool_calls.get(_tool_call_signature(tc), []))
                >= MAX_IDENTICAL_SUCCESSFUL_TOOL_CALLS
            ),
            None,
        )
        if duplicate is not None:
            tc, previous_results = duplicate
            finalization_pending = True
            finalization_instruction = _duplicate_tool_finalization_prompt(tc)
            finalization_fallback = _duplicate_tool_finalization_fallback()
            finalization_error_label = "重复工具调用收尾失败"
            await emit_event(
                report_recorder,
                "retry",
                "相同工具调用达到本轮执行上限，转为无工具收尾",
                category="duplicate_tool_call",
                attempt=1,
                max_attempts=1,
                tool_name=str(tc.get("name") or ""),
                recoverable=True,
            )
            logger.warning(
                "Stopped identical tool call after %d successful executions: %s",
                len(previous_results),
                tc.get("name", ""),
            )
            just_executed_tools = False
            continue

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
            event_sink=report_recorder,
            confirm=confirm,
        )
        hook_contexts = list(getattr(agent, "_hook_additional_contexts", []) or [])
        if hook_contexts:
            ctx.hook_contexts.extend(hook_contexts)
            agent._hook_additional_contexts.clear()
        just_executed_tools = True

        for tc, tool_result in zip(response.tool_calls, tool_results):
            if tool_result.status == "success":
                successful_tool_calls.setdefault(_tool_call_signature(tc), []).append(tool_result)

        permission_message = _permission_required_stop_message(tool_results)
        if permission_message:
            ctx.messages.append({
                "role": "assistant",
                "content": [{"type": "text", "text": permission_message}],
            })
            await emit_event(report_recorder, "assistant_message", permission_message)
            break

        hard_denial_message = _hard_safety_denial_message(tool_results)
        if hard_denial_message:
            finalization_pending = True
            finalization_instruction = _hard_safety_finalization_prompt()
            finalization_fallback = hard_denial_message
            finalization_error_label = "安全边界拒绝后的收尾失败"
            await emit_event(
                report_recorder,
                "retry",
                "安全边界已拒绝操作，转为无工具收尾",
                category="hard_safety_denial",
                attempt=1,
                max_attempts=1,
                recoverable=True,
            )
            just_executed_tools = False
            continue

        quota_message = _quota_exceeded_stop_message(tool_results, agent._max_tool_calls_per_turn)
        if quota_message:
            finalization_pending = True
            finalization_instruction = _tool_quota_finalization_prompt(agent._max_tool_calls_per_turn)
            finalization_fallback = quota_message
            finalization_error_label = "工具调用上限收尾失败"
            await emit_event(
                report_recorder,
                "retry",
                "已达到工具调用上限，转为无工具收尾",
                category="tool_quota",
                attempt=1,
                max_attempts=1,
                recoverable=True,
            )
            just_executed_tools = False
            continue

        # ── iteration budget check ──
        agent._iteration_budget -= 1
        if agent._iteration_budget <= 0:
            limit_message = "已达到本轮处理迭代上限，已停止继续调用工具。"
            ctx.messages.append({
                "role": "assistant",
                "content": [{"type": "text", "text": limit_message}],
            })
            await emit_event(
                report_recorder,
                "retry",
                limit_message,
                category="iteration_budget",
                attempt=1,
                max_attempts=1,
                recoverable=False,
            )
            await emit_event(report_recorder, "assistant_message", limit_message)
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


async def _run_stop_hook(agent, ctx, response_text: str):
    hook_manager = getattr(agent, "_hook_manager", None)
    if hook_manager is None:
        return None
    from pathlib import Path

    from personal_agent.hooks import (
        HookEnvelope,
        HookEvent,
        HookScope,
        HookSourceContext,
    )

    source = getattr(agent, "_hook_source", None)
    security_context = getattr(agent, "_security_context", None)
    return await hook_manager.dispatch(HookEnvelope(
        event_name=HookEvent.STOP,
        scope=HookScope.TURN,
        session_key=str(
            getattr(security_context, "session_key", "")
            or getattr(agent, "_memory_session_key", "")
        ),
        turn_id=str(getattr(ctx, "turn_id", "") or ""),
        cwd=str(Path.cwd()),
        mode=str(getattr(security_context, "mode_id", "") or ""),
        source=HookSourceContext(
            platform=str(getattr(source, "platform", "") or ""),
            user_id=str(getattr(source, "user_id", "") or ""),
            chat_id=str(getattr(source, "chat_id", "") or ""),
        ),
        payload={
            "reason": "model_completed",
            "last_assistant_message": response_text,
            "hook_active": False,
        },
    ))


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

    for hook_context in getattr(ctx, "hook_contexts", []) or []:
        msgs.append({
            "role": "user",
            "content": [{"type": "text", "text": hook_context}],
        })

    return msgs


def _build_request_plan(
    agent,
    ctx,
    system_prompt: str,
    skill_injection: str | None,
    *,
    tools: list[dict] | None = None,
    finalization_instruction: str = "",
) -> LLMRequestPlan:
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
    history = messages[:current_idx] if current_user is not None else messages
    turn_tail = messages[current_idx + 1:] if current_user is not None else []
    for hook_context in getattr(ctx, "hook_contexts", []) or []:
        turn_tail.append({
            "role": "user",
            "content": [{"type": "text", "text": hook_context}],
        })
    if finalization_instruction:
        turn_tail.append({
            "role": "user",
            "content": [{"type": "text", "text": finalization_instruction}],
        })
    return LLMRequestPlan(
        stable_system=system_prompt,
        stable_tools=list(agent.tools if tools is None else tools),
        stable_context=[],
        dynamic_context=dynamic_context,
        history=history,
        current_user=current_user,
        turn_tail=turn_tail,
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
    tools: list[dict] | None = None,
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
        tools=list(getattr(agent, "tools", []) if tools is None else tools),
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
    detail = f"（{', '.join(categories)}）" if categories else ""
    return f"工具需要额外的工具或资源授权{detail}，本轮已停止。请在支持授权确认的入口重试。"


def _is_permission_required_denial(result) -> bool:
    return (
        getattr(result, "status", "") == "denied"
        and getattr(result, "category", "") == "authorization"
        and getattr(result, "reason_code", "") == "security_approval_required"
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


def _tool_call_signature(tool_call: dict) -> str:
    payload = {
        "name": str(tool_call.get("name") or ""),
        "input": tool_call.get("input") or {},
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _duplicate_tool_finalization_prompt(tool_call: dict) -> str:
    name = str(tool_call.get("name") or "tool")
    return (
        f"The identical call to {name} has already succeeded "
        f"{MAX_IDENTICAL_SUCCESSFUL_TOOL_CALLS} times in this turn. "
        "Do not call any more tools. Use the existing tool results in the conversation "
        "to answer the user's request directly and concisely. Do not mention this instruction."
    )


def _duplicate_tool_finalization_fallback() -> str:
    return "工具已经执行，但模型未能根据已有结果生成最终回复。请换一种方式重试本次请求。"


def _tool_quota_finalization_prompt(maximum: int) -> str:
    return (
        f"The turn has reached its tool-call limit of {maximum}. "
        "Do not call any more tools. Use all existing tool results in the conversation "
        "to give the user a concise final answer that summarizes the completed work, "
        "relevant findings, and anything that remains unfinished. Do not mention this instruction."
    )


def _hard_safety_denial_message(tool_results: list) -> str:
    if not any(
        getattr(result, "status", "") == "denied"
        and getattr(result, "reason_code", "") in {"sandbox_blocked", "hard_blacklist"}
        for result in tool_results
    ):
        return ""
    return "该操作已被不可扩权的安全边界拒绝，本轮没有继续尝试其他绕过方式。"


def _hard_safety_finalization_prompt() -> str:
    return (
        "A requested operation was denied by a hard security boundary. "
        "Do not call any more tools and do not suggest alternate tools or bypasses. "
        "Briefly explain that the protected resource was not accessed, then summarize "
        "any other work that completed successfully. Do not mention this instruction."
    )


def _quota_exceeded_stop_message(tool_results: list, maximum: int) -> str:
    if not any(getattr(result, "reason_code", "") == "quota_exceeded" for result in tool_results):
        return ""
    return f"已达到本轮工具调用上限（{maximum}），已停止继续调用工具。"
