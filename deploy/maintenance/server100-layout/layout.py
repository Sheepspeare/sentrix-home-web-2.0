"""Explicit server100 directory migration. Run inspect, then apply once, then verify.

Only named paths in plan.json move. Same-filesystem moves use Linux
RENAME_EXCHANGE so the old path remains continuously resolvable. Frozen records
are not rewritten. Credential-bearing configuration backups remain server-side.
"""
from __future__ import annotations
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import time
import urllib.request

HERE = Path(__file__).resolve().parent
PLAN = json.loads((HERE / 'plan.json').read_text())
ROOT = Path('/home/realmagic/xy/diagnostics/directory-layout')
CLOUD_SERVICES = ['sentrix-atm100-doubao-photobench.service', 'sentrix-atm100-doubao-api.service', 'sentrix-atm100-doubao-gateway.service', 'sentrix-atm100-qwen37-gateway.service']
MAPPING = dict(PLAN['mappings'])
PATTERN = re.compile('|'.join(re.escape(x) for x in sorted(MAPPING, key=len, reverse=True)))

def translate(s):
    return PATTERN.sub(lambda m: MAPPING[m.group()], s)

def run(args, timeout=60):
    p = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if p.returncode:
        raise RuntimeError(f'{args[0]} failed ({p.returncode}): {p.stderr[-1500:]}')
    return p.stdout

def save(name, value):
    ROOT.mkdir(parents=True, exist_ok=True)
    p = ROOT / name
    p.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    p.chmod(0o600)

def get_json(port, path):
    with urllib.request.urlopen(f'http://127.0.0.1:{port}{path}', timeout=30) as r:
        return json.load(r)

def idle():
    work = {str(port): get_json(port, '/api/v1/active-work') for port in [8772, 8872, 8874]}
    assert all(not v['work'] for v in work.values()), 'An evaluation is active'
    db = Path(PLAN['experiment_old']) / 'doubao-reuse-49f347c/product-data/sentrix.db'
    with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=10) as conn:
        counts = {}
        for table in ['agent_turns', 'import_jobs']:
            columns = {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}
            if 'status' in columns:
                counts[table] = dict(conn.execute(f'SELECT status, count(*) FROM {table} GROUP BY status'))
    assert all(not v.get(k, 0) for v in counts.values() for k in ['pending', 'running', 'processing', 'waiting_dependency']), counts
    return {'evaluations': work, 'doubao_database_status_counts': counts}

def exchange(src, dst):
    libc = ctypes.CDLL(None, use_errno=True)
    fn = libc.renameat2
    fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    fn.restype = ctypes.c_int
    rc = fn(-100, os.fsencode(src), -100, os.fsencode(dst), 2)
    if rc:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err), str(src))

def move(src, dst, alias=True):
    src, dst = Path(src), Path(dst)
    assert src.exists() or src.is_symlink(), src
    assert not dst.exists() and not dst.is_symlink(), dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    if alias:
        # Exchanging the real object with this self-pointing link atomically
        # places the link at src and the original object at dst.
        dst.symlink_to(dst, target_is_directory=src.is_dir())
        try:
            exchange(src, dst)
        except BaseException:
            dst.unlink()
            raise
    else:
        src.rename(dst)
    return {'source': str(src), 'destination': str(dst), 'old_path_alias': alias}

def self_test():
    p = ROOT / 'exchange-test'
    assert not p.exists()
    (p / 'old').mkdir(parents=True)
    (p / 'old/value').write_text('same inode')
    identity = (p / 'old/value').stat().st_ino
    move(p / 'old', p / 'new')
    assert (p / 'old/value').stat().st_ino == identity == (p / 'new/value').stat().st_ino
    shutil.rmtree(p)

def metadata(roots):
    rows = {}
    for top in roots:
        for base, dirs, files in os.walk(top, followlinks=False):
            # Media and frozen results are checked without reading large bodies.
            dirs[:] = [d for d in dirs if not (Path(base) / d).is_symlink()]
            for name in files:
                p = Path(base) / name
                if p.is_symlink(): continue
                s = p.stat()
                rows[str(p)] = [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns]
    return rows

def open_cloud_refs():
    prefix = PLAN['cloud_old']
    refs = []
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit() or proc.name == str(os.getpid()): continue
        try:
            if proc.stat().st_uid != os.getuid(): continue
            entries = list((proc / 'fd').iterdir()) + [proc / 'cwd']
        except (OSError, PermissionError): continue
        for entry in entries:
            try: target = os.readlink(entry)
            except OSError: continue
            if target == prefix or target.startswith(prefix + '/'):
                refs.append({'pid': proc.name, 'entry': entry.name, 'path': target})
    return refs

def config_paths():
    paths = set(Path(x) for x in PLAN['path_files'])
    # Include current environment files and unit output paths without printing values.
    bases = [Path('/home/realmagic/.config/systemd/user'), Path('/home/realmagic/xy/deploy'), Path(PLAN['cloud_old']) / 'scripts', Path(PLAN['experiment_old'])]
    for base in bases:
        for root, dirs, files in os.walk(base, followlinks=False):
            dirs[:] = [d for d in dirs if d not in {'.git', '.venv', 'node_modules', 'product-data', 'cloud-results', '__pycache__', 'src', 'tests', 'payload', 'candidate', 'web-dist'}]
            for name in files:
                p = Path(root) / name
                if p.is_symlink() or p.suffix not in {'.env', '.conf', '.service', '.sh'}: continue
                if p.stat().st_size > 200000: continue
                paths.add(p)
    return sorted(p for p in paths if p.is_file() and not p.is_symlink())

def inspect():
    ROOT.mkdir(parents=True, exist_ok=True)
    self_test()
    report = {'idle': idle(), 'cloud_open_refs': open_cloud_refs(), 'path_configuration_count': len(config_paths()), 'moves': PLAN['moves'], 'experiment_entries': len(PLAN['experiment_children']), 'home_files': PLAN['home_files']}
    save('preflight.json', report)
    print(json.dumps(report, ensure_ascii=False, indent=2))

def rewrite(path, backups, rewritten):
    p = Path(path)
    if not p.is_file() or p.is_symlink(): return
    old = p.read_bytes()
    try: text = old.decode('utf-8')
    except UnicodeError: return
    new = translate(text).encode('utf-8')
    if new == old: return
    backup = backups / hashlib.sha256(str(p).encode()).hexdigest()
    backup.write_bytes(old)
    backup.chmod(0o600)
    # Write in place to preserve the inode and any hard-link identity.
    p.write_bytes(new)
    rewritten.append({'path': str(p), 'before_sha256': hashlib.sha256(old).hexdigest(), 'after_sha256': hashlib.sha256(new).hexdigest(), 'backup': str(backup)})

def copy_cloud():
    src, dst = Path(PLAN['cloud_old']), Path(PLAN['cloud_new'])
    assert src.is_dir() and not src.is_symlink()
    assert not dst.exists()
    assert shutil.disk_usage(dst.parent).free > 16 * 1024**3
    dst.mkdir(mode=0o700)
    # The first copy runs while services remain available; the final copy is offline.
    log = ROOT / 'cloud-copy.log'
    with log.open('w') as out:
        subprocess.run(['rsync', '-aH', str(src) + '/', str(dst) + '/'], stdout=out, stderr=out, check=True, timeout=900)
    return src, dst

def apply():
    ROOT.mkdir(parents=True, exist_ok=True)
    assert not (ROOT / 'apply-intent.json').exists(), 'Already started; inspect saved receipts before any further action'
    pre = idle()
    configs = config_paths()
    before_roots = [m['source'] for m in PLAN['moves']] + [PLAN['experiment_old']]
    rows = metadata(before_roots)
    save('file-identities-before.json', rows)
    # Check the immutable manifest bytes separately from inode identities.
    manifests = {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in rows if Path(p).name in {'manifest.json','run-manifest.json','commit.json'} and '/results' in p}
    save('manifest-sha256-before.json', manifests)
    protected = run(['systemctl', '--user', 'show', 'sentrix-atm100-model.service', 'sentrix-atm100-api.service', 'sentrix-atm100-photobench.service', 'sentrix2-api.service', 'sentrix2-photobench.service', 'sentrix-nic-thermal-recorder.service', '-p', 'Id', '-p', 'MainPID'])
    (ROOT / 'protected-pids-before.txt').write_text(protected)
    save('apply-intent.json', {'idle': pre, 'time': time.time(), 'plan_sha256': hashlib.sha256((HERE/'plan.json').read_bytes()).hexdigest(), 'identity_count': len(rows)})
    completed = []
    for m in PLAN['moves']:
        completed.append(move(m['source'], m['destination']))
        save('moves.json', completed)
    old_exp = Path(PLAN['experiment_old'])
    mirror = Path(PLAN['experiment_legacy'])
    completed.append(move(old_exp, mirror))
    save('moves.json', completed)
    for m in PLAN['experiment_children']:
        completed.append(move(mirror / m['name'], m['destination']))
        save('moves.json', completed)
    completed.append(move(**{'src': PLAN['data_move']['source'], 'dst': PLAN['data_move']['destination']}))
    for m in PLAN['tail_moves']:
        completed.append(move(m['source'], m['destination']))
    # Top-level home logs have no live file handles (checked again here).
    home_sources = {m['source'] for m in PLAN['home_files']}
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit(): continue
        try: entries = list((proc/'fd').iterdir())
        except OSError: continue
        for fd in entries:
            try: target = os.readlink(fd)
            except OSError: continue
            assert target not in home_sources, f'Open home file {target}'
    for m in PLAN['home_files']:
        completed.append(move(m['source'], m['destination'], alias=False))
    save('moves.json', completed)
    # Migrate the old /var/tmp cloud instance to the same persistent hierarchy.
    src, dst = copy_cloud()
    idle()
    run(['systemctl', '--user', 'stop', *CLOUD_SERVICES], timeout=60)
    save('cloud-stop.json', {'services': CLOUD_SERVICES, 'time':time.time()})
    try:
        assert not open_cloud_refs(), open_cloud_refs()
        run(['rsync', '-aH', '--delete', str(src)+'/', str(dst)+'/'], timeout=900)
        delta = run(['rsync', '-aHnci', '--delete', str(src)+'/', str(dst)+'/'], timeout=900)
        assert not delta.strip(), delta[:1000]
        save('cloud-content-verification.json', {'rsync_checksum_differences': delta, 'source':str(src),'destination':str(dst)})
        # Exchange within /var/tmp, then remove only the verified old copy.
        temporary = src.with_name('.sentrix-atm-doubao-layout-verified-copy')
        assert not temporary.exists() and not temporary.is_symlink()
        temporary.symlink_to(dst, target_is_directory=True)
        exchange(src, temporary)
        assert src.is_symlink() and src.resolve() == dst
        shutil.rmtree(temporary)
        backups = ROOT / 'config-backups'
        backups.mkdir(mode=0o700)
        rewritten = []
        for original in configs:
            rewrite(original, backups, rewritten)
        # The old path mirror resolves every historical absolute experiment path.
        # Use canonical paths in symlink targets for future reads.
        links = []
        for base in [Path(PLAN['atm'])/'datasets', dst]:
            for root, dirs, files in os.walk(base, followlinks=False):
                dirs[:] = [d for d in dirs if d not in {'product-data','.venv','node_modules','.git'}]
                for name in dirs + files:
                    p = Path(root)/name
                    if not p.is_symlink(): continue
                    target = os.readlink(p)
                    updated = translate(target)
                    if updated != target:
                        q = p.with_name(p.name+'.layout-tmp')
                        q.symlink_to(updated)
                        q.replace(p)
                        links.append({'path':str(p),'before':target,'after':updated})
        save('rewritten-configs.json', rewritten)
        save('rewritten-links.json', links)
        # Record the canonical directory map, without credential values.
        save('path-map.json', MAPPING)
        run(['systemctl','--user','daemon-reload'])
    finally:
        # Starting only the four services that this migration stopped prevents
        # unrelated product queues from being resumed by a gratuitous restart.
        run(['systemctl','--user','start', *reversed(CLOUD_SERVICES)], timeout=90)
    save('apply-complete.json', {'time':time.time(),'moves':len(completed)})
    verify()

def verify():
    rows=json.loads((ROOT/'file-identities-before.json').read_text())
    rewritten=json.loads((ROOT/'rewritten-configs.json').read_text())
    changed={str(Path(r['path']).resolve()) for r in rewritten}
    differences=[]
    stable=0
    for old, expected in rows.items():
        new=Path(translate(old))
        # Generic old experiment-root replacements may not describe nested data:
        # Path translation includes the specific instance mapping above.
        try:s=new.stat()
        except FileNotFoundError:
            differences.append({'old':old,'new':str(new),'reason':'missing'});continue
        actual=[s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns]
        if actual==expected:stable+=1;continue
        if actual[:2] == expected[:2] and (str(new.resolve()) in changed or new.suffix=='.log' or new.name.endswith(('.db','-wal','-shm')) or '/.git/' in str(new) or '/.process-diagnostics/' in str(new) or new.name == '.photobench-instance.lock'):
            continue
        differences.append({'old':old,'new':str(new),'before':expected,'after':actual})
    manifest_diffs=[]
    manifests=json.loads((ROOT/'manifest-sha256-before.json').read_text())
    for old, expected in manifests.items():
        if hashlib.sha256(Path(translate(old)).read_bytes()).hexdigest()!=expected:manifest_diffs.append(old)
    services={}
    for unit in CLOUD_SERVICES:
        raw=run(['systemctl','--user','show',unit,'-p','MainPID','-p','ActiveState','-p','NRestarts'])
        services[unit]=dict(line.split('=',1) for line in raw.splitlines())
    protected=run(['systemctl', '--user', 'show', 'sentrix-atm100-model.service', 'sentrix-atm100-api.service', 'sentrix-atm100-photobench.service', 'sentrix2-api.service', 'sentrix2-photobench.service', 'sentrix-nic-thermal-recorder.service', '-p', 'Id', '-p', 'MainPID'])
    report={'identity_count':len(rows),'unchanged_identity_size_mtime':stable,'identity_differences':differences,'frozen_manifest_count':len(manifests),'frozen_manifest_differences':manifest_diffs,'protected_pids_unchanged':protected==(ROOT/'protected-pids-before.txt').read_text(),'cloud_services':services,'old_experiment_mirror':str(Path(PLAN['experiment_old']).resolve()),'time':time.time()}
    save('verification.json',report)
    print(json.dumps(report,ensure_ascii=False,indent=2))
    assert not differences and not manifest_diffs
    assert report['protected_pids_unchanged']
    assert all(v['ActiveState']=='active' for v in services.values())

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['inspect','apply','verify'])
    args=parser.parse_args()
    {'inspect':inspect,'apply':apply,'verify':verify}[args.action]()
