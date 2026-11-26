#!/bin/sh
# How an online server starts BuyerLenz: update the database layout, load reference lists
# and the first admin, then serve. The host passes the real visitor address through its proxy.
set -e
cd "$(dirname "$0")"
alembic upgrade head
python -m app.bootstrap
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --proxy-headers --forwarded-allow-ips '*'
