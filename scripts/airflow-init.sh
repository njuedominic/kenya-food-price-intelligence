#!/usr/bin/env bash
set -euo pipefail

if airflow db check-migrations --migration-wait-timeout 1; then
  echo "Airflow metadata schema is current."
else
  airflow db migrate
fi

if airflow users list --output json | python -c '
import json
import os
import sys

users = json.load(sys.stdin)
username = os.environ["AIRFLOW_ADMIN_USERNAME"]
sys.exit(0 if any(user.get("username") == username for user in users) else 1)
'; then
  echo "Airflow administrator already exists."
else
  airflow users create \
    --username "$AIRFLOW_ADMIN_USERNAME" \
    --password "$AIRFLOW_ADMIN_PASSWORD" \
    --firstname Platform \
    --lastname Admin \
    --role Admin \
    --email "$AIRFLOW_ADMIN_EMAIL"
fi
