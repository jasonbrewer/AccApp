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
def import_sample():
    with open("sample_statement.csv", "rb") as f:
        return client.post("/import", data={"account_id": "1", "file": (f, "s.csv")},
                           content_type="multipart/form-data")

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

def import_csv(text, name="x.csv", account_id="1"):
    """Post an in-memory CSV through the real import route."""
    return client.post(
        "/import",
        data={"account_id": account_id, "file": (io.BytesIO(text.encode()), name)},
        content_type="multipart/form-data")


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

os.remove("_test.sqlite")
print("ALL TESTS PASSED")
