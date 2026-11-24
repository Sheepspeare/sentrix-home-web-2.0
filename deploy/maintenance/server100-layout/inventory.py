"""Read only server100 path inventory; never print credential values."""
import json
import os
from pathlib import Path
import subprocess

HOME = Path('/home/realmagic')
XY = HOME / 'xy'
UNITS = ['sentrix-atm100-api', 'sentrix-atm100-photobench', 'sentrix-atm100-doubao-api', 'sentrix-atm100-doubao-photobench', 'sentrix-atm100-model', 'sentrix-atm100-wemm', 'sentrix-atm100-doubao-gateway', 'sentrix-atm100-qwen37-gateway', 'sentrix2-api', 'sentrix2-photobench']
KEYS = {'SENTRIX_DATA_DIR','PHOTOBENCH_CONFIG_ROOT','PHOTOBENCH_RESULTS_ROOT','PHOTOBENCH_DATA_ROOT','PHOTOBENCH_WEB_DIST_ROOT','PHOTOBENCH_SENTRIX_REPO_ROOT','PHOTOBENCH_SENTRIX_PID_FILE','PHOTOBENCH_SENTRIX_LOG_PATH','PHOTOBENCH_BUILD_REVISION','TMPDIR','PYTHONPATH'}

def run(args):
    p = subprocess.run(args, text=True, capture_output=True, timeout=30)
    return p.stdout.strip()

report = {'units': {}, 'experiment_children': {}, 'open_paths': [], 'top_files': [], 'path_files': []}
for name in UNITS:
    raw = run(['systemctl','--user','show',name+'.service','-p','MainPID','-p','ExecStart','-p','WorkingDirectory','-p','EnvironmentFiles','-p','ActiveState'])
    vals = dict(line.split('=',1) for line in raw.splitlines() if '=' in line)
    pid = vals.get('MainPID','0')
    if pid != '0':
        try:
            pairs = Path('/proc',pid,'environ').read_bytes().split(b'\0')
            env = dict(pair.decode().split('=',1) for pair in pairs if b'=' in pair)
            vals['paths'] = {k:v for k,v in env.items() if k in KEYS}
        except (OSError, UnicodeError):
            vals['paths'] = {}
    report['units'][name] = vals
exp=XY/'experiments/atm1013-20261118'
for p in sorted(exp.iterdir()):
    report['experiment_children'][p.name] = {'type':'link' if p.is_symlink() else 'dir' if p.is_dir() else 'file','children':sorted(x.name for x in p.iterdir())[:30] if p.is_dir() and not p.is_symlink() else []}
for p in HOME.iterdir():
    if p.is_file() and not p.name.startswith('.'):
        report['top_files'].append({'path':str(p),'size':p.stat().st_size})
for proc in Path('/proc').iterdir():
    if not proc.name.isdigit():continue
    try:
        if proc.stat().st_uid != os.getuid():continue
        for fd in list((proc/'fd').iterdir())+[proc/'cwd']:
            try: target=os.readlink(fd)
            except OSError:continue
            if target.startswith((str(XY),str(HOME)+'/')) and not target.startswith((str(HOME)+'/.',str(HOME)+'/models')):
                report['open_paths'].append({'pid':int(proc.name),'fd':fd.name,'path':target})
    except (PermissionError, FileNotFoundError):pass
# Locate current small path configurations only; file contents stay on server.
for base in [XY/'deploy',exp,HOME/'.config/systemd/user']:
    for root, dirs, files in os.walk(base):
        dirs[:]=[d for d in dirs if d not in {'.git','.venv','node_modules','product-data','cloud-results','__pycache__','tests','src','web-dist'}]
        for name in files:
            p=Path(root)/name
            if p.suffix not in {'.env','.conf','.service','.sh'} or p.is_symlink() or p.stat().st_size>200000:continue
            txt=p.read_text(errors='replace')
            if str(XY)+'/atm-bench-20261110' in txt or str(exp) in txt or str(XY)+'/photobench-typed-20261104' in txt:
                report['path_files'].append(str(p))
print(json.dumps(report,ensure_ascii=False,indent=2))
