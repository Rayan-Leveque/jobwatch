#!/usr/bin/env node
// Poste d'envoi jobwatch (Mac) : réclame un dossier validé sur le serveur, laisse
// Claude Code remplir le formulaire dans le Chrome habituel via l'extension
// Playwright, puis rend le verdict. Lancé toutes les 2 minutes par launchd ; ne
// fait rien si Chrome est fermé ou si aucun dossier n'attend.
//
// Variables (fichier ~/.config/jobwatch-worker/env, lu par install.sh) :
//   JOBWATCH_SSH   commande d'accès au serveur ("" = exécution locale, tests)
//   JOBWATCH_JW    commande jw sur le serveur
//   CLAUDE_BIN, PLAYWRIGHT_CLI, JOBWATCH_ATTACH, JOBWATCH_WORK_ROOT
//   PLAYWRIGHT_MCP_EXTENSION_TOKEN  jeton de l'extension (pas de fenêtre d'accord)

import { spawnSync } from "node:child_process";
import { chmodSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

const env = process.env;
const SSH = (env.JOBWATCH_SSH ?? "ssh -p 2222 rayan@100.116.111.35").split(" ").filter(Boolean);
const JW = env.JOBWATCH_JW ?? "/home/rayan/jobwatch-prod/.venv/bin/jw --instance rayan";
const CLAUDE = env.CLAUDE_BIN ?? "claude";
const BROWSER = env.PLAYWRIGHT_CLI ?? "playwright-cli";
const ATTACH = (env.JOBWATCH_ATTACH ?? "--extension=chrome").split(" ").filter(Boolean);
const WORK_ROOT = env.JOBWATCH_WORK_ROOT ?? join(homedir(), "Library", "Caches", "jobwatch-worker");
const SESSION = "jobwatch-mac";

const log = (message) => console.log(`${new Date().toISOString()} ${message}`);

function remote(command, input) {
  const [bin, args] = SSH.length ? [SSH[0], [...SSH.slice(1), command]] : ["sh", ["-c", command]];
  const done = spawnSync(bin, args, { input, encoding: "utf8", maxBuffer: 64 * 1024 * 1024 });
  if (done.status !== 0) throw new Error(`serveur : ${command} (${(done.stderr || "").trim().slice(0, 300)})`);
  return done.stdout;
}

function browser(cwd, ...args) {
  return spawnSync(BROWSER, args, {
    cwd, encoding: "utf8", timeout: 120_000, env: { ...env, PLAYWRIGHT_CLI_SESSION: SESSION },
  });
}

// `jw mail-code` pour l'agent : seule l'option --minutes passe, et seulement des chiffres.
function writeMailShim(binDir) {
  mkdirSync(binDir, { recursive: true });
  const ssh = SSH.map((part) => `'${part}'`).join(" ");
  const run = SSH.length ? `${ssh} "${JW} mail-code --minutes $MINUTES"` : `${JW} mail-code --minutes $MINUTES`;
  const shim = `#!/bin/sh
[ "$1" = "mail-code" ] || { echo "seul jw mail-code est disponible" >&2; exit 1; }
MINUTES=15
if [ "$2" = "--minutes" ]; then
  case "$3" in ''|*[!0-9]*) echo "--minutes attend un entier" >&2; exit 1 ;; esac
  MINUTES=$3
fi
exec ${run}
`;
  writeFileSync(join(binDir, "jw"), shim);
  chmodSync(join(binDir, "jw"), 0o700);
}

function main() {
  mkdirSync(WORK_ROOT, { recursive: true, mode: 0o700 });
  // Chrome d'abord : sans navigateur joignable, on ne réserve aucun dossier.
  const attach = browser(WORK_ROOT, "attach", ...ATTACH);
  if (attach.status !== 0) {
    log(`Chrome injoignable, rien à faire (${(attach.stderr || attach.stdout || "").trim().slice(0, 200)})`);
    return;
  }
  try {
    const claim = JSON.parse(remote(`${JW} submission-claim`) || "{}");
    if (!claim.job_id) return;
    if (claim.error) {
      log(`job ${claim.job_id} refusé par le serveur : ${claim.error}`);
      return;
    }
    runJob(claim);
  } finally {
    browser(WORK_ROOT, "detach");
  }
}

function runJob(claim) {
  const workDir = join(WORK_ROOT, String(claim.job_id));
  mkdirSync(workDir, { recursive: true, mode: 0o700 });
  let report;
  try {
    for (const [name, content] of Object.entries(claim.files)) {
      writeFileSync(join(workDir, name), Buffer.from(content, "base64"), { mode: 0o600 });
    }
    const binDir = join(workDir, ".bin");
    writeMailShim(binDir);
    const opened = browser(workDir, "tab-new", claim.url);
    if (opened.status !== 0) throw new Error(`ouverture de l'offre impossible : ${(opened.stderr || "").trim().slice(0, 200)}`);
    log(`job ${claim.job_id} : agent lancé sur ${claim.url}`);
    const agent = spawnSync(CLAUDE, claim.args, {
      cwd: workDir,
      encoding: "utf8",
      timeout: claim.timeout_seconds * 1000,
      maxBuffer: 64 * 1024 * 1024,
      env: { ...env, PATH: `${binDir}:${env.PATH}`, PLAYWRIGHT_CLI_SESSION: SESSION },
    });
    // Délai dépassé : le formulaire a pu partir à moitié, le serveur rend la main.
    if (agent.error?.code === "ETIMEDOUT") {
      report = { timeout: true };
      return finish(claim, report);
    }
    if (agent.error) throw agent.error;
    const proof = join(workDir, "preuve.png");
    report = {
      returncode: agent.status ?? 1,
      stdout: agent.stdout,
      stderr: agent.stderr,
      proof_b64: existsSync(proof) ? readFileSync(proof).toString("base64") : null,
    };
  } catch (error) {
    report = { error: String(error.message || error) };
  }
  finish(claim, report);
}

function finish(claim, report) {
  log(`job ${claim.job_id} : ${remote(`${JW} submission-finish ${claim.job_id}`, JSON.stringify(report)).trim()}`);
}

try {
  main();
} catch (error) {
  log(`échec : ${error.message || error}`);
  process.exitCode = 1;
}
