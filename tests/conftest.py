"""Shared fixtures for tests."""

import asyncio
import tempfile
from pathlib import Path

import pytest

from personal_agent.config import Settings
from personal_agent.db.database import Database


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
