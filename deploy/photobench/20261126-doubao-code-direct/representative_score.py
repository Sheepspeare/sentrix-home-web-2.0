"""Score available representative answers on the server, preserving prior successes."""
import argparse
import concurrent.futures
import importlib.util
import json
import os
import shlex
import threading
import time
from pathlib import Path


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(root):
    plan = json.loads((root / 'intent.json').read_text())
    base = load('server_follow', plan['follow_source'])
    owner = base.Follow(root)
    assert not (root / 'started.json').exists()
    for record in plan['source_files']:
        assert base.sha(record['path']) == record['sha256']
    key = None
    for line in Path(plan['secret_env']).read_text().splitlines():
        if line.startswith('QWEN_API_KEY='):
            key = shlex.split(line.split('=', 1)[1])[0]
    assert key
    assert owner.protocol['url'].startswith('https://')
    os.environ.update(QWEN_API_KEY=key, FLASH_DIRECT_URL=owner.protocol['url'],
                      FLASH_DIRECT_HTTP_DIR=str(root / 'http/raw'))
    load('direct_transport', plan['transport_source']).install()
    from photobench.application.run_execution.judge import JudgeRunner
    ids = plan['representative_ids']
    assert len(ids) == len(set(ids)) == 83
    owner.save(root / 'started.json', {'at': time.time(), 'qa_requests': 0,
                                     'per_group_judge_concurrency': 2})

    def group(g):
        name, port, run = g['name'], g['port'], g['run_id']
        folder = root / name
        folder.mkdir()
        scores, snapshot = {}, None
        for record in g['previous_states']:
            assert base.sha(record['path']) == record['sha256']
            state = json.loads(Path(record['path']).read_text())
            assert state['run_id'] == run
            if snapshot is None:
                snapshot = state['qa_snapshot']
            assert snapshot == state['qa_snapshot']
            base.merge_scores(scores, state)
        assert len(scores) == g['previous_scored']
        old_scores = dict(scores)
        present, page = {}, 1
        while True:
            listing = owner.collector.OP.get(port, f'runs/{run}/items?page={page}&page_size=100')
            present.update({v['qa_id']: v for v in listing['items'] if v['qa_id'] in ids})
            if not listing.get('has_next'):
                break
            page += 1
        todo = base.missing_ids(ids, present, scores)
        items, unscorable = [], []
        for qa in todo:
            item, movements = owner.lookup.detail_by_qa(owner.collector.OP.get, port, run, qa)
            if item.get('execution_status') != 'completed':
                unscorable.append({'qa_id': qa, 'execution_status': item.get('execution_status'),
                                   'revision': item['trace_storage']['revision']})
                continue
            item, checks = owner.collector.hydrate(item, port, run)
            assert item['qa_id'] == qa
            owner.save(folder / (qa + '.receipt.json'), {'qa_id': qa,
                'revision': item['trace_storage']['revision'], 'checks': checks,
                'index_movements': movements})
            items.append(item)
        source = {'run_id': run, 'qa_snapshot': snapshot, 'items': items}
        owner.save(folder / 'input.json', source)
        runner = JudgeRunner(http=owner.http(name), judge_url=owner.protocol['url'],
                             judge_model=owner.protocol['model'], judge_api_key=key,
                             cancel=threading.Event(),
                             evaluation_options=owner.protocol['evaluation_options'])
        owner.update(name, status='scoring', available_representatives=len(present),
                     previous_valid=sum(qa in ids for qa, _ in scores), new_targets=len(items))
        result = owner.scorer.score_saved(source, owner.protocol, folder, runner, concurrency=2)
        path = folder / 'scoring-state.json'
        if path.exists():
            base.merge_scores(scores, json.loads(path.read_text()))
        assert all(scores[k] == v for k, v in old_scores.items())
        for record in g['previous_states']:
            assert base.sha(record['path']) == record['sha256']
        selected = [v for (qa, _), v in scores.items() if qa in ids]
        owner.save(folder / 'scores.json', selected)
        summary = {'status': result['status'], 'valid': len(selected),
                   'valid_mean': sum(v['normalized_score'] for v in selected) / len(selected),
                   'missing_answers': [qa for qa in ids if qa not in present],
                   'unscorable': unscorable, 'scorer_result': result}
        owner.save(folder / 'terminal.json', summary)
        owner.update(name, **summary)
        return name, summary

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        results = dict(pool.map(group, plan['groups']))
    good = all(v['status'] == 'completed' for v in results.values())
    owner.save(root / 'terminal.json', {'status': 'completed' if good else 'needs_review',
                                      'at': time.time(), 'groups': results})
    return 0 if good else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    args = parser.parse_args()
    try:
        raise SystemExit(main(args.root))
    except Exception as error:
        (args.root / 'failure.json').write_text(json.dumps({
            'status': 'failed', 'error_type': type(error).__name__, 'error': str(error)}))
        raise
