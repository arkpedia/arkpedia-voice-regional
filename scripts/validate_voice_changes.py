#!/usr/bin/env python3
"""Decode changed MP3 blobs from Git without checking out the whole voice archive."""
import argparse
import subprocess

def git(*args):
    return subprocess.check_output(['git', *args])

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', required=True)
    args = parser.parse_args()
    changed = git('diff', '--name-only', '--diff-filter=ACM', '-z', args.base, 'HEAD').decode().split('\0')
    checked = 0
    for path in changed:
        if not path.endswith('.mp3'):
            continue
        data = git('show', f'HEAD:{path}')
        if not 0 < len(data) < 100 * 1024 * 1024:
            raise ValueError(f'Empty or oversized audio: {path}')
        subprocess.run(['ffmpeg', '-v', 'error', '-xerror', '-i', 'pipe:0', '-f', 'null', '-'], input=data, check=True, timeout=60)
        checked += 1
    print(f'Decoded {checked} added/modified voice files.')

if __name__ == '__main__':
    main()
