#!/usr/bin/env python3
"""
Local Flask webapp for ABN invoice management.

Launch: ./invoice serve [--port 5001]
Opens http://localhost:5001 in your default browser automatically.

Imports generate_invoice as a module so all existing logic (generate, validate,
CSV helpers) is reused directly — no subprocess calls needed.
"""

import csv
import re
import secrets
import sys
import webbrowser
import threading
import argparse
from datetime import datetime
from pathlib import Path

try:
    import keyring
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
except ImportError as e:
    sys.exit(f"Missing dependency ({e}) — run: uv sync")

# generate_invoice.py lives alongside this file
ABN_DIR = Path(__file__).parent
sys.path.insert(0, str(ABN_DIR))

import generate_invoice as gi  # noqa: E402 — path must be set first

app = Flask(__name__, template_folder=str(ABN_DIR / "templates"))
# Random secret per process — fine for a local single-session tool
app.secret_key = secrets.token_hex(32)


# ── Profile context ───────────────────────────────────────────────────────────


@app.context_processor
def inject_profile():
    """Inject active profile and full profiles list into every template."""
    try:
        pid, profile = gi.get_active_profile()
        all_profiles = gi.load_profiles().get("profiles", {})
        return {"active_profile": {"id": pid, **profile}, "all_profiles": all_profiles}
    except SystemExit:
        return {"active_profile": None, "all_profiles": {}}


# ── Helpers ───────────────────────────────────────────────────────────────────


def all_rows() -> list[dict]:
    """All rows from the active profile's transactions.csv, or [] if missing."""
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
    """Append a new row to the active profile's transactions.csv."""
    write_header = not gi.TRANSACTIONS.exists() or gi.TRANSACTIONS.stat().st_size == 0
    with open(gi.TRANSACTIONS, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=gi.CSV_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerow({k: data.get(k, "") for k in gi.CSV_FIELDS})


def form_to_dict(form) -> dict:
    """Extract a transaction dict from a submitted POST form."""
    return {k: form.get(k, "").strip() for k in gi.CSV_FIELDS}


def is_overdue(row: dict) -> bool:
    """True if due_date has passed and the invoice is not yet paid."""
    if row.get("status") == "paid":
        return False
    due = row.get("due_date", "").strip()
    if not due:
        return False
    # ISO date strings sort lexicographically, so string comparison works correctly.
    return due < datetime.today().strftime("%Y-%m-%d")


def compute_summary(rows: list[dict]) -> dict:
    """Aggregate totals across the active profile's transactions."""

    def amt(r):
        try:
            return float(r.get("amount") or 0)
        except ValueError:
            return 0.0

    return {
        "count": len(rows),
        "total": sum(amt(r) for r in rows),
        "paid": sum(amt(r) for r in rows if r.get("status") == "paid"),
        "sent": sum(amt(r) for r in rows if r.get("status") == "sent"),
        "draft": sum(amt(r) for r in rows if r.get("status") == "draft"),
    }


def _bank_status(profile_id: str) -> dict:
    """Return masked display info for the bank details of a profile."""
    bsb = keyring.get_password("abn-invoice", f"{profile_id}:bsb") or ""
    acct = keyring.get_password("abn-invoice", f"{profile_id}:account_number") or ""
    acct_name = keyring.get_password("abn-invoice", f"{profile_id}:account_name") or ""
    return {
        "bsb_set": bool(bsb),
        "bsb_hint": f"****{bsb[-3:]}" if len(bsb) >= 3 else ("set" if bsb else ""),
        "account_set": bool(acct),
        "account_hint": f"****{acct[-4:]}"
        if len(acct) >= 4
        else ("set" if acct else ""),
        "account_name": acct_name,
    }


# ── Transaction routes ────────────────────────────────────────────────────────


@app.route("/")
def index():
    rows = all_rows()
    rows_data = [
        (row, find_pdf(row["invoice_number"], row), is_overdue(row)) for row in rows
    ]
    return render_template(
        "ui/index.html", rows=rows_data, summary=compute_summary(rows)
    )


@app.route("/generate/<invoice_id>", methods=["POST"])
def generate_one(invoice_id):
    """Generate a single PDF. HTMX requests get a <tr> partial back."""
    force = request.form.get("force") == "1"

    try:
        status, message, _ = gi.generate(
            invoice_id, force=force, if_missing=False, dry_run=False
        )
    except SystemExit as e:
        status, message = "fail", str(e)

    row = next(
        (r for r in gi.iter_transactions() if r["invoice_number"] == invoice_id),
        None,
    )
    if row is None:
        abort(404)

    pdf = find_pdf(invoice_id, row)
    result = {"status": status, "message": message}

    if request.headers.get("HX-Request"):
        return render_template(
            "ui/_row.html", row=row, pdf=pdf, result=result, overdue=is_overdue(row)
        )

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


@app.route("/export/csv")
def export_csv():
    """Download the active profile's transactions.csv."""
    if not gi.TRANSACTIONS.exists():
        flash("No transactions to export.", "error")
        return redirect(url_for("index"))
    pid = gi.get_active_profile()[0]
    return send_file(
        gi.TRANSACTIONS,
        as_attachment=True,
        download_name=f"transactions-{pid}.csv",
        mimetype="text/csv",
    )


@app.route("/clients")
def client_list():
    """Return distinct client names and ABNs from the active profile (for autocomplete)."""
    seen: dict[str, str] = {}
    for r in all_rows():
        name = r.get("client_name", "").strip()
        if name and name not in seen:
            seen[name] = r.get("client_abn", "").strip()
    clients = [{"name": k, "abn": v} for k, v in sorted(seen.items())]
    return jsonify({"clients": clients})


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
        updated["invoice_number"] = invoice_id
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


# ── Settings routes ───────────────────────────────────────────────────────────


@app.route("/settings")
def settings():
    """Profile management page: edit identity, set bank details, switch profiles."""
    data = gi.load_profiles()
    bank_status = {pid: _bank_status(pid) for pid in data.get("profiles", {})}
    return render_template(
        "ui/settings.html",
        profiles=data.get("profiles", {}),
        active=data.get("active", ""),
        bank_status=bank_status,
    )


@app.route("/settings/profile", methods=["POST"])
def settings_profile():
    """Create a new profile or update an existing one (name + ABN only)."""
    profile_id = request.form.get("profile_id", "").strip().lower()
    name = request.form.get("name", "").strip()
    abn = request.form.get("abn", "").strip()
    is_new = request.form.get("is_new") == "1"

    if not profile_id or not name or not abn:
        flash("Profile ID, name, and ABN are all required.", "error")
        return redirect(url_for("settings"))
    if not re.match(r"^[a-z0-9_-]+$", profile_id):
        flash(
            "Profile ID must be lowercase letters, numbers, hyphens, or underscores.",
            "error",
        )
        return redirect(url_for("settings"))

    data = gi.load_profiles()
    if is_new and profile_id in data.get("profiles", {}):
        flash(f"Profile '{profile_id}' already exists.", "error")
        return redirect(url_for("settings"))

    data.setdefault("profiles", {})[profile_id] = {
        **data["profiles"].get(profile_id, {}),
        "name": name,
        "abn": abn,
    }

    # Create the data directory and empty ledger for new profiles.
    if is_new:
        data_dir = gi.BASE_DIR / "data" / profile_id
        (data_dir / "Invoices").mkdir(parents=True, exist_ok=True)
        csv_path = data_dir / "transactions.csv"
        if not csv_path.exists():
            with open(csv_path, "w", newline="") as f:
                csv.DictWriter(f, fieldnames=gi.CSV_FIELDS).writeheader()

    gi.PROFILES_FILE.write_text(__import__("json").dumps(data, indent=2))
    flash(f"Profile '{profile_id}' {'created' if is_new else 'updated'}.", "success")
    return redirect(url_for("settings"))


@app.route("/settings/bank/<profile_id>", methods=["POST"])
def settings_bank(profile_id):
    """Save bank details for a profile directly to the system keychain.
    Values are never written to any file — keyring handles secure storage."""
    data = gi.load_profiles()
    if profile_id not in data.get("profiles", {}):
        abort(404)

    # Only update fields that were actually submitted (non-empty).
    for field, key in [
        ("bsb", f"{profile_id}:bsb"),
        ("account_number", f"{profile_id}:account_number"),
        ("account_name", f"{profile_id}:account_name"),
    ]:
        value = request.form.get(field, "").strip()
        if value:
            keyring.set_password("abn-invoice", key, value)

    flash("Bank details saved to keychain.", "success")
    return redirect(url_for("settings"))


@app.route("/settings/switch/<profile_id>", methods=["POST"])
def settings_switch(profile_id):
    """Switch the active profile and reload module globals."""
    data = gi.load_profiles()
    if profile_id not in data.get("profiles", {}):
        abort(404)
    data["active"] = profile_id
    gi.PROFILES_FILE.write_text(__import__("json").dumps(data, indent=2))
    gi._reload_profile()
    flash(f"Switched to profile '{data['profiles'][profile_id]['name']}'.", "success")
    return redirect(url_for("index"))


@app.route("/settings/profile/<profile_id>/delete", methods=["POST"])
def settings_delete_profile(profile_id):
    """Remove a profile from profiles.json and clear its keychain entries.
    The data directory (transactions, PDFs) is NOT deleted — only the profile record."""
    data = gi.load_profiles()
    profiles = data.get("profiles", {})
    if profile_id not in profiles:
        abort(404)
    if profile_id == data.get("active"):
        flash("Switch to a different profile before deleting the active one.", "error")
        return redirect(url_for("settings"))
    if len(profiles) <= 1:
        flash("Cannot delete the only profile.", "error")
        return redirect(url_for("settings"))

    for key in ["bsb", "account_number", "account_name"]:
        try:
            keyring.delete_password("abn-invoice", f"{profile_id}:{key}")
        except Exception:
            pass  # already absent

    del profiles[profile_id]
    gi.PROFILES_FILE.write_text(__import__("json").dumps(data, indent=2))
    flash(
        f"Profile '{profile_id}' deleted. Data in data/{profile_id}/ is untouched.",
        "success",
    )
    return redirect(url_for("settings"))


# ── Entry point ───────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="ABN invoice manager (local webapp)")
    parser.add_argument("--port", type=int, default=5001, help="Port to listen on")
    parser.add_argument(
        "--no-open", action="store_true", help="Do not open browser automatically"
    )
    args = parser.parse_args()

    if not args.no_open:

        def _open_browser():
            import time

            time.sleep(0.8)
            webbrowser.open(f"http://localhost:{args.port}")

        threading.Thread(target=_open_browser, daemon=True).start()

    print(f"ABN invoice manager → http://localhost:{args.port}  (Ctrl-C to stop)")
    app.run(port=args.port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
