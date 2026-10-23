"""Audit an isolated Qdrant copy and export readable points without changing it."""
import argparse
import json
import math
from pathlib import Path
import sys
import urllib.error
import urllib.request


def request(base, route, data):
    req = urllib.request.Request(base.rstrip('/') + '/points' + route,
        data=json.dumps(data).encode(), headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=60) as response:
        return json.load(response)['result']


def audit(base, backups, output):
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    expected = request(base, '/count', {'exact': True})['count']
    ids = []
    offset = None
    while True:
        query = {'limit': 1024, 'with_payload': False, 'with_vector': False}
        if offset is not None:
            query['offset'] = offset
        result = request(base, '/scroll', query)
        ids.extend(point['id'] for point in result['points'])
        offset = result.get('next_page_offset')
        if offset is None:
            break
    if len(ids) != expected or len(set(map(str, ids))) != expected:
        raise RuntimeError('Snapshot enumeration differs from its exact point count')
    counts = {'expected': expected, 'readable': 0, 'recovered_from_backup': 0, 'unreadable': 0}

    with (output / 'points.jsonl').open('w') as good, (output / 'unreadable.jsonl').open('w') as bad:
        def accept(point, recovered=False):
            vector = point.get('vector')
            if not isinstance(vector, list) or len(vector) != 768 or not all(
                    isinstance(x, (int, float)) and math.isfinite(x) for x in vector) or not any(vector):
                raise RuntimeError('Invalid vector in snapshot export')
            if not isinstance(point.get('payload'), dict):
                raise RuntimeError('Missing point payload in snapshot export')
            good.write(json.dumps(point, separators=(',', ':')) + '\n')
            counts['recovered_from_backup' if recovered else 'readable'] += 1

        def read(batch):
            try:
                points = request(base, '', {'ids': batch, 'with_payload': True, 'with_vector': True})
                if {str(p['id']) for p in points} != set(map(str, batch)):
                    raise RuntimeError('Snapshot omitted requested point IDs')
                for point in points:
                    accept(point)
                return
            except urllib.error.HTTPError as error:
                if error.code != 500:
                    raise
            if len(batch) > 1:
                middle = len(batch) // 2
                read(batch[:middle]); read(batch[middle:])
                return
            point_id = batch[0]
            vectors = request(base, '', {'ids': batch, 'with_payload': False, 'with_vector': True})
            current = vectors[0] if len(vectors) == 1 else {'id': point_id}
            for backup in backups:
                try:
                    previous = request(backup, '', {'ids': batch, 'with_payload': True, 'with_vector': True})
                    if len(previous) == 1 and previous[0].get('vector') == current.get('vector'):
                        # Preserve the current indexed search switch rather than
                        # blindly restoring its historical backup value.
                        for active in [True, False]:
                            count = request(base, '/count', {'exact': True, 'filter': {'must': [
                                {'has_id': [point_id]}, {'key': 'active', 'match': {'value': active}},
                            ]}})['count']
                            if count:
                                previous[0]['payload']['active'] = active
                                break
                        accept(previous[0], recovered=True)
                        return
                except urllib.error.HTTPError:
                    continue
            bad.write(json.dumps(current, separators=(',', ':')) + '\n')
            counts['unreadable'] += 1

        for start in range(0, len(ids), 256):
            read(ids[start:start + 256])
            if start % 4096 == 0:
                print(json.dumps(counts), file=sys.stderr, flush=True)
    (output / 'audit.json').write_text(json.dumps(counts, indent=2) + '\n')
    print(json.dumps(counts), flush=True)
    return counts


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True, help='Collection URL on an isolated copy')
    parser.add_argument('--backup-url', action='append', default=[])
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.url, args.backup_url, args.output)
    raise SystemExit(1 if result['unreadable'] else 0)
