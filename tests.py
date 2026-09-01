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
# 22G-22M. Money style toggle. The mapping page used to render ONLY the fields
# for the detected mode, so switching the radio submitted a mode whose column
# selects were never on the page: they resolved to None, read_row did row[None],
# and the preview 500'd. Both groups now always render.
# ---------------------------------------------------------------------------

def preview(phase1, **fields):
    """POST the preview route under an explicit mapping, the way the page does."""
    found = TOKEN_RE.search(phase1.data)
    found or fail("phase 1 did not render a mapping page to preview")
    data = {"token": found.group(1).decode(), "account_id": "1"}
    data.update(fields)
    return client.post("/import/map", data=data)


def selected_col(html, name):
    """The column index a rendered <select> has chosen, or None if it chose none."""
    block = re.search(("<select name=%s>(.*?)</select>" % name).encode(), html, re.S)
    block or fail(f"the mapping page is missing a {name} select")
    chosen = re.search(rb'<option value="(\d+)" selected', block.group(1))
    return int(chosen.group(1)) if chosen else None


# A single-amount file that also carries an Out / In pair, so the same upload
# can be previewed both ways.
TOGGLE = ("Date,Description,Amount,Out,In\n"
          "09/01/2026,FAKE TOGGLE HARDWARE,,5.21,\n"
          "09/02/2026,FAKE TOGGLE REFUND,,,10.00\n")
# ...and a debit/credit file that also carries one signed column, for the
# reverse switch.
REVERSE = ("Date,Description,Debit,Credit,Signed\n"
           "09/03/2026,FAKE REVERSE GROCER,12.34,,-12.34\n"
           "09/04/2026,FAKE REVERSE PAYCHECK,,500.00,500.00\n")

# 22G. THE REGRESSION: a single-amount file previewed as debit/credit with no
# debit_col / credit_col submitted. Both resolve to None, and row[None] raises
# TypeError — which the old IndexError-only skip path let through as a 500.
before = ledger_size()
phase1 = upload_csv(PLAIN, "toggle_crash.csv")
r = preview(phase1, date_col="0", desc_col="1", mode="debitcredit")
r.status_code == 200 or fail(
    f"a mode with no money columns must render, not {r.status_code}")
b"Check the columns" in r.data or fail(
    "the mapping page should still render under an unusable money mapping")
b"row skipped under this mapping" in r.data or fail(
    "rows unreadable under the submitted mapping should be shown as skipped")
ledger_size() == before or fail("the crashing preview wrote to the ledger")

# ...and the guard lives in read_row itself: a None column is an unreadable row,
# never an exception. Remove the guard and these are the assertions that go red.
ROW = ["05/01/2026", "FAKE GUARD CO", "-10.00"]
A.read_row(ROW, {"date": 0, "desc": 1, "mode": "debitcredit",
                 "debit": None, "credit": None}) is None or fail(
    "read_row must read a None debit/credit column as an unreadable row")
A.read_row(ROW, {"date": 0, "desc": 1, "mode": "single",
                 "amount": None}) is None or fail(
    "read_row must read a None amount column as an unreadable row")
A.read_row(ROW, {"date": None, "desc": 1, "mode": "single",
                 "amount": 2}) is None or fail(
    "read_row must read a None date column as an unreadable row")
A.read_row(ROW, {"date": 0, "desc": None, "mode": "single",
                 "amount": 2}) is None or fail(
    "read_row must read a None description column as an unreadable row")
# a short row still skips exactly as it always did
A.read_row(["05/01/2026"], {"date": 0, "desc": 1, "mode": "single",
                            "amount": 2}) is None or fail(
    "a short row must still be skipped")
A.read_row(ROW, {"date": 0, "desc": 1, "mode": "single", "amount": 2}) \
    == ("2026-05-01", "FAKE GUARD CO", -1000) or fail(
    "a readable row must still read exactly as before")

# 22H. both money groups render whatever the detected mode is, so the toggle has
# something to toggle — and every money select names a real column, so a switch
# previews real data instead of nothing.
for text, name, shape in ((PLAIN, "groups_single.csv", "a single-detected file"),
                          (REVERSE, "groups_dc.csv", "a debitcredit-detected file")):
    html = upload_csv(text, name).data
    for field in (b"amount_col", b"debit_col", b"credit_col"):
        b"name=" + field in html or fail(
            f"{shape} should still render the {field.decode()} select")
    b"name=sign" in html or fail(f"{shape} should still render the sign select")
    b"data-money=single" in html or fail(f"{shape} is missing the single-amount group")
    b"data-money=debitcredit" in html or fail(f"{shape} is missing the debit/credit group")
    for field in ("amount_col", "debit_col", "credit_col"):
        selected_col(html, field) is not None or fail(
            f"{shape} rendered {field} with no column chosen")
    selected_col(html, "debit_col") != selected_col(html, "credit_col") or fail(
        f"{shape} defaulted Debit and Credit to the same column")
    # JS-off degradation: neither group may be hidden by an attribute the
    # script has to remove — the page has to work with no script at all.
    b"data-money=single hidden" not in html and b"data-money=debitcredit hidden" not in html \
        or fail(f"{shape} hid a money group server-side; JS-off would lose it")

# 22I. detection still wins where it has an answer: the switch only fills in the
# columns detection left as None.
html = upload_csv(REVERSE, "detect_wins.csv").data
(selected_col(html, "debit_col"), selected_col(html, "credit_col")) == (2, 3) or fail(
    "the detected debit/credit pair should stay selected")
selected_col(html, "amount_col") is not None or fail(
    "the unused single-amount select still needs a real column")

# 22J. switching works: the same single-amount upload previewed as debit/credit
# with valid columns shows the debit/credit-derived amounts.
phase1 = upload_csv(TOGGLE, "toggle.csv")
b"row skipped under this mapping" in phase1.data or fail(
    "under auto-detect TOGGLE's empty Amount column makes every row unreadable")
r = preview(phase1, date_col="0", desc_col="1", mode="debitcredit",
            debit_col="3", credit_col="4")
r.status_code == 200 or fail("the switched preview should render")
b"FAKE TOGGLE HARDWARE" in r.data or fail("the switched preview should show rows")
b"-$5.21" in r.data or fail("a money-out row should preview negative under debit/credit")
b"$10.00" in r.data or fail("a money-in row should preview positive under debit/credit")

# 22K. reverse direction: a debitcredit-detected file previewed as a single
# signed column.
phase1 = upload_csv(REVERSE, "reverse.csv")
r = preview(phase1, date_col="0", desc_col="1", mode="single", amount_col="4",
            sign="negative")
r.status_code == 200 or fail("the reverse switch should render")
b"-$12.34" in r.data or fail("the signed column should preview its own sign")
b"$500.00" in r.data or fail("the money-in row should preview positive")
b"row skipped under this mapping" not in r.data or fail(
    "every row should read under the single-column mapping")

# 22L. ...and a preview never writes, whichever way it was switched
ledger_size() == before or fail(f"a switched preview wrote to the ledger: {ledger_size()}")

# 22M. committing under each mode is unchanged: the same upload imports the
# amounts its preview showed.
r = import_csv(TOGGLE, "toggle_commit.csv", date_col="0", desc_col="1",
               mode="debitcredit", debit_col="3", credit_col="4")
b"Imported 2 new" in r.data or fail("both rows should import under the switched mode")
cents_list("FAKE TOGGLE HARDWARE") == [-521] or fail(
    f"debit 5.21 stored as {cents_list('FAKE TOGGLE HARDWARE')}, expected [-521]")
cents_list("FAKE TOGGLE REFUND") == [1000] or fail(
    f"credit 10.00 stored as {cents_list('FAKE TOGGLE REFUND')}, expected [1000]")

r = import_csv(REVERSE, "reverse_commit.csv", date_col="0", desc_col="1",
               mode="single", amount_col="4", sign="negative")
b"Imported 2 new" in r.data or fail("both rows should import under the single column")
cents_list("FAKE REVERSE GROCER") == [-1234] or fail(
    f"-12.34 stored as {cents_list('FAKE REVERSE GROCER')}, expected [-1234]")
cents_list("FAKE REVERSE PAYCHECK") == [50000] or fail(
    f"500.00 stored as {cents_list('FAKE REVERSE PAYCHECK')}, expected [50000]")

# ...and re-importing that same file under its OWN detected debit/credit mapping
# is all duplicates: the two modes read the file to the same cents, which is the
# switch being cosmetic to the ledger and nothing more.
r = import_csv(REVERSE, "reverse_detected.csv")
b"Imported 0 new" in r.data or fail(
    f"the detected mapping should agree with the switched one: {r.data[-200:]}")
cents_list("FAKE REVERSE GROCER") == [-1234] or fail(
    f"the detected Debit column disagreed: {cents_list('FAKE REVERSE GROCER')}")

# 22N. the toggle script is on the page, and it is the inline vanilla-JS kind:
# no library, no CDN, nothing fetched.
html = upload_csv(PLAIN, "script.csv").data
b'id=map-form' in html or fail("the mapping form needs an id for the toggle script to find")
b'input[name=mode]' in html or fail("the toggle script should read the money-style radios")
b"[data-money]" in html or fail("the toggle script should select the money groups")
b"src=" not in html.split(b"<script>")[-1] or fail(
    "the mapping page must not load a script from anywhere")


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

# 24. transactions page: Debit / Credit columns and whitelisted sorting
#
# Inserted straight into the table (with valid dedup keys) so these assertions
# are about the page, not about the import flow.
conn.executemany(
    """INSERT INTO transactions
       (account_id, txn_date, description, merchant_norm, amount_cents,
        dedup_key, created_at)
       VALUES (?,?,?,?,?,?,?)""",
    [(1, "2026-06-01", "DEBITSIDE RENT", "DEBITSIDE", -33700,
      "dc-debit-1", "now"),
     (1, "2026-06-02", "CREDITSIDE PAYOUT", "CREDITSIDE", 60000,
      "dc-credit-1", "now")])
conn.commit()

page_html = client.get("/transactions").data.decode()
head = page_html[page_html.index("<table id="):page_html.index("</tr>")]
"Debit" in head and "Credit" in head or fail(
    "the transactions header should carry Debit and Credit columns")
">Amount<" not in head or fail("the lone Amount header should be gone")


def cells(description):
    """The two money cells of the row whose description matches."""
    at = page_html.index(description)
    row = page_html[at:page_html.index("</tr>", at)]
    return re.findall(r"<td class='r num ed'[^>]*>([^<]*)</td>", row)


cells("DEBITSIDE RENT") == ["-$337.00", ""] or fail(
    f"a negative amount belongs in the debit cell, got {cells('DEBITSIDE RENT')}")
cells("CREDITSIDE PAYOUT") == ["", "$600.00"] or fail(
    f"a positive amount belongs in the credit cell, got {cells('CREDITSIDE PAYOUT')}")


def rendered_rows(query=""):
    r = client.get("/transactions" + query)
    r.status_code == 200 or fail(f"/transactions{query} returned {r.status_code}")
    body = r.data.decode()
    body = body[body.index("<table id="):]
    # rows now carry data-* hooks for the inline editor
    return re.findall(
        r"<tr data-id='\d+'[^>]*>"
        # the leftmost cell is now the multi-select checkbox, not a data cell
        r"<td class=pick>.*?</td>"
        r"<td class='ed' data-field='date' tabindex='0'>(\d{4}-\d{2}-\d{2})</td>"
        r"<td class='ed' data-field='description' tabindex='0'>(.*?)</td>", body)


def rendered_cents(query=""):
    r = client.get("/transactions" + query)
    r.status_code == 200 or fail(f"/transactions{query} returned {r.status_code}")
    body = r.data.decode()
    out = []
    for money_cells in re.findall(
            r"<td class='r num ed'[^>]*>([^<]*)</td><td class='r num ed'[^>]*>([^<]*)</td>",
            body):
        shown = money_cells[0] or money_cells[1]
        out.append(shown)
    return out


def to_cents(shown):
    neg = shown.startswith("-")
    digits = shown.lstrip("-").lstrip("$").replace(",", "").replace(".", "")
    n = int(digits) if digits else 0
    return -n if neg else n


# 24A. sort=amount&dir=asc orders by the signed cents, most negative first
asc = [to_cents(s) for s in rendered_cents("?sort=amount&dir=asc")]
asc == sorted(asc) or fail(f"amount asc is not ascending by signed cents: {asc[:5]}")
asc[0] == min(asc) or fail("the most negative amount should be on top for amount asc")

desc = [to_cents(s) for s in rendered_cents("?sort=amount&dir=desc")]
desc == sorted(desc, reverse=True) or fail(
    f"amount desc is not descending by signed cents: {desc[:5]}")

# 24B. bad input can never reach SQL — it falls back to the date-desc default
default_order = rendered_rows()
rendered_rows("?sort=bogus&dir=bogus") == default_order or fail(
    "an unrecognized sort/dir must fall back to the default ordering")
rendered_rows("?sort=amount&dir=bogus") == default_order or fail(
    "an unrecognized dir must fall back to the default ordering")
rendered_rows("?sort=t.id;DROP TABLE transactions--&dir=asc") == default_order or fail(
    "an injection attempt must fall back to the default ordering")
conn.execute("SELECT COUNT(*) n FROM transactions").fetchone()["n"] > 0 or fail(
    "the transactions table should still be intact")

# 24C. no query params must order exactly as it did before sorting existed
expected = [(r["txn_date"], r["description"]) for r in conn.execute(
    "SELECT txn_date, description FROM transactions ORDER BY txn_date DESC, id DESC")]
[(d, esc_desc.split("<")[0]) for d, esc_desc in default_order] == [
    (d, A.esc(t).split("<")[0]) for d, t in expected] or fail(
    "the unsorted transactions page changed its default ordering")

# 24D. the sorted date column round-trips both ways
dates_asc = [d for d, _ in rendered_rows("?sort=date&dir=asc")]
dates_asc == sorted(dates_asc) or fail("date asc is not ascending")
"Date ▲" in client.get("/transactions?sort=date&dir=asc").data.decode() or fail(
    "the active column should carry an ascending arrow")
"Date ▼" in client.get("/transactions").data.decode() or fail(
    "the default page should mark Date as sorted descending")

# 25. transactions grid: inline editing via POST /transactions/update
#
# The rule this whole surface is built around: the grid CORRECTS a row, it never
# TEACHES a merchant. Rule learning belongs to /review and /categorize.
import json as J  # noqa: E402

conn.execute(
    """INSERT INTO transactions
       (account_id, txn_date, description, merchant_norm, amount_cents,
        dedup_key, created_at, category_source)
       VALUES (1,'2026-07-01','GRIDEDIT SUPPLY CO','GRIDEDIT SUPPLY',
               -4200,'grid-edit-1','now','none')""")
conn.commit()
grid_id = conn.execute(
    "SELECT id FROM transactions WHERE dedup_key='grid-edit-1'").fetchone()["id"]


def txn(tid):
    return conn.execute(
        """SELECT t.*, c.name catname FROM transactions t
             LEFT JOIN categories c ON c.id=t.category_id WHERE t.id=?""",
        (tid,)).fetchone()


def update(**form):
    return client.post("/transactions/update", data=form)


def rules_count():
    return conn.execute("SELECT COUNT(*) n FROM merchant_rules").fetchone()["n"]


# 25A. THE MARQUEE TEST — a category edit on the grid teaches nothing.
before_rules = rules_count()
r = update(id=grid_id, category="Office Supplies")
r.status_code == 200 or fail(f"a category edit returned {r.status_code}")
out = J.loads(r.data)
out["ok"] is True or fail("a category edit should answer ok:true")
out["category"] == "Office Supplies" or fail(f"response category was {out['category']}")
out["reviewed"] == 1 or fail("a category edit should report the row reviewed")

row = txn(grid_id)
row["catname"] == "Office Supplies" or fail("the category edit did not store the category")
row["category_source"] == "user" or fail(
    f"a grid category edit must be category_source='user', got {row['category_source']}")
row["reviewed"] == 1 or fail("a grid category edit must mark the row reviewed")

rules_count() == before_rules or fail(
    "editing a category on the grid must NOT create a merchant rule")
conn.execute("SELECT COUNT(*) n FROM merchant_rules WHERE merchant_norm=?",
             ("GRIDEDIT SUPPLY",)).fetchone()["n"] == 0 or fail(
    "the grid taught a merchant_rules row for GRIDEDIT SUPPLY — it must never teach")

# 25B. an unknown category name lands on Uncategorized, not an error
J.loads(update(id=grid_id, category="Not A Real Category").data)["category"] \
    == "Uncategorized" or fail("an unknown category name should fall back to Uncategorized")
txn(grid_id)["catname"] == "Uncategorized" or fail(
    "the unknown-category fallback was not stored")
rules_count() == before_rules or fail("the fallback path taught a merchant rule")

# 25C. use=bogus falls back to the account default; use=personal is honored.
#      Neither touches `reviewed`.
account_default = conn.execute(
    "SELECT default_use FROM accounts WHERE id=1").fetchone()["default_use"]
conn.execute("UPDATE transactions SET reviewed=0, use=NULL WHERE id=?", (grid_id,))
conn.commit()

out = J.loads(update(id=grid_id, use="bogus").data)
out["use"] == account_default or fail(
    f"use=bogus should fall back to the account default, got {out['use']}")
txn(grid_id)["use"] == account_default or fail("the use fallback was not stored")
txn(grid_id)["reviewed"] == 0 or fail("a use edit must not mark the row reviewed")

out = J.loads(update(id=grid_id, use="personal").data)
out["use"] == "personal" or fail(f"use=personal was not honored, got {out['use']}")
txn(grid_id)["use"] == "personal" or fail("use=personal was not stored")
txn(grid_id)["reviewed"] == 0 or fail("a use edit must not change reviewed")

# ...and a use edit on an already-reviewed row leaves it reviewed
conn.execute("UPDATE transactions SET reviewed=1 WHERE id=?", (grid_id,))
conn.commit()
update(id=grid_id, use="business")
txn(grid_id)["reviewed"] == 1 or fail("a use edit must not un-review a row")

# 25D. a note-only edit saves the note (trimmed) and leaves reviewed alone
conn.execute("UPDATE transactions SET reviewed=0 WHERE id=?", (grid_id,))
conn.commit()
out = J.loads(update(id=grid_id, note="  paper + toner  ").data)
out["note"] == "paper + toner" or fail(f"the note should be stored trimmed, got {out['note']!r}")
txn(grid_id)["note"] == "paper + toner" or fail("the note edit was not stored")
txn(grid_id)["reviewed"] == 0 or fail("a note edit must not mark the row reviewed")
rules_count() == before_rules or fail("a note edit taught a merchant rule")

# 25E. only the fields sent are applied — a use edit leaves the note untouched
update(id=grid_id, use="personal")
txn(grid_id)["note"] == "paper + toner" or fail(
    "a partial update wiped a field that wasn't in the form")

# 25F. money, date, description, merchant_norm and dedup_key are never touched
frozen = txn(grid_id)
update(id=grid_id, category="Meals", use="business", note="everything at once")
after = txn(grid_id)
for col in ("amount_cents", "txn_date", "description", "merchant_norm", "dedup_key"):
    after[col] == frozen[col] or fail(f"/transactions/update changed {col}")
isinstance(after["amount_cents"], int) or fail("amount_cents stopped being an integer")

# 25G. a missing or unknown id is a 404 that writes nothing
snapshot = (conn.execute("SELECT COUNT(*) n FROM transactions").fetchone()["n"],
            rules_count(), txn(grid_id)["note"])
for bad in ({}, {"id": ""}, {"id": "999999", "note": "ghost"},
            {"id": "not-a-number", "category": "Meals"}):
    r = client.post("/transactions/update", data=bad)
    r.status_code == 404 or fail(f"id={bad.get('id')!r} should be 404, got {r.status_code}")
    J.loads(r.data)["ok"] is False or fail(f"id={bad.get('id')!r} should answer ok:false")
(conn.execute("SELECT COUNT(*) n FROM transactions").fetchone()["n"],
 rules_count(), txn(grid_id)["note"]) == snapshot or fail(
    "a 404 update wrote something")

# 25H. the page hands the editor its hooks: data-id rows and the category JSON
grid_html = client.get("/transactions").data.decode()
f"data-id='{grid_id}'" in grid_html or fail("rows should expose data-id for the editor")
'data-field=\'category\'' in grid_html or fail("the category cell needs an edit hook")
'data-field=\'use\'' in grid_html or fail("the use cell needs an edit hook")
'data-field=\'note\'' in grid_html or fail("the note cell needs an edit hook")
'id="txn-grid"' in grid_html or fail("the table should be addressable as #txn-grid")
'<script type="application/json" id="cat-list">' in grid_html or fail(
    "the category list should be embedded as a JSON block")

start = grid_html.index('id="cat-list">') + len('id="cat-list">')
embedded = J.loads(grid_html[start:grid_html.index("</script>", start)])
embedded == D.category_names(conn) or fail(
    "the embedded category list should be exactly the active category names")

# ...and the read-only columns carry no edit hook
grid_row = grid_html[grid_html.index(f"data-id='{grid_id}'"):]
grid_row = grid_row[:grid_row.index("</tr>")]
editable = grid_row.count("data-field=")
editable == 7 or fail(
    "date/description/category/use/note/debit/credit should all be editable, "
    f"found {editable}")
"<td class='status'>" in grid_row or fail("Status stays read-only")
"-$42.00" in grid_row or fail("the debit cell should still show the amount")

# 25I. injection guard: a note full of markup can't break the page or the JSON
nasty = '</script><script>alert(1)</script> "quoted" & <b>bold</b>'
update(id=grid_id, note=nasty)
txn(grid_id)["note"] == nasty.strip() or fail("the hostile note was not stored verbatim")

hostile = client.get("/transactions").data.decode()
"<script>alert(1)</script>" not in hostile or fail(
    "a note with markup reached the page unescaped")
"&lt;script&gt;alert(1)&lt;/script&gt;" in hostile or fail(
    "a note with markup should render escaped")

# the embedded JSON block still parses, and `<` inside it is neutralized
start = hostile.index('id="cat-list">') + len('id="cat-list">')
block = hostile[start:hostile.index("</script>", start)]
"<" not in block or fail("a raw < inside the embedded JSON could end the script early")
J.loads(block) == D.category_names(conn) or fail(
    "the embedded category JSON stopped parsing")

# every <script> the page opens is closed exactly once — the note didn't add one
hostile.count("<script") == hostile.count("</script>") or fail(
    "the page's script tags no longer balance")

# 25J. a category name containing < would still be escaped out of the JSON block
J.loads(A.script_json(["Meals", "</script> & <b>"])) == ["Meals", "</script> & <b>"] \
    or fail("script_json must round-trip through JSON unchanged")
"<" not in A.script_json(["</script>"]) or fail(
    "script_json must leave no raw < for the HTML parser to find")

# 25K. and the whole no-teach promise, stated once more over every edit above
rules_count() == before_rules or fail(
    "some edit on the transactions grid taught a merchant rule")

# 26. transactions grid: editing the identity columns — date, amount, description
#
# The safety property of this section: dedup_key is FROZEN. It is what makes a
# re-imported statement skip rows you already have, so correcting a typo in an
# amount must leave it byte-identical.
conn.execute(
    """INSERT INTO transactions
       (account_id, txn_date, description, merchant_norm, amount_cents,
        dedup_key, created_at, note)
       VALUES (1,'2026-08-01','IDENTITY EDIT CO','IDENTITY EDIT',
               -1999,'identity-edit-1','now','keep me')""")
conn.commit()
ident = conn.execute(
    "SELECT id FROM transactions WHERE dedup_key='identity-edit-1'").fetchone()["id"]


def frozen_key(tid):
    return conn.execute(
        "SELECT dedup_key FROM transactions WHERE id=?", (tid,)).fetchone()["dedup_key"]


def unchanged_by(tid, **form):
    """Post an edit and hand back (dedup_key before, dedup_key after)."""
    before = frozen_key(tid)
    update(id=tid, **form)
    return before, frozen_key(tid)


# 26A. an amount edit: magnitude from the text, sign from the side
conn.execute("UPDATE transactions SET reviewed=0 WHERE id=?", (ident,))
conn.commit()
key_before = frozen_key(ident)
rules_before_identity = rules_count()

out = J.loads(update(id=ident, amount="50.00", side="debit").data)
out["amount_cents"] == -5000 or fail(f"debit 50.00 should be -5000, got {out['amount_cents']}")
txn(ident)["amount_cents"] == -5000 or fail("the amount edit was not stored")
isinstance(txn(ident)["amount_cents"], int) or fail("amount_cents must stay an integer")
frozen_key(ident) == key_before or fail(
    "an amount edit rewrote dedup_key — it must be frozen at import")
txn(ident)["reviewed"] == 0 or fail("an amount edit must not mark the row reviewed")

# side flips the sign, magnitude is taken as an absolute value either way
J.loads(update(id=ident, amount="50.00", side="credit").data)["amount_cents"] == 5000 \
    or fail("credit 50.00 should be +5000")
J.loads(update(id=ident, amount="-50.00", side="credit").data)["amount_cents"] == 5000 \
    or fail("a typed minus sign must not beat the chosen side")
J.loads(update(id=ident, amount="$1,234.56", side="debit").data)["amount_cents"] == -123456 \
    or fail("$1,234.56 as a debit should be -123456")
J.loads(update(id=ident, amount="0", side="debit").data)["amount_cents"] == 0 \
    or fail("a zero amount is allowed and stores 0")

# 26B. an unreadable amount, or a side that isn't a column, is a 400 that
#      writes nothing
for bad in ({"amount": "abc", "side": "debit"},
            {"amount": "", "side": "debit"},
            {"amount": "12.00", "side": "sideways"},
            {"amount": "12.00"}):                 # side is not optional
    snapshot = (txn(ident)["amount_cents"], frozen_key(ident))
    r = client.post("/transactions/update", data=dict(bad, id=ident))
    r.status_code == 400 or fail(f"{bad} should be 400, got {r.status_code}")
    J.loads(r.data)["ok"] is False or fail(f"{bad} should answer ok:false")
    (txn(ident)["amount_cents"], frozen_key(ident)) == snapshot or fail(
        f"a rejected amount ({bad}) still wrote something")

# 26C. dates go through normalize_date, in either notation
for typed in ("01/15/2026", "2026-01-15"):
    before, after = unchanged_by(ident, date=typed)
    txn(ident)["txn_date"] == "2026-01-15" or fail(
        f"{typed} should normalize to 2026-01-15, got {txn(ident)['txn_date']}")
    before == after or fail("a date edit rewrote dedup_key")
txn(ident)["reviewed"] == 0 or fail("a date edit must not mark the row reviewed")

snapshot = (txn(ident)["txn_date"], frozen_key(ident))
r = client.post("/transactions/update", data={"id": ident, "date": "someday"})
r.status_code == 400 or fail(f"an unreadable date should be 400, got {r.status_code}")
J.loads(r.data)["ok"] is False or fail("an unreadable date should answer ok:false")
(txn(ident)["txn_date"], frozen_key(ident)) == snapshot or fail(
    "a rejected date still wrote something")

# 26D. a description edit also recomputes merchant_norm — a matching key, not
#      an identity — and still teaches nothing
new_desc = "SQ *CIRCLE K #482 0834"
before, after = unchanged_by(ident, description=new_desc)
row = txn(ident)
row["description"] == new_desc or fail("the description edit was not stored")
row["merchant_norm"] == A.normalize_merchant(new_desc) or fail(
    f"merchant_norm should follow the description, got {row['merchant_norm']}")
before == after or fail("a description edit rewrote dedup_key")
row["reviewed"] == 0 or fail("a description edit must not mark the row reviewed")
rules_count() == rules_before_identity or fail(
    "recomputing merchant_norm taught a merchant rule — the grid must never teach")

# a description is trimmed, and cannot be blanked
update(id=ident, description="   PADDED NAME   ")
txn(ident)["description"] == "PADDED NAME" or fail("a description should be stored trimmed")
for blank in ("", "   "):
    snapshot = (txn(ident)["description"], txn(ident)["merchant_norm"], frozen_key(ident))
    r = client.post("/transactions/update", data={"id": ident, "description": blank})
    r.status_code == 400 or fail(f"a blank description should be 400, got {r.status_code}")
    (txn(ident)["description"], txn(ident)["merchant_norm"],
     frozen_key(ident)) == snapshot or fail("a rejected description still wrote something")

# 26E. one bad field rejects the whole request — no half-applied row
snapshot = (txn(ident)["description"], txn(ident)["txn_date"], txn(ident)["note"])
r = client.post("/transactions/update", data={
    "id": ident, "description": "SHOULD NOT LAND", "note": "nor this",
    "date": "not a date"})
r.status_code == 400 or fail("a request with one bad field should be 400")
(txn(ident)["description"], txn(ident)["txn_date"],
 txn(ident)["note"]) == snapshot or fail(
    "a rejected request applied its other fields — it must be all or nothing")

# ...and the same when the bad field is the LAST one evaluated, so a field
# validated earlier can't have been written on the way past
snapshot = (txn(ident)["note"], txn(ident)["txn_date"])
r = client.post("/transactions/update", data={
    "id": ident, "note": "landed?", "date": "01/01/2026", "description": ""})
r.status_code == 400 or fail("a trailing bad field should still be 400")
(txn(ident)["note"], txn(ident)["txn_date"]) == snapshot or fail(
    "fields validated before the bad one were written anyway")

# 26F. the reply carries the display strings, formatted server-side by money()
out = J.loads(update(id=ident, amount="12.50", side="debit").data)
(out["debit"], out["credit"]) == ("-$12.50", "") or fail(
    f"a debit should format as (-$12.50, ''), got {(out['debit'], out['credit'])}")
out = J.loads(update(id=ident, amount="12.50", side="credit").data)
(out["debit"], out["credit"]) == ("", "$12.50") or fail(
    f"a credit should format as ('', $12.50), got {(out['debit'], out['credit'])}")
out = J.loads(update(id=ident, amount="0", side="debit").data)
(out["debit"], out["credit"]) == ("", "") or fail(
    f"zero belongs in neither column, got {(out['debit'], out['credit'])}")
out["date"] == txn(ident)["txn_date"] or fail("the reply should carry the stored date")
out["description"] == txn(ident)["description"] or fail(
    "the reply should carry the stored description")

# 26G. THE MARQUEE TEST — edit an imported row, then re-import the same file.
#      The frozen key still matches, so the row is skipped and keeps the edit.
FREEZE_CSV = ("Date,Description,Amount\n"
              "07/04/2026,FROZEN KEY DINER,-31.40\n"
              "07/05/2026,FROZEN KEY FUEL,-52.10\n")
import_csv(FREEZE_CSV, "freeze.csv")
diner = conn.execute(
    "SELECT * FROM transactions WHERE description='FROZEN KEY DINER'").fetchone()
diner or fail("the freeze fixture did not import")
imported_key = diner["dedup_key"]
diner["amount_cents"] == -3140 or fail("the freeze fixture imported the wrong amount")

rows_before = conn.execute("SELECT COUNT(*) n FROM transactions").fetchone()["n"]
update(id=diner["id"], amount="99.99", side="credit")
update(id=diner["id"], date="12/25/2026")
update(id=diner["id"], description="CORRECTED DINER NAME")

edited = txn(diner["id"])
edited["dedup_key"] == imported_key or fail(
    f"dedup_key changed under editing: {imported_key!r} -> {edited['dedup_key']!r}")
(edited["amount_cents"], edited["txn_date"], edited["description"]) == (
    9999, "2026-12-25", "CORRECTED DINER NAME") or fail("the edits did not all land")

import_csv(FREEZE_CSV, "freeze.csv")
conn.execute("SELECT COUNT(*) n FROM transactions").fetchone()["n"] == rows_before or fail(
    "re-importing after an edit created a duplicate — the frozen key stopped matching")
after_reimport = txn(diner["id"])
(after_reimport["amount_cents"], after_reimport["txn_date"],
 after_reimport["description"]) == (9999, "2026-12-25", "CORRECTED DINER NAME") or fail(
    "the re-import overwrote the edited row")
conn.execute("SELECT COUNT(*) n FROM transactions WHERE description='FROZEN KEY DINER'"
             ).fetchone()["n"] == 0 or fail(
    "the re-import re-added the original line alongside the edited one")

# 26H. identity edits teach nothing, restated over every edit in this section
rules_count() == rules_before_identity or fail(
    "editing date / amount / description on the grid taught a merchant rule")

# 26I. the grid exposes the new edit hooks and prefill data
ident_html = client.get("/transactions").data.decode()
ident_row = ident_html[ident_html.index(f"data-id='{ident}'"):]
ident_row = ident_row[:ident_row.index("</tr>")]
for hook in ("data-field='date'", "data-field='description'",
             "data-field='debit'", "data-field='credit'",
             "data-date=", "data-description=", "data-cents="):
    hook in ident_row or fail(f"the row is missing {hook}")
f"data-cents='{txn(ident)['amount_cents']}'" in ident_row or fail(
    "data-cents should carry the signed integer cents for the editor to prefill")

# 26J. sorting by amount puts the arrow on ONE column, not both
asc_head = client.get("/transactions?sort=amount&dir=asc").data.decode()
asc_head = asc_head[:asc_head.index("</tr>")]
("Debit ▲" in asc_head and "Credit ▲" not in asc_head and "Credit ▼" not in asc_head) \
    or fail("amount asc should mark Debit only")
desc_head = client.get("/transactions?sort=amount&dir=desc").data.decode()
desc_head = desc_head[:desc_head.index("</tr>")]
("Credit ▼" in desc_head and "Debit ▼" not in desc_head and "Debit ▲" not in desc_head) \
    or fail("amount desc should mark Credit only")
# ...and both are still clickable links either way
asc_head.count("sort=amount") == 2 or fail("both money headers should stay sortable links")

# ---------------------------------------------------------------------------
# 27. transactions grid: multi-select + bulk set category / use / delete
#
# Same rule as the single-row editor, at scale: bulk CORRECTS rows and teaches
# nothing. Forty rows is exactly where a silently learned merchant rule would
# do the most damage, so the no-teach check is restated after every bulk op.
# ---------------------------------------------------------------------------

BULK_CSV = """Date,Description,Amount
2026-08-01,BULKONE HARDWARE,-11.00
2026-08-02,BULKTWO HARDWARE,-12.00
2026-08-03,BULKTHREE HARDWARE,-13.00
2026-08-04,BULKFOUR HARDWARE,-14.00
"""

import_csv(BULK_CSV, "bulk.csv")


def bulk_ids(prefix="BULK"):
    """Ids of the bulk fixture rows, in import order."""
    return [r["id"] for r in conn.execute(
        "SELECT id FROM transactions WHERE description LIKE ? ORDER BY id",
        (prefix + "%",))]


def bulk(ids=None, **form):
    data = dict(form)
    if ids is not None:
        data["ids"] = [str(i) for i in ids]
    return client.post("/transactions/bulk", data=data)


ids = bulk_ids()
len(ids) == 4 or fail(f"the bulk fixture should import 4 rows, got {len(ids)}")
rules_before_bulk = rules_count()

# 27A. THE MARQUEE TEST — a bulk category stamps the rows and teaches nothing.
r = bulk(ids[:3], action="category", value="Equipment")
r.status_code == 200 or fail(f"a bulk category returned {r.status_code}")
out = J.loads(r.data)
(out["ok"], out["action"], out["affected"]) == (True, "category", 3) or fail(
    f"bulk category answered {out}")
for tid in ids[:3]:
    row = txn(tid)
    row["catname"] == "Equipment" or fail(
        f"bulk category did not stamp {tid}: {row['catname']}")
    row["category_source"] == "user" or fail(
        f"a bulk category must be category_source='user', got {row['category_source']}")
    row["reviewed"] == 1 or fail("a bulk category must mark the row reviewed")
txn(ids[3])["catname"] != "Equipment" or fail(
    "a bulk category touched a row that was not selected")

rules_count() == rules_before_bulk or fail(
    "BULK CATEGORY TAUGHT A MERCHANT RULE — the grid must never teach")

# 27B. an unknown category name falls back to Uncategorized, like everywhere else
bulk([ids[3]], action="category", value="No Such Category").status_code == 200 or fail(
    "an unknown bulk category name should still be accepted")
txn(ids[3])["catname"] == "Uncategorized" or fail(
    f"unknown bulk category should fall back, got {txn(ids[3])['catname']}")

# 27C. bulk use sets `use` on every selected row and leaves `reviewed` alone
conn.execute("UPDATE transactions SET reviewed=0, use='business' WHERE id IN (?,?)",
             (ids[0], ids[1]))
conn.commit()
reviewed_before = [txn(t)["reviewed"] for t in ids]
r = bulk(ids[:3], action="use", value="personal")
r.status_code == 200 or fail(f"a bulk use returned {r.status_code}")
J.loads(r.data)["affected"] == 3 or fail("bulk use reported the wrong count")
for tid in ids[:3]:
    txn(tid)["use"] == "personal" or fail(f"bulk use did not set {tid}")
[txn(t)["reviewed"] for t in ids] == reviewed_before or fail(
    "a bulk use edit must not change reviewed — it is not a decision about what a row is")
txn(ids[3])["use"] != "personal" or fail(
    "bulk use touched a row that was not selected")
rules_count() == rules_before_bulk or fail("bulk use taught a merchant rule")

# 27D. an invalid use is refused outright and writes nothing
before = [(txn(t)["use"], txn(t)["reviewed"]) for t in ids]
r = bulk(ids, action="use", value="bogus")
r.status_code == 400 or fail(f"use=bogus should be 400, got {r.status_code}")
J.loads(r.data)["ok"] is False or fail("a refused bulk should answer ok:false")
[(txn(t)["use"], txn(t)["reviewed"]) for t in ids] == before or fail(
    "a refused bulk use wrote something anyway")

# 27E. no valid ids, and an unknown action, are both refused with nothing written
rows_before_bad = conn.execute("SELECT COUNT(*) n FROM transactions").fetchone()["n"]
for bad in ({"ids": ["x", "", "12.5"], "action": "category", "value": "Equipment"},
            {"action": "delete"}):
    r = client.post("/transactions/bulk", data=bad)
    r.status_code == 400 or fail(f"{bad} should be 400, got {r.status_code}")
    J.loads(r.data)["ok"] is False or fail("a refused bulk should answer ok:false")
r = bulk(ids, action="teach")
r.status_code == 400 or fail(f"an unknown action should be 400, got {r.status_code}")
r = bulk(ids, action="")
r.status_code == 400 or fail("an empty action should be 400")
conn.execute("SELECT COUNT(*) n FROM transactions").fetchone()["n"] == rows_before_bad or fail(
    "a refused bulk deleted rows")

# 27F. injection guard — an id is an int or it is not an id
r = client.post("/transactions/bulk", data={
    "ids": ["1); DROP TABLE transactions--", "'; DELETE FROM transactions; --"],
    "action": "delete"})
r.status_code == 400 or fail("ids that are not ints must leave nothing valid to act on")
conn.execute("SELECT COUNT(*) n FROM transactions").fetchone()["n"] == rows_before_bad or fail(
    "the transactions table did not survive an injection attempt")
conn.execute("SELECT COUNT(*) n FROM sqlite_master WHERE name='transactions'"
             ).fetchone()["n"] == 1 or fail("the transactions table was dropped")
# ...and a good id mixed in with junk still works, junk dropped
r = bulk(["nope", ids[0], "1;--"], action="category", value="Meals")
J.loads(r.data)["affected"] == 1 or fail("junk ids should be dropped, not fatal")
txn(ids[0])["catname"] == "Meals" or fail("the one real id in the list should still apply")

# 27G. bulk delete removes exactly the selected rows and reports the count
survivor = ids[3]
r = bulk(ids[:2], action="delete")
r.status_code == 200 or fail(f"a bulk delete returned {r.status_code}")
out = J.loads(r.data)
(out["ok"], out["action"], out["affected"]) == (True, "delete", 2) or fail(
    f"bulk delete answered {out}")
for tid in ids[:2]:
    txn(tid) is None or fail(f"row {tid} should be gone after a bulk delete")
txn(ids[2]) is not None and txn(survivor) is not None or fail(
    "a bulk delete removed rows that were not selected")
rules_count() == rules_before_bulk or fail("bulk delete taught a merchant rule")

# 27H. delete <-> dedup: a deleted row's dedup_key goes with it, so re-importing
# the same statement brings that line back — and only that line.
gone_desc = "BULKONE HARDWARE"
conn.execute("SELECT COUNT(*) n FROM transactions WHERE description=?",
             (gone_desc,)).fetchone()["n"] == 0 or fail("the fixture row is still here")
import_csv(BULK_CSV, "bulk.csv")
back = conn.execute(
    "SELECT COUNT(*) n FROM transactions WHERE description LIKE 'BULK%'").fetchone()["n"]
back == 4 or fail(f"re-import should restore the 2 deleted rows and no more, got {back}")
for desc in ("BULKONE HARDWARE", "BULKTWO HARDWARE"):
    conn.execute("SELECT COUNT(*) n FROM transactions WHERE description=?",
                 (desc,)).fetchone()["n"] == 1 or fail(f"{desc} did not come back exactly once")
for desc in ("BULKTHREE HARDWARE", "BULKFOUR HARDWARE"):
    conn.execute("SELECT COUNT(*) n FROM transactions WHERE description=?",
                 (desc,)).fetchone()["n"] == 1 or fail(
        f"the surviving row {desc} was duplicated by the re-import")
# the surviving rows kept the categories they were bulk-stamped with
txn(ids[2])["catname"] == "Equipment" or fail(
    "the re-import overwrote a surviving row's bulk-set category")

# 27I. bulk delete leaves import_batches alone — the history counts live
batches_before = conn.execute("SELECT COUNT(*) n FROM import_batches").fetchone()["n"]
doomed = bulk_ids()[:1]
bulk(doomed, action="delete").status_code == 200 or fail("bulk delete failed")
conn.execute("SELECT COUNT(*) n FROM import_batches").fetchone()["n"] == batches_before or fail(
    "bulk delete removed an import_batches row — the history should count live instead")
client.get("/import").status_code == 200 or fail(
    "the import history should still render after a bulk delete")

# 27J. no teach, restated across every bulk op in this section
rules_count() == rules_before_bulk or fail(
    "some bulk operation taught a merchant rule — bulk must never teach")

# 27K. the grid renders the select column and a hidden bulk bar
bulk_html = client.get("/transactions").data.decode()
"id=sel-all" in bulk_html or fail("the header should carry a select-all checkbox")
bulk_html.count("class=rowsel") == conn.execute(
    "SELECT COUNT(*) n FROM transactions").fetchone()["n"] or fail(
    "every row should carry a select checkbox")
'<div id="bulk-bar" class=bulk hidden>' in bulk_html or fail(
    "the bulk bar should render hidden — with JS off it must stay inert")
for hook in ('data-bulk="category"', 'data-bulk="business"', 'data-bulk="personal"',
             'data-bulk="delete"', 'data-bulk="clear"', 'id="bulk-cat"',
             'id="bulk-count"'):
    hook in bulk_html or fail(f"the bulk bar is missing {hook}")
# the select column is not an editable cell, so a click there opens no editor
"<td class=pick><input type=checkbox class=rowsel" in bulk_html or fail(
    "the select cell must be a plain td.pick, never a td.ed")
"class='ed' data-field='pick'" not in bulk_html or fail(
    "the select column must not be an editable field")
# the bulk category picker offers the same vocabulary as everything else
for name in D.category_names(conn):
    f'<option value="{A.esc(name)}">' in bulk_html or fail(
        f"the bulk category select is missing {name}")

# 27L. nothing in this feature is a schema change
have = {r["name"] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table'")}
have >= {"transactions", "merchant_rules", "import_batches", "accounts",
         "categories", "transfers", "allocations", "documents", "attachments"} or fail(
    "a table went missing")
cols = {r[1] for r in conn.execute("PRAGMA table_info(transactions)")}
cols == {"id", "account_id", "txn_date", "description", "merchant_norm",
         "amount_cents", "use", "category_id", "category_source", "ai_confidence",
         "reviewed", "reconciled", "note", "import_batch_id", "dedup_key",
         "created_at"} or fail(f"the transactions columns changed: {sorted(cols)}")

# 27M. the endpoint never teaches, in the source as well as in behavior
src = io.open("app.py", encoding="utf-8").read()
handler = src[src.index("def transactions_bulk("):]
handler = handler[:handler.index("\ndef ", 1)]
# the docstring names both on purpose; what matters is the code under it
code = handler.split('"""')[2]
"teach_merchant_rule" not in code or fail(
    "/transactions/bulk must not call teach_merchant_rule")
"import_batches" not in code or fail(
    "/transactions/bulk must not write to import_batches")

os.remove("_test.sqlite")
print("ALL TESTS PASSED")
