"""A headless agent run that another agent session started through its shell does
not register as a board session. Conductor's rich sessions, headless but launched
by tmux or the local-server, still register. A registered session is never skipped.

Process tables are injected, so no real `ps` runs.
"""
import importlib.machinery
import importlib.util
import sys
from pathlib import Path

_CLI_PATH = Path(__file__).resolve().parents[3] / "claustrum"


def _load_cli():
    loader = importlib.machinery.SourceFileLoader("claustrum_cli_nested", str(_CLI_PATH))
    spec = importlib.util.spec_from_loader("claustrum_cli_nested", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["claustrum_cli_nested"] = mod
    loader.exec_module(mod)
    return mod


claustrum = _load_cli()

INTERACTIVE = "/Users/me/.local/bin/claude --session-id abc --settings {}"
HOOK_SHELL = "/bin/sh -c claustrum hook start"


def table(*chain):
    """chain is innermost first: [(pid, args), ...]; each row's parent is the next."""
    rows = {}
    for (pid, args), (ppid, _) in zip(chain, list(chain[1:]) + [(1, "")]):
        rows[pid] = (ppid, args)
    return rows


def test_claude_p_run_from_a_session_shell_is_nested():
    t = table((10, HOOK_SHELL), (11, "claude -p 'Production publish. Use ONLY gateway tools'"),
              (12, "/bin/zsh -c source snapshot.sh && claude -p ..."), (13, INTERACTIVE), (14, "tmux new-session"))
    assert claustrum._nested_headless(10, t) is True


def test_a_launcher_between_the_agents_stops_the_walk():
    t = table((10, HOOK_SHELL), (11, "claude -p --input-format stream-json"),
              (12, "node /Users/me/wa/mobile/local-server/index.js"), (13, "/bin/bash wa"), (14, "/bin/zsh"),
              (15, INTERACTIVE))
    assert claustrum._nested_headless(10, t) is False


def test_a_script_run_from_a_session_shell_is_still_nested():
    t = table((10, HOOK_SHELL), (11, "claude -p smoke"), (12, "/bin/bash ./smoke.sh"), (13, "-zsh"), (14, INTERACTIVE))
    assert claustrum._nested_headless(10, t) is True


def test_run_wrappers_do_not_end_the_walk():
    t = table((10, HOOK_SHELL), (11, "claude -p smoke"), (12, "timeout 120 claude -p smoke"),
              (13, "/bin/zsh -c cd /tmp && timeout 120 claude -p smoke"), (14, INTERACTIVE))
    assert claustrum._nested_headless(10, t) is True


def test_codex_profile_flag_is_not_headless_but_exec_after_options_is():
    assert claustrum._is_headless(["codex", "-p", "work"]) is False
    assert claustrum._is_headless(["codex", "-m", "o3", "exec", "go"]) is True
    assert claustrum._is_headless(["codex", "--full-auto", "exec", "go"]) is True


def test_node_flags_before_the_script_are_skipped():
    assert claustrum._agent_argv(["node", "--max-old-space-size=8192", "/usr/local/bin/claude", "-p"]) == ["/usr/local/bin/claude", "-p"]
    assert claustrum._agent_argv(["node", "/srv/app.js"]) is None


def test_a_conductor_rich_session_is_not_nested():
    t = table((10, HOOK_SHELL), (11, "/Users/me/.local/bin/claude -p --input-format stream-json --output-format stream-json"),
              (12, "node /Users/me/wa/mobile/local-server/index.js"), (13, "tmux new-session"))
    assert claustrum._nested_headless(10, t) is False


def test_an_interactive_session_is_not_nested_even_under_another_agent():
    t = table((10, HOOK_SHELL), (11, INTERACTIVE), (12, "/bin/zsh"), (13, "claude -p outer"))
    assert claustrum._nested_headless(10, t) is False


def test_node_run_installs_and_codex_exec_count_as_agents():
    t = table((10, HOOK_SHELL), (11, "codex exec 'Summarise the log'"), (12, "/bin/zsh"),
              (13, "node /usr/local/lib/node_modules/.bin/claude --resume x"))
    assert claustrum._nested_headless(10, t) is True


def test_unknown_or_short_chains_fail_open():
    assert claustrum._nested_headless(99, {}) is False
    assert claustrum._nested_headless(10, table((10, HOOK_SHELL))) is False


def test_a_registered_session_is_never_skipped_and_verdicts_are_remembered(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(claustrum, "_nested_headless", lambda *a, **k: calls.append(1) or True)
    db_path = tmp_path / "state.db"
    db = claustrum.sqlite3.connect(db_path)
    db.execute("CREATE TABLE sessions (uid TEXT)")
    db.execute("INSERT INTO sessions VALUES ('known')")
    db.commit()
    db.close()
    monkeypatch.setattr(claustrum, "DB_PATH", db_path)
    monkeypatch.setattr(claustrum, "NESTED_DIR", tmp_path / "nested")
    monkeypatch.delenv("CLAUSTRUM_NESTED", raising=False)
    assert claustrum._skip_nested_hook('{"session_id": "known"}') is False
    assert claustrum._skip_nested_hook('{"session_id": "new-child"}') is True
    assert claustrum._skip_nested_hook('{"session_id": "new-child"}') is True
    assert len(calls) == 1, "the process walk runs once per nested session"
    monkeypatch.setenv("CLAUSTRUM_NESTED", "1")
    assert claustrum._skip_nested_hook('{"session_id": "other-child"}') is False
