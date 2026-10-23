"""Stage this fork's frontend fixes for controlled updates of prebuilt app images."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--root', type=Path, required=True)
parser.add_argument('--source-repo', type=Path, required=True)
args = parser.parse_args()
files = [
    'inertia/app/app.tsx',
    'inertia/components/AppManageMenu.tsx',
    'inertia/components/StyledButton.tsx',
    'inertia/components/SupplyDepotCard.tsx',
    'inertia/css/app.css',
    'inertia/pages/settings/system.tsx',
    'types/system.ts',
    'vite.config.ts',
]
if not (args.root / 'compose.json').is_file():
    raise SystemExit('Select an existing external-profile deployment root')
subprocess.run([sys.executable, str(args.root / 'scripts/drive-guard.py')],
               env={**os.environ, 'NOMAD_ROOT': str(args.root)}, check=True)
for name in files:
    source = args.source_repo / 'admin' / name
    if not source.is_file():
        raise SystemExit('Missing frontend source: ' + str(source))
for name in files:
    destination = args.root / 'scripts/frontend-overrides' / name
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(args.source_repo / 'admin' / name, destination)
print('Captured frontend fixes; build/test them before recreating the web service')
