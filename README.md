# abn-invoice

A local tool for generating invoices and reimbursement claims as PDFs, with a browser UI and a CSV-backed transaction ledger.

Designed for Australian sole traders who are **not GST-registered** (annual turnover below $75,000). Compliant with ATO invoice requirements for non-registered entities.

**Platform:** macOS only. PDF generation requires Google Chrome and poppler (`pdfunite`), both of which are wired to macOS paths. Linux could work with minor edits; Windows is not supported.

---

## What it does

- Generate professional invoice and reimbursement PDFs from a simple form
- Track all transactions in a local CSV ledger — status, payment date, notes
- Dashboard view with YTD totals, overdue flags, and one-click filters
- Multiple profiles (separate trading identities, ABNs, or bank accounts)
- Bank details stored in your system keychain — never in any file on disk

---

## First-time setup

**Requirements:** macOS, Python 3.11+, [Google Chrome](https://www.google.com/chrome/), [poppler](https://poppler.freedesktop.org/) (`brew install poppler`).

**Recommended:** install [uv](https://docs.astral.sh/uv/) for automatic dependency management:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

```bash
# 1. Clone
git clone https://github.com/YOUR_USERNAME/abn-invoice.git
cd abn-invoice

# 2. Run the setup script — handles everything interactively
bash setup.sh
```

`setup.sh` will:

- Install Python dependencies into a local `.venv/` (via uv, or fall back to system pip)
- Prompt for your name, ABN, and a profile ID
- Create `profiles.json` and your data directory (`data/<profile_id>/`)

**3. Set bank details** — after setup, open the app and go to Settings:

```bash
./invoice serve
# Open http://localhost:5001 → Settings → Bank details
```

BSB and account number are stored in your system keychain — never in any file. If your browser offers to save a password when you enter these, dismiss it; the keychain save has already happened.

> `profiles.json` and `data/` are gitignored. Your name, ABN, client names, amounts, and bank details never appear in git history.

---

## Usage

### Browser UI (recommended)

```bash
./invoice serve
```

Opens `http://localhost:5001` automatically. From there you can add and edit transactions, generate PDFs, view them inline, track status, and manage profiles and bank details in Settings.

### CLI

```bash
./invoice add                          # interactive prompt for a new transaction
./invoice generate INV-2026-001        # generate one PDF
./invoice generate --all               # generate all
./invoice generate --new               # only rows without a PDF or with status=draft
./invoice generate --year 2026         # FY ending 30 June 2026
./invoice generate --year 2026 --month 5   # May 2026 only
./invoice generate --type reimbursement
./invoice generate --status draft
./invoice status                       # audit: missing PDFs, orphans, stale drafts
./invoice status --stale-days 30       # also flag old drafts with no PDF
```

### Render flags

| Flag | Effect |
| ------ | -------- |
| `--force` | Overwrite existing PDFs |
| `--if-missing` | Skip if PDF already exists, even if CSV row changed |
| `--dry-run` | Write HTML only, no PDF |
| `--force-validation` | Bypass pre-flight checks |

---

## Workflow

1. **Add** — click "+ Add" (or `./invoice add`) to record a new invoice or reimbursement claim. The invoice number is auto-assigned.
2. **Generate** — click Generate to produce a PDF from that row. Re-gen overwrites it.
3. **Send** — click View to open the PDF in a new tab. Download or print it to send to your client.
4. **Track status** — update the status on each row manually as things progress:
   - `draft` — created, not yet sent
   - `sent` — sent to client, awaiting payment
   - `paid` — payment received; record the date and reference in Edit
5. **Notes** — use the Notes field (in Edit) to record anything relevant: partial payment details, client contact, disputes, or follow-up dates.

---

## Profiles

A profile is a named trading identity with its own transaction ledger, PDF folder, and bank details. Most people only need one. To add a second profile (e.g. a separate ABN, company, or bank account), go to Settings in the webapp. Each profile stores its data in `data/<profile_id>/`.

---

## Folder structure

```text
abn-invoice/
├── profiles.json           YOUR profiles: name, ABN  [gitignored]
├── profiles.example.json   template — reference for the profiles.json format
├── data/                   YOUR transaction data  [gitignored]
│   └── <profile_id>/
│       ├── transactions.csv
│       ├── Invoices/       generated PDFs and HTMLs
│       │   └── 25-26/      ATO financial year ending 30 June 2026
│       └── logs/
│           └── render.log  JSON-lines audit trail
├── generate_invoice.py     core CLI tool
├── serve.py                local Flask webapp
├── invoice                 shell wrapper (use this, not python directly)
├── setup.sh                first-time setup script
├── pyproject.toml          Python dependencies (managed by uv)
├── templates/
│   ├── invoice.html.j2     Jinja2 template for both document types
│   └── ui/                 webapp HTML templates
└── transactions.sample.csv example with placeholder data
```

Generated PDFs land in `data/<profile_id>/Invoices/<FY>/` where `<FY>` is the ATO financial year the invoice date falls in (1 July to 30 June). Folder labels use two-digit form: `25-26` means the year ending 30 June 2026.

---

## transactions.csv columns

| Column | Notes |
| -------- | ------- |
| `invoice_number` | Auto-assigned by `./invoice add` or the web UI |
| `date` | YYYY-MM-DD — invoice issue date |
| `type` | `service` or `reimbursement` |
| `client_name` | Full legal name of paying organisation |
| `client_abn` | Required on invoices over $82.50 (ATO rule) |
| `description` | What was provided or purchased |
| `amount` | AUD, no currency symbol |
| `status` | `draft` / `sent` / `paid` |
| `service_date` | Optional — date work or event occurred |
| `due_date` | Optional — triggers overdue flag in dashboard when past |
| `receipt_file` | Filename only, e.g. `REIMB-2026-001_receipt.pdf` |
| `notes` | Free text — partial payments, disputes, follow-up dates |
| `payment_date` | YYYY-MM-DD — fill when marking status as paid |
| `payment_ref` | Bank transfer reference, cheque number, or other identifier |

Invoice numbers use the issue year: `INV-YYYY-NNN` (service) or `REIMB-YYYY-NNN` (reimbursement). The PDF lands in the ATO FY folder, which may differ from the issue year — this is intentional.

---

## Bank details

Bank details are stored in your system keychain via the [keyring](https://pypi.org/project/keyring/) library. On macOS this is Keychain Access; on other platforms it uses the native credential store. They are never written to any file. Set them in the webapp under Settings → Bank details.

Keychain service name: `abn-invoice`. Keys follow the pattern `<profile_id>:bsb`, `<profile_id>:account_number`, `<profile_id>:account_name`.

---

## Australian tax and record-keeping (ATO)

- This tool generates **invoices** (not "tax invoices" — that term is reserved for GST-registered entities under the GST Act).
- The ATO requires invoices to include: issuer name and ABN, date issued, description of goods/services, and total amount. All are present in the generated documents.
- The `client_abn` field is required and enforced when `amount > $82.50`. If a client ABN is absent on an invoice above that threshold, the payer is legally required to withhold 47% (ATO PAYG withholding).
- **Record keeping:** retain all invoices, receipts, and the transactions ledger for **five years** after the due date of the relevant tax return (s262A ITAA 1936).
- Not GST-registered (turnover under $75,000). No GST is charged.

---

## Audit log

Every render attempt is appended to `data/<profile_id>/logs/render.log` as JSON-lines:

```json
{"ts":"2026-06-22T18:31:02+10:00","run_id":"20260622-183102","invoice":"INV-2026-001","status":"ok","pdf":"data/yobin/Invoices/25-26/INV-2026-001.pdf","bytes":71234,"elapsed_ms":1200,"selector":"--new"}
```

`run_id` groups a batch run. Use `grep '"run_id":"20260622-183102"' render.log` to pull every row from one batch.

---

## How this was built

This project was built with **[Claude Code](https://claude.com/claude-code)** — the architecture, webapp, CLI tooling, and template were designed and written collaboratively with Claude (model **Sonnet 4.6**, via Claude Code) in June 2026, working from my own requirements and design brief.

Every release is tested by hand before use. Core correctness (PDF output, keychain handling, validation logic, CSV schema) is verified against real invoice data.

I use this personally for sole-trader invoicing as a graduate researcher in Australia. The tool is published as a template so others in a similar situation can adapt it without starting from scratch.

---

## Licence

MIT — see [LICENSE](LICENSE).
