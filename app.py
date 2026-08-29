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
"""

import csv
import html
import io
import os
import sqlite3
from datetime import datetime
from decimal import Decimal, InvalidOperation

from flask import Flask, request, redirect, url_for, render_template_string

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
</style></head><body>
<header><div class=bar>
  <div class=brand>LocalLedger <span>· {{ year }}</span></div>
  <nav>
    <a href="{{ url_for('dashboard') }}" class="{{ 'on' if page=='dash' }}">Dashboard</a>
    <a href="{{ url_for('do_import') }}" class="{{ 'on' if page=='import' }}">Import</a>
    <a href="{{ url_for('review') }}" class="{{ 'on' if page=='review' }}">Review{% if need %} · {{ need }}{% endif %}</a>
    <a href="{{ url_for('transactions') }}" class="{{ 'on' if page=='txns' }}">Transactions</a>
  </nav>
</div></header>
<main>{{ body|safe }}</main>
</body></html>
"""


def page(body, page="dash", need=0):
    conn = get_conn()
    need = conn.execute("SELECT COUNT(*) n FROM transactions WHERE reviewed=0").fetchone()["n"]
    return render_template_string(BASE, body=body, page=page, need=need, year=YEAR)


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
        if cols["date"] is None or cols["desc"] is None or (
            cols["amount"] is None if cols["mode"] == "single"
            else cols["debit"] is None or cols["credit"] is None
        ):
            return page(
                "<div class=empty>Couldn't find date / description / amount columns "
                "in that file's header. (Column-mapping UI is a next step.)</div>", "import")

        cat_names = db.category_names(conn)
        cz = Categorizer(conn, cat_names)
        now = datetime.utcnow().isoformat()
        cur = conn.execute(
            "INSERT INTO import_batches(account_id, filename, imported_at) VALUES (?,?,?)",
            (account_id, f.filename, now))
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
            try:
                raw_date = row[cols["date"]].strip()
                desc = row[cols["desc"]].strip()
                if cols["mode"] == "single":
                    cents = parse_amount_to_cents(row[cols["amount"]])
                else:
                    cents = split_amount_to_cents(
                        row[cols["debit"]], row[cols["credit"]])
            except IndexError:
                skipped += 1
                continue
            date = normalize_date(raw_date)
            if cents is None or not desc or date is None:
                skipped += 1
                continue

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

        note = f"Imported {added} new · {dups} duplicates skipped"
        if skipped:
            note += f" · {skipped} rows unreadable"
        body = f"""<h1>Import complete</h1><p class=sub>{note}.</p>
          <a class=btn href='{url_for('review')}'>Review new transactions →</a>"""
        return page(body, "import")

    opts = "".join(
        f"<option value=\"{a['id']}\">{esc(a['name'])} — {esc(a['default_use'])}</option>"
        for a in accounts)
    body = f"""
      <h1>Import a statement</h1>
      <p class=sub>Drop in a bank or credit-card CSV. Duplicates are detected automatically,
         and each transaction is categorized before you review it.</p>
      <form class=up method=post enctype=multipart/form-data>
        <div><label class=lbl>Account</label><br><select name=account_id>{opts}</select></div>
        <div><label class=lbl>CSV file</label><br><input type=file name=file accept=.csv></div>
        <button class=btn>Import</button>
      </form>"""
    return page(body, "import")


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
                conn.execute(
                    """INSERT INTO merchant_rules(merchant_norm, category_id, use, updated_at)
                       VALUES (?,?,?,?)
                       ON CONFLICT(merchant_norm) DO UPDATE SET
                         category_id=excluded.category_id, use=excluded.use,
                         hits=hits+1, updated_at=excluded.updated_at""",
                    (row["merchant_norm"], cid, use, datetime.utcnow().isoformat()))
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


@app.route("/transactions")
def transactions():
    conn = get_conn()
    rows = conn.execute(
        """SELECT t.*, a.name acct, a.default_use, c.name catname
             FROM transactions t
             JOIN accounts a ON a.id=t.account_id
             LEFT JOIN categories c ON c.id=t.category_id
            ORDER BY t.txn_date DESC, t.id DESC"""
    ).fetchall()
    if not rows:
        return page("<h1>Transactions</h1><div class=empty>No transactions yet.</div>", "txns")

    trs = ""
    for t in rows:
        use = txn_use(t)
        upill = f"<span class='pill {'b' if use=='business' else 'p'}'>{esc(use.title())}</span>"
        state = ("<span class='pill settled'>Reviewed</span>" if t["reviewed"]
                 else "<span class='pill review'>Needs review</span>")
        note = (f"<div class=meta style='color:var(--muted);font-size:12px'>{esc(t['note'])}</div>"
                if t["note"] else "")
        trs += (f"<tr><td>{esc(t['txn_date'])}</td>"
                f"<td>{esc(t['description'])}{note}</td>"
                f"<td>{esc(t['catname'] or 'Uncategorized')}</td>"
                f"<td>{upill}</td><td>{state}</td>"
                f"<td class='r num'>{money(t['amount_cents'])}</td></tr>")
    body = f"""<h1>Transactions</h1>
      <p class=sub>{len(rows)} transactions in {YEAR}.</p>
      <table><tr><th>Date</th><th>Description</th><th>Category</th>
        <th>Use</th><th>Status</th><th class=r>Amount</th></tr>{trs}</table>"""
    return page(body, "txns")


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
