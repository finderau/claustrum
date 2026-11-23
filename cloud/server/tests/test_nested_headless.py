"""A headless agent run started from inside another agent session (a `claude -p`
smoke test a session shells out to) must not register as its own board session:
it inherits the parent's tmux pane and label, so dozens of look-alike rows
appeared. Conductor's rich sessions are headless too, but have no agent ancestor,
and must keep registering.

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


def test_a_registered_session_is_never_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(claustrum, "_nested_headless", lambda *a, **k: True)
    db = claustrum.sqlite3.connect(tmp_path / "s.db")
    db.execute("CREATE TABLE sessions (uid TEXT)")
    db.execute("INSERT INTO sessions VALUES ('known')")
    monkeypatch.setattr(claustrum, "get_db", lambda: db)
    monkeypatch.delenv("CLAUSTRUM_NESTED", raising=False)
    assert claustrum._skip_nested_hook('{"session_id": "known"}') is False
    assert claustrum._skip_nested_hook('{"session_id": "new-child"}') is True
    monkeypatch.setenv("CLAUSTRUM_NESTED", "1")
    assert claustrum._skip_nested_hook('{"session_id": "new-child"}') is False
