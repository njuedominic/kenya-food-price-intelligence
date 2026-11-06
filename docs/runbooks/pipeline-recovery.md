# Pipeline and service recovery

There are no production pipelines in this scaffold. For local service startup
issues:

1. Inspect `docker compose --env-file .env ps` and the affected service logs.
2. Verify `.env` placeholders have been replaced, host ports are available, and
   Docker has sufficient memory and disk.
3. Check PostgreSQL health before Airflow initialization. The initialization
   script fails explicitly if database migration or administrator creation fails.
4. Restart only the affected service after resolving the underlying issue.
   Preserve named volumes unless data loss is intended.
5. Before any future pipeline replay, define source revision, idempotency,
   deduplication, checkpoint, and quarantine recovery behavior. Do not manually
   delete a Spark checkpoint to force a replay without assessing consistency.

Escalate unrecoverable local data issues by preserving logs and volume snapshots
before attempting destructive recovery.
