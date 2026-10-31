# Data Task Queue

Import records, find evidence, and review tasks in a local queue.

## First principle

An agent should use evidence that lasts after a restart. Each task should be ready for review.

The app accepts made-up notes from repositories and meetings. Its sample pipeline writes to two SQLite worker shards. It then saves them to one canonical SQLite database and sends them to DuckDB. The app reads back the destination. It uses that data to rebuild memory. The voodooSQL catalog stores the staging schema and exports its table graph. A task stays in review until you approve its current version.

**Install as a Mac app**

Build the native app on macOS with Python 3.12. The pinned build packages are listed in `requirements-desktop.lock.txt`.

```sh
python3.12 -m venv .desktop-venv
.desktop-venv/bin/python -m pip install -r requirements-desktop.lock.txt
.desktop-venv/bin/python build_desktop.py
.desktop-venv/bin/python install_desktop.py
```

The install step puts `Data Task Queue.app` in `~/Applications`. Open it in Finder. Close its window or choose **Quit** to stop the local server. The app bundle includes Python. Runtime data stays in `~/.local/share/ultimate-agent-harness`; set `UAH_RUNTIME` before launching from a shell to use another folder. Moving the app to Trash leaves runtime data in place.

The bundle uses ad-hoc signing. It has no Developer ID signature and is not notarized. macOS may ask you to approve opening it. The native app builds on macOS only.

To work on the native shell, activate `.desktop-venv` and run `.desktop-venv/bin/python desktop.py`.

## Run in a browser

Use Python 3.11 or later on macOS or Linux. Worker and maintenance locks use file locks from the operating system. DuckDB is the only third-party runtime package.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py
```

Open http://127.0.0.1:8333. The server listens only on this computer. It checks the Host and Origin headers. It also needs a custom header for JSON writes. Cross-origin access is off.

Runtime data goes in `~/.local/share/ultimate-agent-harness`, outside the checkout. Use `--data-dir /path/to/private-runtime` to choose another folder. Do not use a shared folder. The app will not use a runtime folder inside its checkout.

## Try it

1. Select **Run data pipeline** to import two made-up sources through storage.
2. Search for `scope` in Evidence. Each result shows its source link and revision.
3. Open Review queue. Edit the proposed task, then approve its current version. The task goes to a local fake repository. It uses its own SQLite file.
4. Open Health & recovery and select **Test pipeline recovery**. The test stops before the canonical commit. It also stops before destination acknowledgement. It then retries the saved batches.
5. Run maintenance to retry pending projections. It also checks a separate backup copy.

The form for made-up records also accepts a JSON array. Each source needs string fields for `id`, `kind`, `title`, `text`, `scope`, and `pointer`. It also needs an integer `revision` and a boolean `deleted` flag. The `kind` must be `repository` or `meeting`. Pointers must start with `sample://`. Increase the revision when content changes. IDs stay strings in storage and delivery.

An optional `action` needs string fields for `id`, `title`, `owner`, and `acceptance`. Reimports keep edits to existing tasks. A corrected source sends its task back to review. A deleted source removes its memory entry. Its task is withdrawn. A tombstone blocks old replays.

To complete a task, add a separate live result source from the same scope. The app checks the destination by reading it back before it marks the task complete. It does not decide if the evidence proves the task's acceptance condition. A reviewer must decide that.

## Settings and limits

Settings chooses the search scope. This release supports local exact text search. It can publish tasks to a local fake repository. It uses SQLite for staging and DuckDB for delivery. It has no remote credentials or model calls. It has no semantic vectors or GitHub writes. It cannot import live transcripts. It has no real send actions or background scheduler. MemPalace may be a future option. It is not installed or supported.

An import can hold up to 100 source records. Each HTTP request can hold up to 256 KiB. Source text is limited to 20,000 characters. Reads return up to 200 state rows and 50 search results. The sample pipeline uses at most two records. This is a local app for one user. It has no user accounts. Do not put it behind a public proxy.

The cleanup tool only suggests removing exact duplicate lines. It also checks that required phrases remain. It never edits files or changes policy. Run maintenance on demand. Its OS lock ends when a process stops. On the next run, an unfinished run is marked interrupted. Then saved steps are retried.

## Test

```sh
.venv/bin/python -m unittest -v test_engine test_pipeline test_http
```

The suite checks rollback and replay. It checks source edits and deletes. It checks failed projections. It checks stale approvals and destination conflicts. It checks missed acknowledgements too. It also checks completion evidence and concurrent maintenance. Backup integrity and document preservation are tested too. The pipeline test uses real SQLite and DuckDB files in a temporary folder.

## Data and provenance

SQLite is the main store for evidence, history, tasks, and pending projections. DuckDB is a derived destination. These databases do not share a transaction. Receipts and retryable work record each step.

All bundled examples are made up. The `bd3` package has original MIT code shared with SQLite Batch Loader. The `voodoosql` module has original MIT catalog code shared with voodooSQL. DuckDB is MIT licensed. The repository has no private source history. It has no personal settings or real source data. Git ignores runtime databases, exports, and local environments.
