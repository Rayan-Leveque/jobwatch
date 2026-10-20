"""Focused tests for the shared LLM subprocess runners."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from jobwatch.llm_runner import LLMRunnerError, run_codex, run_pi


def test_run_pi_is_ephemeral_toolless_and_attaches_untrusted_text(monkeypatch) -> None:
    captured: dict[str, object] = {}
    announcement = "--help\n$(touch /tmp/never-run)\n" + ("offre très longue\n" * 20)

    def fake_run(command, **kwargs):
        captured["command"] = list(command)
        captured["kwargs"] = kwargs
        captured["cwd_is_dir"] = Path(kwargs["cwd"]).is_dir()
        attachment_arg = next(arg for arg in command if arg.startswith("@"))
        captured["attachment"] = Path(attachment_arg[1:]).read_text(encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="résumé\n", stderr="")

    monkeypatch.setattr("jobwatch.llm_runner.subprocess.run", fake_run)

    result = run_pi(
        binary="/opt/pi/bin/pi",
        model="openai-codex/gpt-5.6-luna",
        prompt="Résume l'annonce.",
        attachment=announcement,
        timeout=42,
        thinking="max",
    )

    assert result == "résumé\n"
    command = captured["command"]
    assert command[0] == "/opt/pi/bin/pi"
    assert "--print" in command
    assert "--no-session" in command
    assert "--no-tools" in command
    assert command[command.index("--model") + 1] == "openai-codex/gpt-5.6-luna"
    assert command[command.index("--thinking") + 1] == "max"
    assert command[-1] == "Résume l'annonce."
    assert announcement not in command
    assert captured["attachment"] == announcement
    kwargs = captured["kwargs"]
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["timeout"] == 42
    assert kwargs["check"] is False
    assert captured["cwd_is_dir"] is True


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "message"),
    [
        (7, "", "échec fournisseur", "code 7.*échec fournisseur"),
        (0, " \n", "", "réponse vide"),
    ],
)
def test_run_pi_rejects_exit_failure_and_empty_output(
    monkeypatch, returncode: int, stdout: str, stderr: str, message: str
) -> None:
    monkeypatch.setattr(
        "jobwatch.llm_runner.subprocess.run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, returncode, stdout=stdout, stderr=stderr
        ),
    )

    with pytest.raises(LLMRunnerError, match=message):
        run_pi(
            binary="pi",
            model="model",
            prompt="prompt",
            attachment="annonce",
            timeout=3,
        )


def test_run_pi_wraps_timeout(monkeypatch) -> None:
    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr("jobwatch.llm_runner.subprocess.run", timeout)

    with pytest.raises(LLMRunnerError, match="appel pi échoué"):
        run_pi(
            binary="pi",
            model="model",
            prompt="prompt",
            attachment="annonce",
            timeout=3,
        )


def test_run_codex_switches_account_when_quota_is_used_up(tmp_path: Path) -> None:
    """Faux codex : le compte principal a épuisé son quota, celui de secours répond."""
    spare = tmp_path / "codex-spare"
    calls = tmp_path / "homes.txt"
    codex = tmp_path / "codex"
    codex.write_text(
        "#!/bin/sh\n"
        f'echo "${{CODEX_HOME:-principal}}" >> {calls}\n'
        f'if [ "$CODEX_HOME" != "{spare}" ]; then\n'
        "  echo \"ERROR: You've hit your usage limit. Upgrade to Pro\" >&2; exit 1\n"
        "fi\n"
        'while [ "$1" != "-o" ]; do shift; done\n'
        'echo "lettre" > "$2"\n'
    )
    codex.chmod(0o755)

    text = run_codex(binary=str(codex), model="m", prompt="p", attachment="a", timeout=30,
                     fallback_homes=[spare])
    assert text.strip() == "lettre"
    assert calls.read_text().split() == ["principal", str(spare)]
    # Sans compte de secours, l'échec reste visible.
    with pytest.raises(LLMRunnerError):
        run_codex(binary=str(codex), model="m", prompt="p", attachment="a", timeout=30)
