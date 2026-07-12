"""Dependency-free tests for the standalone CLI model module.

No installed CLI is executed and no subscription request is made.
Run with: python cli_models_tests.py
"""

import json
import importlib.util
import subprocess
import tempfile
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parent / "spell_daemon" / "cli_models.py"
SPEC = importlib.util.spec_from_file_location("vedium_cli_models_test_target", MODULE_PATH)
assert SPEC and SPEC.loader
cli_models = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli_models)


def test_resolve_and_wrap_windows_shims():
    found = {"codex.cmd": r"C:\npm\codex.cmd"}
    with mock.patch.object(cli_models.shutil, "which", side_effect=found.get) as which:
        assert cli_models.resolve_cli("codex") == found["codex.cmd"]
    assert [call.args[0] for call in which.call_args_list] == ["codex.exe", "codex.cmd"]
    assert cli_models.wrap_command(found["codex.cmd"]) == [
        "cmd.exe", "/c", found["codex.cmd"]
    ]
    assert cli_models.wrap_command(r"C:\bin\codex.exe") == [r"C:\bin\codex.exe"]


def test_flatten_messages_and_served_model():
    messages = [
        {"role": "system", "content": "Be concise."},
        {"role": "user", "content": "My name is Bo."},
        {"role": "assistant", "content": "Hello, Bo."},
        {"role": "user", "content": "What is my name?"},
    ]
    system, prompt = cli_models.flatten_messages(messages)
    assert system == "Be concise."
    assert prompt.startswith("[User]\nMy name is Bo.")
    assert "[Assistant]\nHello, Bo." in prompt
    assert prompt.endswith(
        "[Assistant]\n(Reply as the assistant to the last user message. "
        "Output only the reply itself.)"
    )
    assert cli_models.flatten_messages([
        {"role": "user", "content": "pong"}
    ]) == ("", "pong")

    usage = {
        "claude-haiku-helper": {"outputTokens": 40},
        "claude-sonnet-5": {"outputTokens": 300},
    }
    assert cli_models.served_model(usage, "sonnet") == "claude-sonnet-5"
    assert cli_models.served_model(usage, "opus") == "claude-sonnet-5"
    assert cli_models.served_model(None, "sonnet") == ""


def test_codex_discovery_default_and_fallback():
    with tempfile.TemporaryDirectory() as tmp:
        codex_dir = Path(tmp) / ".codex"
        codex_dir.mkdir()
        (codex_dir / "models_cache.json").write_text(json.dumps({
            "models": [
                {
                    "slug": "gpt-visible",
                    "display_name": "GPT Visible",
                    "visibility": "list",
                    "supported_reasoning_levels": [
                        {"effort": "low"}, {"effort": "high"}, {"effort": "xhigh"}
                    ],
                },
                {"slug": "internal", "visibility": "hide"},
                {"display_name": "missing slug", "visibility": "list"},
            ]
        }), encoding="utf-8")
        (codex_dir / "config.toml").write_text(
            'approval_policy = "never"\nmodel = "gpt-visible"\n', encoding="utf-8"
        )
        assert cli_models.discover_codex_models(tmp) == [{
            "slug": "gpt-visible",
            "label": "GPT Visible",
            "efforts": ["low", "high", "xhigh"],
        }]
        assert cli_models.codex_default_model(tmp) == "gpt-visible"

        (codex_dir / "models_cache.json").write_text("not json", encoding="utf-8")
        assert cli_models.discover_codex_models(tmp) == []

    assert cli_models.discover_codex_models(tmp) == []
    assert cli_models.codex_default_model(tmp) == ""


def test_detection_and_disabled_selection():
    def resolve(name):
        return fr"C:\bin\{name}.cmd"

    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(cli_models, "resolve_cli", side_effect=resolve), \
            mock.patch.object(cli_models, "discover_codex_models", return_value=[]), \
            mock.patch.object(cli_models, "codex_default_model", return_value="gpt-default"):
        detected = cli_models.detect_clis(tmp)
        assert detected["claude"]["models"][0]["id"] == "claude-sonnet"
        assert detected["codex"]["models"] == [{
            "id": "codex",
            "label": "Codex CLI default (gpt-default)",
            "efforts": [],
        }]
        assert detected["antigravity"]["models"][0] == {
            "id": "agy",
            "label": "Antigravity CLI default",
            "efforts": [],
            "agy_model": "",
        }
        assert detected["antigravity"]["models"][3] == {
            "id": "agy-gemini-3.5-flash-low",
            "label": "Gemini 3.5 Flash (Low) (Antigravity CLI)",
            "efforts": [],
            "agy_model": "Gemini 3.5 Flash (Low)",
        }
        assert cli_models.selectable_models(False, tmp) == []
        assert len(cli_models.selectable_models(True, tmp)) == (
            len(cli_models.CLAUDE_ALIASES) + 1
            + 1 + len(cli_models.ANTIGRAVITY_MODELS)
        )


def test_effort_clamping():
    assert cli_models.clamp_codex_effort("", ["low"]) == ""
    assert cli_models.clamp_codex_effort("xhigh", ["low", "medium", "high"]) == "high"
    assert cli_models.clamp_codex_effort("medium", ["high", "xhigh"]) == "high"
    assert cli_models.clamp_codex_effort("max", None) == "high"
    assert cli_models.clamp_codex_effort("low", None) == "low"
    try:
        cli_models.clamp_codex_effort("ultra", None)
    except cli_models.CLIModelError:
        pass
    else:
        raise AssertionError("unknown UI effort should fail")


def test_run_claude_command_and_parsing():
    payload = json.dumps({
        "is_error": False,
        "result": "pong",
        "modelUsage": {
            "claude-haiku-helper": {"outputTokens": 10},
            "claude-sonnet-5": {"outputTokens": 20},
        },
    })
    completed = subprocess.CompletedProcess([], 0, stdout=payload, stderr="")
    with mock.patch.object(cli_models.subprocess, "run", return_value=completed) as run:
        text, served = cli_models.run_claude(
            r"C:\npm\claude.cmd", "sonnet", "System", "Prompt", r"C:\neutral", "low"
        )
    assert (text, served) == ("pong", "claude-sonnet-5")
    command = run.call_args.args[0]
    assert command[:3] == ["cmd.exe", "/c", r"C:\npm\claude.cmd"]
    assert command[command.index("--tools") + 1] == ""
    assert command[command.index("--model") + 1] == "sonnet"
    assert command[command.index("--effort") + 1] == "low"
    assert command[command.index("--system-prompt") + 1] == "System"
    assert run.call_args.kwargs["input"] == "Prompt"
    assert run.call_args.kwargs["cwd"] == r"C:\neutral"
    assert run.call_args.kwargs["encoding"] == "utf-8"


def test_run_claude_error_paths():
    with mock.patch.object(
        cli_models.subprocess,
        "run",
        return_value=subprocess.CompletedProcess([], 2, stdout="", stderr="sign in first"),
    ):
        try:
            cli_models.run_claude("claude.exe", "haiku", "", "x", ".")
        except cli_models.CLIModelError as exc:
            assert "claude exited with 2: sign in first" in str(exc)
        else:
            raise AssertionError("non-zero Claude exit should fail")

    with mock.patch.object(
        cli_models.subprocess, "run", side_effect=subprocess.TimeoutExpired([], 240)
    ):
        try:
            cli_models.run_claude("claude.exe", "haiku", "", "x", ".")
        except cli_models.CLIModelError as exc:
            assert str(exc) == "The CLI took longer than 240s and was stopped."
        else:
            raise AssertionError("Claude timeout should be translated")

    with mock.patch.object(
        cli_models.subprocess,
        "run",
        return_value=subprocess.CompletedProcess([], 0, stdout="not json", stderr=""),
    ):
        try:
            cli_models.run_claude("claude.exe", "haiku", "", "x", ".")
        except cli_models.CLIModelError as exc:
            assert "unparseable output" in str(exc)
        else:
            raise AssertionError("malformed Claude output should fail")


def test_run_codex_command_output_and_cleanup():
    seen = {}

    def fake_run(command, **kwargs):
        output = Path(command[command.index("-o") + 1])
        seen["output"] = output
        output.write_text("pong\n", encoding="utf-8")
        seen["command"] = command
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, stdout="banner", stderr="")

    with tempfile.TemporaryDirectory() as cwd, mock.patch.object(
        cli_models.subprocess, "run", side_effect=fake_run
    ):
        text = cli_models.run_codex(
            r"C:\npm\codex.cmd",
            "gpt-visible",
            "System",
            "Prompt",
            cwd,
            "max",
            ["low", "medium", "high", "xhigh"],
        )
        assert text == "pong"
        command = seen["command"]
        assert command[:4] == ["cmd.exe", "/c", r"C:\npm\codex.cmd", "exec"]
        assert command[command.index("-m") + 1] == "gpt-visible"
        assert command[command.index("-c") + 1] == 'model_reasoning_effort="xhigh"'
        assert command[command.index("--sandbox") + 1] == "read-only"
        assert command[command.index("--cd") + 1] == cwd
        assert command[-1] == "-"
        assert seen["kwargs"]["input"] == "[Instructions]\nSystem\n\nPrompt"
        assert seen["kwargs"]["cwd"] == cwd
        assert not seen["output"].exists()


def test_run_codex_timeout_cleans_output():
    with tempfile.TemporaryDirectory() as cwd, mock.patch.object(
        cli_models.subprocess, "run", side_effect=subprocess.TimeoutExpired([], 240)
    ):
        before = set(Path(cwd).iterdir())
        try:
            cli_models.run_codex("codex.exe", "", "", "Prompt", cwd)
        except cli_models.CLIModelError as exc:
            assert str(exc) == "The CLI took longer than 240s and was stopped."
        else:
            raise AssertionError("Codex timeout should be translated")
        assert set(Path(cwd).iterdir()) == before


def test_run_agy_command_and_log_verification():
    label = "Gemini 3.5 Flash (Low)"
    seen = {}

    def fake_run(command, **kwargs):
        log = Path(command[command.index("--log-file") + 1])
        log.write_text(
            'I0711 printmode.go:97] Print mode: starting\n'
            f'I0711 model_config_manager.go:157] Propagating selected model '
            f'override to backend: label="{label}"\n',
            encoding="utf-8",
        )
        seen["command"] = command
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, stdout="pong\n", stderr="")

    with tempfile.TemporaryDirectory() as cwd, mock.patch.object(
        cli_models.subprocess, "run", side_effect=fake_run
    ):
        text, served = cli_models.run_agy(
            r"C:\agy\agy.exe", label, "System", "Prompt", cwd
        )
        assert (text, served) == ("pong", label)
        command = seen["command"]
        assert command[0] == r"C:\agy\agy.exe"
        assert command[command.index("-p") + 1] == "[Instructions]\nSystem\n\nPrompt"
        assert command[command.index("--mode") + 1] == "plan"
        assert command[command.index("--model") + 1] == label
        assert Path(command[command.index("--log-file") + 1]).parent == Path(cwd)
        assert seen["kwargs"]["input"] == ""
        assert seen["kwargs"]["cwd"] == cwd


def test_run_agy_rejects_silent_model_substitution():
    def fake_run(command, **_kwargs):
        log = Path(command[command.index("--log-file") + 1])
        log.write_text(
            'W0711 model_config_manager.go:54] Failed to resolve model flag\n'
            'I0711 ...] Propagating selected model override to backend: '
            'label="Gemini 3.5 Flash (Medium)"\n',
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout="pong\n", stderr="")

    with tempfile.TemporaryDirectory() as cwd, mock.patch.object(
        cli_models.subprocess, "run", side_effect=fake_run
    ):
        try:
            cli_models.run_agy("agy.exe", "Gemini 3.1 Pro (High)", "", "x", cwd)
        except cli_models.CLIModelError as exc:
            assert "did not run" in str(exc)
        else:
            raise AssertionError("silent model substitution should fail")

        # The default model (no --model) accepts whatever label served.
        text, served = cli_models.run_agy("agy.exe", "", "", "x", cwd)
        assert (text, served) == ("pong", "Gemini 3.5 Flash (Medium)")

    try:
        cli_models.run_agy("agy.exe", "", "", "x" * 30001, ".")
    except cli_models.CLIModelError as exc:
        assert "too long" in str(exc)
    else:
        raise AssertionError("oversized agy prompt should fail")


def test_converse_routes_antigravity_models():
    with mock.patch.object(cli_models, "resolve_cli", return_value="agy.exe"), \
            mock.patch.object(
                cli_models, "run_agy",
                return_value=("pong", "Gemini 3.5 Flash (Low)"),
            ) as run:
        text, provenance = cli_models.converse(
            "agy-gemini-3.5-flash-low",
            [{"role": "user", "content": "Reply pong"}],
            enabled=True,
        )
    assert text == "pong"
    assert provenance == {
        "runner": "Antigravity CLI",
        "model": "Gemini 3.5 Flash (Low)",
        "effort": "",
    }
    assert run.call_args.args[1] == "Gemini 3.5 Flash (Low)"

    with mock.patch.object(cli_models, "resolve_cli", return_value="agy.exe"):
        try:
            cli_models.converse(
                "agy-never-heard-of-it", [{"role": "user", "content": "x"}],
                enabled=True,
            )
        except cli_models.CLIModelError as exc:
            assert "Unknown Antigravity model" in str(exc)
        else:
            raise AssertionError("unknown agy model id should fail")


def test_converse_opt_in_neutral_cwd_and_provenance():
    with mock.patch.object(cli_models, "resolve_cli", return_value="claude.exe"), \
            mock.patch.object(cli_models, "run_claude") as run:
        captured = {}

        def fake_run(_path, _alias, _system, _prompt, cwd, _effort):
            captured["cwd"] = cwd
            assert Path(cwd).is_dir()
            assert list(Path(cwd).iterdir()) == []
            return "pong", "claude-haiku-4-5"

        run.side_effect = fake_run
        text, provenance = cli_models.converse(
            "claude-haiku",
            [{"role": "user", "content": "Reply pong"}],
            enabled=True,
            effort="low",
        )
    assert text == "pong"
    assert provenance == {
        "runner": "Claude Code",
        "model": "claude-haiku-4-5",
        "effort": "low",
    }
    assert not Path(captured["cwd"]).exists()
    assert cli_models.provenance_footer(provenance) == (
        "*(via Claude Code · claude-haiku-4-5 · effort low)*"
    )

    with mock.patch.object(cli_models, "resolve_cli") as resolve:
        try:
            cli_models.converse(
                "claude-haiku", [{"role": "user", "content": "x"}], enabled=False
            )
        except cli_models.CLIModelError as exc:
            assert "switched off" in str(exc)
        else:
            raise AssertionError("disabled CLI models should fail")
        resolve.assert_not_called()


def test_cli_client_keeps_provenance_separate():
    client = cli_models.CLIClient("claude-haiku", "low")
    provenance = {"runner": "Claude Code", "model": "served", "effort": "low"}
    with mock.patch.object(cli_models, "converse", return_value=("clean", provenance)) as call:
        text, returned = client.complete(
            [{"role": "user", "content": "x"}], enabled=True
        )
    assert text == "clean"
    assert returned is provenance
    assert client.last_provenance is provenance
    assert call.call_args.kwargs["enabled"] is True


def main() -> int:
    tests = [
        test_resolve_and_wrap_windows_shims,
        test_flatten_messages_and_served_model,
        test_codex_discovery_default_and_fallback,
        test_detection_and_disabled_selection,
        test_effort_clamping,
        test_run_claude_command_and_parsing,
        test_run_claude_error_paths,
        test_run_codex_command_output_and_cleanup,
        test_run_codex_timeout_cleans_output,
        test_run_agy_command_and_log_verification,
        test_run_agy_rejects_silent_model_substitution,
        test_converse_routes_antigravity_models,
        test_converse_opt_in_neutral_cwd_and_provenance,
        test_cli_client_keeps_provenance_separate,
    ]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception as exc:
            failures += 1
            print(f"FAIL {test.__name__}: {exc}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
