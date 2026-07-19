"""Built-in review workflow plugin entrypoint."""


def register(ctx=None) -> None:
    from luna_agent.plugins.builtin.workflows.review.workflow import register as register_review

    register_review(ctx)
