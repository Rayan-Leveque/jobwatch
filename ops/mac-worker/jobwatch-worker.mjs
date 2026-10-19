#!/usr/bin/env node
// Poste d'envoi jobwatch (Mac) : réclame un dossier validé sur le serveur, laisse
// Claude Code remplir le formulaire dans un Chrome headless au profil dédié, puis
// rend le verdict. Lancé toutes les 2 minutes par launchd ; ne fait rien si
// aucun dossier n'attend. Ton Chrome habituel n'est jamais touché.
//
//   node jobwatch-worker.mjs         un passage (launchd)
//   node jobwatch-worker.mjs login   ouvre le profil dédié en fenêtre visible
//                                    pour s'y connecter (LinkedIn, WTTJ, Google)
//
// Variables (fichier ~/.config/jobwatch-worker/env, écrit par install.sh) :
//   JOBWATCH_SSH   commande d'accès au serveur ("" = exécution locale, tests)
//   JOBWATCH_JW    commande jw sur le serveur
//   CLAUDE_BIN, PLAYWRIGHT_CLI, JOBWATCH_PROFILE, JOBWATCH_WORK_ROOT

import { spawnSync } from "node:child_process";
import { chmodSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

const env = process.env;
const SSH = (env.JOBWATCH_SSH ?? "ssh -p 2222 rayan@100.116.111.35").split(" ").filter(Boolean);
const JW = env.JOBWATCH_JW ?? "/home/rayan/jobwatch-prod/.venv/bin/jw --instance rayan";
const CLAUDE = env.CLAUDE_BIN ?? "claude";
const BROWSER = env.PLAYWRIGHT_CLI ?? "playwright-cli";
const SUPPORT = join(homedir(), "Library", "Application Support", "jobwatch-worker");
const PROFILE = env.JOBWATCH_PROFILE ?? join(SUPPORT, "chrome-profile");
const WORK_ROOT = env.JOBWATCH_WORK_ROOT ?? join(homedir(), "Library", "Caches", "jobwatch-worker");
const SESSION = "jobwatch-mac";
const LOGIN_PAGES = [
  "https://www.linkedin.com/login",
  "https://www.welcometothejungle.com/fr/signin",
  "https://accounts.google.com/",
];

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

function openBrowser(cwd, url, ...flags) {
  mkdirSync(PROFILE, { recursive: true, mode: 0o700 });
  const opened = browser(cwd, "open", url, "--browser=chrome", `--profile=${PROFILE}`, ...flags);
  if (opened.status !== 0) {
    throw new Error(`Chrome ne s'ouvre pas : ${(opened.stderr || opened.stdout || "").trim().slice(0, 200)}`);
  }
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

function login() {
  mkdirSync(WORK_ROOT, { recursive: true, mode: 0o700 });
  openBrowser(WORK_ROOT, LOGIN_PAGES[0], "--headed");
  for (const page of LOGIN_PAGES.slice(1)) browser(WORK_ROOT, "tab-new", page);
  console.log("Connecte-toi dans la fenêtre Chrome qui vient de s'ouvrir (un onglet par site),");
  console.log("puis ferme cette fenêtre. Les sessions restent dans le profil dédié.");
}

function main() {
  mkdirSync(WORK_ROOT, { recursive: true, mode: 0o700 });
  const claim = JSON.parse(remote(`${JW} submission-claim`) || "{}");
  if (!claim.job_id) return;
  if (claim.error) {
    log(`job ${claim.job_id} refusé par le serveur : ${claim.error}`);
    return;
  }
  runJob(claim);
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
    openBrowser(workDir, claim.url);
    try {
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
      } else if (agent.error) {
        throw agent.error;
      } else {
        const proof = join(workDir, "preuve.png");
        report = {
          returncode: agent.status ?? 1,
          stdout: agent.stdout,
          stderr: agent.stderr,
          proof_b64: existsSync(proof) ? readFileSync(proof).toString("base64") : null,
        };
      }
    } finally {
      browser(workDir, "close");
    }
  } catch (error) {
    report = { error: String(error.message || error) };
  }
  log(`job ${claim.job_id} : ${remote(`${JW} submission-finish ${claim.job_id}`, JSON.stringify(report)).trim()}`);
}

try {
  if (process.argv[2] === "login") login();
  else main();
} catch (error) {
  log(`échec : ${error.message || error}`);
  process.exitCode = 1;
}
