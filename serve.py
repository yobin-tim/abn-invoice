#!/usr/bin/env python3
"""
Local Flask webapp for ABN invoice management.

Launch: ./invoice serve [--port 5001]
Opens http://localhost:5001 in your default browser automatically.

Imports generate_invoice as a module so all existing logic (generate, validate,
CSV helpers) is reused directly — no subprocess calls needed.
"""

import csv
import secrets
import sys
import webbrowser
import threading
import argparse
from datetime import datetime
from pathlib import Path

from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

# generate_invoice.py lives alongside this file
ABN_DIR = Path(__file__).parent
sys.path.insert(0, str(ABN_DIR))

import generate_invoice as gi  # noqa: E402 — path must be set first

app = Flask(__name__, template_folder=str(ABN_DIR / "templates"))
# Random secret per process — fine for a local single-session tool
app.secret_key = secrets.token_hex(32)


# ── Helpers ───────────────────────────────────────────────────────────────────


def all_rows() -> list[dict]:
    """All rows from transactions.csv, or [] if the file does not exist."""
    if not gi.TRANSACTIONS.exists():
        return []
    return list(gi.iter_transactions())


def find_pdf(invoice_number: str, row: dict) -> Path | None:
    """Return the PDF path if it exists on disk, else None."""
    d = gi.parse_date_or_none(row.get("date", ""))
    if d is None:
        return None
    p = gi.fy_folder_for_date(d) / f"{invoice_number}.pdf"
    return p if p.exists() else None


def update_row(invoice_number: str, updated: dict) -> None:
    """Rewrite transactions.csv replacing the matching row with `updated`."""
    rows = list(gi.iter_transactions())
    with open(gi.TRANSACTIONS, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=gi.CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            if row.get("invoice_number") == invoice_number:
                writer.writerow({k: updated.get(k, "") for k in gi.CSV_FIELDS})
            else:
                writer.writerow({k: row.get(k, "") for k in gi.CSV_FIELDS})


def append_row(data: dict) -> None:
    """Append a new row to transactions.csv."""
    write_header = not gi.TRANSACTIONS.exists() or gi.TRANSACTIONS.stat().st_size == 0
    with open(gi.TRANSACTIONS, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=gi.CSV_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow({k: data.get(k, "") for k in gi.CSV_FIELDS})


def form_to_dict(form) -> dict:
    """Extract a transaction dict from a submitted POST form."""
    return {k: form.get(k, "").strip() for k in gi.CSV_FIELDS}


# ── Routes ────────────────────────────────────────────────────────────────────


@app.route("/")
def index():
    rows = all_rows()
    # Pair each row with its PDF path (or None) for the table
    rows_with_pdf = [(row, find_pdf(row["invoice_number"], row)) for row in rows]
    return render_template("ui/index.html", rows=rows_with_pdf)


@app.route("/generate/<invoice_id>", methods=["POST"])
def generate_one(invoice_id):
    """Generate a single PDF. HTMX requests get a <tr> partial back."""
    force = request.form.get("force") == "1"

    try:
        status, message, _ = gi.generate(
            invoice_id, force=force, if_missing=False, dry_run=False
        )
    except SystemExit as e:
        # generate() calls sys.exit() only if the invoice row is not found
        status, message = "fail", str(e)

    # Re-read the row so the partial reflects any state changes
    row = next(
        (r for r in gi.iter_transactions() if r["invoice_number"] == invoice_id),
        None,
    )
    if row is None:
        abort(404)

    pdf = find_pdf(invoice_id, row)
    result = {"status": status, "message": message}

    # HTMX request: return just the updated <tr> so it can swap in-place
    if request.headers.get("HX-Request"):
        return render_template("ui/_row.html", row=row, pdf=pdf, result=result)

    flash(message, "success" if status in ("ok", "skip") else "error")
    return redirect(url_for("index"))


@app.route("/generate", methods=["POST"])
def generate_bulk():
    """Bulk generate all rows (or only new/draft ones)."""
    force = request.form.get("force") == "1"
    new_only = request.form.get("new_only") == "1"

    rows = all_rows()
    if new_only:
        rows = [
            r
            for r in rows
            if r.get("status") == "draft" or not find_pdf(r["invoice_number"], r)
        ]

    counts = {"ok": 0, "skip": 0, "fail": 0}
    for row in rows:
        try:
            status, _, _ = gi.generate(
                row["invoice_number"], force=force, if_missing=False, dry_run=False
            )
        except SystemExit:
            status = "fail"
        counts[status] = counts.get(status, 0) + 1

    summary = (
        f"Rendered {counts['ok']}, skipped {counts['skip']}, "
        f"failed {counts['fail']} of {len(rows)}"
    )
    flash(summary, "success" if counts["fail"] == 0 else "warning")
    return redirect(url_for("index"))


@app.route("/pdf/<invoice_id>")
def view_pdf(invoice_id):
    """Serve a PDF inline so the browser displays it in-tab."""
    row = next(
        (r for r in gi.iter_transactions() if r["invoice_number"] == invoice_id),
        None,
    )
    if row is None:
        abort(404)
    pdf = find_pdf(invoice_id, row)
    if pdf is None:
        flash(f"No PDF for {invoice_id} — generate it first.", "error")
        return redirect(url_for("index"))
    return send_file(pdf, mimetype="application/pdf")


@app.route("/autoinvoicenum")
def auto_invoice_num():
    """Return the next invoice number for a given type + year (used by the add form)."""
    tx_type = request.args.get("type", "service")
    try:
        year = int(request.args.get("year", datetime.today().year))
    except ValueError:
        year = datetime.today().year
    return jsonify({"number": gi.next_invoice_number(tx_type, year)})


@app.route("/add", methods=["GET", "POST"])
def add():
    if request.method == "POST":
        data = form_to_dict(request.form)
        errors = gi.validate_row(data)
        force_save = request.form.get("force_save") == "1"
        if errors and not force_save:
            return render_template("ui/edit.html", row=data, errors=errors, is_new=True)
        append_row(data)
        flash(f"Added {data['invoice_number']}.", "success")
        return redirect(url_for("index"))

    today = datetime.today()
    row = {k: "" for k in gi.CSV_FIELDS}
    row["date"] = today.strftime("%Y-%m-%d")
    row["status"] = "draft"
    row["type"] = "service"
    return render_template("ui/edit.html", row=row, errors=[], is_new=True)


@app.route("/edit/<invoice_id>", methods=["GET", "POST"])
def edit(invoice_id):
    rows = list(gi.iter_transactions())
    row = next((r for r in rows if r["invoice_number"] == invoice_id), None)
    if row is None:
        abort(404)

    if request.method == "POST":
        updated = form_to_dict(request.form)
        updated["invoice_number"] = invoice_id  # keep original; field is read-only
        errors = gi.validate_row(updated)
        force_save = request.form.get("force_save") == "1"
        if errors and not force_save:
            return render_template(
                "ui/edit.html",
                row=updated,
                errors=errors,
                is_new=False,
                invoice_id=invoice_id,
            )
        update_row(invoice_id, updated)
        flash(f"Saved {invoice_id}.", "success")
        return redirect(url_for("index"))

    return render_template(
        "ui/edit.html", row=row, errors=[], is_new=False, invoice_id=invoice_id
    )


# ── Entry point ───────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="ABN invoice manager (local webapp)")
    parser.add_argument("--port", type=int, default=5001, help="Port to listen on")
    parser.add_argument(
        "--no-open", action="store_true", help="Do not open browser automatically"
    )
    args = parser.parse_args()

    if not args.no_open:
        # Open browser after a short delay so Flask has time to start
        def _open_browser():
            import time

            time.sleep(0.8)
            webbrowser.open(f"http://localhost:{args.port}")

        threading.Thread(target=_open_browser, daemon=True).start()

    print(f"ABN invoice manager → http://localhost:{args.port}  (Ctrl-C to stop)")
    app.run(port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
