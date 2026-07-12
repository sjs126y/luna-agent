from personal_agent.plugins.builtin.memory.lumora.provider import reciprocal_rank_fusion


def test_reciprocal_rank_fusion_combines_semantic_and_bm25() -> None:
    scores = reciprocal_rank_fusion(["semantic", "both"], ["both", "keyword"])

    assert scores["both"] > scores["semantic"]
    assert scores["semantic"] > scores["keyword"]
