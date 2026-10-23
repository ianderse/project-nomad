import hashlib
import base64
import gzip
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('windows_backup', Path(__file__).parents[1] / 'scripts/windows_backup.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class LocalStream(module.WindowsBackup):
    payload = bytes(range(256)) * 17 + b'\x00\r\n\xff'

    def command(self, script):
        return [sys.executable, '-c', 'import sys;sys.stdout.buffer.write(' + repr(self.payload) + ')']


class WindowsBackupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for name in ['tmp', 'scripts']:
            (self.root / name).mkdir()
        (self.root / 'scripts/windows_uncached_reader.ps1').write_text('# fixture')
        self.remote = LocalStream(self.root, {'host': 'PC', 'user': 'backup', 'remote_path': 'G:\\Backups'})

    def tearDown(self):
        self.tmp.cleanup()

    def test_binary_stream_is_preserved_and_verified(self):
        target = self.root / 'files.tar'
        expected = hashlib.sha256(self.remote.payload).hexdigest()
        self.assertEqual(self.remote.download('20261023T183840Z-ae53f7', 'files.tar', target, expected), expected)
        self.assertEqual(target.read_bytes(), self.remote.payload)

    def test_bad_transfer_cannot_replace_existing_data(self):
        target = self.root / 'files.tar'
        target.write_bytes(b'preserve this file')
        with self.assertRaisesRegex(RuntimeError, 'checksum mismatch'):
            self.remote.download('20261023T183840Z-ae53f7', 'files.tar', target, '0' * 64)
        self.assertEqual(target.read_bytes(), b'preserve this file')
        self.assertTrue((self.root / 'files.tar.transferring').exists())

    def test_rejects_path_traversal_before_retrieving_files(self):
        for name in ['../outside', 'G:\\outside', '\\outside', '..']:
            with self.assertRaises(RuntimeError):
                self.remote.remote_path('20261023T183840Z-ae53f7', name)
        with self.assertRaises(RuntimeError):
            self.remote.remote_path('..', 'files.tar')
        self.assertEqual(module.ps_quote("G:\\Bob's Backups"), "'G:\\Bob''s Backups'")

    def test_large_helper_fits_windows_command_limit_without_losing_source(self):
        remote = module.WindowsBackup(self.root, {'host': 'PC', 'user': 'backup', 'remote_path': 'G:\\Backups'})
        source = (Path(__file__).parents[1] / 'scripts/windows_uncached_reader.ps1').read_text()
        with patch.object(remote, 'connect'):
            command = remote.command(source)[-1]
        self.assertLess(len(command), 8191)
        launcher = base64.b64decode(command.split()[-1]).decode('utf-16le')
        packed = launcher.split("FromBase64String('")[1].split("')")[0]
        self.assertTrue(gzip.decompress(base64.b64decode(packed)).decode().endswith(source))

    def test_verified_staged_backup_is_published_without_duplicate_full_read(self):
        local = self.root / '20261023T183840Z-ae53f7'
        local.mkdir()
        (local / 'files.tar').write_bytes(b'archive')
        (local / 'manifest.json').write_text(json.dumps({'sha256': {'files.tar': module.digest(local / 'files.tar')}}))
        rows = {name: {'sha256': module.digest(local / name), 'bytes': (local / name).stat().st_size}
                for name in ['files.tar', 'manifest.json']}
        with patch.object(self.remote, 'hashes', side_effect=[{}, rows]) as hashes, \
             patch.object(self.remote, 'upload') as upload, \
             patch.object(self.remote, 'verify') as verify, \
             patch.object(self.remote, 'run') as run:
            self.assertEqual(self.remote.mirror(local), local.name)
        self.assertEqual(hashes.call_count, 2)
        upload.assert_not_called()
        verify.assert_not_called()
        self.assertIn('[IO.Directory]::Move', run.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
