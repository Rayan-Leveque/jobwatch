CREATE TABLE IF NOT EXISTS source (
  id INTEGER PRIMARY KEY,
  type TEXT NOT NULL,                -- 'france_travail' | 'smartrecruiters'
  name TEXT NOT NULL UNIQUE,
  last_run_at TEXT
);
CREATE TABLE IF NOT EXISTS company (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS offer (
  id INTEGER PRIMARY KEY,
  source_id INTEGER NOT NULL REFERENCES source(id),
  company_id INTEGER REFERENCES company(id),
  title TEXT NOT NULL,
  url TEXT NOT NULL UNIQUE,          -- global dedup key
  platform TEXT,
  location TEXT,
  contract TEXT,                     -- 'permanent' | 'fixed_term' | 'internship' | 'other'
  published_at TEXT,
  deadline TEXT,
  collected_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_offer_company_title ON offer(company_id, title);
CREATE TABLE IF NOT EXISTS offer_summary (
  id INTEGER PRIMARY KEY,
  offer_id INTEGER NOT NULL UNIQUE REFERENCES offer(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS summary_field (
  summary_id INTEGER NOT NULL REFERENCES offer_summary(id) ON DELETE CASCADE,
  key TEXT NOT NULL,                 -- 'experience' | 'salary' | 'remote' | 'stack'
  value TEXT NOT NULL,               -- texte libre du LLM ; 'non précisé' si l'offre ne dit rien
  PRIMARY KEY(summary_id, key)
);
CREATE TABLE IF NOT EXISTS summary_bullet (
  summary_id INTEGER NOT NULL REFERENCES offer_summary(id) ON DELETE CASCADE,
  position INTEGER NOT NULL,
  text TEXT NOT NULL,
  PRIMARY KEY(summary_id, position)
);
CREATE TABLE IF NOT EXISTS offer_content (
  id INTEGER PRIMARY KEY,
  offer_id INTEGER NOT NULL UNIQUE REFERENCES offer(id) ON DELETE CASCADE,
  markdown TEXT,                     -- texte complet de l'offre converti en Markdown ; NULL si status='failed'
  fetch_method TEXT,                 -- 'http' | 'playwright'
  status TEXT NOT NULL,              -- 'ok' | 'failed'
  fetched_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS search (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  include_json TEXT NOT NULL,        -- JSON list of keywords (title match, any-of)
  exclude_json TEXT NOT NULL,        -- JSON list of keywords (title match, none-of)
  locations_json TEXT NOT NULL,      -- JSON list; empty = anywhere
  contract TEXT,                     -- NULL = any
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS match (
  id INTEGER PRIMARY KEY,
  search_id INTEGER NOT NULL REFERENCES search(id),
  offer_id INTEGER NOT NULL REFERENCES offer(id),
  state TEXT NOT NULL DEFAULT 'new', -- 'new' | 'seen' | 'later' | 'discarded' | 'applied'
  notified_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  fit TEXT,                          -- 'high' | 'medium' | 'low' | NULL = unknown
  discarded_at TEXT,                 -- horodatage du passage à 'discarded' ; NULL sinon
  UNIQUE(search_id, offer_id)
);
CREATE TABLE IF NOT EXISTS application (
  id INTEGER PRIMARY KEY,
  match_id INTEGER REFERENCES match(id),
  offer_id INTEGER NOT NULL REFERENCES offer(id),
  note TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS event (
  id INTEGER PRIMARY KEY,
  application_id INTEGER NOT NULL REFERENCES application(id),
  type TEXT NOT NULL,                -- 'applied' | 'follow_up' | 'interview' | 'rejected' | 'offer'
  at TEXT NOT NULL DEFAULT (datetime('now')),
  comment TEXT
);
CREATE TABLE IF NOT EXISTS document (
  id INTEGER PRIMARY KEY,
  application_id INTEGER NOT NULL REFERENCES application(id),
  type TEXT NOT NULL,                -- 'cv' | 'cover_letter'
  path TEXT NOT NULL,
  sent_at TEXT
);
CREATE TABLE IF NOT EXISTS document_library (
  id INTEGER PRIMARY KEY,
  type TEXT NOT NULL,                -- 'cv' | 'cover_letter'
  label TEXT NOT NULL,
  file_path TEXT NOT NULL,
  uploaded_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS draft_job (
  id INTEGER PRIMARY KEY,
  match_id INTEGER NOT NULL REFERENCES match(id),
  track TEXT NOT NULL,               -- 'engineer' | 'project' : choisit les lettres exemples
  cv_library_id INTEGER REFERENCES document_library(id),
  instruction TEXT,                  -- consigne libre de régénération ; NULL sinon
  status TEXT NOT NULL DEFAULT 'running', -- 'queued' | 'running' | 'ok' | 'failed'
  error TEXT,                        -- message d'échec ; NULL si ok
  warning TEXT,                      -- ex. lettre générée sans le texte complet de l'offre
  tex_path TEXT,
  pdf_path TEXT,
  png_pages INTEGER,                 -- nombre de pages rendues en PNG
  library_id INTEGER REFERENCES document_library(id), -- entrée cover_letter produite
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_draft_job_match ON draft_job(match_id, id);
