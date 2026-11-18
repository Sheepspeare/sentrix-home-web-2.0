"""Local Python-only monitor; callbacks contain evidence and never restart a run."""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

parser = argparse.ArgumentParser(__doc__)
parser.add_argument('--stage', choices=['q4km', 'qwen37', 'doubao'], required=True)
parser.add_argument('--registration', type=Path, required=True)
parser.add_argument('--audit', type=Path, required=True)
parser.add_argument('--run-id', help='Require this exact active run; a mapping change needs review')
parser.add_argument('--reviewed-active-work-outage', action='store_true',
                    help='Record the reviewed API timeout, observe real product events, wake on API recovery')
parser.add_argument('--check', action='store_true')
args = parser.parse_args()
sender = Path.home() / '.codex/skills/wakecodex/scripts/send_event.py'
args.audit.mkdir(parents=True, exist_ok=True)
started = changed = time.monotonic()
prior = None
failures = 0
while True:
    reason = ''
    try:
        call = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
            '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=2', '192.168.0.100',
            '/home/realmagic/xy/releases/psh-b2030e1-100-20261117/.venv/bin/python',
            '/home/realmagic/xy/experiments/atm1013-20261118/probe.py', args.stage,
            *(['--allow-active-timeout'] if args.reviewed_active_work_outage else [])],
            capture_output=True, text=True, timeout=75, check=True)
        snapshot = json.loads(call.stdout)
        failures = 0
        (args.audit / f'{args.stage}-latest.json').write_text(json.dumps(snapshot, indent=2))
        fingerprint = json.dumps([snapshot['current_phase'], snapshot['item_count'],
            snapshot['imports'], snapshot.get('live_turns'),
            {k: {f: ({key: value for key, value in p.get(f, {}).items()
                if key not in {'poll_count', 'polled_at'}} if f == 'progress' and isinstance(p.get(f), dict)
                else p.get(f)) for f in ['status', 'progress', 'agent_completed', 'judge_completed']}
            for k, p in snapshot['phases'].items()}], sort_keys=True)
        if fingerprint != prior:
            changed, prior = time.monotonic(), fingerprint
        active_rows = snapshot['active']
        active = any(w.get('id') == snapshot['run_id'] and w.get('worker_alive') for w in active_rows or [])
        if args.run_id and snapshot['run_id'] != args.run_id:
            reason = 'Active run mapping changed; inspect the expected exact run'
        elif snapshot['status'] not in ['pending', 'running', 'cancelling']:
            reason = 'Run reached a terminal status; inspect completeness, scoring and exports'
        elif active_rows is not None and not active:
            reason = 'Exact run has no live worker'
        elif not snapshot['model_matches']:
            reason = 'Actual model identity changed'
        elif min(snapshot['disk_free'].values()) < 1536 * 1024**2:
            reason = 'Disk free space below 1.5 GiB'
        elif args.reviewed_active_work_outage and active_rows is not None:
            reason = 'Reviewed status API outage recovered; inspect staged read optimizations'
        elif time.monotonic() - changed > 7200:
            reason = 'No recorded phase/import/QA progress for two hours'
        elif time.monotonic() - started > 7 * 86400:
            reason = 'Watcher reached seven-day deadline'
    except Exception as error:
        failures += 1
        snapshot = {'stage': args.stage, 'probe_error': str(error), 'consecutive_failures': failures}
        if isinstance(error, subprocess.CalledProcessError):
            snapshot['probe_returncode'] = error.returncode
            snapshot['probe_stderr'] = (error.stderr or '')[-4000:]
        if failures >= 3:
            reason = 'Three consecutive exact-run probes failed'
    if args.check:
        print(json.dumps({'stage': args.stage, 'run_id': snapshot.get('run_id'), 'reason': reason,
                          'status': snapshot.get('status'), 'phase': snapshot.get('current_phase'),
                          'error': snapshot.get('probe_error')}))
        sys.exit(1 if reason or failures else 0)
    if reason:
        event = args.audit / f'{args.stage}-event-{int(time.time())}.json'
        event.write_text(json.dumps({'status': 'needs_review', 'reason': reason, 'snapshot': snapshot,
                                    'evidence': str(args.audit / f'{args.stage}-latest.json')}, indent=2))
        subprocess.run([sys.executable, str(sender), '--registration', str(args.registration),
                        '--result', str(event)], check=True)
        print(json.dumps({'event': str(event), 'reason': reason}), flush=True)
        break
    time.sleep(60)
