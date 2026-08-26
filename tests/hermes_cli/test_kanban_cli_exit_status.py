"""Regression coverage for Kanban CLI process exit status propagation."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from hermes_cli import kanban_db as kb


ROOT = Path(__file__).parents[2]


def _run_hermes(home: Path, *args: str, marker: bool = False) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["HERMES_KANBAN_HOME"] = str(home)
    for name in (
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
    ):
        env.pop(name, None)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    if marker:
        env["HERMES_DELEGATED_CHILD_CONTEXT"] = "1"
    else:
        env.pop("HERMES_DELEGATED_CHILD_CONTEXT", None)
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def _assert_bounded_delegated_read_failure(
    result: subprocess.CompletedProcess[str],
    message: str,
) -> None:
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert message.lower() in result.stderr.lower()
    assert "traceback" not in result.stderr.lower()
    assert len(result.stderr) < 2_000
    assert len(result.stdout) < 2_000


def test_delegated_child_kanban_cli_refusal_returns_nonzero_exit_status(tmp_path):
    """A printed Kanban mutation refusal must not look like CLI success."""
    home = tmp_path / "hermes"
    home.mkdir()

    created = _run_hermes(home, "kanban", "create", "exit status probe", "--json")
    assert created.returncode == 0, created.stderr
    task_id = json.loads(created.stdout)["id"]

    refused = _run_hermes(
        home,
        "kanban",
        "comment",
        task_id,
        "must be refused",
        marker=True,
    )

    assert refused.returncode == 1
    assert "delegate_task child contexts cannot mutate Kanban tasks via the CLI" in refused.stderr


def test_delegated_child_can_read_initialized_board_without_mutating_it(tmp_path):
    home = tmp_path / "hermes"
    home.mkdir()

    created = _run_hermes(home, "kanban", "create", "read-only probe", "--json")
    assert created.returncode == 0, created.stderr
    task_id = json.loads(created.stdout)["id"]
    db_path = home / "kanban.db"
    before_digest = hashlib.sha256(db_path.read_bytes()).hexdigest()

    listed = _run_hermes(home, "kanban", "list", "--json", marker=True)
    assert listed.returncode == 0, listed.stderr
    assert task_id in {row["id"] for row in json.loads(listed.stdout)}

    shown = _run_hermes(home, "kanban", "show", task_id, "--json", marker=True)
    assert shown.returncode == 0, shown.stderr
    assert json.loads(shown.stdout)["task"]["id"] == task_id

    boards = _run_hermes(home, "kanban", "boards", "list", "--json", marker=True)
    assert boards.returncode == 0, boards.stderr
    default = next(row for row in json.loads(boards.stdout) if row["slug"] == "default")
    assert default["total"] == 1
    assert sum(default["counts"].values()) == 1

    board = _run_hermes(home, "kanban", "boards", "show", marker=True)
    assert board.returncode == 0, board.stderr
    assert "Tasks:        1 total" in board.stdout
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before_digest


def test_delegated_child_read_refuses_to_initialize_a_fresh_board(tmp_path):
    home = tmp_path / "hermes"
    home.mkdir()

    commands = [
        ("kanban", "list", "--json"),
        ("kanban", "show", "missing-task", "--json"),
        ("kanban", "tail", "missing-task", "--interval", "0.1"),
    ]
    for command in commands:
        result = _run_hermes(home, *command, marker=True)
        _assert_bounded_delegated_read_failure(
            result,
            "may only read an already initialized Kanban board",
        )
    assert not (home / "kanban.db").exists()


def test_delegated_child_invalid_header_reads_fail_without_tracebacks(tmp_path):
    home = tmp_path / "hermes"
    home.mkdir()
    db_path = home / "kanban.db"
    db_path.write_bytes(b"not a sqlite database\n")
    before = db_path.read_bytes()

    commands = [
        ("kanban", "list", "--json"),
        ("kanban", "show", "missing-task", "--json"),
        ("kanban", "tail", "missing-task", "--interval", "0.1"),
    ]
    for command in commands:
        result = _run_hermes(home, *command, marker=True)
        _assert_bounded_delegated_read_failure(result, "invalid SQLite header")

    assert db_path.read_bytes() == before


def test_delegated_child_legacy_board_reads_fail_loudly_without_false_zero_counts(tmp_path):
    home = tmp_path / "hermes"
    home.mkdir()
    db_path = home / "kanban.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(kb.SCHEMA_SQL)
        conn.execute(
            "INSERT INTO tasks (id, title, status, created_at) VALUES (?, ?, ?, ?)",
            ("legacy-task", "legacy nonzero", "ready", 1),
        )
        conn.commit()
    finally:
        conn.close()

    commands = [
        ("kanban", "list", "--json"),
        ("kanban", "show", "legacy-task", "--json"),
    ]
    for command in commands:
        result = _run_hermes(home, *command, marker=True)
        assert result.returncode == 1, (command, result.stdout, result.stderr)
        assert result.stdout.strip() == "", (command, result.stdout)
        assert "schema contract" in result.stderr.lower(), (command, result.stderr)

    with sqlite3.connect(db_path) as check:
        assert check.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_delegated_boards_list_preserves_healthy_rows_and_marks_unreadable_board(tmp_path):
    home = tmp_path / "hermes"
    home.mkdir()

    healthy = _run_hermes(home, "kanban", "create", "healthy nonzero", "--json")
    assert healthy.returncode == 0, healthy.stderr
    created_board = _run_hermes(home, "kanban", "boards", "create", "legacy")
    assert created_board.returncode == 0, created_board.stderr

    legacy_path = home / "kanban" / "boards" / "legacy" / "kanban.db"
    legacy_path.unlink()
    with sqlite3.connect(legacy_path) as conn:
        conn.executescript(kb.SCHEMA_SQL)
        conn.execute(
            "INSERT INTO tasks (id, title, status, created_at) VALUES (?, ?, ?, ?)",
            ("legacy-task", "legacy nonzero", "ready", 1),
        )
        conn.commit()

    listed = _run_hermes(home, "kanban", "boards", "list", "--json", marker=True)
    assert listed.returncode == 1, (listed.stdout, listed.stderr)
    assert "traceback" not in listed.stderr.lower()
    rows = {row["slug"]: row for row in json.loads(listed.stdout)}
    assert rows["default"]["total"] == 1
    assert sum(rows["default"]["counts"].values()) == 1
    assert rows["default"]["error"] is None
    assert rows["legacy"]["counts"] is None
    assert rows["legacy"]["total"] is None
    assert rows["legacy"]["error"]["type"] == "IntegrityError"
    assert "schema contract" in rows["legacy"]["error"]["message"].lower()

    human = _run_hermes(home, "kanban", "boards", "list", marker=True)
    assert human.returncode == 1, (human.stdout, human.stderr)
    assert "traceback" not in human.stderr.lower()
    assert "default" in human.stdout
    assert "ready=1" in human.stdout
    assert "legacy" in human.stdout
    assert "ERROR:" in human.stdout
    assert "schema contract" in human.stdout.lower()
    assert len(human.stdout) < 4_000


def test_delegated_boards_show_keeps_repair_metadata_for_unreadable_current_board(tmp_path):
    home = tmp_path / "hermes"
    home.mkdir()
    created_board = _run_hermes(home, "kanban", "boards", "create", "legacy")
    assert created_board.returncode == 0, created_board.stderr
    switched = _run_hermes(home, "kanban", "boards", "switch", "legacy")
    assert switched.returncode == 0, switched.stderr

    legacy_path = home / "kanban" / "boards" / "legacy" / "kanban.db"
    legacy_path.unlink()
    with sqlite3.connect(legacy_path) as conn:
        conn.executescript(kb.SCHEMA_SQL)
        conn.execute(
            "INSERT INTO tasks (id, title, status, created_at) VALUES (?, ?, ?, ?)",
            ("legacy-task", "legacy nonzero", "ready", 1),
        )
        conn.commit()

    shown = _run_hermes(home, "kanban", "boards", "show", marker=True)
    assert shown.returncode == 1, (shown.stdout, shown.stderr)
    assert "traceback" not in shown.stderr.lower()
    assert "Current board: legacy" in shown.stdout
    assert f"DB path:      {legacy_path}" in shown.stdout
    assert "Tasks:        ERROR:" in shown.stdout
    assert "schema contract" in shown.stdout.lower()
    assert "0 total" not in shown.stdout
    assert len(shown.stdout) < 2_000


def test_metadata_present_missing_db_is_unreadable_and_repairable_on_every_board_view(tmp_path):
    home = tmp_path / "hermes"
    home.mkdir()
    healthy = _run_hermes(home, "kanban", "create", "healthy", "--json")
    assert healthy.returncode == 0, healthy.stderr
    created = _run_hermes(home, "kanban", "boards", "create", "missing", "--switch")
    assert created.returncode == 0, created.stderr

    missing_path = home / "kanban" / "boards" / "missing" / "kanban.db"
    for suffix in ("", "-wal", "-shm"):
        missing_path.with_name(missing_path.name + suffix).unlink(missing_ok=True)

    listed_json = _run_hermes(home, "kanban", "boards", "list", "--json", marker=True)
    assert listed_json.returncode == 1, (listed_json.stdout, listed_json.stderr)
    rows = {row["slug"]: row for row in json.loads(listed_json.stdout)}
    assert rows["default"]["total"] == 1
    assert rows["default"]["error"] is None
    missing = rows["missing"]
    assert missing["counts"] is None
    assert missing["total"] is None
    assert missing["error"] == {
        "type": "BoardDatabaseMissing",
        "message": "Kanban board database is missing",
        "repairable": True,
        "repair": {
            "action": "initialize",
            "command": "hermes kanban --board missing init",
            "requires_writable_parent": True,
        },
    }
    assert missing["db_path"] == str(missing_path)

    listed_human = _run_hermes(home, "kanban", "boards", "list", marker=True)
    assert listed_human.returncode == 1
    assert "default" in listed_human.stdout and "ready=1" in listed_human.stdout
    assert "missing" in listed_human.stdout
    assert "ERROR: Kanban board database is missing" in listed_human.stdout
    assert "hermes kanban --board missing init" in listed_human.stdout
    assert "(empty)" not in next(
        line for line in listed_human.stdout.splitlines() if "missing" in line
    )

    shown_json = _run_hermes(home, "kanban", "boards", "show", "--json", marker=True)
    assert shown_json.returncode == 1, (shown_json.stdout, shown_json.stderr)
    shown_payload = json.loads(shown_json.stdout)
    assert shown_payload["slug"] == "missing"
    assert shown_payload["counts"] is None
    assert shown_payload["total"] is None
    assert shown_payload["error"] == missing["error"]

    shown_human = _run_hermes(home, "kanban", "boards", "show", marker=True)
    assert shown_human.returncode == 1
    assert "Tasks:        ERROR: Kanban board database is missing" in shown_human.stdout
    assert "Repair:       hermes kanban --board missing init" in shown_human.stdout
    assert "0 total" not in shown_human.stdout
