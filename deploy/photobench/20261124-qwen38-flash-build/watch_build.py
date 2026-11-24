"""Wait without inference, then emit one terminal event for the exact build."""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--wait-id', required=True)
parser.add_argument('--state', required=True)
parser.add_argument('--audit', required=True)
args = parser.parse_args()
audit = Path(args.audit)
remote = '/home/realmagic/xy/benchmarks/atm-bench/experiments/qwen38-flash-20261124'
wake = Path.home() / '.codex/skills/wakecodex/scripts/wakecodex.py'
deadline = time.monotonic() + 72 * 3600
errors = 0
while time.monotonic() < deadline:
    try:
        output = subprocess.check_output(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
            '192.168.0.100', f'python3 {remote}/operate.py status'], timeout=100, stderr=subprocess.DEVNULL)
        observed = json.loads(output)
        run = observed.get('run') or {}
        expected = json.loads((audit / 'launch-receipt.json').read_text())['run_ids'][0]
        assert run.get('run_id') == expected, 'Unexpected build identity'
        compact = {k: v for k, v in run.items() if k != 'phases'}
        compact['phases'] = {name: {k: v for k, v in phase.items()
             if k in ['status', 'progress', 'stage', 'processed', 'failed', 'total_seconds', 'error']}
             for name, phase in (run.get('phases') or {}).items()}
        compact['gateway'] = observed['gateway']
        (audit / 'latest-progress.json').write_text(json.dumps(compact, ensure_ascii=False, indent=2))
        errors = 0
        if run.get('status') in ['completed', 'completed_with_errors', 'failed', 'cancelled', 'interrupted']:
            (audit / 'terminal-snapshot.json').write_text(json.dumps(observed, ensure_ascii=False))
            status = 'completed' if run['status'] == 'completed' else 'needs_review'
            event = {'status': status, 'summary': f"Flash album build {run['status']}",
                     'data': {'run_id': expected, 'evidence': str(audit / 'terminal-snapshot.json')}}
            break
    except Exception as error:
        errors += 1
        (audit / 'watch-errors.json').write_text(json.dumps({'at': time.time(), 'consecutive': errors,
                                                          'error_type': type(error).__name__}))
        if errors >= 10:
            event = {'status': 'needs_review', 'summary': 'Ten consecutive build status failures',
                     'data': {'evidence': str(audit / 'watch-errors.json')}}
            break
    time.sleep(60)
else:
    event = {'status': 'needs_review', 'summary': 'Build watcher reached 72 hour deadline'}
path = audit / 'wake-event.json'
path.write_text(json.dumps(event, ensure_ascii=False))
result = subprocess.run([sys.executable, str(wake), '--state', args.state, 'emit', args.wait_id,
                         '--file', str(path)], capture_output=True, text=True)
(audit / 'wake-emission.txt').write_text(result.stdout + result.stderr)
raise SystemExit(result.returncode)
