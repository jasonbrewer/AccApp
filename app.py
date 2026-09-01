"""
LocalLedger — a local, private personal/business ledger.

Runs entirely on your machine. The browser is just the window; nothing is sent
anywhere. Local AI (Ollama) is optional — with it off, you get rule-based
categorization and manual review, and the app is fully usable.

Run:
    pip install flask
    python app.py
    open http://127.0.0.1:5000

Milestone 1 (this build): accounts, CSV import + duplicate detection,
rule-first + optional-Ollama categorization, two-at-a-time review that learns
from your confirmations, and a spending dashboard.
Next milestones: transfers, splits, the Monthly Nut, and the document dump / OCR.

Pages are server-rendered. The one exception is the Transactions grid, which is
client-interactive: a small amount of vanilla JS (no libraries, no CDN, all of
it inline and local) adds inline editing of category / use / note with autosave.
That grid is a deliberate non-teaching corrections surface — editing a row there
never writes a merchant_rule. Rule learning lives on Categorize (and /review).
Corrections there never rewrite dedup_key either: it is frozen at import, so a
re-imported statement still dedups a row whose amount or date you have fixed.
Multi-select and the bulk actions built on it (set category, set use, delete)
inherit both rules: they stamp the selected rows and teach nothing, and a bulk
delete takes each row's dedup_key with it, so re-importing the same statement
re-adds exactly what was deleted — the same bargain as undoing an import.

The import mapping page can save a column layout as a named profile. Those live
in one small JSON file (LEDGER_PROFILES, default localledger_profiles.json)
beside this one — deliberately NOT in a ledger_YYYY.sqlite, so a bank's layout
is global across years and the schema is untouched. A profile stores each
column's header NAME as well as its index, and applying one matches by name
first, so a bank reordering its export doesn't cost you the mapping.
"""

import csv
import html
import io
import json
import os
import secrets
import sqlite3
import subprocess
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation

from flask import (Flask, Response, request, redirect, url_for,
                   render_template_string, jsonify)

import db
from categorizer import Categorizer, normalize_merchant, ollama_available

YEAR = datetime.now().year
DB_PATH = os.environ.get("LEDGER_DB", f"ledger_{YEAR}.sqlite")

app = Flask(__name__)


# ----------------------------- helpers -----------------------------------

VALID_USES = ("business", "personal")


def get_conn():
    return db.init_db(DB_PATH)


def esc(value):
    """The one escape hatch for anything user- or CSV-provided.

    Pages are f-strings handed to `body|safe`, so nothing is escaped for us.
    Every interpolated value that didn't come from this file goes through here.
    """
    return html.escape("" if value is None else str(value), quote=True)


def money(cents):
    # Integer math only — invariant 1. abs(cents)/100 would put money on a float.
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(cents), 100)
    return f"{sign}${whole:,}.{frac:02d}"


def parse_amount_to_cents(raw):
    """Handle $1,234.56, (12.34) as negative, plain -47.31, etc."""
    s = str(raw).strip().replace("$", "").replace(",", "")
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1]
    try:
        val = Decimal(s)
    except InvalidOperation:
        return None
    if not val.is_finite():
        # Decimal happily parses 'nan' / 'Infinity'; int() then blows up.
        return None
    cents = int((val * 100).to_integral_value())
    return -abs(cents) if neg else cents


def split_amount_to_cents(raw_debit, raw_credit):
    """Fold a Debit/Credit column pair into one signed cents value.

    Money out is negative, money in positive. Taking abs() of each side means a
    debit written as 5.21 and one written as -41.31 land the same way — banks do
    both. Returns None when neither side is filled in or a filled side won't
    parse, which the caller treats as an unreadable row.
    """
    d, c = str(raw_debit).strip(), str(raw_credit).strip()
    if not d and not c:
        return None
    debit_cents = credit_cents = 0
    if d:
        debit_cents = parse_amount_to_cents(d)
        if debit_cents is None:
            return None
    if c:
        credit_cents = parse_amount_to_cents(c)
        if credit_cents is None:
            return None
    return abs(credit_cents) - abs(debit_cents)


def is_dedup_conflict(err):
    """True only for the dedup_key UNIQUE violation — i.e. an already-imported row.

    Everything else (NOT NULL, foreign key, disk) is a real failure and must not
    be quietly counted as a duplicate.
    """
    msg = str(err).lower()
    return "unique constraint failed" in msg and "dedup_key" in msg


HEADER_KEYWORDS = (
    "date", "posted", "posting", "clearing", "description", "desc", "memo",
    "amount", "amt", "debit", "credit", "balance", "transaction", "merchant",
    "category", "type", "payee", "withdrawal", "deposit", "check", "fees", "card",
)


def find_header_row(rows):
    """Index of the row that actually looks like a header.

    Real statements often carry a preamble ("Date Range : ...") above the
    header, so rows[0] is not a safe assumption. Score each of the first 20
    rows by how many of its cells look like column names; the best-scoring
    row that clears the bar wins, ties going to the earliest. If nothing
    qualifies we return 0 — the old behavior, including its error path.
    """
    best_idx, best_score = 0, 0
    for i, row in enumerate(rows[:20]):
        cells = [c.strip().lower() for c in row]
        filled = sum(1 for c in cells if c)
        score = sum(1 for c in cells if any(k in c for k in HEADER_KEYWORDS))
        if score >= 2 and filled >= 2 and score > best_score:
            best_idx, best_score = i, score
    return best_idx


def detect_columns(header):
    """Map a bank CSV's header row to date / description / amount column(s).

    Two shapes exist in the wild: one signed amount column, or a split
    Debit/Credit pair. The split is decided FIRST — otherwise a header like
    "Amount Debit,Amount Credit" would be misread as a single amount column
    and every credit would be lost.
    """
    lower = [h.strip().lower() for h in header]
    def find(cands, skip=()):
        for i, h in enumerate(lower):
            if i in skip:
                continue
            if any(c in h for c in cands):
                return i
        return None

    date = find(["date", "posted", "posting", "clearing"])
    desc = find(["description", "merchant", "name", "payee", "memo", "details"])
    debit = find(["debit", "withdrawal", "charge"])
    credit = find(["credit", "deposit"])

    if debit is not None and credit is not None and debit != credit:
        return {"date": date, "desc": desc, "mode": "debitcredit",
                "debit": debit, "credit": credit, "amount": None}

    amount = find(["amount", "amt", "value"], skip={date, desc})
    return {"date": date, "desc": desc, "mode": "single",
            "amount": amount, "debit": None, "credit": None}


# -------------------- native macOS integration (mac-only) ------------------
# Same shape the footage pipeline uses for its native folder picker: check
# sys.platform, shell out to osascript with an argv LIST, treat a user cancel
# as a normal answer rather than an error. Everything mac-only funnels through
# run_command — the one place this app starts a process — so there is a single
# seam to check the platform at, and a single seam a test replaces to exercise
# all of this without a dialog or a Finder window ever opening.
#
# None of it is load-bearing: with no picker at all the browser upload beside
# it does the whole job, which is what happens on any non-mac OS.
MAX_IMPORT_BYTES = 32 * 1024 * 1024      # a statement this big is not a statement
PICKER_TIMEOUT = 300                     # a modal dialog: the user may take a while
REVEAL_TIMEOUT = 15

# No interpolation, ever: the script is a constant, so nothing a user types can
# reach AppleScript. The path comes back OUT of it, it never goes in.
CHOOSE_FILE_SCRIPT = 'POSIX path of (choose file with prompt "Choose a CSV file")'


def is_mac():
    """True on macOS. A function, not a constant, so tests can drive both paths."""
    return sys.platform == "darwin"


def run_command(argv, timeout=REVEAL_TIMEOUT):
    """Run `argv` and hand back the CompletedProcess. The only spawn in the app.

    argv is always a list and `shell=` is never passed, so a path with spaces,
    quotes or a semicolon in it stays one argument instead of something a shell
    gets to parse.
    """
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


class NotOnMac(Exception):
    """Raised by the mac-only helpers so routes answer 501 in exactly one way."""


def native_choose_file():
    """The macOS file dialog -> an absolute POSIX path, or None if cancelled.

    A browser <input type=file> hands over bytes and a bare filename; it never
    says where the file lives. That is the whole reason this exists — with the
    real path we can reveal the file in Finder later.
    """
    if not is_mac():
        raise NotOnMac()
    try:
        done = run_command(["osascript", "-e", CHOOSE_FILE_SCRIPT], timeout=PICKER_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        # Cancel is returncode 1 with "User canceled. (-128)" on stderr. A
        # dialog that genuinely failed gets the same answer: no file chosen.
        return None
    path = (done.stdout or "").strip()
    return path or None


def reveal_in_finder(path):
    """`open -R <path>` — select the file in Finder. True if Finder took it."""
    if not is_mac():
        raise NotOnMac()
    try:
        return run_command(["open", "-R", path]).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def not_on_mac(note):
    """501, with the page still rendered — the browser upload is right there."""
    body = f"""<h1>Not available here</h1>
      <p class=sub>{esc(note)}</p>
      <a class=btn href="{url_for('do_import')}">Back to import</a>"""
    return page(body, "import"), 501


# Phase-1 uploads waiting for the user to confirm their column mapping, keyed by
# a random token. Deliberately in memory and nowhere near the schema: a parked
# upload is not ledger data, and a half-finished import should not survive a
# restart. If the process restarts (or the user sits on the page past the cap
# below), the token goes stale and phase 2 says so instead of crashing. Fine for
# a local single-user app; it would not be for a shared server.
PENDING_IMPORTS = {}
MAX_PENDING = 8
PREVIEW_ROWS = 5

# The two ways a bank writes spending in a single signed column.
SIGN_CHOICES = (("negative", "Negative numbers"), ("positive", "Positive numbers"))


def stash_import(account_id, filename, header, data, preamble, detected,
                 source_path=None):
    """Park a parsed upload for phase 2 and return its token.

    `source_path` is the absolute path of a natively-picked file (None for a
    browser upload). It rides in the stash and nowhere else — never in a URL,
    never in a hidden field — so the only path phase 2 can commit is one this
    process read off a dialog, not one a request handed it.
    """
    while len(PENDING_IMPORTS) >= MAX_PENDING:
        PENDING_IMPORTS.pop(next(iter(PENDING_IMPORTS)))     # oldest first
    token = secrets.token_urlsafe(16)
    PENDING_IMPORTS[token] = {
        "account_id": account_id, "filename": filename, "header": header,
        "data": data, "preamble": preamble, "detected": detected,
        "source_path": source_path,
    }
    return token


def form_column(field, ncols):
    """A column index the user picked, or None if absent/unusable."""
    raw = request.form.get(field)
    if raw is None or raw == "":
        return None
    try:
        i = int(raw)
    except ValueError:
        return None
    return i if 0 <= i < ncols else None


def effective_mapping(detected, ncols):
    """The mapping to read the file with: what was submitted, else what was detected.

    Every field falls back independently, so a commit carrying only a token
    imports under pure auto-detect — which is exactly what today's behavior is.
    """
    m = dict(detected)
    mode = request.form.get("mode")
    if mode in ("single", "debitcredit"):
        m["mode"] = mode
    for field, key in (("date_col", "date"), ("desc_col", "desc"),
                       ("amount_col", "amount"), ("debit_col", "debit"),
                       ("credit_col", "credit")):
        picked = form_column(field, ncols)
        if picked is not None:
            m[key] = picked
    sign = request.form.get("sign")
    m["sign"] = sign if sign in dict(SIGN_CHOICES) else detected.get("sign", "negative")
    return m


def mapping_is_complete(m):
    return not (m["date"] is None or m["desc"] is None or (
        m["amount"] is None if m["mode"] == "single"
        else m["debit"] is None or m["credit"] is None))


# ------------------------- saved column profiles --------------------------
# A bank's CSV layout is a property of the bank, not of a year, so profiles
# live in ONE small JSON file beside app.py — deliberately not in any
# ledger_YYYY.sqlite. No schema change, no migration, and a profile saved
# while working on 2026 is there when 2027's file is opened. The path is read
# at call time (never cached at import) so a test can point it somewhere else.
PROFILES_VERSION = 1
PROFILE_ROLES = ("date", "desc", "amount", "debit", "credit")
MAX_PROFILE_NAME = 120
MAX_PROFILE_FILE = 512 * 1024          # a store this big is not a mapping file


def profiles_path():
    return os.environ.get("LEDGER_PROFILES", "localledger_profiles.json")


def empty_profiles():
    return {"version": PROFILES_VERSION, "profiles": {}}


def valid_profile(prof):
    """Is this entry shaped like a profile? Used on anything read off disk."""
    if not isinstance(prof, dict):
        return False
    if prof.get("mode") not in ("single", "debitcredit"):
        return False
    for role in PROFILE_ROLES:
        got = prof.get(role)
        if got is None:
            continue
        if not isinstance(got, dict) or not isinstance(got.get("name"), str):
            return False
        idx = got.get("index")
        if idx is not None and (not isinstance(idx, int) or isinstance(idx, bool)):
            return False
    return True


def clean_profile(prof):
    """The canonical stored shape, so an imported file can't smuggle in extras."""
    out = {
        "mode": prof.get("mode"),
        "sign": prof.get("sign") if prof.get("sign") in dict(SIGN_CHOICES) else "negative",
    }
    for role in PROFILE_ROLES:
        got = prof.get(role)
        out[role] = ({"name": got.get("name"), "index": got.get("index")}
                     if isinstance(got, dict) else None)
    return out


def load_profiles():
    """The whole store. A missing, empty, or corrupt file is an EMPTY store.

    This is called on the way into the import page, so it must never raise:
    a hand-edited file with a stray comma should cost you your profiles, not
    your ability to import a statement. Entries that don't look like profiles
    are dropped here rather than at every use site.
    """
    try:
        with open(profiles_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):          # missing, unreadable, or not JSON
        return empty_profiles()
    if not isinstance(data, dict) or not isinstance(data.get("profiles"), dict):
        return empty_profiles()
    return {
        "version": PROFILES_VERSION,
        "profiles": {name: clean_profile(prof)
                     for name, prof in data["profiles"].items()
                     if isinstance(name, str) and valid_profile(prof)},
    }


def save_profiles(data):
    """Write the store atomically: full temp file, then one rename.

    os.replace is atomic on the same filesystem, so a crash (or a full disk)
    mid-write leaves the previous store intact instead of a truncated file
    that load_profiles would have to throw away.
    """
    path = profiles_path()
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def profile_names():
    return sorted(load_profiles()["profiles"])


def mapping_to_profile(mapping, header):
    """An effective_mapping (indices) -> the stored shape (name + index).

    Both are kept for every role. The name is what makes a profile survive a
    bank reordering its export; the index is the fallback for a file whose
    header text has changed but whose layout hasn't.
    """
    def role(i):
        if not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < len(header):
            return None
        return {"name": header[i].strip(), "index": i}

    mode = mapping.get("mode")
    return {
        "mode": mode if mode in ("single", "debitcredit") else "single",
        "sign": mapping.get("sign") if mapping.get("sign") in dict(SIGN_CHOICES)
                else "negative",
        "date": role(mapping.get("date")), "desc": role(mapping.get("desc")),
        "amount": role(mapping.get("amount")), "debit": role(mapping.get("debit")),
        "credit": role(mapping.get("credit")),
    }


def profile_to_mapping(profile, header):
    """A stored profile -> an effective_mapping-shaped dict of indices.

    Name first, index second: match the saved header text against this file's
    header (trimmed, case-insensitive) and use whatever column it landed in
    today; only if the name is gone fall back to the index it had when saved,
    and only if that is still in range. A role that resolves to neither is
    None, which the mapping page renders and read_row treats as an unreadable
    row — the same as any other incomplete mapping.
    """
    by_name = {}
    for i, h in enumerate(header):
        by_name.setdefault(h.strip().lower(), i)      # first column of a name wins

    def resolve(role):
        if not isinstance(role, dict):
            return None
        name = role.get("name")
        if isinstance(name, str):
            found = by_name.get(name.strip().lower())
            if found is not None:
                return found
        idx = role.get("index")
        if isinstance(idx, int) and not isinstance(idx, bool) and 0 <= idx < len(header):
            return idx
        return None

    mapping = {role: resolve(profile.get(role)) for role in PROFILE_ROLES}
    mode = profile.get("mode")
    mapping["mode"] = mode if mode in ("single", "debitcredit") else "single"
    mapping["sign"] = (profile.get("sign") if profile.get("sign") in dict(SIGN_CHOICES)
                       else "negative")
    return mapping


def merge_profiles(store, incoming):
    """Copy well-formed entries over the store by name. Returns how many landed."""
    merged = 0
    for name, prof in incoming.items():
        if not isinstance(name, str) or not name.strip() or not valid_profile(prof):
            continue
        store["profiles"][name.strip()[:MAX_PROFILE_NAME]] = clean_profile(prof)
        merged += 1
    return merged


def cell(row, i):
    """The cell at column `i`, or None if `i` isn't a usable index into the row.

    Folds the short-row skip (what the old `except IndexError` caught) together
    with a column the mapping never resolved. A missing column really is None —
    a mapping submitted for a mode whose selects weren't filled in resolves that
    way — and `row[None]` is a TypeError, which is not an unreadable row, it's a
    500. Unreadable is the honest answer, so it is given in one place.
    """
    if not isinstance(i, int) or isinstance(i, bool):
        return None
    if not 0 <= i < len(row):
        return None
    return row[i]


def read_row(row, m):
    """One data row under a mapping -> (date, desc, cents), or None if unreadable.

    The skip rules are the ones the importer has always used: a short row, an
    unparseable date, an empty description or an unparseable amount is skipped.
    Cents come from the existing parsers — parse_amount_to_cents for a single
    signed column, split_amount_to_cents for a Debit/Credit pair — never from
    anything new. The preview and the commit both go through here, so what you
    see on the mapping page is what gets written.
    """
    raw_date, raw_desc = cell(row, m["date"]), cell(row, m["desc"])
    if raw_date is None or raw_desc is None:
        return None
    desc = raw_desc.strip()
    if m["mode"] == "single":
        raw_amount = cell(row, m["amount"])
        if raw_amount is None:
            return None
        cents = parse_amount_to_cents(raw_amount)
        # A statement that writes spending as positive numbers is the same
        # file with every sign flipped; income keeps its own (opposite) sign.
        if cents is not None and m.get("sign") == "positive":
            cents = -cents
    else:
        raw_debit, raw_credit = cell(row, m["debit"]), cell(row, m["credit"])
        if raw_debit is None or raw_credit is None:
            return None
        cents = split_amount_to_cents(raw_debit, raw_credit)
    date = normalize_date(raw_date.strip())
    if cents is None or not desc or date is None:
        return None
    return date, desc, cents


def teach_merchant_rule(conn, merchant_norm, category_id, use):
    """Learn merchant -> category/use so the next import is rule-matched.

    The single statement that writes merchant_rules. /review's confirm and the
    grouped categorize screen both come through here, so the learning loop can
    never drift into two subtly different upserts.
    """
    conn.execute(
        """INSERT INTO merchant_rules(merchant_norm, category_id, use, updated_at)
           VALUES (?,?,?,?)
           ON CONFLICT(merchant_norm) DO UPDATE SET
             category_id=excluded.category_id, use=excluded.use,
             hits=hits+1, updated_at=excluded.updated_at""",
        (merchant_norm, category_id, use, datetime.utcnow().isoformat()))


def txn_use(row):
    """Effective business/personal for a txn: its own override, else account default."""
    return row["use"] or row["default_use"]


# ----------------------------- styling -----------------------------------
# Quiet ledger aesthetic: ink on paper, tabular figures, one settled-green
# accent reserved for confirmed/reconciled state (green = money settled).

BASE = """
<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>LocalLedger — {{ year }}</title>
<style>
  :root{
    --paper:#fbfaf7; --ink:#1c2433; --muted:#6a7382; --line:#e6e3dc;
    --settled:#2f7d5b; --review:#b7791f; --card:#ffffff; --accent:#2b4a6f;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--paper);color:var(--ink);
    font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
  .num{font-variant-numeric:tabular-nums;font-family:ui-monospace,"SF Mono",Menlo,monospace}
  header{border-bottom:1px solid var(--line);background:var(--card)}
  .bar{max-width:960px;margin:0 auto;padding:14px 20px;display:flex;align-items:center;gap:20px}
  .brand{font-weight:700;letter-spacing:-.01em}
  .brand span{color:var(--muted);font-weight:500}
  nav{display:flex;gap:6px;margin-left:auto}
  nav a{color:var(--ink);text-decoration:none;padding:6px 12px;border-radius:8px;font-size:14px}
  nav a:hover{background:#f0eee8}
  nav a.on{background:var(--ink);color:var(--paper)}
  main{max-width:960px;margin:0 auto;padding:28px 20px}
  h1{font-size:22px;margin:0 0 4px} h2{font-size:15px;margin:26px 0 10px;color:var(--muted);
    text-transform:uppercase;letter-spacing:.06em;font-weight:600}
  .sub{color:var(--muted);margin:0 0 20px}
  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
  .card .k{color:var(--muted);font-size:13px} .card .v{font-size:26px;font-weight:700;margin-top:4px}
  table{width:100%;border-collapse:collapse;background:var(--card);
    border:1px solid var(--line);border-radius:12px;overflow:hidden}
  th,td{text-align:left;padding:10px 14px;border-bottom:1px solid var(--line);font-size:14px}
  th{color:var(--muted);font-weight:600;background:#faf9f5} tr:last-child td{border-bottom:0}
  tr.sum td{border-top:2px solid var(--line);font-weight:600;background:#faf9f5}
  td.r,th.r{text-align:right}
  th a{color:inherit;text-decoration:none} th a:hover{text-decoration:underline}
  .pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:600}
  .pill.settled{background:#e7f2ec;color:var(--settled)}
  .pill.review{background:#fbf1dd;color:var(--review)}
  .pill.b{background:#e9eef6;color:var(--accent)} .pill.p{background:#f2ecf6;color:#6b4c8a}
  .btn{background:var(--ink);color:var(--paper);border:0;border-radius:9px;
    padding:10px 16px;font-size:14px;font-weight:600;cursor:pointer;text-decoration:none;display:inline-block}
  .btn.ghost{background:#f0eee8;color:var(--ink)}
  .rev{background:var(--card);border:1px solid var(--line);border-radius:14px;
    padding:18px 20px;margin-bottom:16px}
  .rev .top{display:flex;justify-content:space-between;align-items:baseline;gap:12px}
  .rev .desc{font-weight:600} .rev .meta{color:var(--muted);font-size:13px}
  .rev .amt{font-size:20px;font-weight:700}
  .field{margin-top:12px;display:flex;flex-wrap:wrap;gap:14px;align-items:center}
  label.lbl{font-size:13px;color:var(--muted);margin-right:6px}
  select,input[type=text]{font:inherit;padding:8px 10px;border:1px solid var(--line);
    border-radius:8px;background:#fff}
  input[type=text]{min-width:260px;flex:1}
  .radio{display:inline-flex;gap:2px;border:1px solid var(--line);border-radius:8px;overflow:hidden}
  .radio label{padding:7px 12px;cursor:pointer;font-size:13px}
  .radio input{display:none}
  .radio input:checked + span{background:var(--ink);color:var(--paper)}
  .radio span{padding:7px 12px;display:inline-block}
  .src{font-size:12px;color:var(--muted)}
  .empty{background:var(--card);border:1px dashed var(--line);border-radius:12px;
    padding:40px;text-align:center;color:var(--muted)}
  form.up{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px;
    display:flex;flex-direction:column;gap:14px;max-width:520px}
  .status{font-size:13px;padding:6px 10px;border-radius:8px}
  .status.on{background:#e7f2ec;color:var(--settled)} .status.off{background:#f0eee8;color:var(--muted)}
  /* buttons that sit side by side in a table cell or under a form */
  .rowbtns{display:flex;gap:6px;justify-content:flex-end;align-items:center;flex-wrap:wrap}
  /* --- import mapping: the saved-profiles bar (plain forms, no script) --- */
  .profiles{display:flex;flex-wrap:wrap;gap:8px;align-items:center;
    padding:0 0 12px;margin:0 0 4px;border-bottom:1px solid var(--line)}
  .profiles .btn{padding:6px 12px;font-size:13px}
  .profiles select{padding:6px 9px;font-size:13px}
  /* the shared input rule is a wide flexing text box; this bar wants neither */
  .profiles input[type=text]{min-width:0;flex:0 0 auto;width:190px;padding:6px 9px;font-size:13px}
  .profiles input[type=file]{font-size:12px;max-width:190px}
  .profiles .gap{flex:1 1 auto}
  /* --- transactions grid: inline editing (category / use / note) --- */
  td.ed{cursor:pointer;position:relative}
  td.ed:hover{background:#f6f4ee;box-shadow:inset 0 0 0 1px var(--line)}
  td.ed:focus{outline:2px solid var(--accent);outline-offset:-2px}
  td.ed .ph{color:var(--muted)}
  /* the table clips to its radius; the grid must let the dropdown out */
  #txn-grid{overflow:visible}
  #txn-grid tr:first-child th:first-child{border-top-left-radius:12px}
  #txn-grid tr:first-child th:last-child{border-top-right-radius:12px}
  tr.saving td{opacity:.5}
  tr.stale td{background:#fbeeec}
  .cbx{position:relative}
  .cbx input[type=text],td.ed input[type=text]{min-width:0;width:100%;padding:4px 6px;font-size:14px}
  .cbx-list{position:absolute;z-index:30;left:0;top:calc(100% + 3px);margin:0;padding:4px;
    list-style:none;min-width:200px;max-height:230px;overflow:auto;background:var(--card);
    border:1px solid var(--line);border-radius:9px;box-shadow:0 8px 22px rgba(28,36,51,.14)}
  .cbx-list li{padding:5px 8px;border-radius:6px;font-size:13px;white-space:nowrap;cursor:pointer}
  .cbx-list li.on{background:var(--ink);color:var(--paper)}
  .cbx-list li.none{color:var(--muted);cursor:default}
  .amt{position:absolute;right:0;top:4px;z-index:30;display:flex;gap:6px;align-items:center;
    padding:4px;background:var(--card);border:1px solid var(--line);border-radius:9px;
    box-shadow:0 8px 22px rgba(28,36,51,.14)}
  td.ed .amt input[type=text]{width:92px;text-align:right;
    font-variant-numeric:tabular-nums;font-family:ui-monospace,"SF Mono",Menlo,monospace}
  .seg{display:inline-flex;border:1px solid var(--line);border-radius:7px;overflow:hidden}
  .seg button{font:inherit;font-size:12px;padding:4px 9px;border:0;cursor:pointer;
    background:#fff;color:var(--muted)}
  .seg button.on{background:var(--ink);color:var(--paper)}
  input.bad{border-color:#b4453c;background:#fdf5f4}
  /* --- transactions grid: multi-select + bulk action bar --- */
  th.pick,td.pick{width:1px;padding-right:6px}
  td.pick{cursor:default}
  tr.picked td{background:#f3f6fb}
  .bulk{display:flex;flex-wrap:wrap;gap:8px;align-items:center;background:var(--card);
    border:1px solid var(--line);border-radius:12px;padding:10px 14px;margin:0 0 12px}
  /* the bar is a flex box, so `hidden` needs saying twice to win */
  .bulk[hidden]{display:none}
  .bulk .n{font-weight:600;margin-right:4px}
  .bulk .btn{padding:6px 12px;font-size:13px}
  .btn.danger{background:#b4453c;color:#fff}
  .bulk select{padding:6px 9px;font-size:13px}
</style></head><body>
<header><div class=bar>
  <div class=brand>LocalLedger <span>· {{ year }}</span></div>
  <nav>
    <a href="{{ url_for('dashboard') }}" class="{{ 'on' if page=='dash' }}">Dashboard</a>
    <a href="{{ url_for('do_import') }}" class="{{ 'on' if page=='import' }}">Import</a>
    <a href="{{ url_for('review') }}" class="{{ 'on' if page=='review' }}">Review{% if need %} · {{ need }}{% endif %}</a>
    <a href="{{ url_for('categorize') }}" class="{{ 'on' if page=='cat' }}">Categorize</a>
    <a href="{{ url_for('transactions') }}" class="{{ 'on' if page=='txns' }}">Transactions</a>
    <a href="{{ url_for('categories') }}" class="{{ 'on' if page=='cats' }}">Categories</a>
  </nav>
</div></header>
<main>{{ body|safe }}</main>
<script>
/* Transactions grid — inline editing of category / use / note.

   Vanilla JS, no libraries, no CDN, no page reload. The table itself is
   rendered by the server and stays readable with JS off; this only adds
   editing on top of it.

   One shared editor is moved between cells by event delegation rather than a
   widget per row, so a grid with thousands of rows costs three DOM nodes.

   This surface deliberately does NOT teach merchant rules — it corrects one
   row at a time. Rule learning lives on the Categorize page.  */
(function () {
  var grid = document.getElementById("txn-grid");
  if (!grid) return;

  var SAVE_URL = "{{ url_for('transactions_update') }}";
  var src = document.getElementById("cat-list");
  /* Leaves only, each {name, label}: `name` is the wire value the server has
     always been sent, `label` is its full "A \u203a B" path. Headings are not in
     here at all, because nothing is ever assigned to one. */
  var CATS = src ? JSON.parse(src.textContent) : [];

  /* Categories nest now, and this is the seam that was left for it: the
     filter, the list and the cell all read whatever it returns. A path the
     server already labelled comes back untouched. */
  var PATHS = {};
  CATS.forEach(function (c) { PATHS[c.name] = c.label; });
  function label(name) { return PATHS[name] || name; }

  /* ---- the two shared editors ---- */
  var box = document.createElement("div");
  box.className = "cbx";
  var catInput = document.createElement("input");
  catInput.type = "text";
  catInput.autocomplete = "off";
  catInput.setAttribute("aria-label", "Category");
  var list = document.createElement("ul");
  list.className = "cbx-list";
  box.appendChild(catInput);
  box.appendChild(list);

  // One input serves note, date and description — they differ only in where
  // the prefill comes from and whether the server can reject the value.
  var TEXT_FIELDS = {note: "data-note", date: "data-date",
                     description: "data-description"};
  var textInput = document.createElement("input");
  textInput.type = "text";
  textInput.autocomplete = "off";

  // Amount: one editor for the row, opened from either money column, because
  // the magnitude and the column it sits in are a single decision.
  var amt = document.createElement("div");
  amt.className = "amt";
  var amtInput = document.createElement("input");
  amtInput.type = "text";
  amtInput.autocomplete = "off";
  amtInput.setAttribute("aria-label", "Amount");
  var seg = document.createElement("div");
  seg.className = "seg";
  ["debit", "credit"].forEach(function (side) {
    var b = document.createElement("button");
    b.type = "button";
    b.setAttribute("data-side", side);
    b.textContent = side === "debit" ? "Debit" : "Credit";
    seg.appendChild(b);
  });
  amt.appendChild(amtInput);
  amt.appendChild(seg);
  var amtSide = "debit";

  var open = null;      // {cell, field, html} while an editor is mounted
  var matches = [];
  var hi = -1;

  /* ---- painting from server truth ---- */
  function cellOf(tr, field) {
    return tr.querySelector("td.ed[data-field='" + field + "']");
  }

  function usePill(use) {
    // `use` came back from the server, already narrowed to business|personal.
    return use === "business"
      ? "<span class='pill b'>Business</span>"
      : "<span class='pill p'>Personal</span>";
  }

  function statusPill(reviewed) {
    return reviewed
      ? "<span class='pill settled'>Reviewed</span>"
      : "<span class='pill review'>Needs review</span>";
  }

  function setNote(cell, note) {
    cell.textContent = "";
    if (note) {
      cell.textContent = note;          // textContent, never innerHTML
    } else {
      var ph = document.createElement("span");
      ph.className = "ph";
      ph.textContent = "—";
      cell.appendChild(ph);
    }
  }

  function text(cell, value) {
    if (cell) cell.textContent = value;   // textContent, never innerHTML
  }

  function paint(tr, d) {
    tr.dataset.category = d.category;
    tr.dataset.use = d.use;
    tr.dataset.note = d.note;
    tr.dataset.date = d.date;
    tr.dataset.description = d.description;
    tr.dataset.cents = d.amount_cents;
    text(cellOf(tr, "date"), d.date);
    text(cellOf(tr, "description"), d.description);
    text(cellOf(tr, "category"), label(d.category));
    // Both money columns every time, so a sign flip is visibly a move from one
    // column to the other. The strings were formatted by money() server-side.
    text(cellOf(tr, "debit"), d.debit);
    text(cellOf(tr, "credit"), d.credit);
    var u = cellOf(tr, "use");
    if (u) u.innerHTML = usePill(d.use);
    var n = cellOf(tr, "note");
    if (n) setNote(n, d.note);
    var s = tr.querySelector("td.status");
    if (s) s.innerHTML = statusPill(d.reviewed);
  }

  function post(tr, fields) {
    var body = new URLSearchParams();
    body.append("id", tr.dataset.id);
    Object.keys(fields).forEach(function (k) { body.append(k, fields[k]); });
    tr.classList.remove("stale");
    tr.classList.add("saving");
    return fetch(SAVE_URL, {
      method: "POST",
      headers: {"Content-Type": "application/x-www-form-urlencoded"},
      body: body.toString()
    }).then(function (r) {
      tr.classList.remove("saving");
      if (r.ok) return r.json();
      // 400 means the server refused this value and wrote nothing, so the row
      // on screen is still right — the caller keeps the editor up instead.
      // Anything else is a real failure and the row can no longer be trusted.
      if (r.status !== 400) stale(tr);
      return null;
    }).catch(function () {
      tr.classList.remove("saving");
      stale(tr);
      return null;
    });
  }

  function save(tr, field, value) {
    var fields = {};
    fields[field] = value;
    post(tr, fields).then(function (d) {
      if (d) { paint(tr, d); } else { stale(tr); }
    });
  }

  function stale(tr) {
    // Never leave a cell showing a value the database didn't accept.
    tr.classList.remove("saving");
    tr.classList.add("stale");
    tr.title = "Not saved — reload the page.";
  }

  /* ---- editor plumbing ---- */
  function closeEditor() {
    if (!open) return null;
    var o = open;
    open = null;
    o.cell.innerHTML = o.html;   // save() repaints from the response
    return o;
  }

  function settle(o, tr, pending, input) {
    /* What to do once a commit comes back.

       On success the row repaints from the reply. On a refusal the editor
       stays up holding what was typed — a date that won't parse shouldn't
       silently vanish and leave the old one looking accepted. If another
       editor was opened while the request was in flight, this one is stale
       and gets out of the way. */
    return pending.then(function (d) {
      if (d) {
        if (open === o) closeEditor();
        paint(tr, d);
      } else if (open === o) {
        input.className = "bad";
        input.focus();
      }
    });
  }

  /* ---- category: type-ahead combobox ---- */
  function filter(q) {
    var needle = q.trim().toLowerCase();
    matches = CATS.filter(function (c) {
      return !needle || c.label.toLowerCase().indexOf(needle) !== -1;
    });
    hi = matches.length ? 0 : -1;
    draw();
  }

  function draw() {
    list.textContent = "";
    if (!matches.length) {
      var none = document.createElement("li");
      none.className = "none";
      none.textContent = "No matching category";
      list.appendChild(none);
      return;
    }
    matches.forEach(function (c, i) {
      var li = document.createElement("li");
      li.textContent = c.label;
      li.setAttribute("data-name", c.name);
      if (i === hi) li.className = "on";
      list.appendChild(li);
    });
    if (hi >= 0 && list.children[hi].scrollIntoView) {
      list.children[hi].scrollIntoView({block: "nearest"});
    }
  }

  function openCategory(cell) {
    closeEditor();
    var tr = cell.parentNode;
    open = {cell: cell, field: "category", html: cell.innerHTML};
    cell.textContent = "";
    cell.appendChild(box);
    catInput.value = "";
    catInput.placeholder = tr.getAttribute("data-category") || "";
    filter("");
    catInput.focus();
  }

  function chooseCategory(name) {
    if (!open || open.field !== "category") return;
    var tr = open.cell.parentNode;
    closeEditor();
    save(tr, "category", name);
    // Straight down the column: the next row is almost always the next edit.
    var next = tr.nextElementSibling;
    var target = next && cellOf(next, "category");
    if (target) target.focus();
  }

  catInput.addEventListener("input", function () { filter(catInput.value); });

  catInput.addEventListener("keydown", function (e) {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!matches.length) return;
      hi = (hi + (e.key === "ArrowDown" ? 1 : matches.length - 1)) % matches.length;
      draw();
    } else if (e.key === "Enter") {
      e.preventDefault();
      // Only a highlighted, real category can be chosen. Free text never saves.
      if (hi >= 0) chooseCategory(matches[hi].name);   // the NAME goes on the wire
    } else if (e.key === "Escape") {
      e.preventDefault();
      var o = closeEditor();
      if (o) o.cell.focus();
    }
  });

  catInput.addEventListener("blur", function () {
    if (open && open.field === "category") closeEditor();
  });

  // Swallow the mousedown so the input never loses focus — no blur, so the
  // editor is still standing when the click lands on the item below.
  list.addEventListener("mousedown", function (e) { e.preventDefault(); });

  list.addEventListener("click", function (e) {
    var li = e.target.closest("li[data-name]");
    if (li) chooseCategory(li.getAttribute("data-name"));
  });

  /* ---- note / date / description: one plain inline input ---- */
  function openText(cell, field) {
    closeEditor();
    var tr = cell.parentNode;
    open = {cell: cell, field: field, html: cell.innerHTML};
    cell.textContent = "";
    cell.appendChild(textInput);
    textInput.className = "";
    textInput.value = tr.getAttribute(TEXT_FIELDS[field]) || "";
    textInput.focus();
    textInput.select();
  }

  function commitText() {
    if (!open || !TEXT_FIELDS[open.field]) return;
    var o = open, tr = o.cell.parentNode, field = o.field;
    var value = textInput.value;
    if (value.trim() === (tr.getAttribute(TEXT_FIELDS[field]) || "")) {
      closeEditor();          // nothing actually changed
      return;
    }
    var fields = {};
    fields[field] = value;
    settle(o, tr, post(tr, fields), textInput);
  }

  textInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter") {
      e.preventDefault();
      commitText();
    } else if (e.key === "Escape") {
      e.preventDefault();
      // closeEditor() clears `open`, so the blur that follows is a no-op.
      var o = closeEditor();
      if (o) o.cell.focus();
    }
  });

  textInput.addEventListener("blur", commitText);

  /* ---- amount: magnitude in the input, sign in the Debit/Credit toggle ---- */
  function magnitude(cents) {
    // Integer cents only — invariant 1 holds on this side of the wire too.
    var n = Math.abs(cents);
    return Math.floor(n / 100) + "." + ("0" + (n % 100)).slice(-2);
  }

  function drawSides() {
    for (var i = 0; i < seg.children.length; i++) {
      var b = seg.children[i];
      b.className = b.getAttribute("data-side") === amtSide ? "on" : "";
    }
  }

  function openAmount(cell) {
    closeEditor();
    var tr = cell.parentNode;
    var cents = parseInt(tr.getAttribute("data-cents"), 10);
    if (isNaN(cents)) cents = 0;
    open = {cell: cell, field: "amount", html: cell.innerHTML};
    amtSide = cents > 0 ? "credit" : "debit";     // a zero row opens as a debit
    cell.textContent = "";
    cell.appendChild(amt);
    amtInput.className = "";
    amtInput.value = cents ? magnitude(cents) : "";
    drawSides();
    amtInput.focus();
    amtInput.select();
  }

  function commitAmount() {
    if (!open || open.field !== "amount") return;
    var o = open, tr = o.cell.parentNode;
    settle(o, tr, post(tr, {amount: amtInput.value, side: amtSide}), amtInput);
  }

  amtInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter") {
      e.preventDefault();
      commitAmount();
    } else if (e.key === "Escape") {
      e.preventDefault();
      var o = closeEditor();
      if (o) o.cell.focus();
    }
  });

  amtInput.addEventListener("blur", commitAmount);

  // Same trick as the dropdown: the toggle must not take focus, or the input
  // would blur and commit before the new side had been chosen.
  seg.addEventListener("mousedown", function (e) { e.preventDefault(); });

  seg.addEventListener("click", function (e) {
    var b = e.target.closest("button[data-side]");
    if (!b) return;
    amtSide = b.getAttribute("data-side");
    drawSides();
    amtInput.focus();
  });

  /* ---- multi-select + bulk actions ----------------------------------

     Selection is client-only: it lives in the checkboxes and nowhere else, so
     it is gone the moment the page reloads. The bulk actions stamp the rows
     they were handed and teach nothing — the same rule the cell editor above
     follows. Rule learning stays on Categorize.  */
  var BULK_URL = "{{ url_for('transactions_bulk') }}";
  var bar = document.getElementById("bulk-bar");
  var barCount = document.getElementById("bulk-count");
  var barCat = document.getElementById("bulk-cat");
  var selAll = document.getElementById("sel-all");
  var anchor = -1;          // index of the last plain (non-shift) click

  function pickers() {
    // Current DOM order, which is the sort order the server rendered — that
    // is what a shift-click range is expected to mean.
    return Array.prototype.slice.call(grid.querySelectorAll("input.rowsel"));
  }

  function picked() {
    return pickers().filter(function (cb) { return cb.checked; });
  }

  function syncBar() {
    var all = pickers(), on = [];
    all.forEach(function (cb) {
      var tr = cb.closest("tr");
      if (tr) tr.classList.toggle("picked", cb.checked);
      if (cb.checked) on.push(cb);
    });
    if (selAll) {
      selAll.checked = all.length > 0 && on.length === all.length;
      selAll.indeterminate = on.length > 0 && on.length < all.length;
    }
    if (bar) {
      bar.hidden = on.length === 0;          // 0 selected -> no bar at all
      if (barCount) barCount.textContent = on.length + " selected";
    }
    return on;
  }

  function pick(cb, range) {
    var all = pickers(), i = all.indexOf(cb);
    if (range && anchor >= 0 && anchor < all.length) {
      // Shift-click fills the span from the anchor to here; the anchor stays
      // put, so widening and narrowing the range both work.
      var lo = Math.min(anchor, i), up = Math.max(anchor, i);
      for (var k = lo; k <= up; k++) all[k].checked = true;
    } else {
      anchor = i;                            // a plain click sets the anchor
    }
    syncBar();
  }

  function pickAll(on) {
    pickers().forEach(function (cb) { cb.checked = on; });
    anchor = -1;
    syncBar();
  }

  function bulkFail() {
    if (barCount) barCount.textContent = "Not saved — reload the page.";
  }

  function bulk(action, value, ask) {
    var on = picked();
    if (!on.length) return;
    if (ask && !window.confirm(ask.replace("N", on.length))) return;
    var body = new URLSearchParams();
    body.append("action", action);
    if (value !== null) body.append("value", value);
    on.forEach(function (cb) {
      var tr = cb.closest("tr");
      if (tr) body.append("ids", tr.getAttribute("data-id"));
    });
    fetch(BULK_URL, {
      method: "POST",
      headers: {"Content-Type": "application/x-www-form-urlencoded"},
      body: body.toString()
    }).then(function (r) {
      return r.ok ? r.json() : null;
    }).then(function (d) {
      // Repainting N rows here would only re-derive what the server already
      // renders — and after a delete there is nothing to repaint. reload()
      // re-fetches this exact URL, so ?sort= and &dir= survive untouched.
      if (d && d.ok) { window.location.reload(); } else { bulkFail(); }
    }).catch(bulkFail);
  }

  if (bar) {
    bar.addEventListener("click", function (e) {
      var b = e.target.closest("button[data-bulk]");
      if (!b) return;
      var what = b.getAttribute("data-bulk");
      if (what === "category") {
        bulk("category", barCat ? barCat.value : "", null);
      } else if (what === "business" || what === "personal") {
        bulk("use", what, null);
      } else if (what === "delete") {
        bulk("delete", null, "Delete N transactions? This cannot be undone.");
      } else if (what === "clear") {
        pickAll(false);
      }
    });
  }

  // Some browsers restore checkbox state across a reload. Selection is meant
  // to be gone after one, so the grid starts empty-handed every time.
  pickAll(false);

  /* ---- one delegated listener for the whole grid ---- */
  function edit(cell) {
    var field = cell.getAttribute("data-field");
    if (field === "use") {
      var tr = cell.parentNode;
      save(tr, "use", tr.getAttribute("data-use") === "business" ? "personal" : "business");
    } else if (field === "category") {
      openCategory(cell);
    } else if (field === "debit" || field === "credit") {
      openAmount(cell);
    } else if (TEXT_FIELDS[field]) {
      openText(cell, field);
    }
  }

  grid.addEventListener("click", function (e) {
    // The select column is not an editable cell — a click there chooses rows
    // and must never open an editor, so it is handled and dropped first.
    if (selAll && e.target === selAll) { pickAll(selAll.checked); return; }
    var sel = e.target.closest("input.rowsel");
    if (sel) { pick(sel, e.shiftKey); return; }
    if (e.target.closest("td.pick")) return;
    // A pick from the dropdown bubbles through the cell it belongs to; it is
    // the selection, not a fresh click on that cell.
    if (e.target.closest(".cbx, .amt")) return;
    var cell = e.target.closest("td.ed");
    if (!cell) return;
    if (open && open.cell === cell) return;   // a click inside the live editor
    edit(cell);
  });

  grid.addEventListener("keydown", function (e) {
    if (e.key !== "Enter" && e.key !== " ") return;
    if (!e.target.getAttribute || e.target.getAttribute("data-field") === null) return;
    if (open && open.cell === e.target) return;
    e.preventDefault();
    edit(e.target);
  });
})();
</script>
</body></html>
"""


def page(body, page="dash", need=0):
    conn = get_conn()
    need = conn.execute("SELECT COUNT(*) n FROM transactions WHERE reviewed=0").fetchone()["n"]
    return render_template_string(BASE, body=body, page=page, need=need, year=YEAR)


def recent_imports(conn, limit=25):
    """Import batches, newest first, each with its LIVE transaction count.

    The count is computed here rather than read from import_batches.row_count:
    row_count is what the import wrote once and can drift from what is actually
    still attached to the batch.
    """
    return conn.execute(
        """SELECT b.id, b.filename, b.imported_at, b.source_path, a.name AS acct,
                  (SELECT COUNT(*) FROM transactions t
                    WHERE t.import_batch_id = b.id) AS live_count
             FROM import_batches b
             LEFT JOIN accounts a ON a.id = b.account_id
            ORDER BY b.id DESC LIMIT ?""",
        (limit,)).fetchall()


def batch_detail(conn, batch_id):
    """One batch with its live transaction count, or None if it's gone."""
    return conn.execute(
        """SELECT b.id, b.filename, b.imported_at, b.source_path, a.name AS acct,
                  (SELECT COUNT(*) FROM transactions t
                    WHERE t.import_batch_id = b.id) AS live_count
             FROM import_batches b
             LEFT JOIN accounts a ON a.id = b.account_id
            WHERE b.id = ?""",
        (batch_id,)).fetchone()


# ----------------------------- routes ------------------------------------

def spending_rollup(conn):
    """Spending by category, rolled up the tree. Returns (nodes, grand).

    Every transaction sits on a LEAF — nothing is ever assigned to a heading —
    so spend is MEASURED on the leaves and a heading's figure is exactly the
    sum of what sits beneath it. Two consequences worth stating out loud:

      * A heading has no spend of its own to add. Its row and its children's
        rows are the same money at two zoom levels, not more money.
      * Which is why the grand total is computed from the LEAF buckets and
        never from the rows this returns. Adding up every printed row would
        count a nested dollar once per ancestor.

    A node with no spend anywhere beneath it is left out entirely, so an empty
    heading never prints and a flat book renders exactly the rows it always
    did. Amounts are positive integer cents (money out), bucketed by `use`
    exactly as the dashboard has always bucketed them — invariant 1 holds:
    nothing here is ever a float.
    """
    rows = conn.execute(
        """SELECT t.category_id, t.use, a.default_use, t.amount_cents
             FROM transactions t
             JOIN accounts a ON a.id=t.account_id
            WHERE t.amount_cents < 0""").fetchall()

    # A row with no category at all reads as Uncategorized, which is how this
    # table has always shown it. Uncategorized is seeded and cannot be deleted
    # (it is the fallback the whole app resolves to), so this lookup finds it.
    seeded = conn.execute("SELECT id FROM categories WHERE name=?",
                          (db.SYSTEM_LEAF,)).fetchone()
    homeless = seeded["id"] if seeded else None

    leaf = {}                      # category id -> {use: cents}
    for r in rows:
        cid = r["category_id"] if r["category_id"] is not None else homeless
        bucket = leaf.setdefault(cid, {})
        use = txn_use(r)
        bucket[use] = bucket.get(use, 0) + (-r["amount_cents"])

    # The grand total counts each dollar ONCE: it reads the leaf buckets, which
    # are the only place money actually sits, and never the rolled-up rows.
    grand = {"business": 0, "personal": 0}
    for bucket in leaf.values():
        for use in grand:
            grand[use] += bucket.get(use, 0)

    cats = conn.execute("SELECT id, name, parent_id FROM categories").fetchall()
    kids = {}
    for c in cats:
        kids.setdefault(c["parent_id"], []).append(c)

    rolled = {}                    # category id -> {use: cents}, self + descendants

    def gather(cid):
        total = dict(leaf.get(cid, {}))
        for kid in kids.get(cid, []):
            for use, cents in gather(kid["id"]).items():
                total[use] = total.get(use, 0) + cents
        rolled[cid] = total
        return total

    for top in kids.get(None, []):
        gather(top["id"])

    def ranked(cents):
        # The order this table has always used: biggest spender first. Applied
        # within each parent, so a heading's children rank among themselves.
        return -(cents.get("business", 0) + cents.get("personal", 0))

    nodes = []

    def walk(parent_id, depth):
        for kid in sorted((k for k in kids.get(parent_id, []) if rolled.get(k["id"])),
                          key=lambda k: ranked(rolled[k["id"]])):
            cents = rolled[kid["id"]]
            nodes.append({"id": kid["id"], "name": kid["name"], "depth": depth,
                          "business": cents.get("business", 0),
                          "personal": cents.get("personal", 0)})
            walk(kid["id"], depth + 1)

    walk(None, 0)

    # Belt and braces: spend that landed on no category and found no
    # Uncategorized row to adopt it still gets a line, rather than quietly
    # leaving the table while staying in the grand total.
    if homeless is None and None in leaf:
        nodes.append({"id": None, "name": db.SYSTEM_LEAF, "depth": 0,
                      "business": leaf[None].get("business", 0),
                      "personal": leaf[None].get("personal", 0)})
    return nodes, grand


@app.route("/")
def dashboard():
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) n FROM transactions").fetchone()["n"]
    need = conn.execute("SELECT COUNT(*) n FROM transactions WHERE reviewed=0").fetchone()["n"]
    uncat = conn.execute(
        """SELECT COUNT(*) n FROM transactions t LEFT JOIN categories c ON c.id=t.category_id
           WHERE c.name IS NULL OR c.name='Uncategorized'"""
    ).fetchone()["n"]

    nodes, grand = spending_rollup(conn)

    ai = "on" if ollama_available() else "off"
    cards = f"""
      <div class=cards>
        <div class=card><div class=k>Transactions</div><div class="v num">{total}</div></div>
        <div class=card><div class=k>Needs review</div><div class="v num">{need}</div></div>
        <div class=card><div class=k>Uncategorized</div><div class="v num">{uncat}</div></div>
      </div>"""

    if nodes:
        def cat_cell(n):
            # Indentation carries the nesting, so the label stays the node's own
            # name. A top-level row is written exactly as it always was, which
            # is what keeps a flat book's table byte-identical to before.
            label = esc(n["name"])
            return (f"<span style='padding-left:{n['depth'] * 18}px'>{label}</span>"
                    if n["depth"] else label)

        def money_row(cells, cls=""):
            b, p = cells["business"], cells["personal"]
            return (f"<tr{cls}><td>{cells['label']}</td>"
                    f"<td class='r num'>{money(-b)}</td>"
                    f"<td class='r num'>{money(-p)}</td>"
                    f"<td class='r num'>{money(-(b + p))}</td></tr>")

        rowshtml = "".join(
            money_row({"label": cat_cell(n), "business": n["business"],
                       "personal": n["personal"]})
            for n in nodes)
        # Each dollar once: this is the leaf total, not the sum of the rows
        # above it, which would count a nested dollar once per ancestor.
        rowshtml += money_row({"label": "All spending", **grand}, cls=" class=sum")
        spendtbl = f"""<h2>Spending by category</h2>
          <table><tr><th>Category</th><th class=r>Business</th>
          <th class=r>Personal</th><th class=r>Total</th></tr>{rowshtml}</table>"""
    else:
        spendtbl = "<div class=empty>No transactions yet. Import a statement to begin.</div>"

    action = (f"<a class=btn href='{url_for('review')}'>Review {need} →</a>"
              if need else f"<a class='btn ghost' href='{url_for('do_import')}'>Import a statement</a>")

    body = f"""
      <h1>Dashboard</h1>
      <p class=sub>Everything here stays on this machine.
        <span class='status {ai}'>Local AI (Ollama): {ai}</span></p>
      {cards}
      <div style='margin-top:18px'>{action}</div>
      {spendtbl}"""
    return page(body, "dash")


def phase_one(account_id, filename, text, source_path=None):
    """Parse a CSV and render the mapping page. Writes nothing, ever.

    Both ways of choosing a file — the browser upload and the native picker —
    land here, so a natively-picked file gets exactly the same parse, header
    detection, auto-detected mapping and stash as an uploaded one. The single
    difference is `source_path`, which goes into the stash and nowhere else.
    """
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if any(c.strip() for c in r)]
    if not rows:
        return page("<div class=empty>That file looked empty.</div>", "import")

    # Preamble lines above the real header ("Date Range : ...") are discarded,
    # not counted as unreadable rows.
    h = find_header_row(rows)
    header, data = rows[h], rows[h + 1:]
    cols = detect_columns(header)
    if not mapping_is_complete(cols):
        return page(
            "<div class=empty>Couldn't find date / description / amount columns "
            "in that file's header, so there's nothing to map yet. "
            "Check the file and try again.</div>", "import")

    cols["sign"] = "negative"      # a signed amount column, read as written
    token = stash_import(account_id, filename, header, data, h, cols, source_path)
    return page(mapping_body(token, PENDING_IMPORTS[token], cols), "import")


@app.route("/import", methods=["GET", "POST"])
def do_import():
    conn = get_conn()
    accounts = conn.execute("SELECT * FROM accounts ORDER BY name").fetchall()

    if request.method == "POST":
        account_id = int(request.form["account_id"])
        f = request.files.get("file")
        if not f or not f.filename:
            return page("<div class=empty>No file chosen.</div>", "import")
        # A browser upload knows the bytes and the bare filename, never the
        # path — so no source_path, and this batch gets no Reveal button.
        text = f.read().decode("utf-8-sig", errors="replace")
        return phase_one(account_id, f.filename, text)

    opts = "".join(
        f"<option value=\"{a['id']}\">{esc(a['name'])} — {esc(a['default_use'])}</option>"
        for a in accounts)

    # A note handed back by the undo route. It carries a filename, so it is
    # escaped like any other CSV-provided value.
    msg = request.args.get("msg", "")
    note = f"<p class=sub><span class='status off'>{esc(msg)}</span></p>" if msg else ""

    # The picker is an extra button on the very same form, so it carries the
    # account you picked. Off-mac it simply isn't there and the upload — which
    # is the primary path either way — is the whole story.
    picker = ("<button class='btn ghost' formaction=\"%s\" formenctype=\"multipart/form-data\">"
              "Choose file on this Mac…</button>" % url_for("choose_import_file")
              if is_mac() else "")

    batches = recent_imports(conn)
    if batches:
        rows_html = "".join(
            f"<tr><td>{esc(b['filename'])}</td><td>{esc(b['acct'] or '—')}</td>"
            f"<td>{esc(str(b['imported_at'])[:10])}</td>"
            f"<td class='r num'>{b['live_count']}</td>"
            f"<td class=r><div class=rowbtns>{reveal_button(b)}"
            f"<form method=post action=\"{url_for('undo_import')}\">"
            f"<input type=hidden name=batch_id value=\"{b['id']}\">"
            f"<button class='btn ghost'>Undo import</button></form></div></td></tr>"
            for b in batches)
        recent = f"""<h2>Recent imports</h2>
          <table><tr><th>File</th><th>Account</th><th>Imported</th>
            <th class=r>Transactions</th><th class=r></th></tr>{rows_html}</table>"""
    else:
        recent = "<h2>Recent imports</h2><p class=sub>No imports yet.</p>"

    body = f"""
      <h1>Import a statement</h1>
      <p class=sub>Drop in a bank or credit-card CSV. Duplicates are detected automatically,
         and each transaction is categorized before you review it.</p>
      {note}
      <form class=up method=post enctype=multipart/form-data>
        <div><label class=lbl>Account</label><br><select name=account_id>{opts}</select></div>
        <div><label class=lbl>CSV file</label><br><input type=file name=file accept=.csv></div>
        <div class=rowbtns style='justify-content:flex-start'>
          <button class=btn>Import</button>
          {picker}
        </div>
      </form>
      {recent}"""
    return page(body, "import")


def reveal_button(batch):
    """The Reveal button for one import row — nothing at all without a path.

    A browser-uploaded batch has source_path NULL and gets no button; off-mac
    nobody gets one. The button carries the batch id and never a path: what
    Finder is handed is looked up from that row's own source_path column.
    """
    if not batch["source_path"] or not is_mac():
        return ""
    return (f"<form method=post action=\"{url_for('reveal_import')}\">"
            f"<input type=hidden name=batch_id value=\"{batch['id']}\">"
            f"<button class='btn ghost'>Reveal in Finder</button></form>")


@app.route("/import/choose", methods=["POST"])
def choose_import_file():
    """Phase 1 from the native macOS file dialog. Writes nothing.

    Everything after the dialog is the ordinary import: the same parse, the same
    mapping page, the same commit. The only thing gained by coming through here
    is the file's real path, which a browser upload can never tell us.
    """
    try:
        account_id = int(request.form.get("account_id", ""))
    except (TypeError, ValueError):
        return redirect(url_for("do_import"))

    try:
        path = native_choose_file()
    except NotOnMac:
        return not_on_mac("Choosing a file this way needs macOS. "
                          "Use the file upload instead — it does the same import.")
    if not path:
        return redirect(url_for("do_import", msg="No file chosen."))

    try:
        with open(path, "rb") as fh:
            raw = fh.read(MAX_IMPORT_BYTES + 1)
    except OSError:
        return redirect(url_for("do_import", msg="Couldn't read that file."))
    if len(raw) > MAX_IMPORT_BYTES:
        return redirect(url_for("do_import", msg="That file is too large to import."))

    return phase_one(account_id, os.path.basename(path),
                     raw.decode("utf-8-sig", errors="replace"), path)


@app.route("/import/reveal", methods=["POST"])
def reveal_import():
    """Show one import's original file in Finder.

    The path comes from that batch's own source_path column and from nowhere
    else — a path in the request body is ignored entirely — and it reaches
    Finder as one argv element, never a shell string. Every sad path (no batch,
    no path, file moved, not a mac) is a note on the import page, never a 500.
    """
    conn = get_conn()
    try:
        batch_id = int(request.form.get("batch_id", ""))
    except (TypeError, ValueError):
        return redirect(url_for("do_import"))

    row = conn.execute("SELECT source_path FROM import_batches WHERE id=?",
                       (batch_id,)).fetchone()
    if row is None:
        return redirect(url_for("do_import", msg="That import was already removed."))
    if not is_mac():
        return not_on_mac("Reveal is macOS-only.")
    path = row["source_path"]
    if not path:
        return redirect(url_for("do_import", msg="No stored file for this import."))
    if not os.path.exists(path):
        return redirect(url_for("do_import", msg="That file has moved or been deleted."))

    ok = reveal_in_finder(path)
    return redirect(url_for("do_import", msg="Revealed in Finder." if ok
                            else "Finder wouldn't open that file."))


def column_select(name, headers, chosen):
    """A <select> over the file's own column names."""
    opts = "".join(
        f"<option value=\"{i}\" {'selected' if i == chosen else ''}>"
        f"{esc(h.strip() or f'(column {i + 1})')}</option>"
        for i, h in enumerate(headers))
    return f"<select name={name}>{opts}</select>"


# What each money column's header tends to be called, for the fallback below.
MONEY_KEYWORDS = {"amount": ("amount", "amt", "value"),
                  "debit": ("debit", "withdrawal", "charge", "out"),
                  "credit": ("credit", "deposit", "in")}


def money_column(chosen, headers, field, avoid=None):
    """A real column index for a money select — never None.

    Both money groups are always on the page, so a select for the mode the user
    isn't in still has to name a column: a <select> rendered with nothing chosen
    submits its first option anyway, and the page would then be showing one
    mapping while submitting another. Order is what was picked or detected, then
    a keyword guess off the file's own header, then the first column that isn't
    already spoken for. `avoid` keeps Credit off Debit's column.
    """
    if isinstance(chosen, int) and not isinstance(chosen, bool) \
            and 0 <= chosen < len(headers):
        return chosen
    for i, h in enumerate(headers):
        if i != avoid and any(k in h.strip().lower() for k in MONEY_KEYWORDS[field]):
            return i
    return 1 if avoid == 0 and len(headers) > 1 else 0


def profiles_bar(token, selected=""):
    """The Profiles row above the column selects. Plain forms — no JS at all.

    Apply / Delete / Save live INSIDE the mapping form (the caller renders this
    as its first child), so a "Save as" captures exactly the selects that are
    on screen and an Apply carries the token without a round-trip of its own.
    The file import needs multipart and a form cannot nest inside another form,
    so its controls join a sibling `<form id=profile-file>` through the HTML
    `form=` attribute. That keeps one visual bar out of two real forms, with
    nothing scripted: with JS off this behaves identically.
    """
    names = profile_names()
    if names:
        opts = "".join(
            f"<option value=\"{esc(n)}\"{' selected' if n == selected else ''}>"
            f"{esc(n)}</option>" for n in names)
        pick = (f"<select name=profile_name>{opts}</select>"
                f"<button class='btn ghost' formaction=\"{url_for('apply_profile')}\">"
                f"Apply</button>"
                f"<button class='btn ghost' formaction=\"{url_for('delete_profile')}\">"
                f"Delete</button>")
    else:
        pick = "<span class=src>No saved profiles yet.</span>"

    return f"""
        <div class=profiles>
          <label class=lbl>Profiles</label>
          {pick}
          <input type=text name=save_as placeholder="Save this mapping as…">
          <button class='btn ghost' formaction="{url_for('save_profile')}">Save</button>
          <span class=gap></span>
          <a class='btn ghost' href="{url_for('export_profiles')}">Export</a>
          <input type=file name=file accept=.json form=profile-file>
          <button class='btn ghost' form=profile-file>Import</button>
        </div>"""


def mapping_body(token, stash, m, note="", selected=""):
    """The mapping + preview screen. Renders, never writes.

    `note` is the one-line result of a profile action (saved / applied /
    deleted / imported); `selected` keeps that profile chosen in the bar.
    """
    headers, data = stash["header"], stash["data"]

    # Both money groups render whatever the mode is — the script below hides the
    # one you aren't using. Rendering only the active group meant switching the
    # radio submitted a mode whose columns weren't on the page at all.
    signopts = "".join(
        f"<option value={v} {'selected' if m.get('sign') == v else ''}>{label}</option>"
        for v, label in SIGN_CHOICES)
    amount_i = money_column(m["amount"], headers, "amount")
    debit_i = money_column(m["debit"], headers, "debit")
    credit_i = money_column(m["credit"], headers, "credit", avoid=debit_i)
    money_fields = f"""
        <div class=field data-money=single>
          <span><label class=lbl>Amount column</label>
            {column_select('amount_col', headers, amount_i)}</span>
          <span><label class=lbl>Spending shows as</label>
            <select name=sign>{signopts}</select></span>
        </div>
        <div class=field data-money=debitcredit>
          <span><label class=lbl>Debit column</label>
            {column_select('debit_col', headers, debit_i)}</span>
          <span><label class=lbl>Credit column</label>
            {column_select('credit_col', headers, credit_i)}</span>
        </div>"""

    modes = "".join(
        f"<label><input type=radio name=mode value={v} "
        f"{'checked' if m['mode'] == v else ''}><span>{label}</span></label>"
        for v, label in (("single", "Single signed amount"),
                         ("debitcredit", "Separate debit / credit")))

    prows = ""
    for row in data[:PREVIEW_ROWS]:
        got = read_row(row, m)
        if got is None:
            prows += ("<tr><td colspan=3 class=src>row skipped under this mapping "
                      "— unreadable date, description or amount</td></tr>")
            continue
        date, desc, cents = got
        prows += (f"<tr><td>{esc(date)}</td><td>{esc(desc)}</td>"
                  f"<td class='r num'>{money(cents)}</td></tr>")
    if not prows:
        prows = "<tr><td colspan=3 class=src>no data rows below the header</td></tr>"

    pre = stash["preamble"]
    prenote = (f"<p class=sub>skipped {pre} preamble row{'' if pre == 1 else 's'} "
               f"above the header.</p>" if pre else "")

    # Escaped like any other value that came off the page: a note can carry a
    # profile name, and a profile name is user input.
    notehtml = (f"<p class=sub><span class='status off'>{esc(note)}</span></p>"
                if note else "")

    return f"""
      <h1>Check the columns</h1>
      <p class=sub>{esc(stash['filename'])} — nothing has been imported yet.
         Adjust anything that looks wrong, preview it, then import.</p>
      {prenote}
      {notehtml}
      <form method=post id=profile-file enctype=multipart/form-data
            action="{url_for('import_profiles')}">
        <input type=hidden name=token value="{esc(token)}">
      </form>
      <form method=post class=rev id=map-form>
        <input type=hidden name=token value="{esc(token)}">
        <input type=hidden name=account_id value="{stash['account_id']}">
        {profiles_bar(token, selected)}
        <div class=field>
          <span><label class=lbl>Date column</label>
            {column_select('date_col', headers, m['date'])}</span>
          <span><label class=lbl>Description column</label>
            {column_select('desc_col', headers, m['desc'])}</span>
        </div>
        <div class=field><label class=lbl>Money style</label>
          <span class=radio>{modes}</span></div>
        {money_fields}
        <h2>Preview</h2>
        <table><tr><th>Date</th><th>Description</th><th class=r>Amount</th></tr>
          {prows}</table>
        <div class=field style='margin-top:16px'>
          <button class='btn ghost' formaction="{url_for('preview_import')}">Update preview</button>
          <button class=btn formaction="{url_for('commit_import')}">Import</button>
        </div>
      </form>
      <script>
      /* Money style toggle — show the field group for the checked style only.

         Vanilla JS, inline, no libraries: same rules as the Transactions grid.
         Both groups are server-rendered VISIBLE, so with JS off the page still
         works — you fill in the pair you mean and import; nothing here is
         load-bearing. All this does is hide the group you aren't using, and
         switch without a round-trip to the server. */
      (function () {{
        var form = document.getElementById("map-form");
        if (!form) return;
        var radios = form.querySelectorAll("input[name=mode]");
        var groups = form.querySelectorAll("[data-money]");
        function show() {{
          var mode = "single", i;
          for (i = 0; i < radios.length; i++)
            if (radios[i].checked) mode = radios[i].value;
          for (i = 0; i < groups.length; i++)
            /* "" not "block": .field is a flex row in the stylesheet. */
            groups[i].style.display =
              groups[i].getAttribute("data-money") === mode ? "" : "none";
        }}
        for (var k = 0; k < radios.length; k++)
          radios[k].addEventListener("change", show);
        show();
      }})();
      </script>"""


EXPIRED = "That upload expired — please choose the file again."


@app.route("/import/map", methods=["POST"])
def preview_import():
    """Re-render the mapping page under the submitted mapping. Writes nothing."""
    stash = PENDING_IMPORTS.get(request.form.get("token", ""))
    if stash is None:
        return redirect(url_for("do_import", msg=EXPIRED))
    m = effective_mapping(stash["detected"], len(stash["header"]))
    return page(mapping_body(request.form["token"], stash, m), "import")


def profile_form_name(*fields):
    """The profile name this POST is about, from the first field it carries.

    The bar has two name inputs in one form — the Apply/Delete <select> and the
    "Save as" box — so they cannot share an attribute name. Save reads its own
    box first and falls back to `profile_name`, so a bare {token, profile_name}
    post still saves.
    """
    for field in fields:
        if field in request.form:
            return request.form[field].strip()[:MAX_PROFILE_NAME]
    return ""


def profile_page(token, stash, m, note, selected=""):
    return page(mapping_body(token, stash, m, note, selected), "import")


@app.route("/import/profile/save", methods=["POST"])
def save_profile():
    """Save the mapping now on screen under a name. Writes the JSON file only."""
    token = request.form.get("token", "")
    stash = PENDING_IMPORTS.get(token)
    if stash is None:
        return redirect(url_for("do_import", msg=EXPIRED))

    m = effective_mapping(stash["detected"], len(stash["header"]))
    name = profile_form_name("save_as", "profile_name")
    if not name:
        return profile_page(token, stash, m, "Name the profile before saving it.")

    store = load_profiles()
    store["profiles"][name] = mapping_to_profile(m, stash["header"])
    save_profiles(store)
    return profile_page(token, stash, m, f"Saved profile '{name}'.", name)


@app.route("/import/profile/apply", methods=["POST"])
def apply_profile():
    """Re-render the mapping page under a saved profile. Writes nothing."""
    token = request.form.get("token", "")
    stash = PENDING_IMPORTS.get(token)
    if stash is None:
        return redirect(url_for("do_import", msg=EXPIRED))

    name = profile_form_name("profile_name")
    profile = load_profiles()["profiles"].get(name)
    if profile is None:
        # Deleted in another tab, or an empty picker: say so and leave the
        # mapping exactly as the user had it.
        m = effective_mapping(stash["detected"], len(stash["header"]))
        note = f"No profile named '{name}'." if name else "Choose a profile to apply."
        return profile_page(token, stash, m, note)

    m = profile_to_mapping(profile, stash["header"])
    return profile_page(token, stash, m, f"Applied profile '{name}'.", name)


@app.route("/import/profile/delete", methods=["POST"])
def delete_profile():
    """Forget one profile. Touches no ledger data — this is the JSON file only."""
    token = request.form.get("token", "")
    stash = PENDING_IMPORTS.get(token)
    if stash is None:
        return redirect(url_for("do_import", msg=EXPIRED))

    name = profile_form_name("profile_name")
    store = load_profiles()
    if name in store["profiles"]:
        del store["profiles"][name]
        save_profiles(store)
        note = f"Deleted profile '{name}'."
    else:
        note = f"No profile named '{name}'." if name else "Choose a profile to delete."
    m = effective_mapping(stash["detected"], len(stash["header"]))
    return profile_page(token, stash, m, note)


@app.route("/import/profiles/export")
def export_profiles():
    """Hand back the store as a download — a local file copy, not a network call."""
    payload = json.dumps(load_profiles(), indent=2, sort_keys=True)
    return Response(payload, mimetype="application/json", headers={
        "Content-Disposition": 'attachment; filename="localledger-profiles.json"'})


@app.route("/import/profiles/import", methods=["POST"])
def import_profiles():
    """Merge an exported profiles file into this machine's store, by name.

    Everything about the file is suspect: it may not be JSON, may not be a
    store, may hold entries that aren't profiles. Each of those is a note on
    the page, never a traceback, and a file that fails to parse leaves the
    store exactly as it was.
    """
    token = request.form.get("token", "")
    stash = PENDING_IMPORTS.get(token)

    upload = request.files.get("file")
    raw = upload.read(MAX_PROFILE_FILE + 1) if upload and upload.filename else b""
    incoming = None
    if raw and len(raw) <= MAX_PROFILE_FILE:
        try:
            parsed = json.loads(raw.decode("utf-8-sig", errors="replace"))
        except ValueError:
            parsed = None
        if isinstance(parsed, dict) and isinstance(parsed.get("profiles"), dict):
            incoming = parsed["profiles"]

    if incoming is None:
        note = "Couldn't read that profiles file."
    else:
        store = load_profiles()
        merged = merge_profiles(store, incoming)
        save_profiles(store)
        note = f"Imported {merged} profile{'' if merged == 1 else 's'}."

    if stash is None:
        # No live upload to go back to (expired token, or imported from the
        # import page): the note rides the redirect instead.
        return redirect(url_for("do_import", msg=note))
    m = effective_mapping(stash["detected"], len(stash["header"]))
    return profile_page(token, stash, m, note)


@app.route("/import/commit", methods=["POST"])
def commit_import():
    """Phase 2: import the parked file under the mapping the user confirmed.

    This is the only route in the flow that writes. Everything below the mapping
    lookup — dedup_key, merchant_norm, categorization, the INSERT and the
    import_batches row — is the importer as it has always been; the mapping only
    decides which cells are read.
    """
    token = request.form.get("token", "")
    stash = PENDING_IMPORTS.get(token)
    if stash is None:
        return redirect(url_for("do_import", msg=EXPIRED))

    conn = get_conn()
    account_id = stash["account_id"]
    data = stash["data"]
    cols = effective_mapping(stash["detected"], len(stash["header"]))
    if not mapping_is_complete(cols):
        return page(mapping_body(token, stash, cols), "import")

    # Leaves only, here too: the model is offered the assignable categories,
    # and whatever comes back is resolved as a leaf or lands on Uncategorized.
    cat_names = [c["name"] for c in db.leaf_choices(conn)]
    cz = Categorizer(conn, cat_names)
    now = datetime.utcnow().isoformat()
    # source_path is the stash's, which only the native picker ever sets: a
    # browser-uploaded batch stores NULL, because no path was ever known.
    cur = conn.execute(
        """INSERT INTO import_batches(account_id, filename, imported_at, source_path)
           VALUES (?,?,?,?)""",
        (account_id, stash["filename"], now, stash.get("source_path")))
    batch_id = cur.lastrowid
    cat_id = db.category_map(conn)

    added = dups = skipped = 0
    # Three identical $5 coffees on one day are three purchases, not one.
    # Counting occurrences of each base key within THIS file and appending
    # the index keeps them distinct while a re-import of the same file
    # regenerates #1/#2/#3 identically and still dedups (invariant 7).
    # This does assume stable row ordering across re-imports of a file.
    seen = {}
    for row in data:            # file order — the sequence must be stable
        got = read_row(row, cols)
        if got is None:
            skipped += 1
            continue
        date, desc, cents = got

        merchant = normalize_merchant(desc)
        base = f"{account_id}|{date}|{cents}|{desc}"
        seen[base] = seen.get(base, 0) + 1
        dedup = f"{base}#{seen[base]}"

        guess = cz.categorize(desc, cents, merchant)
        cid = db.resolve_leaf(conn, guess["category"]) or cat_id["Uncategorized"]
        try:
            conn.execute(
                """INSERT INTO transactions
                   (account_id, txn_date, description, merchant_norm, amount_cents,
                    use, category_id, category_source, ai_confidence,
                    import_batch_id, dedup_key, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (account_id, date, desc, merchant, cents, guess["use"], cid,
                 guess["source"], guess["confidence"], batch_id, dedup, now))
            added += 1
        except sqlite3.IntegrityError as err:
            if not is_dedup_conflict(err):
                raise      # NOT NULL / FK / anything else is a real failure
            dups += 1      # this exact row of this file is already imported

    conn.execute("UPDATE import_batches SET row_count=?, dup_count=? WHERE id=?",
                 (added, dups, batch_id))
    conn.commit()
    PENDING_IMPORTS.pop(token, None)

    note = f"Imported {added} new · {dups} duplicates skipped"
    if skipped:
        note += f" · {skipped} rows unreadable"
    body = f"""<h1>Import complete</h1><p class=sub>{note}.</p>
      <a class=btn href='{url_for('review')}'>Review new transactions →</a>"""
    return page(body, "import")


@app.route("/import/undo", methods=["POST"])
def undo_import():
    """Undo one import: its transactions, then the batch row. POST only.

    Two steps on purpose. The first post only *shows* what would go; nothing is
    deleted until a second post carries confirm=1. There is no GET handler, so a
    stray link or a crawler can never remove anything.

    Scope is deliberately narrow: an import does not create merchant rules (only
    /review does), so undoing one must not remove any. Categories, accounts and
    merchant_rules are left exactly as they were.
    """
    conn = get_conn()
    try:
        batch_id = int(request.form.get("batch_id", ""))
    except (TypeError, ValueError):
        return redirect(url_for("do_import"))

    batch = batch_detail(conn, batch_id)
    if batch is None:
        # Already undone — a stale button in another tab, not an error.
        return redirect(url_for("do_import", msg="That import was already removed."))

    # Read once, from this batch's own row: the only path this route can ever
    # act on. Nothing in the request body is consulted for it.
    path = batch["source_path"]
    has_file = bool(path) and os.path.exists(path)

    if request.form.get("confirm") != "1":
        # Off by default, and only offered when there really is a file to
        # delete. Unticked, the file on disk is not touched at all.
        filebox = (f"""
            <label class=meta style='display:flex;gap:8px;align-items:flex-start'>
              <input type=checkbox name=delete_file value="1">
              <span>Also delete the original CSV file from disk —
                <code>{esc(path)}</code>. Off by default; the import is removed
                from the ledger either way.</span></label>""" if has_file else "")
        body = f"""
          <h1>Undo this import?</h1>
          <p class=sub>The transactions that came in with this file will be deleted.
             Learned rules, categories and accounts are left alone.</p>
          <div class=rev>
            <div class=top>
              <div><div class=desc>{esc(batch['filename'])}</div>
                   <div class=meta>{esc(batch['acct'] or '—')} · imported
                     {esc(str(batch['imported_at'])[:10])}</div></div>
              <div class='amt num'>{batch['live_count']}</div>
            </div>
            <div class=meta>transactions will be removed</div>
          </div>
          <form method=post action="{url_for('undo_import')}">
            <input type=hidden name=batch_id value="{batch['id']}">
            <input type=hidden name=confirm value="1">
            {filebox}
            <div class=rowbtns style='justify-content:flex-start;margin-top:12px'>
              <button class=btn>Confirm delete</button>
              <a class='btn ghost' href="{url_for('do_import')}">Cancel</a>
            </div>
          </form>"""
        return page(body, "import")

    # Children first — foreign_keys is ON — and both statements land in the same
    # transaction, so the rows and their batch go together or not at all.
    removed = conn.execute(
        "DELETE FROM transactions WHERE import_batch_id=?", (batch_id,)).rowcount
    conn.execute("DELETE FROM import_batches WHERE id=?", (batch_id,))
    conn.commit()

    plural = "" if removed == 1 else "s"
    note = f"Removed {removed} transaction{plural} from {batch['filename']}."

    # Strictly after the ledger removal, which is already committed above and
    # cannot be undone or blocked by anything that happens here. The file is
    # touched only if the box was ticked, and only this batch's own path.
    if request.form.get("delete_file") == "1" and path:
        try:
            os.remove(path)
            note += " The original file was deleted."
        except OSError:
            note += " The original file could not be deleted."

    return redirect(url_for("do_import", msg=note))


@app.route("/review", methods=["GET", "POST"])
def review():
    conn = get_conn()

    if request.method == "POST":
        ids = request.form.getlist("txn_id")
        cat_id = db.category_map(conn)
        for tid in ids:
            row = conn.execute(
                """SELECT t.merchant_norm, a.default_use
                     FROM transactions t JOIN accounts a ON a.id=t.account_id
                    WHERE t.id=?""", (tid,)).fetchone()
            if not row:
                continue
            catname = request.form.get(f"cat_{tid}")
            use = request.form.get(f"use_{tid}")
            note = request.form.get(f"note_{tid}", "").strip()
            if use not in VALID_USES:
                # Anything else would be stored and then silently dropped from the
                # dashboard, which only totals business + personal.
                use = row["default_use"]
            # Leaves only: a heading's name is no more assignable than a name
            # nobody knows, and both land on Uncategorized.
            cid = db.resolve_leaf(conn, catname) or cat_id["Uncategorized"]
            conn.execute(
                """UPDATE transactions
                     SET category_id=?, category_source='user', use=?, note=?, reviewed=1
                   WHERE id=?""",
                (cid, use, note, tid))
            # learn: this merchant -> this category/use, so next time it's rule-matched
            if cid != cat_id["Uncategorized"]:
                teach_merchant_rule(conn, row["merchant_norm"], cid, use)
        conn.commit()
        return redirect(url_for("review"))

    batch = conn.execute(
        """SELECT t.*, a.name acct, a.default_use, c.name catname
             FROM transactions t
             JOIN accounts a ON a.id=t.account_id
             LEFT JOIN categories c ON c.id=t.category_id
            WHERE t.reviewed=0
            ORDER BY t.txn_date, t.id LIMIT 2"""
    ).fetchall()
    remaining = conn.execute("SELECT COUNT(*) n FROM transactions WHERE reviewed=0").fetchone()["n"]
    cats = db.leaf_choices(conn)

    if not batch:
        body = """<h1>Review</h1>
          <div class=empty>Nothing to review — you're all caught up. 🎉<br>
          Import another statement whenever you're ready.</div>"""
        return page(body, "review")

    def card(t):
        suggested = t["catname"] or "Uncategorized"
        opts = leaf_options(cats, suggested)
        use = txn_use(t)
        src = {"rule": "matched a learned rule", "ai": f"AI suggestion · {int((t['ai_confidence'] or 0)*100)}%",
               "none": "no match — pick one", "user": "you set this"}.get(t["category_source"], "")
        return f"""
          <div class=rev>
            <div class=top>
              <div><div class=desc>{esc(t['description'])}</div>
                   <div class=meta>{esc(t['txn_date'])} · {esc(t['acct'])}</div></div>
              <div class='amt num'>{money(t['amount_cents'])}</div>
            </div>
            <input type=hidden name=txn_id value={t['id']}>
            <div class=field>
              <span><label class=lbl>Category</label>
                <select name=cat_{t['id']}>{opts}</select></span>
              <span class=radio>
                <label><input type=radio name=use_{t['id']} value=business {'checked' if use=='business' else ''}><span>Business</span></label>
                <label><input type=radio name=use_{t['id']} value=personal {'checked' if use=='personal' else ''}><span>Personal</span></label>
              </span>
              <span class=src>{src}</span>
            </div>
            <div class=field>
              <label class=lbl>Note</label>
              <input type=text name=note_{t['id']} value="{esc(t['note'])}" placeholder="e.g. tripod mounting screw">
            </div>
          </div>"""

    cardshtml = "".join(card(t) for t in batch)
    body = f"""
      <h1>Review</h1>
      <p class=sub>Two at a time — confirm or change, then move on. {remaining} left.</p>
      <form method=post>{cardshtml}
        <button class=btn>Confirm & next two →</button>
      </form>"""
    return page(body, "review")


LEAVE = "— leave unchanged —"


def leaf_options(cats, selected=None):
    """<option> list for a category picker, from leaf_choices().

    The value is the bare NAME and the text is the full path label. That split
    is the whole trick: nesting changes what a picker SHOWS and never what it
    SENDS, so every form and the grid's autosave keep the contract they had
    before categories could nest. Headings are simply not in the list.
    """
    return "".join(
        f'<option value="{esc(c["name"])}"'
        f'{" selected" if c["name"] == selected else ""}>{esc(c["label"])}</option>'
        for c in cats)


def cat_select(name, cats):
    """Category picker that defaults to leaving the row/group alone."""
    opts = f"<option value=''>{LEAVE}</option>" + leaf_options(cats)
    return f"<select name={name}>{opts}</select>"


def use_select(name, current):
    opts = "".join(
        f"<option value={u} {'selected' if u == current else ''}>{u.title()}</option>"
        for u in VALID_USES)
    return f"<select name={name}>{opts}</select>"


def merchant_groups(conn):
    """Every transaction, bucketed by merchant_norm.

    Groups holding unreviewed rows come first — that is the work — then the
    biggest merchants, then alphabetical so the page is stable between visits.
    """
    rows = conn.execute(
        """SELECT t.*, a.name acct, a.default_use, c.name catname
             FROM transactions t
             JOIN accounts a ON a.id=t.account_id
             LEFT JOIN categories c ON c.id=t.category_id
            ORDER BY t.txn_date, t.id"""
    ).fetchall()
    groups = {}
    for r in rows:
        groups.setdefault(r["merchant_norm"], []).append(r)
    ordered = sorted(
        groups.items(),
        key=lambda kv: (all(r["reviewed"] for r in kv[1]), -len(kv[1]), kv[0]))
    return rows, ordered


def group_default_use(conn, merchant_norm):
    """The account default shared by a merchant's rows, else plain business."""
    uses = {r["default_use"] for r in conn.execute(
        """SELECT DISTINCT a.default_use FROM transactions t
             JOIN accounts a ON a.id=t.account_id
            WHERE t.merchant_norm=?""", (merchant_norm,))}
    if len(uses) == 1:
        only = uses.pop()
        if only in VALID_USES:
            return only
    return "business"        # ambiguous (or odd) — the safer business default


@app.route("/categorize", methods=["GET", "POST"])
def categorize():
    """Categorize a whole merchant at once, with per-row exceptions.

    The distinction this screen exists for: a GROUP action says "this is what
    this merchant is", so it applies to every row of that merchant AND teaches
    the merchant rule; a PER-ROW override says "this one transaction is an
    exception", so it changes that row and deliberately leaves merchant_rules
    alone. That is what lets one Amazon order be business without every future
    Amazon becoming business.

    Only category_id, use, category_source and reviewed are ever written here
    (plus merchant_rules, for group actions). Amounts, dates, descriptions and
    dedup keys are never touched.
    """
    conn = get_conn()

    if request.method == "POST":
        cat_id = db.category_map(conn)
        uncategorized = cat_id["Uncategorized"]
        touched, merchants = set(), 0

        # 1) group actions first, so a row override in the same submit wins.
        indices = sorted(
            int(k[len("group_norm_"):]) for k in request.form
            if k.startswith("group_norm_") and k[len("group_norm_"):].isdigit())
        for i in indices:
            norm = request.form.get(f"group_norm_{i}")
            catname = request.form.get(f"group_cat_{i}", "")
            if not norm or not catname:      # "— leave unchanged —"
                continue
            cid = db.resolve_leaf(conn, catname) or uncategorized
            use = request.form.get(f"group_use_{i}")
            if use not in VALID_USES:
                use = group_default_use(conn, norm)

            ids = [r["id"] for r in conn.execute(
                "SELECT id FROM transactions WHERE merchant_norm=?", (norm,))]
            if not ids:
                continue
            conn.execute(
                """UPDATE transactions
                     SET category_id=?, use=?, category_source='user', reviewed=1
                   WHERE merchant_norm=?""", (cid, use, norm))
            touched.update(ids)
            merchants += 1
            # Same rule as /review: naming a real category teaches it,
            # Uncategorized teaches nothing.
            if cid != uncategorized:
                teach_merchant_rule(conn, norm, cid, use)

        # 2) per-row exceptions. These never write merchant_rules — that is the
        # whole point of an override.
        for key in list(request.form):
            if not key.startswith("row_cat_"):
                continue
            raw = key[len("row_cat_"):]
            if not raw.isdigit():
                continue
            catname = request.form.get(key, "")
            if not catname:
                continue
            tid = int(raw)
            row = conn.execute(
                """SELECT t.use, a.default_use FROM transactions t
                     JOIN accounts a ON a.id=t.account_id WHERE t.id=?""",
                (tid,)).fetchone()
            if row is None:
                continue
            cid = db.resolve_leaf(conn, catname) or uncategorized
            use = request.form.get(f"row_use_{tid}")
            if use not in VALID_USES:
                use = txn_use(row) if txn_use(row) in VALID_USES else "business"
            conn.execute(
                """UPDATE transactions
                     SET category_id=?, use=?, category_source='user', reviewed=1
                   WHERE id=?""", (cid, use, tid))
            touched.add(tid)

        conn.commit()
        return redirect(url_for(
            "categorize",
            msg=f"Updated {len(touched)} transactions across {merchants} merchants."))

    rows, ordered = merchant_groups(conn)
    cats = db.leaf_choices(conn)
    labels = db.category_labels(conn)
    if not rows:
        return page("<h1>Categorize</h1><div class=empty>No transactions yet. "
                    "Import a statement to begin.</div>", "cat")

    need = sum(1 for r in rows if not r["reviewed"])
    msg = request.args.get("msg", "")
    note = f"<p class=sub><span class='status off'>{esc(msg)}</span></p>" if msg else ""

    blocks = ""
    for i, (norm, grows) in enumerate(ordered):
        total = sum(r["amount_cents"] for r in grows)
        left = sum(1 for r in grows if not r["reviewed"])
        trs = ""
        for t in grows:
            tid, use = t["id"], txn_use(t)
            state = ("<span class='pill settled'>Reviewed</span>" if t["reviewed"]
                     else "<span class='pill review'>Needs review</span>")
            trs += (
                f"<tr><td>{esc(t['txn_date'])}</td>"
                f"<td>{esc(t['description'])}</td>"
                f"<td class='r num'>{money(t['amount_cents'])}</td>"
                f"<td>{esc(labels.get(t['category_id'], 'Uncategorized'))}"
                f" · {esc(use.title())}</td>"
                f"<td>{state}</td>"
                f"<td>{cat_select('row_cat_%d' % tid, cats)}</td>"
                f"<td>{use_select('row_use_%d' % tid, use)}</td></tr>")

        blocks += f"""
          <div class=rev>
            <div class=top>
              <div><div class=desc>{esc(norm)}</div>
                   <div class=meta>{len(grows)} transaction{'' if len(grows) == 1 else 's'}
                     · {left} needing review</div></div>
              <div class='amt num'>{money(total)}</div>
            </div>
            <input type=hidden name=group_norm_{i} value="{esc(norm)}">
            <div class=field>
              <label class=lbl>This merchant is</label>
              {cat_select(f'group_cat_{i}', cats)}
              {use_select(f'group_use_{i}', group_default_use(conn, norm))}
              <span class=src>applies to all {len(grows)} and teaches the rule</span>
            </div>
            <table><tr><th>Date</th><th>Description</th><th class=r>Amount</th>
              <th>Now</th><th>Status</th><th>Just this row</th><th>Use</th></tr>
              {trs}</table>
          </div>"""

    body = f"""
      <h1>Categorize</h1>
      <p class=sub>{len(rows)} transactions · {len(ordered)} merchants · {need} needing review.
         Set a merchant once — it applies to every row and is remembered. Change a single
         row instead to make it an exception, which is not remembered.</p>
      {note}
      <form method=post>{blocks}
        <button class=btn>Save changes</button>
      </form>"""
    return page(body, "cat")


# Sorting is server-side and whitelisted: the query string picks a key, never
# a fragment of SQL. Anything unrecognized falls back to SORT_DEFAULT.
#
# `cat` sorts on the full PATH, not the leaf's own name, so a nested leaf sorts
# beneath its parent instead of away under its own initial. The path is built
# by CAT_PATHS below; a parent's path is a prefix of its children's, so plain
# text order already puts a heading's rows together and in tree order.
SORT_COLUMNS = {
    "date": "t.txn_date",
    "desc": "t.description",
    "cat": "COALESCE(cp.path,'Uncategorized')",
    "amount": "t.amount_cents",
}
SORT_DIRS = {"asc": "ASC", "desc": "DESC"}
SORT_DEFAULT = ("date", "DESC")

# Each category's full path as one string, walked down from the top level.
# Written once here because it is fixed SQL, not user input: the only value
# that reaches it is db.CATEGORY_SEP, bound as a parameter by the caller.
CAT_PATHS = """
    WITH RECURSIVE cat_path(id, path) AS (
        SELECT id, name FROM categories WHERE parent_id IS NULL
        UNION ALL
        SELECT c.id, p.path || ? || c.name
          FROM categories c JOIN cat_path p ON c.parent_id = p.id
    )"""


def parse_sort(args):
    """(key, 'ASC'|'DESC') from the query string — both sides whitelisted."""
    key = args.get("sort", "")
    direction = SORT_DIRS.get(args.get("dir", "").lower(), "")
    if key not in SORT_COLUMNS or not direction:
        return SORT_DEFAULT
    return key, direction


def sort_header(label, key, default_dir, sort, direction, cls="", arrow_dir=None):
    """A clickable <th>: flips the active column, else opens on default_dir.

    Debit and Credit are one sort key (`amount`) shown in two columns, so
    without `arrow_dir` they would both claim the arrow at once. Passing it
    lets each own a single direction: money-out on asc, money-in on desc.
    """
    if key == sort:
        nxt = "asc" if direction == "DESC" else "desc"
        if arrow_dir is None or arrow_dir == direction:
            label += " ▲" if direction == "ASC" else " ▼"
    else:
        nxt = default_dir
    href = f"{url_for('transactions')}?sort={key}&amp;dir={nxt}"
    return f"<th{cls}><a href='{href}'>{label}</a></th>"


def script_json(value):
    """JSON for embedding in a <script> block.

    `<` is the only character that can end a script element early, so escaping
    it as \\u003c (still valid JSON) neutralizes `</script>`, `<!--` and
    `<script` in one move. Nothing else in the block can break out.
    """
    return json.dumps(value).replace("<", "\\u003c")


def amount_cells(cents):
    """(debit, credit) display strings for one signed amount.

    The single place that decides which column a value belongs in, so the page
    and the autosave reply can never disagree. Zero shows in neither column.
    """
    return (money(cents) if cents < 0 else "",
            money(cents) if cents > 0 else "")


def edit_cell(field, inner, cls="ed"):
    """An editable grid cell. `inner` is already escaped by the caller."""
    return f"<td class='{cls}' data-field='{field}' tabindex='0'>{inner}</td>"


@app.route("/transactions")
def transactions():
    conn = get_conn()
    sort, direction = parse_sort(request.args)
    # Both halves come from the whitelists above; t.id keeps the order stable.
    order = f"ORDER BY {SORT_COLUMNS[sort]} {direction}, t.id DESC"
    # The separator is bound, not interpolated: db.CATEGORY_SEP stays the one
    # place the path is spelled, in SQL as well as in Python.
    rows = conn.execute(
        f"""{CAT_PATHS}
            SELECT t.*, a.name acct, a.default_use, c.name catname
             FROM transactions t
             JOIN accounts a ON a.id=t.account_id
             LEFT JOIN categories c ON c.id=t.category_id
             LEFT JOIN cat_path cp ON cp.id=t.category_id
            {order}""", (db.CATEGORY_SEP,)
    ).fetchall()
    if not rows:
        return page("<h1>Transactions</h1><div class=empty>No transactions yet.</div>", "txns")

    # Display is the path ("A › B"); the picker still sends the bare name.
    labels = db.category_labels(conn)
    trs = ""
    for t in rows:
        use = txn_use(t)
        catname = labels.get(t["category_id"], "Uncategorized")
        upill = f"<span class='pill {'b' if use=='business' else 'p'}'>{esc(use.title())}</span>"
        state = ("<span class='pill settled'>Reviewed</span>" if t["reviewed"]
                 else "<span class='pill review'>Needs review</span>")
        note = esc(t["note"]) if t["note"] else "<span class=ph>—</span>"
        # One signed amount_cents, shown in two columns. Display only — storage
        # is unchanged, and the debit keeps its minus sign.
        cents = t["amount_cents"]
        debit, credit = amount_cells(cents)
        # data-* carries the row's current values so the editor never has to
        # parse them back out of the cells.
        trs += (f"<tr data-id='{t['id']}' data-category=\"{esc(catname)}\""
                f" data-use=\"{esc(use)}\" data-note=\"{esc(t['note'])}\""
                f" data-date=\"{esc(t['txn_date'])}\""
                f" data-description=\"{esc(t['description'])}\""
                f" data-cents='{cents}'>"
                # Not an editable cell: clicking here selects the row, and the
                # grid's delegation drops it before any editor can open.
                + "<td class=pick><input type=checkbox class=rowsel"
                  " aria-label='Select row'></td>"
                + edit_cell("date", esc(t["txn_date"]))
                + edit_cell("description", esc(t["description"]))
                + edit_cell("category", esc(catname))
                + edit_cell("use", upill)
                + f"<td class='status'>{state}</td>"
                + edit_cell("note", note)
                + edit_cell("debit", debit, cls="r num ed")
                + edit_cell("credit", credit, cls="r num ed") + "</tr>")
    # Debit opens on asc (biggest money out on top), Credit on desc (biggest in).
    heads = ("<th class=pick><input type=checkbox id=sel-all"
             " aria-label='Select all rows'></th>"
             + sort_header("Date", "date", "desc", sort, direction)
             + sort_header("Description", "desc", "asc", sort, direction)
             + sort_header("Category", "cat", "asc", sort, direction)
             + "<th>Use</th><th>Status</th><th>Note</th>"
             + sort_header("Debit", "amount", "asc", sort, direction,
                           cls=" class=r", arrow_dir="ASC")
             + sort_header("Credit", "amount", "desc", sort, direction,
                           cls=" class=r", arrow_dir="DESC"))
    # The picker's whole vocabulary, handed over once. No fetch, no round trip.
    # Leaves only, each as {name, label}: nothing is ever assigned to a heading,
    # so a heading is not offered anywhere a category can be chosen.
    choices = db.leaf_choices(conn)
    cats = script_json([{"name": c["name"], "label": c["label"]} for c in choices])
    # A plain <select> for the bulk category. The type-ahead combobox stays on
    # the cells, where you are picking for one row at a time.
    options = leaf_options(choices)
    # Rendered hidden and stays hidden without JS: bulk needs the checkboxes,
    # and the checkboxes need JS. The table itself still reads fine either way.
    bulk_bar = f"""<div id="bulk-bar" class=bulk hidden>
        <span class=n id="bulk-count">0 selected</span>
        <select id="bulk-cat" aria-label="Category for the selected rows">{options}</select>
        <button type=button class=btn data-bulk="category">Apply</button>
        <button type=button class="btn ghost" data-bulk="business">Business</button>
        <button type=button class="btn ghost" data-bulk="personal">Personal</button>
        <button type=button class="btn danger" data-bulk="delete">Delete</button>
        <button type=button class="btn ghost" data-bulk="clear">Clear</button>
      </div>"""
    body = f"""<h1>Transactions</h1>
      <p class=sub>{len(rows)} transactions in {YEAR}. Click any cell but Status
      to edit it — changes save as you go and stay on this row. Tick the boxes
      (shift-click for a range) to set a category or use on many rows at once.</p>
      <script type="application/json" id="cat-list">{cats}</script>
      {bulk_bar}
      <table id="txn-grid"><tr>{heads}</tr>{trs}</table>"""
    return page(body, "txns")


@app.route("/transactions/update", methods=["POST"])
def transactions_update():
    """Autosave one field of one row from the transactions grid.

    Partial by design: only the fields actually present in the form are
    written, so the grid can send `use` alone without disturbing a note.

    This endpoint NEVER calls teach_merchant_rule. The grid is a corrections
    surface — it fixes the row in front of you and says nothing about what the
    merchant means. Teaching stays on /categorize and /review, where the user
    is deliberately deciding for a whole merchant.

    It NEVER writes dedup_key. That key is frozen at import: it is what makes
    a re-imported statement skip rows you already have, so correcting a typo in
    an amount or a date must not change it. merchant_norm is different — it is
    a live matching key, not an identity, so an edited description recomputes
    it. Same no-CSRF posture as every other POST here — a single-user app bound
    to localhost.

    Validation all happens before the single UPDATE, so a rejected field means
    nothing at all was written, not a half-applied row.
    """
    conn = get_conn()
    row = conn.execute(
        """SELECT t.id, a.default_use
             FROM transactions t JOIN accounts a ON a.id=t.account_id
            WHERE t.id=?""", (request.form.get("id", ""),)).fetchone()
    if row is None:
        return jsonify(ok=False, error="unknown transaction"), 404

    sets, vals = [], []
    if "category" in request.form:
        cat_id = db.category_map(conn)
        # Leaves only. A heading's name is not assignable, and neither is a
        # name nobody knows; both land on Uncategorized, the same fallback the
        # importer and /review use, and neither is an error the user can see.
        cid = db.resolve_leaf(conn, request.form["category"]) or cat_id["Uncategorized"]
        sets += ["category_id=?", "category_source='user'", "reviewed=1"]
        vals.append(cid)
    if "use" in request.form:
        use = request.form["use"]
        if use not in VALID_USES:
            # Same rule as /review: anything else would be stored and then
            # silently dropped from the dashboard's business/personal totals.
            use = row["default_use"]
        sets.append("use=?")
        vals.append(use)
    if "note" in request.form:
        sets.append("note=?")
        vals.append(request.form["note"].strip())
    if "date" in request.form:
        stamp = normalize_date(request.form["date"].strip())
        if stamp is None:
            return jsonify(ok=False, error="unreadable date"), 400
        sets.append("txn_date=?")
        vals.append(stamp)
    if "amount" in request.form:
        # Magnitude from the text, sign from the column — the two Debit/Credit
        # cells are the only thing that decides in or out.
        cents = parse_amount_to_cents(request.form["amount"])
        side = request.form.get("side")
        if cents is None:
            return jsonify(ok=False, error="unreadable amount"), 400
        if side not in ("debit", "credit"):
            return jsonify(ok=False, error="side must be debit or credit"), 400
        magnitude = abs(cents)      # integer cents throughout — invariant 1
        sets.append("amount_cents=?")
        vals.append(-magnitude if side == "debit" else magnitude)
    if "description" in request.form:
        desc = request.form["description"].strip()
        if not desc:
            return jsonify(ok=False, error="description cannot be empty"), 400
        # merchant_norm follows the description because it is a matching key
        # derived from it. Deriving it is not teaching: no merchant_rules row
        # is written here, and none ever is on this path.
        sets += ["description=?", "merchant_norm=?"]
        vals += [desc, normalize_merchant(desc)]

    if sets:
        conn.execute(f"UPDATE transactions SET {', '.join(sets)} WHERE id=?",
                     vals + [row["id"]])
        conn.commit()

    # Answer with what is actually stored, not with what was asked for, so the
    # grid repaints from the database rather than from its own optimism.
    saved = conn.execute(
        """SELECT t.id, t.txn_date, t.description, t.amount_cents, t.use, t.note,
                  t.reviewed, t.category_id, a.default_use, c.name catname
             FROM transactions t
             JOIN accounts a ON a.id=t.account_id
             LEFT JOIN categories c ON c.id=t.category_id
            WHERE t.id=?""", (row["id"],)).fetchone()
    # money() runs here, not in the browser: the client does no money math.
    debit, credit = amount_cells(saved["amount_cents"])
    # The reply's `category` is for DISPLAY, so it is the path label; the
    # picker goes on sending the bare name.
    labels = db.category_labels(conn)
    return jsonify(ok=True, id=saved["id"],
                   category=labels.get(saved["category_id"], "Uncategorized"),
                   use=txn_use(saved), reviewed=saved["reviewed"],
                   note=saved["note"], date=saved["txn_date"],
                   description=saved["description"],
                   amount_cents=saved["amount_cents"],
                   debit=debit, credit=credit)


BULK_ACTIONS = ("category", "use", "delete")


@app.route("/transactions/bulk", methods=["POST"])
def transactions_bulk():
    """Apply one action to many rows at once from the transactions grid.

    Form: `ids` repeated once per row (the grid appends one field per checked
    box — comma-joined ids are NOT accepted), `action` in BULK_ACTIONS, and
    `value` for category/use.

    Like /transactions/update, this NEVER calls teach_merchant_rule. Bulk is
    still the corrections surface: stamping forty rows with a category says
    nothing about what those merchants mean, and forty rows is exactly where a
    silent rule would do the most damage. Teaching stays on /categorize.

    Delete is a real delete — the dedup_key goes with the row, so re-importing
    the same statement will bring those lines back. That is the same bargain as
    undoing an import, and it is why nothing here touches import_batches: the
    import history computes its counts live from the transactions still
    present, so a batch whose rows you bulk-deleted reports itself honestly
    without any bookkeeping on this side.

    SQL safety: the IN (...) clause is a run of `?` placeholders bound to ints
    that survived int() — no id ever reaches the SQL string. `action` and `use`
    are whitelisted; a category is a dict lookup, never text in a query. Same
    no-CSRF posture as every other POST here — single user, bound to localhost.

    Validation all happens before the single statement, so a rejected request
    wrote nothing at all.
    """
    conn = get_conn()

    ids = []
    for raw in request.form.getlist("ids"):
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            continue        # anything that is not an integer is simply not an id
    if not ids:
        return jsonify(ok=False, error="no valid ids"), 400

    action = request.form.get("action", "")
    if action not in BULK_ACTIONS:
        return jsonify(ok=False, error="unknown action"), 400

    holes = ",".join("?" * len(ids))     # placeholders, never values
    if action == "category":
        cat_id = db.category_map(conn)
        # Leaves only, and an unknown name lands on Uncategorized — the same
        # fallback the importer, /review and the single-row grid edit already
        # use. Forty rows is exactly where a heading must not be stampable.
        cid = (db.resolve_leaf(conn, request.form.get("value", ""))
               or cat_id["Uncategorized"])
        affected = conn.execute(
            f"""UPDATE transactions
                   SET category_id=?, category_source='user', reviewed=1
                 WHERE id IN ({holes})""", [cid] + ids).rowcount
    elif action == "use":
        use = request.form.get("value", "")
        if use not in VALID_USES:
            return jsonify(ok=False, error="use must be business or personal"), 400
        # reviewed is left alone, exactly as a single-row use edit leaves it:
        # which pocket a charge came out of is not a decision about what it is.
        affected = conn.execute(
            f"UPDATE transactions SET use=? WHERE id IN ({holes})",
            [use] + ids).rowcount
    else:
        affected = conn.execute(
            f"DELETE FROM transactions WHERE id IN ({holes})", ids).rowcount
    conn.commit()

    return jsonify(ok=True, action=action, affected=affected)


# --------------------------- category management ---------------------------
# The tree lives behind four plain server forms — add, rename, move, delete —
# with no JavaScript anywhere near it. Every one of them can only refuse in one
# way: it returns a sentence, the page renders it as a note, and nothing was
# written. There is no 500 on this page and no half-applied change.
#
# Two rules do most of the work here:
#   * A category that HOLDS transactions cannot be given a child. A heading's
#     total is the sum of its children, so those rows would sit outside every
#     child and silently stop adding up. Move them first.
#   * Uncategorized is the system leaf. It is where every unresolvable name
#     lands, so it can never be renamed, moved, deleted, or given children.


def category_row(conn, raw_id):
    """One category by id from a form field, or None if that is not one."""
    try:
        cid = int(raw_id)
    except (TypeError, ValueError):
        return None
    return conn.execute(
        "SELECT id, name, parent_id FROM categories WHERE id=?", (cid,)).fetchone()


def name_taken(conn, name):
    """Names are globally unique (UNIQUE(name)) whatever the tree looks like."""
    return conn.execute(
        "SELECT 1 FROM categories WHERE name=? LIMIT 1", (name,)).fetchone() is not None


def direct_txns(conn, cat_id):
    """Transactions pointed straight at this category — not at its children."""
    return conn.execute(
        "SELECT COUNT(*) n FROM transactions WHERE category_id=?",
        (cat_id,)).fetchone()["n"]


def plural(n, one, many):
    return f"{n} {one if n == 1 else many}"


def parent_block(conn, parent):
    """Why `parent` cannot be given a child right now, or "" if it can.

    Add and Move ask exactly the same question, so they ask it in one place.
    """
    if parent["name"] == db.SYSTEM_LEAF:
        return f"{db.SYSTEM_LEAF} is a system category — it cannot be given subcategories."
    held = direct_txns(conn, parent["id"])
    if held:
        return (f"{parent['name']} has {plural(held, 'transaction', 'transactions')} — "
                "move them to a subcategory or elsewhere first.")
    return ""


def category_add(conn):
    name = request.form.get("name", "").strip()
    if not name:
        return "A category needs a name — nothing was added."
    if name_taken(conn, name):
        return f"There is already a category called {name} — nothing was added."
    parent_id = None
    raw = request.form.get("parent_id", "").strip()
    if raw:
        parent = category_row(conn, raw)
        if parent is None:
            return "That parent category no longer exists — nothing was added."
        blocked = parent_block(conn, parent)
        if blocked:
            return blocked
        parent_id = parent["id"]
    conn.execute("INSERT INTO categories(name, parent_id) VALUES (?,?)", (name, parent_id))
    conn.commit()
    return f"Added {name}."


def category_rename(conn):
    node = category_row(conn, request.form.get("id"))
    if node is None:
        return "That category no longer exists — nothing was renamed."
    if node["name"] == db.SYSTEM_LEAF:
        return f"{db.SYSTEM_LEAF} is a system category — it cannot be renamed."
    name = request.form.get("name", "").strip()
    if not name:
        return "A category needs a name — nothing was renamed."
    if name == node["name"]:
        return f"{name} already has that name — nothing was renamed."
    if name_taken(conn, name):
        return f"There is already a category called {name} — nothing was renamed."
    conn.execute("UPDATE categories SET name=? WHERE id=?", (name, node["id"]))
    conn.commit()
    return f"Renamed {node['name']} to {name}."


def category_move(conn):
    node = category_row(conn, request.form.get("id"))
    if node is None:
        return "That category no longer exists — nothing was moved."
    if node["name"] == db.SYSTEM_LEAF:
        return f"{db.SYSTEM_LEAF} is a system category — it cannot be moved."
    raw = request.form.get("parent_id", "").strip()
    if not raw:
        conn.execute("UPDATE categories SET parent_id=NULL WHERE id=?", (node["id"],))
        conn.commit()
        return f"Moved {node['name']} to the top level."
    parent = category_row(conn, raw)
    if parent is None:
        return "That parent category no longer exists — nothing was moved."
    if parent["id"] == node["id"]:
        return f"{node['name']} cannot be its own parent — nothing was moved."
    if db.is_descendant(conn, parent["id"], node["id"]):
        return (f"{parent['name']} already sits under {node['name']} — that move "
                "would make a loop, so nothing was moved.")
    blocked = parent_block(conn, parent)
    if blocked:
        return blocked
    conn.execute("UPDATE categories SET parent_id=? WHERE id=?", (parent["id"], node["id"]))
    conn.commit()
    return f"Moved {node['name']} under {parent['name']}."


def category_delete(conn):
    """Only a genuinely unused leaf goes. Anything pointing at it blocks."""
    node = category_row(conn, request.form.get("id"))
    if node is None:
        return "That category no longer exists — nothing was deleted."
    if node["name"] == db.SYSTEM_LEAF:
        return f"{db.SYSTEM_LEAF} is a system category — it cannot be deleted."
    kids = len(db.children(conn, node["id"]))
    if kids:
        return (f"{node['name']} has {plural(kids, 'subcategory', 'subcategories')} — "
                "nothing was deleted.")
    held = direct_txns(conn, node["id"])
    if held:
        return (f"{node['name']} has {plural(held, 'transaction', 'transactions')} — "
                "nothing was deleted.")
    rules = conn.execute(
        "SELECT COUNT(*) n FROM merchant_rules WHERE category_id=?",
        (node["id"],)).fetchone()["n"]
    if rules:
        return f"{node['name']} is used by a merchant rule — nothing was deleted."
    try:
        conn.execute("DELETE FROM categories WHERE id=?", (node["id"],))
    except sqlite3.IntegrityError:
        # The guards above cover everything M1 writes; a stub table (allocations)
        # could still hold a reference in a later milestone. A refusal on this
        # page is a sentence, never a stack trace.
        conn.rollback()
        return f"{node['name']} is still referenced — nothing was deleted."
    conn.commit()
    return f"Deleted {node['name']}."


CATEGORY_ACTIONS = {"add": category_add, "rename": category_rename,
                    "move": category_move, "delete": category_delete}

TOP_LEVEL = "— top level —"


def category_paths(conn):
    """[(label, id)] for every category, by label. Built once per page."""
    labels = db.category_labels(conn)
    return sorted(((labels[r["id"]], r["id"])
                   for r in conn.execute("SELECT id FROM categories")),
                  key=lambda pair: pair[0])


def parent_options(paths, exclude=None, selected=None):
    """A <select> body of every category by path, for Add's and Move's parent.

    Everything is offered, including a category that cannot take a child right
    now: the refusal is a sentence the page shows, not an option quietly
    missing from a list.
    """
    opts = f"<option value=''>{TOP_LEVEL}</option>"
    for label, cid in paths:
        if cid == exclude:
            continue
        opts += (f"<option value='{cid}'"
                 f"{' selected' if cid == selected else ''}>{esc(label)}</option>")
    return opts


@app.route("/categories", methods=["GET", "POST"])
def categories():
    """The category tree: what it is, and the four ways to change it."""
    conn = get_conn()

    if request.method == "POST":
        handler = CATEGORY_ACTIONS.get(request.form.get("action", ""))
        msg = handler(conn) if handler else "Unknown action — nothing was changed."
        return redirect(url_for("categories", msg=msg))

    msg = request.args.get("msg", "")
    note = f"<p class=sub><span class='status off'>{esc(msg)}</span></p>" if msg else ""

    paths = category_paths(conn)
    trs = ""
    for node in db.category_tree(conn):
        locked = node["name"] == db.SYSTEM_LEAF
        # The settled green is reserved for reviewed/reconciled state, so the
        # tree marks structure quietly: a heading is called out, a leaf is the
        # ordinary case and reads as muted text.
        kind = ("<span class=meta>Leaf</span>" if node["is_leaf"]
                else "<span class='pill b'>Heading</span>")
        indent = f"padding-left:{node['depth'] * 18}px"
        # A heading holding transactions is not reachable through this page —
        # it is shown rather than hidden so an odd book is visible.
        held = node["direct_txn_count"]
        if locked:
            actions = ("<td colspan=3 class=meta>System category — it is where "
                       "anything unresolved lands, so it stays exactly as it is.</td>")
        else:
            actions = (
                "<td><form method=post>"
                "<input type=hidden name=action value=rename>"
                f"<input type=hidden name=id value={node['id']}>"
                f"<input type=text name=name value=\"{esc(node['name'])}\" "
                "aria-label='New name'>"
                "<button class='btn ghost'>Rename</button></form></td>"
                "<td><form method=post>"
                "<input type=hidden name=action value=move>"
                f"<input type=hidden name=id value={node['id']}>"
                "<select name=parent_id aria-label='New parent'>"
                f"{parent_options(paths, exclude=node['id'], selected=node['parent_id'])}"
                "</select>"
                "<button class='btn ghost'>Move</button></form></td>"
                "<td><form method=post>"
                "<input type=hidden name=action value=delete>"
                f"<input type=hidden name=id value={node['id']}>"
                "<button class='btn danger'>Delete</button></form></td>")
        trs += (f"<tr><td><span style='{indent}'>{esc(node['name'])}</span></td>"
                f"<td>{kind}</td><td class='r num'>{held}</td>"
                f"<td class='r num'>{node['child_count']}</td>{actions}</tr>")

    body = f"""<h1>Categories</h1>
      <p class=sub>Transactions are filed on the leaves. A category with
      subcategories is a heading — its total is the sum of what sits under it,
      and nothing is ever filed on it directly. Which is why a category holding
      transactions cannot be given a subcategory until those rows are moved.</p>
      {note}
      <form method=post class=field>
        <input type=hidden name=action value=add>
        <span><label class=lbl>New category</label>
          <input type=text name=name aria-label='New category name'></span>
        <span><label class=lbl>Inside</label>
          <select name=parent_id aria-label='Parent category'>
            {parent_options(paths)}</select></span>
        <button class=btn>Add</button>
      </form>
      <table><tr><th>Category</th><th>Type</th><th class=r>Transactions</th>
        <th class=r>Subcategories</th><th>Rename</th><th>Move</th><th>Delete</th></tr>
        {trs}</table>"""
    return page(body, "cats")


def normalize_date(raw):
    """Return YYYY-MM-DD, or None if nothing recognizes it.

    US-first on purpose: 03/04/2026 is March 4. Ambiguity is resolved, not
    rejected — only genuinely unparseable dates return None, and txn_date is
    documented as YYYY-MM-DD so a raw string must never be stored there.
    """
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%d/%m/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


if __name__ == "__main__":
    db.init_db(DB_PATH)
    print(f"LocalLedger running →  http://127.0.0.1:5000   (database: {DB_PATH})")
    app.run(host="127.0.0.1", port=5000, debug=False)
