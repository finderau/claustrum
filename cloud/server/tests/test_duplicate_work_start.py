"""A repeated opening brief stops before a second session starts work."""

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest


_CLI_PATH = Path(__file__).resolve().parents[3] / "claustrum"
loader = importlib.machinery.SourceFileLoader("claustrum_cli_duplicate", str(_CLI_PATH))
spec = importlib.util.spec_from_loader("claustrum_cli_duplicate", loader)
cli = importlib.util.module_from_spec(spec)
sys.modules["claustrum_cli_duplicate"] = cli
loader.exec_module(cli)

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
    monkeypatch.delenv("CLAUSTRUM_DUPLICATE_WORK", raising=False)
    monkeypatch.delenv("CLAUSTRUM_PRIVATE", raising=False)
    return cli


def _prompt(local, monkeypatch, uid, brief):
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
    assert _prompt(local, monkeypatch, "second", BRIEF)[0] == 0
    monkeypatch.setenv("CLAUSTRUM_DUPLICATE_WORK", "0")
    assert _prompt(local, monkeypatch, "third", BRIEF)[0] == 0
