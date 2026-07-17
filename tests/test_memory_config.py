from types import SimpleNamespace

from personal_agent.memory.config import resolve_memory_context


def test_memory_context_inherits_llm_and_resolves_selected_backend_keys() -> None:
    settings = SimpleNamespace(
        memory_external_provider="lumora", memory_llm_provider="inherit",
        llm_provider="deepseek", llm_model="deepseek-chat", llm_base_url="https://llm",
        llm_api_key="llm-secret", llm_api_mode="chat_completions",
        memory_provider_options={"lumora": {
            "embedding": {"provider": "openai_compatible", "api_key_env": "DASHSCOPE_API_KEY"},
            "vector": {"provider": "qdrant", "api_key_env": "QDRANT_API_KEY"},
        }},
        get_env=lambda name, default="": {
            "DASHSCOPE_API_KEY": "bailian-secret",
        }.get(name, default),
    )

    context = resolve_memory_context(settings)

    assert context.llm.model == "deepseek-chat"
    assert context.get_env("DASHSCOPE_API_KEY") == "bailian-secret"
    assert context.get_env("QDRANT_API_KEY") == ""
    assert context.provider_options["embedding"]["provider"] == "openai_compatible"


def test_memory_context_does_not_read_process_environment(monkeypatch) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "process-secret")
    settings = SimpleNamespace(
        memory_external_provider="lumora", memory_llm_provider="inherit",
        llm_provider="deepseek", llm_model="deepseek-chat", llm_base_url="https://llm",
        llm_api_key="llm-secret", llm_api_mode="chat_completions",
        memory_provider_options={"lumora": {
            "embedding": {"api_key_env": "DASHSCOPE_API_KEY"},
        }},
    )

    context = resolve_memory_context(settings)

    assert context.get_env("DASHSCOPE_API_KEY") == ""
