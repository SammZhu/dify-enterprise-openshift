#!/usr/bin/env bash
set -euo pipefail
exec "${OFFLINE_PYTHON:-python3}" "$(dirname "$0")/offline.py" post-render
