#!/usr/bin/env bash
# First-time setup for abn-invoice.
# Run once from the project directory:  bash setup.sh
set -euo pipefail

echo ""
echo "=== ABN Invoice — First-time setup ==="
echo ""

# Guard: must be run from the repo root (where 'invoice' lives).
if [[ ! -f "invoice" ]]; then
    echo "ERROR: Run this script from the abn-invoice directory:"
    echo "       cd abn-invoice && bash setup.sh"
    exit 1
fi

INSTALL_DIR="$(pwd)"

# ── 1. Make the invoice wrapper executable ──────────────────────────────────
chmod +x invoice
echo "[1/5] Made ./invoice executable."

# ── 2. Install Python dependencies ──────────────────────────────────────────
if command -v uv &>/dev/null; then
    echo "[2/5] Installing Python dependencies with uv..."
    uv sync --quiet
    PYTHON="$INSTALL_DIR/.venv/bin/python3"
    echo "      Done. Virtual environment created in .venv/"
else
    echo "[2/5] uv not found."
    echo "      Recommended: install uv with the following command, then re-run setup.sh"
    echo "      curl -LsSf https://astral.sh/uv/install.sh | sh"
    echo ""
    echo "      Continuing with system Python. Make sure flask, jinja2, and keyring are installed:"
    echo "      pip install flask jinja2 keyring"
    PYTHON="python3"
fi

# ── 3. Check optional system dependencies ───────────────────────────────────
echo "[3/5] Checking system dependencies..."
if [[ "$(uname)" == "Darwin" ]]; then
    CHROME_PATH="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if [[ -x "$CHROME_PATH" ]]; then
        echo "      Google Chrome: found."
    else
        echo "      WARNING: Google Chrome not found. PDF generation will not work."
        echo "               Install from https://www.google.com/chrome/"
    fi
    if command -v pdfunite &>/dev/null; then
        echo "      pdfunite (poppler): found."
    else
        echo "      WARNING: pdfunite not found. Receipt merging will not work."
        echo "               Install with: brew install poppler"
    fi
fi

# ── 4. Create your first profile ────────────────────────────────────────────
echo ""
echo "[4/5] Set up your first profile."
echo "      You can add more profiles later via the webapp (Settings)."
echo ""

read -rp "  Your full name (as it will appear on invoices): " FULL_NAME
while [[ -z "$FULL_NAME" ]]; do
    read -rp "  Your full name (required): " FULL_NAME
done

read -rp "  Your ABN (format: XX XXX XXX XXX): " MY_ABN
while [[ -z "$MY_ABN" ]]; do
    read -rp "  Your ABN (required): " MY_ABN
done

read -rp "  Profile ID — lowercase, no spaces (press Enter for 'main'): " PROFILE_ID
PROFILE_ID="${PROFILE_ID:-main}"
# Sanitise: lowercase, replace anything that is not a-z 0-9 _ - with a hyphen
PROFILE_ID="$(echo "$PROFILE_ID" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9_-' '-' | sed 's/-$//')"
echo "      Using profile ID: $PROFILE_ID"

# Write profiles.json and create data directory using env vars (avoids shell-injection)
FULL_NAME="$FULL_NAME" MY_ABN="$MY_ABN" PROFILE_ID="$PROFILE_ID" "$PYTHON" - <<'PYEOF'
import json, pathlib, csv, os

pid   = os.environ["PROFILE_ID"]
name  = os.environ["FULL_NAME"]
abn   = os.environ["MY_ABN"]

profiles = {
    "active": pid,
    "profiles": {pid: {"name": name, "abn": abn}}
}
pathlib.Path("profiles.json").write_text(json.dumps(profiles, indent=2))

data_dir = pathlib.Path("data") / pid
(data_dir / "Invoices").mkdir(parents=True, exist_ok=True)
csv_path = data_dir / "transactions.csv"
if not csv_path.exists():
    fields = [
        "invoice_number", "date", "type", "client_name", "client_abn",
        "description", "service_date", "amount", "status",
        "due_date", "receipt_file", "notes",
        "payment_date", "payment_ref",
    ]
    with open(csv_path, "w", newline="") as f:
        csv.DictWriter(f, fieldnames=fields).writeheader()

print(f"      Profile '{pid}' created.")
print(f"      Transaction ledger: data/{pid}/transactions.csv")
PYEOF

# ── 5. Smoke test — confirm all Python dependencies import correctly ─────────
echo ""
echo "[5/5] Verifying Python dependencies..."
if "$PYTHON" -c "import flask, jinja2, keyring" 2>/dev/null; then
    echo "      All dependencies OK."
else
    echo "      WARNING: One or more dependencies failed to import."
    echo "               Run: pip install flask jinja2 keyring"
fi

echo ""
echo "=== Setup complete! ==="
echo ""
echo "Next step: set your bank details (BSB, account number, account name)."
echo "These are stored securely in your system keychain — never in any file."
echo ""
echo "To start the app:"
echo "  • Double-click 'Start ABN Invoices.command' in Finder"
echo "  • Or from Terminal: ./invoice serve"
echo ""
echo "Then go to Settings → Bank details to enter your bank account."
echo ""
