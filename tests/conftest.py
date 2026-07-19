"""Shared fixtures for tests."""

import asyncio
import tempfile
from pathlib import Path

import pytest

from luna_agent.config import Settings
from luna_agent.db.database import Database


@pytest.fixture
def isolate_audit_log(tmp_path):
    from luna_agent.tools.audit import set_audit_path

    set_audit_path(tmp_path / "audit.log")
    yield
    set_audit_path(Path("./data/audit.log"))


@pytest.fixture(autouse=True)
def _isolate_audit_log(isolate_audit_log):
    yield


@pytest.fixture
def temp_db_path():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td) / "test.db"


@pytest.fixture
async def db(temp_db_path):
    database = Database(temp_db_path)
    await database.initialize()
    yield database
    await database.close()


@pytest.fixture
def config():
    return Settings(
        llm_api_key="test-key",
        llm_base_url="https://test.example.com/anthropic",
        llm_model="test-model",
        feishu_app_id="",
        feishu_app_secret="",
        telegram_bot_token="",
    )
