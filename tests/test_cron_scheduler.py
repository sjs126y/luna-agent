from types import SimpleNamespace

import pytest

from personal_agent.conversation import SessionDirectory, SubmissionOutcome, SubmissionStatus
from personal_agent.cron.entry import CronEntry
from personal_agent.cron.scheduler import CronScheduler


class SubmissionPort:
    def __init__(self):
        self.requests = []

    async def submit(self, request):
        self.requests.append(request)

        class Handle:
            async def outcome(self):
                return SubmissionOutcome(
                    request_id=request.request_id,
                    session_key=request.session_key,
                    status=SubmissionStatus.COMPLETED,
                )

        return Handle()


@pytest.mark.asyncio
async def test_cron_submits_directly_without_faking_gateway_message():
    port = SubmissionPort()
    sessions = SessionDirectory()
    scheduler = CronScheduler(SimpleNamespace(), port, sessions=sessions)
    job = CronEntry(
        job_id="job-1",
        name="brief",
        schedule="0 8 * * *",
        prompt="summarize",
        session_key="wechat:c1:u1",
        platform="wechat",
        chat_id="c1",
    )

    await scheduler._execute_job(job)

    request = port.requests[0]
    assert request.origin.value == "cron"
    assert request.response_mode.value == "deliver"
    assert request.input.text == "summarize"
    assert request.input.source.user_id == "u1"
    assert request.metadata["cron_job_id"] == "job-1"
    assert sessions.resolve("wechat:c1:u1").source.chat_id == "c1"
