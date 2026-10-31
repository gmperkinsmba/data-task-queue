import tempfile
import unittest
from pathlib import Path
import duckdb
from engine import Store
from pipeline_adapter import run_pipeline


class PipelineCompositionTests(unittest.TestCase):
    def test_real_sink_projection_replay_and_interruptions(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(root)
            first = run_pipeline(store, recover=True)
            self.assertEqual(len(first['interruptions']), 4)
            self.assertEqual(len(first['rows']), 2)
            self.assertEqual(first['rows'][0]['amount'], '12.340000000000000001')
            self.assertGreater(first['catalog']['tables'], 0)
            self.assertTrue(first['catalog']['complete'])
            self.assertTrue((Path(root) / 'schema-catalog.sqlite').exists())
            self.assertTrue((Path(root) / 'schema-graph.json').exists())
            run_pipeline(Store(root), recover=True)
            self.assertEqual(len(store.state()['memory']), 2)
            self.assertEqual(len(store.state()['tasks']), 1)
            self.assertEqual(len(list((Path(root) / 'pipeline/shards').glob('worker-*.sqlite'))), 2)
            with duckdb.connect(str(Path(root) / 'pipeline/destination.duckdb'), read_only=True) as sink:
                self.assertEqual(sink.execute('SELECT count(*) FROM records').fetchone()[0], 2)
                self.assertEqual(sink.execute('SELECT count(*) FROM delivery_receipts').fetchone()[0], 2)


if __name__ == '__main__':
    unittest.main()
