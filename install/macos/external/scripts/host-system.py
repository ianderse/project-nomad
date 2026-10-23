"""Capture static Mac hardware separately from live Linux VM metrics."""
import datetime
import json
import os
from pathlib import Path
import platform
import subprocess

root = Path(os.environ.get('NOMAD_ROOT', Path(__file__).resolve().parents[1]))
def value(*command):
    return subprocess.check_output(command, text=True).strip()

snapshot = {
    'capturedAt': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    'os': {'platform': 'darwin', 'distro': 'macOS ' + value('/usr/bin/sw_vers', '-productVersion'),
           'kernel': platform.release(), 'arch': platform.machine(), 'hostname': platform.node()},
    'cpu': {'manufacturer': 'Apple', 'brand': value('/usr/sbin/sysctl', '-n', 'machdep.cpu.brand_string'),
            'cores': int(value('/usr/sbin/sysctl', '-n', 'hw.logicalcpu')),
            'physicalCores': int(value('/usr/sbin/sysctl', '-n', 'hw.physicalcpu'))},
    'memoryTotal': int(value('/usr/sbin/sysctl', '-n', 'hw.memsize')),
}
destination = root / 'storage/host-system.json'
temporary = destination.with_suffix('.tmp')
temporary.write_text(json.dumps(snapshot, indent=2) + '\n')
temporary.replace(destination)
