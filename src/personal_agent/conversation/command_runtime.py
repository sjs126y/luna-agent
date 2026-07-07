"""Shared command runtime behavior backed by ConversationService."""

from __future__ import annotations


class ConversationCommandRuntime:
    reset_session_response = "会话已重置。开始新的对话吧。"
    usage_create_agent = True
    usage_empty_message = "暂无会话数据。"
    allow_all_cached_agents = False

    async def get_or_create_agent(self):
        return await self.conversation_service.get_or_create_agent(self.session_key)

    async def get_agent(self):
        return await self.get_or_create_agent()

    async def reset_session(self) -> str:
        await self.conversation_service.reset_session(self.session_key, self.source)
        return self.reset_session_response

    async def clear_agent(self) -> None:
        self.conversation_service.clear_agent(self.session_key)

    async def list_sessions(self) -> str:
        platform, user_id = self.session_owner()
        return await self.conversation_service.session_list_summary(
            platform=platform,
            user_id=user_id,
            current_key=self.session_list_current_key(),
        )

    async def current_session(self) -> str:
        return await self.conversation_service.current_session_summary(
            self.session_key, self.source
        )

    async def load_history(self) -> list[dict]:
        return await self.conversation_service.load_history(self.session_key, self.source)

    async def export_session(self) -> tuple[int, str]:
        export_path = self.conversation_service.default_export_path(self.session_key)
        count = await self.conversation_service.export_session(
            self.session_key, self.source, export_path
        )
        return count, str(export_path)

    async def memory_report(self) -> dict:
        return await self.conversation_service.memory_manager.health_snapshot()

    async def memory_entries(self, *, target: str = "all") -> list[dict]:
        return await self.conversation_service.memory_manager.list_entries(target=target)

    async def memory_search(self, query: str, *, target: str = "all") -> list[dict]:
        return await self.conversation_service.memory_manager.search_entries(query, target=target)

    async def memory_entry(self, identifier: str, *, target: str = "all") -> dict | None:
        return await self.conversation_service.memory_manager.get_entry(identifier, target=target)

    async def memory_delete(self, identifier: str, *, target: str = "all") -> bool:
        return await self.conversation_service.memory_manager.delete(identifier, target=target)

    async def activity_snapshot(self, *, limit: int = 20) -> dict:
        from personal_agent.activity import activity_snapshot

        return activity_snapshot(gateway_snapshot=self._gateway_snapshot(), limit=limit)

    async def activity_detail(self, kind: str, id_: str) -> dict | None:
        from personal_agent.activity import activity_detail

        return activity_detail(kind, id_, gateway_snapshot=self._gateway_snapshot())

    async def activity_choices(self, provider: str, *, query: str = "", limit: int = 20) -> list[dict]:
        from personal_agent.activity import activity_choices

        return activity_choices(
            provider,
            query=query,
            limit=limit,
            gateway_snapshot=self._gateway_snapshot(),
        )

    def slash_command_metadata(self) -> list[dict]:
        from personal_agent.commands.runtime import slash_command_metadata

        return slash_command_metadata(self)

    async def slash_argument_choices(
        self,
        provider: str,
        *,
        command: str = "",
        args: tuple[str, ...] = (),
        query: str = "",
        limit: int = 20,
    ) -> list[dict]:
        from personal_agent.commands.runtime import slash_argument_choices

        return await slash_argument_choices(
            self,
            provider,
            command=command,
            args=args,
            query=query,
            limit=limit,
        )

    async def usage(self, *, current_user_message: str = "") -> str:
        return await self.conversation_service.usage_summary(
            self.session_key,
            self.source,
            current_user_message=current_user_message,
            create_agent=self.usage_create_agent,
            empty_message=self.usage_empty_message,
        )

    async def tool_runs_recent(
        self,
        *,
        limit: int = 10,
        all_sessions: bool = False,
    ) -> dict:
        return await self.conversation_service.queries.recent_tool_runs(
            limit=limit,
            session_key=None if all_sessions else self.session_key,
        )

    async def tool_run_detail(self, run_id: int) -> dict | None:
        return await self.conversation_service.queries.tool_run_detail(run_id)

    async def tool_runs_summary(
        self,
        *,
        limit: int = 50,
        all_sessions: bool = False,
    ) -> dict:
        return await self.conversation_service.queries.tool_run_summary(
            limit=limit,
            session_key=None if all_sessions else self.session_key,
        )

    async def current_execution_mode(self) -> str:
        from personal_agent.commands.runtime import current_mode, current_mode_from_policy

        agent = self.conversation_service.get_cached_agent(self.session_key)
        if agent is None:
            return current_mode_from_policy(getattr(self.settings, "execution_policy", None))
        return current_mode(agent)

    async def allow_category(self, category: str) -> str:
        if self.allow_all_cached_agents:
            self.conversation_service.allow_all_cached_agents(category)
        else:
            if not self.conversation_service.allow_agent_category(self.session_key, category):
                await self.get_agent()
                self.conversation_service.allow_agent_category(self.session_key, category)
        return f"已授权 {category} 操作，本轮对话内有效。"

    async def is_session_running(self) -> bool:
        snapshot = self.conversation_service.steer_snapshot(self.session_key)
        return bool(snapshot.get("active_turn_id"))

    async def add_steer(self, text: str) -> str:
        if not str(text or "").strip():
            return "用法: /steer <运行中修正内容>"
        if not await self.is_session_running():
            return "当前没有运行中的任务可修正。"
        signal = self.conversation_service.add_steer(self.session_key, self.source, text)
        return f"已收到，会在当前任务下一步应用。（{signal.id}）"

    async def steer_snapshot(self) -> dict:
        return self.conversation_service.steer_snapshot(self.session_key)

    async def stop_agents(self) -> str:
        stopped = self.conversation_service.request_stop(None)
        if stopped:
            return f"已停止。已请求停止 {stopped} 个子 agent。"
        return "已停止。"

    def session_owner(self) -> tuple[str, str]:
        return self.source.platform, self.source.user_id

    def session_list_current_key(self) -> str:
        return self.session_key

    def _gateway_snapshot(self) -> dict:
        gateway = getattr(self, "gateway", None)
        if gateway is not None and hasattr(gateway, "health_snapshot"):
            return gateway.health_snapshot()
        app_runtime = getattr(self, "app_runtime", None)
        gateway = getattr(app_runtime, "gateway", None)
        if gateway is not None and hasattr(gateway, "health_snapshot"):
            return gateway.health_snapshot()
        return {}
