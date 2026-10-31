import json
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class BoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); cls.port = sock.getsockname()[1]
        cls.base = f'http://127.0.0.1:{cls.port}'
        cls.proc = subprocess.Popen([sys.executable, 'app.py', '--port', str(cls.port), '--data-dir', cls.tmp.name], stdout=subprocess.DEVNULL)
        for _ in range(50):
            try:
                with urlopen(cls.base + '/api/state', timeout=1):
                    return
            except OSError:
                time.sleep(.05)
        raise RuntimeError('Test server did not start.')

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate(); cls.proc.wait(timeout=5); cls.tmp.cleanup()

    def call(self, path, body=None, headers=None):
        data = None if body is None else json.dumps(body).encode()
        req = Request(self.base + path, data=data, headers=headers or {})
        with urlopen(req, timeout=5) as response:
            return json.loads(response.read())

    def test_origin_host_and_write_header(self):
        for headers in ({'Host': 'attacker.test'}, {'Origin': 'https://attacker.test'}):
            with self.assertRaises(HTTPError) as error:
                self.call('/api/state', headers=headers)
            self.assertEqual(error.exception.code, 403)
            error.exception.close()
        with self.assertRaises(HTTPError) as error:
            self.call('/api/import', {}, {'Content-Type': 'application/json'})
        self.assertEqual(error.exception.code, 403)
        error.exception.close()

    def test_real_import_settings_search_and_validation(self):
        headers = {'Content-Type': 'application/json', 'X-Local-Request': '1', 'Origin': self.base}
        self.assertEqual(self.call('/api/import', {}, headers)['changed'], 2)
        self.assertEqual(self.call('/api/import', {}, headers)['changed'], 0)
        self.assertEqual(len(self.call('/api/search?q=scope&scope=sample')), 2)
        self.assertEqual(self.call('/api/settings', {'scope': 'sandbox'}, headers)['scope'], 'sandbox')
        with self.assertRaises(HTTPError) as error:
            self.call('/api/import', {'sources': [{'id': 1.5}]}, headers)
        self.assertEqual(error.exception.code, 400)
        error.exception.close()


if __name__ == '__main__':
    unittest.main()
