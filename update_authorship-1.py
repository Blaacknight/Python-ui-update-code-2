#!/usr/bin/env python3
"""
update_authorship.py

Recomputes per-file linemaps (and ai/human line totals) in an authorship
JSON file after a GitHub-UI edit, by diffing two git revisions
(default: HEAD~1 -> HEAD). Every line touched by the diff is attributed
to "human". Lines untouched by the diff keep their original author and
are shifted to their new line numbers.

ASSUMPTIONS (tell me if any of these are wrong and I'll adjust):
  - Lines are 1-indexed, inclusive (start/end both included in the range).
  - Each file's linemap fully covers the file with no gaps, from line 1
    to the last line, e.g. [{1,10,human},{11,40,ai}] for a 40-line file.
  - JSON shape:
      {
        "version": ...,
        "username": ...,
        "displayname": ...,
        "hostname": ...,
        "totals": {"aiLines": int, "humanLines": int, ...other fields untouched...},
        "files": [
          {
            "path": "src/foo.py",
            "aiLines": int,
            "humanLines": int,
            ...other fields untouched...
            "linemap": [{"start": 1, "end": 10, "author": "human"}, ...]
          }
        ]
      }
  - Only "aiLines"/"humanLines"/"linemap" and totals.aiLines/humanLines are
    written. Char counts and percentages are left completely untouched.

USAGE (e.g. from a GitHub Actions workflow step):
    python update_authorship.py authorship.json \
        --repo "$GITHUB_WORKSPACE" \
        --base HEAD~1 \
        --head HEAD

  This overwrites authorship.json in place. Pass --output to write
  somewhere else instead.
"""

import argparse
import json
import re
import subprocess
import sys

HUNK_RE = re.compile(r'^@@ -(\d+)(?:,(\d+))?\s\+(\d+)(?:,(\d+))?\s@@')


# --------------------------------------------------------------------------
# Git helpers
# --------------------------------------------------------------------------

def run_git(args, repo):
    result = subprocess.run(
        ['git', '-C', repo] + args,
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def get_name_status(repo, base, head):
    """Returns list of (status_letter, path_a, path_b_or_None)."""
    out = run_git(['diff', '--name-status', '-M', base, head], repo)
    entries = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split('\t')
        status = parts[0]
        if status.startswith('R') or status.startswith('C'):
            entries.append((status[0], parts[1], parts[2]))
        else:
            entries.append((status[0], parts[1], None))
    return entries


def get_diff_hunks(repo, base, head):
    """Returns dict: new_path -> list of (old_start, old_count, new_start, new_count)."""
    out = run_git(['diff', '-U0', '--no-color', base, head], repo)
    hunks_by_path = {}
    current_path = None
    is_binary = False

    for line in out.splitlines():
        if line.startswith('diff --git '):
            current_path = None
            is_binary = False
        elif line.startswith('Binary files'):
            is_binary = True
        elif line.startswith('+++ '):
            raw = line[4:].strip()
            if raw == '/dev/null':
                current_path = None
            else:
                current_path = raw[2:] if raw.startswith('b/') else raw
                hunks_by_path.setdefault(current_path, [])
        elif line.startswith('@@'):
            if is_binary or current_path is None:
                continue
            m = HUNK_RE.match(line)
            if m:
                old_start = int(m.group(1))
                old_count = int(m.group(2)) if m.group(2) is not None else 1
                new_start = int(m.group(3))
                new_count = int(m.group(4)) if m.group(4) is not None else 1
                hunks_by_path[current_path].append((old_start, old_count, new_start, new_count))

    return hunks_by_path


def count_lines_at_rev(repo, rev, path):
    try:
        content = run_git(['show', f'{rev}:{path}'], repo)
    except RuntimeError:
        return 0
    if content == '':
        return 0
    parts = content.split('\n')
    if parts and parts[-1] == '':
        parts = parts[:-1]
    return len(parts)


# --------------------------------------------------------------------------
# Linemap math
# --------------------------------------------------------------------------

def clip_linemap(linemap, lo, hi):
    """Returns the portion of linemap covering [lo, hi], clipped to that range."""
    result = []
    for seg in linemap:
        s, e = seg['start'], seg['end']
        cs, ce = max(s, lo), min(e, hi)
        if cs <= ce:
            result.append({'start': cs, 'end': ce, 'author': seg['author']})
    return sorted(result, key=lambda x: x['start'])


def apply_hunks_to_linemap(old_linemap, hunks, old_total_lines):
    """
    Walks the old linemap + the diff hunks (old file -> new file) and
    produces the new linemap. Lines added by the diff become 'human'.
    Lines removed are simply dropped. Untouched lines keep their author
    and get shifted to their new line numbers.
    """
    new_segments = []
    old_pointer = 1
    new_pointer = 1

    for old_start, old_count, new_start, new_count in sorted(hunks, key=lambda h: h[0]):
        # last old line still "unchanged" before this hunk starts
        unchanged_end = old_start if old_count == 0 else old_start - 1

        if unchanged_end >= old_pointer:
            offset = new_pointer - old_pointer
            for seg in clip_linemap(old_linemap, old_pointer, unchanged_end):
                new_segments.append({
                    'start': seg['start'] + offset,
                    'end': seg['end'] + offset,
                    'author': seg['author'],
                })
            new_pointer += (unchanged_end - old_pointer + 1)
            old_pointer = unchanged_end + 1
        else:
            old_pointer = max(old_pointer, unchanged_end + 1)

        if old_count > 0:
            old_pointer += old_count  # deleted old lines: drop their authorship

        if new_count > 0:
            new_segments.append({
                'start': new_start,
                'end': new_start + new_count - 1,
                'author': 'human',
            })
            new_pointer = new_start + new_count

    # flush whatever is left unchanged after the last hunk
    if old_total_lines >= old_pointer:
        offset = new_pointer - old_pointer
        for seg in clip_linemap(old_linemap, old_pointer, old_total_lines):
            new_segments.append({
                'start': seg['start'] + offset,
                'end': seg['end'] + offset,
                'author': seg['author'],
            })

    return sorted(new_segments, key=lambda x: x['start'])


def merge_adjacent(linemap):
    if not linemap:
        return []
    segs = sorted(linemap, key=lambda x: x['start'])
    merged = [dict(segs[0])]
    for seg in segs[1:]:
        last = merged[-1]
        if seg['author'] == last['author'] and seg['start'] == last['end'] + 1:
            last['end'] = seg['end']
        else:
            merged.append(dict(seg))
    return merged


def count_lines_by_author(linemap):
    ai = human = 0
    for seg in linemap:
        n = seg['end'] - seg['start'] + 1
        if seg['author'] == 'ai':
            ai += n
        else:
            human += n
    return ai, human


def full_human_linemap(total_lines):
    if total_lines <= 0:
        return []
    return [{'start': 1, 'end': total_lines, 'author': 'human'}]


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description='Update authorship linemaps after a GitHub UI edit.')
    parser.add_argument('authorship_file', help='Path to the authorship JSON file')
    parser.add_argument('--repo', default='.', help='Path to the git repo (default: current dir)')
    parser.add_argument('--base', default='HEAD~1', help='Base revision (default: HEAD~1)')
    parser.add_argument('--head', default='HEAD', help='Head revision (default: HEAD)')
    parser.add_argument('--output', default=None, help='Output path (default: overwrite authorship_file)')
    args = parser.parse_args()

    with open(args.authorship_file, 'r') as f:
        data = json.load(f)

    files_by_path = {f['path']: f for f in data.get('files', [])}

    name_status = get_name_status(args.repo, args.base, args.head)
    hunks_by_path = get_diff_hunks(args.repo, args.base, args.head)

    for status, path_a, path_b in name_status:
        if status == 'A':
            new_path = path_a
            total = count_lines_at_rev(args.repo, args.head, new_path)
            files_by_path[new_path] = {
                'path': new_path,
                'aiLines': 0,
                'humanLines': total,
                'linemap': full_human_linemap(total),
            }

        elif status == 'D':
            files_by_path.pop(path_a, None)

        elif status == 'R':
            old_path, new_path = path_a, path_b
            entry = files_by_path.pop(old_path, None)
            if entry is None:
                total = count_lines_at_rev(args.repo, args.head, new_path)
                entry = {
                    'path': new_path,
                    'aiLines': 0,
                    'humanLines': total,
                    'linemap': full_human_linemap(total),
                }
            else:
                entry['path'] = new_path
                hunks = hunks_by_path.get(new_path)
                if hunks:
                    old_total = max((s['end'] for s in entry.get('linemap', [])), default=0)
                    new_linemap = apply_hunks_to_linemap(entry.get('linemap', []), hunks, old_total)
                    entry['linemap'] = merge_adjacent(new_linemap)
                ai, human = count_lines_by_author(entry['linemap'])
                entry['aiLines'], entry['humanLines'] = ai, human
            files_by_path[new_path] = entry

        else:
            # Modified (M), type-change (T), copy (C), etc.
            path = path_a
            entry = files_by_path.get(path)
            hunks = hunks_by_path.get(path)

            if entry is None:
                # Tracked by git but missing from the authorship file: add it fresh.
                total = count_lines_at_rev(args.repo, args.head, path)
                files_by_path[path] = {
                    'path': path,
                    'aiLines': 0,
                    'humanLines': total,
                    'linemap': full_human_linemap(total),
                }
                continue

            if not hunks:
                # No textual hunks captured (e.g. binary file) -> leave as-is.
                continue

            old_total = max((s['end'] for s in entry.get('linemap', [])), default=0)
            new_linemap = apply_hunks_to_linemap(entry.get('linemap', []), hunks, old_total)
            entry['linemap'] = merge_adjacent(new_linemap)
            ai, human = count_lines_by_author(entry['linemap'])
            entry['aiLines'], entry['humanLines'] = ai, human

    data['files'] = list(files_by_path.values())

    data.setdefault('totals', {})
    data['totals']['aiLines'] = sum(f.get('aiLines', 0) for f in data['files'])
    data['totals']['humanLines'] = sum(f.get('humanLines', 0) for f in data['files'])

    out_path = args.output or args.authorship_file
    with open(out_path, 'w') as f:
        json.dump(data, f, indent=2)

    print(f"Updated {len(data['files'])} files. "
          f"ai={data['totals']['aiLines']} human={data['totals']['humanLines']}",
          file=sys.stderr)


if __name__ == '__main__':
    main()
