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
    active        INTEGER NOT NULL DEFAULT 1,
    -- Nesting. NULL = top level. A category with at least one child is a
    -- HEADING whose total is the sum of its children; a category with no
    -- children is a LEAF, and only leaves are ever assigned to a transaction
    -- or a merchant_rule. Names stay globally unique whatever the shape of
    -- the tree, so every wire contract that passes a category by name is
    -- unchanged.
    parent_id     INTEGER REFERENCES categories(id)
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
    dup_count     INTEGER NOT NULL DEFAULT 0,
    -- Absolute path of a natively-picked file, NULL for a browser upload (a
    -- browser never tells us where the file really lives). Read only to reveal
    -- the file in Finder and for the opt-in file delete on undo; nothing in the
    -- ledger depends on it, so NULL is always a valid state.
    source_path   TEXT
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

# A near-empty starting chart. A fresh book opens on THREE categories, and the
# user builds their own tree from there on /categories — an accounting chart is
# a personal thing, and twenty opinionated spending buckets are twenty someone
# else's guesses to delete before you can start.
#
# What is left is structural rather than opinionated: money in, money moved
# between your own accounts, and the system fallback. Nothing here says
# anything about how you spend.
#
#   - "Income"        money in, as opposed to spending
#   - "Transfer"      an internal move, neither income nor spending
#   - "Uncategorized" the system fallback leaf. NON-NEGOTIABLE: every picker,
#                     the importer and resolve_leaf() fall back to it by name,
#                     and it is locked against rename/move/delete/children.
#
# All three seed at top level (parent_id NULL), so a fresh book is three leaves.
SEED_CATEGORIES = [
    # name, kind, use_default
    ("Income",                 "variable", "either"),
    ("Transfer",               "variable", "either"),
    ("Uncategorized",          "variable", "either"),
]

# No starter rules ship. A rule has to point at a category, and the only
# categories left are ones no merchant belongs to — a seeded rule would either
# name a category that no longer exists or teach an opinion the user never
# asked for. The learning loop is unchanged: confirm a merchant on Categorize
# (or Review) and the rule is written then, from the user's own decision.
SEED_RULES = []

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


# ------------------------------ the category tree --------------------------
# A category may have a parent (parent_id NULL = top level). One with at least
# one child is a HEADING; one with no children is a LEAF. Transactions and
# merchant_rules point at LEAVES ONLY — a heading's total is the sum of its
# children, so nothing is ever posted directly to it. Names stay globally
# unique (UNIQUE(name) above), which is why every picker in the app can go on
# passing a category by NAME and only the DISPLAY needs the path.

CATEGORY_SEP = " › "        # "A > B > C" with a single-guillemet separator
SYSTEM_LEAF = "Uncategorized"    # never renamed, moved, deleted, or given children


def children(conn, cat_id):
    """The direct children of one category, by name."""
    return conn.execute(
        "SELECT id, name, parent_id, active FROM categories WHERE parent_id=? ORDER BY name",
        (cat_id,)).fetchall()


def is_leaf(conn, cat_id):
    """True when nothing claims `cat_id` as its parent. That is the whole test."""
    return conn.execute(
        "SELECT 1 FROM categories WHERE parent_id=? LIMIT 1", (cat_id,)).fetchone() is None


def category_labels(conn):
    """{id: 'A › B › C'} — every category's full path, for display.

    Built from one query and a walk up the parent chain, so a page can label a
    hundred rows without a hundred lookups. Anything at the top level labels as
    its own bare name, which is why the seeded flat categories read exactly as
    they always have.
    """
    rows = {r["id"]: (r["name"], r["parent_id"])
            for r in conn.execute("SELECT id, name, parent_id FROM categories")}
    labels = {}
    for cid in rows:
        parts, node, seen = [], cid, set()
        # `seen` is belt and braces: the move guard rejects cycles, so one can
        # only exist if a row was edited outside the app. A label is display —
        # it must never be the thing that hangs.
        while node is not None and node in rows and node not in seen:
            seen.add(node)
            name, parent = rows[node]
            parts.append(name)
            node = parent
        labels[cid] = CATEGORY_SEP.join(reversed(parts))
    return labels


def leaf_choices(conn):
    """Every assignable category — LEAVES ONLY — as {id, name, label}, by label.

    The single source every picker reads: the grid's embedded list, the bulk
    bar, /review and /categorize. `name` is what goes on the wire and into the
    database; `label` is only ever shown.
    """
    labels = category_labels(conn)
    parents = {r["parent_id"] for r in conn.execute(
        "SELECT DISTINCT parent_id FROM categories WHERE parent_id IS NOT NULL")}
    rows = conn.execute("SELECT id, name FROM categories WHERE active=1")
    return sorted(
        ({"id": r["id"], "name": r["name"], "label": labels[r["id"]]}
         for r in rows if r["id"] not in parents),
        key=lambda c: c["label"])


def resolve_leaf(conn, name):
    """The id of `name` if it names an active LEAF, else None.

    Every assignment goes through here. A heading's name, or a name nobody
    knows, is simply not assignable and the caller falls back to Uncategorized
    — the same fallback an unknown name has always taken.
    """
    row = conn.execute(
        "SELECT id FROM categories WHERE name=? AND active=1", (name,)).fetchone()
    if row is None or not is_leaf(conn, row["id"]):
        return None
    return row["id"]


def is_descendant(conn, node_id, maybe_ancestor_id):
    """True if node_id sits anywhere under maybe_ancestor_id. Not reflexive."""
    parent = {r["id"]: r["parent_id"]
              for r in conn.execute("SELECT id, parent_id FROM categories")}
    node, seen = parent.get(node_id), set()
    while node is not None and node not in seen:
        if node == maybe_ancestor_id:
            return True
        seen.add(node)
        node = parent.get(node)
    return False


def category_tree(conn):
    """Depth-first nodes for the management page.

    Each is {id, name, parent_id, depth, is_leaf, direct_txn_count,
    child_count}. `direct_txn_count` counts transactions pointed straight at
    that row: under the leaves-only rule a heading's is 0, and the page shows
    the number rather than assuming it.
    """
    rows = conn.execute(
        "SELECT id, name, parent_id FROM categories ORDER BY name").fetchall()
    kids = {}
    for r in rows:
        kids.setdefault(r["parent_id"], []).append(r)
    counts = {r["category_id"]: r["n"] for r in conn.execute(
        """SELECT category_id, COUNT(*) n FROM transactions
            WHERE category_id IS NOT NULL GROUP BY category_id""")}

    out = []

    def walk(parent_id, depth):
        for r in kids.get(parent_id, []):
            mine = kids.get(r["id"], [])
            out.append({"id": r["id"], "name": r["name"], "parent_id": r["parent_id"],
                        "depth": depth, "is_leaf": not mine,
                        "direct_txn_count": counts.get(r["id"], 0),
                        "child_count": len(mine)})
            walk(r["id"], depth + 1)

    walk(None, 0)
    return out
