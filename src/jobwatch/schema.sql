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
  quote TEXT,                        -- citation littérale de l'annonce, vérifiée présente dans offer_content ; NULL si le champ n'est pas ancré
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
  markdown TEXT,                     -- bloc utile de l'offre en Markdown ; NULL si status='failed'
  fetch_method TEXT,                 -- 'http' | 'playwright'
  extract_method TEXT,               -- 'jsonld' | 'readable' | 'raw' (repli : page entière)
  html_gz BLOB,                      -- HTML brut compressé, pour re-extraire sans refetch
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
  active INTEGER NOT NULL DEFAULT 1, -- 0 = ne collecte plus (config.yaml ou catégorie retirée)
  archived_at TEXT                   -- non NULL = retirée par l'utilisateur, masquée du tableau de bord
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

-- Authentification et espaces. Une base d'instance ne contient actuellement qu'un
-- espace, mais ce modèle permet la future mutualisation de plusieurs espaces.
CREATE TABLE IF NOT EXISTS workspace (
  id INTEGER PRIMARY KEY,
  slug TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS account (
  id INTEGER PRIMARY KEY,
  email TEXT NOT NULL COLLATE NOCASE UNIQUE,
  password_hash TEXT,
  disabled INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS membership (
  account_id INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
  workspace_id INTEGER NOT NULL REFERENCES workspace(id) ON DELETE CASCADE,
  role TEXT NOT NULL,                 -- 'owner' pour la phase instance isolée
  PRIMARY KEY(account_id, workspace_id)
);
CREATE TABLE IF NOT EXISTS account_invite (
  id INTEGER PRIMARY KEY,
  workspace_id INTEGER NOT NULL REFERENCES workspace(id) ON DELETE CASCADE,
  email TEXT NOT NULL COLLATE NOCASE,
  role TEXT NOT NULL DEFAULT 'owner',
  token_hash TEXT NOT NULL UNIQUE,
  expires_at TEXT NOT NULL,
  accepted_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS web_session (
  token_hash TEXT PRIMARY KEY,
  account_id INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
  workspace_id INTEGER NOT NULL REFERENCES workspace(id) ON DELETE CASCADE,
  csrf_token TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  last_seen_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_web_session_account ON web_session(account_id);
CREATE TABLE IF NOT EXISTS instance_setting (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS login_throttle (
  key_hash TEXT PRIMARY KEY,
  failures INTEGER NOT NULL,
  window_started_at TEXT NOT NULL,
  blocked_until TEXT
);

-- Profil candidat et pistes confirmées pendant l'onboarding. Le profil est lié
-- au compte, pas à l'instance, afin de rester compatible avec le futur mode C.
CREATE TABLE IF NOT EXISTS candidate_profile (
  account_id INTEGER PRIMARY KEY REFERENCES account(id) ON DELETE CASCADE,
  workspace_id INTEGER NOT NULL REFERENCES workspace(id) ON DELETE CASCADE,
  cv_library_id INTEGER REFERENCES document_library(id),
  completed_at TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS candidate_profile_document (
  account_id INTEGER NOT NULL REFERENCES candidate_profile(account_id) ON DELETE CASCADE,
  document_library_id INTEGER NOT NULL REFERENCES document_library(id) ON DELETE CASCADE,
  position INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (account_id, document_library_id)
);
CREATE TABLE IF NOT EXISTS career_intent (
  id INTEGER PRIMARY KEY,
  account_id INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
  label TEXT NOT NULL,
  keywords_json TEXT NOT NULL,
  exclude_json TEXT NOT NULL DEFAULT '[]',
  position INTEGER NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1,
  search_id INTEGER REFERENCES search(id),
  UNIQUE(account_id, label)
);
CREATE INDEX IF NOT EXISTS idx_career_intent_account ON career_intent(account_id, position);
