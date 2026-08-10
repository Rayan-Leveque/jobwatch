"""Shared subprocess seam for the supported LLM runners."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

OPENCODE_TOOLS = (
    "bash", "read", "edit", "write", "patch", "glob", "grep", "list",
    "lsp", "skill", "task", "todowrite", "webfetch", "websearch",
)


class LLMRunnerError(RuntimeError):
    """The runner could not produce a successful response."""


def opencode_sandbox(tmp_dir: Path, allow: tuple[str, ...] = ()) -> dict[str, str]:
    permission = {"*": "deny"}
    permission.update({tool: "deny" for tool in OPENCODE_TOOLS if tool not in allow})
    permission.update({tool: "allow" for tool in allow})
    (tmp_dir / "opencode.json").write_text(
        json.dumps({"$schema": "https://opencode.ai/config.json", "permission": permission}),
        encoding="utf-8",
    )
    return {**os.environ, "OPENCODE_PERMISSION": json.dumps(permission)}


def run_codex(
    *,
    binary: str,
    model: str,
    prompt: str,
    attachment: str,
    timeout: float,
    variant: str | None = None,
    output_schema: dict | None = None,
) -> str:
    """Run read-only codex, returning its file-based final response."""
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            output_path = root / "response.txt"
            command = [
                binary, "exec", "--ignore-user-config",
                "--disable", "shell_tool", "--disable", "code_mode_host",
                "--disable", "apps", "--disable", "plugins", "--model", model,
                "-s", "read-only", "--skip-git-repo-check", "--ephemeral",
            ]
            if output_schema is not None:
                schema_path = root / "schema.json"
                schema_path.write_text(json.dumps(output_schema), encoding="utf-8")
                command += ["--output-schema", str(schema_path)]
            command += ["-o", str(output_path)]
            if variant:
                command += ["-c", f"model_reasoning_effort={variant}"]
            command.append(prompt)
            completed = subprocess.run(
                command, input=attachment, capture_output=True, text=True,
                timeout=timeout, check=False, cwd=tmp_dir,
            )
            if completed.returncode != 0:
                raise LLMRunnerError(f"codex exited with status {completed.returncode}")
            try:
                return output_path.read_text(encoding="utf-8")
            except OSError as exc:
                raise LLMRunnerError("codex did not write its final response") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LLMRunnerError(f"codex subprocess failed: {exc}") from exc


def run_opencode(
    *,
    binary: str,
    model: str,
    prompt: str,
    attachment: str,
    timeout: float,
    variant: str | None = None,
    pass_variant: bool = False,
    auto: bool = False,
    allow: tuple[str, ...] = (),
    attachment_name: str = "attachment.txt",
) -> str:
    """Run OpenCode with a file attachment, returning its JSON event stream."""
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            attachment_path = root / attachment_name
            attachment_path.write_text(attachment, encoding="utf-8")
            env = opencode_sandbox(root, allow=allow)
            command = [binary, "run", "--pure"]
            if auto:
                command.append("--auto")
            command += ["--model", model]
            if pass_variant and variant:
                command += ["--variant", variant]
            command += ["--format", "json", f"--file={attachment_path}", "--", prompt]
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=timeout,
                check=False, cwd=tmp_dir, env=env,
            )
            if completed.returncode != 0:
                raise LLMRunnerError(f"OpenCode exited with status {completed.returncode}")
            return completed.stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LLMRunnerError(f"OpenCode subprocess failed: {exc}") from exc
