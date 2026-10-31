# Domain

A source is an identified repository note or meeting record. Its revision increases when content changes. Equal revisions with different content are conflicts. A deletion is a newer source revision that remains in history.

Memory is a derived text index with source ID, revision, scope and evidence pointer. Search requires an explicit scope. Pending or failed projections remain visible until a retry succeeds.

A task is a reviewed proposal with a stable ID, owner, acceptance condition and version. Source reimports preserve human edits. Approval refers to one version; the local destination is a separate fake repository. Completion requires a live result source and destination readback.

A batch crosses three storage stages: a worker SQLite snapshot, canonical SQLite and DuckDB. Receipts distinguish acceptance from destination acknowledgement. Replaying an acknowledged batch does not duplicate records.

Maintenance retries derived views and validates a backup. One process holds its OS lock. A failed step remains in its run receipt. Cleanup produces a proposal and never applies policy changes.

All state is local and outside the source checkout. The application accepts fabricated inputs and has no live remote adapters.
