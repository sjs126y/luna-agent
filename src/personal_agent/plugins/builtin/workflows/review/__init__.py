"""Built-in review workflow plugin entrypoint."""


def register(ctx) -> None:
    from personal_agent.workflow.builtin.review import register as register_review

    register_review(ctx)

