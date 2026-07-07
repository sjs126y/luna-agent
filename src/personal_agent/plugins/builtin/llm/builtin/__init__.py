"""Built-in LLM provider plugin entrypoint."""


def register(ctx) -> None:
    from personal_agent.llm.transport_registry import transport_registry
    from personal_agent.plugins.builtin.llm.builtin.anthropic import AnthropicMessagesTransport
    from personal_agent.plugins.builtin.llm.builtin.chat_completions import ChatCompletionsTransport
    from personal_agent.plugins.builtin.llm.builtin.responses import CodexResponsesTransport, OpenAIResponsesTransport

    transport_registry.register(
        "anthropic_messages",
        lambda provider: AnthropicMessagesTransport(provider),
    )
    transport_registry.register(
        "chat_completions",
        lambda provider: ChatCompletionsTransport(provider),
    )
    transport_registry.register(
        "responses",
        lambda provider: OpenAIResponsesTransport(provider),
    )
    transport_registry.register(
        "codex_responses",
        lambda provider: CodexResponsesTransport(provider),
    )
