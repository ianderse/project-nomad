#!/usr/bin/env python3
"""Back up, verify, restore, check, and update this external-drive NOMAD install."""
import argparse
import contextlib
import datetime as dt
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import struct
import urllib.parse
import urllib.request
import uuid

ROOT = Path(os.environ.get('NOMAD_ROOT', Path(__file__).resolve().parents[1]))
COMPOSE = ROOT / 'compose.json'
POLICY = ROOT / 'state/maintenance.json'
DB_INFRA = {'nomad_mysql', 'nomad_redis', 'nomad_qdrant', 'nomad_ai_adapter', 'nomad_embeddings'}


def run(args, *, output=False, input=None, stdout=None, quiet=False):
    result = subprocess.run([str(x) for x in args], input=input, stdout=(subprocess.PIPE if output else stdout),
                            stderr=subprocess.PIPE, check=False)
    if result.returncode:
        # Credentials are read inside containers or from protected env files, never argv.
        raise RuntimeError(f'{args[0]} failed: {result.stderr.decode(errors="replace")[-1800:]}')
    if result.stderr and not quiet and not output:
        print(result.stderr.decode(errors='replace').strip(), flush=True)
    return result.stdout if output else None


def docker(*args, **kwargs):
    return run(['docker', *args], **kwargs)


def dc(*args):
    docker('compose', '--env-file', ROOT / '.env', '-f', COMPOSE, *args)


def config():
    return json.loads(COMPOSE.read_text())


def write_json(path, data):
    path = Path(path)
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(json.dumps(data, indent=2) + '\n')
    temp.replace(path)


def image(service):
    return config()['services'][service]['image']


def stamp():
    return dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:6]


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def http(url, data=None, method=None, timeout=20):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method,
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def internal(url, data=None, method=None, raw=False, outfile=None, container='nomad_ai_adapter'):
    code = ('import urllib.request,json,sys; '
            f'u={url!r}; data={data!r}; method={method!r}; '
            'body=json.dumps(data).encode() if data is not None else None; '
            'req=urllib.request.Request(u,data=body,method=method,headers={"Content-Type":"application/json"}); '
            'r=urllib.request.urlopen(req,timeout=180); sys.stdout.buffer.write(r.read())')
    if outfile:
        with Path(outfile).open('wb') as f:
            docker('exec', container, 'python', '-c', code, stdout=f, quiet=True)
        return
    result = docker('exec', container, 'python', '-c', code, output=True)
    return result if raw else json.loads(result)


def wait_for(check, seconds=90):
    end = time.monotonic() + seconds
    last = None
    while time.monotonic() < end:
        try:
            return check()
        except Exception as exc:
            last = exc
            time.sleep(1)
    raise RuntimeError(f'Timed out: {last}')


def running_names():
    return docker('ps', '--filter', 'name=^/nomad_', '--format', '{{.Names}}', output=True).decode().split()


@contextlib.contextmanager
def quiesce():
    names = [n for n in running_names() if n not in DB_INFRA]
    if names:
        print('Pausing applications for a consistent recovery backup...', flush=True)
        docker('stop', '--timeout', '30', *names, quiet=True)
    try:
        yield names
    finally:
        if names:
            docker('start', *names, quiet=True)


def sql(statement, container='nomad_mysql'):
    script = 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -u root --batch --skip-column-names nomad'
    return docker('exec', '-i', container, 'sh', '-c', script, input=statement.encode(), output=True).decode()


def table_counts(container='nomad_mysql'):
    tables = sql('SHOW TABLES;', container).splitlines()
    result = {}
    for table in tables:
        assert table.replace('_', '').isalnum(), table
        result[table] = int(sql('SELECT COUNT(*) FROM `' + table + '`;', container).strip())
    return result


def qcounts(url='http://nomad_qdrant:6333', container='nomad_ai_adapter'):
    collections = internal(url + '/collections', container=container)['result']['collections']
    return {c['name']: internal(url + '/collections/' + urllib.parse.quote(c['name'], safe=''),
                               container=container)['result']['points_count'] for c in collections}


def enable_redis_aof(container):
    docker('exec', container, 'redis-cli', 'CONFIG', 'SET', 'appendonly', 'yes', output=True)
    def ready():
        raw = docker('exec', container, 'redis-cli', 'INFO', 'persistence', output=True).decode()
        fields = dict(line.split(':', 1) for line in raw.splitlines() if ':' in line)
        if not (fields.get('aof_enabled') == '1' and fields.get('aof_rewrite_in_progress') == '0'
                and fields.get('aof_rewrite_scheduled') == '0'
                and fields.get('aof_last_bgrewrite_status') == 'ok'
                and int(fields.get('aof_current_size', '0')) > 0):
            raise RuntimeError('Redis is rebuilding its persistence file')
        return True
    wait_for(ready)


def check_backup(path):
    path = Path(path).resolve()
    manifest = json.loads((path / 'manifest.json').read_text())
    if manifest.get('format') != 1 or not manifest.get('complete') or (path / 'INCOMPLETE').exists():
        raise RuntimeError('Unsupported backup format')
    for name, digest in manifest['sha256'].items():
        if Path(name).name != name or sha(path / name) != digest:
            raise RuntimeError('Backup checksum mismatch: ' + name)
    return manifest


def backup(destination=None):
    path = ROOT / 'backups' / stamp()
    path.mkdir(parents=True, mode=0o700)
    path.chmod(0o700)
    try:
        with quiesce():
            manifest = {'format': 1, 'created_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                        'root': str(ROOT), 'compose': config(), 'tables': table_counts(),
                        'qdrant_points': qcounts(), 'complete': False}
            dump = ('MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysqldump -u root --single-transaction '
                    '--routines --triggers --events --hex-blob --no-tablespaces '
                    '--add-drop-database --databases nomad')
            with (path / 'mysql.sql').open('wb') as f:
                docker('exec', 'nomad_mysql', 'sh', '-c', dump, stdout=f, quiet=True)
            snapshot = internal('http://nomad_qdrant:6333/snapshots', method='POST')['result']['name']
            try:
                internal('http://nomad_qdrant:6333/snapshots/' + urllib.parse.quote(snapshot, safe=''),
                         outfile=path / 'qdrant.snapshot')
            finally:
                internal('http://nomad_qdrant:6333/snapshots/' + urllib.parse.quote(snapshot, safe=''), method='DELETE')
            docker('exec', 'nomad_redis', 'redis-cli', 'SAVE', output=True)
            docker('cp', 'nomad_redis:/data/dump.rdb', str(path / 'redis.rdb'), quiet=True)
            manifest['redis_keys'] = int(docker('exec', 'nomad_redis', 'redis-cli', 'DBSIZE', output=True))
            print('Saving configuration, credentials, documents, library, and maps...', flush=True)
            with tarfile.open(path / 'files.tar', 'w') as archive:
                for name in ['.env', '.dockerignore', 'compose.json', 'nomad', 'INSTALLATION.txt',
                             'Start NOMAD.command', 'Stop NOMAD.command', 'scripts', 'source']:
                    if (ROOT / name).exists():
                        archive.add(ROOT / name, arcname=name)
                for p in (ROOT / 'state').iterdir():
                    if p.is_file() and p.name != '.maintenance.lock':
                        archive.add(p, arcname='state/' + p.name)
                for p in (ROOT / 'storage').iterdir():
                    if p.name not in {'qdrant', 'embedding-models', 'tmp', 'logs'}:
                        archive.add(p, arcname='storage/' + p.name)
                # Include the exact adapter source used by a versioned release.
                source = next((v.split(':/adapter')[0] for v in config()['services']['ai-adapter']['volumes']
                               if ':/adapter' in v), None)
                if source and '/releases/' in source:
                    release = Path(source).parents[4]
                    if release.is_relative_to(ROOT / 'releases'):
                        archive.add(release, arcname=str(release.relative_to(ROOT)))
            manifest['sha256'] = {p.name: sha(p) for p in path.iterdir() if p.is_file()}
            manifest['complete'] = True
            write_json(path / 'manifest.json', manifest)
        check_backup(path)
        write_json(ROOT / 'state/latest-backup.json', {'path': str(path)})
        policy = json.loads(POLICY.read_text()) if POLICY.exists() else {}
        target = destination or policy.get('backup_destination')
        if target:
            mirror_backup(path, Path(target))
        print('Recovery backup: ' + str(path), flush=True)
        return path
    except BaseException:
        if not (path / 'manifest.json').exists():
            (path / 'INCOMPLETE').write_text('Backup did not complete. Do not use for restoration.\n')
        raise


def mirror_backup(path, destination):
    # Never create a directory in place of an unmounted SMB share.
    policy = json.loads(POLICY.read_text()) if POLICY.exists() else {}
    mountpoint = policy.get('backup_mount')
    if mountpoint and not os.path.ismount(mountpoint):
        raise RuntimeError('Recovery share is not mounted: ' + mountpoint + '. Local backup is complete.')
    if not destination.parent.exists():
        raise RuntimeError('Backup destination parent is unavailable: ' + str(destination.parent))
    destination.mkdir(exist_ok=True, mode=0o700)
    partial = destination / (path.name + '.copying')
    print('Copying recovery backup to ' + str(destination), flush=True)
    final = destination / path.name
    if final.exists():
        check_backup(final)
        print('Existing network recovery copy verified: ' + str(final))
        return
    partial.mkdir(exist_ok=True, mode=0o700)
    for source in path.iterdir():
        if not source.is_file(): continue
        target = partial / source.name
        if target.exists() and target.stat().st_size == source.stat().st_size and sha(target) == sha(source):
            continue
        temp = target.with_name(target.name + '.transferring')
        last = time.monotonic()
        copied = 0
        with source.open('rb') as src, temp.open('wb') as out:
            for block in iter(lambda: src.read(4 * 1024 * 1024), b''):
                out.write(block); copied += len(block)
                if time.monotonic() - last >= 10:
                    print(f'Copying {source.name}: {copied // (1024 * 1024)} / {source.stat().st_size // (1024 * 1024)} MiB', flush=True)
                    last = time.monotonic()
            out.flush()
            os.fsync(out.fileno())  # Request durable server-side storage before verification/rename.
        temp.replace(target)
    check_backup(partial)
    partial.rename(final)
    write_json(ROOT / 'state/latest-network-backup.json', {'path': str(final), 'verified_sha256': True})
    print('Network recovery copy verified: ' + str(final), flush=True)


def test_restore(path):
    path = Path(path).resolve()
    manifest = check_backup(path)
    tag = 'nomad_restore_' + uuid.uuid4().hex[:10]
    network = tag + '_network'
    names = [tag + '_mysql', tag + '_qdrant', tag + '_redis', tag + '_probe']
    volumes = [tag + '_mysql_data', tag + '_qdrant_data', tag + '_redis_data']
    stage = ROOT / 'tmp' / tag
    stage.mkdir(mode=0o700)
    recovery = stage / 'recovery'
    recovery.mkdir()
    for name in ['qdrant.snapshot', 'redis.rdb']:
        shutil.copyfile(path / name, recovery / name)
    testenv = stage / 'mysql.env'
    testenv.write_text('MYSQL_ROOT_PASSWORD=' + uuid.uuid4().hex + '\n')
    testenv.chmod(0o600)
    services = manifest['compose']['services']
    docker('network', 'create', '--internal', network, output=True)
    try:
        for volume in volumes:
            docker('volume', 'create', volume, output=True)
        docker('run', '-d', '--name', names[0], '--network', network, '--env-file', testenv,
               '-v', volumes[0] + ':/var/lib/mysql', services['mysql']['image'], output=True)
        wait_for(lambda: docker('exec', names[0], 'sh', '-c',
                               'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -h 127.0.0.1 -u root -e "SELECT 1"', output=True))
        with (path / 'mysql.sql').open('rb') as f:
            result = subprocess.run(['docker', 'exec', '-i', names[0], 'sh', '-c',
                                     'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -u root'],
                                    stdin=f, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if result.returncode:
                raise RuntimeError('Isolated SQL restoration failed: ' + result.stderr.decode()[-1000:])
        if table_counts(names[0]) != manifest['tables']:
            raise RuntimeError('Restored SQL table counts do not match')
        docker('run', '-d', '--name', names[1], '--network', network,
               '-e', 'QDRANT__TELEMETRY_DISABLED=true', '-v', volumes[1] + ':/qdrant/storage',
               '-v', str(recovery) + ':/recovery:ro', services['qdrant']['image'],
               './qdrant', '--storage-snapshot', '/recovery/qdrant.snapshot', output=True)
        # A probe on the isolated network uses the existing Python adapter image.
        docker('run', '-d', '--name', names[3], '--network', network, '--entrypoint', 'python',
               image('ai-adapter'), '-c', 'import time; time.sleep(600)', output=True)
        counts = wait_for(lambda: qcounts('http://' + names[1] + ':6333', names[3]))
        if counts != manifest['qdrant_points']:
            raise RuntimeError('Restored Qdrant point counts do not match')
        # Counts alone cannot detect a corrupt compressed text payload.
        docker('cp', ROOT / 'scripts/index_audit.py', names[3] + ':/tmp/index_audit.py', quiet=True)
        for collection in counts:
            url = 'http://' + names[1] + ':6333/collections/' + urllib.parse.quote(collection, safe='')
            docker('exec', names[3], 'python', '/tmp/index_audit.py', '--url', url,
                   '--output', '/tmp/audit-' + uuid.uuid4().hex[:8], output=True)
        docker('run', '--rm', '--network', 'none', '--entrypoint', 'sh',
               '-v', volumes[2] + ':/data', '-v', str(recovery) + ':/recovery:ro',
               services['redis']['image'], '-c', 'cp /recovery/redis.rdb /data/dump.rdb; chown -R redis:redis /data')
        docker('run', '-d', '--name', names[2], '--network', network,
               '-v', volumes[2] + ':/data', services['redis']['image'], output=True)
        wait_for(lambda: docker('exec', names[2], 'redis-cli', 'PING', output=True))
        enable_redis_aof(names[2])
        docker('stop', names[2], output=True); docker('rm', names[2], output=True)
        docker('run', '-d', '--name', names[2], '--network', network,
               '-v', volumes[2] + ':/data', services['redis']['image'],
               'redis-server', '--appendonly', 'yes', output=True)
        wait_for(lambda: docker('exec', names[2], 'redis-cli', 'PING', output=True))
        restored_keys = int(docker('exec', names[2], 'redis-cli', 'DBSIZE', output=True))
        if manifest['redis_keys'] and not restored_keys:
            raise RuntimeError('Redis restored without any keys')
        with tarfile.open(path / 'files.tar') as archive:
            members = {m.name: m for m in archive.getmembers()}
            for name in ['.env', 'compose.json', 'state/omlx-key', 'nomad']:
                if name not in members or not archive.extractfile(members[name]).read():
                    raise RuntimeError('Missing recovery configuration: ' + name)
            uploaded = [m for m in members if m.startswith('storage/kb_uploads/') and members[m].isfile()]
            for name in uploaded:
                with archive.extractfile(members[name]) as f:
                    while f.read(1024 * 1024):
                        pass
        result = {'backup': str(path), 'verified_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                  'sql_tables': len(manifest['tables']), 'qdrant_points': counts,
                  'redis_keys': restored_keys, 'configuration_and_documents': 'passed',
                  'qdrant_vectors_and_payloads': 'all readable'}
        write_json(path / 'restore-test.json', result)
        print('Isolated restore passed: SQL tables, document vectors, Redis, credentials, and documents.', flush=True)
        return result
    finally:
        for name in names:
            subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(['docker', 'network', 'rm', network], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for volume in volumes:
            subprocess.run(['docker', 'volume', 'rm', volume], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shutil.rmtree(stage)


def health(verbose=True):
    results = []
    def check(name, function):
        try:
            detail = function()
            results.append({'check': name, 'ok': True, 'detail': detail})
        except Exception as exc:
            results.append({'check': name, 'ok': False, 'detail': str(exc)[-500:]})
    def containers():
        expected = [v['container_name'] for v in config()['services'].values()]
        data = json.loads(docker('inspect', *expected, output=True))
        bad = [d['Name'] for d in data if not d['State']['Running'] or
               d['State'].get('Health', {}).get('Status') == 'unhealthy']
        if bad:
            raise RuntimeError('Not ready: ' + ', '.join(bad))
        return str(len(data)) + ' core services running'
    def inference():
        key = (ROOT / 'state/omlx-key').read_text().strip()
        req = urllib.request.Request('http://127.0.0.1:8000/v1/models',
                                     headers={'Authorization': 'Bearer ' + key})
        with urllib.request.urlopen(req, timeout=8) as r:
            models = json.load(r).get('data', [])
        if not models:
            raise RuntimeError('oMLX has no chat models')
        advertised = internal('http://nomad_ai_adapter:11436/api/tags')['models']
        if not advertised:
            raise RuntimeError('AI adapter has no models')
        return str(len(models)) + ' authenticated oMLX models; adapter reachable'
    def capacity():
        filesystems = http('http://127.0.0.1:8080/api/system/info').get('fsSize', [])
        content = next((f for f in filesystems if f.get('mount') == '/app/storage'), None)
        native = next((f for f in filesystems if f.get('mount') == '/'), None)
        if content is None or native is None:
            raise RuntimeError('Content and VM storage are not reported separately')
        stat = os.statvfs(ROOT / 'storage')
        actual = stat.f_blocks * stat.f_frsize
        if abs(content['size'] - actual) > actual * 0.01 or not 0 <= content['available'] <= content['size']:
            raise RuntimeError('Reported content capacity does not match the external filesystem')
        return f"Content: {content['available'] / 1e12:.2f} TB free; VM/index: {native['available'] / 1e9:.0f} GB free"
    def embedding():
        fs = docker('exec', 'nomad_qdrant', 'stat', '-f', '-c', '%T', '/qdrant/storage', output=True).decode().strip()
        if 'fuse' in fs.lower():
            raise RuntimeError('Qdrant is using an unsafe shared filesystem')
        data = internal('http://nomad_ai_adapter:11436/api/embed',
                        {'model': 'nomic-embed-text:v1.5', 'input': ['NOMAD readiness check']})
        vector = data['embeddings'][0]
        if len(vector) != 768 or not any(vector):
            raise RuntimeError('Invalid embedding response')
        collections = internal('http://nomad_qdrant:6333/collections')['result']['collections']
        for c in collections:
            info = internal('http://nomad_qdrant:6333/collections/' + c['name'])['result']
            if info['status'] != 'green':
                raise RuntimeError('Index is not green')
            if info['points_count']:
                result = internal('http://nomad_qdrant:6333/collections/' + c['name'] + '/points/search',
                                  {'vector': vector, 'limit': 1, 'with_payload': False})
                if not result.get('result'):
                    raise RuntimeError('Document index search returned no points')
        return '768-dimensional embeddings and document-index search passed'
    def library():
        with urllib.request.urlopen('http://127.0.0.1:8090/catalog/v2/entries', timeout=10) as r:
            data = r.read()
        if b'<entry>' not in data:
            raise RuntimeError('Offline library has no books')
        return 'Offline library catalog contains books'
    def maps():
        return check_maps()
    def logs_dashboard():
        if 'dozzle' not in config()['services']:
            return 'Logs dashboard not configured'
        with urllib.request.urlopen('http://127.0.0.1:9999', timeout=10) as response:
            if not response.read():
                raise RuntimeError('Logs dashboard returned an empty page')
        return 'Service logs dashboard reachable on loopback port 9999'
    check('containers', containers)
    check('dashboard', lambda: http('http://127.0.0.1:8080/api/health'))
    check('content and index capacity', capacity)
    check('oMLX and adapter', inference)
    check('embeddings and document search', embedding)
    check('offline library', library)
    check('offline map assets', maps)
    check('service logs dashboard', logs_dashboard)
    if verbose:
        for item in results:
            print(('PASS ' if item['ok'] else 'FAIL ') + item['check'] + ': ' + str(item['detail']), flush=True)
    write_json(ROOT / 'state/latest-health.json', {'at': dt.datetime.now(dt.timezone.utc).isoformat(), 'checks': results})
    return all(item['ok'] for item in results)


def migrate_qdrant():
    current = config()
    old_mount = current['services']['qdrant']['volumes'][0]
    if not old_mount.startswith(str(ROOT)):
        print('Qdrant already uses a native Linux volume.')
        return
    volume = 'nomad_qdrant_data_' + stamp().lower()
    recovery = ROOT / 'state' / ('qdrant-migration-' + stamp())
    recovery.mkdir(mode=0o700)
    with quiesce():
        expected = qcounts()
        name = internal('http://nomad_qdrant:6333/snapshots', method='POST')['result']['name']
        internal('http://nomad_qdrant:6333/snapshots/' + name, outfile=recovery / 'qdrant.snapshot')
        internal('http://nomad_qdrant:6333/snapshots/' + name, method='DELETE')
        docker('volume', 'create', volume, output=True)
        initializer = 'nomad_qdrant_migration'
        docker('stop', 'nomad_qdrant', output=True)
        try:
            docker('run', '-d', '--name', initializer, '--network', 'project-nomad_default',
                   '-e', 'QDRANT__TELEMETRY_DISABLED=true', '-v', volume + ':/qdrant/storage',
                   '-v', str(recovery) + ':/recovery:ro', image('qdrant'),
                   './qdrant', '--storage-snapshot', '/recovery/qdrant.snapshot', output=True)
            restored = wait_for(lambda: qcounts('http://' + initializer + ':6333'))
            if restored != expected:
                raise RuntimeError('Migration point counts differ from the source index')
            docker('stop', initializer, output=True)
            docker('rm', initializer, output=True)
            updated = json.loads(json.dumps(current))
            updated.setdefault('volumes', {})['qdrant-data'] = {'name': volume, 'external': True}
            updated['services']['qdrant']['volumes'][0] = 'qdrant-data:/qdrant/storage'
            write_json(COMPOSE, updated)
            dc('up', '-d', 'qdrant')
            if wait_for(qcounts) != expected:
                raise RuntimeError('Production index did not retain all points')
            fs = docker('exec', 'nomad_qdrant', 'stat', '-f', '-c', '%T', '/qdrant/storage', output=True).decode().strip()
            if 'fuse' in fs.lower():
                raise RuntimeError('Migration still exposes a shared filesystem')
            # Preserve the native volume if NOMAD's own UI recreates this service.
            raw = sql("SELECT container_config FROM services WHERE service_name='nomad_qdrant';").strip()
            container_config = json.loads(raw)
            container_config.setdefault('HostConfig', {})['Binds'] = [volume + ':/qdrant/storage']
            encoded = json.dumps(container_config).encode().hex()
            sql("UPDATE services SET container_config=CONVERT(0x" + encoded +
                " USING utf8mb4), is_user_modified=1, installed=1, installation_status='idle' "
                "WHERE service_name='nomad_qdrant';")
            write_json(recovery / 'migration.json', {'volume': volume, 'filesystem': fs, 'points': expected,
                                                    'old_mount_preserved': old_mount})
            print('Qdrant moved to native Linux storage (' + fs + '); all points preserved: ' + str(expected))
        except BaseException:
            subprocess.run(['docker', 'rm', '-f', initializer], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            write_json(COMPOSE, current)
            dc('up', '-d', 'qdrant')
            raise


def safe_extract(archive, target):
    target = Path(target).resolve()
    for member in archive.getmembers():
        p = target / member.name
        if not p.resolve().is_relative_to(target) or member.isdev() or member.isfifo():
            raise RuntimeError('Unsafe recovery archive entry: ' + member.name)
        if member.issym():
            if not (p.parent / member.linkname).resolve().is_relative_to(target):
                raise RuntimeError('Unsafe recovery symlink: ' + member.name)
        if member.islnk() and not (target / member.linkname).resolve().is_relative_to(target):
            raise RuntimeError('Unsafe recovery hardlink: ' + member.name)
    archive.extractall(target)


def repair_maps():
    repo = 'Crosstalk-Solutions/project-nomad-maps'
    revision = http('https://api.github.com/repos/' + repo + '/commits/master')['sha']
    metadata = http('https://api.github.com/repos/' + repo + '/contents/base-assets.tar.gz?ref=' + revision)
    url = 'https://raw.githubusercontent.com/' + repo + '/' + revision + '/base-assets.tar.gz'
    archive = ROOT / 'downloads' / ('maps-assets-' + revision + '.tar.gz')
    partial = archive.with_suffix('.downloading')
    req = urllib.request.Request(url, headers={'Accept-Encoding': 'identity'})
    with urllib.request.urlopen(req, timeout=90) as response, partial.open('wb') as out:
        shutil.copyfileobj(response, out)
    digest = hashlib.sha1()
    digest.update(('blob ' + str(partial.stat().st_size) + '\0').encode())
    with partial.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            digest.update(block)
    if digest.hexdigest() != metadata['sha']:
        raise RuntimeError('Map archive differs from its pinned GitHub object')
    stage = ROOT / 'tmp' / ('maps-assets-' + stamp())
    stage.mkdir()
    try:
        with tarfile.open(partial, 'r:gz') as tar:
            safe_extract(tar, stage)
        top = stage / 'tozip'
        font_files = list((top / 'basemaps-assets/fonts').rglob('*.pbf'))
        if len(font_files) < 100:
            raise RuntimeError('Downloaded map archive contains incomplete fonts')
        current = ROOT / 'storage/maps'
        differing = []
        checked = 0
        for p in top.rglob('*'):
            if p.is_file():
                checked += 1
                target = current / p.relative_to(top)
                if not target.is_file() or sha(p) != sha(target):
                    differing.append(str(p.relative_to(top)))
        if differing:
            quarantine = ROOT / 'state' / ('map-assets-before-' + stamp())
            quarantine.mkdir()
            for name in ['basemaps-assets', 'nomad-base-styles.json']:
                if (current / name).exists():
                    (current / name).rename(quarantine / name)
                (top / name).rename(current / name)
        partial.replace(archive)
        report = {'source_commit': revision, 'sha256': sha(archive), 'files_verified': checked,
                  'files_repaired': len(differing), 'verified_at': dt.datetime.now(dt.timezone.utc).isoformat()}
        write_json(ROOT / 'state/map-assets-integrity.json', report)
        print('Map asset archive verified: ' + str(checked) + ' files; ' + str(len(differing)) + ' repaired.')
        print(check_maps())
    finally:
        shutil.rmtree(stage)


def check_maps():
    # MapLibre 6 ships a separate module worker. A missing build artifact makes
    # the whole canvas blank even when fonts, sprites and PMTiles all pass.
    worker = json.loads(docker('exec', 'nomad_admin', 'node', '-e',
        "const fs=require('node:fs'),crypto=require('node:crypto');"
        "const p='/app/node_modules/maplibre-gl/dist/maplibre-gl-worker.mjs';"
        "console.log(JSON.stringify(fs.existsSync(p)?{"
        "version:require('/app/node_modules/maplibre-gl/package.json').version,"
        "sha256:crypto.createHash('sha256').update(fs.readFileSync(p)).digest('hex')}:null))",
        output=True))
    if worker:
        url = 'http://127.0.0.1:8080/assets/maplibre-gl-worker.mjs?v=' + worker['version']
        with urllib.request.urlopen(url, timeout=10) as r:
            if 'javascript' not in r.headers.get('Content-Type', ''):
                raise RuntimeError('Map worker has the wrong MIME type')
            if hashlib.sha256(r.read()).hexdigest() != worker['sha256']:
                raise RuntimeError('Map worker does not match the installed renderer')
    styles = http('http://127.0.0.1:8080/api/maps/styles')
    if styles.get('version') != 8 or not styles.get('layers'):
        raise RuntimeError('Map styles unavailable')
    fonts = ROOT / 'storage/maps/basemaps-assets/fonts'
    glyph = next(fonts.rglob('*.pbf'), None)
    if glyph is None:
        raise RuntimeError('Map fonts are missing; run nomad repair-maps')
    relative = str(glyph.relative_to(ROOT / 'storage/maps'))
    with urllib.request.urlopen('http://127.0.0.1:8080/' + urllib.parse.quote(relative), timeout=10) as r:
        if not r.read():
            raise RuntimeError('Map glyph endpoint is empty')
    for suffix in ['.json', '.png']:
        url = styles['sprite'].replace('http://localhost:', 'http://127.0.0.1:') + suffix
        with urllib.request.urlopen(url, timeout=10) as r:
            if not r.read():
                raise RuntimeError('Map sprite endpoint is empty')
    sources = styles.get('sources', {})
    if not sources:
        return 'Map assets ready; no regions installed'
    source = next(iter(sources.values()))['url'].removeprefix('pmtiles://')
    def read_range(offset, size):
        req = urllib.request.Request(source, headers={'Range': f'bytes={offset}-{offset + size - 1}'})
        with urllib.request.urlopen(req, timeout=15) as r:
            if r.status != 206:
                raise RuntimeError('Map endpoint does not support byte ranges')
            b = r.read(size + 1)
            if len(b) != size:
                raise RuntimeError('Incomplete PMTiles range response')
            return b
    header = read_range(0, 127)
    if header[:8] != b'PMTiles\x03':
        raise RuntimeError('Unsupported or incomplete regional PMTiles header')
    root_offset, root_size = struct.unpack_from('<QQ', header, 8)
    leaf_offset = struct.unpack_from('<Q', header, 40)[0]
    tile_offset = struct.unpack_from('<Q', header, 56)[0]
    compression, tile_compression = header[97], header[98]
    def directory(offset, size, depth=0):
        if depth > 4:
            raise RuntimeError('Invalid nested PMTiles directory')
        b = read_range(offset, size)
        if compression == 2:
            b = gzip.decompress(b)
        elif compression != 1:
            raise RuntimeError('Unsupported PMTiles directory compression')
        cursor = 0
        def varint():
            nonlocal cursor
            value = shift = 0
            while True:
                n = b[cursor]; cursor += 1
                value |= (n & 127) << shift
                if not n & 128:
                    return value
                shift += 7
                if shift > 63:
                    raise RuntimeError('Invalid PMTiles integer')
        n = varint()
        if not 0 < n < 100000:
            raise RuntimeError('Invalid PMTiles directory size')
        for _ in range(n): varint()  # tile ID deltas
        lengths_of_runs = [varint() for _ in range(n)]
        lengths = [varint() for _ in range(n)]
        offsets = []
        for i in range(n):
            value = varint()
            offsets.append((offsets[i-1] + lengths[i-1]) if value == 0 and i else value - 1)
        for i in range(n):
            if lengths_of_runs[i] > 0:
                tile = read_range(tile_offset + offsets[i], lengths[i])
                if tile_compression == 2:
                    tile = gzip.decompress(tile)
                if not tile:
                    raise RuntimeError('Empty regional map tile')
                return len(tile)
        return directory(leaf_offset + offsets[0], lengths[0], depth + 1)
    size = directory(root_offset, root_size)
    return f'Map renderer worker, offline glyphs, sprites, and a regional vector tile served ({size} decoded bytes)'


def restore_backup(path, safety_backup=True):
    path = Path(path).resolve()
    manifest = check_backup(path)
    current = config()
    if current['services']['mysql']['image'] != manifest['compose']['services']['mysql']['image']:
        raise RuntimeError('This restore requires the same MySQL version; database upgrades need a separate migration.')
    for service in manifest['compose']['services'].values():
        docker('image', 'inspect', service['image'], output=True)
    rescue = backup() if safety_backup else None
    identifier = stamp().lower()
    stage = ROOT / 'tmp' / ('restore-' + identifier)
    stage.mkdir(mode=0o700)
    payload = stage / 'payload'; payload.mkdir()
    snapshots = stage / 'snapshots'; snapshots.mkdir()
    for name in ['qdrant.snapshot', 'redis.rdb']:
        shutil.copyfile(path / name, snapshots / name)
    with tarfile.open(path / 'files.tar') as tar:
        safe_extract(tar, payload)
    def credentials(file):
        return dict(line.split('=', 1) for line in file.read_text().splitlines() if '=' in line)
    old_keys, restore_keys = credentials(ROOT / '.env'), credentials(payload / '.env')
    for key in ['MYSQL_ROOT_PASSWORD', 'DB_PASSWORD']:
        if old_keys.get(key) != restore_keys.get(key):
            raise RuntimeError('Backup uses different database credentials; restore into a fresh installation instead.')
    qvolume = 'nomad_qdrant_restored_' + identifier
    rvolume = 'nomad_redis_restored_' + identifier
    initializer = 'nomad_recovery_initializer'
    quarantine = ROOT / 'state' / ('before-restore-' + identifier)
    quarantine.mkdir()
    try:
        with quiesce():
            docker('volume', 'create', qvolume, output=True)
            docker('volume', 'create', rvolume, output=True)
            docker('run', '-d', '--name', initializer, '--network', 'project-nomad_default',
                   '-e', 'QDRANT__TELEMETRY_DISABLED=true', '-v', qvolume + ':/qdrant/storage',
                   '-v', str(snapshots) + ':/recovery:ro', manifest['compose']['services']['qdrant']['image'],
                   './qdrant', '--storage-snapshot', '/recovery/qdrant.snapshot', output=True)
            restored = wait_for(lambda: qcounts('http://' + initializer + ':6333'))
            if restored != manifest['qdrant_points']:
                raise RuntimeError('Restored document index does not match the backup')
            docker('stop', initializer, output=True); docker('rm', initializer, output=True)
            docker('run', '--rm', '--network', 'none', '--entrypoint', 'sh',
                   '-v', rvolume + ':/data', '-v', str(snapshots) + ':/recovery:ro',
                   manifest['compose']['services']['redis']['image'], '-c',
                   'cp /recovery/redis.rdb /data/dump.rdb; chown -R redis:redis /data')
            docker('run', '-d', '--name', initializer, '--network', 'none',
                   '-v', rvolume + ':/data', manifest['compose']['services']['redis']['image'],
                   'redis-server', '--appendonly', 'no', output=True)
            wait_for(lambda: docker('exec', initializer, 'redis-cli', 'PING', output=True))
            enable_redis_aof(initializer)
            docker('stop', initializer, output=True); docker('rm', initializer, output=True)
            with (path / 'mysql.sql').open('rb') as f:
                result = subprocess.run(['docker', 'exec', '-i', 'nomad_mysql', 'sh', '-c',
                                         'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -u root'],
                                        stdin=f, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                if result.returncode:
                    raise RuntimeError('SQL restoration failed: ' + result.stderr.decode()[-1000:])
            if table_counts() != manifest['tables']:
                raise RuntimeError('SQL row counts differ after restoration')
            # Keep the maintenance tools that are doing the recovery. Restore runtime
            # configuration, exact application sources, and all backed-up content.
            for name in ['.env', 'source']:
                target = ROOT / name
                if target.exists() or target.is_symlink(): target.rename(quarantine / name)
                (payload / name).rename(target)
            (ROOT / '.env').chmod(0o600)
            for name in ['omlx-key', 'model-map.json']:
                if (payload / 'state' / name).exists():
                    shutil.copy2(payload / 'state' / name, ROOT / 'state' / name)
            for item in (payload / 'storage').iterdir():
                target = ROOT / 'storage' / item.name
                (quarantine / 'storage').mkdir(exist_ok=True)
                if target.exists(): target.rename(quarantine / 'storage' / item.name)
                item.rename(target)
            if (payload / 'releases').exists():
                (ROOT / 'releases').mkdir(exist_ok=True)
                for item in (payload / 'releases').iterdir():
                    target = ROOT / 'releases' / item.name
                    if not target.exists(): item.rename(target)
            updated = manifest['compose']
            updated.setdefault('volumes', {})['qdrant-data'] = {'name': qvolume, 'external': True}
            updated['volumes']['redis-data'] = {'name': rvolume, 'external': True}
            updated['services']['qdrant']['volumes'][0] = 'qdrant-data:/qdrant/storage'
            updated['services']['redis']['volumes'][0] = 'redis-data:/data'
            write_json(COMPOSE, updated)
            raw = sql("SELECT container_config FROM services WHERE service_name='nomad_qdrant';").strip()
            c = json.loads(raw); c.setdefault('HostConfig', {})['Binds'] = [qvolume + ':/qdrant/storage']
            sql('UPDATE services SET container_config=CONVERT(0x' + json.dumps(c).encode().hex() +
                " USING utf8mb4), is_user_modified=1 WHERE service_name='nomad_qdrant';")
            dc('up', '-d')
        wait_for(lambda: http('http://127.0.0.1:8080/api/health'))
        if not health():
            raise RuntimeError('Restored state failed readiness checks; the pre-restore backup is preserved.')
        print('Restored recovery backup: ' + str(path))
    except BaseException:
        if rescue: print('Pre-restore recovery backup: ' + str(rescue), file=sys.stderr)
        raise
    finally:
        subprocess.run(['docker', 'rm', '-f', initializer], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shutil.rmtree(stage)


def update(check_only=False, reapply=False):
    policy = json.loads(POLICY.read_text())
    tag = policy.get('app_image_tag', 'ghcr.io/caweis/project-nomad-macos-arm64:edge')
    print('Checking the Mac port image...', flush=True)
    docker('pull', '--platform', 'linux/arm64', tag)
    candidate = json.loads(docker('image', 'inspect', tag, output=True))[0]
    current = config()
    active = json.loads(docker('image', 'inspect', current['services']['admin']['image'], output=True))[0]
    revision = candidate['Config'].get('Labels', {}).get('org.opencontainers.image.revision', '')
    if candidate['Architecture'] != 'arm64' or len(revision) != 40 or any(c not in '0123456789abcdef' for c in revision):
        raise RuntimeError('Candidate lacks verified ARM64 architecture or source revision metadata')
    digest = next(d for d in candidate['RepoDigests'] if d.startswith(tag.rsplit(':', 1)[0] + '@'))
    print('Candidate source revision: ' + revision)
    if check_only:
        print('A newer image is available.' if candidate['Id'] != active['Id'] else 'The installed app image is current.')
        return
    if candidate['Id'] == active['Id'] and not reapply:
        print('The installed app image is current; no live changes made.')
        return
    release = ROOT / 'releases' / revision
    source = release / 'source'
    if not source.exists():
        source.mkdir(parents=True)
        run(['git', '-C', source, 'init'], output=True)
        run(['git', '-C', source, 'remote', 'add', 'origin', policy.get('source_repo', 'https://github.com/caweis/project-nomad.git')])
        run(['git', '-C', source, 'fetch', '--depth', '1', 'origin', revision])
        run(['git', '-C', source, 'checkout', '--detach', 'FETCH_HEAD'], output=True)
    if run(['git', '-C', source, 'rev-parse', 'HEAD'], output=True).decode().strip() != revision:
        raise RuntimeError('Candidate source checkout does not match its container image')
    scripts = release / 'scripts'; scripts.mkdir(exist_ok=True)
    shutil.copy2(ROOT / 'scripts/container-adapter.py', scripts / 'container-adapter.py')
    base = json.loads(docker('image', 'inspect', 'python:3.11-slim', output=True))[0]['RepoDigests'][0]
    definition = (ROOT / 'scripts/adapter.Dockerfile').read_text()
    definition = definition.replace('FROM python:3.11-slim', 'FROM ' + base)
    (scripts / 'adapter.Dockerfile').write_text(definition)
    shutil.copy2(ROOT / '.dockerignore', release / '.dockerignore')
    adapter_tag = 'nomad-local/ai-adapter:' + revision[:12] + '-' + stamp().lower()
    docker('build', '-t', adapter_tag, '-f', scripts / 'adapter.Dockerfile', release)
    # Some Mac port images pair MapLibre 6 with a wrapper that still calls
    # removed APIs. Build a matching frontend with the upstream wrapper fix,
    # before pausing or changing any live services.
    from map_frontend import prepare as prepare_map_frontend
    frontend = prepare_map_frontend(digest, source)
    saved = backup()
    # Exercise database and vector restoration before touching the live deployment.
    test_restore(saved)
    updated = json.loads(json.dumps(current))
    for service in ['admin', 'worker']:
        updated['services'][service]['image'] = digest
    volumes = updated['services']['admin']['volumes']
    volumes[:] = [v for v in volumes if ':/app/public/assets' not in v]
    if frontend:
        volumes.append(str(frontend) + ':/app/public/assets:ro')
    adapter = updated['services']['ai-adapter']
    adapter['image'] = adapter_tag
    adapter['build']['context'] = str(release)
    adapter['volumes'] = [str(source / 'install/macos/omlx-proxy/vendor') + ':/adapter:ro'
                          if ':/adapter' in v else v for v in adapter['volumes']]
    try:
        write_json(COMPOSE, updated)
        dc('up', '-d', '--no-deps', '--force-recreate', 'ai-adapter', 'admin', 'worker')
        wait_for(lambda: http('http://127.0.0.1:8080/api/health'))
        if not health():
            raise RuntimeError('Candidate failed readiness checks')
        write_json(ROOT / 'state/latest-update.json', {'at': dt.datetime.now(dt.timezone.utc).isoformat(),
                   'rollback_backup': str(saved), 'from': active['Id'], 'to': digest, 'source_revision': revision})
        print('Update verified. Roll back with: nomad rollback')
    except BaseException:
        print('Update failed; restoring its recovery backup...', file=sys.stderr)
        write_json(COMPOSE, current)
        restore_backup(saved, safety_backup=False)
        raise


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('backup'); p.add_argument('--destination')
    p = sub.add_parser('verify-backup'); p.add_argument('path')
    p = sub.add_parser('test-restore'); p.add_argument('path')
    p = sub.add_parser('backup-sync'); p.add_argument('path', nargs='?')
    sub.add_parser('check')
    sub.add_parser('migrate-qdrant')
    sub.add_parser('repair-maps')
    p = sub.add_parser('restore'); p.add_argument('path')
    p = sub.add_parser('rollback'); p.add_argument('path', nargs='?')
    p = sub.add_parser('update'); p.add_argument('--check', action='store_true'); p.add_argument('--reapply', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    with (ROOT / 'state/.maintenance.lock').open('a') as lock:
        if args.command != 'check':
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command == 'backup': backup(args.destination)
        elif args.command == 'verify-backup': check_backup(args.path); print('Backup checksums passed.')
        elif args.command == 'test-restore': test_restore(args.path)
        elif args.command == 'backup-sync':
            path = Path(args.path or json.loads((ROOT / 'state/latest-backup.json').read_text())['path'])
            destination = json.loads(POLICY.read_text())['backup_destination']
            check_backup(path); mirror_backup(path, Path(destination))
        elif args.command == 'check':
            if not health(): raise SystemExit(1)
        elif args.command == 'migrate-qdrant': migrate_qdrant()
        elif args.command == 'repair-maps': repair_maps()
        elif args.command == 'restore': restore_backup(args.path)
        elif args.command == 'rollback':
            path = args.path or json.loads((ROOT / 'state/latest-update.json').read_text())['rollback_backup']
            restore_backup(path)
        elif args.command == 'update': update(args.check, args.reapply)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('NOMAD: ' + str(exc), file=sys.stderr)
        raise SystemExit(1)
