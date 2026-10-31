"""Compose worker snapshots, canonical SQLite, DuckDB and local memory."""
import json
import time
from dataclasses import asdict
from bd3 import Pipeline
from engine import SAMPLE, digest, stable
from voodoosql import Catalog, save_catalog


def run_pipeline(store, recover=False):
    import duckdb
    pipeline = Pipeline(store.root / 'pipeline')
    receipts = []
    interruptions = []
    for index, source in enumerate(SAMPLE):
        batch_id = ('recovery-' if recover else 'demo-') + source['id']
        record = {'record_id': source['id'], 'source_id': source['id'], 'revision': str(source['revision']),
                  'occurred_at': '2026-01-15T12:30:00Z', 'amount': '12.340000000000000001',
                  'payload': stable(source), 'deleted': source['deleted']}
        manifest = pipeline.write_shard('worker-a' if index == 0 else 'worker-b', batch_id,
                                        source['id'], str(source['revision']), [record])
        if recover:
            try:
                pipeline.consolidate(manifest, failpoint='after_commit')
            except RuntimeError as exc:
                interruptions.append(str(exc))
            pipeline = Pipeline(store.root / 'pipeline')
        pipeline.consolidate(manifest)
        if recover:
            try:
                pipeline.deliver(batch_id, failpoint='before_ack')
            except RuntimeError as exc:
                interruptions.append(str(exc))
            pipeline = Pipeline(store.root / 'pipeline')
        receipts.append(asdict(pipeline.deliver(batch_id)))
    sink = duckdb.connect(str(pipeline.sink_path), read_only=True)
    try:
        readback = sink.execute('SELECT record_id,payload,amount FROM records WHERE NOT deleted ORDER BY record_id').fetchall()
    finally:
        sink.close()
    # Memory is projected from verified destination rows, not from an optimistic tool response.
    imported = store.ingest([json.loads(row[1]) for row in readback])
    catalog = Catalog.from_sqlite(pipeline.stage_path)
    save_catalog(catalog, store.root / 'schema-catalog.sqlite')
    (store.root / 'schema-graph.json').write_text(stable(catalog.to_graph()))
    result = {'message': f'{len(readback)} source records verified through two shards, canonical SQLite, DuckDB and memory.',
              'rows': [{'id': row[0], 'amount': str(row[2])} for row in readback],
              'receipts': receipts, 'interruptions': interruptions, 'memory_changed': imported['changed'],
              'catalog': {'tables': len(catalog.tables), 'fingerprint': catalog.fingerprint, 'complete': catalog.completeness['complete']}}
    with store.db() as db:
        db.execute('INSERT OR REPLACE INTO receipts VALUES(?,?,?,?)', ('pipeline:recovery' if recover else 'pipeline:demo', 'pipeline', stable(result), time.time()))
    return result
