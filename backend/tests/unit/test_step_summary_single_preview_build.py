"""build_step_context_summary must run build_data_preview exactly once.

The projection helper also knows how to build a ui_preview (for legacy prompt
paths that only hold five rows); the full-data builder replaces it with the
20-row preview. Building both on every Step write doubled the clamping and
byte-accounting cost for a result that was always discarded.
"""
from app.ai import persisted_summary


def test_full_step_summary_builds_the_preview_once(monkeypatch):
    calls = []
    real = persisted_summary.build_data_preview

    def counting(data, **kwargs):
        calls.append(len(data.get("rows") or []))
        return real(data, **kwargs)

    monkeypatch.setattr(persisted_summary, "build_data_preview", counting)

    rows = [{"a": i} for i in range(100)]
    summary = persisted_summary.build_step_context_summary(
        {"rows": rows, "columns": [{"field": "a"}], "info": {"total_rows": 100}}
    )

    assert len(calls) == 1, f"expected one preview build, saw {len(calls)}"
    assert calls[0] == persisted_summary.UI_STEP_PREVIEW_ROWS
    assert summary["ui_preview"]["rows"]
    assert summary["version"] == persisted_summary.CONTEXT_SUMMARY_VERSION
    assert len(summary["preview_rows"]) == persisted_summary.STEP_PREVIEW_ROWS


def test_projection_summary_still_includes_ui_preview_by_default():
    rows = [{"a": i} for i in range(7)]
    summary = persisted_summary.build_step_context_summary_from_projection(
        info={"total_rows": 7},
        columns=[{"field": "a"}],
        preview_rows=rows,
    )
    assert isinstance(summary.get("ui_preview"), dict)
    assert summary["version"] == persisted_summary.CONTEXT_SUMMARY_VERSION
