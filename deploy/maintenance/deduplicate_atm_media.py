"""Replace audited duplicate original media with links to the canonical dataset."""

import argparse
import hashlib
import json
import os
import stat
import time
from pathlib import Path

BASE = Path('/home/realmagic/xy/benchmarks/atm-bench')
SOURCE = BASE / 'datasets/sources/2026.11.17-atm-sources-addresses'
AUDIT = Path('/var/tmp/realmagic-cleanup-20261127')


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def identity(path):
    s = path.lstat()
    assert stat.S_ISREG(s.st_mode), str(path)
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns


def apply(manifest):
    rows = json.loads(manifest.read_text())['matches']
    targets = {r['path'] for r in rows}
    assert len(targets) == len(rows)
    inode_ids = {(r['dev'], r['inode']) for r in rows}
    # Refuse to replace any media currently opened for writing.
    for process in Path('/proc').iterdir():
        if not process.name.isdigit():
            continue
        try:
            fds = list((process / 'fd').iterdir())
        except (FileNotFoundError, PermissionError):
            continue
        for fd in fds:
            try:
                s = fd.stat()
                if (s.st_dev, s.st_ino) not in inode_ids:
                    continue
                info = (process / 'fdinfo' / fd.name).read_text()
                flags = int(next(v.split()[1] for v in info.splitlines() if v.startswith('flags:')), 8)
                assert flags & os.O_ACCMODE == os.O_RDONLY, str(fd)
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue
    verified_targets = set()
    verified_sources = set()
    for r in rows:
        p, source = Path(r['path']), Path(r['source'])
        assert p.is_relative_to(BASE) and '/media/' in str(p)
        assert source.is_relative_to(SOURCE) and not source.is_symlink()
        assert str(source.resolve()) not in targets
        expected = (r['dev'], r['inode'], r['size'], r['mtime_ns'])
        assert identity(p) == expected, str(p)
        if expected not in verified_targets:
            assert digest(p) == r['sha256'], str(p)
            verified_targets.add(expected)
        if str(source) not in verified_sources:
            assert source.stat().st_size == r['size'] and digest(source) == r['sha256'], str(source)
            verified_sources.add(str(source))
    before = os.statvfs(BASE)
    receipt = AUDIT / 'media-replacement-journal.jsonl'
    replaced = 0
    with receipt.open('x') as journal:
        for r in rows:
            p, source = Path(r['path']), Path(r['source'])
            assert identity(p) == (r['dev'], r['inode'], r['size'], r['mtime_ns']), str(p)
            temporary = p.with_name(p.name + '.dedup-link-20261127')
            assert not temporary.exists() and not temporary.is_symlink()
            os.symlink(source, temporary)
            journal.write(json.dumps(r) + '\n')
            journal.flush()
            os.fsync(journal.fileno())
            os.replace(temporary, p)
            assert p.is_symlink() and p.resolve() == source and p.stat().st_size == r['size']
            replaced += 1
    after = os.statvfs(BASE)
    result = {'replaced_paths': replaced, 'unique_sources': len(verified_sources),
              'available_bytes_before': before.f_bavail * before.f_frsize,
              'available_bytes_after': after.f_bavail * after.f_frsize, 'at': time.time()}
    (AUDIT / 'media-dedup-completed.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--manifest', type=Path, required=True)
    apply(parser.parse_args().manifest)
