"""
LocalLedger — database layer.

Everything durable lives here: the schema and the seed data. This is the part
that ports over unchanged if you ever rebuild the UI natively in Swift — the
same .sqlite file opens in GRDB with this exact schema.

Design rules that matter:
  - Money is stored as integer CENTS. Never floats. (amount_cents)
  - Each year is its own database file (ledger_YYYY.sqlite), so no year ever
    bloats the others.
  - The schema already contains stub tables (transfers, allocations, documents,
    attachments) so milestones 2+ don't require a migration.
"""

import sqlite3
import os
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'bank',      -- bank | credit | cash
    default_use   TEXT NOT NULL DEFAULT 'business',  -- business | personal
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS categories (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    kind          TEXT NOT NULL DEFAULT 'variable',  -- fixed | variable | optional
    use_default   TEXT NOT NULL DEFAULT 'either',    -- business | personal | either
    active        INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS transactions (
    id              INTEGER PRIMARY KEY,
    account_id      INTEGER NOT NULL REFERENCES accounts(id),
    txn_date        TEXT NOT NULL,                   -- YYYY-MM-DD
    description     TEXT NOT NULL,                   -- raw bank text
    merchant_norm   TEXT NOT NULL,                   -- normalized key for matching
    amount_cents    INTEGER NOT NULL,               -- signed: negative = money out
    use             TEXT,                            -- business | personal | mixed
    category_id     INTEGER REFERENCES categories(id),
    category_source TEXT NOT NULL DEFAULT 'none',    -- user | rule | ai | none
    ai_confidence   REAL,
    reviewed        INTEGER NOT NULL DEFAULT 0,
    reconciled      INTEGER NOT NULL DEFAULT 0,
    note            TEXT NOT NULL DEFAULT '',
    import_batch_id INTEGER REFERENCES import_batches(id),
    dedup_key       TEXT NOT NULL UNIQUE,            -- blocks duplicate imports
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS merchant_rules (
    id              INTEGER PRIMARY KEY,
    merchant_norm   TEXT NOT NULL UNIQUE,            -- learned: normalized merchant -> category
    category_id     INTEGER NOT NULL REFERENCES categories(id),
    use             TEXT,
    hits            INTEGER NOT NULL DEFAULT 1,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS import_batches (
    id            INTEGER PRIMARY KEY,
    account_id    INTEGER NOT NULL REFERENCES accounts(id),
    filename      TEXT NOT NULL,
    imported_at   TEXT NOT NULL,
    row_count     INTEGER NOT NULL DEFAULT 0,
    dup_count     INTEGER NOT NULL DEFAULT 0
);

-- ---- stubs for later milestones (created now so no migration is needed) ----
CREATE TABLE IF NOT EXISTS transfers (
    id         INTEGER PRIMARY KEY,
    txn_a_id   INTEGER REFERENCES transactions(id),
    txn_b_id   INTEGER REFERENCES transactions(id),
    confirmed  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS allocations (      -- splits: one txn -> many category/use slices
    id           INTEGER PRIMARY KEY,
    txn_id       INTEGER NOT NULL REFERENCES transactions(id),
    category_id  INTEGER REFERENCES categories(id),
    use          TEXT,
    amount_cents INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (        -- receipts / pdfs / emails, copied INTO the book
    id           INTEGER PRIMARY KEY,
    filename     TEXT NOT NULL,
    sha256       TEXT NOT NULL UNIQUE,         -- dedup by content hash
    stored_path  TEXT NOT NULL,
    imported_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS attachments (
    id                INTEGER PRIMARY KEY,
    document_id       INTEGER NOT NULL REFERENCES documents(id),
    txn_id            INTEGER NOT NULL REFERENCES transactions(id),
    confirmed_by_user INTEGER NOT NULL DEFAULT 0
);
"""

# Broad, stable categories — deliberately few (transcript: 15-30, not a deep tree).
SEED_CATEGORIES = [
    # name, kind, use_default
    ("Equipment",              "variable", "business"),
    ("Software & Subscriptions","fixed",   "business"),
    ("Office Supplies",        "variable", "business"),
    ("Advertising & Marketing","variable", "business"),
    ("Professional Services",  "variable", "business"),
    ("Shipping",               "variable", "business"),
    ("Office Rent",            "fixed",    "business"),
    ("Meals",                  "variable", "business"),
    ("Travel",                 "variable", "either"),
    ("Gas / Vehicle",          "variable", "either"),
    ("Utilities / Phone",      "fixed",    "either"),
    ("Insurance",              "fixed",    "either"),
    ("Groceries",              "variable", "personal"),
    ("Dining",                 "optional", "personal"),
    ("Household",              "variable", "personal"),
    ("Entertainment",          "optional", "personal"),
    ("Health",                 "variable", "personal"),
    ("Mortgage / Rent",        "fixed",    "personal"),
    ("Shopping",               "optional", "personal"),
    ("Pets",                   "variable", "personal"),
    ("Income",                 "variable", "either"),
    ("Transfer",               "variable", "either"),
    ("Uncategorized",          "variable", "either"),
]

# A few starter rules so you can see rule-first categorization working immediately.
# The app LEARNS the rest from your confirmations.
SEED_RULES = [
    ("ADOBE",      "Software & Subscriptions"),
    ("BH PHOTO",   "Equipment"),
    ("VERIZON",    "Utilities / Phone"),
    ("WAWA",       "Gas / Vehicle"),
    ("SHELL",      "Gas / Vehicle"),
    ("NETFLIX",    "Entertainment"),
]

SEED_ACCOUNTS = [
    ("Business Checking", "bank",   "business"),
    ("Personal Visa",     "credit", "personal"),
]


def connect(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path):
    """Create schema + seed reference data if the file is new."""
    fresh = not os.path.exists(path)
    conn = connect(path)
    conn.executescript(SCHEMA)
    if fresh:
        now = datetime.utcnow().isoformat()
        for name, kind, use in SEED_CATEGORIES:
            conn.execute(
                "INSERT OR IGNORE INTO categories(name, kind, use_default) VALUES (?,?,?)",
                (name, kind, use),
            )
        for name, kind, use in SEED_ACCOUNTS:
            conn.execute(
                "INSERT INTO accounts(name, kind, default_use, created_at) VALUES (?,?,?,?)",
                (name, kind, use, now),
            )
        cat_id = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM categories")}
        for merchant, catname in SEED_RULES:
            conn.execute(
                "INSERT OR IGNORE INTO merchant_rules(merchant_norm, category_id, updated_at) VALUES (?,?,?)",
                (merchant, cat_id[catname], now),
            )
        conn.commit()
    return conn


def category_map(conn):
    return {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM categories WHERE active=1")}


def category_names(conn):
    return [r["name"] for r in conn.execute("SELECT name FROM categories WHERE active=1 ORDER BY name")]
