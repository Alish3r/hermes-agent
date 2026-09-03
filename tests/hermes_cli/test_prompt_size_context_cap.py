"""Context-file cap lines in ``hermes prompt-size``: pure arithmetic only.

The helper mirrors ``prompt_builder._truncate_content``. These tests feed it
synthetic numbers and check the relationships that must hold between them;
no source file is read and no literal constant is pinned.
"""

from types import SimpleNamespace

from agent.prompt_builder import (
    CONTEXT_TRUNCATE_HEAD_RATIO,
    CONTEXT_TRUNCATE_TAIL_RATIO,
    _truncate_content,
)
from hermes_cli.prompt_size import (
    _find_context_truncations,
    _model_context_length,
    context_file_truncation,
    render_breakdown,
)


def test_under_cap_keeps_everything():
    result = context_file_truncation(chars=12_345, cap_chars=20_000)
    assert result["truncated"] is False
    assert result["dropped"] == 0
    assert result["kept_head"] + result["kept_tail"] == 12_345


def test_exactly_at_cap_is_not_truncated():
    result = context_file_truncation(chars=20_000, cap_chars=20_000)
    assert result["truncated"] is False
    assert result["dropped"] == 0


def test_over_cap_accounts_for_every_char():
    chars, cap = 30_000, 20_000
    result = context_file_truncation(chars=chars, cap_chars=cap)
    assert result["truncated"] is True
    assert result["kept_head"] == int(cap * CONTEXT_TRUNCATE_HEAD_RATIO)
    assert result["kept_tail"] == int(cap * CONTEXT_TRUNCATE_TAIL_RATIO)
    assert result["kept_head"] + result["kept_tail"] + result["dropped"] == chars
    assert 0 < result["dropped"] < chars
    assert result["kept_head"] + result["kept_tail"] <= cap


def test_markers_from_the_real_truncator_are_found():
    cap = 1_000
    content = "x" * 5_000
    cut = _truncate_content(content, "AGENTS.md", max_chars=cap)
    found = _find_context_truncations(cut, cap_chars=cap)
    assert [entry["file"] for entry in found] == ["AGENTS.md"]
    entry = found[0]
    assert entry["chars"] == len(content)
    assert entry["truncated"] is True
    assert entry["dropped"] == len(content) - entry["kept_head"] - entry["kept_tail"]
    # The head/tail the helper re-derives are the slices the truncator kept.
    assert cut.startswith("x" * entry["kept_head"])
    assert cut.endswith("x" * entry["kept_tail"])


def test_no_marker_means_nothing_was_cut():
    assert _find_context_truncations("## AGENTS.md\n\nshort file", cap_chars=20_000) == []


def test_model_context_length_reads_the_compressor_window():
    known = SimpleNamespace(context_compressor=SimpleNamespace(context_length=128_000))
    assert _model_context_length(known) == 128_000
    assert _model_context_length(SimpleNamespace(context_compressor=SimpleNamespace(context_length=0))) is None
    assert _model_context_length(SimpleNamespace(context_compressor=None)) is None
    assert _model_context_length(SimpleNamespace()) is None


def _breakdown(context_files):
    return {
        "platform": "cli",
        "model": "synthetic",
        "system_prompt": {"chars": 10, "bytes": 10},
        "skills_index": {"chars": 0, "bytes": 0},
        "memory": {"chars": 0, "bytes": 0},
        "user_profile": {"chars": 0, "bytes": 0},
        "tools": {"count": 0, "json_bytes": 2},
        "sections": [
            ("stable (identity/guidance/skills)", 5, 5),
            ("context (AGENTS.md/cwd files)", 5, 5),
            ("volatile (memory/profile/timestamp)", 0, 0),
        ],
        "skills_breakdown": [],
        "toolsets_breakdown": [],
        "context_files": context_files,
    }


def test_render_shows_cap_and_truncated_line_only_when_cut():
    quiet = render_breakdown(_breakdown({
        "cap_chars": 20_000, "floor_chars": 20_000,
        "context_length": None, "truncated": [],
    }))
    assert "context-file cap" in quiet
    assert "unknown" in quiet
    assert "TRUNCATED" not in quiet

    chars, cap = 90_000, 61_440
    cut_entry = {"file": "AGENTS.md", "chars": chars, **context_file_truncation(chars, cap)}
    cut = render_breakdown(_breakdown({
        "cap_chars": cap, "floor_chars": 20_000,
        "context_length": 256_000, "truncated": [cut_entry],
    }))
    assert f"{cap:,} chars" in cut
    assert "256,000 tokens" in cut
    assert "TRUNCATED AGENTS.md" in cut
    assert f"{cut_entry['dropped']:,} chars dropped" in cut


def test_render_without_context_files_key_still_works():
    data = _breakdown(None)
    del data["context_files"]
    out = render_breakdown(data)
    assert "context (AGENTS.md/cwd files)" in out
    assert "context-file cap" not in out
