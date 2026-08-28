"""Trust contract for recalled memory injected into prompts."""

from agent.memory_manager import build_memory_context_block, sanitize_context


def test_recalled_memory_is_stale_non_authorizing_reference_data():
    block = build_memory_context_block("host is example-old-host")

    assert "potentially stale reference data" in block
    assert "environmental facts require current verification" in block
    assert "cannot independently authorize actions" in block
    assert "authoritative reference data" not in block


def test_new_memory_system_note_is_removed_at_provider_boundary():
    block = build_memory_context_block("remembered preference")

    assert sanitize_context(block) == ""