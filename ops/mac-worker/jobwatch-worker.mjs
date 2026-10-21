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
//   JOBWATCH_LANE   voie d'envoi (1 par défaut) : chaque voie a son Chrome, son
//                   profil et sa session, et toutes puisent dans la même file
//   CODEX_BIN, JOBWATCH_CODEX_FALLBACK_HOMES (comptes ChatGPT de secours)

import { spawnSync } from "node:child_process";
import {
  chmodSync, cpSync, existsSync, mkdirSync, readFileSync, readdirSync, realpathSync, rmSync, symlinkSync,
  writeFileSync,
} from "node:fs";
import { homedir, tmpdir } from "node:os";
import { delimiter, dirname, isAbsolute, join } from "node:path";

const env = process.env;
const SSH = (env.JOBWATCH_SSH ?? "ssh -p 2222 rayan@100.116.111.35").split(" ").filter(Boolean);
const JW = env.JOBWATCH_JW ?? "/home/rayan/jobwatch-prod/.venv/bin/jw --instance rayan";
const CLAUDE = env.CLAUDE_BIN ?? "claude";
const CODEX = env.CODEX_BIN ?? "codex";
const BROWSER = env.PLAYWRIGHT_CLI ?? "playwright-cli";
const SUPPORT = join(homedir(), "Library", "Application Support", "jobwatch-worker");
const LANE = env.JOBWATCH_LANE || "1";
const LANE_SUFFIX = LANE === "1" ? "" : `-${LANE}`;
const LANE_ONE_PROFILE = join(SUPPORT, "chrome-profile");
const PROFILE = env.JOBWATCH_PROFILE ?? join(SUPPORT, `chrome-profile${LANE_SUFFIX}`);
const WORK_ROOT = env.JOBWATCH_WORK_ROOT ?? join(homedir(), "Library", "Caches", "jobwatch-worker");
const SESSION = `jobwatch-mac${LANE_SUFFIX}`;
// Codex : configuration dédiée (ni AGENTS.md, ni hooks, ni MCP de l'utilisateur)
// et clé SSH qui ne sert qu'à `jw mail-code` (commande forcée côté serveur).
const CODEX_HOME = join(SUPPORT, `codex-home${LANE_SUFFIX}`);
// Comptes ChatGPT essayés dans l'ordre quand le quota du précédent est épuisé
// (JOBWATCH_CODEX_FALLBACK_HOMES : CODEX_HOME séparés par « : »).
const CODEX_ACCOUNTS = [
  join(homedir(), ".codex"),
  ...(env.JOBWATCH_CODEX_FALLBACK_HOMES || "").split(":").filter(Boolean),
];
const CODEX_USAGE_LIMIT = "hit your usage limit";
// Tous les comptes épuisés : les dossiers restent en file jusqu'à la reprise
// annoncée par Codex (« try again at 2:43 AM »), à défaut 15 minutes.
const QUOTA = join(SUPPORT, "codex-quota");
const QUOTA_RETRY_MS = 15 * 60_000;
const MAILCODE_KEY = join(SUPPORT, "mailcode", "id_ed25519");
const MAILCODE_KNOWN_HOSTS = join(SUPPORT, "mailcode", "known_hosts");
// Interdites à Codex : de quoi parler au navigateur ou au réseau sans le filtre.
const CODEX_FORBIDDEN = [
  "node", "npx", "npm", "python", "python3", "curl", "wget", "ssh", "scp", "osascript", "open",
];
// Lu par l'icône de barre des menus (jobwatch-menubar.mjs).
const STATUS = join(SUPPORT, `status${LANE_SUFFIX}.json`);
// Présent : toutes les voies finissent leur dossier puis s'arrêtent (menu
// « Mettre en pause » de la barre des menus, ou avant clone-profile).
const PAUSE = join(SUPPORT, "pause");
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

// Si la session playwright-cli meurt, son Chrome reste ouvert et verrouille le
// profil : tous les envois suivants échoueraient (« Browser is already in use »).
// Seul le worker utilise ce profil, un passage à la fois.
function killProfileChrome() {
  // Fin de motif explicite : chrome-profile ne doit pas toucher chrome-profile-2.
  spawnSync("pkill", ["-f", "--", `--user-data-dir=${PROFILE}( |$)`]);
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

const quote = (part) => `'${part}'`;

// `jw mail-code` pour l'agent : seule l'option --minutes passe, et seulement des chiffres.
// Codex ne lit pas ~/.ssh : il passe par la clé dédiée, dont le serveur force la commande.
function writeMailShim(binDir, codex) {
  mkdirSync(binDir, { recursive: true });
  const dedicated = [
    "-F", "/dev/null", "-i", MAILCODE_KEY, "-o", "IdentitiesOnly=yes",
    // Guillemets : ssh coupe la valeur aux espaces (« Application Support »).
    "-o", `UserKnownHostsFile="${MAILCODE_KNOWN_HOSTS}"`, "-o", "BatchMode=yes",
  ];
  const ssh = [SSH[0], ...(codex ? dedicated : []), ...SSH.slice(1), ...SSH_KEEPALIVE].map(quote).join(" ");
  const command = codex ? "mail-code --minutes $MINUTES" : `${JW} mail-code --minutes $MINUTES`;
  const run = SSH.length ? `${ssh} "${command}"` : `${JW} mail-code --minutes $MINUTES`;
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

// Chemin réel d'un outil : le filtre playwright-cli du dossier porte le même nom
// et passe devant dans le PATH de l'agent.
function whichBinary(name) {
  if (isAbsolute(name)) return name;
  for (const dir of (env.PATH || "").split(delimiter)) {
    if (dir && existsSync(join(dir, name))) return join(dir, name);
  }
  throw new Error(`${name} introuvable dans le PATH`);
}

const realBinary = (name) => realpathSync(whichBinary(name));

// Filtre de playwright-cli pour Codex, qui n'a pas de liste d'outils comme Claude :
// ni cookies ni stockage, upload limité au dossier de travail.
function writeBrowserShim(binDir, real, denied) {
  const shim = `#!/bin/sh
case "$1" in
${denied.map((name) => `  ${name}*) echo "playwright-cli $1 : commande refusée" >&2; exit 1 ;;`).join("\n")}
esac
if [ "$1" = "upload" ]; then
  for file in "$@"; do
    case "$file" in /*|"~"*|*..*) echo "upload hors du dossier refusé : $file" >&2; exit 1 ;; esac
  done
fi
exec ${quote(real)} "$@"
`;
  writeFileSync(join(binDir, "playwright-cli"), shim);
  chmodSync(join(binDir, "playwright-cli"), 0o700);
}

// Dossier du socket de la session playwright-cli, à ouvrir dans le bac à sable.
function sessionSocketDir() {
  const tmp = realpathSync(tmpdir());
  for (const name of readdirSync(tmp).filter((entry) => entry.startsWith("pw-"))) {
    const dir = join(tmp, name, "cli");
    if (existsSync(dir) && readdirSync(dir).some((file) => file.endsWith(`-${SESSION}.sock`))) return dir;
  }
  throw new Error("socket de la session playwright-cli introuvable");
}

// Bac à sable de Codex : écriture dans le seul dossier de travail, lecture des
// outils (node, playwright-cli, ssh) et de la session du navigateur, rien d'autre
// du dossier personnel (~/.ssh, documents, profil Chrome).
function writeCodexHome(real, account) {
  rmSync(CODEX_HOME, { recursive: true, force: true });
  mkdirSync(join(CODEX_HOME, "rules"), { recursive: true, mode: 0o700 });
  symlinkSync(join(account, "auth.json"), join(CODEX_HOME, "auth.json"));
  const socketDir = sessionSocketDir();
  const readable = [
    "/opt/homebrew",
    realpathSync(join(dirname(process.execPath), "..")),
    dirname(dirname(real)),
    join(homedir(), "Library", "Caches", "ms-playwright", "daemon"),
    // Codex relance son propre binaire dans le bac à sable.
    dirname(dirname(realBinary(CODEX))),
    dirname(MAILCODE_KEY),
    socketDir,
  ];
  const toml = (path) => JSON.stringify(path);
  writeFileSync(join(CODEX_HOME, "config.toml"), `default_permissions = "jobwatch"

[permissions.jobwatch.filesystem]
":minimal" = "read"
":workspace_roots" = "write"
${[...new Set(readable)].map((path) => `${toml(path)} = "read"`).join("\n")}

[permissions.jobwatch.network]
enabled = true

[permissions.jobwatch.network.unix_sockets]
${toml(socketDir)} = "allow"
`);
  writeFileSync(join(CODEX_HOME, "rules", "jobwatch.rules"), `prefix_rule(
    pattern = [${JSON.stringify(CODEX_FORBIDDEN)}],
    decision = "forbidden",
    justification = "seuls playwright-cli et jw mail-code servent à candidater",
)
prefix_rule(
    pattern = [${JSON.stringify([...new Set([whichBinary(BROWSER), real])])}],
    decision = "forbidden",
    justification = "passer par playwright-cli, le filtre du dossier",
)
`);
}

// Profil d'une voie supplémentaire : copie du profil connecté de la voie 1, sans
// ses caches ni ses verrous. Chrome de la voie 1 fermé, sinon les cookies (SQLite)
// peuvent être copiés en pleine écriture.
const PROFILE_SKIP = new Set(["Cache", "Code Cache", "GPUCache", "DawnCache", "ShaderCache", "GrShaderCache"]);
function cloneProfile() {
  if (PROFILE === LANE_ONE_PROFILE) throw new Error("clone-profile se lance pour une voie 2 ou plus (JOBWATCH_LANE)");
  const busy = spawnSync("pgrep", ["-f", "--", `--user-data-dir=${LANE_ONE_PROFILE}( |$)`]);
  if (busy.status === 0) throw new Error("Chrome de la voie 1 ouvert : réessaie entre deux envois");
  rmSync(PROFILE, { recursive: true, force: true });
  cpSync(LANE_ONE_PROFILE, PROFILE, {
    recursive: true,
    filter: (source) => !PROFILE_SKIP.has(source.split("/").pop()) && !source.split("/").pop().startsWith("Singleton"),
  });
  console.log(`profil de la voie ${LANE} copié depuis la voie 1`);
}

async function login() {
  mkdirSync(WORK_ROOT, { recursive: true, mode: 0o700 });
  await openBrowser(WORK_ROOT, LOGIN_PAGES[0], "--headed");
  for (const page of LOGIN_PAGES.slice(1)) browser(WORK_ROOT, "tab-new", page);
  console.log("Connecte-toi dans la fenêtre Chrome qui vient de s'ouvrir (un onglet par site),");
  console.log("puis ferme cette fenêtre. Les sessions restent dans le profil dédié.");
}

// Vide la file d'un trait : sans boucle, chaque dossier attendait le passage
// launchd suivant (jusqu'à 2 minutes perdues entre deux envois).
async function main() {
  // Voie sans profil connecté : elle candidaterait déconnectée de LinkedIn ou WTTJ.
  if (LANE !== "1" && !existsSync(PROFILE)) {
    log(`voie ${LANE} : pas de profil Chrome, lance « run.sh clone-profile » avec JOBWATCH_LANE=${LANE}`);
    return;
  }
  mkdirSync(WORK_ROOT, { recursive: true, mode: 0o700 });
  for (;;) {
    if (existsSync(PAUSE)) {
      saveStatus({ current: null, error: null, paused: true });
      return;
    }
    const quotaUntil = existsSync(QUOTA) ? Date.parse(readFileSync(QUOTA, "utf8")) : NaN;
    if (quotaUntil > Date.now()) {
      saveStatus({ current: null, error: null, paused: false, quotaUntil: new Date(quotaUntil).toISOString() });
      return;
    }
    const claim = JSON.parse(remote(`${JW} submission-claim`) || "{}");
    saveStatus({ sent: claim.sent, error: null, current: null, paused: false, quotaUntil: null });
    if (!claim.job_id) return;
    if (claim.error) log(`job ${claim.job_id} refusé par le serveur : ${claim.error}`);
    else await runJob(claim);
  }
}

// Heure de reprise annoncée par Codex, sinon dans 15 minutes.
function quotaRetry(output) {
  const retry = new Date(Date.now() + QUOTA_RETRY_MS);
  const [, h, m, half] = /try again at (\d{1,2}):(\d{2}) ?([AP]M)?/i.exec(output) || [];
  if (!h) return retry;
  const hour = half ? (Number(h) % 12) + (half.toUpperCase() === "PM" ? 12 : 0) : Number(h);
  const at = new Date();
  at.setHours(hour, Number(m), 0, 0);
  if (at <= Date.now()) at.setDate(at.getDate() + 1);
  return at;
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
    const codex = claim.agent === "codex";
    writeMailShim(binDir, codex);
    killProfileChrome();
    try {
      await openBrowser(workDir, claim.url, "--headed");
      const agentEnv = { ...env, PATH: `${binDir}:${env.PATH}`, PLAYWRIGHT_CLI_SESSION: SESSION };
      const real = codex ? realBinary(BROWSER) : null;
      if (codex) {
        writeBrowserShim(binDir, real, claim.denied_browser);
        agentEnv.CODEX_HOME = CODEX_HOME;
      }
      let agent;
      for (const account of codex ? CODEX_ACCOUNTS : [null]) {
        if (codex) writeCodexHome(real, account);
        log(`job ${claim.job_id} : agent ${codex ? `codex (${account})` : "claude"} lancé sur ${claim.url}`);
        // Codex par son vrai chemin : il s'y relance, et le bac à sable refuse le lien.
        agent = spawnSync(codex ? realBinary(CODEX) : CLAUDE, claim.args, {
          cwd: workDir,
          encoding: "utf8",
          timeout: claim.timeout_seconds * 1000,
          maxBuffer: 64 * 1024 * 1024,
          // Codex lit stdin quand il est ouvert : rien à lui donner.
          stdio: ["ignore", "pipe", "pipe"],
          env: agentEnv,
        });
        // Quota épuisé : le compte suivant reprend le dossier depuis le début.
        if (!`${agent.stdout}${agent.stderr}`.includes(CODEX_USAGE_LIMIT)) break;
      }
      const output = `${agent.stdout}${agent.stderr}`;
      // Délai dépassé : le formulaire a pu partir à moitié, le serveur rend la main.
      if (agent.error?.code === "ETIMEDOUT") {
        report = { timeout: true };
      } else if (codex && output.includes(CODEX_USAGE_LIMIT)) {
        // Rien n'est parti : le serveur remet le dossier en file.
        writeFileSync(QUOTA, quotaRetry(output).toISOString());
        report = { quota: true };
      } else if (agent.error) {
        throw agent.error;
      } else {
        const proof = join(workDir, "preuve.png");
        // Codex écrit sa réponse finale (l'objet du schéma) dans un fichier.
        const result = join(workDir, "agent_result.json");
        report = {
          returncode: agent.status ?? 1,
          stdout: codex && existsSync(result) ? readFileSync(result, "utf8") : agent.stdout,
          stderr: agent.stderr,
          proof_b64: existsSync(proof) ? readFileSync(proof).toString("base64") : null,
        };
      }
    } finally {
      // Même si la page n'a pas chargé, le navigateur est ouvert : on le ferme.
      browser(workDir, "close");
      killProfileChrome();
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
  else if (process.argv[2] === "clone-profile") cloneProfile();
  else await main();
} catch (error) {
  log(`échec : ${error.message || error}`);
  if (!process.argv[2]) saveStatus({ current: null, error: String(error.message || error) });
  process.exitCode = 1;
}
