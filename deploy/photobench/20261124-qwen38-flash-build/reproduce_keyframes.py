"""Compare timestamp precision on failed videos, writing only temporary JPEGs."""

import argparse
import json
import sqlite3
import subprocess
import tempfile
from pathlib import Path

import cv2

from probe_finalization import DATABASE, SCOPE


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--limit', type=int, default=8)
    args = parser.parse_args()
    media = DATABASE.parent / 'media'
    with sqlite3.connect(f'{DATABASE.as_uri()}?mode=ro', uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            'SELECT a.id,a.path FROM assets a WHERE a.scope_id=? AND a.media_type=? '
            'AND a.parent_asset_id IS NULL AND NOT EXISTS '
            '(SELECT 1 FROM events e WHERE e.source_asset_id=a.id) ORDER BY a.id LIMIT ?',
            (SCOPE, 'video', args.limit),
        ).fetchall()
    results = []
    with tempfile.TemporaryDirectory(prefix='sentrix-keyframe-repro-') as temporary:
        for row in rows:
            analysis = json.loads((media / 'derived/svd_katna' / row['id'] / 'analysis.json').read_text())
            timestamp = analysis['selected_timestamps_sec'][-1]
            capture = cv2.VideoCapture(row['path'])
            decoded_times = []
            while True:
                ok, _ = capture.read()
                if not ok:
                    break
                decoded_times.append(capture.get(cv2.CAP_PROP_POS_MSEC) / 1000)
            capture.release()
            variants = []
            for name, seek in [('milliseconds', f'{timestamp:.3f}'),
                               ('microseconds', f'{timestamp:.6f}'),
                               ('nanoseconds', f'{timestamp:.9f}'),
                               ('decoded_pts', f'{decoded_times[-1]:.9f}')]:
                target = Path(temporary) / f'{row["id"]}-{name}.jpg'
                result = subprocess.run(
                    ['ffmpeg', '-y', '-v', 'error', '-ss', seek, '-i', row['path'],
                     '-frames:v', '1', '-vf', 'scale=iw*sar:ih,setsar=1', '-q:v', '2', str(target)],
                    capture_output=True, text=True, timeout=300,
                )
                variants.append({'name': name, 'seek': seek, 'exit_code': result.returncode,
                                 'output_bytes': target.stat().st_size if target.exists() else 0,
                                 'stderr': result.stderr[-1000:]})
            probe = subprocess.run(
                ['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_frames',
                 '-show_entries', 'frame=best_effort_timestamp_time', '-of', 'json', row['path']],
                capture_output=True, text=True, timeout=300, check=True,
            )
            frames = json.loads(probe.stdout)['frames']
            results.append({'asset_id': row['id'], 'selected_timestamp': timestamp,
                            'sampling': analysis['sampling'], 'variants': variants,
                            'last_decoded_times': decoded_times[-3:],
                            'last_frame_pts': [f.get('best_effort_timestamp_time') for f in frames[-3:]]})
    print(json.dumps({'scope_id': SCOPE, 'database_read_only': True,
                      'temporary_outputs_removed': True, 'results': results}, ensure_ascii=False))


if __name__ == '__main__':
    main()
