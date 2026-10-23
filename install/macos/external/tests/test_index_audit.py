import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

spec = importlib.util.spec_from_file_location('index_audit', Path(__file__).parents[1] / 'scripts/index_audit.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class AuditTest(unittest.TestCase):
    def setUp(self):
        self.points = [{'id': n, 'vector': [1.0] + [0.0] * 767,
                        'payload': {'text': 'fixture ' + str(n), 'active': n != 2}} for n in [1, 2, 3]]
        self.before = copy.deepcopy(self.points)
        self.requests = []
        test = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                test.requests.append(self.path)
                if self.path.endswith('/count'):
                    clauses = body.get('filter', {}).get('must', [])
                    result = {'count': 3 if not clauses else int(clauses[-1]['match']['value'] is False)}
                elif self.path.endswith('/scroll'):
                    result = {'points': [{'id': p['id']} for p in test.points], 'next_page_offset': None}
                else:
                    if self.path.startswith('/live') and body.get('with_payload') and 2 in body['ids']:
                        self.send_response(500); self.end_headers(); return
                    result = copy.deepcopy([p for p in test.points if p['id'] in body['ids']])
                    if self.path.startswith('/backup'):
                        for point in result:
                            point['payload']['active'] = True
                    if not body.get('with_payload'):
                        for point in result:
                            point.pop('payload', None)
                encoded = json.dumps({'result': result}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(encoded)))
                self.end_headers(); self.wfile.write(encoded)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.temp.cleanup()

    def test_reports_corruption_without_silently_dropping_a_point(self):
        out = Path(self.temp.name)
        result = module.audit(self.base + '/live', [], out)
        self.assertEqual(result, {'expected': 3, 'readable': 2, 'recovered_from_backup': 0, 'unreadable': 1})
        bad = json.loads((out / 'unreadable.jsonl').read_text())
        self.assertEqual(bad['id'], 2)
        self.assertEqual(self.points, self.before)

    def test_backup_recovery_preserves_the_current_search_switch(self):
        out = Path(self.temp.name)
        result = module.audit(self.base + '/live', [self.base + '/backup'], out)
        records = [json.loads(line) for line in (out / 'points.jsonl').read_text().splitlines()]
        self.assertEqual(result['unreadable'], 0)
        self.assertEqual(result['recovered_from_backup'], 1)
        self.assertEqual({p['id'] for p in records}, {1, 2, 3})
        self.assertFalse(next(p for p in records if p['id'] == 2)['payload']['active'])
        self.assertEqual(self.points, self.before)


if __name__ == '__main__':
    unittest.main()
