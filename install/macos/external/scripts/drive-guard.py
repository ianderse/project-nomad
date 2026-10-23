"""Refuse storage operations unless the configured external APFS volume is mounted."""
import json
import os
from pathlib import Path
import plistlib
import subprocess

root = Path(os.environ.get('NOMAD_ROOT', Path(__file__).resolve().parents[1]))
settings = json.loads((root / 'state/storage.json').read_text())
volume = settings['mount']
if not os.path.ismount(volume):
    raise SystemExit('The configured external drive is not mounted. NOMAD will not start.')
info = plistlib.loads(subprocess.check_output(['/usr/sbin/diskutil', 'info', '-plist', volume]))
if not settings.get('uuid') or info.get('VolumeUUID') != settings['uuid'] or info.get('Internal'):
    raise SystemExit('The mounted volume is not the configured external drive. NOMAD will not start.')
