"""Durable evidence and reviewed task projections. All external adapters are opt-in."""
import contextlib
import fcntl
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path


def stable(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(stable(value).encode()).hexdigest()


SAMPLE = [
    {'id': 'repo:001', 'revision': 1, 'kind': 'repository', 'title': 'Synthetic release notes',
     'text': 'The sample search API requires an explicit project scope. Keep identifiers as strings.',
     'scope': 'sample', 'pointer': 'sample://repository/release-notes#L1', 'deleted': False},
    {'id': 'meeting:001', 'revision': 1, 'kind': 'meeting', 'title': 'Synthetic planning meeting',
     'text': 'Morgan will test search scope before the sample release. Acceptance: results exclude other projects.',
     'scope': 'sample', 'pointer': 'sample://meeting/planning#00:42', 'deleted': False,
     'action': {'id': 'task:001', 'title': 'Test search scope', 'owner': 'Morgan',
                'acceptance': 'Results exclude other projects.'}}
]


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.path = self.root / 'state.sqlite'
        with self.db() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY, revision INTEGER NOT NULL, payload TEXT NOT NULL, hash TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS source_history(id TEXT, revision INTEGER, payload TEXT, hash TEXT, PRIMARY KEY(id,revision));
            CREATE TABLE IF NOT EXISTS projections(source_id TEXT PRIMARY KEY, revision INTEGER, status TEXT, error TEXT);
            CREATE TABLE IF NOT EXISTS memory(source_id TEXT PRIMARY KEY, revision INTEGER, scope TEXT, title TEXT, text TEXT, pointer TEXT);
            CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, source_id TEXT, source_revision INTEGER, version INTEGER, title TEXT, owner TEXT, acceptance TEXT, status TEXT, evidence TEXT);
            CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY, kind TEXT, result TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS maintenance(id TEXT PRIMARY KEY, status TEXT, result TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
            ''')
        with self.repo() as db:
            db.execute('CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, version INTEGER, payload TEXT)')

    @contextlib.contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextlib.contextmanager
    def repo(self):
        db = sqlite3.connect(self.root / 'fake-task-repository.sqlite', timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def validate(self, item):
        if not isinstance(item, dict):
            raise ValueError('Each source must be an object.')
        for key in ('id', 'kind', 'title', 'text', 'scope', 'pointer'):
            if not isinstance(item.get(key), str) or not item[key] or len(item[key]) > (20000 if key == 'text' else 500):
                raise ValueError('Source fields must be nonempty bounded strings: ' + key)
        if type(item.get('revision')) is not int or not 1 <= item['revision'] <= 2147483647:
            raise ValueError('Revision must be a positive integer.')
        if item['kind'] not in ('repository', 'meeting') or type(item.get('deleted')) is not bool:
            raise ValueError('Invalid source kind or deletion marker.')
        if not item['pointer'].startswith('sample://'):
            raise ValueError('This release accepts only sample:// evidence pointers.')
        if item.get('action'):
            for key in ('id', 'title', 'owner', 'acceptance'):
                if not isinstance(item['action'].get(key), str) or not item['action'][key] or len(item['action'][key]) > 500:
                    raise ValueError('Action fields must be nonempty bounded strings.')

    def ingest(self, sources, failpoint=None):
        if not isinstance(sources, list) or not 1 <= len(sources) <= 100:
            raise ValueError('Import between 1 and 100 source records.')
        for item in sources:
            self.validate(item)
        changed = 0
        with self.db() as db:
            for item in sources:
                old = db.execute('SELECT * FROM sources WHERE id=?', (item['id'],)).fetchone()
                if old and item['revision'] < old['revision']:
                    continue
                if old and item['revision'] == old['revision']:
                    if old['hash'] != digest(item):
                        raise ValueError('Conflicting source content at the same revision.')
                    continue
                db.execute('INSERT OR REPLACE INTO sources VALUES(?,?,?,?)', (item['id'], item['revision'], stable(item), digest(item)))
                db.execute('INSERT INTO source_history VALUES(?,?,?,?)', (item['id'], item['revision'], stable(item), digest(item)))
                db.execute('INSERT OR REPLACE INTO projections VALUES(?,?,?,?)', (item['id'], item['revision'], 'pending', ''))
                changed += 1
            if failpoint == 'before_commit':
                raise RuntimeError('Injected failure before source commit.')
        if failpoint == 'after_commit':
            raise RuntimeError('Injected failure after source commit. Projection remains pending.')
        self.project()
        return {'changed': changed, 'state': self.state()}

    def project(self, failpoint=None):
        with self.db() as db:
            pending = db.execute("SELECT s.* FROM sources s JOIN projections p ON s.id=p.source_id WHERE p.status!='complete'").fetchall()
        for row in pending:
            try:
                if failpoint == 'memory':
                    raise RuntimeError('Injected memory projection failure.')
                item = json.loads(row['payload'])
                with self.db() as db:
                    # Recheck under the write transaction; a stale retry cannot restore old evidence.
                    db.execute('BEGIN IMMEDIATE')
                    current = db.execute('SELECT revision FROM sources WHERE id=?', (item['id'],)).fetchone()
                    if current['revision'] != item['revision']:
                        continue
                    db.execute('DELETE FROM memory WHERE source_id=?', (item['id'],))
                    affected = list(db.execute("SELECT id FROM tasks WHERE evidence=? AND status='complete'", (item['id'],)))
                    db.execute("UPDATE tasks SET status='review',version=version+1 WHERE evidence=? AND status='complete'", (item['id'],))
                    with self.repo() as repo:
                        for task in affected:
                            repo.execute('DELETE FROM tasks WHERE id=?', (task['id'],))
                    if item['deleted']:
                        db.execute("UPDATE tasks SET status='source_deleted', version=version+1 WHERE source_id=? AND status!='source_deleted'", (item['id'],))
                    else:
                        db.execute('INSERT INTO memory VALUES(?,?,?,?,?,?)', (item['id'], item['revision'], item['scope'], item['title'], item['text'], item['pointer']))
                        if item.get('action'):
                            action = item['action']
                            existing = db.execute('SELECT source_id FROM tasks WHERE id=?', (action['id'],)).fetchone()
                            if existing and existing['source_id'] != item['id']:
                                raise ValueError('Task ID belongs to a different source.')
                            db.execute('INSERT OR IGNORE INTO tasks VALUES(?,?,?,?,?,?,?,?,?)', (action['id'], item['id'], item['revision'], 1, action['title'], action['owner'], action['acceptance'], 'review', ''))
                            db.execute("UPDATE tasks SET source_revision=?,status='review',version=version+1 WHERE id=? AND source_revision<?", (item['revision'], action['id'], item['revision']))
                    with self.repo() as repo:
                        for task in db.execute('SELECT * FROM tasks WHERE source_id=?', (item['id'],)):
                            if item['deleted']:
                                repo.execute('DELETE FROM tasks WHERE id=?', (task['id'],))
                            elif task['status'] == 'review':
                                # A corrected source revokes the previous published action until review.
                                repo.execute('DELETE FROM tasks WHERE id=? AND version<?', (task['id'], task['version']))
                    db.execute("UPDATE projections SET status='complete',error='' WHERE source_id=? AND revision=?", (item['id'], item['revision']))
            except Exception as exc:
                with self.db() as db:
                    db.execute("UPDATE projections SET status='failed',error=? WHERE source_id=? AND revision=?", (str(exc), row['id'], row['revision']))
                raise
        return self.state()

    def search(self, query, scope):
        if not isinstance(query, str) or len(query) > 500 or not isinstance(scope, str) or not scope:
            raise ValueError('Search needs text and an explicit scope.')
        with self.db() as db:
            return [dict(row) for row in db.execute('SELECT * FROM memory WHERE scope=? AND instr(lower(text),lower(?))>0 ORDER BY source_id LIMIT 50', (scope, query))]

    def edit_task(self, task_id, version, title, owner, acceptance):
        if not all(isinstance(v, str) and 0 < len(v) <= 500 for v in (task_id, title, owner, acceptance)):
            raise ValueError('Task text is required and limited to 500 characters per field.')
        with self.db() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not row or row['version'] != version:
                raise ValueError('Task changed since this view. Reload before editing.')
            if row['status'] == 'source_deleted':
                raise ValueError('Restore the source with a newer revision before reviewing this task.')
            db.execute("UPDATE tasks SET title=?,owner=?,acceptance=?,status='review',version=version+1 WHERE id=?", (title, owner, acceptance, task_id))
        return self.state()

    def approve(self, task_id, version, failpoint=None):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if not row or row['version'] != version or row['status'] not in ('review', 'publish_pending'):
                raise ValueError('Task approval is stale or task is not reviewable.')
            item = dict(row)
            db.execute("UPDATE tasks SET status='publish_pending' WHERE id=?", (task_id,))
        item['status'] = 'approved'
        with self.repo() as repo:
            previous = repo.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            if previous and previous['version'] > version:
                raise ValueError('Destination has a newer human edit.')
            if previous and previous['version'] == version and previous['payload'] != stable(item):
                raise ValueError('Destination changed. Review the conflict.')
            repo.execute('INSERT OR REPLACE INTO tasks VALUES(?,?,?)', (task_id, version, stable(item)))
        if failpoint == 'after_destination':
            raise RuntimeError('Injected interruption after fake repository commit.')
        with self.repo() as repo:
            readback = repo.execute('SELECT payload FROM tasks WHERE id=?', (task_id,)).fetchone()
        if not readback or readback['payload'] != stable(item):
            raise ValueError('Destination verification failed.')
        with self.db() as db:
            updated = db.execute("UPDATE tasks SET status='approved' WHERE id=? AND version=? AND status='publish_pending'", (task_id, version)).rowcount
            if not updated:
                raise ValueError('Local task changed during publication; review before retry.')
            db.execute('INSERT OR REPLACE INTO receipts VALUES(?,?,?,?)', ('task:' + task_id + ':' + str(version), 'fake_repository', digest(item), time.time()))
        return self.state()

    def complete(self, task_id, version, evidence):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
            proof = db.execute('SELECT * FROM memory WHERE source_id=?', (evidence,)).fetchone()
            if not row or row['version'] != version or row['status'] != 'approved' or not proof:
                raise ValueError('Completion requires the current approved task and a live source evidence ID.')
            if proof['source_id'] == row['source_id']:
                raise ValueError('Add a separate result source before completing the task.')
            source = json.loads(db.execute('SELECT payload FROM sources WHERE id=?', (row['source_id'],)).fetchone()['payload'])
            if proof['scope'] != source['scope']:
                raise ValueError('Completion evidence must belong to the same source scope.')
            item = dict(row)
            item.update(status='complete', evidence=evidence)
            with self.repo() as repo:
                destination = repo.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
                if not destination or destination['version'] != version:
                    raise ValueError('Destination task changed. Review its current version.')
                prior = json.loads(destination['payload'])
                if any(prior[k] != row[k] for k in ('title', 'owner', 'acceptance')):
                    raise ValueError('Destination task content changed.')
                repo.execute('UPDATE tasks SET payload=? WHERE id=? AND version=?', (stable(item), task_id, version))
            with self.repo() as repo:
                verified = repo.execute('SELECT payload FROM tasks WHERE id=?', (task_id,)).fetchone()['payload'] == stable(item)
            if not verified:
                raise ValueError('Completion readback failed.')
            db.execute("UPDATE tasks SET status='complete',evidence=? WHERE id=? AND version=?", (evidence, task_id, version))
        return self.state()

    def maintenance(self, failpoint=None):
        lock = self.root / 'maintenance.lock'
        handle = lock.open('a')
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise ValueError('Maintenance already running.')
        run_id = str(time.time_ns())
        steps = []
        with self.db() as db:
            db.execute("UPDATE maintenance SET status='interrupted',result=? WHERE status='running'", (stable([{'step': 'previous run', 'status': 'failed', 'reason': 'Process stopped before its receipt. Retrying durable steps.'}]),))
            db.execute('INSERT INTO maintenance VALUES(?,?,?,?)', (run_id, 'running', '[]', time.time()))
        try:
            self.project()
            steps.append({'step': 'retry projections', 'status': 'passed'})
            if failpoint == 'backup':
                raise RuntimeError('Injected backup failure.')
            backup = self.root / 'backup.sqlite'
            with self.db() as db:
                target = sqlite3.connect(backup)
                db.backup(target)
                target.close()
            restored = sqlite3.connect(backup)
            check = restored.execute('PRAGMA integrity_check').fetchone()[0]
            count = restored.execute('SELECT count(*) FROM sources').fetchone()[0]
            restored.close()
            if check != 'ok':
                raise RuntimeError('Backup failed integrity check.')
            steps.append({'step': 'restore backup in isolated connection', 'status': 'passed', 'sources': count})
            steps.append({'step': 'remote adapters', 'status': 'skipped', 'reason': 'No remote adapters configured.'})
            status = 'passed'
        except Exception as exc:
            steps.append({'step': 'maintenance', 'status': 'failed', 'reason': str(exc)})
            status = 'failed'
        finally:
            with self.db() as db:
                db.execute('UPDATE maintenance SET status=?,result=? WHERE id=?', (status, stable(steps), run_id))
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()
        return {'id': run_id, 'status': status, 'steps': steps}

    def settings(self, value=None):
        if value is not None:
            if value not in ('sample', 'sandbox'):
                raise ValueError('Select a supported synthetic scope.')
            with self.db() as db:
                db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', ('scope', value))
        with self.db() as db:
            row = db.execute("SELECT value FROM settings WHERE key='scope'").fetchone()
        return {'scope': row['value'] if row else 'sample', 'memory': 'Local exact text', 'tasks': 'Local fake repository', 'remote_credentials': 'Not supported in this release'}

    def state(self):
        with self.db() as db:
            return {table: [dict(row) for row in db.execute('SELECT * FROM ' + table + ' LIMIT 200')] for table in ('sources', 'projections', 'memory', 'tasks', 'receipts', 'maintenance')}


def propose_cleanup(text, required):
    """Propose exact duplicate line removal. Never applies user policy changes."""
    if not isinstance(text, str) or len(text) > 20000 or not isinstance(required, list) or len(required) > 100 or not all(isinstance(item, str) for item in required):
        raise ValueError('Cleanup input exceeds its budget.')
    seen, lines = set(), []
    for line in text.splitlines():
        if line.strip() and line in seen:
            continue
        seen.add(line)
        lines.append(line)
    proposal = '\n'.join(lines)
    missing = [fact for fact in required if fact not in proposal]
    return {'proposal': proposal, 'preserved': not missing, 'missing': missing, 'applied': False}
