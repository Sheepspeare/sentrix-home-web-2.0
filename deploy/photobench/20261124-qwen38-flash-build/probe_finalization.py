"""Read-only progress evidence for this exact build's finalization stage."""

import json
import sqlite3
import time
from collections import Counter
from pathlib import Path

BASE = Path('/home/realmagic/xy/benchmarks/atm-bench/experiments/qwen38-flash-20261124')
DATABASE = Path('/var/tmp/sentrix-qwen38-flash-20261124/product-data/sentrix.db')
SCOPE = 'PhotoBench-20261124-221506-2026.11.17-atm-sources-addresses-qwen3.8-flash'


def inspect(database=DATABASE, request_log=BASE / 'model-requests.jsonl'):
    with sqlite3.connect(f'{database.as_uri()}?mode=ro', uri=True, timeout=5) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA query_only=ON')
        events = dict(connection.execute(
            'SELECT count(*) AS total, sum(revision) AS revision_sum, '
            'max(updated_at) AS latest_updated_at FROM events WHERE scope_id=?', (SCOPE,)
        ).fetchone())
        events['revision_distribution'] = {
            str(row['revision']): row['n'] for row in connection.execute(
                'SELECT revision,count(*) AS n FROM events WHERE scope_id=? GROUP BY revision', (SCOPE,)
            )
        }
        assets = [dict(row) for row in connection.execute(
            'SELECT status,count(*) AS n FROM assets WHERE scope_id=? '
            'AND parent_asset_id IS NULL GROUP BY status', (SCOPE,)
        )]
        operations = [dict(row) for row in connection.execute(
            'SELECT operation_id,kind,status,started_at,finished_at,error '
            'FROM pipeline_receipts WHERE scope_id=? ORDER BY started_at DESC LIMIT 3', (SCOPE,)
        )]
    model_counts, finishes, errors = Counter(), Counter(), Counter()
    successes, completed, last_completed = 0, 0, None
    with request_log.open() as stream:
        for line in stream:
            row = json.loads(line)
            completed += 1
            succeeded = row.get('http_status') == 200 and row.get('actual_model') == 'qwen3.8-flash'
            successes += int(succeeded)
            model_counts[row.get('actual_model', 'missing')] += 1
            finishes.update(row.get('finish_reasons', []))
            if not succeeded:
                errors[str(row.get('http_status') or row.get('error_type') or 'unknown')] += 1
            if succeeded:
                last_completed = row['started_at'] + row['elapsed_seconds']
    return {
        'observed_at': time.time(), 'scope_id': SCOPE,
        'database_read_only': True, 'events': events, 'source_assets': assets,
        'operations': operations,
        'model_requests': {
            'completed': completed, 'successful': successes, 'errors': dict(errors),
            'models': dict(model_counts), 'finish_reasons': dict(finishes),
            'latest_success_finished_at': last_completed,
        },
    }


if __name__ == '__main__':
    print(json.dumps(inspect(), ensure_ascii=False))
