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

## KNBS monthly CPI PDF contract

### Source discovery and bounded acquisition

The M3.2 command reads the official KNBS CPI listing at
`https://www.knbs.or.ke/cpi-and-inflation-rates/` and accepts exactly one
requested reference month per invocation:

```sh
uv run python scripts/ingest_knbs.py --month 2026-10
```

Months must use `YYYY-MM` with a valid calendar month. The discovery client
rejects an empty or duplicate month list and caps a discovery request at 12
months; this CLI deliberately does not implement range backfills. A missing
listing match or more than one distinct matching official PDF is a failure.
Ambiguous candidate URLs are included in the error for manual review; no
filename-based or newest-looking-version preference is applied, and no URL is
constructed from a date. A publication not yet listed produces a failed run
record and no successful snapshot.

The existing HTTP transport provides bounded timeouts and exponential retries.
Defaults are a 25-second timeout, two retries (three attempts maximum), and a
0.5-second backoff factor. The listing response is limited to 5,000,000 bytes;
the streamed PDF is limited to 40,000,000 bytes by default, configurable with
`--max-pdf-bytes`. The client enforces the limit while streaming and rejects a
declared or observed oversize response. The standard-library redirect handler
limits redirects to ten; final listing and PDF URLs must remain on an official
KNBS host. A 403 is reported as an error and is not bypassed. The PDF content
type must be `application/pdf` or `application/octet-stream`; the latter is
accepted only together with the PDF signature. Acquisition uses temporary
system storage outside published snapshot directories. Failed temporary
downloads are removed by the HTTP client; a process interruption cannot
publish a snapshot, though the operating system may retain an abandoned
temporary file until its normal cleanup.

### PDF integrity and identity validation

The PDF is saved byte-for-byte, without rewriting metadata, compression, pages,
or embedded content. SHA-256 is computed over the downloaded bytes, then
verified again while copying them into the same-filesystem snapshot staging
directory. A snapshot is not published unless HTTP status is 200, the response
is nonempty and within the configured limit, the content type and `%PDF-`
signature are appropriate, a trailing `%%EOF` marker is present, and `pypdf`
can read a nonempty document with at least one page. No page-count constant is
assumed. The parser is used only for structural validation and non-mutating
cover/reference-period text checks; it does not execute active PDF content or
extract statistical tables. Nonfatal parser warnings alone do not reject a
document. A clearly mismatching cover period fails validation. If the report
period or title cannot be verified from source listing/PDF text, the manifest
marks it `unverified`; a publication date is recorded only when official page
metadata provides a parseable date and its basis.

### Snapshot identity, layout, and manifest

The publication identity is the statistical release month, for example
`2026-10`. It is independent of the snapshot identity, which identifies the
exact raw PDF version. Binary descriptor schema version 2 separates identity
from filename provenance:

```json
{
  "descriptor_schema_version": 2,
  "identity": {
    "source_id": "knbs_cpi_monthly",
    "publication_identity": "2026-10",
    "sha256": "<raw PDF SHA-256>",
    "size_bytes": 12345
  },
  "provenance": {"filename": "report.pdf"}
}
```

The snapshot ID is `sha256-` plus the SHA-256 of the canonical `identity`
object. A renamed file with the same bytes and publication identity therefore
reuses the existing snapshot; its original filename remains in the immutable
descriptor and manifest, and the newly discovered URL remains in the new run
record. Run IDs, filenames, retrieval timestamps, source URLs, and publication
dates do not affect the content-derived identity. With the default output root,
storage is:

```text
data/
├── bronze/knbs_cpi_monthly/publication=2026-10/snapshots/<snapshot-id>/
│   ├── descriptor.json
│   ├── manifest.json
│   └── report.pdf
└── manifests/knbs_cpi_monthly/
    ├── locks/publication=2026-10.lock
    └── runs/<run-id>.json
```

The immutable manifest uses schema version 1 and includes:

- **Identity:** source ID, publication identity/reference month, and snapshot ID.
- **Source:** official listing URL, discovered PDF URL, optional report page,
  source title, verified publication date and its evidence, and KNBS attribution.
- **File:** original filename, response content type, PDF size, SHA-256, and
  structurally verified page count.
- **Validation:** HTTP status, content type/signature, EOF marker, readable PDF
  structure, page count, and verified or explicitly unverified period/title.
- **Integrity:** the content-derived snapshot digest.
- The UTC time the snapshot was first published.

Schema version 2 changes only new binary descriptor identity semantics.
Previously published M3.2 schema-version-1 descriptors and snapshot directories
are not rewritten or deleted. Their historical IDs included the filename; an
ingestion after upgrade can consequently publish a schema-version-2 snapshot
for matching bytes rather than automatically migrating or relinking the old
snapshot. World Bank descriptors and snapshot IDs are unchanged.

The first successful manifest records the discovered URL. If the listing later
uses a different URL for identical bytes and the same publication identity,
the existing snapshot and manifest are reused unchanged. The new discovered URL
is recorded in that run's record rather than mutating immutable provenance.

### Run records, idempotency, and recovery

Each invocation writes an atomic JSON run record to
`data/manifests/knbs_cpi_monthly/runs/<uuid-hex>.json`. Schema version 1
records the run ID and timestamps, requested month, discovered source URL and
filename, retrieval time, outcome, snapshot ID, checksum, size, page count,
validation results, and bounded failure category/message where applicable. It
contains no PDF bytes or credentials.

- `created`: a new complete, verified snapshot was published.
- `unchanged`: the same publication identity and PDF bytes already exist, and
  the stored descriptor, manifest, and payload checksum passed verification.
- `failed`: discovery, download, PDF validation, existing-snapshot integrity,
  or publication failed. A failed run creates no new successful snapshot.

Changed bytes for an existing publication month create a new content-addressed
snapshot; prior snapshots are retained. Existing corruption is reported as an
integrity failure and is not overwritten. Each month has a cooperating-writer
advisory `fcntl.flock` lock. Downloads remain outside published snapshot
directories; the storage layer copies and hashes into a `.staging-` directory
under the final snapshots parent, writes and verifies the manifest, then
atomically renames the complete directory. Identical concurrent ingestions
converge on one snapshot; different revisions remain separate. As for World
Bank storage, non-cooperating writers and filesystem/hardware behavior remain
outside the advisory-lock and `fsync` guarantees.

This command preserves whole source documents only. KNBS attribution is kept in
the manifest, but the project does not assert that a PDF is licensed for
redistribution; confirm current KNBS terms before sharing stored copies. No
county-level inflation coverage or estimates are produced.

## FAOSTAT normalized Producer Prices ZIP contract

### Source identity and acquisition

M3.3 ingests the catalogued FAOSTAT Producer Prices (`PP`) normalized bulk
archive:

- Dataset: `47c17894-8ca1-4bd0-ba4d-6078e919e9b1`
- Domain: `https://www.fao.org/faostat/en/#data/PP`
- Catalog: `https://data.fao.org/catalog/dataset/47c17894-8ca1-4bd0-ba4d-6078e919e9b1`
- Download: `https://bulks-faostat.fao.org/production/Prices_E_All_Data_(Normalized).zip`

Run the standalone command with the repository's locked environment:

```sh
uv run python scripts/ingest_faostat.py --help
uv run python scripts/ingest_faostat.py
```

The command has no arbitrary URL override. It reuses the established FAOSTAT
dataset descriptor, source client, shared HTTP transport, binary Bronze
publisher, and source run-record writer. The profiling command continues to
use `data/landing/faostat/`; M3.3 downloads into a unique temporary directory
and never replaces or removes a profiling archive.

The archive layout is checked against the source-profiled normalized dataset:
one `Prices_E_All_Data_(Normalized).csv` data member and the four
`Prices_E_AreaCodes.csv`, `Prices_E_Elements.csv`, `Prices_E_Flags.csv`, and
`Prices_E_ItemCodes.csv` lookup members. All are expected at the archive root.
Missing required files, duplicate names, or unrecognized additional members
fail closed rather than silently selecting a different dataset. A future
upstream layout change therefore requires an explicit reviewed update to this
contract.

### Download and validation resource limits

Defaults are finite and configurable through the CLI and `FAOSTATClient`:

| Limit | Default | Enforcement |
|---|---:|---|
| Compressed download | 256 MiB | Reject oversized declared `Content-Length` before streaming and count streamed bytes in the HTTP transport |
| Archive members | 100 | Checked from the central directory before reading member data |
| Declared uncompressed size per member | 512 MiB | Checked before opening the member |
| Aggregate declared uncompressed size | 1 GiB | Checked before opening any member |
| Actual uncompressed bytes per member | 512 MiB | Counted during 64-KiB member reads; the read is capped at the remaining budget plus one byte |
| Actual aggregate uncompressed bytes | 1 GiB | Counted across member reads with the same bounded-read rule |

An available `Content-Length` must be a nonnegative decimal integer, must not
exceed the download limit, and must match the number of bytes actually read.
An absent length is allowed; streaming still enforces the cap. A timeout,
interrupted response, retryable HTTP status, invalid length, or non-retryable
HTTP error does not produce a successful snapshot. The shared `urllib` client
uses its configured socket timeout and follows its bounded standard-library
redirect policy; a final URL outside HTTPS `fao.org` hosts is rejected. Two
retries and exponential backoff from 0.5 seconds are the defaults.

The compressed response is first written to a unique file-backed temporary
location. ZIP members are read sequentially in bounded chunks without
`extractall`, permanent extraction, or full-member buffering. Declared limits
are preflight checks; actual byte counts are independently enforced while
decompressing. No compression-ratio threshold is applied, avoiding rejection
of legitimate highly-compressed CSV data. ZIP metadata and decompression
libraries still consume process memory; these input limits are not a strict
maximum on total Python memory. A complete temporary archive and a staging
copy may coexist during publication, so up to roughly twice the configured
compressed cap can be used temporarily in addition to retained snapshots.

### ZIP security and integrity checks

The validator requires a nonempty regular ZIP file with a readable central
directory, the exact expected archive member set, and no encrypted members.
Member paths must be flat, relative names; POSIX absolute paths, drive paths,
backslashes, empty/dot/parent components, and names colliding after Unicode
NFC normalization and case folding are rejected. Duplicate or ambiguous names,
directory entries, symlinks, special file types, and compression methods other
than stored/deflated are rejected. There are no extracted paths that can escape
the validator's temporary area.

Before reading, declared per-member and aggregate uncompressed sizes are
checked. Then every regular member is streamed to completion. The reader counts
actual decompressed bytes, compares them with declared lengths, and relies on
the ZIP reader's CRC check at end-of-member. The manifest inventory records
each member name, compressed/decompressed sizes, CRC value, validated byte
count, and `crc_passed` result. Raw ZIP SHA-256 is computed by the HTTP
transport and verified again while the binary publisher copies the file.

These checks establish byte integrity and constrained ZIP readability, not
source authenticity or statistical correctness. CRC32 is an accidental
corruption check, not a cryptographic authenticity mechanism. Bronze does not
parse every CSV row, validate FAOSTAT statistical flags, or assert that values
are analytically correct. The original complete global archive remains the
source of truth; Kenya filtering and analytical checks are deferred to Silver.

### Immutable payload, identity, and manifest

The ZIP is retained exactly as acquired under:

```text
data/
├── bronze/faostat_producer_prices_normalized/publication=normalized/
│   └── snapshots/<snapshot-id>/
│       ├── Prices_E_All_Data_(Normalized).zip
│       ├── descriptor.json
│       └── manifest.json
└── manifests/faostat_producer_prices_normalized/
    ├── locks/publication=normalized.lock
    └── runs/<run-id>.json
```

The source ID is `faostat_producer_prices_normalized`; `normalized` is a
stable partition key for this single logical archive family, not a claimed
upstream release date. Binary descriptor schema version 2 identifies content
using canonical JSON containing the stable source ID, that partition key, raw
ZIP SHA-256, and compressed byte size. The snapshot ID is `sha256-` followed
by the SHA-256 of those canonical identity bytes. Retrieval time, run ID,
temporary path, ETag, and source URL do not affect the ID. Identical archive
bytes from the same logical dataset therefore reuse a snapshot, while changed
bytes create a separate content-addressed version. No existing World Bank,
KNBS, or profiling snapshot is migrated or rewritten.

The schema-version-1 snapshot manifest includes the source/dataset/publisher
and license attribution; archive filename, content type, size, and SHA-256;
the complete ZIP member inventory; aggregate declared and validated
uncompressed sizes; validation markers for structure, member safety, CRC and
resource bounds; and the snapshot creation timestamp. No FAOSTAT release date
is invented. When no reliable upstream release identifier is available, the
content-derived snapshot ID tracks the locally observed version.

The source-profiled files are preserved whole, including records for every
area. Ingestion does not extract the CSV, filter observations to Kenya, rewrite
ZIP metadata, normalize CSV cells, or create Parquet/database outputs.

### Run records, idempotency, and recovery

Every invocation writes an atomic append-only schema-version-1 run record to
`data/manifests/faostat_producer_prices_normalized/runs/<uuid-hex>.json`. It
records the run and UTC timestamps, approved request URL, HTTP status and
retrieval time when available, source `Last-Modified`/ETag provenance,
outcome, snapshot ID, archive byte count and SHA-256, member count, validation
status, and bounded failure category/message. These HTTP validators do not
affect content identity. The record contains no payload bytes or request
headers.

- `created`: a new complete archive passed ZIP/resource/CRC validation and was
  atomically published.
- `unchanged`: the content-derived snapshot already existed; its payload,
  descriptor, manifest and inventory were verified and left unchanged.
- `failed`: retrieval, archive validation, existing-snapshot integrity or
  publication failed. A failed run never reports a successful outcome.

The publisher copies and hashes the archive into a unique `.staging-` directory
under the final snapshots parent, writes and verifies metadata, then atomically
renames the complete directory into place. A per-archive advisory
`fcntl.flock` serializes cooperating publishers. Interrupted publication
staging is removed on the next publication attempt; an interrupted acquisition
can leave only a unique temporary file/directory and cannot publish a
snapshot. Identical concurrent runs converge on one snapshot; different
revisions remain separate. Existing payload or metadata corruption raises an
integrity failure and is never silently overwritten. A snapshot can exist if
the subsequent run-record write fails; inspect both locations before retrying.
As with the other Bronze publishers, advisory locks do not control unrelated
writers and filesystem `fsync`/rename behavior depends on the local filesystem.

FAOSTAT and its catalog generally identify CC BY 4.0, subject to FAO terms and
possible third-party restrictions. Attribution and terms metadata are retained
in the manifest, but this implementation does not claim that every archive
component is unrestricted for redistribution. Check current dataset/source
conditions before sharing stored archives.

## Non-goals and limitations

- No RTFP, WFP, or NDMA Bronze ingestion.
- No transformation to analytical rows, Parquet, Silver, or warehouse tables.
- No Airflow, Spark, Kafka, Docker, cloud storage, or database dependency.
- No automatic deletion or overwrite policy for snapshots.
- No claim that a successful API response is licensed for redistribution;
  source-specific terms still apply.
- Retrieval is bounded by HTTP timeout and retry configuration. The complete
  paginated result is held in memory before publication, which is appropriate
  for the currently profiled national CPI series but is not a general
  large-dataset streaming design.
