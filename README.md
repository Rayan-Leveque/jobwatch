# jobwatch

Self-hosted job-posting watcher. It collects job postings from job-board APIs, dedupes them
into a local SQLite database, matches them against your saved searches, sends a digest
notification for new matches, and lets you track your applications from the command line.

Flow: **collect -> dedup -> match -> notify -> track**.

No LLM features, no cloud, no tracking. Your data stays in one SQLite file on your machine.

## Quickstart

```bash
git clone <this repo> && cd jobwatch
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/jw init                # creates config.yaml + an empty database
# edit config.yaml: add sources and notification channels, then:
.venv/bin/jw run                # collect, match, notify
.venv/bin/jw list               # show new matches
.venv/bin/jw apply 1 --note "cv sent"
.venv/bin/jw log 1 interview -m "phone screen"
.venv/bin/jw apps               # applications with current status
```

`jw init` refuses to overwrite an existing `config.yaml`. Every command accepts
`--config PATH` (default `./config.yaml`, falling back to `~/.config/jobwatch/config.yaml`).

### Cron

Run `jw run` daily from cron:

```
0 7 * * * cd ~/jobwatch && .venv/bin/jw run
```

## Config reference

`config.yaml` (a copy of `config.example.yaml`) has four sections.

| Key | Description |
| --- | --- |
| `db` | Path to the SQLite database. `~` is expanded. Directories are created automatically. |
| `searches` | List of saved searches. Each search has: `name` (unique id), `include` (keywords, any-of, case-insensitive substring match on the title), `exclude` (keywords, none-of), `locations` (substring match on the offer location; empty = anywhere), `contract` (optional: `permanent`, `fixed_term`, `internship`, `other`). |
| `sources` | The job boards to watch. `france_travail` needs `client_id`, `client_secret`, `keywords` (server-side query) and optionally `department`. `smartrecruiters` takes a list of company slugs. |
| `notify` | Notification channels. `ntfy` posts to `https://ntfy.sh/<topic>`. `smtp` sends via `host`, `port`, `user`, `password`, `to`. Both are optional; you can use one, both, or none. |

Searches are synced into the database on every `jw run`: new ones are inserted, changed ones
updated, removed ones deactivated (existing matches are kept).

## France Travail credentials

1. Go to https://francetravail.io and create an account (or use your France Travail account).
2. Create an application ("créer une application") for the API `api_offresdemploiv2`.
3. Note the `client_id` and `client_secret` and put them in `config.yaml`.
4. Request the scope `api_offresdemploiv2` for the application.

jobwatch then performs the OAuth2 client-credentials flow automatically on each `jw run`.
A broken or unconfigured source is logged as a warning and never aborts the run.

## Data model

Offers are deduped globally by URL, and additionally skipped when the same company already has
an offer with the same title. Each offer is matched against every active search; matches are
stored with a state (`new`, `seen`, `applied`, `discarded`). An application is created from a
match, and its current status is the latest event in its event log.

| Table | Purpose |
| --- | --- |
| `source` | Configured job-board sources and their last run time |
| `company` | Companies (deduped by name) |
| `offer` | Job postings (deduped by URL and company+title) |
| `search` | Saved searches (keywords, locations, contract) |
| `match` | Offer/search pair with state and notification status |
| `application` | Your application for an offer |
| `event` | Timeline of an application (applied, interview, rejected, offer, ...) |
| `document` | CV and cover-letter files attached to an application |

## Roadmap

- v0.2: dashboard (`jw serve`), markdown export of applications, more sources.
- v0.3: optional LLM summaries and fit-scoring for collected offers.

## License

MIT. Copyright 2026 Rayan Leveque.
