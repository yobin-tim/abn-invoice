# abn-invoice

A minimal Python tool for generating invoices and reimbursement claims as PDFs, with a local browser UI and a CSV-backed transaction ledger.

Designed for Australian sole traders who are **not GST-registered** (annual turnover below $75,000). Compliant with ATO invoice requirements for non-registered entities.

---

## First-time setup

**Requirements**: macOS, Python (Anaconda/conda recommended), Google Chrome, [poppler](https://poppler.freedesktop.org/) (`brew install poppler` for `pdfunite`).

```bash
# 1. Clone
git clone https://github.com/YOUR_USERNAME/abn-invoice.git
cd abn-invoice

# 2. Identity — copy example and edit with your name and ABN
cp config.example.py config.py
# Edit config.py: set MY_NAME and MY_ABN

# 3. Transactions ledger — copy sample and edit or start fresh
cp transactions.sample.csv transactions.csv

# 4. Bank details — stored in macOS Keychain, never in code
tokens add ABN_Westpac_BSB
tokens add ABN_Westpac_Account_Number
tokens add ABN_Westpac_Account_Name
# (the tokens utility: https://github.com/YOUR_USERNAME/scripts)

# 5. Make the wrapper executable
chmod +x invoice
```

> `config.py` and `transactions.csv` are gitignored. Your ABN, name, client names, and amounts never appear in git history.

---

## Usage

### Browser UI (recommended)

```bash
./invoice serve
```

Opens `http://localhost:5001` in your browser. From there you can view, add, and edit transactions, generate PDFs, and view them inline.

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

## Folder structure

```
abn-invoice/
├── config.py               YOUR identity: MY_NAME, MY_ABN  [gitignored]
├── config.example.py       template — copy to config.py
├── generate_invoice.py     core CLI tool
├── serve.py                local Flask webapp
├── invoice                 shell wrapper (use this, not python directly)
├── templates/
│   ├── invoice.html.j2     Jinja2 template for both document types
│   └── ui/                 webapp HTML templates
├── transactions.csv        YOUR ledger  [gitignored]
├── transactions.sample.csv example with placeholder data
├── Invoices/               generated PDFs and HTMLs  [gitignored]
│   └── 25-26/              ATO financial year ending 30 June 2026
└── logs/
    └── render.log          JSON-lines audit trail  [gitignored]
```

Generated PDFs land in `Invoices/<FY>/` where `<FY>` is the ATO financial year the invoice date falls in (1 July – 30 June). Folder labels use two-digit form: `25-26` means FY ending 30 June 2026.

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

Bank details are stored in macOS Keychain via the [tokens](https://github.com/YOUR_USERNAME/scripts) utility. They are never hardcoded. To re-register:

```bash
tokens add ABN_Westpac_BSB
tokens add ABN_Westpac_Account_Number
tokens add ABN_Westpac_Account_Name
```

---

## Australian tax and record-keeping (ATO)

- This tool generates **invoices** (not "tax invoices" — that term is reserved for GST-registered entities under the GST Act).
- The ATO requires invoices to include: issuer name and ABN, date issued, description of goods/services, and total amount. All are present in the generated documents.
- The `client_abn` field is required and enforced when `amount > $82.50`. If a client ABN is absent on an invoice above that threshold, the payer is legally required to withhold 47% (ATO PAYG withholding).
- **Record keeping**: retain all invoices, receipts, and the transactions ledger for **five years** after the due date of the relevant tax return (s262A ITAA 1936).
- Not GST-registered (turnover under $75,000). No GST is charged.

---

## Audit log

Every render attempt is appended to `logs/render.log` as JSON-lines:

```json
{"ts":"2026-06-22T18:31:02+10:00","run_id":"20260622-183102","invoice":"INV-2026-001","status":"ok","pdf":"Invoices/25-26/INV-2026-001.pdf","bytes":71234,"elapsed_ms":1200,"selector":"--new"}
```

`run_id` groups a batch run. `grep '"run_id":"20260622-183102"' logs/render.log` gives every row from that batch.

---

## Licence

MIT — see [LICENSE](LICENSE).
