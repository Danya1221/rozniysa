#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
echo "▶️ rozniysa: запуск launcher.py" >&2
exec python -u launcher.py
