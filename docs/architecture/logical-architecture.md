# Logical architecture

```mermaid
flowchart TB
  Sources["Verified sources (future)"] --> Ingestion["Python ingestion (future)"]
  Ingestion --> Landing["Landing / immutable Bronze"]
  Landing --> Spark["Spark batch processing (future)"]
  Kafka["Optional Kafka broker"] -. "future simulated events" .-> Streaming["Structured Streaming (future)"]
  Spark --> Silver["Silver: normalized and validated"]
  Streaming --> Silver
  Silver --> Gold["Gold: documented analytical products"]
  Gold --> PostgreSQL["PostgreSQL warehouse"]
  PostgreSQL --> Grafana["Grafana (future dashboards)"]
  Airflow["Airflow (future DAGs)"] -. "orchestrates batch work" .-> Ingestion
  Airflow -.-> Spark
```

Bronze, Silver, and Gold represent distinct trust and transformation boundaries,
not implementations currently present. Preserve original source material and
metadata; make derived and synthetic records identifiable.
