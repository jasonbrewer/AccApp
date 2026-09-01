# LocalLedger — project brief for Claude Code

Read this first. It's the contract for how this project is built. Follow the
invariants; they encode decisions that are expensive to reverse later.

## What this is

A **local, private** personal + business ledger for one user. Workflow: drop in a
bank/credit-card CSV (and later a pile of receipts/PDFs/emails), auto-categorize,
then confirm and reconcile in an accounting-style UI. Built local specifically so
financial data never goes to a cloud AI.

Guiding principle: **AI suggests *what* a transaction is; deterministic code
computes *how much*.** The math is never left to a model.

## Run & test

```bash
pip3 install flask          # only dependency
python3 app.py              # -> http://127.0.0.1:5000
python3 tests.py            # smoke tests — run before AND after every change
```

Target platform is macOS **Ventura** (13). Don't introduce anything that needs a
newer OS or that won't run on Ventura.

## Architecture (three files, deliberate boundaries)

- **`db.py` — the durable core.** SQLite schema + seed data. This is what ports
  unchanged to a future native Swift/GRDB app, so treat the schema as long-lived.
  Stub tables (`transfers`, `allocations`, `documents`, `attachments`) already
  exist so upcoming milestones need **no migration**.
- **`categorizer.py` — the ONLY place AI lives.** Behind one small interface:
  `Categorizer.categorize()` runs rule → local Ollama → fallback. Keep all model
  logic here. If the project ever targets the Mac App Store, this single file gets
  swapped for a bundled on-device model and nothing else changes.
- **`app.py` — the local web UI.** Flask, **mostly** server-rendered: import,
  two-at-a-time review, transactions, dashboard. One exception — the
  Transactions page is a client-interactive grid. A small amount of vanilla JS
  (inline, no libraries, no CDN, localhost only) adds inline cell editing with
  autosave and multi-select + bulk actions on top of a table that still renders
  and reads fine with JS off. Every other page is plain server-rendered HTML.
- **The Transactions grid never teaches.** It is a deliberate non-teaching
  corrections surface — editing a row there fixes that row and writes no
  `merchant_rules` entry. Merchant-rule learning lives on the Categorize page
  (and `/review`), where the user is deciding for a whole merchant on purpose.
  - **Bulk actions on the grid are non-teaching too.** Multi-select plus
    `POST /transactions/bulk` (set category, set use, delete) stamps the rows
    you ticked and nothing else — no `merchant_rules` row, ever. Forty rows at
    once is precisely where a silently learned rule would do the most damage,
    so the rule is stricter here, not looser. A test enforces it.
  - **Bulk delete really deletes.** The row goes, and its `dedup_key` goes with
    it, so re-importing the same statement re-adds exactly what you removed.
    That is the same bargain as undoing an import, and it is deliberate: there
    is no tombstone. Bulk delete leaves `import_batches` alone — the import
    history counts the rows still present, so it stays honest on its own.

## Invariants — do not break these

1. **Money is integer cents.** Never store or compute money as float. Parse to
   cents at the boundary (`parse_amount_to_cents`). A test enforces this.
2. **Fully usable with Ollama off.** AI is additive. Rules + manual review must
   always work with no model present. Never put AI on the critical path.
3. **Nothing leaves the machine at runtime.** The only network call is to
   `http://localhost:11434` (the user's own Ollama). No other outbound calls,
   analytics, or telemetry. Ever.
4. **Never trust model output.** Validate the LLM's category against the known
   list; on any bad/failed response fall back to "Uncategorized". (See
   `ollama_lookup`.)
5. **One SQLite file per year** (`ledger_YYYY.sqlite`). Don't merge years into
   one file.
   - Note: saved column-import profiles are deliberately kept OUTSIDE the yearly
     file. They live in a single local JSON file next to `app.py` — path from
     the `LEDGER_PROFILES` env var, default `localledger_profiles.json`. A
     bank's CSV layout belongs to the bank, not to a year, so profiles are
     global across years; the DB schema is unaffected by them.
6. **The categorizer boundary stays clean.** UI and DB code must not call Ollama
   directly — go through `Categorizer`.
7. **Import is idempotent.** Re-importing an overlapping statement must not create
   duplicates (`dedup_key` UNIQUE). Preserve this.
   - **`dedup_key` is frozen at import.** No edit ever recomputes or rewrites
     it — correcting a date, an amount or a description on the Transactions
     grid leaves it byte-identical, so re-importing the original statement
     still recognizes the row and skips it. The key identifies *the line as the
     bank sent it*, not the line as you've since corrected it.
   - `merchant_norm`, by contrast, IS recomputed from an edited description: it
     is a live matching key derived from the text, not an identity. Deriving it
     writes no `merchant_rules` row — the grid still never teaches.
   - Both notes above are the grid's half of the bargain with invariant 8: a
     correction there is a correction, never a lesson. See the note under 8.
8. **Confirmations teach rules.** When the user sets a category in review, upsert
   a `merchant_rules` row so it's rule-matched next time. Keep this learning loop.
   - Note: teaching happens on Categorize and Review, and only there. The
     Transactions grid is a non-teaching corrections surface — editing a cell,
     or bulk-setting a category/use across the rows you ticked, stamps those
     rows and NEVER writes a `merchant_rules` entry. Tests enforce it.

## Conventions

- Merchant matching uses `normalize_merchant()` (strips store #s / noise, keeps
  ~2 leading words). Rule lookup matches exact OR rule-is-a-leading-prefix, most
  specific (longest) rule wins.
- Categories are broad and few (see `SEED_CATEGORIES`) — tags/aliases over deep
  trees. Don't proliferate categories.
- `use` = business | personal, per-transaction, defaulting to the account's
  `default_use`.
- Keep the UI quiet: tabular figures for amounts, one accent color reserved for
  settled/reviewed state, sentence-case labels.

## Definition of done for any change

- `python3 tests.py` passes.
- App still starts and the affected page renders.
- App still works with Ollama off.
- No new runtime dependency beyond Flask unless there's a clear reason.

## Privacy note for development (important for this user)

Claude Code is cloud-backed — files it reads are sent to Anthropic's API. That's a
different layer from the app's runtime privacy (Ollama is local). **While
developing, test with `sample_statement.csv` or synthetic data — do not paste a
real bank statement into a coding session.** The finished app is local; the
assistant building it is not.

## Roadmap

- **M1 (done):** accounts, CSV import + dedup, rule/Ollama/fallback
  categorization, two-at-a-time review with rule learning, spending dashboard.
- **M2 (next): document dump.** Drop a folder of receipts / PDFs / `.eml` emails.
  Extract text locally (Apple Vision for images, direct text for PDFs/emails),
  copy files into the book, dedup by SHA-256 (`documents` table), then match each
  document to a transaction by amount + date + merchant for visual confirmation
  (`attachments` table). Keep it local; keep AI optional.
- **M3:** transfers (two-sided matching) and splits (one line → many
  category/use allocations via `allocations`).
- **M4:** the Monthly Nut — Must Pay / Must Buy / Optional, business & personal.
- **M5:** reconciliation against a statement balance (cleared/uncleared).

When starting a milestone, extend the schema stubs already in `db.py` rather than
redesigning, and add tests to `tests.py` alongside the feature.
