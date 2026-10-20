#!/usr/bin/env node
// Poste d'envoi jobwatch (Mac) : réclame un dossier validé sur le serveur, laisse
// Claude Code remplir le formulaire dans une fenêtre Chrome visible au profil
// dédié (pour suivre l'agent en direct), puis rend le verdict. Lancé toutes les
// 2 minutes par launchd ; ne fait rien si aucun dossier n'attend. Ton Chrome
// habituel n'est jamais touché.
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
// Lu par l'icône de barre des menus (jobwatch-menubar.mjs).
const STATUS = join(SUPPORT, "status.json");
const HISTORY_SIZE = 10;
const LOGIN_PAGES = [
  "https://www.linkedin.com/login",
  "https://www.welcometothejungle.com/fr/signin",
  "https://accounts.google.com/",
];

// Codes HTTP d'une offre retirée : rien à remplir, le dossier revient à l'humain.
const GONE_STATUSES = new Set([404, 410]);

const log = (message) => console.log(`${new Date().toISOString()} ${message}`);

// Sans ces délais, une mise en veille en plein échange laisse SSH ouvert pour
// toujours : le passage ne finit jamais et launchd n'en relance pas d'autre.
const SSH_KEEPALIVE = ["-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4", "-o", "ConnectTimeout=15"];
const REMOTE_TIMEOUT_MS = 180_000;

function remote(command, input) {
  const [bin, args] = SSH.length
    ? [SSH[0], [...SSH.slice(1), ...SSH_KEEPALIVE, command]]
    : ["sh", ["-c", command]];
  const done = spawnSync(bin, args, {
    input, encoding: "utf8", maxBuffer: 64 * 1024 * 1024, timeout: REMOTE_TIMEOUT_MS,
  });
  if (done.error?.code === "ETIMEDOUT") throw new Error(`serveur muet : ${command}`);
  if (done.status !== 0) throw new Error(`serveur : ${command} (${(done.stderr || "").trim().slice(0, 300)})`);
  return done.stdout;
}

function browser(cwd, ...args) {
  return spawnSync(BROWSER, args, {
    cwd, encoding: "utf8", timeout: 120_000, env: { ...env, PLAYWRIGHT_CLI_SESSION: SESSION },
  });
}

function readStatus() {
  try {
    return JSON.parse(readFileSync(STATUS, "utf8"));
  } catch {
    return { history: [] };
  }
}

function saveStatus(changes) {
  const status = { ...readStatus(), ...changes, updated: new Date().toISOString() };
  mkdirSync(SUPPORT, { recursive: true, mode: 0o700 });
  writeFileSync(STATUS, JSON.stringify(status), { mode: 0o600 });
}

class OfferGone extends Error {
  constructor(status) {
    super(`offre expirée (HTTP ${status})`);
    this.status = status;
  }
}

async function httpStatus(url) {
  try {
    return (await fetch(url, { signal: AbortSignal.timeout(20_000) })).status;
  } catch {
    return null;
  }
}

// Une offre retirée s'affiche (code lu dans la sortie de `open`) ou fait échouer
// Chrome sans code (net::ERR_HTTP_RESPONSE_CODE_FAILURE, en headless surtout) :
// on le demande alors directement au site.
async function openBrowser(cwd, url, ...flags) {
  mkdirSync(PROFILE, { recursive: true, mode: 0o700 });
  const opened = browser(cwd, "open", url, "--browser=chrome", `--profile=${PROFILE}`, ...flags);
  if (opened.status === 0) {
    const status = Number(/^- HTTP status: (\d+)/m.exec(opened.stdout || "")?.[1]);
    if (GONE_STATUSES.has(status)) throw new OfferGone(status);
    return;
  }
  const status = await httpStatus(url);
  if (GONE_STATUSES.has(status)) throw new OfferGone(status);
  throw new Error(`Chrome ne s'ouvre pas : ${(opened.stderr || opened.stdout || "").trim().slice(0, 200)}`);
}

// `jw mail-code` pour l'agent : seule l'option --minutes passe, et seulement des chiffres.
function writeMailShim(binDir) {
  mkdirSync(binDir, { recursive: true });
  const ssh = [...SSH, ...SSH_KEEPALIVE].map((part) => `'${part}'`).join(" ");
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

async function login() {
  mkdirSync(WORK_ROOT, { recursive: true, mode: 0o700 });
  await openBrowser(WORK_ROOT, LOGIN_PAGES[0], "--headed");
  for (const page of LOGIN_PAGES.slice(1)) browser(WORK_ROOT, "tab-new", page);
  console.log("Connecte-toi dans la fenêtre Chrome qui vient de s'ouvrir (un onglet par site),");
  console.log("puis ferme cette fenêtre. Les sessions restent dans le profil dédié.");
}

async function main() {
  mkdirSync(WORK_ROOT, { recursive: true, mode: 0o700 });
  const claim = JSON.parse(remote(`${JW} submission-claim`) || "{}");
  saveStatus({ sent: claim.sent, error: null, current: null });
  if (!claim.job_id) return;
  if (claim.error) {
    log(`job ${claim.job_id} refusé par le serveur : ${claim.error}`);
    return;
  }
  await runJob(claim);
}

async function runJob(claim) {
  const workDir = join(WORK_ROOT, String(claim.job_id));
  const label = claim.label || `dossier ${claim.job_id}`;
  saveStatus({ current: { label, started: new Date().toISOString(), timeout: claim.timeout_seconds } });
  let report;
  try {
    // Dans le try : un disque plein est rapporté au serveur, qui alerte par ntfy.
    mkdirSync(workDir, { recursive: true, mode: 0o700 });
    for (const [name, content] of Object.entries(claim.files)) {
      writeFileSync(join(workDir, name), Buffer.from(content, "base64"), { mode: 0o600 });
    }
    const binDir = join(workDir, ".bin");
    writeMailShim(binDir);
    try {
      await openBrowser(workDir, claim.url, "--headed");
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
      // Même si la page n'a pas chargé, le navigateur est ouvert : on le ferme.
      browser(workDir, "close");
    }
  } catch (error) {
    report = error instanceof OfferGone ? { expired: error.status } : { error: String(error.message || error) };
  }
  const verdict = remote(`${JW} submission-finish ${claim.job_id}`, JSON.stringify(report)).trim();
  log(`job ${claim.job_id} : ${verdict}`);
  const [, status = "failed", reason = verdict] = /^(\w+) : ([\s\S]*)$/.exec(verdict) || [];
  const entry = { label, status, reason, at: new Date().toISOString() };
  const { history = [], sent = 0 } = readStatus();
  saveStatus({
    current: null,
    sent: sent + (status === "submitted" ? 1 : 0),
    history: [entry, ...history].slice(0, HISTORY_SIZE),
  });
}

try {
  if (process.argv[2] === "login") await login();
  else await main();
} catch (error) {
  log(`échec : ${error.message || error}`);
  if (process.argv[2] !== "login") saveStatus({ current: null, error: String(error.message || error) });
  process.exitCode = 1;
}
