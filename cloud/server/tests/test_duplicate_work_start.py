"""A repeated opening brief stops before a second session starts work."""

import contextlib
from concurrent.futures import ThreadPoolExecutor
import importlib.machinery
import importlib.util
import io
import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest


_CLI_PATH = Path(__file__).resolve().parents[3] / "claustrum"
loader = importlib.machinery.SourceFileLoader("claustrum_cli_duplicate", str(_CLI_PATH))
spec = importlib.util.spec_from_loader("claustrum_cli_duplicate", loader)
cli = importlib.util.module_from_spec(spec)
sys.modules["claustrum_cli_duplicate"] = cli
loader.exec_module(cli)
REAL_CLOUD_WORK_START = cli._cloud_work_start

PLAN = "/projects/workspace-automation/docs/bot-iap-gateway-migration-plan.md"
BRIEF = (
    f"Continue the bot MCP identity migration. Read the plan at {PLAN} "
    "(local branch docs/bot-iap-gateway-migration, commit 6d0b1505). "
    "Verify the bot principals before implementation and use a worktree and PRs."
)


@pytest.fixture
def local(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "DB_DIR", tmp_path)
    monkeypatch.setattr(cli, "DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(cli, "_reap_dead", lambda db, now: [])
    monkeypatch.setattr(cli, "_get_session_label", lambda: "bot-iap-gateway-migration")
    monkeypatch.setattr(cli, "_spawn_cloud_sync", lambda uid, cwd: None)
    monkeypatch.setattr(cli, "_cloud_work_start", lambda record, deadline: None)
    root = tmp_path / "workspace-automation"
    (root / "docs").mkdir(parents=True)
    cli._test_plan = root / "docs" / "bot-iap-gateway-migration-plan.md"
    cli._test_plan.write_text("# Bot migration\n")
    monkeypatch.setattr(cli, "_git_info", lambda path: ("workspace-automation", "docs/bot-iap-gateway-migration"))
    monkeypatch.setattr(cli, "_git_toplevel", lambda path: str(root))
    monkeypatch.delenv("CLAUSTRUM_DUPLICATE_WORK", raising=False)
    monkeypatch.delenv("CLAUSTRUM_PRIVATE", raising=False)
    monkeypatch.delenv("CLAUSTRUM_PUBLIC", raising=False)
    return cli


def _prompt(local, monkeypatch, uid, brief):
    brief = brief.replace(PLAN, str(local._test_plan))
    payload = {"session_id": uid, "cwd": "/projects", "prompt": brief}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            local.hook_prompt(None)
            code = 0
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def test_identical_seed_blocks_once_even_after_original_session_is_reaped(local, monkeypatch):
    assert _prompt(local, monkeypatch, "first", BRIEF)[0] == 0
    db = local.get_db()
    db.execute("DELETE FROM sessions WHERE uid = 'first'")
    db.commit()
    db.close()

    code, _, err = _prompt(local, monkeypatch, "second", BRIEF)
    assert code == 2
    assert "bot-iap-gateway-migration" in err
    assert "review that session's progress" in err
    assert "resend the prompt" in err
    assert _prompt(local, monkeypatch, "second", BRIEF)[0] == 0


def test_same_plan_and_branch_catches_changed_commit_and_wording(local, monkeypatch):
    assert _prompt(local, monkeypatch, "first", BRIEF)[0] == 0
    changed = BRIEF.replace("Continue the bot MCP identity migration", "Resume the gateway work")
    changed = changed.replace("6d0b1505", "999d2639")
    assert _prompt(local, monkeypatch, "second", changed)[0] == 2
    assert _prompt(local, monkeypatch, "second", BRIEF)[0] == 0


def test_plain_markdown_plan_and_branch_without_slash_match(local, monkeypatch):
    plain_plan = local._test_plan.with_name("migration.md")
    plain_plan.write_text("# Migration\n")
    first = BRIEF.replace(PLAN, str(plain_plan))
    first = first.replace("docs/bot-iap-gateway-migration, commit", "migration, commit")
    second = first.replace("Continue the bot MCP identity migration", "Continue the shared gateway task")
    assert _prompt(local, monkeypatch, "first", first)[0] == 0
    assert _prompt(local, monkeypatch, "second", second)[0] == 2


def test_same_repo_relative_plan_across_different_worktrees(local, monkeypatch):
    first = BRIEF.replace("Continue the bot MCP identity migration", "Start the gateway work")
    assert _prompt(local, monkeypatch, "first", first)[0] == 0
    other_root = local._test_plan.parents[1].parent / "other-worktree"
    other_plan = other_root / "docs" / local._test_plan.name
    other_plan.parent.mkdir(parents=True)
    other_plan.write_text("# Bot migration\n")
    monkeypatch.setattr(local, "_git_toplevel", lambda path: str(other_root))
    second = BRIEF.replace(PLAN, str(other_plan))
    second = second.replace("Continue the bot MCP identity migration", "Resume the gateway task")
    assert _prompt(local, monkeypatch, "second", second)[0] == 2


def test_different_branch_private_work_and_short_prompts_do_not_match(local, monkeypatch):
    assert _prompt(local, monkeypatch, "first", BRIEF)[0] == 0
    assert _prompt(local, monkeypatch, "other-branch", BRIEF.replace(
        "docs/bot-iap-gateway-migration", "feat/other-bot-migration").replace(
        "Continue the bot MCP identity migration", "A separate migration task"))[0] == 0
    monkeypatch.setenv("CLAUSTRUM_PRIVATE", "1")
    assert _prompt(local, monkeypatch, "private", BRIEF)[0] == 0
    monkeypatch.delenv("CLAUSTRUM_PRIVATE")
    assert _prompt(local, monkeypatch, "short-one", "Run tests")[0] == 0
    assert _prompt(local, monkeypatch, "short-two", "Run tests")[0] == 0


def test_old_brief_expires_and_opt_out_works(local, monkeypatch):
    assert _prompt(local, monkeypatch, "first", BRIEF)[0] == 0
    db = local.get_db()
    db.execute("UPDATE work_starts SET first_at = first_at - ? WHERE uid = 'first'",
               (local.DUPLICATE_WORK_SECONDS + 1,))
    db.commit()
    db.close()
    db = local.get_db()
    assert db.execute("SELECT 1 FROM work_starts WHERE uid = 'first'").fetchone() is None
    db.close()
    assert _prompt(local, monkeypatch, "second", BRIEF)[0] == 0
    monkeypatch.setenv("CLAUSTRUM_DUPLICATE_WORK", "0")
    assert _prompt(local, monkeypatch, "third", BRIEF)[0] == 0


def test_existing_conversation_after_upgrade_does_not_register_late_prompt(local, monkeypatch):
    db = local.get_db()
    local._ensure_session(db, "old", "/projects")
    db.execute("UPDATE sessions SET turn_count = 5, work_start_eligible = 0 WHERE uid = 'old'")
    db.commit()
    db.close()
    assert _prompt(local, monkeypatch, "old", BRIEF)[0] == 0
    db = local.get_db()
    assert db.execute("SELECT 1 FROM work_starts WHERE uid = 'old'").fetchone() is None
    db.close()
    assert _prompt(local, monkeypatch, "new", BRIEF)[0] == 0


def test_new_session_stays_eligible_after_a_short_first_prompt(local, monkeypatch):
    assert _prompt(local, monkeypatch, "new", "Run tests")[0] == 0
    assert _prompt(local, monkeypatch, "new", BRIEF)[0] == 0
    db = local.get_db()
    assert db.execute("SELECT opening_id FROM work_starts WHERE uid = 'new'").fetchone()[0]
    db.close()
    assert _prompt(local, monkeypatch, "other", BRIEF)[0] == 2


def test_clear_reopens_same_session_with_new_opening_id(local, monkeypatch):
    sent = []
    monkeypatch.setattr(local, "_should_cloud_send", lambda: True)
    monkeypatch.setattr(local, "_cloud_work_start", lambda record, deadline: sent.append(record) or {"match": None})
    assert _prompt(local, monkeypatch, "same", BRIEF)[0] == 0
    monkeypatch.setattr(local, "_cloud_checkin", lambda uid, cwd=None: None)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "session_id": "same", "cwd": "/projects", "source": "clear",
    })))
    local.hook_start(None)
    changed = (
        "Prepare a separate training guide for the support team, with examples "
        "of the customer-facing steps and a review checklist for the new workflow. "
        "This is independent of the bot gateway migration."
    )
    assert _prompt(local, monkeypatch, "same", changed)[0] == 0
    assert len(sent) == 2 and sent[0]["opening_id"] != sent[1]["opening_id"]


def test_private_opening_survives_clear_for_other_sessions(local, monkeypatch):
    monkeypatch.setenv("CLAUSTRUM_PRIVATE", "1")
    assert _prompt(local, monkeypatch, "first", BRIEF)[0] == 0
    monkeypatch.setattr(local, "_cloud_checkin", lambda uid, cwd=None: None)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "session_id": "first", "cwd": "/projects", "source": "clear",
    })))
    local.hook_start(None)
    unrelated = (
        "Prepare a separate training guide for the support team, with examples "
        "of the customer-facing steps and a review checklist for the new workflow. "
        "This is independent of the bot gateway migration."
    )
    assert _prompt(local, monkeypatch, "first", unrelated)[0] == 0
    assert _prompt(local, monkeypatch, "other", BRIEF)[0] == 2
    db = local.get_db()
    assert db.execute("SELECT COUNT(*) FROM work_starts WHERE uid = 'first'").fetchone()[0] == 2
    db.close()


def test_unrelated_prompt_does_not_acknowledge_blocked_opening(local, monkeypatch):
    assert _prompt(local, monkeypatch, "first", BRIEF)[0] == 0
    assert _prompt(local, monkeypatch, "second", BRIEF)[0] == 2
    unrelated = (
        "Prepare a separate training guide for the support team, with examples "
        "of the customer-facing steps and a review checklist for the new workflow. "
        "This is independent of the bot gateway migration."
    )
    assert _prompt(local, monkeypatch, "second", unrelated)[0] == 0
    db = local.get_db()
    rows = db.execute(
        "SELECT acknowledged, is_current FROM work_starts WHERE uid = 'second' "
        "ORDER BY first_at"
    ).fetchall()
    assert [tuple(row) for row in rows] == [(0, 0), (1, 1)]
    db.close()


def test_old_local_work_start_schema_migrates_without_losing_fingerprint(local):
    db = sqlite3.connect(local.DB_PATH)
    db.execute(
        "CREATE TABLE work_starts (uid TEXT PRIMARY KEY, opening_id TEXT, "
        "first_at REAL NOT NULL, prompt_hash TEXT NOT NULL, plan_path TEXT, "
        "plan_key TEXT, repo TEXT, branch TEXT, label TEXT, private INTEGER, "
        "acknowledged INTEGER)"
    )
    db.execute(
        "INSERT INTO work_starts (uid, first_at, prompt_hash, private, acknowledged) "
        "VALUES ('old', ?, 'abc', 1, 1)", (time.time() - 100,)
    )
    db.commit()
    db.close()
    def read_upgraded(_):
        upgraded = local.get_db()
        row = upgraded.execute("SELECT opening_id, uid, prompt_hash FROM work_starts").fetchone()
        key = [r["name"] for r in upgraded.execute("PRAGMA table_info(work_starts)") if r["pk"]]
        old = upgraded.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'work_starts_old'"
        ).fetchone()
        upgraded.close()
        return tuple(row), key, old

    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(read_upgraded, range(2)))
    assert rows == [(("old", "old", "abc"), ["opening_id"], None)] * 2


def test_cloud_match_blocks_and_retry_acknowledges(local, monkeypatch):
    sent = []
    monkeypatch.setattr(local, "_should_cloud_send", lambda: True)

    def cloud(record, deadline):
        sent.append(record)
        if not record["acknowledge"]:
            return {"match": {
                "uid": "other-machine", "user_email": "colleague@finder.com",
                "machine": "colleague-laptop", "label": "bot-migration",
                "repo": "workspace-automation", "branch": "docs/bot-iap-gateway-migration",
                "first_at": "2026-11-18T07:14:52Z",
            }}
        return {"match": None}

    monkeypatch.setattr(local, "_cloud_work_start", cloud)
    code, _, err = _prompt(local, monkeypatch, "new", BRIEF)
    assert code == 2 and "colleague@finder.com" in err
    assert len(sent) == 1 and sent[0]["plan_key"]
    assert "plan_path" not in sent[0] and "private" not in sent[0]
    assert _prompt(local, monkeypatch, "new", BRIEF)[0] == 0
    assert len(sent) == 2 and sent[1]["acknowledge"] is True


def test_unavailable_company_check_warns_and_retries_later(local, monkeypatch):
    calls = []
    monkeypatch.setattr(local, "_should_cloud_send", lambda: True)
    monkeypatch.setattr(local, "_spawn_cloud_sync", lambda uid, cwd: calls.append(("warm", uid)))
    monkeypatch.setattr(local, "_cloud_work_start", lambda record, deadline: calls.append(("check", record)) or None)

    code, out, _ = _prompt(local, monkeypatch, "new", BRIEF)
    assert code == 0 and "company-wide check is unavailable" in out
    assert calls[0][0] == "check" and calls[1] == ("warm", "new")
    db = local.get_db()
    assert db.execute("SELECT cloud_checked FROM work_starts WHERE uid = 'new'").fetchone()[0] == 0
    db.close()

    monkeypatch.setattr(local, "_cloud_work_start", lambda record, deadline: {"match": None})
    assert _prompt(local, monkeypatch, "new", BRIEF)[0] == 0
    db = local.get_db()
    assert db.execute("SELECT cloud_checked FROM work_starts WHERE uid = 'new'").fetchone()[0] == 1
    db.close()


def test_private_session_never_calls_cloud(local, monkeypatch):
    monkeypatch.setenv("CLAUSTRUM_PRIVATE", "1")
    monkeypatch.setattr(local, "_cloud_work_start", lambda record, deadline: pytest.fail("private cloud call"))
    assert _prompt(local, monkeypatch, "private", BRIEF)[0] == 0


@pytest.mark.parametrize("privacy,disabled", [
    (False, False),
    (True, False),
    (False, True),
])
def test_prompt_hook_db_error_fails_open(local, monkeypatch, privacy, disabled):
    def locked_db(_args):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(local, "hook_prompt", locked_db)
    monkeypatch.setattr(sys, "argv", ["claustrum", "hook", "prompt"])
    if privacy:
        monkeypatch.setenv("CLAUSTRUM_PRIVATE", "1")
    if disabled:
        monkeypatch.setenv("CLAUSTRUM_DUPLICATE_WORK", "0")
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        local.main()
    assert "database is locked" in err.getvalue()


class _CommitFails:
    def __init__(self, db):
        self._db = db

    def __getattr__(self, name):
        return getattr(self._db, name)

    def commit(self):
        raise sqlite3.OperationalError("database is locked")


def test_confirmed_local_duplicate_blocks_when_commit_fails(local, monkeypatch):
    assert _prompt(local, monkeypatch, "first", BRIEF)[0] == 0
    real_get_db = local.get_db
    monkeypatch.setattr(local, "get_db", lambda: _CommitFails(real_get_db()))
    code, _, err = _prompt(local, monkeypatch, "second", BRIEF)
    assert code == 2
    assert "database is locked" in err and "review that session's progress" in err


def test_confirmed_cloud_duplicate_blocks_when_commit_fails(local, monkeypatch):
    monkeypatch.setattr(local, "_should_cloud_send", lambda: True)
    monkeypatch.setattr(local, "_cloud_work_start", lambda record, deadline: {"match": {
        "uid": "other-machine", "user_email": "colleague@finder.com",
        "label": "bot-migration", "first_at": "2026-11-18T07:14:52Z",
    }})
    real_get_db = local.get_db
    calls = {"n": 0}

    class _SecondCommitFails(_CommitFails):
        def commit(self):
            calls["n"] += 1
            if calls["n"] > 1:
                raise sqlite3.OperationalError("database is locked")
            self._db.commit()

    monkeypatch.setattr(local, "get_db", lambda: _SecondCommitFails(real_get_db()))
    code, _, err = _prompt(local, monkeypatch, "new", BRIEF)
    assert code == 2
    assert "database is locked" in err and "colleague@finder.com" in err


def test_cloud_work_start_respects_prompt_deadline(local, monkeypatch):
    monkeypatch.setattr(local, "_should_cloud_send", lambda: True)

    def slow_request(*args, **kwargs):
        time.sleep(0.7)
        return {"match": None}

    monkeypatch.setattr(local, "_cloud_request", slow_request)
    start = time.monotonic()
    assert REAL_CLOUD_WORK_START({}, start + 0.02) is None
    assert time.monotonic() - start < 0.5

    monkeypatch.setattr(local, "_cloud_request", lambda *args, **kwargs: {"match": None})
    assert REAL_CLOUD_WORK_START({}, time.monotonic() + 1) == {"match": None}
