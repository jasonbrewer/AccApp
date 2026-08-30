"""
LocalLedger — smoke tests for the Milestone 1 loop.

Run before and after any change:
    python3 tests.py

Uses a throwaway database and never touches your real ledger files.
Ollama is not required — these exercise the deterministic rule + fallback path,
which is the behavior you must never break (the app has to work with AI off).
"""

import io
import os
import re
import sqlite3
import sys

os.environ["LEDGER_DB"] = "_test.sqlite"
if os.path.exists("_test.sqlite"):
    os.remove("_test.sqlite")

import app as A  # noqa: E402
import db as D   # noqa: E402


def fail(msg):
    print("FAIL:", msg)
    sys.exit(1)


client = A.app.test_client()

# 1. empty dashboard renders
r = client.get("/")
r.status_code == 200 or fail("dashboard did not load")
b"Import a statement" in r.data or fail("empty dashboard missing import prompt")

# 2. import sample; then re-import -> all duplicates

# Import is two-phase: POST /import parses and shows a mapping page (writing
# nothing), and POST /import/commit writes. These helpers drive both phases so
# every test below still reads as one import.
TOKEN_RE = re.compile(rb'name=token value="([^"]+)"')


def commit_mapping(phase1, account_id="1", **mapping):
    """Phase 2: take the token off the mapping page and commit under `mapping`.

    With no mapping fields this commits under the auto-detected mapping — what
    the one-shot importer did — so existing assertions are unchanged.
    """
    found = TOKEN_RE.search(phase1.data)
    if not found:
        return phase1        # phase 1 rejected the file; that IS the response
    data = {"token": found.group(1).decode(), "account_id": account_id}
    data.update(mapping)
    return client.post("/import/commit", data=data)


def upload_csv(text, name="x.csv", account_id="1"):
    """Phase 1 only: post an in-memory CSV and get the mapping page back."""
    return client.post(
        "/import",
        data={"account_id": account_id, "file": (io.BytesIO(text.encode()), name)},
        content_type="multipart/form-data")


def import_sample():
    with open("sample_statement.csv", "rb") as f:
        r = client.post("/import", data={"account_id": "1", "file": (f, "s.csv")},
                        content_type="multipart/form-data")
    return commit_mapping(r)

r = import_sample()
b"Imported 20 new" in r.data or fail("first import should add 20 rows")
r = import_sample()
b"Imported 0 new" in r.data or fail("re-import should detect 20 duplicates")

conn = A.get_conn()
n = conn.execute("SELECT COUNT(*) n FROM transactions").fetchone()["n"]
n == 20 or fail(f"expected 20 transactions, got {n}")

# 3. money stored as integer cents, never float
bad = conn.execute("SELECT COUNT(*) n FROM transactions WHERE typeof(amount_cents)!='integer'").fetchone()["n"]
bad == 0 or fail("amount_cents must always be integer")

# 4. rule-first categorization fires deterministically (no Ollama needed)
ruled = conn.execute("SELECT COUNT(*) n FROM transactions WHERE category_source='rule'").fetchone()["n"]
ruled >= 8 or fail(f"expected >=8 rule matches from seeds, got {ruled}")
print(f"seed rule matches on sample_statement.csv: {ruled}")

# 5. review confirms and LEARNS a rule for an unknown merchant
gh = conn.execute("SELECT id, merchant_norm FROM transactions WHERE description LIKE 'GITHUB%'").fetchone()
client.post("/review", data={
    "txn_id": [str(gh["id"])],
    f"cat_{gh['id']}": "Software & Subscriptions",
    f"use_{gh['id']}": "business",
    f"note_{gh['id']}": "",
})
learned = conn.execute("SELECT 1 FROM merchant_rules WHERE merchant_norm=?", (gh["merchant_norm"],)).fetchone()
learned or fail("confirming a category should create a learned merchant rule")
conn.execute("SELECT reviewed FROM transactions WHERE id=?", (gh["id"],)).fetchone()["reviewed"] == 1 \
    or fail("confirmed transaction should be marked reviewed")

# 6. dashboard + transactions render with data
b"Spending by category" in client.get("/").data or fail("dashboard missing spending table")
client.get("/transactions").status_code == 200 or fail("transactions page errored")

# 7. amount parsing edge cases
for raw, want in [("$1,234.56", 123456), ("(12.34)", -1234), ("-47.31", -4731), ("89", 8900)]:
    got = A.parse_amount_to_cents(raw)
    got == want or fail(f"parse_amount_to_cents({raw!r}) => {got}, expected {want}")


# ---------------------------------------------------------------------------
# Regression tests — one per fix in the import-safety / escaping batch.
# ---------------------------------------------------------------------------

def import_csv(text, name="x.csv", account_id="1", **mapping):
    """Post an in-memory CSV through the real (two-phase) import route."""
    return commit_mapping(upload_csv(text, name, account_id), account_id, **mapping)


# 8. money() is integer math (invariant 1) and formats exactly as before
A.money(-1500) == "-$15.00" or fail(f"money(-1500) => {A.money(-1500)!r}")
A.money(29) == "$0.29" or fail(f"money(29) => {A.money(29)!r}")
for c in (0, 1, 29, 99, 100, 1500, -1500, 123456, -98765, 100000000):
    old = ("-" if c < 0 else "") + f"${abs(c)/100:,.2f}"   # the old float formatting
    A.money(c) == old or fail(f"money({c}) => {A.money(c)!r}, expected {old!r}")
# ...and beyond float's 53-bit mantissa the old abs(cents)/100 path loses cents,
# which is what makes this more than a style fix.
A.money(99999999999999999) == "$999,999,999,999,999.99" or fail(
    f"money() went through a float: {A.money(99999999999999999)!r}")
A.money(2**53 + 1) == "$90,071,992,547,409.93" or fail(
    f"money() went through a float: {A.money(2**53 + 1)!r}")

# 9. non-finite amounts are rejected, not crashed on
for raw, want in [("1.15", 115), ("4.35", 435), ("8.87", 887), ("0.29", 29),
                  ("nan", None), ("Infinity", None), ("-inf", None), ("NaN", None)]:
    try:
        got = A.parse_amount_to_cents(raw)
    except Exception as e:                                  # noqa: BLE001
        fail(f"parse_amount_to_cents({raw!r}) raised {e!r}")
    got == want or fail(f"parse_amount_to_cents({raw!r}) => {got}, expected {want}")

# 10. three identical purchases stay three rows, and re-import still dedups
COFFEE = "Date,Description,Amount\n" + "02/02/2026,DAILY GRIND COFFEE,-5.00\n" * 3
r = import_csv(COFFEE, "coffee.csv")
b"Imported 3 new" in r.data or fail("3 identical coffees should import as 3 new rows")
b"0 duplicates skipped" in r.data or fail("a valid new row must count as new, not duplicate")


def coffee_state():
    row = conn.execute(
        """SELECT COUNT(*) n, COALESCE(SUM(amount_cents), 0) total
             FROM transactions WHERE description='DAILY GRIND COFFEE'""").fetchone()
    keys = {r["dedup_key"] for r in conn.execute(
        "SELECT dedup_key FROM transactions WHERE description='DAILY GRIND COFFEE'")}
    return row["n"], row["total"], keys


n, total, keys_first = coffee_state()
(n, total) == (3, -1500) or fail(f"expected 3 coffees totalling -1500, got {n} / {total}")

r = import_csv(COFFEE, "coffee.csv")
b"Imported 0 new" in r.data or fail("re-importing the same file must add nothing")
n, total, keys_second = coffee_state()
(n, total) == (3, -1500) or fail(f"re-import changed the ledger: {n} rows / {total} cents")
keys_first == keys_second or fail("dedup sequence did not regenerate identically on re-import")

# 11. only the dedup_key UNIQUE violation counts as a duplicate
existing = conn.execute("SELECT dedup_key FROM transactions LIMIT 1").fetchone()["dedup_key"]
INSERT = """INSERT INTO transactions
            (account_id, txn_date, description, merchant_norm, amount_cents,
             dedup_key, created_at)
            VALUES (?,?,?,?,?,?,?)"""
cases = [
    # (params, should is_dedup_conflict() call it a duplicate?)
    ((1, "2026-02-02", "X", "X", -1, existing, "now"), True),    # UNIQUE dedup_key
    ((1, "2026-02-02", "X", "X", -1, None, "now"), False),       # NOT NULL dedup_key
    ((9999, "2026-02-02", "X", "X", -1, "fk-probe", "now"), False),  # FK: no such account
]
for params, want_dup in cases:
    try:
        conn.execute(INSERT, params)
        conn.rollback()
        fail(f"expected an IntegrityError for {params!r}")
    except sqlite3.IntegrityError as err:
        conn.rollback()
        A.is_dedup_conflict(err) == want_dup or fail(
            f"is_dedup_conflict({err}) => {A.is_dedup_conflict(err)}, expected {want_dup}")

# 12. unparseable dates are skipped, never stored raw
A.normalize_date("03/04/2026") == "2026-03-04" or fail("US-first parsing must be preserved")
A.normalize_date("2026-03-04") == "2026-03-04" or fail("ISO dates should parse")
A.normalize_date("31/12/2026") == "2026-12-31" or fail("day-first fallback should still parse")
A.normalize_date("last tuesday") is None or fail("unparseable date must return None")
A.normalize_date("") is None or fail("empty date must return None")

r = import_csv("Date,Description,Amount\n"
               "someday,BAD DATE MERCHANT,-1.00\n"
               "03/04/2026,GOOD DATE MERCHANT,-2.00\n", "dates.csv")
b"Imported 1 new" in r.data or fail("only the row with a readable date should import")
b"1 rows unreadable" in r.data or fail("the unparseable date should be counted as skipped")
conn.execute("SELECT COUNT(*) n FROM transactions WHERE description='BAD DATE MERCHANT'"
             ).fetchone()["n"] == 0 or fail("a row with an unparseable date must not be stored")
conn.execute("SELECT txn_date FROM transactions WHERE description='GOOD DATE MERCHANT'"
             ).fetchone()["txn_date"] == "2026-03-04" or fail("valid date must store as YYYY-MM-DD")

# 13. user/CSV text is HTML-escaped on the way out
import_csv("Date,Description,Amount\n"
           "03/05/2026,<script>alert(1)</script> SHOP,-9.99\n", "xss.csv")
page = client.get("/transactions").data
b"&lt;script&gt;" in page or fail("a description with markup should render escaped")
b"<script>alert" not in page or fail("raw <script> from CSV data reached the page")

# ...including a note with a double quote, which used to truncate the value= attribute
first = conn.execute(
    "SELECT id FROM transactions WHERE reviewed=0 ORDER BY txn_date, id LIMIT 1").fetchone()
conn.execute("UPDATE transactions SET note=? WHERE id=?", ('5" tripod', first["id"]))
conn.commit()
b'value="5&quot; tripod"' in client.get("/review").data or fail(
    'a note containing " must render as value="5&quot; tripod"')

# ...and survives a confirm round-trip intact
tid = conn.execute(
    "SELECT id FROM transactions WHERE reviewed=0 ORDER BY txn_date, id LIMIT 1").fetchone()["id"]
client.post("/review", data={"txn_id": [str(tid)], f"cat_{tid}": "Equipment",
                             f"use_{tid}": "business", f"note_{tid}": '5" tripod'})
got = conn.execute("SELECT note FROM transactions WHERE id=?", (tid,)).fetchone()["note"]
got == '5" tripod' or fail(f'note round-tripped as {got!r}, expected \'5" tripod\'')

# 14. a bogus `use` falls back to the account default instead of vanishing
tid = conn.execute(
    "SELECT id FROM transactions WHERE reviewed=0 ORDER BY txn_date, id LIMIT 1").fetchone()["id"]
client.post("/review", data={"txn_id": [str(tid)], f"cat_{tid}": "Equipment",
                             f"use_{tid}": "bogus", f"note_{tid}": ""})
stored = conn.execute("SELECT use FROM transactions WHERE id=?", (tid,)).fetchone()["use"]
stored == "business" or fail(f"bogus use stored as {stored!r}, expected the account default")

# the dashboard only totals business + personal, so every row must land in one of them
rows = conn.execute(
    """SELECT t.use, a.default_use, t.amount_cents FROM transactions t
         JOIN accounts a ON a.id=t.account_id WHERE t.amount_cents < 0""").fetchall()
counted = sum(-r["amount_cents"] for r in rows if A.txn_use(r) in A.VALID_USES)
counted == sum(-r["amount_cents"] for r in rows) or fail(
    "some spending is not counted in the dashboard's business/personal totals")
client.get("/").status_code == 200 or fail("dashboard errored after the bogus-use write")


# ---------------------------------------------------------------------------
# Header detection + Debit/Credit columns. Synthetic data only.
# ---------------------------------------------------------------------------

BAD_HEADER = b"Couldn't find date / description / amount columns"
# proves the constant above matches a real rejection, so the "not in" asserts below
# are meaningful rather than vacuous
BAD_HEADER in import_csv("Alpha,Beta\n1,2\n", "nohdr.csv").data or fail(
    "a file with no recognizable header must still hit the detection-failed page")


def cents_of(desc):
    row = conn.execute(
        "SELECT amount_cents FROM transactions WHERE description=?", (desc,)).fetchone()
    return row and row["amount_cents"]


# 15. a preamble line above the header is found, skipped, and not "unreadable"
r = import_csv(
    "Date Range : 01/01/2026-03/31/2026\n"
    "Transaction Number,Date,Description,Memo,Amount Debit,Amount Credit,"
    "Balance,Check Number,Fees\n"
    "9001,02/10/2026,PREAMBLE HARDWARE CO,,-41.31,,1000.00,,\n", "preamble.csv")
BAD_HEADER not in r.data or fail("a file with a preamble line should still detect columns")
b"Imported 1 new" in r.data or fail("the row below a preamble header should import")
b"rows unreadable" not in r.data or fail("the preamble line must not count as unreadable")
cents_of("PREAMBLE HARDWARE CO") == -4131 or fail(
    f"preamble file debit stored as {cents_of('PREAMBLE HARDWARE CO')}, expected -4131")

# 16. Debit/Credit split with a POSITIVE debit column
r = import_csv(
    "Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit\n"
    "02/11/2026,02/12/2026,1234,POSITIVE DEBIT CAFE,Meals,5.21,\n"
    "02/12/2026,02/13/2026,1234,POSITIVE CREDIT REFUND,Meals,,10.00\n", "poscc.csv")
BAD_HEADER not in r.data or fail("Debit/Credit header should be detected, not rejected")
b"Imported 2 new" in r.data or fail("both split-column rows should import")
cents_of("POSITIVE DEBIT CAFE") == -521 or fail(
    f"debit 5.21 stored as {cents_of('POSITIVE DEBIT CAFE')}, expected -521")
cents_of("POSITIVE CREDIT REFUND") == 1000 or fail(
    f"credit 10.00 stored as {cents_of('POSITIVE CREDIT REFUND')}, expected 1000")

# 17. Debit/Credit split where the debit is ALREADY negative — never double-negated
r = import_csv(
    "Date,No.,Description,Debit,Credit\n"
    "02/13/2026,7,NEGATIVE DEBIT SUPPLY,-41.31,\n"
    "02/14/2026,8,NEGATIVE CREDIT PAYOUT,,1400\n", "negcc.csv")
BAD_HEADER not in r.data or fail("short Debit/Credit header should be detected")
b"Imported 2 new" in r.data or fail("both negative-debit rows should import")
cents_of("NEGATIVE DEBIT SUPPLY") == -4131 or fail(
    f"debit -41.31 stored as {cents_of('NEGATIVE DEBIT SUPPLY')}, expected -4131")
cents_of("NEGATIVE CREDIT PAYOUT") == 140000 or fail(
    f"credit 1400 stored as {cents_of('NEGATIVE CREDIT PAYOUT')}, expected 140000")

# 18. the single signed-amount path is unchanged
r = import_csv(
    "Transaction Date,Clearing Date,Description,Merchant,Category,Type,"
    "Amount (USD),Purchased By\n"
    "02/15/2026,02/16/2026,SINGLE AMOUNT STUDIO,SINGLE AMOUNT STUDIO,Equipment,"
    "Sale,-327.71,J. Brewer\n", "single.csv")
BAD_HEADER not in r.data or fail("single-amount header should still be detected")
b"Imported 1 new" in r.data or fail("the single-amount row should import")
cents_of("SINGLE AMOUNT STUDIO") == -32771 or fail(
    f"single amount -327.71 stored as {cents_of('SINGLE AMOUNT STUDIO')}, expected -32771")

# 19. detection precedence, unit level
d = A.detect_columns(["Transaction Number", "Date", "Description", "Memo",
                      "Amount Debit", "Amount Credit", "Balance"])
(d["mode"], d["date"], d["desc"], d["debit"], d["credit"]) == ("debitcredit", 1, 2, 4, 5) or fail(
    f"'Amount Debit'/'Amount Credit' must map to the split path, got {d}")
d = A.detect_columns(["Date", "Description", "Amount"])
(d["mode"], d["date"], d["desc"], d["amount"]) == ("single", 0, 1, 2) or fail(
    f"the plain three-column shape must stay on the single path, got {d}")
A.find_header_row([["Date Range : 01/01/2026-03/31/2026"],
                   ["Date", "Description", "Amount"]]) == 1 or fail(
    "find_header_row should skip a single-cell preamble line")
A.find_header_row([["Date", "Description", "Amount"], ["a", "b", "c"]]) == 0 or fail(
    "find_header_row should pick row 0 when it is the header")
A.find_header_row([["nothing", "here"], ["still", "nothing"]]) == 0 or fail(
    "find_header_row must fall back to 0 so the existing error path is preserved")

# ---------------------------------------------------------------------------
# 20. normalize_merchant strips generic boilerplate wherever it appears, so the
# real merchant surfaces. Synthetic descriptions only — never real statement data.
# ---------------------------------------------------------------------------

from categorizer import normalize_merchant  # noqa: E402

# Boilerplate + reference numbers + a masked card block sit in FRONT of the
# merchant; the key must still be the merchant.
PINNED = [
    ("MERCHANT PURCHASE TERMINAL 55421356 PMUSA 153020 RICHMOND ATLANTA GA "
     "XXXXXXXXXXXX1545 05-15-26 12:00 AM", "PMUSA RICHMOND"),
    ("POS WITHDRAWAL - AMAZON MKTPL*B25S28KZ1 440 TERRY AVE N SEATTLE W "
     "- CARD ENDING IN 4997", "AMAZON MKTPL"),
    ("POS PURCHASE HULU 123456", "HULU"),
    ("POS PURCHASE AMAZON 789", "AMAZON"),
]
for desc, want in PINNED:
    got = normalize_merchant(desc)
    got == want or fail(f"normalize_merchant({desc[:40]!r}...) => {got!r}, expected {want!r}")

# the seed rule "ADOBE" must keep matching through the leading-prefix lookup
normalize_merchant("ADOBE *CREATIVE CLD 408-536-6000").startswith("ADOBE") or fail(
    "an ADOBE description must still normalize to a key starting with ADOBE")

# THE REPORTED BUG: distinct merchants behind the same boilerplate used to
# collapse onto one key ("MERCHANT TERMINAL"), so teaching one taught all.
keys = [normalize_merchant("POS PURCHASE HULU 123456"),
        normalize_merchant("POS PURCHASE AMAZON 789"),
        normalize_merchant("MERCHANT PURCHASE TERMINAL 55421356 PMUSA 153020 "
                           "RICHMOND ATLANTA GA XXXXXXXXXXXX1545")]
len(set(keys)) == 3 or fail(f"boilerplate-led descriptions still collapse: {keys}")
for k in keys:
    k not in ("MERCHANT TERMINAL", "MERCHANT PURCHASE") or fail(
        f"normalize_merchant returned pure boilerplate: {k!r}")

# a description that is nothing but boilerplate still yields a usable key
normalize_merchant("POS PURCHASE") or fail("normalize_merchant must never return ''")

# ---------------------------------------------------------------------------
# 21. Undo an import — two-step confirm, and the delete stays inside one batch.
# Synthetic statements only; never real statement data.
# ---------------------------------------------------------------------------

def latest_batch_id():
    return conn.execute(
        "SELECT id FROM import_batches ORDER BY id DESC LIMIT 1").fetchone()["id"]


def batch_state(bid):
    """(transactions still attached to the batch, batch rows with that id)."""
    return (conn.execute("SELECT COUNT(*) n FROM transactions WHERE import_batch_id=?",
                         (bid,)).fetchone()["n"],
            conn.execute("SELECT COUNT(*) n FROM import_batches WHERE id=?",
                         (bid,)).fetchone()["n"])


def undo(bid, confirm=False):
    data = {"batch_id": str(bid)}
    if confirm:
        data["confirm"] = "1"
    return client.post("/import/undo", data=data, follow_redirects=True)


def table_counts():
    """Everything undo must leave alone."""
    return tuple(conn.execute(f"SELECT COUNT(*) n FROM {t}").fetchone()["n"]
                 for t in ("merchant_rules", "categories", "accounts"))


UNDO_A = ("Date,Description,Amount\n"
          "04/01/2026,UNDO ALPHA TOOLS,-11.11\n"
          "04/02/2026,UNDO ALPHA FREIGHT,-22.22\n"
          "04/03/2026,UNDO ALPHA CAFE,-33.33\n")
UNDO_B = ("Date,Description,Amount\n"
          "04/04/2026,UNDO BETA LUMBER,-44.44\n"
          "04/05/2026,UNDO BETA PAINT,-55.55\n")

# 20a. an import creates exactly one batch, and its rows point at it
b"Imported 3 new" in import_csv(UNDO_A, "undo_a.csv").data or fail(
    "the undo fixture should import 3 new rows")
bid_a = latest_batch_id()
batch_state(bid_a) == (3, 1) or fail(
    f"after import expected 3 transactions and 1 batch row, got {batch_state(bid_a)}")

untouched = table_counts()

# 20b. first post, WITHOUT the confirm flag: a confirmation page, and no delete
r = undo(bid_a)
b"Confirm delete" in r.data or fail("the first undo post should render the confirm page")
b"undo_a.csv" in r.data or fail("the confirm page should name the file being removed")
batch_state(bid_a) == (3, 1) or fail(
    f"the unconfirmed undo deleted something: {batch_state(bid_a)}")

# ...and a GET must never delete either
client.get("/import/undo").status_code == 405 or fail(
    "GET on the undo route must not be handled at all")
batch_state(bid_a) == (3, 1) or fail("a GET to the undo route deleted rows")

# 20c. second post WITH confirm=1: the rows and the batch row are both gone
r = undo(bid_a, confirm=True)
b"Removed 3 transactions from undo_a.csv." in r.data or fail(
    "the undo should report what it removed")
batch_state(bid_a) == (0, 0) or fail(
    f"after confirming, expected nothing left, got {batch_state(bid_a)}")

# ...and nothing outside transactions/import_batches moved
table_counts() == untouched or fail(
    f"undo touched another table: {table_counts()} != {untouched}")

# 20d. undoing again is gentle, not an error; a malformed id is ignored
b"That import was already removed." in undo(bid_a, confirm=True).data or fail(
    "undoing an already-removed batch should redirect with a gentle message")
r = client.post("/import/undo", data={"batch_id": "not-an-int", "confirm": "1"},
                follow_redirects=True)
r.status_code == 200 or fail("a malformed batch_id should redirect, not error")

# 20e. isolation: undoing one batch leaves the other completely intact
import_csv(UNDO_A, "undo_a2.csv")
bid_a2 = latest_batch_id()
import_csv(UNDO_B, "undo_b.csv")
bid_b = latest_batch_id()
bid_a2 != bid_b or fail("two imports must produce two batches")
(batch_state(bid_a2), batch_state(bid_b)) == ((3, 1), (2, 1)) or fail(
    f"expected 3+2 rows across two batches, got {batch_state(bid_a2)} / {batch_state(bid_b)}")

undo(bid_a2, confirm=True)
batch_state(bid_a2) == (0, 0) or fail("the undone batch should be empty")
batch_state(bid_b) == (2, 1) or fail(
    f"undoing one batch disturbed the other: {batch_state(bid_b)}")
conn.execute("SELECT COUNT(*) n FROM transactions WHERE description LIKE 'UNDO BETA%'"
             ).fetchone()["n"] == 2 or fail("the other batch's transactions were deleted")

# 20f. re-importing the same file after an undo brings the rows back as NEW —
# proof the undo really cleared them rather than leaving dedup keys behind
r = import_csv(UNDO_A, "undo_a2.csv")
b"Imported 3 new" in r.data or fail(
    "re-importing after an undo must add rows, not report them as duplicates")
b"0 duplicates skipped" in r.data or fail("no row should be blocked as a duplicate")
batch_state(latest_batch_id()) == (3, 1) or fail(
    "the re-imported rows should be attached to a fresh batch")

# 20g. the Import page lists recent imports with a live count and an undo button
page_html = client.get("/import").data
b"Recent imports" in page_html or fail("the import page should list recent imports")
b"Undo import" in page_html or fail("each listed import needs an undo button")
b"undo_b.csv" in page_html or fail("the recent-imports list should name the file")

# 20h. a filename is CSV-provided text: escaped on the list, the confirm page,
# and in the message the redirect carries back
import_csv(UNDO_B, "<script>alert(1)</script>.csv")
bid_x = latest_batch_id()
b"&lt;script&gt;" in client.get("/import").data or fail(
    "a filename containing markup must render escaped on the import page")
b"<script>alert" not in client.get("/import").data or fail(
    "raw markup from a filename reached the import page")
b"<script>alert" not in undo(bid_x).data or fail(
    "raw markup from a filename reached the confirm page")
b"<script>alert" not in undo(bid_x, confirm=True).data or fail(
    "raw markup from a filename reached the removal message")

# ---------------------------------------------------------------------------
# 22. Two-phase import with an explicit column mapping. Phase 1 and the preview
# round-trip must never write; only /import/commit does. Synthetic files only.
# ---------------------------------------------------------------------------

def ledger_size():
    """(transactions, import_batches) — what a write would move."""
    return (conn.execute("SELECT COUNT(*) n FROM transactions").fetchone()["n"],
            conn.execute("SELECT COUNT(*) n FROM import_batches").fetchone()["n"])


def cents_list(like):
    return [r["amount_cents"] for r in conn.execute(
        "SELECT amount_cents FROM transactions WHERE description LIKE ? ORDER BY id",
        (like,))]


PLAIN = ("Date,Description,Amount\n"
         "05/01/2026,MAPPING PHASE ONE CO,-10.00\n"
         "05/02/2026,MAPPING PHASE ONE INC,-20.00\n")

# 22A. phase 1 renders the mapping page and writes nothing
before = ledger_size()
r = upload_csv(PLAIN, "phase1.csv")
b"name=date_col" in r.data or fail("the mapping page needs a Date column select")
b"name=desc_col" in r.data or fail("the mapping page needs a Description column select")
b"name=amount_col" in r.data or fail("a single-amount file needs an Amount column select")
b"name=sign" in r.data or fail("a single-amount file needs a spending-sign select")
b"name=mode" in r.data or fail("the mapping page needs the money-style radios")
b"Update preview" in r.data or fail("the mapping page needs an Update preview button")
b"Preview" in r.data or fail("the mapping page needs a preview table")
b"MAPPING PHASE ONE CO" in r.data or fail("the preview should show the first data rows")
b"-$10.00" in r.data or fail("the preview should show money under the current mapping")
b"Imported" not in r.data or fail("phase 1 must not report an import")
ledger_size() == before or fail(f"phase 1 wrote to the ledger: {ledger_size()} != {before}")

# 22F. Update preview re-renders under the submitted mapping and still writes nothing
token = TOKEN_RE.search(r.data).group(1).decode()
r2 = client.post("/import/map", data={"token": token, "account_id": "1",
                                      "date_col": "0", "desc_col": "1",
                                      "mode": "single", "amount_col": "2",
                                      "sign": "negative"})
b"Preview" in r2.data or fail("/import/map should re-render the mapping page")
b"MAPPING PHASE ONE CO" in r2.data or fail("the re-rendered preview should show rows")
b"Imported" not in r2.data or fail("/import/map must not report an import")
ledger_size() == before or fail(f"/import/map wrote to the ledger: {ledger_size()}")

# 22B. THE CORE WIN: re-point Description at a Memo column. Auto-detect picks the
# literal "Description" column, which every row fills with the same boilerplate;
# the real merchant is in Memo. A preamble line sits above the header.
MEMO = ("Statement period : 05/01/2026-05/31/2026\n"
        "Date,Description,Memo,Amount\n"
        "05/03/2026,POS PURCHASE,FAKEMART 1120 SPRINGFIELD,-12.34\n"
        "05/04/2026,POS PURCHASE,FAKECOFFEE 88 DOWNTOWN,-4.56\n")

r = upload_csv(MEMO, "memo.csv")
b"skipped 1 preamble row" in r.data or fail(
    "the mapping page should note the skipped preamble row")
b"POS PURCHASE" in r.data or fail(
    "the preview should show what auto-detect picked, boilerplate and all")

# commit with Description explicitly re-pointed at the Memo column (index 2)
r = commit_mapping(r, desc_col="2")
b"Imported 2 new" in r.data or fail("both memo rows should import")
stored = [row["description"] for row in conn.execute(
    "SELECT description FROM transactions WHERE description LIKE 'FAKE%' ORDER BY id")]
len(stored) == 2 or fail(f"expected 2 rows stored from the Memo column, got {stored}")
"FAKEMART" in stored[0] or fail(f"stored description is {stored[0]!r}, expected the Memo text")
conn.execute("SELECT COUNT(*) n FROM transactions WHERE description='POS PURCHASE'"
             ).fetchone()["n"] == 0 or fail(
    "the boilerplate Description column was stored instead of the mapped Memo column")

# 22C. an explicit debit/credit mapping overrides an auto-detected single column.
# Auto-detect reads "Amount" (empty on every row); the user points the import at
# the Out / In pair instead.
SPLIT = ("Date,Description,Amount,Out,In\n"
         "06/01/2026,FAKE HARDWARE MAPPED,,5.21,\n"
         "06/02/2026,FAKE LUMBER MAPPED,,-41.31,\n"
         "06/03/2026,FAKE REFUND MAPPED,,,10.00\n")
r = import_csv(SPLIT, "split.csv", mode="debitcredit", debit_col="3", credit_col="4")
b"Imported 3 new" in r.data or fail(
    f"all three split rows should import under the explicit mapping: {r.data[:200]}")
cents_list("FAKE HARDWARE MAPPED") == [-521] or fail(
    f"positive debit 5.21 stored as {cents_list('FAKE HARDWARE MAPPED')}, expected [-521]")
cents_list("FAKE LUMBER MAPPED") == [-4131] or fail(
    f"negative debit -41.31 stored as {cents_list('FAKE LUMBER MAPPED')}, expected [-4131]")
cents_list("FAKE REFUND MAPPED") == [1000] or fail(
    f"credit 10.00 stored as {cents_list('FAKE REFUND MAPPED')}, expected [1000]")

# 22D. single column where the bank writes spending as POSITIVE numbers
POSSPEND = ("Date,Description,Amount\n"
            "07/01/2026,FAKE POSITIVE GROCER,42.50\n"
            "07/02/2026,FAKE POSITIVE REFUND,-15.00\n")
r = import_csv(POSSPEND, "possign.csv", sign="positive")
b"Imported 2 new" in r.data or fail("both positive-spending rows should import")
cents_list("FAKE POSITIVE GROCER") == [-4250] or fail(
    f"positive spending 42.50 stored as {cents_list('FAKE POSITIVE GROCER')}, expected [-4250]")
cents_list("FAKE POSITIVE REFUND") == [1500] or fail(
    f"the money-in row stored as {cents_list('FAKE POSITIVE REFUND')}, expected [1500]")

# ...and the same file under the default sign is the untouched signed column
r = import_csv(POSSPEND, "possign2.csv")
cents_list("FAKE POSITIVE GROCER") == [-4250, 4250] or fail(
    f"the default sign should store 42.50 as +4250, got {cents_list('FAKE POSITIVE GROCER')}")

# 22E. a stash that is gone (server restarted, or the page sat too long)
before = ledger_size()
r = client.post("/import/commit", data={"token": "no-such-token", "account_id": "1"},
                follow_redirects=True)
r.status_code == 200 or fail("an expired token must not crash the commit route")
b"That upload expired" in r.data or fail(
    "an expired token should say so instead of failing silently")
ledger_size() == before or fail(f"an expired commit wrote to the ledger: {ledger_size()}")

# ...and so must the preview round-trip
r = client.post("/import/map", data={"token": "no-such-token"}, follow_redirects=True)
b"That upload expired" in r.data or fail("/import/map should report an expired token too")
ledger_size() == before or fail("an expired preview wrote to the ledger")

# ---------------------------------------------------------------------------
# 23. Grouped categorize screen: set a merchant once, or make one row an
# exception. Synthetic merchants only — never real statement data.
# ---------------------------------------------------------------------------

GROUP_RE = re.compile(rb'name=group_norm_(\d+) value="([^"]*)"')


def group_indices():
    """merchant_norm -> the group index the page rendered it under."""
    return {norm.decode(): i.decode()
            for i, norm in GROUP_RE.findall(client.get("/categorize").data)}


def categorize_post(**fields):
    return client.post("/categorize", data=fields, follow_redirects=True)


def group_apply(norm, catname, use="business", **extra):
    """Post a group action for one merchant, addressed the way the page renders it."""
    i = group_indices()[norm]
    fields = {f"group_norm_{i}": norm, f"group_cat_{i}": catname, f"group_use_{i}": use}
    fields.update(extra)
    return categorize_post(**fields)


def rows_for(norm):
    return conn.execute(
        """SELECT t.id, t.use, t.reviewed, t.category_source, c.name cat
             FROM transactions t LEFT JOIN categories c ON c.id=t.category_id
            WHERE t.merchant_norm=? ORDER BY t.id""", (norm,)).fetchall()


def rule_for(norm):
    r = conn.execute(
        """SELECT c.name cat, r.use FROM merchant_rules r
             JOIN categories c ON c.id=r.category_id
            WHERE r.merchant_norm=?""", (norm,)).fetchone()
    return (r["cat"], r["use"]) if r else None


def immutables():
    """The columns this screen must never write."""
    return [tuple(r) for r in conn.execute(
        """SELECT id, txn_date, description, amount_cents, dedup_key
             FROM transactions ORDER BY id""")]


CATZ = ("Date,Description,Amount\n"
        "09/01/2026,ZED SOFTWARE HQ INVOICE ONE,-19.00\n"
        "09/02/2026,ZED SOFTWARE HQ INVOICE TWO,-19.00\n"
        "09/03/2026,QUUX SUPPLY DEPOT ORDER A,-31.00\n"
        "09/04/2026,QUUX SUPPLY DEPOT ORDER B,-32.00\n"
        "09/05/2026,QUUX SUPPLY DEPOT ORDER C,-33.00\n"
        "09/06/2026,NULLCO SERVICES RETAINER,-44.00\n"
        "09/07/2026,FEEBACK CLIENT DEPOSIT,1200.00\n")
b"Imported 7 new" in import_csv(CATZ, "categorize.csv").data or fail(
    "the categorize fixture should import 7 rows")

frozen = immutables()

# 23A. the screen groups by merchant and shows every row, income included
html = client.get("/categorize").data
for norm in ("ZED SOFTWARE", "QUUX SUPPLY", "NULLCO SERVICES", "FEEBACK CLIENT"):
    norm.encode() in html or fail(f"/categorize should show a group for {norm}")
for desc in (b"ZED SOFTWARE HQ INVOICE ONE", b"QUUX SUPPLY DEPOT ORDER C",
             b"FEEBACK CLIENT DEPOSIT"):
    desc in html or fail(f"/categorize should list the row {desc!r}")
b"$1,200.00" in html or fail("an income row should appear with its amount")
b"leave unchanged" in html or fail("both pickers should default to leaving things alone")
b"Save changes" in html or fail("the page needs its single submit button")
len(rows_for("ZED SOFTWARE")) == 2 or fail("ZED SOFTWARE should have 2 rows")
len(rows_for("QUUX SUPPLY")) == 3 or fail("QUUX SUPPLY should have 3 rows")

# 23B. a group action applies to every row AND teaches the rule
r = group_apply("ZED SOFTWARE", "Software & Subscriptions", "business")
b"Updated 2 transactions across 1 merchants." in r.data or fail(
    "the group action should report what it changed")
for row in rows_for("ZED SOFTWARE"):
    (row["cat"], row["use"], row["reviewed"], row["category_source"]) == (
        "Software & Subscriptions", "business", 1, "user") or fail(
        f"group action left a row as {tuple(row)}")
rule_for("ZED SOFTWARE") == ("Software & Subscriptions", "business") or fail(
    f"the group action should teach the rule, got {rule_for('ZED SOFTWARE')}")

# 23C. Uncategorized updates the rows but teaches nothing — same rule as /review
rule_for("NULLCO SERVICES") is None or fail("no rule should exist for NULLCO yet")
group_apply("NULLCO SERVICES", "Uncategorized", "business")
rows_for("NULLCO SERVICES")[0]["cat"] == "Uncategorized" or fail(
    "the rows should still be updated for an Uncategorized group action")
rows_for("NULLCO SERVICES")[0]["reviewed"] == 1 or fail(
    "an Uncategorized group action should still mark the rows reviewed")
rule_for("NULLCO SERVICES") is None or fail(
    "Uncategorized must not teach a merchant rule")

# 23D. THE CORE GUARANTEE: a per-row exception never rewrites the merchant rule
group_apply("QUUX SUPPLY", "Office Supplies", "business")
rule_for("QUUX SUPPLY") == ("Office Supplies", "business") or fail(
    "the group action should have taught Office Supplies")
odd = rows_for("QUUX SUPPLY")[0]["id"]
categorize_post(**{f"row_cat_{odd}": "Meals", f"row_use_{odd}": "business"})
after = {row["id"]: row["cat"] for row in rows_for("QUUX SUPPLY")}
after[odd] == "Meals" or fail(f"the overridden row is {after[odd]!r}, expected Meals")
[c for i, c in after.items() if i != odd] == ["Office Supplies"] * 2 or fail(
    f"an override leaked onto its siblings: {after}")
rule_for("QUUX SUPPLY") == ("Office Supplies", "business") or fail(
    f"a per-row override rewrote merchant_rules to {rule_for('QUUX SUPPLY')} "
    "— it must never touch the learned rule")

# 23E. in ONE submit, the row override wins for its row; the rule follows the group
group_apply("QUUX SUPPLY", "Shipping", "business",
            **{f"row_cat_{odd}": "Travel", f"row_use_{odd}": "personal"})
after = {row["id"]: (row["cat"], row["use"]) for row in rows_for("QUUX SUPPLY")}
after[odd] == ("Travel", "personal") or fail(
    f"the same-submit override lost to the group action: {after[odd]}")
[v for i, v in after.items() if i != odd] == [("Shipping", "business")] * 2 or fail(
    f"siblings should carry the group action: {after}")
rule_for("QUUX SUPPLY") == ("Shipping", "business") or fail(
    f"the rule should follow the group action, got {rule_for('QUUX SUPPLY')}")

# 23F. already-reviewed rows can be re-set — this screen edits, not just fills in
all(row["reviewed"] for row in rows_for("QUUX SUPPLY")) or fail(
    "the QUUX rows should all be reviewed by now")
group_apply("QUUX SUPPLY", "Equipment", "business")
{row["cat"] for row in rows_for("QUUX SUPPLY")} == {"Equipment"} or fail(
    "a group action must re-set already-categorized rows, exception included")

# 23G. merchant and description text is escaped
import_csv("Date,Description,Amount\n"
           "09/08/2026,<b>BOLDMART</b> SUPPLIES,-7.00\n", "catesc.csv")
html = client.get("/categorize").data
b"&lt;b&gt;BOLDMART&lt;/b&gt; SUPPLIES" in html or fail(
    "a description containing markup must render escaped on /categorize")
b"<b>BOLDMART</b>" not in html or fail("raw markup reached /categorize")

# ...and nothing this screen touched moved an amount, date, description or dedup key
now_frozen = {r[0]: r for r in immutables()}
all(now_frozen[r[0]] == r for r in frozen) or fail(
    "categorizing changed a column it must never write")

os.remove("_test.sqlite")
print("ALL TESTS PASSED")
