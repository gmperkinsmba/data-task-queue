import copy
import fcntl
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from engine import Store, SAMPLE, propose_cleanup


class DurableTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_replay_and_restart(self):
        self.store.ingest(SAMPLE)
        self.assertEqual(Store(self.tmp.name).ingest(SAMPLE)['changed'], 0)
        self.assertEqual(len(self.store.state()['tasks']), 1)
        self.assertEqual(len(self.store.search('scope', 'sample')), 2)
        self.assertEqual(self.store.search('scope', 'sandbox'), [])

    def test_validation_rolls_back(self):
        bad = copy.deepcopy(SAMPLE)
        bad[1]['id'] = 1.0
        with self.assertRaises(ValueError):
            self.store.ingest(bad)
        self.assertEqual(self.store.state()['sources'], [])
        with self.assertRaises(RuntimeError):
            self.store.ingest(SAMPLE, 'before_commit')
        self.assertEqual(self.store.state()['sources'], [])

    def test_projection_recovery(self):
        with self.assertRaises(RuntimeError):
            self.store.ingest(SAMPLE, 'after_commit')
        self.assertEqual(self.store.state()['memory'], [])
        self.assertTrue(all(r['status'] == 'pending' for r in self.store.state()['projections']))
        with self.assertRaises(RuntimeError):
            self.store.project('memory')
        self.assertTrue(any(r['status'] == 'failed' for r in self.store.state()['projections']))
        Store(self.tmp.name).project()
        self.assertEqual(len(self.store.state()['memory']), 2)

    def test_correction_preserves_human_edit_and_deletion(self):
        self.store.ingest(SAMPLE)
        self.store.edit_task('task:001', 1, 'Human title', 'Riley', 'Human criteria')
        updated = copy.deepcopy(SAMPLE[1]); updated['revision'] = 2
        updated['text'] = 'Corrected transcript.'
        self.store.ingest([updated])
        task = self.store.state()['tasks'][0]
        self.assertEqual(task['title'], 'Human title')
        self.assertEqual(task['source_revision'], 2)
        self.store.approve(task['id'], task['version'])
        updated['revision'] = 3; updated['deleted'] = True
        self.store.ingest([updated])
        self.store.ingest(SAMPLE)
        self.assertEqual(len(self.store.state()['memory']), 1)
        self.assertEqual(self.store.state()['tasks'][0]['status'], 'source_deleted')
        with self.store.repo() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM tasks').fetchone()[0], 0)

    def test_same_revision_conflict(self):
        self.store.ingest(SAMPLE)
        conflict = copy.deepcopy(SAMPLE[0]); conflict['text'] = 'Changed without revision'
        with self.assertRaises(ValueError):
            self.store.ingest([conflict])
        self.assertEqual(self.store.search('scope', 'sample')[1]['text'], SAMPLE[0]['text'])

    def test_task_stale_approval_and_destination_ack_recovery(self):
        self.store.ingest(SAMPLE)
        self.store.edit_task('task:001', 1, 'Edited task', 'Morgan', 'Check the scope')
        with self.assertRaises(ValueError):
            self.store.approve('task:001', 1)
        with self.assertRaises(RuntimeError):
            self.store.approve('task:001', 2, 'after_destination')
        self.assertEqual(self.store.state()['tasks'][0]['status'], 'publish_pending')
        Store(self.tmp.name).approve('task:001', 2)
        self.assertEqual(len(self.store.state()['receipts']), 1)
        with self.store.repo() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM tasks').fetchone()[0], 1)

    def test_completion_requires_separate_live_evidence(self):
        self.store.ingest(SAMPLE)
        self.store.approve('task:001', 1)
        for invalid in ('missing', 'meeting:001'):
            with self.assertRaises(ValueError):
                self.store.complete('task:001', 1, invalid)
        self.store.complete('task:001', 1, 'repo:001')
        self.assertEqual(self.store.state()['tasks'][0]['status'], 'complete')
        with self.store.repo() as db:
            self.assertEqual(json.loads(db.execute('SELECT payload FROM tasks').fetchone()[0])['status'], 'complete')

    def test_destination_human_edit_preserved(self):
        self.store.ingest(SAMPLE)
        with self.assertRaises(RuntimeError):
            self.store.approve('task:001', 1, 'after_destination')
        with self.store.repo() as db:
            db.execute("UPDATE tasks SET version=9, payload='human edit'")
        with self.assertRaises(ValueError):
            self.store.approve('task:001', 1)
        with self.store.repo() as db:
            self.assertEqual(db.execute('SELECT payload FROM tasks').fetchone()[0], 'human edit')

    def test_corrected_completion_evidence_withdraws_claim(self):
        self.store.ingest(SAMPLE)
        self.store.approve('task:001', 1)
        self.store.complete('task:001', 1, 'repo:001')
        result = copy.deepcopy(SAMPLE[0]); result['revision'] = 2; result['deleted'] = True
        self.store.ingest([result])
        self.assertEqual(self.store.state()['tasks'][0]['status'], 'review')
        with self.store.repo() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM tasks').fetchone()[0], 0)

    def test_maintenance_exclusion_failure_and_restore(self):
        self.store.ingest(SAMPLE)
        with (Path(self.tmp.name) / 'maintenance.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(ValueError):
                self.store.maintenance()
        self.assertEqual(self.store.maintenance('backup')['status'], 'failed')
        self.assertEqual(self.store.maintenance()['status'], 'passed')
        with sqlite3.connect(Path(self.tmp.name) / 'backup.sqlite') as db:
            self.assertEqual(db.execute('SELECT count(*) FROM sources').fetchone()[0], 2)
        self.assertEqual(len(self.store.state()['maintenance']), 2)

    def test_cleanup_preserves_rules_and_never_applies(self):
        text = 'Require review before publication.\nIDs remain strings.\nIDs remain strings.'
        result = propose_cleanup(text, ['Require review before publication.', 'IDs remain strings.'])
        self.assertTrue(result['preserved']); self.assertFalse(result['applied'])
        self.assertEqual(result['proposal'].count('IDs remain strings.'), 1)
        self.assertFalse(propose_cleanup(text, ['Missing rule'])['preserved'])


if __name__ == '__main__':
    unittest.main()
