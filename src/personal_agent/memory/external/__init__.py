"""External memory contracts, fallback, and provider routing."""

from personal_agent.memory.external.base import ExternalMemoryProvider
from personal_agent.memory.external.fallback import FallbackMemoryProvider
from personal_agent.memory.external.router import ExternalMemoryRouter

__all__ = ["ExternalMemoryProvider", "ExternalMemoryRouter", "FallbackMemoryProvider"]
