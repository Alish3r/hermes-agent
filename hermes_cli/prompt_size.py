"""Prompt-size diagnostic: ``hermes prompt-size``.

Reports a byte/char breakdown of the system prompt the agent would build for
a fresh session — system prompt total, the ``<available_skills>`` index,
memory + user profile, and tool-schema JSON. Lets users see where their fixed
prompt budget goes (issue #34667) without parsing a saved session JSON by hand.

The diagnostic builds a real inspection agent (so the numbers match what
actually ships on the wire) but never makes a network call: it passes dummy
credentials so ``AIAgent.__init__`` takes the direct-construction path, then
calls ``build_system_prompt_parts`` / inspects ``agent.tools`` offline.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# The skills index is wrapped in this tag pair inside the stable tier.
_SKILLS_BLOCK_RE = re.compile(r"<available_skills>.*?</available_skills>", re.DOTALL)

# A rendered skill entry inside <available_skills> is ``    - name: desc`` (or
# ``    - name`` when the skill has no description). Category headers use two
# leading spaces, so the four-space + ``- `` prefix isolates skill lines.
_SKILL_LINE_PREFIX = "    - "

# Posture-demoted categories render all visible skill names on one shared line.
_NAMES_ONLY_LINE_RE = re.compile(r"^  .+ \[names only\]: (?P<names>.+)$")

# Cap the human-readable "Skills by size" table; ``--json`` always has them all.
_SKILLS_TABLE_LIMIT = 20

# Marker ``prompt_builder._truncate_content`` leaves inside a context file it
# cut; the numbers in it are the builder's own accounting for that file.
_TRUNCATION_MARKER_RE = re.compile(
    r"\[\.\.\.truncated (?P<name>.+?): kept \d+\+\d+ of (?P<total>\d+) chars\."
)


def _bytes(s: str) -> int:
    return len(s.encode("utf-8"))


def _tool_name(tool: Any) -> str:
    """Return the callable name of a tool schema (OpenAI ``function`` shape)."""
    if not isinstance(tool, dict):
        return ""
    fn = tool.get("function")
    if isinstance(fn, dict) and fn.get("name"):
        return str(fn["name"])
    return str(tool.get("name", ""))


def _build_inspection_agent(platform: str) -> Any:
    """Construct an offline AIAgent for prompt inspection.

    Dummy ``api_key`` + ``base_url`` force the direct-construction path in
    ``run_agent.py`` (no provider auto-detection, no network). Toolsets and
    platform come from the caller so the breakdown matches a real session.
    """
    from run_agent import AIAgent
    from hermes_cli.config import load_config
    from hermes_cli.tools_config import _get_platform_tools

    cfg = load_config()
    model_cfg = cfg.get("model", {}) if isinstance(cfg.get("model"), dict) else {}
    model = model_cfg.get("default") or model_cfg.get("model") or ""

    # Resolve platform-specific toolsets the same way the gateway does.
    enabled_toolsets = sorted(_get_platform_tools(cfg, platform))
    agent_cfg = cfg.get("agent") or {}
    from agent.skill_utils import parse_config_string_list

    disabled_toolsets = parse_config_string_list(agent_cfg.get("disabled_toolsets")) or None

    return AIAgent(
        model=model,
        api_key="inspect-only",
        base_url="https://openrouter.ai/api/v1",
        quiet_mode=True,
        save_trajectories=False,
        platform=platform,
        enabled_toolsets=enabled_toolsets,
        disabled_toolsets=disabled_toolsets,
    )


def _skill_md_paths_by_name() -> Dict[str, Path]:
    """Map each installed skill's name to its ``SKILL.md`` path on disk.

    Keyed by both the frontmatter ``name`` (what the index renders) and the
    skill directory name, so either resolves. Local skills win over external
    dirs (``get_all_skills_dirs`` yields local first), matching the index's own
    precedence. Used to attribute the real on-disk read cost per skill.
    """
    from agent.skill_utils import (
        get_all_skills_dirs,
        iter_skill_index_files,
        parse_frontmatter,
    )

    mapping: Dict[str, Path] = {}
    for skills_dir in get_all_skills_dirs():
        if not skills_dir.exists():
            continue
        for skill_file in iter_skill_index_files(skills_dir, "SKILL.md"):
            frontmatter_name = skill_file.parent.name
            try:
                frontmatter, _ = parse_frontmatter(
                    skill_file.read_text(encoding="utf-8")
                )
                frontmatter_name = str(frontmatter.get("name") or frontmatter_name)
            except Exception:
                pass
            # setdefault keeps the first (local) occurrence on name collisions.
            mapping.setdefault(frontmatter_name, skill_file)
            mapping.setdefault(skill_file.parent.name, skill_file)
    return mapping


def _compute_skills_breakdown(skills_block: str) -> List[Dict[str, Any]]:
    """Per-skill byte breakdown parsed from the rendered ``<available_skills>``.

    Two honest, distinct numbers per skill:

    * ``index_line_bytes`` — the skill's attributed bytes in the always-on
      index (the fixed per-call cost of *listing* the skill). For a compact
      ``[names only]`` line, each name keeps its own bytes and receives an
      even share of the category prefix and separators. The attributed bytes
      therefore sum exactly to the shared rendered line.
    * ``skill_md_bytes`` — the on-disk size of the skill's ``SKILL.md`` (the
      real token cost paid only when the model loads it via ``skill_view``).
      ``None`` when the name can't be mapped to a file (e.g. a plugin skill
      whose source lives outside the scanned skill dirs).

    Sorted largest-first by ``skill_md_bytes`` (the read cost that dominates
    pruning decisions), tie-broken by name.
    """
    name_to_path = _skill_md_paths_by_name()
    entries: List[Dict[str, Any]] = []

    def append_entry(
        name: str,
        *,
        attributed_bytes: int,
        total_bytes: int,
        shared_bytes: int,
        skill_count: int,
    ) -> None:
        path = name_to_path.get(name)
        md_bytes: Optional[int] = None
        if path is not None:
            try:
                md_bytes = path.stat().st_size
            except OSError:
                md_bytes = None
        entries.append({
            "name": name,
            "index_line_bytes": attributed_bytes,
            "index_line_total_bytes": total_bytes,
            "index_line_shared_bytes": shared_bytes,
            "index_line_skill_count": skill_count,
            "skill_md_bytes": md_bytes,
            "path": str(path) if path is not None else "",
        })

    for line in skills_block.splitlines():
        compact_match = _NAMES_ONLY_LINE_RE.match(line)
        if compact_match is not None:
            names = [
                name.strip()
                for name in compact_match.group("names").split(",")
                if name.strip()
            ]
            if not names:
                continue
            total_bytes = _bytes(line)
            name_bytes = [_bytes(name) for name in names]
            shared_total = total_bytes - sum(name_bytes)
            shared_base, shared_remainder = divmod(shared_total, len(names))
            for index, name in enumerate(names):
                shared_bytes = shared_base + (1 if index < shared_remainder else 0)
                append_entry(
                    name,
                    attributed_bytes=name_bytes[index] + shared_bytes,
                    total_bytes=total_bytes,
                    shared_bytes=shared_bytes,
                    skill_count=len(names),
                )
            continue

        if not line.startswith(_SKILL_LINE_PREFIX):
            continue
        rest = line[len(_SKILL_LINE_PREFIX):]
        # ``name: desc`` — the first ``": "`` separates name from description.
        # Namespaced names (``codex:rescue``) have no space after their colon,
        # so partitioning on ``": "`` keeps the full name intact.
        name = rest.partition(": ")[0].strip()
        if not name:
            continue
        line_bytes = _bytes(line)
        append_entry(
            name,
            attributed_bytes=line_bytes,
            total_bytes=line_bytes,
            shared_bytes=0,
            skill_count=1,
        )
    entries.sort(key=lambda e: (-(e["skill_md_bytes"] or 0), e["name"]))
    return entries


def _compute_toolsets_breakdown(tools: List[Any]) -> List[Dict[str, Any]]:
    """Per-toolset schema-byte breakdown of the resolved tool list.

    Each tool is attributed to its single canonical toolset from the registry,
    so ``json_bytes`` sums are fully attributable: the grand total equals the
    sum of the individual tool serializations (which is the array total from
    ``tools['json_bytes']`` minus JSON framing of ``2 * count`` bytes). Sorted
    largest-first by ``json_bytes``, tie-broken by toolset name.
    """
    from tools.registry import registry

    tool_to_toolset = registry.get_tool_to_toolset_map()
    groups: Dict[str, Dict[str, Any]] = {}
    for tool in tools:
        name = _tool_name(tool)
        toolset = tool_to_toolset.get(name) or "(unknown)"
        group = groups.setdefault(
            toolset, {"toolset": toolset, "tool_count": 0, "json_bytes": 0}
        )
        group["tool_count"] += 1
        group["json_bytes"] += _bytes(json.dumps(tool, ensure_ascii=False))
    out = list(groups.values())
    out.sort(key=lambda g: (-g["json_bytes"], g["toolset"]))
    return out


def context_file_truncation(chars: int, cap_chars: int) -> Dict[str, Any]:
    """Pure mirror of the ``prompt_builder._truncate_content`` arithmetic.

    For a context file of ``chars`` chars under an effective cap of
    ``cap_chars``, report whether the builder cuts it and how many chars
    survive in the head and tail slices versus how many are dropped from the
    middle. The ratios are imported from the builder so the two never drift.
    """
    from agent.prompt_builder import (
        CONTEXT_TRUNCATE_HEAD_RATIO,
        CONTEXT_TRUNCATE_TAIL_RATIO,
    )

    if chars <= cap_chars:
        return {"truncated": False, "kept_head": chars, "kept_tail": 0, "dropped": 0}
    kept_head = int(cap_chars * CONTEXT_TRUNCATE_HEAD_RATIO)
    kept_tail = int(cap_chars * CONTEXT_TRUNCATE_TAIL_RATIO)
    return {
        "truncated": True,
        "kept_head": kept_head,
        "kept_tail": kept_tail,
        "dropped": chars - kept_head - kept_tail,
    }


def _model_context_length(agent: Any) -> Optional[int]:
    """The context window the prompt builder used for this agent, or None.

    Mirrors ``build_system_prompt_parts``: the window is read from the agent's
    context compressor, so the cap reported here is the cap the builder
    actually applied to the context files.
    """
    compressor = getattr(agent, "context_compressor", None)
    length = getattr(compressor, "context_length", None)
    if isinstance(length, int) and not isinstance(length, bool) and length > 0:
        return length
    return None


def _find_context_truncations(context: str, cap_chars: int) -> List[Dict[str, Any]]:
    """Context files the builder cut, read from the markers it left in the tier.

    The context tier is measured *after* truncation, so its size alone cannot
    reveal a cut; the marker carries the file's real size, and the head/tail
    split is re-derived from it with :func:`context_file_truncation`.
    """
    found: List[Dict[str, Any]] = []
    for match in _TRUNCATION_MARKER_RE.finditer(context):
        total = int(match.group("total"))
        entry: Dict[str, Any] = {"file": match.group("name"), "chars": total}
        entry.update(context_file_truncation(total, cap_chars))
        found.append(entry)
    return found


def compute_prompt_breakdown(platform: str = "cli") -> Dict[str, Any]:
    """Return a dict of prompt-size measurements for a fresh session.

    Keys: ``system_prompt`` (chars/bytes), ``skills_index``, ``memory``,
    ``user_profile``, ``tools`` (count + json bytes), ``sections`` (a list of
    (label, chars, bytes) for the three prompt tiers), ``skills_breakdown``
    (per-skill index-line + on-disk SKILL.md bytes, largest-first), and
    ``toolsets_breakdown`` (per-toolset tool count + schema json bytes,
    largest-first). The last two answer "what should I disable to cut tokens?".
    ``context_files`` carries the effective context-file cap for this model and
    any context file the builder truncated on the way in.
    """
    from agent.system_prompt import build_system_prompt, build_system_prompt_parts

    agent = _build_inspection_agent(platform)

    parts = build_system_prompt_parts(agent)
    full = build_system_prompt(agent)

    stable = parts.get("stable", "")
    context = parts.get("context", "")
    volatile = parts.get("volatile", "")

    # Skills index — the <available_skills> block (the largest single block
    # when many skills are installed). Lives in the volatile tier (moved from
    # stable so skill edits don't invalidate the cached identity prefix).
    skills_match = _SKILLS_BLOCK_RE.search(volatile) or _SKILLS_BLOCK_RE.search(stable)
    skills_index = skills_match.group(0) if skills_match else ""

    # Memory + user profile live in the volatile tier. We re-derive their
    # blocks directly from the memory store so the numbers are attributable
    # even though they're joined into ``volatile``.
    memory_block = ""
    user_block = ""
    store = getattr(agent, "_memory_store", None)
    if store is not None:
        try:
            if getattr(agent, "_memory_enabled", True):
                memory_block = store.format_for_system_prompt("memory") or ""
            if getattr(agent, "_user_profile_enabled", True):
                user_block = store.format_for_system_prompt("user") or ""
        except Exception:
            pass

    # Tool-schema JSON — the other half of the fixed per-call payload.
    tools = getattr(agent, "tools", None) or []
    tools_json = json.dumps(tools, ensure_ascii=False)

    sections: List[Tuple[str, int, int]] = [
        ("stable (identity/guidance/skills)", len(stable), _bytes(stable)),
        ("context (AGENTS.md/cwd files)", len(context), _bytes(context)),
        ("volatile (memory/profile/timestamp)", len(volatile), _bytes(volatile)),
    ]

    # Effective context-file cap: what prompt_builder applied to AGENTS.md and
    # friends for this model, plus any file it had to cut to fit.
    from agent.prompt_builder import CONTEXT_FILE_MAX_CHARS, _get_context_file_max_chars

    context_length = _model_context_length(agent)
    cap_chars = _get_context_file_max_chars(context_length)
    context_files = {
        "cap_chars": cap_chars,
        "floor_chars": CONTEXT_FILE_MAX_CHARS,
        "context_length": context_length,
        "truncated": _find_context_truncations(context, cap_chars),
    }

    return {
        "platform": platform,
        "model": getattr(agent, "model", "") or "",
        "system_prompt": {"chars": len(full), "bytes": _bytes(full)},
        "skills_index": {"chars": len(skills_index), "bytes": _bytes(skills_index)},
        "memory": {"chars": len(memory_block), "bytes": _bytes(memory_block)},
        "user_profile": {"chars": len(user_block), "bytes": _bytes(user_block)},
        "tools": {"count": len(tools), "json_bytes": _bytes(tools_json)},
        "sections": sections,
        "skills_breakdown": _compute_skills_breakdown(skills_index),
        "toolsets_breakdown": _compute_toolsets_breakdown(tools),
        "context_files": context_files,
    }


def _fmt_kb(n: int) -> str:
    return f"{n / 1024:.1f} KB"


def _render_context_cap(info: Optional[Dict[str, Any]]) -> List[str]:
    """Lines shown under the context tier: the effective cap and any cut file."""
    if not info:
        return []
    cap = info["cap_chars"]
    length = info.get("context_length")
    if length:
        origin = f"model context length {length:,} tokens"
    else:
        origin = (
            f"model context length unknown; {info.get('floor_chars', cap):,}-char "
            "floor unless context_file_max_chars is set"
        )
    lines = [f"      context-file cap                  : {cap:>8,} chars  ({origin})"]
    for cut in info.get("truncated") or []:
        lines.append(
            f"      TRUNCATED {cut['file']}: {cut['chars']:,} chars > cap; "
            f"{cut['dropped']:,} chars dropped "
            f"(kept {cut['kept_head']:,} head + {cut['kept_tail']:,} tail)"
        )
    return lines


def render_breakdown(data: Dict[str, Any]) -> str:
    """Render the breakdown as plain text suitable for a terminal."""
    lines: List[str] = []
    sp = data["system_prompt"]
    lines.append(f"Prompt-size breakdown (platform={data['platform']}, model={data['model'] or 'unset'})")
    lines.append("")
    lines.append(f"  System prompt total : {sp['bytes']:>8,} B  ({_fmt_kb(sp['bytes'])}, {sp['chars']:,} chars)")
    lines.append("")
    lines.append("  Major blocks:")
    si = data["skills_index"]
    mem = data["memory"]
    up = data["user_profile"]
    lines.append(f"    skills index       : {si['bytes']:>8,} B  ({_fmt_kb(si['bytes'])})")
    lines.append(f"    memory             : {mem['bytes']:>8,} B  ({_fmt_kb(mem['bytes'])})")
    lines.append(f"    user profile       : {up['bytes']:>8,} B  ({_fmt_kb(up['bytes'])})")
    lines.append("")
    lines.append("  Prompt tiers:")
    for label, chars, byts in data["sections"]:
        lines.append(f"    {label:<36}: {byts:>8,} B  ({_fmt_kb(byts)})")
        if label.startswith("context"):
            lines.extend(_render_context_cap(data.get("context_files")))
    lines.append("")
    tools = data["tools"]
    lines.append(f"  Tool schemas         : {tools['json_bytes']:>8,} B  ({_fmt_kb(tools['json_bytes'])}, {tools['count']} tools)")

    # Per-toolset schema cost — which toolset's tools cost the most to ship.
    toolsets = data.get("toolsets_breakdown") or []
    if toolsets:
        lines.append("")
        lines.append("  Toolsets by size (tool-schema JSON, largest first):")
        lines.append(f"    {'toolset':<22} {'tools':>5}  {'schema':>10}")
        for ts in toolsets:
            lines.append(
                f"    {ts['toolset']:<22} {ts['tool_count']:>5}  "
                f"{ts['json_bytes']:>8,} B  ({_fmt_kb(ts['json_bytes'])})"
            )

    # Per-skill cost — index line (always shipped) vs SKILL.md (read on load).
    skills = data.get("skills_breakdown") or []
    if skills:
        lines.append("")
        lines.append(
            "  Skills by size (SKILL.md on-disk = read cost; index cost = "
            "attributed always-on bytes, largest first):"
        )
        lines.append(f"    {'skill':<28} {'SKILL.md':>10}  {'index cost':>10}")
        shown = skills[:_SKILLS_TABLE_LIMIT]
        for sk in shown:
            md = sk["skill_md_bytes"]
            md_str = f"{md:>8,} B" if md is not None else f"{'n/a':>10}"
            name = sk["name"]
            if len(name) > 28:
                name = name[:27] + "…"
            lines.append(
                f"    {name:<28} {md_str}  {sk['index_line_bytes']:>8,} B"
            )
        remaining = len(skills) - len(shown)
        if remaining > 0:
            lines.append(f"    … and {remaining} more (use --json for the full list)")
    return "\n".join(lines)


def cmd_prompt_size(args: Any) -> None:
    """Entry point for ``hermes prompt-size``."""
    platform = getattr(args, "platform", "cli") or "cli"
    as_json = getattr(args, "json", False)
    try:
        data = compute_prompt_breakdown(platform)
    except Exception as e:
        print(f"Could not compute prompt-size breakdown: {e}")
        return
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(render_breakdown(data))
