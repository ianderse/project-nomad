import importlib.util
import json
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import patch

PROFILE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('bootstrap', PROFILE / 'bootstrap.py')
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class BootstrapTest(unittest.TestCase):
    def test_rejects_long_vm_socket_paths_before_provisioning(self):
        bootstrap.check_socket_path(Path('/Volumes/SSD/nomad'))
        with self.assertRaisesRegex(RuntimeError, 'too long'):
            bootstrap.check_socket_path(Path('/Volumes/SSD') / ('long-folder-' * 10))

    def test_refuses_existing_data_before_modifying_anything(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = b'APP_KEY=preserve-me\n'
            (root / '.env').write_bytes(original)
            with self.assertRaisesRegex(RuntimeError, 'not empty'):
                bootstrap.fresh_root(root)
            self.assertEqual((root / '.env').read_bytes(), original)
            self.assertEqual([p.name for p in root.iterdir()], ['.env'])

    def test_rejects_internal_and_non_apfs_drives(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / 'new-install'
            info = {'Internal': False, 'FilesystemType': 'apfs',
                    'VolumeUUID': 'test-uuid', 'MountPoint': str(root.parent)}
            with patch.object(bootstrap.os.path, 'ismount', side_effect=lambda p: Path(p) == root.parent):
                for change in [{'Internal': True}, {'FilesystemType': 'exfat'}, {'VolumeUUID': ''}]:
                    with patch.object(bootstrap, 'run', return_value=plistlib.dumps(dict(info, **change))):
                        with self.assertRaises(RuntimeError):
                            bootstrap.volume_for(root)
                with patch.object(bootstrap, 'run', return_value=plistlib.dumps(info)) as diskutil:
                    self.assertEqual(bootstrap.volume_for(root)['uuid'], 'test-uuid')
                    self.assertEqual(Path(diskutil.call_args.args[0][-1]), root.parent)
            self.assertFalse(root.exists())

    def test_configuration_keeps_credentials_as_environment_references(self):
        root = Path('/Volumes/Test Drive/a "quoted" folder')
        config = bootstrap.configuration(root, root / 'releases/abc/source', 'example@sha256:abc')
        self.assertEqual(config['services']['admin']['environment']['APP_KEY'], '${APP_KEY}')
        self.assertEqual(config['services']['admin']['volumes'][0], str(root) + '/storage:/app/storage')
        self.assertEqual(config['services']['admin']['image'], 'example@sha256:abc')
        self.assertTrue(config['volumes']['qdrant-data']['external'])
        self.assertNotIn(':/qdrant/storage', '\n'.join(config['services']['admin']['volumes']))
        self.assertEqual(json.loads(json.dumps(config)), config)

    def test_download_rejects_wrong_hash_and_leaves_no_completed_tool(self):
        from io import BytesIO
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            tool = {'file': 'colima', 'url': 'https://example.invalid/tool', 'sha256': '0' * 64}
            with patch.object(bootstrap.urllib.request, 'urlopen', return_value=BytesIO(b'bad download')):
                with self.assertRaisesRegex(RuntimeError, 'checksum mismatch'):
                    bootstrap.fetch(tool, folder)
            self.assertFalse((folder / 'colima').exists())


if __name__ == '__main__':
    unittest.main()
