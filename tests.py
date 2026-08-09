"""
LocalLedger — smoke tests for the Milestone 1 loop.

Run before and after any change:
    python3 tests.py

Uses a throwaway database and never touches your real ledger files.
Ollama is not required — these exercise the deterministic rule + fallback path,
which is the behavior you must never break (the app has to work with AI off).
"""

import os
import re
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

os.remove("_test.sqlite")
print("ALL TESTS PASSED")
