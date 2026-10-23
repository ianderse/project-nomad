"""Verify Windows backup files and fetch them over the existing SSH service."""
import base64
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def safe_name(name):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', name):
        raise RuntimeError('Unsafe backup filename')
    return name


class WindowsBackup:
    def __init__(self, root, settings):
        self.root = Path(root)
        self.settings = settings
        self.host, self.user = settings['host'], settings['user']
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.:-]*', self.host):
            raise RuntimeError('Invalid Windows SSH hostname')
        if not self.user or any(c in self.user for c in '\r\n\0'):
            raise RuntimeError('Invalid Windows SSH username')
        self.remote_root = settings['remote_path'].rstrip('\\/')
        if not re.match(r'^[A-Za-z]:[\\/]', self.remote_root):
            raise RuntimeError('Use a Windows drive path for the SSH backup folder, such as G:\\Backups\\Project-NOMAD')
        tag = hashlib.sha256((self.host + '/' + self.user).encode()).hexdigest()[:10]
        self.socket = self.root / 'state' / ('win-ssh-' + tag + '.sock')
        known = str(self.root / 'state/ssh-known-hosts').replace('\\', '\\\\').replace('"', '\\"')
        self.options = ['-T', '-S', str(self.socket), '-o', 'UserKnownHostsFile="' + known + '"',
                        '-o', 'StrictHostKeyChecking=accept-new', '-o', 'ConnectTimeout=10',
                        '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3']

    def connect(self):
        target = ['-l', self.user, self.host]
        ready = subprocess.run(['ssh', '-S', str(self.socket), '-O', 'check', *target],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if ready.returncode == 0:
            return
        env = dict(os.environ)
        env.update(SSH_ASKPASS=str(self.root / 'scripts/ssh-keychain-askpass.py'),
                   SSH_ASKPASS_REQUIRE='force', DISPLAY=env.get('DISPLAY') or ':0',
                   NOMAD_SSH_SERVER=self.host, NOMAD_SSH_USER=self.user)
        log = self.root / 'logs/windows-ssh.log'
        # Real files, rather than PIPEs, keep a background control master from
        # holding a caller's communicate() open after authentication completes.
        with log.open('ab') as errors:
            result = subprocess.run(['ssh', *self.options, '-fN', '-M',
                '-o', 'ControlPersist=600', '-o', 'PreferredAuthentications=password',
                '-o', 'PubkeyAuthentication=no', '-o', 'NumberOfPasswordPrompts=1', *target],
                env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors)
        if result.returncode:
            raise RuntimeError('Windows SSH sign-in failed. Save the backup account password in macOS '
                               'Keychain through Finder, and allow its use by the SSH credential helper. '
                               'Details: ' + str(log))

    def command(self, script):
        self.connect()
        header = ("$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue'; "
                  "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);\n")
        encoded = base64.b64encode((header + script).encode('utf-16le')).decode()
        # Windows OpenSSH invokes cmd.exe, whose command line is limited to
        # 8191 characters. Compress larger helpers before encoding the launcher.
        if len(encoded) > 7000:
            packed = base64.b64encode(gzip.compress((header + script).encode('utf-8'))).decode()
            launcher = ("$bytes=[Convert]::FromBase64String('" + packed + "'); "
                "$stream=[IO.MemoryStream]::new([byte[]]$bytes); "
                "$gzip=[IO.Compression.GZipStream]::new($stream,[IO.Compression.CompressionMode]::Decompress); "
                "$reader=[IO.StreamReader]::new($gzip,[Text.Encoding]::UTF8); "
                "try { & ([ScriptBlock]::Create($reader.ReadToEnd())) } finally { $reader.Dispose() }")
            encoded = base64.b64encode(launcher.encode('utf-16le')).decode()
        if len(encoded) > 7800:
            raise RuntimeError('Windows backup command exceeds the Windows SSH command limit')
        return ['ssh', *self.options, '-o', 'ControlMaster=no', '-o', 'BatchMode=yes',
                '-l', self.user, self.host,
                'powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand ' + encoded]

    def run(self, script):
        result = subprocess.run(self.command(script), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode:
            raise RuntimeError('Windows backup operation failed: ' + result.stderr.decode(errors='replace')[-1600:])
        return result.stdout

    def remote_path(self, backup, filename):
        if not re.fullmatch(r'\d{8}T\d{6}Z-[a-f0-9]{6}(?:\.copying)?', backup):
            raise RuntimeError('Invalid backup identifier')
        return self.remote_root + '\\' + backup + '\\' + safe_name(filename)

    def hashes(self, backup, names):
        paths = ','.join(ps_quote(self.remote_path(backup, name)) for name in names)
        script = (self.root / 'scripts/windows_uncached_reader.ps1').read_text() + '\n' + (
            "$rows=@(); foreach($file in @(" + paths + ")) { "
            "if(Test-Path -LiteralPath $file) { $item=Get-Item -LiteralPath $file; "
            "$rows += [PSCustomObject]@{name=$item.Name;bytes=$item.Length;"
            "sha256=[NomadUncachedReader]::Hash($file)} } }; "
            "ConvertTo-Json -InputObject @($rows) -Compress")
        rows = json.loads(self.run(script).decode('utf-8-sig'))
        return {row['name']: row for row in rows}

    def verify(self, local, backup=None):
        local = Path(local)
        manifest = json.loads((local / 'manifest.json').read_text())
        expected = dict(manifest['sha256'])
        expected['manifest.json'] = digest(local / 'manifest.json')
        actual = self.hashes(backup or local.name, expected)
        for name, sha256 in expected.items():
            row = actual.get(name, {})
            if row.get('sha256') != sha256 or row.get('bytes') != (local / name).stat().st_size:
                raise RuntimeError('Windows-native checksum mismatch: ' + name)
        return actual

    def upload(self, backup, source):
        source = Path(source)
        target = self.remote_path(backup, source.name)
        self.run('[IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName(' + ps_quote(target) + '))|Out-Null')
        remote = '/' + (target + '.transferring').replace('\\', '/')
        def quote(value):
            return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"') + '"'
        batch = ('put ' + quote(source) + ' ' + quote(remote) + '\n').encode()
        # SFTP handles binary uploads directly; Windows PowerShell's console
        # input can wait indefinitely when used as a binary SSH upload stream.
        args = ['sftp', '-q', '-b', '-', '-o', 'ControlPath=' + quote(self.socket),
                '-o', self.options[4], '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                self.user + '@' + self.host]
        result = subprocess.run(args, input=batch, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode:
            raise RuntimeError('Windows SSH upload failed: ' + result.stderr.decode(errors='replace')[-1600:])
        self.run('$temporary=' + ps_quote(target + '.transferring') + '; '
            '$outputFile=[IO.FileStream]::new($temporary,[IO.FileMode]::Open,[IO.FileAccess]::ReadWrite,[IO.FileShare]::Read); '
            'try { $outputFile.Flush($true) } finally { $outputFile.Dispose() }; '
            'Move-Item -LiteralPath $temporary -Destination ' + ps_quote(target) + ' -Force')

    def mirror(self, local):
        local = Path(local)
        manifest = json.loads((local / 'manifest.json').read_text())
        expected = dict(manifest['sha256'])
        expected['manifest.json'] = digest(local / 'manifest.json')
        final, partial = local.name, local.name + '.copying'
        rows = self.hashes(final, expected)
        if rows:
            self.verify(local, final)
            return final
        rows = self.hashes(partial, expected)
        # Publish the manifest last; incomplete transfers remain visibly staged.
        for name in [n for n in expected if n != 'manifest.json'] + ['manifest.json']:
            if rows.get(name, {}).get('sha256') == expected[name]:
                continue
            print('Sending ' + name + ' through SSH...', flush=True)
            self.upload(partial, local / name)
        self.verify(local, partial)
        source = self.remote_root + '\\' + partial
        target = self.remote_root + '\\' + final
        self.run('[IO.Directory]::Move(' + ps_quote(source) + ',' + ps_quote(target) + ')')
        return final

    def download(self, backup, name, destination, expected=None):
        destination = Path(destination)
        temporary = destination.with_name(destination.name + '.transferring')
        script = (self.root / 'scripts/windows_uncached_reader.ps1').read_text() + '\n' + (
            '[NomadUncachedReader]::Copy(' + ps_quote(self.remote_path(backup, name)) +
            ',[Console]::OpenStandardOutput())')
        h = hashlib.sha256()
        transferred, last = 0, time.monotonic()
        with tempfile.TemporaryFile(dir=self.root / 'tmp') as errors:
            process = subprocess.Popen(self.command(script), stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=errors)
            try:
                with temporary.open('wb') as output:
                    for block in iter(lambda: process.stdout.read(1024 * 1024), b''):
                        h.update(block)
                        output.write(block)
                        transferred += len(block)
                        if time.monotonic() - last >= 30:
                            print(f'SSH {name}: {transferred // (1024 * 1024)} MiB received', flush=True)
                            last = time.monotonic()
                    output.flush()
                    os.fsync(output.fileno())
                if process.wait():
                    errors.seek(0)
                    raise RuntimeError('Windows SSH download failed: ' + errors.read().decode(errors='replace')[-1600:])
            finally:
                process.stdout.close()
                if process.poll() is None:
                    process.terminate()
                    process.wait()
        if expected and h.hexdigest() != expected:
            raise RuntimeError('SSH download checksum mismatch: ' + name)
        temporary.replace(destination)
        return h.hexdigest()

    def fetch(self, backup, destination):
        destination = Path(destination)
        destination.mkdir(mode=0o700, parents=True, exist_ok=False)
        incomplete = destination / 'INCOMPLETE'
        incomplete.write_text('SSH retrieval has not completed. Do not restore this folder.\n')
        self.download(backup, 'manifest.json', destination / 'manifest.json')
        manifest = json.loads((destination / 'manifest.json').read_text())
        if manifest.get('format') != 1 or manifest.get('complete') is not True:
            raise RuntimeError('Unsupported or incomplete remote backup')
        names = [safe_name(name) for name in manifest['sha256']]
        for name in names:
            print('Retrieving ' + name + ' through SSH...', flush=True)
            self.download(backup, name, destination / name, expected=manifest['sha256'][name])
        incomplete.unlink()
        return destination
