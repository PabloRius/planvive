#!/usr/bin/env bash
# Flujo diario del Plan Vive: scrape -> ingesta CDC -> informe.
# Pensado para un cron diario. Requiere proxies.txt (ver README).
set -euo pipefail
cd "$(dirname "$0")"

echo "[planvive] $(date '+%F %T') — scraping snapshot de hoy…"
python3 planvive.py --proxy-file proxies.txt --rotating-gateway --workers 24 --per-proxy-interval 0.06

echo "[planvive] ingiriendo cambios en la BD (CDC)…"
python3 ingest.py

echo "[planvive] regenerando dashboard…"
python3 dashboard.py

echo "[planvive] hecho -> output/dashboard.html  ·  planvive.db actualizada"
