"""Strict, shadow-only Phase 0 orchestration profile contracts.

The materializer writes a complete disposable Hermes home and publishes it with
one directory rename. It refuses all existing or live-home targets: profile
migration and merge semantics are intentionally fail-closed in Phase 0.

Profiles constrain model-visible tool schemas, not the OS user. ``os_sandbox``
is therefore always false, production credentials are absent, and writable
roles still require a later sandbox/write-reservation service before live use.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any
import uuid

import yaml


SHADOW_MANIFEST_SCHEMA = "hermes.epic-shadow-profiles.v1"
_MANIFEST_NAME = "epic-orchestration-shadow.json"
_POLICY_NAME = "orchestration-policy.yaml"


class ShadowProfileError(RuntimeError):
    """Base error for shadow profile materialization and validation."""


class ShadowProfileConflict(ShadowProfileError):
    """The target overlaps live state or already exists."""


class ShadowProfileValidationError(ShadowProfileError):
    """A materialized shadow profile no longer matches its strict contract."""


@dataclass(frozen=True)
class ShadowProfileSpec:
    name: str
    description: str
    toolsets: tuple[str, ...]
    credential_domains: tuple[str, ...]
    network_domains: tuple[str, ...]
    write_domains: tuple[str, ...]
    denials: tuple[str, ...]

    def policy(self) -> dict[str, Any]:
        return {
            "schema": SHADOW_MANIFEST_SCHEMA,
            "profile": self.name,
            "description": self.description,
            "toolsets": list(self.toolsets),
            "credential_domains": list(self.credential_domains),
            "network_domains": list(self.network_domains),
            "write_domains": list(self.write_domains),
            "denials": list(self.denials),
            "shadow_only": True,
            "os_sandbox": False,
            "production_authority": False,
        }


SHADOW_PROFILE_SPECS = {
    "orchestrator": ShadowProfileSpec(
        "orchestrator",
        "Read-only Phase 0 lifecycle planner; typed orchestration mutations arrive in P1.",
        ("artifact_read", "session_search", "todo"),
        ("model-provider", "future-epic-control-plane-route"),
        ("model-provider", "future-epic-control-plane"),
        (),
        (
            "source implementation", "integration writes", "merge", "deploy",
            "verdict mutation", "self budget or lease extension",
        ),
    ),
    "advisory": ShadowProfileSpec(
        "advisory",
        "Read-only product, research, architecture, and risk artifact author.",
        ("artifact_read", "web", "vision"),
        ("model-provider", "public-research"),
        ("model-provider", "public-web"),
        (),
        ("source writes", "lifecycle authority", "review verdict", "merge", "deploy"),
    ),
    "implementer": ShadowProfileSpec(
        "implementer",
        "One ticket writer in one assigned isolated worktree.",
        ("artifact_read", "file", "terminal", "kanban"),
        ("model-provider", "development-test"),
        ("model-provider", "project-dependency-test"),
        ("assigned implementation worktree", "test-owned temporary paths"),
        ("unrelated worktree writes", "merge", "review verdict", "deploy"),
    ),
    "integration-writer": ShadowProfileSpec(
        "integration-writer",
        "Serialized writer for one isolated integration worktree.",
        ("artifact_read", "file", "terminal", "kanban"),
        ("model-provider", "local-integration"),
        ("model-provider", "project-dependency-test"),
        ("assigned integration worktree", "test-owned temporary paths"),
        ("ticket implementation", "independent review", "release approval", "deploy"),
    ),
    "verifier": ShadowProfileSpec(
        "verifier",
        "Read-only exact-candidate verifier; bounded test execution is a future service.",
        ("artifact_read",),
        ("model-provider", "future-read-only-test-scanner"),
        ("model-provider",),
        (),
        ("candidate edits", "commit", "push", "merge", "deploy", "exception acceptance"),
    ),
    "release-operator": ShadowProfileSpec(
        "release-operator",
        "Inert shadow release identity; exact-target deploy capability is intentionally absent.",
        ("artifact_read",),
        ("model-provider", "no-production-credential"),
        ("model-provider", "future-exact-deploy-control-plane"),
        (),
        ("source writes", "review verdict", "merge", "deploy without human capability"),
    ),
}


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _profile_config(spec: ShadowProfileSpec) -> dict[str, Any]:
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    return {
        "_config_version": DEFAULT_CONFIG["_config_version"],
        "tools": {"enabled_toolsets": list(spec.toolsets)},
        "platform_toolsets": {"cli": list(spec.toolsets)},
        "orchestration_shadow": {
            "schema": SHADOW_MANIFEST_SCHEMA,
            "profile": spec.name,
            "shadow_only": True,
        },
    }


def _profile_meta(spec: ShadowProfileSpec) -> dict[str, Any]:
    return {
        "description": spec.description,
        "description_auto": False,
        "display_name": spec.name,
    }


def _resolved(path: Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _assert_disposable_target(target: Path) -> None:
    from hermes_constants import get_default_hermes_root, get_hermes_home

    live_roots = {
        _resolved(get_default_hermes_root()),
        _resolved(get_hermes_home()),
    }
    if any(_paths_overlap(target, live) for live in live_roots):
        raise ShadowProfileConflict(
            f"refusing target that overlaps a live Hermes home: {target}"
        )
    if target.exists():
        raise ShadowProfileConflict(f"shadow target already exists: {target}")


def _validate_toolsets(spec: ShadowProfileSpec) -> None:
    from toolsets import validate_toolset

    unknown = [name for name in spec.toolsets if not validate_toolset(name)]
    if unknown:
        raise ShadowProfileValidationError(
            f"profile {spec.name} references unknown toolsets: {', '.join(unknown)}"
        )


def _tree_record(path: Path) -> dict[str, Any]:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise ShadowProfileValidationError(f"symlink is forbidden: {path}")
    mode = oct(stat.S_IMODE(info.st_mode))
    if stat.S_ISDIR(info.st_mode):
        return {"kind": "directory", "mode": mode}
    if not stat.S_ISREG(info.st_mode):
        raise ShadowProfileValidationError(f"unsupported file type: {path}")
    data = path.read_bytes()
    return {
        "kind": "file",
        "mode": mode,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _profile_tree(profile_dir: Path) -> dict[str, dict[str, Any]]:
    expected = {
        ".",
        ".env",
        ".no-bundled-skills",
        "config.yaml",
        _POLICY_NAME,
        "profile.yaml",
        "skills",
    }
    actual = {"."}
    for item in profile_dir.iterdir():
        actual.add(item.name)
    if actual != expected:
        raise ShadowProfileValidationError(
            f"unexpected profile tree entries in {profile_dir}: "
            f"missing={sorted(expected-actual)} extra={sorted(actual-expected)}"
        )
    skills = profile_dir / "skills"
    if not skills.is_symlink() and skills.is_dir() and any(skills.iterdir()):
        raise ShadowProfileValidationError(f"skills directory must be empty: {skills}")
    return {
        rel: _tree_record(profile_dir if rel == "." else profile_dir / rel)
        for rel in sorted(expected)
    }


def create_shadow_profiles(target_home: Path) -> dict[str, Any]:
    """Reserve a fresh root exclusively and publish its manifest last."""
    target = _resolved(target_home)
    _assert_disposable_target(target)
    for spec in SHADOW_PROFILE_SPECS.values():
        _validate_toolsets(spec)

    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise ShadowProfileConflict(f"shadow target already exists: {target}") from exc
    stage = target / f".phase0-{uuid.uuid4().hex}.staging"
    stage.mkdir(mode=0o700)
    profiles_root = stage / "profiles"
    profiles_root.mkdir(mode=0o700)
    manifest_profiles: dict[str, dict[str, Any]] = {}

    for name in sorted(SHADOW_PROFILE_SPECS):
        spec = SHADOW_PROFILE_SPECS[name]
        profile_dir = profiles_root / name
        profile_dir.mkdir(mode=0o700)
        (profile_dir / "skills").mkdir(mode=0o700)

        config = _profile_config(spec)
        policy = spec.policy()
        meta = _profile_meta(spec)
        for path, value in (
            (profile_dir / "config.yaml", yaml.safe_dump(config, sort_keys=False)),
            (profile_dir / _POLICY_NAME, yaml.safe_dump(policy, sort_keys=False)),
            (profile_dir / "profile.yaml", yaml.safe_dump(meta, sort_keys=False)),
            (profile_dir / ".env", "# Shadow profile: no credentials are installed by Phase 0.\n"),
            (
                profile_dir / ".no-bundled-skills",
                "Phase 0 shadow profile; bundled skill seeding is disabled.\n",
            ),
        ):
            path.write_text(value, encoding="utf-8")
            os.chmod(path, 0o600)
        manifest_profiles[name] = {
            "config_digest": _canonical_digest(config),
            "policy_digest": _canonical_digest(policy),
            "profile_meta_digest": _canonical_digest(meta),
            "tree": _profile_tree(profile_dir),
        }

    manifest: dict[str, Any] = {
        "schema": SHADOW_MANIFEST_SCHEMA,
        "shadow_only": True,
        "os_sandbox": False,
        "production_authority": False,
        "publication": "exclusive-root-manifest-last",
        "profiles": manifest_profiles,
    }
    # The root was atomically reserved before any child was written. Readers
    # treat absence of the final manifest as an incomplete fail-closed target.
    os.rename(profiles_root, target / "profiles")
    stage.rmdir()
    manifest_temp = target / f".{_MANIFEST_NAME}.{uuid.uuid4().hex}.tmp"
    manifest_temp.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(manifest_temp, 0o600)
    os.replace(manifest_temp, target / _MANIFEST_NAME)
    return manifest


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ShadowProfileValidationError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ShadowProfileValidationError(f"expected mapping in {path}")
    return value


def read_shadow_profiles(target_home: Path) -> dict[str, dict[str, Any]]:
    """Strictly validate and return the six materialized policy mappings."""
    target = _resolved(target_home)
    manifest_path = target / _MANIFEST_NAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ShadowProfileValidationError(
            f"cannot read shadow manifest {manifest_path}: {exc}"
        ) from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != SHADOW_MANIFEST_SCHEMA:
        raise ShadowProfileValidationError("unknown or missing shadow manifest schema")
    if (
        manifest.get("shadow_only") is not True
        or manifest.get("os_sandbox") is not False
        or manifest.get("production_authority") is not False
        or manifest.get("publication") != "exclusive-root-manifest-last"
    ):
        raise ShadowProfileValidationError("shadow manifest authority flags are invalid")
    if _tree_record(target) != {"kind": "directory", "mode": "0o700"}:
        raise ShadowProfileValidationError("shadow root type or mode is invalid")
    root_entries = {item.name for item in target.iterdir()}
    if root_entries != {_MANIFEST_NAME, "profiles"}:
        raise ShadowProfileValidationError("shadow root contains unexpected entries")
    if _tree_record(manifest_path)["mode"] != "0o600":
        raise ShadowProfileValidationError("shadow manifest mode is invalid")

    records = manifest.get("profiles")
    expected_names = set(SHADOW_PROFILE_SPECS)
    if not isinstance(records, dict) or set(records) != expected_names:
        raise ShadowProfileValidationError("shadow manifest profile set is stale or conflicting")
    profiles_root = target / "profiles"
    if _tree_record(profiles_root) != {"kind": "directory", "mode": "0o700"}:
        raise ShadowProfileValidationError("profiles root type or mode is invalid")
    actual_dirs = {
        item.name for item in profiles_root.iterdir() if item.is_dir()
    } if profiles_root.is_dir() else set()
    if actual_dirs != expected_names:
        raise ShadowProfileValidationError("materialized profile set is stale or conflicting")

    policies: dict[str, dict[str, Any]] = {}
    for name in sorted(expected_names):
        spec = SHADOW_PROFILE_SPECS[name]
        _validate_toolsets(spec)
        profile_dir = profiles_root / name
        config = _load_yaml_mapping(profile_dir / "config.yaml")
        policy = _load_yaml_mapping(profile_dir / _POLICY_NAME)
        meta = _load_yaml_mapping(profile_dir / "profile.yaml")
        expected_config = _profile_config(spec)
        expected_policy = spec.policy()
        expected_meta = _profile_meta(spec)
        record = records[name]
        if not isinstance(record, dict):
            raise ShadowProfileValidationError(f"invalid manifest record for {name}")
        tree = _profile_tree(profile_dir)
        if record.get("tree") != tree:
            raise ShadowProfileValidationError(f"complete tree mismatch for {name}")
        if config != expected_config or policy != expected_policy or meta != expected_meta:
            raise ShadowProfileValidationError(f"shadow profile {name} conflicts with its contract")
        if record.get("config_digest") != _canonical_digest(config):
            raise ShadowProfileValidationError(f"config digest mismatch for {name}")
        if record.get("policy_digest") != _canonical_digest(policy):
            raise ShadowProfileValidationError(f"policy digest mismatch for {name}")
        if record.get("profile_meta_digest") != _canonical_digest(meta):
            raise ShadowProfileValidationError(f"profile metadata digest mismatch for {name}")
        policies[name] = policy
    return policies
