#!/usr/bin/env node
// Icône de barre des menus du poste d'envoi (plugin SwiftBar, rafraîchi toutes
// les 30 s) : lit status.json, écrit par jobwatch-worker.mjs à chaque passage.
//   en veille   avion en papier + nombre de candidatures envoyées
//   en cours    avion plein + entreprise
//   en panne    triangle rouge : dernier passage en échec ou trop ancien

import { readFileSync } from "node:fs";
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

let status;
try {
  status = JSON.parse(readFileSync(join(SUPPORT, "status.json"), "utf8"));
} catch {
  status = null;
}

const lines = [];
const current = status?.current;
const age = status ? minutesSince(status.updated) : null;
const overdue = current && minutesSince(current.started) > (current.timeout ?? 1800) / 60 + STALE_MINUTES;
let problem = null;
if (!status) problem = "aucun passage enregistré";
else if (overdue) problem = `bloqué sur ${current.label}`;
else if (status.error) problem = status.error;
else if (!current && age > STALE_MINUTES) problem = `aucun passage depuis ${age} min`;

if (problem) lines.push("| sfimage=exclamationmark.triangle.fill sfcolor=#d33");
else if (current) lines.push(`${clean(current.label.split(" - ")[0])} | sfimage=paperplane.fill length=18`);
else lines.push(`${status.sent ?? ""} | sfimage=paperplane`);
lines.push("---");

if (problem) lines.push(`En panne : ${clean(problem)} | length=70 color=#d33`);
if (current) lines.push(`En cours : ${clean(current.label)} (${minutesSince(current.started)} min) | length=70`);
else if (!problem) lines.push("En veille, aucun dossier en attente");
if (status) {
  lines.push(`Dernier passage : il y a ${age} min`);
  lines.push(`${status.sent ?? 0} candidatures envoyées`);
}

const history = status?.history ?? [];
if (history.length) lines.push("---");
for (const entry of history) {
  lines.push(`${MARKS[entry.status] ?? "•"} ${clean(entry.label)}  ${hour(entry.at)} | length=60`);
  lines.push(`--${clean(entry.reason)} | length=90`);
}

lines.push("---");
lines.push(`Ouvrir le journal | bash=/usr/bin/open param1=-a param2=Console param3="${LOG}" terminal=false`);
console.log(lines.join("\n"));
