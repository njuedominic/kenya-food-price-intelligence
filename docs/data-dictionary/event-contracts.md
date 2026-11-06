# Event contracts

There are no event producers or consumers in this milestone. Before implementing
one, specify a versioned contract containing event identity, source/classification,
event and ingestion timestamps, payload schema, unit/currency context, and
deduplication semantics. Validate schema compatibility, quarantine malformed
messages, and document replay/checkpoint behavior.

Simulated events must include an unambiguous synthetic classification and must
not be represented as observed market or official statistical data.
