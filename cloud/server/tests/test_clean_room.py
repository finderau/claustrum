"""Clean-room sessions in the `claustrum` CLI client (repo-root script).

A clean-room session (Conductor; workspace-automation
docs/adr/conductor-clean-room-sessions.md) stays registered and keeps file
claims, but is shown nothing about other sessions until a human reconnects
it. The wa registry ($XDG_STATE_HOME/wa/clean-sessions.tsv) decides; the
launch env vars only count when the registry has no row for the session.

No DB server, no cloud, no tmux/git: local sqlite goes to a tmp dir and every
_cloud_* call is stubbed.
"""
import importlib.machinery
import importlib.util
import io
import json
import sys
import types
from pathlib import Path

import pytest

_CLI_PATH = Path(__file__).resolve().parents[3] / "claustrum"


def _load_cli():
    loader = importlib.machinery.SourceFileLoader("claustrum_cli", str(_CLI_PATH))
    spec = importlib.util.spec_from_loader("claustrum_cli", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["claustrum_cli"] = mod
    loader.exec_module(mod)
    return mod


claustrum = _load_cli()

CLEAN_UID = "clean-uid-1"


@pytest.fixture
def cli(tmp_path, monkeypatch):
    monkeypatch.setattr(claustrum, "DB_DIR", tmp_path)
    monkeypatch.setattr(claustrum, "DB_PATH", tmp_path / "state.db")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    for k in ("CLAUSTRUM_NO_INJECT", "WA_CLEAN_SESSION"):
        monkeypatch.delenv(k, raising=False)
    calls = {"checkin": 0, "list": 0, "inbox": 0}

    def _checkin(u, task=None, cwd=None, label=None, **k):
        calls["checkin"] += 1
        return {}

    def _list(u, **k):
        calls["list"] += 1
        return {"tiers": {"t3_topic": {"topic": "peer-topic", "count": 1,
                                       "peers": [{"user_email": "alice@finder.com"}]}},
                "solved": []}

    def _inbox(u):
        calls["inbox"] += 1
        return [{"id": "m1", "type": "info", "from_uid": "peer-9",
                 "body": "cross-machine idea"}]

    monkeypatch.setattr(claustrum, "_should_cloud_send", lambda *a, **k: True)
    monkeypatch.setattr(claustrum, "_cloud_checkin", _checkin)
    monkeypatch.setattr(claustrum, "_cloud_list_peers", _list)
    monkeypatch.setattr(claustrum, "_cloud_inbox_drain", _inbox)
    monkeypatch.setattr(claustrum, "_close_intents_on_merge", lambda *a, **k: None)
    monkeypatch.setattr(claustrum, "_reap_dead", lambda db, now: [])
    monkeypatch.setattr(claustrum, "_get_session_label", lambda: "clean-room-test")
    monkeypatch.setattr(claustrum, "_self_rename_if_generic", lambda *a, **k: None)
    monkeypatch.setattr(claustrum, "_cloud_cache_path",
                        lambda u: tmp_path / "cloud-cache" / f"{u}.json")
    monkeypatch.setattr(claustrum, "_inbox_path",
                        lambda u: tmp_path / "inbox" / f"{u}.json")
    monkeypatch.setenv("CLAUSTRUM_DUPLICATE_WORK", "0")
    return claustrum, calls


def _register(m, uid, event):
    path = Path(m._clean_registry_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"1700000000\t$1\t1700000000\tclaude\t{uid}\tclean-test\t{event}\n")


def _add_peer(m, task="peer secret plan"):
    db = m.get_db()
    m._ensure_session(db, "peer-uid-1", "/tmp/x")
    db.execute("UPDATE sessions SET status = 'active', last_seen = ?, task = ?, "
               "label = 'peer-session' WHERE uid = 'peer-uid-1'", (m.time.time(), task))
    db.execute("INSERT INTO messages (from_uid, to_uid, type, body, created_at) "
               "VALUES ('peer-uid-1', NULL, 'info', 'peer message body', ?)",
               (m.time.time(),))
    db.commit()
    db.close()


def _unread(m, uid):
    db = m.get_db()
    try:
        return db.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE read_at IS NULL AND from_uid != ?",
            (uid,)).fetchone()["n"]
    finally:
        db.close()


def _run_prompt(m, monkeypatch, uid, prompt="design the thing"):
    payload = {"session_id": uid, "cwd": "/tmp/x", "prompt": prompt, "transcript_path": None}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    monkeypatch.setattr(m, "_spawn_cloud_sync", lambda u, c: None)
    m.hook_prompt(None)
    return buf.getvalue()


# --- state precedence -------------------------------------------------------

def test_not_clean_without_env_or_row(cli):
    m, _ = cli
    assert m._clean_room(CLEAN_UID) is False


@pytest.mark.parametrize("var", ["CLAUSTRUM_NO_INJECT", "WA_CLEAN_SESSION"])
def test_env_alone_means_clean(cli, monkeypatch, var):
    m, _ = cli
    monkeypatch.setenv(var, "1")
    assert m._clean_room(CLEAN_UID) is True


def test_reconnected_row_overrides_env(cli, monkeypatch):
    m, _ = cli
    monkeypatch.setenv("CLAUSTRUM_NO_INJECT", "1")
    _register(m, CLEAN_UID, "clean")
    assert m._clean_room(CLEAN_UID) is True
    _register(m, CLEAN_UID, "reconnected")
    assert m._clean_room(CLEAN_UID) is False


def test_row_without_env_is_clean(cli):
    m, _ = cli
    _register(m, CLEAN_UID, "clean")
    assert m._clean_room(CLEAN_UID) is True


def test_unknown_event_fails_closed(cli):
    m, _ = cli
    _register(m, CLEAN_UID, "reconnected")
    _register(m, CLEAN_UID, "garbled")
    assert m._clean_room(CLEAN_UID) is True


def test_other_conversations_rows_ignored(cli):
    m, _ = cli
    _register(m, "someone-else", "clean")
    assert m._clean_room(CLEAN_UID) is False


# --- prompt hook ------------------------------------------------------------

def test_clean_prompt_hides_peers_and_keeps_messages_unread(cli, monkeypatch):
    m, calls = cli
    _register(m, CLEAN_UID, "clean")
    _add_peer(m)
    m.cmd_cloud_sync(types.SimpleNamespace(uid=CLEAN_UID, cwd="/tmp/x"))
    out = _run_prompt(m, monkeypatch, CLEAN_UID)
    for leaked in ("peer-session", "peer message body", "Cross-machine",
                   "peer-topic", "cross-machine idea", "other session"):
        assert leaked not in out
    assert _unread(m, CLEAN_UID) == 1
    # Still registered: the session row and its task exist locally.
    db = m.get_db()
    row = db.execute("SELECT task FROM sessions WHERE uid = ?", (CLEAN_UID,)).fetchone()
    db.close()
    assert row and row["task"] == "design the thing"


def test_reconnect_restores_context_without_restart(cli, monkeypatch):
    m, _ = cli
    monkeypatch.setenv("CLAUSTRUM_NO_INJECT", "1")
    _register(m, CLEAN_UID, "clean")
    _add_peer(m)
    assert "peer-session" not in _run_prompt(m, monkeypatch, CLEAN_UID)
    _register(m, CLEAN_UID, "reconnected")
    out = _run_prompt(m, monkeypatch, CLEAN_UID)
    assert "peer-session" in out
    assert "peer message body" in out


def test_clean_collision_names_files_not_tasks(cli, monkeypatch):
    m, _ = cli
    _register(m, CLEAN_UID, "clean")
    _add_peer(m)
    db = m.get_db()
    m._ensure_session(db, CLEAN_UID, "/tmp/x")
    now = m.time.time()
    db.execute("INSERT INTO claims (filepath, uid, claimed_at) VALUES ('/r/a.py', ?, ?)",
               (CLEAN_UID, now))
    db.execute("INSERT INTO claims (filepath, uid, claimed_at) "
               "VALUES ('/r/a.py', 'peer-uid-1', ?)", (now,))
    db.commit()
    db.close()
    out = _run_prompt(m, monkeypatch, CLEAN_UID)
    assert "Collisions" in out and "a.py" in out and "peer-uid-1" in out
    # Labels are often derived from the task, so they're withheld too.
    assert "peer-session" not in out
    assert "peer secret plan" not in out
    assert "peer message body" not in out


def test_event_match_ignores_case(cli):
    m, _ = cli
    _register(m, CLEAN_UID, "clean")
    _register(m, CLEAN_UID, "Reconnected")
    assert m._clean_room(CLEAN_UID) is False


def test_reconnected_long_prompt_is_not_an_opening(cli, monkeypatch):
    m, _ = cli
    _register(m, CLEAN_UID, "clean")
    _run_prompt(m, monkeypatch, CLEAN_UID)
    _register(m, CLEAN_UID, "reconnected")
    monkeypatch.setenv("CLAUSTRUM_DUPLICATE_WORK", "1")
    monkeypatch.setattr(m, "_should_cloud_send", lambda *a, **k: False)
    recorded = []
    real = m._record_work_start

    def _spy(db, uid, raw, now):
        result = real(db, uid, raw, now)
        recorded.append(result[1])
        return result

    monkeypatch.setattr(m, "_record_work_start", _spy)
    _run_prompt(m, monkeypatch, CLEAN_UID, prompt="reconcile " * 40)
    assert recorded == [None]


def test_clean_sync_retires_pre_clean_cache(cli):
    m, _ = cli
    m.cmd_cloud_sync(types.SimpleNamespace(uid=CLEAN_UID, cwd="/tmp/x"))
    assert m._read_cloud_cache(CLEAN_UID).get("tiers")
    _register(m, CLEAN_UID, "clean")
    m.cmd_cloud_sync(types.SimpleNamespace(uid=CLEAN_UID, cwd="/tmp/x"))
    assert not m._read_cloud_cache(CLEAN_UID).get("synced_at")


def test_directed_message_not_pushed_to_clean_room(cli, monkeypatch):
    m, _ = cli
    _register(m, CLEAN_UID, "clean")
    monkeypatch.setenv("CLAUSTRUM_SOCKET_PUSH", "1")
    pushed = []
    monkeypatch.setattr(m, "_socket_push", lambda *a: pushed.append(a))
    db = m.get_db()
    for uid in (CLEAN_UID, "peer-uid-2"):
        m._ensure_session(db, uid, "/tmp/x")
        db.execute("UPDATE sessions SET status = 'active', msg_socket = '/tmp/s' "
                   "WHERE uid = ?", (uid,))
    m._deliver(db, "peer-uid-2", CLEAN_UID, "info", "I already concluded X")
    assert pushed == []
    m._deliver(db, CLEAN_UID, "peer-uid-2", "info", "hello")
    assert len(pushed) == 1
    db.commit()
    db.close()


def test_env_seeded_recipient_not_pushed_until_reconnected(cli, monkeypatch):
    m, _ = cli
    monkeypatch.setenv("CLAUSTRUM_SOCKET_PUSH", "1")
    pushed = []
    monkeypatch.setattr(m, "_socket_push", lambda *a: pushed.append(a))
    db = m.get_db()
    monkeypatch.setenv("WA_CLEAN_SESSION", "1")
    m._ensure_session(db, CLEAN_UID, "/tmp/x")
    monkeypatch.delenv("WA_CLEAN_SESSION")
    db.execute("UPDATE sessions SET msg_socket = '/tmp/s' WHERE uid = ?", (CLEAN_UID,))
    m._deliver(db, "peer-uid-2", CLEAN_UID, "info", "I already concluded X")
    assert pushed == []
    _register(m, CLEAN_UID, "reconnected")
    m._deliver(db, "peer-uid-2", CLEAN_UID, "info", "now you can hear me")
    assert len(pushed) == 1
    db.close()


def test_drained_inbox_survives_going_clean(cli, monkeypatch):
    m, calls = cli
    m.cmd_cloud_sync(types.SimpleNamespace(uid=CLEAN_UID, cwd="/tmp/x"))
    _register(m, CLEAN_UID, "clean")
    out = _run_prompt(m, monkeypatch, CLEAN_UID)
    assert "cross-machine idea" not in out
    m.cmd_cloud_sync(types.SimpleNamespace(uid=CLEAN_UID, cwd="/tmp/x"))
    assert not m._read_cloud_cache(CLEAN_UID).get("synced_at")

    _register(m, CLEAN_UID, "reconnected")
    monkeypatch.setattr(m, "_cloud_inbox_drain", lambda u: [])
    m.cmd_cloud_sync(types.SimpleNamespace(uid=CLEAN_UID, cwd="/tmp/x"))
    assert "cross-machine idea" in _run_prompt(m, monkeypatch, CLEAN_UID)
    m.cmd_cloud_sync(types.SimpleNamespace(uid=CLEAN_UID, cwd="/tmp/x"))
    assert "cross-machine idea" not in _run_prompt(m, monkeypatch, CLEAN_UID)


def test_inbox_shown_during_sync_is_not_carried(cli, monkeypatch):
    m, _ = cli
    m.cmd_cloud_sync(types.SimpleNamespace(uid=CLEAN_UID, cwd="/tmp/x"))

    def _drain_while_prompt_shows(u):
        assert "cross-machine idea" in _run_prompt(m, monkeypatch, CLEAN_UID)
        return []

    monkeypatch.setattr(m, "_cloud_inbox_drain", _drain_while_prompt_shows)
    m.cmd_cloud_sync(types.SimpleNamespace(uid=CLEAN_UID, cwd="/tmp/x"))
    assert m._read_cloud_cache(CLEAN_UID).get("inbox") == []


def test_clean_skips_duplicate_work_check(cli, monkeypatch):
    m, _ = cli
    monkeypatch.setenv("CLAUSTRUM_DUPLICATE_WORK", "1")
    _register(m, CLEAN_UID, "clean")

    def _boom(*a, **k):
        raise AssertionError("duplicate-work check ran for a clean room")

    monkeypatch.setattr(m, "_record_work_start", _boom)
    _run_prompt(m, monkeypatch, CLEAN_UID)


# --- cloud sync, session start, overlap gate --------------------------------

def test_clean_sync_checks_in_but_pulls_nothing(cli):
    m, calls = cli
    _register(m, CLEAN_UID, "clean")
    m.cmd_cloud_sync(types.SimpleNamespace(uid=CLEAN_UID, cwd="/tmp/x"))
    assert calls == {"checkin": 1, "list": 0, "inbox": 0}


def test_clean_session_start_hides_peer_count(cli, monkeypatch):
    m, _ = cli
    _register(m, CLEAN_UID, "clean")
    _add_peer(m)
    monkeypatch.setattr(m, "_cloud_resume_check",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("resume check ran")))
    payload = {"session_id": CLEAN_UID, "cwd": "/tmp/x", "source": "startup",
               "hook_event_name": "SessionStart"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    m.hook_start(None)
    out = buf.getvalue()
    assert "Clean room" in out
    assert "other active session" not in out


def test_overlap_gate_skips_clean_room(cli, monkeypatch):
    m, _ = cli
    _register(m, CLEAN_UID, "clean")
    monkeypatch.setattr(m, "_overlap_gate_enabled", lambda: True)
    monkeypatch.setattr(m, "_collect_overlap",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("overlap lookup ran")))
    m._overlap_gate({"session_id": CLEAN_UID, "tool_name": "ExitPlanMode",
                     "tool_input": {"plan": "# Plan\nidea"}, "cwd": "/tmp/x"})
