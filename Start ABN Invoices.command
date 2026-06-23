#!/bin/bash
# Double-click this file in Finder to start the ABN Invoice Manager.
# A Terminal window will stay open while the server is running.
# Close this window (or press Ctrl-C) to stop.

DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR" || {
  echo "ERROR: Could not change to the invoice folder."
  read -r -p "Press Enter to close..."
  exit 1
}

if [[ ! -f "./invoice" ]]; then
  echo "ERROR: 'invoice' script not found in $DIR"
  echo "Make sure this file is in the same folder as your ABN invoice setup."
  read -r -p "Press Enter to close..."
  exit 1
fi

echo "=============================="
echo "  ABN Invoice Manager"
echo "=============================="
echo ""
echo "  Server: http://localhost:5001"
echo "  Your browser will open automatically."
echo "  Close this window to stop the server."
echo ""

./invoice serve
