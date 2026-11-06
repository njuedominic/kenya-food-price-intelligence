# System context

The platform is a locally operated analytical system. KNBS, World Bank, FAOSTAT,
and any verified county source are candidate external providers, not implemented
or assumed integrations. Users operate local orchestration, compute, storage,
and visualization services.

```mermaid
flowchart LR
  Sources["Candidate public data sources<br/>verified before connection"] -->|future, licensed ingestion| Platform["Local Kenya Food Price<br/>Intelligence Platform"]
  Operator["Developer / analyst"] -->|local Compose| Platform
  Platform --> Lake["Local filesystem lake"]
  Platform --> Warehouse["Local PostgreSQL"]
  Platform --> Views["Future Grafana dashboards"]
```

Only the local service foundation is scaffolded. There are no source
observations, external API credentials, or county-level inflation values in this
repository.
