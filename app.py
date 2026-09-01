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
"""

import csv
import html
import io
import json
import os
import secrets
import sqlite3
from datetime import datetime
from decimal import Decimal, InvalidOperation

from flask import Flask, request, redirect, url_for, render_template_string, jsonify

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


def stash_import(account_id, filename, header, data, preamble, detected):
    """Park a parsed upload for phase 2 and return its token."""
    while len(PENDING_IMPORTS) >= MAX_PENDING:
        PENDING_IMPORTS.pop(next(iter(PENDING_IMPORTS)))     # oldest first
    token = secrets.token_urlsafe(16)
    PENDING_IMPORTS[token] = {
        "account_id": account_id, "filename": filename, "header": header,
        "data": data, "preamble": preamble, "detected": detected,
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


def read_row(row, m):
    """One data row under a mapping -> (date, desc, cents), or None if unreadable.

    The skip rules are the ones the importer has always used: a short row, an
    unparseable date, an empty description or an unparseable amount is skipped.
    Cents come from the existing parsers — parse_amount_to_cents for a single
    signed column, split_amount_to_cents for a Debit/Credit pair — never from
    anything new. The preview and the commit both go through here, so what you
    see on the mapping page is what gets written.
    """
    try:
        raw_date = row[m["date"]].strip()
        desc = row[m["desc"]].strip()
        if m["mode"] == "single":
            cents = parse_amount_to_cents(row[m["amount"]])
            # A statement that writes spending as positive numbers is the same
            # file with every sign flipped; income keeps its own (opposite) sign.
            if cents is not None and m.get("sign") == "positive":
                cents = -cents
        else:
            cents = split_amount_to_cents(row[m["debit"]], row[m["credit"]])
    except IndexError:
        return None
    date = normalize_date(raw_date)
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
</style></head><body>
<header><div class=bar>
  <div class=brand>LocalLedger <span>· {{ year }}</span></div>
  <nav>
    <a href="{{ url_for('dashboard') }}" class="{{ 'on' if page=='dash' }}">Dashboard</a>
    <a href="{{ url_for('do_import') }}" class="{{ 'on' if page=='import' }}">Import</a>
    <a href="{{ url_for('review') }}" class="{{ 'on' if page=='review' }}">Review{% if need %} · {{ need }}{% endif %}</a>
    <a href="{{ url_for('categorize') }}" class="{{ 'on' if page=='cat' }}">Categorize</a>
    <a href="{{ url_for('transactions') }}" class="{{ 'on' if page=='txns' }}">Transactions</a>
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
  var CATS = src ? JSON.parse(src.textContent) : [];

  /* A category is its bare name today. When categories nest, this is the only
     place that has to learn to say "Parent > Child" — the filter, the list and
     the cell all read whatever it returns. */
  function label(name) { return name; }

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

  var noteInput = document.createElement("input");
  noteInput.type = "text";
  noteInput.autocomplete = "off";
  noteInput.setAttribute("aria-label", "Note");

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

  function paint(tr, d) {
    tr.dataset.category = d.category;
    tr.dataset.use = d.use;
    tr.dataset.note = d.note;
    var c = cellOf(tr, "category");
    if (c) c.textContent = label(d.category);
    var u = cellOf(tr, "use");
    if (u) u.innerHTML = usePill(d.use);
    var n = cellOf(tr, "note");
    if (n) setNote(n, d.note);
    var s = tr.querySelector("td.status");
    if (s) s.innerHTML = statusPill(d.reviewed);
  }

  function save(tr, field, value) {
    var body = new URLSearchParams();
    body.append("id", tr.dataset.id);
    body.append(field, value);
    tr.classList.remove("stale");
    tr.classList.add("saving");
    fetch(SAVE_URL, {
      method: "POST",
      headers: {"Content-Type": "application/x-www-form-urlencoded"},
      body: body.toString()
    }).then(function (r) {
      return r.ok ? r.json() : null;
    }).then(function (d) {
      tr.classList.remove("saving");
      if (d && d.ok) { paint(tr, d); } else { stale(tr); }
    }).catch(function () {
      tr.classList.remove("saving");
      stale(tr);
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

  /* ---- category: type-ahead combobox ---- */
  function filter(q) {
    var needle = q.trim().toLowerCase();
    matches = CATS.filter(function (name) {
      return !needle || label(name).toLowerCase().indexOf(needle) !== -1;
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
    matches.forEach(function (name, i) {
      var li = document.createElement("li");
      li.textContent = label(name);
      li.setAttribute("data-name", name);
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
      if (hi >= 0) chooseCategory(matches[hi]);
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

  /* ---- note: plain inline input ---- */
  function openNote(cell) {
    closeEditor();
    var tr = cell.parentNode;
    open = {cell: cell, field: "note", html: cell.innerHTML};
    cell.textContent = "";
    cell.appendChild(noteInput);
    noteInput.value = tr.getAttribute("data-note") || "";
    noteInput.focus();
    noteInput.select();
  }

  function commitNote() {
    if (!open || open.field !== "note") return;
    var tr = open.cell.parentNode;
    var value = noteInput.value;
    closeEditor();
    if (value.trim() !== (tr.getAttribute("data-note") || "")) save(tr, "note", value);
  }

  noteInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter") {
      e.preventDefault();
      commitNote();
    } else if (e.key === "Escape") {
      e.preventDefault();
      // closeEditor() clears `open`, so the blur that follows is a no-op.
      var o = closeEditor();
      if (o) o.cell.focus();
    }
  });

  noteInput.addEventListener("blur", commitNote);

  /* ---- one delegated listener for the whole grid ---- */
  function edit(cell) {
    var field = cell.getAttribute("data-field");
    if (field === "use") {
      var tr = cell.parentNode;
      save(tr, "use", tr.getAttribute("data-use") === "business" ? "personal" : "business");
    } else if (field === "category") {
      openCategory(cell);
    } else if (field === "note") {
      openNote(cell);
    }
  }

  grid.addEventListener("click", function (e) {
    // A pick from the dropdown bubbles through the cell it belongs to; it is
    // the selection, not a fresh click on that cell.
    if (e.target.closest(".cbx")) return;
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
        """SELECT b.id, b.filename, b.imported_at, a.name AS acct,
                  (SELECT COUNT(*) FROM transactions t
                    WHERE t.import_batch_id = b.id) AS live_count
             FROM import_batches b
             LEFT JOIN accounts a ON a.id = b.account_id
            ORDER BY b.id DESC LIMIT ?""",
        (limit,)).fetchall()


def batch_detail(conn, batch_id):
    """One batch with its live transaction count, or None if it's gone."""
    return conn.execute(
        """SELECT b.id, b.filename, b.imported_at, a.name AS acct,
                  (SELECT COUNT(*) FROM transactions t
                    WHERE t.import_batch_id = b.id) AS live_count
             FROM import_batches b
             LEFT JOIN accounts a ON a.id = b.account_id
            WHERE b.id = ?""",
        (batch_id,)).fetchone()


# ----------------------------- routes ------------------------------------

@app.route("/")
def dashboard():
    conn = get_conn()
    total = conn.execute("SELECT COUNT(*) n FROM transactions").fetchone()["n"]
    need = conn.execute("SELECT COUNT(*) n FROM transactions WHERE reviewed=0").fetchone()["n"]
    uncat = conn.execute(
        """SELECT COUNT(*) n FROM transactions t LEFT JOIN categories c ON c.id=t.category_id
           WHERE c.name IS NULL OR c.name='Uncategorized'"""
    ).fetchone()["n"]

    rows = conn.execute(
        """SELECT c.name cat, t.use, a.default_use, t.amount_cents
             FROM transactions t
             JOIN accounts a ON a.id=t.account_id
             LEFT JOIN categories c ON c.id=t.category_id
            WHERE t.amount_cents < 0"""
    ).fetchall()
    spend = {}
    for r in rows:
        cat = r["cat"] or "Uncategorized"
        use = txn_use(r)
        d = spend.setdefault(cat, {"business": 0, "personal": 0})
        d[use] = d.get(use, 0) + (-r["amount_cents"])
    ranked = sorted(spend.items(), key=lambda kv: -(kv[1]["business"] + kv[1]["personal"]))

    ai = "on" if ollama_available() else "off"
    cards = f"""
      <div class=cards>
        <div class=card><div class=k>Transactions</div><div class="v num">{total}</div></div>
        <div class=card><div class=k>Needs review</div><div class="v num">{need}</div></div>
        <div class=card><div class=k>Uncategorized</div><div class="v num">{uncat}</div></div>
      </div>"""

    if ranked:
        rowshtml = "".join(
            f"<tr><td>{esc(c)}</td><td class='r num'>{money(-v['business'])}</td>"
            f"<td class='r num'>{money(-v['personal'])}</td>"
            f"<td class='r num'>{money(-(v['business']+v['personal']))}</td></tr>"
            for c, v in ranked
        )
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


@app.route("/import", methods=["GET", "POST"])
def do_import():
    conn = get_conn()
    accounts = conn.execute("SELECT * FROM accounts ORDER BY name").fetchall()

    if request.method == "POST":
        # ---- phase 1: parse and auto-detect a starting mapping. Nothing is
        # written here — no import_batches row, no transactions. The parsed file
        # is parked in memory and the user gets a mapping + preview screen.
        account_id = int(request.form["account_id"])
        f = request.files.get("file")
        if not f or not f.filename:
            return page("<div class=empty>No file chosen.</div>", "import")

        text = f.read().decode("utf-8-sig", errors="replace")
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
        token = stash_import(account_id, f.filename, header, data, h, cols)
        return page(mapping_body(token, PENDING_IMPORTS[token], cols), "import")

    opts = "".join(
        f"<option value=\"{a['id']}\">{esc(a['name'])} — {esc(a['default_use'])}</option>"
        for a in accounts)

    # A note handed back by the undo route. It carries a filename, so it is
    # escaped like any other CSV-provided value.
    msg = request.args.get("msg", "")
    note = f"<p class=sub><span class='status off'>{esc(msg)}</span></p>" if msg else ""

    batches = recent_imports(conn)
    if batches:
        rows_html = "".join(
            f"<tr><td>{esc(b['filename'])}</td><td>{esc(b['acct'] or '—')}</td>"
            f"<td>{esc(str(b['imported_at'])[:10])}</td>"
            f"<td class='r num'>{b['live_count']}</td>"
            f"<td class=r><form method=post action=\"{url_for('undo_import')}\">"
            f"<input type=hidden name=batch_id value=\"{b['id']}\">"
            f"<button class='btn ghost'>Undo import</button></form></td></tr>"
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
        <button class=btn>Import</button>
      </form>
      {recent}"""
    return page(body, "import")


def column_select(name, headers, chosen):
    """A <select> over the file's own column names."""
    opts = "".join(
        f"<option value=\"{i}\" {'selected' if i == chosen else ''}>"
        f"{esc(h.strip() or f'(column {i + 1})')}</option>"
        for i, h in enumerate(headers))
    return f"<select name={name}>{opts}</select>"


def mapping_body(token, stash, m):
    """The mapping + preview screen. Renders, never writes."""
    headers, data = stash["header"], stash["data"]

    if m["mode"] == "single":
        signopts = "".join(
            f"<option value={v} {'selected' if m.get('sign') == v else ''}>{label}</option>"
            for v, label in SIGN_CHOICES)
        money_fields = f"""
          <span><label class=lbl>Amount column</label>
            {column_select('amount_col', headers, m['amount'])}</span>
          <span><label class=lbl>Spending shows as</label>
            <select name=sign>{signopts}</select></span>"""
    else:
        money_fields = f"""
          <span><label class=lbl>Debit column</label>
            {column_select('debit_col', headers, m['debit'])}</span>
          <span><label class=lbl>Credit column</label>
            {column_select('credit_col', headers, m['credit'])}</span>"""

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

    return f"""
      <h1>Check the columns</h1>
      <p class=sub>{esc(stash['filename'])} — nothing has been imported yet.
         Adjust anything that looks wrong, preview it, then import.</p>
      {prenote}
      <form method=post class=rev>
        <input type=hidden name=token value="{esc(token)}">
        <input type=hidden name=account_id value="{stash['account_id']}">
        <div class=field>
          <span><label class=lbl>Date column</label>
            {column_select('date_col', headers, m['date'])}</span>
          <span><label class=lbl>Description column</label>
            {column_select('desc_col', headers, m['desc'])}</span>
        </div>
        <div class=field><label class=lbl>Money style</label>
          <span class=radio>{modes}</span></div>
        <div class=field>{money_fields}</div>
        <h2>Preview</h2>
        <table><tr><th>Date</th><th>Description</th><th class=r>Amount</th></tr>
          {prows}</table>
        <div class=field style='margin-top:16px'>
          <button class='btn ghost' formaction="{url_for('preview_import')}">Update preview</button>
          <button class=btn formaction="{url_for('commit_import')}">Import</button>
        </div>
      </form>"""


EXPIRED = "That upload expired — please choose the file again."


@app.route("/import/map", methods=["POST"])
def preview_import():
    """Re-render the mapping page under the submitted mapping. Writes nothing."""
    stash = PENDING_IMPORTS.get(request.form.get("token", ""))
    if stash is None:
        return redirect(url_for("do_import", msg=EXPIRED))
    m = effective_mapping(stash["detected"], len(stash["header"]))
    return page(mapping_body(request.form["token"], stash, m), "import")


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

    cat_names = db.category_names(conn)
    cz = Categorizer(conn, cat_names)
    now = datetime.utcnow().isoformat()
    cur = conn.execute(
        "INSERT INTO import_batches(account_id, filename, imported_at) VALUES (?,?,?)",
        (account_id, stash["filename"], now))
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
        cid = cat_id.get(guess["category"], cat_id["Uncategorized"])
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

    if request.form.get("confirm") != "1":
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
          <form method=post action="{url_for('undo_import')}"
                style='display:flex;gap:10px;align-items:center'>
            <input type=hidden name=batch_id value="{batch['id']}">
            <input type=hidden name=confirm value="1">
            <button class=btn>Confirm delete</button>
            <a class='btn ghost' href="{url_for('do_import')}">Cancel</a>
          </form>"""
        return page(body, "import")

    # Children first — foreign_keys is ON — and both statements land in the same
    # transaction, so the rows and their batch go together or not at all.
    removed = conn.execute(
        "DELETE FROM transactions WHERE import_batch_id=?", (batch_id,)).rowcount
    conn.execute("DELETE FROM import_batches WHERE id=?", (batch_id,))
    conn.commit()

    plural = "" if removed == 1 else "s"
    return redirect(url_for(
        "do_import", msg=f"Removed {removed} transaction{plural} from {batch['filename']}."))


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
            cid = cat_id.get(catname, cat_id["Uncategorized"])
            conn.execute(
                """UPDATE transactions
                     SET category_id=?, category_source='user', use=?, note=?, reviewed=1
                   WHERE id=?""",
                (cid, use, note, tid))
            # learn: this merchant -> this category/use, so next time it's rule-matched
            if catname != "Uncategorized":
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
    cats = db.category_names(conn)

    if not batch:
        body = """<h1>Review</h1>
          <div class=empty>Nothing to review — you're all caught up. 🎉<br>
          Import another statement whenever you're ready.</div>"""
        return page(body, "review")

    def card(t):
        suggested = t["catname"] or "Uncategorized"
        opts = "".join(
            f"<option {'selected' if c==suggested else ''}>{esc(c)}</option>" for c in cats)
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


def cat_select(name, cats):
    """Category picker that defaults to leaving the row/group alone."""
    opts = f"<option value=''>{LEAVE}</option>" + "".join(
        f"<option>{esc(c)}</option>" for c in cats)
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
            cid = cat_id.get(catname, uncategorized)
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
            cid = cat_id.get(catname, uncategorized)
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
    cats = db.category_names(conn)
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
                f"<td>{esc(t['catname'] or 'Uncategorized')} · {esc(use.title())}</td>"
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
SORT_COLUMNS = {
    "date": "t.txn_date",
    "desc": "t.description",
    "cat": "COALESCE(c.name,'Uncategorized')",
    "amount": "t.amount_cents",
}
SORT_DIRS = {"asc": "ASC", "desc": "DESC"}
SORT_DEFAULT = ("date", "DESC")


def parse_sort(args):
    """(key, 'ASC'|'DESC') from the query string — both sides whitelisted."""
    key = args.get("sort", "")
    direction = SORT_DIRS.get(args.get("dir", "").lower(), "")
    if key not in SORT_COLUMNS or not direction:
        return SORT_DEFAULT
    return key, direction


def sort_header(label, key, default_dir, sort, direction, cls=""):
    """A clickable <th>: flips the active column, else opens on default_dir."""
    if key == sort:
        nxt = "asc" if direction == "DESC" else "desc"
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


def edit_cell(field, inner, extra=""):
    """An editable grid cell. `inner` is already escaped by the caller."""
    return f"<td class='ed'{extra} data-field='{field}' tabindex='0'>{inner}</td>"


@app.route("/transactions")
def transactions():
    conn = get_conn()
    sort, direction = parse_sort(request.args)
    # Both halves come from the whitelists above; t.id keeps the order stable.
    order = f"ORDER BY {SORT_COLUMNS[sort]} {direction}, t.id DESC"
    rows = conn.execute(
        f"""SELECT t.*, a.name acct, a.default_use, c.name catname
             FROM transactions t
             JOIN accounts a ON a.id=t.account_id
             LEFT JOIN categories c ON c.id=t.category_id
            {order}"""
    ).fetchall()
    if not rows:
        return page("<h1>Transactions</h1><div class=empty>No transactions yet.</div>", "txns")

    trs = ""
    for t in rows:
        use = txn_use(t)
        catname = t["catname"] or "Uncategorized"
        upill = f"<span class='pill {'b' if use=='business' else 'p'}'>{esc(use.title())}</span>"
        state = ("<span class='pill settled'>Reviewed</span>" if t["reviewed"]
                 else "<span class='pill review'>Needs review</span>")
        note = esc(t["note"]) if t["note"] else "<span class=ph>—</span>"
        # One signed amount_cents, shown in two columns. Display only — storage
        # is unchanged, and the debit keeps its minus sign.
        cents = t["amount_cents"]
        debit = money(cents) if cents < 0 else ""
        credit = money(cents) if cents > 0 else ""
        # data-* carries the row's current values so the editor never has to
        # parse them back out of the cells.
        trs += (f"<tr data-id='{t['id']}' data-category=\"{esc(catname)}\""
                f" data-use=\"{esc(use)}\" data-note=\"{esc(t['note'])}\">"
                f"<td>{esc(t['txn_date'])}</td>"
                f"<td>{esc(t['description'])}</td>"
                + edit_cell("category", esc(catname))
                + edit_cell("use", upill)
                + f"<td class='status'>{state}</td>"
                + edit_cell("note", note)
                + f"<td class='r num'>{debit}</td>"
                f"<td class='r num'>{credit}</td></tr>")
    # Debit opens on asc (biggest money out on top), Credit on desc (biggest in).
    heads = (sort_header("Date", "date", "desc", sort, direction)
             + sort_header("Description", "desc", "asc", sort, direction)
             + sort_header("Category", "cat", "asc", sort, direction)
             + "<th>Use</th><th>Status</th><th>Note</th>"
             + sort_header("Debit", "amount", "asc", sort, direction, cls=" class=r")
             + sort_header("Credit", "amount", "desc", sort, direction, cls=" class=r"))
    # The picker's whole vocabulary, handed over once. No fetch, no round trip.
    cats = script_json(db.category_names(conn))
    body = f"""<h1>Transactions</h1>
      <p class=sub>{len(rows)} transactions in {YEAR}. Click a category, use or
      note to edit it — changes save as you go and stay on this row.</p>
      <script type="application/json" id="cat-list">{cats}</script>
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

    It also never touches amount_cents, txn_date, description, merchant_norm or
    dedup_key: no money, no identity, no dedup. Same no-CSRF posture as every
    other POST here — this is a single-user app bound to localhost.
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
        # An unknown name is not an error the user can see — it lands on
        # Uncategorized, the same fallback the importer and /review use.
        cid = cat_id.get(request.form["category"], cat_id["Uncategorized"])
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

    if sets:
        conn.execute(f"UPDATE transactions SET {', '.join(sets)} WHERE id=?",
                     vals + [row["id"]])
        conn.commit()

    # Answer with what is actually stored, not with what was asked for, so the
    # grid repaints from the database rather than from its own optimism.
    saved = conn.execute(
        """SELECT t.id, t.use, t.note, t.reviewed, a.default_use, c.name catname
             FROM transactions t
             JOIN accounts a ON a.id=t.account_id
             LEFT JOIN categories c ON c.id=t.category_id
            WHERE t.id=?""", (row["id"],)).fetchone()
    return jsonify(ok=True, id=saved["id"],
                   category=saved["catname"] or "Uncategorized",
                   use=txn_use(saved), reviewed=saved["reviewed"],
                   note=saved["note"])


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
