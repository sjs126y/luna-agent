"""Built-in review workflow plugin entrypoint."""


def register(ctx=None) -> None:
    from personal_agent.plugins.builtin.workflows.review.workflow import register as register_review

    register_review(ctx)
