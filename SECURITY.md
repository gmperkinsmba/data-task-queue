# Security

Data Task Queue stores imported records, task reviews, and pipeline state in local SQLite databases. Its server binds to loopback, checks Host and Origin, requires a local request header for writes, and caps request size. Database operations use parameters. SHA-256 source fingerprints support replay and revision checks; they do not encrypt records or prove their origin. Use made-up records and task text in reports.

Local state is plaintext protected by account permissions. Use your Mac’s disk encryption to protect saved files. The native app has an ad-hoc code signature. This lets the installer check whether its files changed. It does not prove who made the app. The build is not notarized.

Report security issues through [GitHub private vulnerability reporting](https://github.com/gmperkinsmba/data-task-queue/security/advisories/new).
