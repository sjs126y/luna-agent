from personal_agent.agent.agent import _maybe_refresh_memory_snapshot, init_agent
from personal_agent.llm.provider import ProviderProfile
from personal_agent.memory.models import InternalMemorySnapshot


class Manager:
    def __init__(self):
        self.revision = 1

    def get_internal_snapshot(self, session_key):
        return InternalMemorySnapshot("default", self.revision, f"memory-{self.revision}")

    def get_system_prompt_text(self):
        raise AssertionError("pinned snapshot should be used")


def test_agent_pins_and_refreshes_internal_memory_at_turn_boundary() -> None:
    manager = Manager()
    provider = ProviderProfile("test", "", "", "model")
    agent = init_agent(
        object(), provider, memory_manager=manager, memory_session_key="cli:1",
        memory_snapshot_refresh_interval=2,
    )
    assert "memory-1" in agent._cached_system_prompt
    manager.revision = 2
    assert _maybe_refresh_memory_snapshot(agent) is False
    assert _maybe_refresh_memory_snapshot(agent) is True
    assert agent._internal_memory_snapshot.revision == 2
    assert agent._cached_system_prompt is None
