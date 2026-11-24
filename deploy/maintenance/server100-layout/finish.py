"""Finish canonical dataset storage and classify the original PhotoBench files."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import layout

HERE=Path(__file__).resolve().parent
P=json.loads((HERE/'finish-plan.json').read_text())
R=layout.ROOT
assert (R/'apply-complete.json').exists()
assert not (R/'finish-intent.json').exists()
layout.idle()
layout.save('finish-intent.json',{'default_files':len(P['default_files'])})
extra={m['source']:m['destination'] for m in P['default_files']}
for m in P['default_files']:
    extra[m['source'].replace(P['default_new'],P['default_old'])]=m['destination']
extra[P['dataset_old']]=P['dataset_new']
# Keep the old instance's complete name space in the hidden compatibility map.
base=Path(P['default_new'])
original_children=list(base.iterdir())
mirror=Path(P['default_mirror'])
mirror.mkdir()
for p in original_children:
    dest=extra.get(str(p),str(p))
    (mirror/p.name).symlink_to(dest,target_is_directory=p.is_dir())
old=Path(P['default_old'])
assert old.is_symlink() and old.resolve()==base
q=old.with_name(old.name+'.layout-new')
q.symlink_to(mirror,target_is_directory=True)
q.replace(old)
for m in P['default_files']:
    layout.move(m['source'],m['destination'],alias=False)
# The complete frozen ATM source package is on another filesystem.
src,dst=Path(P['dataset_old']),Path(P['dataset_new'])
assert src.is_dir() and not src.is_symlink() and not dst.exists()
assert shutil.disk_usage(dst.parent).free>5*1024**3
before=layout.metadata([src])
for proc in Path('/proc').iterdir():
    if not proc.name.isdigit():continue
    try:cwd=os.readlink(proc/'cwd')
    except OSError:continue
    assert not cwd.startswith(str(src)+'/')
dst.mkdir()
layout.run(['rsync','-aH',str(src)+'/',str(dst)+'/'],timeout=600)
delta=layout.run(['rsync','-aHnci','--delete',str(src)+'/',str(dst)+'/'],timeout=600)
assert not delta.strip(),delta[:1000]
assert before==layout.metadata([src]),'Source changed during dataset copy'
layout.save('datasets-content-verification.json',{'file_count':len(before),'rsync_checksum_differences':delta,'source_metadata_unchanged':True,'source':str(src),'destination':str(dst)})
tmp=src.with_name('.sentrix-atm-sources-layout-verified-copy')
assert not tmp.exists() and not tmp.is_symlink()
tmp.symlink_to(dst,target_is_directory=True)
layout.exchange(src,tmp)
shutil.rmtree(tmp)
# Update current configuration paths, including systemd log outputs.
layout.MAPPING.update(extra)
layout.PATTERN=re.compile('|'.join(re.escape(x) for x in sorted(layout.MAPPING,key=len,reverse=True)))
backups=R/'config-backups'
rewritten=[]
for p in layout.config_paths():layout.rewrite(p,backups,rewritten)
# Rewrite canonical paths too (the earlier inventory paths still resolve via mirrors).
for root in [Path('/home/realmagic/xy/deploy'),Path('/home/realmagic/.config/systemd/user')]:
    for p in root.rglob('*'):
        if p.is_file() and not p.is_symlink() and p.suffix in {'.env','.conf','.service'} and p.stat().st_size<200000:
            layout.rewrite(p,backups,rewritten)
for p in (Path(layout.PLAN['atm'])/'datasets').iterdir():
    if p.is_symlink():
        target=os.readlink(p);new=layout.translate(target)
        if target!=new:
            q=p.with_name(p.name+'.layout-new');q.symlink_to(new);q.replace(p)
# Remove two obsolete names from the canonical ATM root, retaining them in its mirror.
atm=Path(layout.PLAN['atm']);mirror_atm=Path('/home/realmagic/xy/.legacy/atm-bench')
mirror_atm.mkdir()
for p in list(atm.iterdir()):
    dest=str(p.resolve()) if p.name in {'qwen37-quota-incident-20261113','doubao-text-index-20261113.db'} else str(p)
    (mirror_atm/p.name).symlink_to(dest,target_is_directory=p.is_dir())
for name in ['qwen37-quota-incident-20261113','doubao-text-index-20261113.db']:
    assert (atm/name).is_symlink();(atm/name).unlink()
old_atm=Path('/home/realmagic/xy/atm-bench-20261110')
q=old_atm.with_name(old_atm.name+'.layout-new');q.symlink_to(mirror_atm,target_is_directory=True);q.replace(old_atm)
# Archive the original environment guide unchanged, then publish current navigation.
original=atm/'README.md';archive=atm/'audit/initial-environment-20261110.md'
assert not archive.exists();original.rename(archive)
for name,target in {'xy.md':Path('/home/realmagic/xy/README.md'),'benchmarks.md':atm.parent/'README.md','atm.md':atm/'README.md','photobench.md':Path(layout.PLAN['photobench'])/'README.md'}.items():
    assert not target.exists();shutil.copyfile(HERE/'readmes'/name,target)
shutil.copyfile(HERE/'server100-directory-layout.md',R/'server100-directory-layout.md')
layout.save('finish-path-map.json',extra)
layout.save('finish-rewritten-configs.json',rewritten)
layout.run(['systemctl','--user','daemon-reload'])
layout.save('finish-complete.json',{'default_files':len(P['default_files']),'dataset_file_count':len(before),'configuration_edits':len(rewritten)})
print(json.dumps({'default_files':len(P['default_files']),'dataset_file_count':len(before),'configuration_edits':len(rewritten)}))
