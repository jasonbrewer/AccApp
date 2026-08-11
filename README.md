# LocalLedger — Milestone 1

A local, private personal + business ledger. Drop in a bank/credit CSV, let it
categorize, then confirm and reconcile two transactions at a time. **Everything
runs on your machine.** The browser is only the window — no data leaves your Mac,
and it runs fine on macOS Ventura.

## Run it

```bash
cd localledger
pip3 install flask          # only dependency
python3 app.py
```

Then open <http://127.0.0.1:5000> and click **Import** → choose *Business Checking*
→ upload `sample_statement.csv` to see the whole loop with realistic data.

To point at your own data, just import your bank's CSV. A new database file
`ledger_2026.sqlite` is created next to `app.py` (one file per year).

## The local AI is optional

With **Ollama** installed and running, unknown merchants get a suggested category;
without it, you get rule-based matching plus manual review, and the app is fully
usable. The dashboard shows whether it's connected.

```bash
# optional — enables AI suggestions, still 100% local
brew install ollama        # or download from ollama.com
ollama pull llama3.2       # or qwen2.5 / mistral
ollama serve
```

The model name lives at the top of `categorizer.py` (`DEFAULT_MODEL`).
The app only ever calls `http://localhost:11434` — your own machine. Nothing is
sent to any cloud service, ever. You can confirm that by running it with your
network off.

## What this build does

- **Import** a bank/credit CSV, with automatic column detection and
  **duplicate detection** (re-importing an overlapping statement is safe).
- **Categorize** each transaction: learned rule → local Ollama → "Uncategorized".
- **Review two at a time** — confirm or change the category and business/personal
  use. Every confirmation **teaches a rule**, so the app needs you less over time.
- **Dashboard** with spending by category, split business vs. personal.

## How it's built (and why it ports later)

Three files, with a deliberate boundary:

- `db.py` — SQLite schema + seed data. **This is the durable core.** The same
  `.sqlite` file and schema open unchanged in a future native (Swift/GRDB) app.
  Money is stored as integer cents. Stub tables for transfers, splits, documents,
  and attachments already exist so later milestones need no migration.
- `categorizer.py` — the **only** place the AI lives, behind one small interface.
  Today it points at Ollama. If you ever ship on the Mac App Store (where an
  external Ollama server is a review problem), you swap *only this file* for a
  bundled on-device model. Nothing else changes.
- `app.py` — the local web UI (import, review, transactions, dashboard).

## Roadmap (next milestones)

2. **Document dump** — drop a folder of receipts / PDFs / emails; extract text
   locally (Apple Vision for images, direct text for PDFs/`.eml`), then match each
   to a transaction by amount + date + merchant for you to confirm visually.
3. **Transfers & splits** — two-sided transfer matching; one line split across
   categories / business+personal.
4. **The Monthly Nut** — Must Pay / Must Buy / Optional, business & personal.
5. **Reconciliation** — mark transactions cleared against a statement balance.

Then, if you want the native Mac feel, port the UI to SwiftUI — the schema,
matching logic, and category taxonomy come straight across.
