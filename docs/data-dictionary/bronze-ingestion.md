# World Bank Bronze ingestion contract

## Scope and source identity

This contract covers only the Kenya national CPI Indicators API series:

| Source field | Meaning |
|---|---|
| API root | `https://api.worldbank.org/v2` |
| Country | `KEN` (response identity `KE` / `Kenya`) |
| Indicators | `FP.CPI.TOTL` and `FP.CPI.TOTL.ZG` |
| Request path | `/country/KEN/indicator/{indicator}` |
| Request parameters | `format=json`, configured `per_page`, and page number |

## Acquisition resource bounds

The client and CLI enforce finite acquisition bounds:

| Limit | Default | CLI option |
|---|---:|---|
| Maximum response pages | 100 | `--max-pages` |
| Maximum raw bytes per response | 5 MiB (5,242,880 bytes) | `--max-page-bytes` |
| Maximum raw bytes across all response pages | 25 MiB (26,214,400 bytes) | `--max-total-bytes` |

The same values can be configured through `WorldBankClient`'s `max_pages`,
`max_page_bytes`, and `max_total_bytes` constructor arguments. Every limit must
be a positive integer (booleans are not accepted), and the aggregate byte limit
must be at least as large as the per-response limit. The existing `per_page`
limit remains 1,000 observations.

The HTTP transport reads at most the configured page limit plus one byte to
detect overflow. Later pages are additionally limited by the remaining
aggregate-byte budget. A page-count, per-response, or aggregate-byte violation
raises a validation error, records a failed run when local storage is
available, and publishes no snapshot. The client never truncates an oversized
response or silently drops pages; the acquired snapshot is all pages or none.
An empty result is accepted only with a structurally valid response page and a
positive reported page count.

These limits bound the retained raw response payload, not total process memory.
The complete set of response bodies and decoded observation objects is held in
memory before publication, and Python object overhead can exceed the raw-byte
limits.

The API response is a two-element JSON array: pagination metadata followed by
the observation array. Every page is independently validated for JSON format,
pagination identity, requested indicator, Kenya identity, and observation
structure. Null observation values are valid and remain untouched. No
analytical normalization, filtering, sorting, unit conversion, or Parquet
conversion is performed.

## Local layout

With the default `--output-root data`, a completed snapshot is published as:

```text
data/
├── bronze/world_bank/indicator=<indicator>/snapshots/<snapshot-id>/
│   ├── descriptor.json
│   ├── manifest.json
│   ├── page-0001.json
│   └── page-0002.json
└── manifests/world_bank/
    ├── locks/indicator=<indicator>.lock
    └── runs/indicator=<indicator>/<run-id>.json
```

Only the pages actually retrieved appear in a snapshot. Empty API results still
preserve the first response page, including its original JSON representation.
Runtime payloads, manifests, run records, lock files, and staging directories
are local data and are excluded from Git.

## Snapshot identity and immutability

For each page, the client retains the exact HTTP response body bytes and
computes SHA-256 over those bytes. The content descriptor contains:

- `source_id`, `country_code`, and `indicator_code`
- canonical request parameters (`format` and `per_page`)
- pages in retrieval order, with page number, requested URL, and page SHA-256

The descriptor is serialized as UTF-8 JSON with sorted keys, compact separators,
and no retrieval time. The snapshot ID is `sha256-` followed by the SHA-256
hex digest of that canonical serialization. Thus identical bytes and request
identity produce the same ID; a changed page or request parameter creates a
different ID. Page framing is explicit in the descriptor; raw page bodies are
not concatenated to calculate the snapshot ID.

The raw `page-NNNN.json` files are byte-for-byte API responses. They are never
re-serialized. A changed response creates a new snapshot directory; prior
snapshots are retained and never overwritten by this implementation.

## Snapshot manifest schema

`manifest.json` has `manifest_schema_version: 1` and describes immutable
content. It includes:

- **Identity:** source, country, indicator, and content-derived snapshot ID.
- **Source:** API root, dataset identity, request parameters, and page URLs.
- **Pagination:** API-reported page count, retrieved page count, page size,
  and API-reported observation total.
- **Reference-year coverage:** minimum/maximum year and distinct year count
  from validated observations; both bounds are null for an empty result.
- **Integrity:** per-page filename, request URL, content type, SHA-256, byte
  size, and observation count; total raw bytes, total observation count, and
  snapshot digest.
- **Validation:** successful complete-pagination and total-count checks; null
  observations are explicitly permitted.
- The UTC time the snapshot was first published.

The snapshot manifest does not contain a run ID or change on an identical
rerun. Existing snapshots are verified before reuse: the descriptor-derived
ID, page order, raw checksums, byte sizes, response structure, pagination,
observation counts, and manifest totals must agree. Missing or corrupt files
raise an integrity error and are not overwritten automatically.

## Run-record schema and outcomes

Each invocation receives a UUID-hex `ingestion_run_id` and writes one atomic
JSON run record under
`data/manifests/world_bank/runs/indicator=<indicator>/`. The run-record
schema version is `1`. A record includes source/country/indicator identity,
UTC start and completion timestamps, request parameters, outcome, snapshot ID
when known, created/reused action, page and observation counts when available,
validation status, and a bounded error category/message on failure. It contains
no response body, authorization header, traceback, or secret.

The only outcomes are:

- `created`: a new complete snapshot was published.
- `unchanged`: the same content-derived snapshot already existed and verified.
- `failed`: retrieval, validation, integrity, or publication failed.

To find the latest successfully observed content, inspect run records in
completion-time order and select the latest `created` or `unchanged` outcome;
a failed attempt does not replace the previous successful snapshot.

## Atomic publication, locking, and recovery

All work is local. For one indicator, an advisory `fcntl.flock` lock serializes
the check-and-publish operation on macOS and Linux. Independent indicators use
separate locks. Temporary snapshot directories are uniquely created under the
final snapshots parent, so staging and publication remain on the same
filesystem. Files are flushed and `fsync`ed, staged content is verified, and
the completed directory is renamed into place atomically. The success manifest
is present before that directory becomes visible at the final snapshot path.
Run records are written to unique temporary files and linked into place without
replacing an existing record.

An exception during a normal staging write removes that staging directory. If
the process is interrupted, the next locked publication for that indicator
removes only leftover directories with the implementation's `.staging-`
prefix. Partial retrievals are not staged or published; an API failure is
recorded as `failed` where the local manifest filesystem remains writable.

The lock is advisory: another program that ignores it can interfere with local
files. Atomic rename and `fsync` behavior depend on the local filesystem;
although file and directory synchronization is requested, this contract does
not claim guaranteed survival of every power-loss or hardware failure. A
snapshot can be present even if the subsequent run-record write fails; in that
case the CLI surfaces a storage error and operators should inspect both the
snapshot and run-record directory before retrying.

## Non-goals and limitations

- No KNBS, FAOSTAT, RTFP, WFP, or NDMA ingestion.
- No transformation to analytical rows, Parquet, Silver, or warehouse tables.
- No Airflow, Spark, Kafka, Docker, cloud storage, or database dependency.
- No automatic deletion or overwrite policy for snapshots.
- No claim that a successful API response is licensed for redistribution;
  source-specific terms still apply.
- Retrieval is bounded by HTTP timeout and retry configuration. The complete
  paginated result is held in memory before publication, which is appropriate
  for the currently profiled national CPI series but is not a general
  large-dataset streaming design.
