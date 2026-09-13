"""Data apps keep existing query types, snapshot values, and scoped bindings."""

import pytest

from app.services.artifact_payload import build_params_payload
from app.ai.agents.planner.artifact_refinement import ArtifactRefinementBudget


@pytest.mark.parametrize(
    "value", [None, "West", ["Direct", "Partner"], {"from": "2025-03-01", "to": "2025-06-30"}, 125.5]
)
def test_parameter_context_preserves_snapshot_values_and_all_consumers(value):
    kind = (
        "list"
        if isinstance(value, list)
        else "date_range"
        if isinstance(value, dict)
        else "number"
        if isinstance(value, float)
        else "string"
    )
    parameter = {"name": "scope", "type": kind, "default": "unused"}
    visualizations = [
        {"id": vid, "query_id": qid, "parameters": [parameter], "applied_params": {"scope": value}}
        for vid, qid in [("chart", "sales"), ("table", "sales"), ("detail", "accounts")]
    ]
    payload = build_params_payload(visualizations)
    assert payload["values"]["scope"] == value
    assert len(payload["declarations"]) == 1
    assert set(payload["declarations"][0]["query_ids"]) == {"sales", "accounts"}
    assert set(payload["declarations"][0]["visualization_ids"]) == {"chart", "table", "detail"}


def test_identity_values_are_not_projected_into_anonymous_snapshots():
    payload = build_params_payload(
        [
            {
                "id": "v",
                "query_id": "q",
                "parameters": [
                    {"name": "viewer", "source": "identity", "default": "owner@example.com"},
                    {
                        "name": "region",
                        "options": ["East", {"value": "West", "label": "Western market"}],
                        "options_source": {"query_id": "choices", "value_column": "region"},
                        "strict_options": True,
                    },
                ],
                "applied_params": {"viewer": "private@example.com"},
            }
        ]
    )
    assert "viewer" not in payload["values"]
    declaration = next(p for p in payload["declarations"] if p["name"] == "region")
    assert declaration["options_source"]["query_id"] == "choices"
    assert declaration["strict_options"] is True
    assert payload["options"]["region"] == [
        {"value": "East", "label": "East"},
        {"value": "West", "label": "Western market"},
    ]


def test_optional_visual_refinement_is_bounded_without_consuming_requested_edits():
    budget = ArtifactRefinementBudget()
    for name in ["create_artifact", "read_artifact", "edit_artifact"]:
        assert budget.allow(name, {})
    assert budget.allow("edit_artifact", {"purpose": "visual_refinement"})
    assert not budget.allow("edit_artifact", {"purpose": "visual_refinement"})
    assert budget.allow("edit_artifact", {"purpose": "requested_change"})
    assert ArtifactRefinementBudget().allow("edit_artifact", {"purpose": "visual_refinement"})


def test_identity_defaults_do_not_copy_the_snapshot_owners_value():
    payload = build_params_payload(
        [
            {
                "id": "v",
                "query_id": "q",
                "parameters": [
                    {
                        "name": "department",
                        "source": "input_identity_default",
                        "default": None,
                        "identity_binding": "viewer.profile_attributes.department",
                    },
                ],
                "applied_params": {"department": "private-team"},
            }
        ]
    )
    assert payload["values"]["department"] is None
    assert "private-team" not in str(payload)
