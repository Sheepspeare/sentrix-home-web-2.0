"""Resume only missing scores after the limited concurrency transition."""

import json
import subprocess
import sys
import threading
from pathlib import Path

from representative_qa import load, process_environment, save

B = Path('/home/realmagic/xy/benchmarks/atm-bench/experiments')
ORIGINAL = B / 'improved-limited-continue16-20261127'
ROOT = B / 'improved-limited-judge-recovery1-20261127'


def prepare():
    assert not ROOT.exists()
    source = ORIGINAL / 'prepared/improved-limited'
    failure = json.loads((source / 'judge-failure.json').read_text())
    assert failure['type'] == 'ReadIndexRequired'
    assert not list((source / 'judge').glob('batch-*'))
    g = json.loads((source / 'started.json').read_text())
    old_pid = json.loads((source / 'judge-process.json').read_text())['pid']
    proc = Path(f'/proc/{old_pid}/stat')
    assert not proc.exists() or proc.read_text().split(') ')[1].split()[0] == 'Z'
    target = ROOT / 'improved-limited'
    target.mkdir(parents=True)
    save(ROOT / 'intent.json', {'groups': [g], 'qa_unchanged': True})
    save(target / 'started.json', g)
    (target / 'results').symlink_to(source / 'results', target_is_directory=True)
    _, _, env = process_environment(8884)
    env['PYTHONPATH'] = g['evaluation_source'] + ':' + env.get('PYTHONPATH', '')
    # The inherited salvage is a committed JSON snapshot. This reader process
    # accepts both it and existing split manifests; it never writes QA results.
    env.pop('PHOTOBENCH_SPLIT_RESULTS', None)
    log = (ROOT / 'judge.log').open('x')
    child = subprocess.Popen(
        [sys.executable, __file__, '--worker'], env=env, stdout=log, stderr=log,
        start_new_session=True,
    )
    receipt = {'pid': child.pid, 'concurrency': 2}
    save(ROOT / 'launch.json', receipt)
    save(target / 'judge-process.json', receipt)
    print(json.dumps(receipt))


def worker():
    g = json.loads((ROOT / 'intent.json').read_text())['groups'][0]
    finished = threading.Event()

    def await_qa():
        while not (ORIGINAL / 'prepared/improved-limited/qa-terminal.json').exists():
            if finished.wait(10):
                return
        finished.set()

    threading.Thread(target=await_qa, daemon=True).start()
    try:
        load('limited_judge_resume', ORIGINAL / 'continue_four.py').judge(ROOT, g, finished)
    except Exception as exc:
        save(ROOT / 'improved-limited/judge-failure.json',
             {'error': str(exc), 'type': type(exc).__name__})
        raise


if __name__ == '__main__':
    if '--worker' in sys.argv:
        worker()
    else:
        prepare()
