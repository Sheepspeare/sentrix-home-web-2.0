"""Inspect current-build warning assets without modifying the live database."""

import json
import re
import sqlite3
from collections import Counter
from pathlib import Path

from probe_finalization import DATABASE, SCOPE

LOG = Path('/home/realmagic/xy/benchmarks/atm-bench/instances/doubao-legacy/logs/api.log')


def main():
    warnings = []
    active = False
    with LOG.open(errors='replace') as stream:
        for line in stream:
            if 'Started server process [2427489]' in line:
                active = True
            if active and any(text in line for text in ['视频场景抽取失败', '视觉调用失败', '事件总结的模型调用失败']):
                warnings.append(line.strip()[:1500])
    failed = sorted(set(re.findall(r'asset=(asset_\w+)', '\n'.join(warnings))))
    with sqlite3.connect(f'{DATABASE.as_uri()}?mode=ro', uri=True, timeout=5) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA query_only=ON')
        counts = [dict(r) for r in connection.execute(
            'SELECT a.id,a.file_name,a.path,a.metadata_json,a.status,'
            '(SELECT count(*) FROM assets d WHERE d.parent_asset_id=a.id) AS derived_count,'
            '(SELECT count(*) FROM events e WHERE e.source_asset_id=a.id) AS scene_count '
            'FROM assets a WHERE a.scope_id=? AND a.media_type=? AND a.parent_asset_id IS NULL',
            (SCOPE, 'video'),
        )]
        jobs = [dict(r) for r in connection.execute(
            'SELECT j.asset_id,j.status,j.current_stage,j.error_code,j.error_message '
            'FROM video_jobs j JOIN assets a ON j.asset_id=a.id WHERE a.scope_id=?', (SCOPE,)
        )]
        stages = [dict(r) for r in connection.execute(
            'SELECT s.job_id,s.stage,s.status,s.output_artifact,s.error_message '
            'FROM video_job_stages s JOIN video_jobs j ON s.job_id=j.job_id '
            'JOIN assets a ON j.asset_id=a.id WHERE a.scope_id=? AND s.status<>?', (SCOPE, 'COMPLETED'),
        )]
    failed_rows = [r for r in counts if r['id'] in failed]
    print(json.dumps({
        'scope_id': SCOPE, 'product_pid_log_boundary': 2427489,
        'warnings': warnings, 'failed_video_ids': failed,
        'source_video_count': len(counts),
        'video_coverage': dict(Counter('has_scene' if r['scene_count'] else 'no_scene' for r in counts)),
        'failed_video_coverage': dict(Counter('has_scene' if r['scene_count'] else 'no_scene' for r in failed_rows)),
        'failed_video_assets': failed_rows, 'jobs': jobs, 'noncompleted_job_stages': stages,
    }, ensure_ascii=False))


if __name__ == '__main__':
    main()
