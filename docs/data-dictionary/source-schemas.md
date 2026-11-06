# Source schemas and provenance

No upstream schema is asserted until its source, license, access method, and
current schema have been inspected and recorded. Candidate providers are KNBS,
World Bank Open Data, FAOSTAT, and a verified Kenya county-level source if one
is accessible. Do not infer county inflation observations.

Every observation should preserve:

| Field | Meaning |
|---|---|
| `source_system` | Identifiable originating provider or explicit synthetic generator |
| `dataset_name` | Provider's dataset/series identifier, not an invented label |
| `source_url` | Retrieval endpoint or source document |
| `ingestion_timestamp` | UTC time this version entered the platform |
| `observation_date` | Source period/date, retaining the source's precision |
| `pipeline_run_id` | Reproducible execution identifier |
| `schema_version` | Version of the platform's validated source contract |
| `record_hash` | Deterministic content identity for revision/deduplication |

Record units, currency, geography, license, retrieval parameters, and source
revision metadata where applicable. Keep official observations, derived
statistics, and synthetic events explicitly classified.
