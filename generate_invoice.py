#!/usr/bin/env python3
"""
ABN invoice and reimbursement-claim tool.

Dependencies: flask, jinja2, keyring — install with: uv sync

Commands
--------
generate  [NUM ...] [--all] [--year YYYY] [--type T] [--status S]
          [--if-missing] [--force] [--dry-run] [--force-validation]
          Render to HTML, then auto-produce a PDF.
          With --all or any filter, renders every matching row in one run.
status   [--stale-days N]           CSV-vs-filesystem audit
add                                Interactively add a new transaction

Examples
--------
    ./invoice generate INV-2026-001
    ./invoice generate --year 2026 --dry-run
    ./invoice generate --all
    ./invoice status --stale-days 30
    ./invoice add

Identity and bank details are stored in profiles.json (non-secret fields)
and the system keychain (BSB, account number). Run ./setup.sh on first use.

Audit log: data/<profile>/logs/render.log (JSON-lines, append-only).
"""

import sys
import csv
import json
import time
import argparse
import subprocess
import tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path

try:
    from jinja2 import Environment, FileSystemLoader
    import keyring
except ImportError as e:
    sys.exit(f"Missing dependency ({e}) — run: uv sync")

# ── Config ────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent
TEMPLATES_DIR = BASE_DIR / "templates"
PROFILES_FILE = BASE_DIR / "profiles.json"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# Profile-dependent module globals — initialised by _reload_profile() below.
# Call _reload_profile() again after switching the active profile (serve.py does this).
MY_NAME: str = ""
MY_ABN: str = ""
TRANSACTIONS: Path = BASE_DIR / "transactions.csv"  # placeholder; overridden at startup
INVOICES_DIR: Path = BASE_DIR / "Invoices"  # placeholder; overridden at startup
LOG_DIR: Path = BASE_DIR / "logs"  # placeholder; overridden at startup
LOG_FILE: Path = BASE_DIR / "logs" / "render.log"  # placeholder; overridden at startup


def load_profiles() -> dict:
    """Load profiles.json. Exits with a clear message if the file is missing."""
    if not PROFILES_FILE.exists():
        sys.exit(
            "Error: profiles.json not found.\n"
            "Run ./setup.sh to create your first profile, or open the webapp."
        )
    with open(PROFILES_FILE) as f:
        return json.load(f)


def get_active_profile() -> tuple[str, dict]:
    """Return (profile_id, profile_dict) for the currently active profile."""
    data = load_profiles()
    pid = data.get("active", "")
    profiles = data.get("profiles", {})
    if pid not in profiles:
        sys.exit(
            f"Error: active profile '{pid}' not found in profiles.json.\n"
            f"Available: {', '.join(profiles) or '(none)'}"
        )
    return pid, profiles[pid]


def _reload_profile() -> None:
    """Update module globals from the current active profile.
    Called at import time and by serve.py after the user switches profiles."""
    global MY_NAME, MY_ABN, TRANSACTIONS, INVOICES_DIR, LOG_DIR, LOG_FILE
    pid, profile = get_active_profile()
    MY_NAME = profile.get("name", "")
    MY_ABN = profile.get("abn", "")
    data_dir = BASE_DIR / "data" / pid
    data_dir.mkdir(parents=True, exist_ok=True)
    TRANSACTIONS = data_dir / "transactions.csv"
    INVOICES_DIR = data_dir / "Invoices"
    LOG_DIR = data_dir / "logs"
    LOG_FILE = data_dir / "logs" / "render.log"


# Initialise from profiles.json at import time.
_reload_profile()

# CSV column order — kept explicit so appended rows serialise correctly
CSV_FIELDS = [
    "invoice_number",
    "date",
    "type",
    "client_name",
    "client_abn",
    "description",
    "service_date",
    "amount",
    "status",
    "due_date",
    "receipt_file",
    "notes",
]


# ── Helpers ───────────────────────────────────────────────────────────────────


def get_bank_details() -> dict:
    """Pull bank details from the system keychain for the active profile.
    Values are stored under service 'abn-invoice', never hardcoded in any file."""
    pid, _ = get_active_profile()
    fields = {
        "bsb": f"{pid}:bsb",
        "account": f"{pid}:account_number",
        "account_name": f"{pid}:account_name",
    }
    result = {}
    for field, key in fields.items():
        value = keyring.get_password("abn-invoice", key)
        if not value:
            sys.exit(
                f"Error: bank detail '{key}' missing from keychain.\n"
                f"Open the webapp → Settings → Bank details to set it."
            )
        result[field] = value
    return result


def iter_transactions():
    """Yield rows from transactions.csv with all keys and values stripped.
    Spreadsheet apps (Numbers, Excel) often pad column names with spaces."""
    with open(TRANSACTIONS, newline="") as f:
        reader = csv.DictReader(f)
        reader.fieldnames = [h.strip() for h in reader.fieldnames]
        for row in reader:
            yield {k.strip(): v.strip() for k, v in row.items()}


def load_transaction(invoice_number: str) -> dict:
    for row in iter_transactions():
        if row["invoice_number"] == invoice_number:
            return row
    sys.exit(f"Error: '{invoice_number}' not found in transactions.csv")


def fmt_date(date_str: str) -> str:
    """YYYY-MM-DD → '14 June 2026' for display in the invoice."""
    return datetime.strptime(date_str, "%Y-%m-%d").strftime("%-d %B %Y")


def parse_date_or_none(date_str: str):
    """Return a datetime for YYYY-MM-DD, or None on empty / ValueError.
    Used by validation and by generate() so a malformed CSV row fails cleanly
    rather than raising a traceback."""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return None


# ── Financial year (ATO: 1 July – 30 June) ────────────────────────────────────


def fy_year_ending(dt: datetime) -> int:
    """Return the calendar year in which the financial year ends.
    ATO FY runs 1 July to 30 June. 2026-03-15 → FY ending 2026. 2026-07-15 → FY ending 2027."""
    return dt.year + 1 if dt.month >= 7 else dt.year


def fy_label(dt: datetime) -> str:
    """Return the two-digit FY folder label, e.g. '25-26' for FY ending 2026."""
    ye = fy_year_ending(dt)
    start = ye - 1
    return f"{start % 100:02d}-{ye % 100:02d}"


def fy_date_range(year_ending: int) -> tuple[datetime, datetime]:
    """Return (start, end) datetimes for the FY ending in year_ending.
    FY ending 2026 → 2025-07-01 through 2026-06-30."""
    return (
        datetime(year_ending - 1, 7, 1),
        datetime(year_ending, 6, 30, 23, 59, 59),
    )


def fy_folder_for_date(dt: datetime) -> Path:
    """Return Invoices/<FY>/ for an issue date."""
    return INVOICES_DIR / fy_label(dt)


# ATO: invoices over $82.50 (incl. GST) must carry the supplier's ABN.
# We are not GST-registered, so the threshold is the pre-GST amount of $82.50.
ABN_THRESHOLD = 82.50


def validate_row(tx: dict) -> list[str]:
    """Return a list of human-readable validation errors for tx.
    Empty list means the row is OK to render."""
    errors = []
    if not tx.get("invoice_number"):
        errors.append("invoice_number is empty")
    if tx.get("type") not in ("service", "reimbursement"):
        errors.append(
            f"type must be 'service' or 'reimbursement' (got {tx.get('type')!r})"
        )
    if tx.get("status") not in ("draft", "sent", "paid"):
        errors.append(f"status must be draft/sent/paid (got {tx.get('status')!r})")
    if parse_date_or_none(tx.get("date", "")) is None:
        errors.append("date missing or not YYYY-MM-DD")
    for col in ("service_date", "due_date"):
        v = tx.get(col, "")
        if v and parse_date_or_none(v) is None:
            errors.append(f"{col} not YYYY-MM-DD (got {v!r})")
    try:
        amount = float(tx.get("amount") or 0)
    except ValueError:
        errors.append(f"amount is not numeric (got {tx.get('amount')!r})")
        amount = 0.0
    if amount > ABN_THRESHOLD and not tx.get("client_abn"):
        errors.append(f"client_abn required for amount > ${ABN_THRESHOLD:.2f}")
    receipt = tx.get("receipt_file", "")
    if receipt:
        # Receipts must live alongside the invoice in Invoices/<FY>/. Reject any
        # path that tries to escape that folder (typos like 'Desktop/x.pdf' or
        # '../foo.pdf' would otherwise produce a misleading 'not found' error
        # because the absolute path would resolve outside INVOICES_DIR).
        if "/" in receipt or receipt.startswith(".."):
            errors.append(
                "receipt_file must be a filename only (no slashes, no '..'); "
                f"got {receipt!r}"
            )
            return errors
        d = parse_date_or_none(tx.get("date", ""))
        if d is None:
            pass  # already reported
        else:
            receipt_path = fy_folder_for_date(d) / receipt
            if not receipt_path.exists():
                errors.append(
                    f"receipt_file not found: {receipt_path.relative_to(BASE_DIR)}"
                )
    return errors


# ── Audit log ────────────────────────────────────────────────────────────────


class RunLog:
    """JSON-lines append-only log of every row attempt in a single process.

    A run_id ties all rows from one CLI invocation together so a batch is
    grep-able. Stdout is for humans; this file is the audit trail."""

    def __init__(self, selector: str) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        # Local time matches the user's wall clock; the +offset makes it explicit.
        now = datetime.now().astimezone()
        self.run_id = now.strftime("%Y%m%d-%H%M%S")
        self.selector = selector
        self.started = now.isoformat(timespec="seconds")

    def write(
        self,
        invoice: str,
        status: str,
        pdf: Path | None,
        error: str | None,
        elapsed_ms: int,
        bytes_written: int,
    ) -> None:
        record = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "run_id": self.run_id,
            "invoice": invoice,
            "status": status,
            "pdf": str(pdf.relative_to(BASE_DIR)) if pdf else None,
            "bytes": bytes_written if pdf else 0,
            "elapsed_ms": elapsed_ms,
            "selector": self.selector,
        }
        if error:
            record["error"] = error
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def next_invoice_number(tx_type: str, year: int) -> str:
    """
    Return the next sequential invoice number for the given type and year.
    Service invoices:       INV-YYYY-NNN
    Reimbursement claims:   REIMB-YYYY-NNN
    """
    prefix = "INV" if tx_type == "service" else "REIMB"
    pattern = f"{prefix}-{year}-"
    max_seq = 0
    if TRANSACTIONS.exists():
        for row in iter_transactions():
            num = row.get("invoice_number", "")
            if num.startswith(pattern):
                try:
                    seq = int(num[len(pattern) :])
                    max_seq = max(max_seq, seq)
                except ValueError:
                    pass
    return f"{pattern}{max_seq + 1:03d}"


# ── PDF generation ────────────────────────────────────────────────────────────


def html_to_pdf(html_path: Path, pdf_path: Path) -> None:
    """Render an HTML file to PDF using Chrome headless.
    Chrome prints exactly what the browser renders, including the invoice CSS."""
    result = subprocess.run(
        [
            CHROME,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--no-pdf-header-footer",  # suppress Chrome's URL/page-number overlay
            f"--print-to-pdf={pdf_path.absolute()}",
            f"file://{html_path.absolute()}",
        ],
        capture_output=True,
        text=True,
    )
    if not pdf_path.exists():
        sys.exit(f"PDF generation failed:\n{result.stderr}")


def merge_pdfs(parts: list[Path], out: Path) -> None:
    """Merge multiple PDFs into one using pdfunite (poppler, installed via Homebrew)."""
    subprocess.run(["pdfunite", *[str(p) for p in parts], str(out)], check=True)


# ── Generator ─────────────────────────────────────────────────────────────────


# Result tuple for one render attempt: (status, message, pdf_path_or_None)
# status is 'ok' | 'skip' | 'fail'. message is the short string for stdout/log.


def generate(
    invoice_number: str,
    *,
    force: bool = False,
    if_missing: bool = False,
    dry_run: bool = False,
) -> tuple[str, str, Path | None]:
    """Render a single transaction.

    Returns (status, message, pdf_path) where status ∈ {ok, skip, fail}.
    Raises nothing — failures are returned, not raised, so batch drivers can
    keep going. Force/if_missing/dry_run come from CLI flags; defaults match
    the safe behaviour (skip if exists, render HTML+PDF)."""
    tx = load_transaction(invoice_number)
    bank = get_bank_details()

    tx_date_raw = tx["date"]
    tx_date = parse_date_or_none(tx_date_raw)
    if tx_date is None:
        return ("fail", f"date column missing or invalid: {tx_date_raw!r}", None)

    receipt_file = tx.get("receipt_file", "")
    # Belt-and-braces: refuse paths that escape the FY folder even if
    # --force-validation skipped the validator. Cheap to recheck.
    if receipt_file and ("/" in receipt_file or receipt_file.startswith("..")):
        return (
            "fail",
            f"receipt_file must be a filename only (got {receipt_file!r})",
            None,
        )
    receipt_path = fy_folder_for_date(tx_date) / receipt_file if receipt_file else None
    if receipt_file and not receipt_path.exists():
        # Treat as a warning rather than a hard error so a missing receipt still
        # produces a usable PDF — the user has already been told via validation
        # at the CLI level, and this branch only fires if they bypassed it.
        receipt_path = None

    due_date = fmt_date(tx["due_date"]) if tx.get("due_date") else None
    service_date = fmt_date(tx["service_date"]) if tx.get("service_date") else None

    env = Environment(loader=FileSystemLoader(TEMPLATES_DIR), autoescape=False)
    template = env.get_template("invoice.html.j2")

    html = template.render(
        my_name=MY_NAME,
        my_abn=MY_ABN,
        bank=bank,
        tx=tx,
        tx_date=fmt_date(tx_date_raw),
        service_date=service_date,
        due_date=due_date,
        receipt_file=receipt_file,
    )

    out_dir = fy_folder_for_date(tx_date)
    out_dir.mkdir(parents=True, exist_ok=True)
    html_file = out_dir / f"{invoice_number}.html"
    pdf_file = out_dir / f"{invoice_number}.pdf"

    # Idempotency: never silently overwrite a PDF the user has already produced.
    if pdf_file.exists() and not force:
        if if_missing:
            return ("skip", "already exists (--if-missing)", pdf_file)
        return ("skip", "already exists (use --force to overwrite)", pdf_file)

    html_file.write_text(html, encoding="utf-8")

    if dry_run:
        return (
            "ok",
            f"dry-run: HTML only → {html_file.relative_to(BASE_DIR)}",
            html_file,
        )

    if receipt_path:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            claim_pdf = Path(tmp.name)
        html_to_pdf(html_file, claim_pdf)
        merge_pdfs([claim_pdf, receipt_path], pdf_file)
        claim_pdf.unlink()
    else:
        html_to_pdf(html_file, pdf_file)

    return ("ok", str(pdf_file.relative_to(BASE_DIR)), pdf_file)


# ── Interactive add ───────────────────────────────────────────────────────────


def prompt(label: str, default: str = "") -> str:
    """Prompt with an optional default; return stripped input or the default.
    Returns the sentinel '<EOF>' on EOFError so callers can abort cleanly."""
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"{label}{suffix}: ").strip()
    except EOFError:
        print()  # newline so the abort message lands on its own line
        return "<EOF>"
    return value if value else default


def prompt_choice(label: str, choices: tuple[str, ...], default: str = "") -> str:
    """Prompt until the user picks one of `choices`. Re-prompts on invalid input.
    Returns '<EOF>' on EOFError so the caller can abort the whole flow."""
    while True:
        v = prompt(label, default).lower()
        if v == "<eof>":
            return "<EOF>"
        if v in choices:
            return v
        print(f"  Must be one of: {', '.join(choices)}")


def prompt_date(label: str, default: str = "") -> str:
    """Prompt for a YYYY-MM-DD date. Re-prompts on invalid input. Empty stays empty
    (caller decides whether to accept). Returns '<EOF>' on EOFError."""
    while True:
        v = prompt(label, default)
        if v == "<EOF>":
            return "<EOF>"
        if not v:
            return ""
        if parse_date_or_none(v) is not None:
            return v
        print("  Must be YYYY-MM-DD (e.g. 2026-06-22).")


def prompt_amount(label: str) -> str:
    """Prompt for a non-negative numeric amount. Re-prompts on invalid input.
    Returns '<EOF>' on EOFError."""
    while True:
        v = prompt(label)
        if v == "<EOF>":
            return "<EOF>"
        try:
            float(v)
            return v
        except ValueError:
            print("  Must be a number (no $ symbol).")


def add_transaction():
    """Interactively build a new transaction row and append it to transactions.csv.
    Validates every field as the user types; only writes to the CSV when the row
    is clean and the user confirms. Aborts cleanly on EOF (e.g. piped stdin)."""
    print("\nNew transaction\n" + "─" * 40)

    today = datetime.today().strftime("%Y-%m-%d")

    tx_type = prompt_choice(
        "Type [service / reimbursement]", ("service", "reimbursement")
    )
    if tx_type == "<EOF>":
        print("Aborted (no input).")
        return

    date = prompt_date("Date (YYYY-MM-DD) — invoice issue date", today)
    if date == "<EOF>":
        print("Aborted (no input).")
        return
    service_date = prompt_date("Service/event date (YYYY-MM-DD, optional)")
    if service_date == "<EOF>":
        print("Aborted (no input).")
        return

    client_name = prompt("Client name")
    if client_name == "<EOF>":
        print("Aborted (no input).")
        return
    client_abn = prompt("Client ABN (optional)")
    if client_abn == "<EOF>":
        print("Aborted (no input).")
        return
    description = prompt("Description")
    if description == "<EOF>":
        print("Aborted (no input).")
        return
    amount = prompt_amount("Amount (AUD, no $)")
    if amount == "<EOF>":
        print("Aborted (no input).")
        return
    status = prompt_choice(
        "Status [draft / sent / paid]", ("draft", "sent", "paid"), "draft"
    )
    if status == "<EOF>":
        print("Aborted (no input).")
        return
    due_date = prompt_date("Due date (YYYY-MM-DD, optional — not legally required)")
    if due_date == "<EOF>":
        print("Aborted (no input).")
        return
    receipt_file = ""
    if tx_type == "reimbursement":
        receipt_file = prompt(
            "Receipt filename (e.g. REIMB-2026-002_receipt.pdf, optional)"
        )
        if receipt_file == "<EOF>":
            print("Aborted (no input).")
            return
    notes = prompt("Notes (optional)")
    if notes == "<EOF>":
        print("Aborted (no input).")
        return

    year = parse_date_or_none(date).year
    invoice_number = next_invoice_number(tx_type, year)
    print(f"\nNext invoice number: {invoice_number}")

    # Build a draft row and run the same validator `generate` would, so the user
    # sees ABN-missing-on-big-invoice errors before the row lands in the CSV.
    row = {
        "invoice_number": invoice_number,
        "date": date,
        "type": tx_type,
        "client_name": client_name,
        "client_abn": client_abn,
        "description": description,
        "service_date": service_date,
        "amount": amount,
        "status": status,
        "due_date": due_date,
        "receipt_file": receipt_file,
        "notes": notes,
    }
    errors = validate_row(row)
    if errors:
        print("\nValidation issues:")
        for e in errors:
            print(f"  - {e}")
        confirm = prompt("Add anyway? [y/N]", "N").upper()
        if confirm != "Y":
            print("Aborted.")
            return

    confirm = prompt("Add to transactions.csv? [Y/n]", "Y").upper()
    if confirm == "<EOF>" or confirm != "Y":
        print("Aborted.")
        return

    file_exists = TRANSACTIONS.exists()
    with open(TRANSACTIONS, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)

    print(f"Added {invoice_number} to transactions.csv")

    gen = prompt("Generate PDF now? [Y/n]", "Y").upper()
    if gen == "Y":
        status, message, _ = generate(invoice_number)
        print(f"[{status}] {message}")


# ── Row selection & batch driver ─────────────────────────────────────────────


def select_rows(args) -> list[dict]:
    """Apply the CLI's filter flags to transactions.csv.
    Returns the list of matching rows. Raises SystemExit if no rows match
    or if selectors are empty (argparse already catches most of this).

    --year is the financial-year-ending year (ATO: 1 Jul to 30 Jun).
    --month is a calendar month within the FY (1–12); requires --year.
    --new = render rows whose PDF is missing OR whose status is 'draft'."""
    rows = list(iter_transactions())
    selected = rows

    if args.year is not None:
        start, end = fy_date_range(args.year)
        selected = [
            r
            for r in selected
            if (d := parse_date_or_none(r.get("date", ""))) and start <= d <= end
        ]
    if args.month is not None:
        if args.year is None:
            print(
                "--month requires --year (so the FY is unambiguous).",
                file=sys.stderr,
            )
            sys.exit(2)
        selected = [
            r
            for r in selected
            if (d := parse_date_or_none(r.get("date", ""))) and d.month == args.month
        ]
    if args.type is not None:
        selected = [r for r in selected if r.get("type") == args.type]
    if args.status is not None:
        selected = [r for r in selected if r.get("status") == args.status]

    # --new: render only rows whose PDF is missing OR whose status is 'draft'.
    if getattr(args, "new", False):
        selected = [
            r for r in selected if r.get("status") == "draft" or not pdf_exists_for(r)
        ]

    # Positional invoice numbers are explicit overrides — keep only those rows
    # from the filtered set (or all rows if no filter was applied).
    if args.invoice_numbers:
        wanted = set(args.invoice_numbers)
        selected = [r for r in rows if r.get("invoice_number") in wanted]

    # Preserve CSV order so stdout and log lines match what the user sees.
    seen = set()
    ordered = []
    for r in selected:
        n = r.get("invoice_number", "")
        if n and n not in seen:
            seen.add(n)
            ordered.append(r)
    return ordered


def pdf_exists_for(row: dict) -> bool:
    """True if the rendered PDF already exists on disk for this row."""
    num = row.get("invoice_number", "")
    if not num:
        return False
    d = parse_date_or_none(row.get("date", ""))
    if d is None:
        return False
    return (fy_folder_for_date(d) / f"{num}.pdf").exists()


def format_selector(args) -> str:
    """A short string describing what the user asked for, written into the log."""
    parts = []
    if args.invoice_numbers:
        parts.append(" ".join(args.invoice_numbers))
    if args.all:
        parts.append("--all")
    if args.year is not None:
        parts.append(f"--year {args.year}")
    if args.month is not None:
        parts.append(f"--month {args.month}")
    if args.type is not None:
        parts.append(f"--type {args.type}")
    if args.status is not None:
        parts.append(f"--status {args.status}")
    if args.dry_run:
        parts.append("--dry-run")
    if args.force:
        parts.append("--force")
    if args.if_missing:
        parts.append("--if-missing")
    if getattr(args, "new", False):
        parts.append("--new")
    return " ".join(parts) or "(implicit all)"


def print_summary(ok: int, skip: int, fail: int, elapsed: float) -> None:
    """Final summary line after a batch run."""
    total = ok + skip + fail
    bar = "─" * 29
    print(bar)
    print(
        f"Rendered: {ok}   Skipped: {skip}   Failed: {fail}   "
        f"Total: {total}   Elapsed: {elapsed:.1f}s"
    )


def run_generate_batch(args) -> int:
    """Drive `generate` for every selected row. Returns process exit code."""
    rows = select_rows(args)
    if not rows:
        print("No transactions match the given selectors.", file=sys.stderr)
        return 2

    if not args.force_validation:
        all_errors = [
            (r.get("invoice_number", "<unnamed>"), e)
            for r in rows
            for e in validate_row(r)
        ]
        if all_errors:
            print("Pre-flight validation failed:\n", file=sys.stderr)
            # Group errors by row for readability
            grouped = defaultdict(list)
            for num, err in all_errors:
                grouped[num].append(err)
            for num, errs in grouped.items():
                for err in errs:
                    print(f"  {num}: {err}", file=sys.stderr)
            print(
                f"\n{len(grouped)} row(s) have issues. "
                "Fix them, or re-run with --force-validation to bypass.",
                file=sys.stderr,
            )
            return 2

    log = RunLog(format_selector(args))
    counts = {"ok": 0, "skip": 0, "fail": 0}
    started = time.monotonic()
    for r in rows:
        num = r.get("invoice_number", "")
        if not num:
            log.write("<unnamed>", "fail", None, "invoice_number missing", 0, 0)
            counts["fail"] += 1
            print("[fail] <unnamed>        → invoice_number missing")
            continue
        t0 = time.monotonic()
        try:
            status, message, pdf = generate(
                num,
                force=args.force,
                if_missing=args.if_missing,
                dry_run=args.dry_run,
            )
        except Exception as e:  # noqa: BLE001 — surface anything as a fail row
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            log.write(num, "fail", None, str(e), elapsed_ms, 0)
            counts["fail"] += 1
            print(f"[fail] {num:<18} → {e}")
            continue

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        bytes_written = 0
        if pdf and pdf.exists() and not args.dry_run:
            bytes_written = pdf.stat().st_size
        log.write(num, status, pdf, None, elapsed_ms, bytes_written)
        counts[status] += 1
        print(f"[{status}] {num:<18} → {message}  ({elapsed_ms / 1000:.1f}s)")

    elapsed = time.monotonic() - started
    print_summary(counts["ok"], counts["skip"], counts["fail"], elapsed)
    return 0 if counts["fail"] == 0 else 2


def run_generate_single(args) -> int:
    """Single-row generate path. Mirrors the batch validation contract."""
    rows = select_rows(args)  # applies any filters; positional = the row
    if len(rows) != 1:
        print(
            f"Expected exactly one matching row, got {len(rows)}. "
            "Use positional numbers or filters that match one row.",
            file=sys.stderr,
        )
        return 2

    num = rows[0].get("invoice_number", "")
    if not args.force_validation:
        errors = validate_row(rows[0])
        if errors:
            print("Pre-flight validation failed:", file=sys.stderr)
            for e in errors:
                print(f"  {num}: {e}", file=sys.stderr)
            print(
                "Fix the row, or re-run with --force-validation to bypass.",
                file=sys.stderr,
            )
            return 2

    log = RunLog(format_selector(args))
    t0 = time.monotonic()
    try:
        status, message, pdf = generate(
            num,
            force=args.force,
            if_missing=args.if_missing,
            dry_run=args.dry_run,
        )
    except Exception as e:  # noqa: BLE001
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        log.write(num, "fail", None, str(e), elapsed_ms, 0)
        print(f"[fail] {num}: {e}", file=sys.stderr)
        return 2

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    bytes_written = (
        pdf.stat().st_size if pdf and pdf.exists() and not args.dry_run else 0
    )
    log.write(num, status, pdf, None, elapsed_ms, bytes_written)
    print(f"[{status}] {num:<18} → {message}  ({elapsed_ms / 1000:.1f}s)")
    return 2 if status == "fail" else 0


# ── Status audit ─────────────────────────────────────────────────────────────


def status_audit(stale_days: int | None) -> int:
    """Diff transactions.csv against Invoices/<FY>/ folders.

    Reports rows with no PDF, stale PDFs (older than transactions.csv),
    orphan PDFs (no CSV row), and (with --stale-days) drafts older than N days.
    Returns 0 — this is read-only."""
    rows = list(iter_transactions())
    csv_mtime = TRANSACTIONS.stat().st_mtime

    # Build {invoice_number → row} for lookups
    by_num = {r.get("invoice_number", ""): r for r in rows if r.get("invoice_number")}

    # PDFs that exist on disk under any FY folder, indexed by invoice_number.
    # setdefault means if a PDF somehow exists in two folders (it shouldn't),
    # the first one wins — orphan detection still flags the duplicate.
    found_pdfs: dict[str, Path] = {}
    if INVOICES_DIR.exists():
        for pdf in INVOICES_DIR.glob("*/*.pdf"):
            if pdf.stem.endswith("_receipt"):
                continue  # skip source receipts; only generated PDFs count
            found_pdfs.setdefault(pdf.stem, pdf)

    missing = []
    stale = []
    for num, row in by_num.items():
        pdf = found_pdfs.get(num)
        if pdf is None:
            missing.append(num)
            continue
        if pdf.stat().st_mtime < csv_mtime:
            stale.append(num)

    csv_nums = set(by_num.keys())
    orphan = [n for n in found_pdfs if n not in csv_nums]

    stale_drafts: list[tuple[str, int]] = []
    if stale_days is not None:
        threshold = time.time() - stale_days * 86400
        for num, row in by_num.items():
            if row.get("status") != "draft":
                continue
            pdf = found_pdfs.get(num)
            if pdf is not None:
                continue  # already has a PDF — not stale-draft
            d = parse_date_or_none(row.get("date", ""))
            if d is None:
                continue
            # 'date' is the invoice issue date; for draft rows older than N days
            # since issuance with no PDF, flag as stale-draft.
            age_seconds = time.time() - d.timestamp()
            if age_seconds > stale_days * 86400:
                stale_drafts.append((num, int(age_seconds / 86400)))

    def section(title: str, items) -> None:
        print(f"\n{title}")
        if not items:
            print("  (none)")
            return
        for x in items:
            print(f"  {x}")

    print(f"Audit at {datetime.now().astimezone().isoformat(timespec='seconds')}")
    print(
        f"CSV:  {TRANSACTIONS.relative_to(BASE_DIR)}  "
        f"({len(rows)} row{'s' if len(rows) != 1 else ''})"
    )
    print(f"PDFs: {len(found_pdfs)} on disk under Invoices/")

    section("Missing PDF (in CSV, no file on disk):", missing)
    section("Stale PDF (PDF older than transactions.csv):", stale)
    section("Orphan PDF (file on disk, not in CSV):", orphan)

    if stale_days is not None:
        if stale_drafts:
            print(f"\nStale drafts (status=draft, no PDF, older than {stale_days}d):")
            for num, age in stale_drafts:
                print(f"  {num}  ({age}d old)")
        else:
            print(f"\nStale drafts (status=draft, no PDF, older than {stale_days}d):")
            print("  (none)")

    return 0


# ── CLI ───────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="ABN invoice and reimbursement-claim tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  ./invoice generate INV-2026-001\n"
            "  ./invoice generate --year 2026 --dry-run\n"
            "  ./invoice generate --all --force\n"
            "  ./invoice status --stale-days 30\n"
            "  ./invoice add"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # generate ────────────────────────────────────────────────────────────────
    gen_p = sub.add_parser(
        "generate",
        help="Render transactions to HTML + PDF",
    )
    gen_p.add_argument(
        "invoice_numbers",
        nargs="*",
        help="One or more invoice numbers (e.g. INV-2026-001 REIMB-2026-002)",
    )
    gen_p.add_argument(
        "--all", action="store_true", help="Render every row in transactions.csv"
    )
    gen_p.add_argument(
        "--year",
        type=int,
        default=None,
        help="Filter by financial-year-ending year (ATO FY: 1 Jul to 30 Jun)",
    )
    gen_p.add_argument(
        "--month",
        type=int,
        choices=range(1, 13),
        default=None,
        help="Filter by calendar month (1–12) within --year",
    )
    gen_p.add_argument(
        "--type",
        choices=("service", "reimbursement"),
        default=None,
        help="Filter by transaction type",
    )
    gen_p.add_argument(
        "--status",
        choices=("draft", "sent", "paid"),
        default=None,
        help="Filter by transaction status",
    )
    gen_p.add_argument(
        "--new",
        action="store_true",
        help="Render only rows whose PDF is missing OR whose status is 'draft'",
    )
    gen_p.add_argument(
        "--if-missing",
        action="store_true",
        help="Skip rows whose .pdf already exists (even if CSV is newer)",
    )
    gen_p.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing PDFs without prompting",
    )
    gen_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Render HTML only; skip PDF generation and receipt merge",
    )
    gen_p.add_argument(
        "--force-validation",
        action="store_true",
        help="Bypass pre-flight validation (use with caution)",
    )

    # status ──────────────────────────────────────────────────────────────────
    stat_p = sub.add_parser(
        "status",
        help="Audit transactions.csv vs files on disk",
    )
    stat_p.add_argument(
        "--stale-days",
        type=int,
        default=None,
        help="Also flag draft rows older than N days with no PDF",
    )

    # add ─────────────────────────────────────────────────────────────────────
    sub.add_parser("add", help="Interactively add a new transaction")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "generate":
        # Enforce "at least one selector" and mutual exclusion of --all vs positional
        selectors_given = (
            args.all
            or args.invoice_numbers
            or args.year is not None
            or args.month is not None
            or args.type is not None
            or args.status is not None
            or args.new
        )
        if not selectors_given:
            parser.error(
                "generate needs at least one selector: invoice numbers, --all, "
                "--year, --month, --type, --status, or --new"
            )
        if args.all and args.invoice_numbers:
            parser.error("--all cannot be combined with explicit invoice numbers")

        is_batch = (
            args.all
            or args.year is not None
            or args.month is not None
            or args.type is not None
            or args.status is not None
            or args.new
            or len(args.invoice_numbers) > 1
        )
        if is_batch:
            sys.exit(run_generate_batch(args))
        else:
            sys.exit(run_generate_single(args))
    elif args.command == "status":
        sys.exit(status_audit(args.stale_days))
    elif args.command == "add":
        add_transaction()


if __name__ == "__main__":
    main()
