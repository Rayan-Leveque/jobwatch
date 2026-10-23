#!/usr/bin/env node
// Icône de barre des menus du poste d'envoi (plugin SwiftBar, rafraîchi toutes
// les 30 s) : lit le status*.json de chaque voie, écrit par jobwatch-worker.mjs.
//   en veille   avion en papier + nombre de candidatures envoyées
//   en cours    avion plein + entreprise
//   en panne    triangle rouge : dernier passage en échec ou trop ancien

import { existsSync, readFileSync, readdirSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

const SUPPORT = join(homedir(), "Library", "Application Support", "jobwatch-worker");
const LOG = join(homedir(), "Library", "Logs", "jobwatch-worker.log");
// launchd lance un passage toutes les 2 minutes : au-delà, le poste ne tourne plus.
const STALE_MINUTES = 10;
const MARKS = { submitted: "✓", needs_human: "⏸", failed: "✗" };

const minutesSince = (iso) => Math.round((Date.now() - Date.parse(iso)) / 60_000);
const clean = (text) => String(text).replace(/[|\n]+/g, " ").trim();
const hour = (iso) => new Date(iso).toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });

// Une voie d'envoi par fichier : status.json (voie 1), status-2.json, etc.
const lanes = [];
try {
  for (const name of readdirSync(SUPPORT).filter((file) => /^status(-\d+)?\.json$/.test(file)).sort()) {
    try {
      lanes.push(JSON.parse(readFileSync(join(SUPPORT, name), "utf8")));
    } catch {
      // Fichier en cours d'écriture : relu au prochain rafraîchissement.
    }
  }
} catch {
  // Dossier absent : aucun passage encore.
}

const PAUSE = join(SUPPORT, "pause");
const paused = existsSync(PAUSE);
const lines = [];
const problems = [];
if (!lanes.length) problems.push("aucun passage enregistré");
for (const lane of lanes) {
  const current = lane.current;
  const age = minutesSince(lane.updated);
  if (current && minutesSince(current.started) > (current.timeout ?? 1800) / 60 + STALE_MINUTES) {
    problems.push(`bloqué sur ${current.label}`);
  } else if (lane.error) problems.push(lane.error);
  else if (!current && !paused && age > STALE_MINUTES) problems.push(`aucun passage depuis ${age} min`);
}
const running = lanes.map((lane) => lane.current).filter(Boolean);
const quotaUntil = lanes.map((lane) => lane.quotaUntil).find((at) => at && Date.parse(at) > Date.now());
const sent = Math.max(0, ...lanes.map((lane) => lane.sent ?? 0));
const age = lanes.length ? Math.min(...lanes.map((lane) => minutesSince(lane.updated))) : null;

if (problems.length) lines.push("| sfimage=exclamationmark.triangle.fill sfcolor=#d33");
else if (running.length) {
  const more = running.length > 1 ? ` +${running.length - 1}` : "";
  lines.push(`${clean(running[0].label.split(" - ")[0])}${more} | sfimage=paperplane.fill length=22`);
} else if (quotaUntil && !paused) lines.push(`${sent} | sfimage=hourglass`);
else lines.push(`${sent} | sfimage=${paused ? "pause.circle" : "paperplane"}`);
lines.push("---");

for (const problem of problems) lines.push(`En panne : ${clean(problem)} | length=70 color=#d33`);
for (const current of running) {
  lines.push(`En cours : ${clean(current.label)} (${minutesSince(current.started)} min) | length=70`);
}
if (paused) lines.push(running.length ? "En pause après les envois en cours" : "En pause");
else if (quotaUntil) lines.push(`Quota Codex épuisé, reprise vers ${hour(quotaUntil)}`);
else if (!running.length && !problems.length) lines.push("En veille, aucun dossier en attente");
if (lanes.length) {
  lines.push(`Dernier passage : il y a ${age} min`);
  lines.push(`${sent} candidatures envoyées`);
}

const history = lanes
  .flatMap((lane) => lane.history ?? [])
  .sort((a, b) => Date.parse(b.at) - Date.parse(a.at))
  .slice(0, 10);
if (history.length) lines.push("---");
for (const entry of history) {
  lines.push(`${MARKS[entry.status] ?? "•"} ${clean(entry.label)}  ${hour(entry.at)} | length=60`);
  lines.push(`--${clean(entry.reason)} | length=90`);
}

lines.push("---");
lines.push(paused
  ? `Reprendre les envois | bash=/bin/rm param1="${PAUSE}" terminal=false refresh=true`
  : `Mettre en pause | bash=/usr/bin/touch param1="${PAUSE}" terminal=false refresh=true`);
lines.push(`Ouvrir le journal | bash=/usr/bin/open param1=-a param2=Console param3="${LOG}" terminal=false`);
console.log(lines.join("\n"));
