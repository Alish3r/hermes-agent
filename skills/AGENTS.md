# `skills/` instructions (the root `AGENTS.override.md` still applies)

Also covers `optional-skills/`.

## Two skill surfaces

`skills/` holds built-in skills that ship and load by default, organized by category directory (`skills/github/`, `skills/mlops/`). `optional-skills/` holds heavier or niche skills that ship with the repo but are not active by default; users install them with `hermes skills install official/<category>/<skill>` through the `OptionalSkillSource` adapter in `tools/skills_hub.py`. Heavy-dependency or niche skills belong in `optional-skills/`; check which directory a new skill targets before writing it.

## SKILL.md frontmatter

Standard fields are `name`, `description`, `version`, `author`, `license`, `platforms` (an OS-gating list such as `[macos]` or `[linux, macos]`), and `metadata.hermes.tags`, `.category`, `.related_skills` and `.config` (config.yaml settings the skill needs, stored under `skills.config.<key>`, prompted during setup and injected at load time). Top-level `tags:` and `category:` are also accepted and mirrored from `metadata.hermes.*` by the loader.

## Authoring standards

The authoring standards are HARDLINE and are not repeated here: load the `hermes-agent-skill-authoring` skill and follow its checklist before writing or reviewing a skill.

## Further reading

- `website/docs/developer-guide/creating-skills.md`
