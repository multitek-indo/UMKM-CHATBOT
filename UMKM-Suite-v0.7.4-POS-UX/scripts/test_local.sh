#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python -m compileall -q "${ROOT}/bot_release/app" "${ROOT}/bridge_app"
bash -n "${ROOT}"/scripts/*.sh
PYTHONPATH="${ROOT}:${ROOT}/bot_release" python -m unittest discover -s "${ROOT}/tests" -v
echo "LOCAL CHECK LULUS"
