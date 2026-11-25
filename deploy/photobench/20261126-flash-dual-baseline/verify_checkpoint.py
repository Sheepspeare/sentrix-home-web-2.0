"""Read-only verification of a baseline checkpoint and its existing HTTP archive."""

import argparse
import gzip
import importlib.util
import json
import sqlite3
from collections import Counter
from pathlib import Path

BASE = Path('/home/realmagic/xy/benchmarks/atm-bench/experiments/flash-dual-baseline-20261126')


def verify(group):
    spec = importlib.util.spec_from_file_location('operate', BASE / 'scripts/operate.py')
    operate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(operate)
    target = BASE / group
    expected = json.loads((target / 'clone-verification.json').read_text())['memory_fingerprints']
    with sqlite3.connect(f'file:{target}/data/sentrix.db?mode=ro', uri=True) as db:
        db.execute('BEGIN')
        actual = operate.memory_fingerprints(db, expected, target / 'data')
    assert actual == expected, 'Album memory changed during QA'
    prepared = json.loads((BASE / 'prepared.json').read_text())
    for pid, ticks in prepared['protected_processes_unchanged'].items():
        assert Path(f'/proc/{pid}/stat').read_text().split()[21] == ticks
    records = [json.loads(line) for line in (BASE / 'model-requests.jsonl').read_text().splitlines()]
    qa = [record for record in records if record.get('max_tokens') != 8]
    for record in records:
        with gzip.open(record['archive'], 'rt') as stream:
            raw = json.load(stream)
        assert raw['metadata']['id'] == record['id']
        assert raw['request']['model'] == 'qwen3.8-flash'
        assert raw['request']['enable_thinking'] is False
        assert record.get('actual_model') in {None, 'qwen3.8-flash'}
        output_tokens = raw['request'].get('max_tokens')
        assert isinstance(output_tokens, int) and 0 < output_tokens <= 4096
        assert output_tokens == record.get('max_tokens')
        if record.get('max_tokens') != 8:
            assert raw['request'].get('temperature') == 0
    return {
        'group': group, 'memory_tables_unchanged': len(actual), 'memory_fingerprints': actual,
        'protected_processes_unchanged': True, 'raw_archives_verified': len(records),
        'qa_requests_shared_gateway': len(qa), 'health_requests': len(records) - len(qa),
        'http_status_counts': dict(Counter(str(record.get('http_status')) for record in records)),
        'qa_finish_counts': dict(Counter(
            reason for record in qa for reason in record.get('finish_reasons', [])
        )),
        'health_finish_counts': dict(Counter(reason for r in records if r not in qa
                                            for reason in r.get('finish_reasons', []))),
        'qa_output_token_limits': dict(Counter(str(record['max_tokens']) for record in qa)),
        'qa_output_limit_reduced_for_context': sum(record['max_tokens'] < 4096 for record in qa),
        'model': 'qwen3.8-flash', 'enable_thinking': False, 'max_tokens': 4096, 'temperature': 0,
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--group', choices=['limited', 'large'], required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    assert not args.output.exists(), 'Keep prior checkpoint evidence'
    result = verify(args.group)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({key: value for key, value in result.items() if key != 'memory_fingerprints'}))
