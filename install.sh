#!/bin/bash
# Puts `rn-review` on your PATH so you can run it from anywhere.
set -e
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="/usr/local/bin/rn-review"
ln -sf "$SRC/rn-review" "$DEST"
echo "Installed: $DEST -> $SRC/rn-review"
echo
echo "Try:  rn-review help"
