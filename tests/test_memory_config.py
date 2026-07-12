from types import SimpleNamespace

from personal_agent.memory.config import resolve_memory_context


def test_memory_context_inherits_llm_and_resolves_bailian_key(monkeypatch) -> None:
    monkeypatch.setenv("DASHSCOPE_API_KEY", "bailian-secret")
    settings = SimpleNamespace(
        memory_external_provider="lumora", memory_llm_provider="inherit",
        llm_provider="deepseek", llm_model="deepseek-chat", llm_base_url="https://llm",
        llm_api_key="llm-secret", llm_api_mode="chat_completions",
        memory_embedding_api_key="", memory_embedding_api_key_env="DASHSCOPE_API_KEY",
        memory_embedding_api_mode="openai_compatible", memory_embedding_base_url="https://embedding",
        memory_embedding_model="text-embedding-v4", memory_embedding_dimensions=0,
        memory_qdrant_api_key="", memory_qdrant_api_key_env="QDRANT_API_KEY",
        memory_qdrant_url="http://qdrant", memory_qdrant_collection="memory",
        memory_qdrant_timeout_seconds=10, memory_provider_options={"lumora": {"rrf_k": 60}},
        raw_env={},
    )

    context = resolve_memory_context(settings)

    assert context.llm.model == "deepseek-chat"
    assert context.embedding.api_key == "bailian-secret"
    assert context.embedding.model == "text-embedding-v4"
    assert context.provider_options == {"rrf_k": 60}
