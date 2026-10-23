#!/usr/bin/python3
"""Supply a saved Windows credential only when invoked as an SSH askpass helper."""
import os
import subprocess
import sys

if len(sys.argv) < 2 or 'password' not in sys.argv[1].lower():
    raise SystemExit(1)
result = subprocess.run(['/usr/bin/security', 'find-internet-password',
    '-s', os.environ['NOMAD_SSH_SERVER'], '-a', os.environ['NOMAD_SSH_USER'], '-w'],
    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
if result.returncode:
    raise SystemExit(1)
sys.stdout.buffer.write(result.stdout.rstrip(b'\r\n') + b'\n')
