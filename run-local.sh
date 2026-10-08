#!/bin/bash
set -euo pipefail
APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$APP_DIR"
exec "${PAPRE_PYTHON:-python3}" -m papre "$@"
