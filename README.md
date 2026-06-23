# abn-invoice

A local tool for generating invoices and reimbursement claims as PDFs, with a browser UI and a CSV-backed transaction ledger.

Designed for Australian sole traders who are **not GST-registered** (annual turnover below $75,000). Compliant with ATO invoice requirements for non-registered entities.

---

## First-time setup

**Requirements**: macOS (recommended), Python 3.11+, Google Chrome, [poppler](https://poppler.freedesktop.org/) (`brew install poppler` for receipt merging).

**Recommended**: install [uv](https://docs.astral.sh/uv/) for automatic dependency management:
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

```bash
# 1. Clone
git clone https://github.com/YOUR_USERNAME/abn-invoice.git
cd abn-invoice

# 2. Run the setup script — it handles everything interactively
bash setup.sh
```

`setup.sh` will:
- Install Python dependencies into a local `.venv/`
- Prompt for your name, ABN, and a profile ID
- Create `profiles.json` and your data directory (`data/<profile_id>/`)

**3. Set bank details** — after setup, start the app and go to Settings:
```bash
./invoice serve
# Open http://localhost:5001 → Settings → Bank details
```
BSB and account number are stored in your system keychain (macOS Keychain / Windows Credential Manager) — never in any file.

> `profiles.json` and `data/` are gitignored. Your name, ABN, client names, amounts, and bank details never appear in git history.

---

## Usage

### Browser UI (recommended for most tasks)

```bash
./invoice serve
```

Opens `http://localhost:5001`. From there you can view, add, and edit transactions, generate PDFs, view them inline, and manage profiles and bank details in Settings.

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
./invoice status                       # audit: missing PDFs, orphans, stale
./invoice status --stale-days 30       # also flag old drafts with no PDF
```

### Render flags

| Flag | Effect |
|------|--------|
| `--force` | Overwrite existing PDFs |
| `--if-missing` | Skip if PDF already exists, even if CSV changed |
| `--dry-run` | Write HTML only, no PDF |
| `--force-validation` | Bypass pre-flight checks |

---

## Profiles

A profile is a named trading identity with its own transaction ledger, invoices folder, and bank details. Most people will only need one.

To add a second profile (e.g. a separate ABN, company, or bank account), go to Settings in the webapp. Each profile stores its data in `data/<profile_id>/`.

---

## Folder structure

```
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

Generated PDFs land in `data/<profile_id>/Invoices/<FY>/` where `<FY>` is the ATO financial year the invoice date falls in (1 July to 30 June). Folder labels use two-digit form: `25-26` means FY ending 30 June 2026.

---

## transactions.csv columns

| Column | Notes |
|--------|-------|
| `invoice_number` | Auto-assigned by `./invoice add` or the web UI |
| `date` | YYYY-MM-DD — invoice issue date |
| `type` | `service` or `reimbursement` |
| `client_name` | Full legal name of paying organisation |
| `client_abn` | Required on invoices over $82.50 (ATO rule) |
| `description` | What was provided or purchased |
| `amount` | AUD, no currency symbol |
| `status` | `draft` / `sent` / `paid` |
| `service_date` | Optional — date work/event occurred |
| `due_date` | Optional |
| `receipt_file` | Filename only, e.g. `REIMB-2026-001_receipt.pdf` |
| `notes` | Free text |

Invoice numbers use the issue year: `INV-YYYY-NNN` (service) or `REIMB-YYYY-NNN` (reimbursement). The file lives in the ATO FY folder, which may differ from the issue year — this is intentional.

---

## Bank details

Bank details are stored in your system keychain via the [keyring](https://pypi.org/project/keyring/) library. They are never written to any file. Set them in the webapp under Settings → Bank details.

Keychain service name: `abn-invoice`. Keys follow the pattern `<profile_id>:bsb`, `<profile_id>:account_number`, `<profile_id>:account_name`.

---

## Australian tax and record-keeping (ATO)

- This tool generates **invoices** (not "tax invoices" — that term is reserved for GST-registered entities under the GST Act).
- The ATO requires invoices to include: issuer name and ABN, date issued, description of goods/services, and total amount. All are present in the generated documents.
- The `client_abn` field is required and enforced when `amount > $82.50`. If a client ABN is absent on an invoice above that threshold, the payer is legally required to withhold 47% (ATO PAYG withholding).
- **Record keeping**: retain all invoices, receipts, and the transactions ledger for **five years** after the due date of the relevant tax return (s262A ITAA 1936).
- Not GST-registered (turnover under $75,000). No GST is charged.

---

## Audit log

Every render attempt is appended to `data/<profile_id>/logs/render.log` as JSON-lines:

```json
{"ts":"2026-06-22T18:31:02+10:00","run_id":"20260622-183102","invoice":"INV-2026-001","status":"ok","pdf":"data/yobin/Invoices/25-26/INV-2026-001.pdf","bytes":71234,"elapsed_ms":1200,"selector":"--new"}
```

`run_id` groups a batch run. `grep '"run_id":"20260622-183102"' render.log` gives every row from that batch.

---

## Licence

MIT — see [LICENSE](LICENSE).
