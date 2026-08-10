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
    """Refuse à OpenCode tout outil hors `allow`, et renvoie l'environnement à utiliser.

    Le texte fourni au modèle vient d'une page tierce. `--pure` ne coupe que les
    plugins externes : la configuration globale de l'utilisateur reste fusionnée
    avec le fichier de projet écrit ici, et une permission nommée qu'elle
    accorderait l'emporterait sur un simple `*`. Chaque outil connu est donc
    refusé nommément, dans le fichier et dans OPENCODE_PERMISSION, que OpenCode
    applique après tous les fichiers de configuration.
    """
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
    """Run read-only codex, returning its file-based final response.

    L'attache passe par stdin (bloc <stdin> côté codex) et la réponse finale
    est écrite par codex dans un fichier (-o) : pas de limite d'argument ni
    de parsing de log. Les appelants passent du contenu tiers non fiable
    (annonce, CV, candidats de recherche) : lecture seule, config
    utilisateur ignorée et outils désactivés.
    """
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
                raise LLMRunnerError(f"codex a quitté avec le code {completed.returncode}")
            try:
                return output_path.read_text(encoding="utf-8")
            except OSError as exc:
                raise LLMRunnerError("codex n'a pas écrit sa réponse finale") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LLMRunnerError(f"appel codex échoué : {exc}") from exc


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
    """Run OpenCode with a file attachment, returning its JSON event stream.

    La pièce jointe est écrite dans un fichier et passée via --file plutôt
    qu'en argument CLI : le noyau limite chaque argument à ~128 Ko
    (MAX_ARG_STRLEN), qu'une offre, un CV ou des candidats de recherche
    peuvent dépasser.
    """
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
                raise LLMRunnerError(f"OpenCode a quitté avec le code {completed.returncode}")
            return completed.stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LLMRunnerError(f"appel OpenCode échoué : {exc}") from exc
