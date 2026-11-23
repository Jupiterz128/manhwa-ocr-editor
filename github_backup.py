"""Preflight the working files and staged blobs before backing up the project."""
import datetime
import os
from pathlib import Path
import re
import subprocess
import sys

MAX_BYTES = 50_000_000
PATTERNS = [
    rb'AIza[0-9A-Za-z_-]{35}', rb'gh[pousr]_[0-9A-Za-z]{24,}',
    rb'github_pat_[0-9A-Za-z_]{30,}', rb'AKIA[A-Z0-9]{16}',
    rb'-----BEGIN (?:[A-Z0-9 ]* )?PRIVATE KEY-----',
    rb'"type"\s*:\s*"service_account"', rb'"private_key(?:_id)?"\s*:\s*"[^"]+',
    rb'(?:api[_-]?key|access[_-]?token|client[_-]?secret|password|roboflow[_-]?key)["\x27]?\s*[:=]\s*["\x27]([A-Za-z0-9_/+=.-]{12,})["\x27]',
    rb'(?:api_key|access_token|key)=([A-Za-z0-9_-]{16,})(?:[&\s"\x27]|$)',
]
SECRET = re.compile(b'|'.join(PATTERNS), re.I)


def git(*args):
    return subprocess.check_output(['git', *args])


def inspect(name, size, read):
    if size >= MAX_BYTES:
        raise ValueError('File too large: ' + name)
    data = read()
    if SECRET.search(data) or SECRET.search(data.replace(b'\0', b'')):
        raise ValueError('Possible credential (value hidden): ' + name)


def audit(staged=False):
    if git('ls-files', '-ci', '--exclude-standard', '-z'):
        raise ValueError('Ignored files are already tracked. Check git ls-files -ci --exclude-standard.')
    total = count = 0
    if staged:
        items = git('ls-files', '--stage', '-z').split(b'\0')
    else:
        items = git('ls-files', '-co', '--exclude-standard', '-z').split(b'\0')
    for item in filter(None, items):
        if staged:
            meta, raw = item.split(b'\t', 1)
            mode, oid, stage = meta.decode().split()
            name = os.fsdecode(raw)
            if mode not in ('100644', '100755') or stage != '0':
                raise ValueError('Unsupported index entry: ' + name)
            size = int(git('cat-file', '-s', oid))
            inspect(name, size, lambda: git('cat-file', 'blob', oid))
        else:
            name = os.fsdecode(item)
            path = Path(name)
            if not path.exists():
                continue
            if path.is_symlink() or not path.is_file():
                raise ValueError('Unsupported file: ' + name)
            size = path.stat().st_size
            inspect(name, size, path.read_bytes)
        total += size
        count += 1
    print(f'Checked {count} files, {total:,} bytes.')


def backup():
    subprocess.run(['git', 'status', '-s'], check=True)
    message = input('Commit message (Enter for timestamp): ').strip()
    audit()
    git('add', '.')
    audit(staged=True)
    changed = subprocess.run(['git', 'diff', '--cached', '--quiet']).returncode
    if changed == 1:
        git('commit', '-m', message or datetime.datetime.now().strftime('Update %Y-%m-%d %H:%M'))
    elif changed:
        raise RuntimeError('Could not inspect staged changes.')
    subprocess.run(['git', 'push'], check=True)


if __name__ == '__main__':
    try:
        if '--backup' in sys.argv:
            backup()
        else:
            audit('--staged' in sys.argv)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        sys.exit(str(exc))
