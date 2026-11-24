"""Read-only final acceptance, with receipts written outside the migrated data."""
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import urllib.parse
import urllib.request
import layout

R=layout.ROOT
mapping=dict(layout.MAPPING)
mapping.update(json.loads((R/'finish-path-map.json').read_text()))
mapping['/home/realmagic/xy/atm-bench-20261110/README.md']=layout.PLAN['atm']+'/audit/initial-environment-20261110.md'
pattern=re.compile('|'.join(re.escape(k) for k in sorted(mapping,key=len,reverse=True)))
def translate(s):return pattern.sub(lambda m:mapping[m.group()],s)
rows=json.loads((R/'file-identities-before.json').read_text())
changes=json.loads((R/'rewritten-configs.json').read_text())+json.loads((R/'finish-rewritten-configs.json').read_text())
allowed={str(Path(v['path']).resolve()) for v in changes}
differences=[];stable=0;mutable=[];manifests={}
for old,expected in rows.items():
    p=Path(translate(old))
    try:s=p.stat()
    except FileNotFoundError:
        if old.endswith(('-wal','-shm')):mutable.append({'old':old,'reason':'database journal closed'});continue
        differences.append({'old':old,'new':str(p),'reason':'missing'});continue
    actual=[s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns]
    if expected==actual:stable+=1
    elif str(p.resolve()) in allowed or p.suffix=='.log' or p.name.endswith(('.db','-wal','-shm')) or '/.git/' in str(p) or '/.process-diagnostics/' in str(p) or p.name=='.photobench-instance.lock':
        mutable.append({'old':old,'new':str(p),'reason':'configuration, runtime database, log or lock'})
    else:differences.append({'old':old,'new':str(p),'before':expected,'after':actual})
    if p.name=='split-commit.json':manifests[str(p)]=hashlib.sha256(p.read_bytes()).hexdigest()
# The status counts must remain the same after the dependency recovery.
idle=layout.idle()
baseline=json.loads((R/'preflight.json').read_text())['idle']['doubao_database_status_counts']
assert idle['doubao_database_status_counts']==baseline
protected=layout.run(['systemctl','--user','show','sentrix-atm100-model.service','sentrix-atm100-api.service','sentrix-atm100-photobench.service','sentrix2-api.service','sentrix2-photobench.service','sentrix-nic-thermal-recorder.service','-p','Id','-p','MainPID'])
assert protected==(R/'protected-pids-before.txt').read_text()
services={}
for name in layout.CLOUD_SERVICES+['sentrix-atm100-wemm.service']:
    raw=layout.run(['systemctl','--user','show',name,'-p','MainPID','-p','ActiveState','-p','NRestarts'])
    services[name]=dict(line.split('=',1) for line in raw.splitlines())
assert all(v['ActiveState']=='active' for v in services.values())
# Live product probe avoids requesting model inference.
products={str(p):layout.get_json(p,'/api/v1/health/live') for p in [8191,8291,8294]}
ports={}
for port in [8772,8872,8874]:
    conf=layout.get_json(port,'/api/v1/config')
    runs=layout.get_json(port,'/api/v1/runs?page=1&page_size=1')
    ms=layout.get_json(port,'/api/v1/manifests')
    ports[str(port)]={'build_revision':conf['build_revision'],'run_total':runs.get('total'),'manifest_response_keys':list(ms),'active_work_count':len(idle['evaluations'][str(port)]['work'])}
# Check the complete ATM package with its normal HTTP media endpoint.
album='2026.11.17-atm-sources-addresses'
manifest=layout.get_json(8874,'/api/v1/manifests/'+album)
photo_name=manifest['photos'][0]
assert isinstance(photo_name,str) and photo_name.startswith('photos/')
media_path='/api/v1/albums/'+urllib.parse.quote(album,safe='')+'/photos/'+urllib.parse.quote(photo_name.removeprefix('photos/'),safe='/')
media={}
for port in [8872,8874]:
    with urllib.request.urlopen(f'http://127.0.0.1:{port}'+media_path,timeout=30) as r:
        data=r.read();media[str(port)]={'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}
assert media['8872']==media['8874']
# Read one existing product asset through the persisted absolute path.
db=Path(layout.PLAN['atm'])/'instances/doubao/product-data/sentrix.db'
with sqlite3.connect(db.as_uri()+'?mode=ro',uri=True) as conn:
    fields=[r[1] for r in conn.execute('PRAGMA table_info(assets)')]
    path_column=next((c for c in ['file_path','path'] if c in fields),None)
    sample=None
    if path_column:
        sample=conn.execute(f'SELECT {path_column} FROM assets WHERE {path_column} IS NOT NULL LIMIT 1').fetchone()
asset=None
if sample:
    p=Path(sample[0]);assert p.is_file()
    asset={'recorded_path':str(p),'resolved_path':str(p.resolve()),'bytes':p.stat().st_size}
report={'time':time.time(),'identity_count':len(rows),'fully_unchanged_files':stable,'explained_mutable_files':mutable,'unexplained_differences':differences,'split_commit_count':len(manifests),'protected_pids_unchanged':True,'doubao_status_counts_unchanged':True,'services':services,'products':products,'ports':ports,'sample_dataset_media':media,'sample_existing_asset':asset,'cloud_copy':json.loads((R/'cloud-content-verification.json').read_text()),'dataset_copy':json.loads((R/'datasets-content-verification.json').read_text()),'rewritten_configurations':len(changes)}
layout.save('acceptance.json',report)
layout.save('split-commit-sha256-after.json',manifests)
layout.save('final-path-map.json',mapping)
print(json.dumps(report,ensure_ascii=False,indent=2))
assert not differences
