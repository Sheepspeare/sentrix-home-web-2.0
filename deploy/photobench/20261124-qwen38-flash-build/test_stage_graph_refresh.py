"""Read-only graph proposals retain causal results and archive only genuine differences."""

import stage_graph_refresh as stage


def test_projection_preserves_successful_causal_edge_without_rejudging():
    causal = {
        "id": "causal",
        "scope_id": "scope",
        "source_id": "asset:a",
        "target_id": "asset:b",
        "relation": "asset_causes_asset:ENABLES",
        "weight": 0.8,
        "attributes_json": '{"reason":"original reason"}',
        "build_revision": "old",
        "created_at": "old",
    }
    obsolete = {**causal, "id": "obsolete", "relation": "observation_mentions_concept"}
    delta = stage.graph_delta({"causal": causal, "obsolete": obsolete}, [], "edge")
    assert delta["add"] == delta["update"] == []
    assert delta["remove"] == [obsolete] and delta["desired_count"] == 1


def test_equal_attributes_with_different_json_order_do_not_change_nodes():
    current = {
        "id": "event:a",
        "scope_id": "scope",
        "node_type": "event",
        "label": "title",
        "attributes_json": '{"a":1,"b":2}',
    }
    desired = {
        "id": "event:a",
        "scope_id": "scope",
        "node_type": "event",
        "label": "title",
        "attributes": {"b": 2, "a": 1},
    }
    delta = stage.graph_delta({"event:a": current}, [desired], "node")
    assert delta["add"] == delta["remove"] == delta["update"] == []
    desired["label"] = "updated title"
    delta = stage.graph_delta({"event:a": current}, [desired], "node")
    assert delta["update"] == [{"before": current, "after": desired}]
