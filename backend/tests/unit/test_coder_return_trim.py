"""The generated function must return the frame it actually built.

Regression: the post-processing that trims anything after the final return was
`re.sub(r'(?s)return\s+df.*$', 'return df', result)` — a *replacement*, not a
trim. A function ending in `return df_aggregated` came back as `return df`, so
the pre-aggregation frame shipped in place of the summary: a request for
"total quantity per planner" rendered as several hundred raw rows, with no
error anywhere.
"""
import inspect

from app.ai.agents.coder.coder import Coder, trim_after_final_df_return as _trim


def test_aggregated_frame_is_not_rewritten_to_df():
    code = "def generate_df(a, b):\n    df = load()\n    df_aggregated = df.groupby('p').sum()\n    return df_aggregated\n"
    assert _trim(code).rstrip().endswith("return df_aggregated")


def test_plain_return_df_is_preserved():
    code = "def generate_df(a, b):\n    df = load()\n    return df\n"
    assert _trim(code).rstrip().endswith("return df")


def test_trailing_junk_after_the_return_is_dropped():
    code = "def generate_df(a, b):\n    return df_summary\n\n# example usage\ngenerate_df(1, 2)\nSome prose.\n"
    trimmed = _trim(code)
    assert trimmed.rstrip().endswith("return df_summary")
    assert "example usage" not in trimmed
    assert "prose" not in trimmed


def test_early_return_does_not_truncate_the_function():
    """Anchoring on the last return, not the first."""
    code = (
        "def generate_df(a, b):\n"
        "    if not a:\n"
        "        return df_empty\n"
        "    df_final = build()\n"
        "    return df_final\n"
    )
    trimmed = _trim(code)
    assert "df_final = build()" in trimmed
    assert trimmed.rstrip().endswith("return df_final")


def test_code_without_a_df_return_is_untouched():
    code = "def generate_df(a, b):\n    pass\n"
    assert _trim(code) == code


def test_both_codegen_paths_use_the_shared_trim():
    """Two methods post-process generated code. Both must go through the shared
    helper — the copy that doesn't would quietly reintroduce the rewrite."""
    import inspect

    for method in (Coder.generate_code, Coder.data_model_to_code):
        src = inspect.getsource(method)
        assert "trim_after_final_df_return" in src
        assert "'return df'" not in src, (
            f"{method.__name__} still rewrites the returned name"
        )
