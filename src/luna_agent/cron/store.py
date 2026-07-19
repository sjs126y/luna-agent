"""CronStore — jobs.json persistence."""

from __future__ import annotations

import logging
from pathlib import Path

from luna_agent.cron.entry import CronEntry
from luna_agent.persistence.json_store import backup_corrupt_file, read_json, write_json_atomic

logger = logging.getLogger(__name__)


class CronStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load_all(self) -> list[CronEntry]:
        if not self._path.exists():
            return []
        data = read_json(self._path, [])
        if not isinstance(data, list):
            backup_corrupt_file(self._path)
            logger.error("Cron jobs state is not a list: %s", self._path)
            return []
        jobs = []
        for item in data:
            try:
                if isinstance(item, dict):
                    jobs.append(CronEntry(**item))
            except Exception:
                logger.exception("Failed to load cron job entry")
        return jobs

    def save_all(self, jobs: list[CronEntry]) -> None:
        data = [
            {k: v for k, v in j.__dict__.items()}
            for j in jobs
        ]
        write_json_atomic(self._path, data)

    def seed_defaults(self) -> list[CronEntry]:
        """Create default jobs if jobs.json doesn't exist."""
        if self._path.exists():
            return self.load_all()

        import uuid
        jobs = [
            CronEntry(
                job_id=str(uuid.uuid4()),
                name="daily-summary",
                schedule="0 21 * * *",
                prompt="请帮我总结今天发生的事情，包括对话历史中的关键信息。用中文，200字以内。",
                session_key="feishu::",
                platform="feishu",
                chat_id="",
                next_run=0,  # will be calculated on first tick
            ),
            CronEntry(
                job_id=str(uuid.uuid4()),
                name="morning-brief",
                schedule="0 8 * * *",
                prompt="早上好！请根据我的记忆和最近对话，告诉我今天可能需要注意的事项。用中文，简洁。",
                session_key="feishu::",
                platform="feishu",
                chat_id="",
                next_run=0,
            ),
        ]
        self.save_all(jobs)
        logger.info("Seeded %d default cron jobs", len(jobs))
        return jobs
