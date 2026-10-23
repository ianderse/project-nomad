#!/usr/bin/env python3
"""Provision a new external-drive NOMAD installation on an Apple Silicon Mac."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import secrets
import shutil
import socket
import subprocess
import tarfile
import time
import urllib.request

PROFILE = Path(__file__).resolve().parent


def run(args, env=None, capture=False, data=None):
    return subprocess.run([str(a) for a in args], env=env, input=data, check=True,
                          stdout=subprocess.PIPE if capture else None).stdout


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(4 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2) + '\n')


def volume_for(root):
    existing = root
    while not os.path.ismount(existing):
        if existing == existing.parent:
            raise RuntimeError('Cannot find the installation volume.')
        existing = existing.parent
    info = plistlib.loads(run(['/usr/sbin/diskutil', 'info', '-plist', existing], capture=True))
    mount = Path(info.get('MountPoint', '/')).resolve()
    if (info.get('Internal', True) or info.get('FilesystemType') != 'apfs'
            or not info.get('VolumeUUID') or not os.path.ismount(mount)
            or not root.is_relative_to(mount) or root == mount):
        raise RuntimeError('Choose a folder on a mounted external APFS volume.')
    if any(c in str(root) for c in [':', '\n', '\r', '$']):
        raise RuntimeError('The installation path cannot contain :, $, or a newline.')
    return {'mount': str(mount), 'uuid': info['VolumeUUID']}


def fresh_root(root):
    # Resuming operations on an existing installation belongs to nomad/restore.
    # Refuse before downloading or overwriting any credentials or VM state.
    if root.exists() and any(root.iterdir()):
        raise RuntimeError('Installation folder is not empty. Use nomad start or restore for existing data.')


def check_socket_path(root):
    socket_path = root / 'runtime/colima/_lima/colima-nomad/ssh.sock.1234567890123456'
    if len(os.fsencode(socket_path)) >= 104:
        raise RuntimeError('Installation path is too long for macOS VM sockets. Choose a shorter external folder path.')


def fetch(tool, downloads):
    destination = downloads / tool['file']
    if destination.exists() and digest(destination) == tool['sha256']:
        return destination
    temporary = destination.with_suffix(destination.suffix + '.part')
    print('Downloading ' + tool['url'], flush=True)
    with urllib.request.urlopen(tool['url'], timeout=60) as src, temporary.open('wb') as dst:
        shutil.copyfileobj(src, dst, 4 * 1024 * 1024)
        dst.flush()
        os.fsync(dst.fileno())
    if digest(temporary) != tool['sha256']:
        raise RuntimeError('Downloaded checksum mismatch: ' + tool['file'])
    temporary.replace(destination)
    return destination


def unpack(archive, destination):
    # Archives are pinned official releases. Also reject escaping paths/links.
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            target = (destination / member.name).resolve()
            if not target.is_relative_to(destination.resolve()):
                raise RuntimeError('Archive path escapes its destination')
            if member.issym() or member.islnk():
                link = (target.parent / member.linkname if member.issym()
                        else destination / member.linkname).resolve()
                if not link.is_relative_to(destination.resolve()):
                    raise RuntimeError('Archive link escapes its destination')
        tar.extractall(destination)


def environment(root):
    env = dict(os.environ)
    env.update(NOMAD_ROOT=str(root), COLIMA_HOME=str(root / 'runtime/colima'),
               COLIMA_PROFILE='nomad', COLIMA_CACHE_HOME=str(root / 'cache/colima'),
               LIMA_HOME=str(root / 'runtime/colima/_lima'), DOCKER_CONFIG=str(root / 'docker'),
               DOCKER_CONTEXT='colima-nomad', XDG_CACHE_HOME=str(root / 'cache'),
               TMPDIR=str(root / 'tmp') + '/', PIP_CACHE_DIR=str(root / 'cache/pip'),
               PYTHONDONTWRITEBYTECODE='1')
    env['PATH'] = str(root / 'bin') + ':' + str(root / 'runtime/lima/bin') + ':' + env['PATH']
    return env


def configuration(root, source, image):
    # Replace only path placeholders, leaving credentials for Compose's env file.
    def expand(value):
        if isinstance(value, str):
            return value.replace('${NOMAD_ROOT}', str(root)).replace('${NOMAD_RELEASE_SOURCE}', str(source))
        if isinstance(value, list):
            return [expand(v) for v in value]
        if isinstance(value, dict):
            return {k: expand(v) for k, v in value.items()}
        return value
    config = expand(json.loads((PROFILE / 'compose.template.json').read_text()))
    for service in ['admin', 'worker']:
        config['services'][service]['image'] = image
    config['volumes']['qdrant-data']['name'] = 'nomad-qdrant-' + secrets.token_hex(6)
    return config


def wait(check, seconds=180):
    end = time.monotonic() + seconds
    while True:
        try:
            return check()
        except Exception:
            if time.monotonic() >= end:
                raise
            time.sleep(2)


def install(args, lock, storage):
    root = args.root
    key = args.omlx_key_file.read_text().strip()
    if not key or '\n' in key:
        raise RuntimeError('The oMLX key file must contain one API key.')
    # Read-only discovery; never load, unload, configure, or test-generate a model.
    req = urllib.request.Request('http://127.0.0.1:8000/v1/models',
                                 headers={'Authorization': 'Bearer ' + key})
    with urllib.request.urlopen(req, timeout=10) as response:
        models = json.load(response).get('data', [])
    names = {m['id'] for m in models}
    for selected in [args.chat_model, args.tasks_model]:
        if selected and selected not in names:
            raise RuntimeError('Selected model is not advertised by the existing oMLX server: ' + selected)
    if not args.prepare_only:
        for port in [8080, 11435, 9999]:
            with socket.socket() as check:
                check.bind(('127.0.0.1', port))
    os.umask(0o077)
    root.mkdir(parents=True, exist_ok=True)
    for name in ['bin', 'runtime/lima', 'runtime/colima', 'docker/cli-plugins',
                 'downloads', 'state', 'storage', 'storage/logs', 'storage/tmp',
                 'storage/zim', 'storage/maps', 'storage/kb_uploads',
                 'backups', 'cache', 'logs', 'tmp']:
        (root / name).mkdir(parents=True, exist_ok=True)
    write_json(root / 'state/storage.json', storage)
    (root / 'state/INSTALL-INCOMPLETE').write_text('Provisioning has not completed. See installer output.\n')
    for name in ['scripts', 'nomad']:
        src, dst = PROFILE / name, root / name
        if src.is_dir():
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns('__pycache__'))
        else:
            shutil.copy2(src, dst)
    (root / 'nomad').chmod(0o700)
    shutil.copy2(PROFILE / 'dockerignore.template', root / '.dockerignore')
    write_json(root / 'state/toolchain-lock.json', lock)
    files = {name: fetch(spec, root / 'downloads') for name, spec in lock['tools'].items()}
    shutil.copy2(files['colima'], root / 'bin/colima')
    unpack(files['lima'], root / 'runtime/lima')
    unpack(files['docker'], root / 'tmp')
    shutil.copy2(root / 'tmp/docker/docker', root / 'bin/docker')
    shutil.copy2(files['compose'], root / 'docker/cli-plugins/docker-compose')
    for path in [root / 'bin/colima', root / 'bin/docker', root / 'docker/cli-plugins/docker-compose']:
        path.chmod(0o700)
    env = environment(root)
    source = root / 'releases' / lock['source_revision'] / 'source'
    source.mkdir(parents=True)
    run(['git', 'init', source], env)
    run(['git', '-C', source, 'remote', 'add', 'origin', lock['source_repo']], env)
    run(['git', '-C', source, 'fetch', '--depth', '1', 'origin', lock['source_revision']], env)
    run(['git', '-C', source, 'checkout', '--detach', 'FETCH_HEAD'], env)
    actual = run(['git', '-C', source, 'rev-parse', 'HEAD'], env, capture=True).decode().strip()
    if actual != lock['source_revision']:
        raise RuntimeError('Source revision differs from the pinned image.')
    # Adapter build context includes requirements, never credentials or weights.
    build_source = root / 'source/install/macos/omlx-proxy/vendor'
    build_source.mkdir(parents=True)
    shutil.copy2(source / 'install/macos/omlx-proxy/vendor/requirements.txt', build_source)
    definition = root / 'scripts/adapter.Dockerfile'
    definition.write_text(definition.read_text().replace('FROM python:3.11-slim', 'FROM ' + lock['adapter_base']))
    (root / 'state/omlx-key').write_text(key + '\n')
    chip = run(['/usr/sbin/sysctl', '-n', 'machdep.cpu.brand_string'], capture=True).decode().strip()
    (root / '.env').write_text('APP_KEY=' + secrets.token_hex(32) + '\nDB_PASSWORD=' + secrets.token_hex(32)
                             + '\nMYSQL_ROOT_PASSWORD=' + secrets.token_hex(32)
                             + '\nAPPLE_CHIP_MODEL=' + chip + '\nAPPLE_GPU_MODEL=' + chip + '\n')
    shutil.copy2(PROFILE / 'model-map.example.json', root / 'state/model-map.json')
    policy = json.loads((PROFILE / 'maintenance.example.json').read_text())
    # Backup share is configured after Finder authentication, not guessed.
    for field in ['backup_destination', 'backup_mount', 'backup_share', 'backup_server']:
        policy.pop(field, None)
    policy.update(app_image_tag=lock['image'].split('@')[0] + ':edge', source_repo=lock['source_repo'])
    write_json(root / 'state/maintenance.json', policy)
    config = configuration(root, source, lock['image'])
    write_json(root / 'compose.json', config)
    run(['/usr/bin/python3', root / 'scripts/host-system.py'], env)
    if args.prepare_only:
        print('Prepared files and verified tools/source. VM and services have not been started.')
        return
    finish_install(args, lock, source, env)


def finish_install(args, lock, source, env):
    root = args.root
    config = json.loads((root / 'compose.json').read_text())
    app_url = config['services']['admin']['environment']['URL']
    run(['colima', 'start', '--vm-type', 'vz', '--mount-type', 'virtiofs', '--cpus', args.cpus,
         '--memory', args.memory, '--disk', args.disk, '--mount', str(root) + ':w', '--ssh-config=false'], env)
    run(['docker', 'volume', 'create', config['volumes']['qdrant-data']['name']], env)
    compose = ['docker', 'compose', '--env-file', root / '.env', '-f', root / 'compose.json']
    run(compose + ['pull', '--ignore-buildable'], env)
    details = json.loads(run(['docker', 'image', 'inspect', lock['image']], env, capture=True))[0]
    if (details['Architecture'] != 'arm64' or details['Config']['Labels'].get(
            'org.opencontainers.image.revision') != lock['source_revision']):
        raise RuntimeError('Image architecture or source revision differs from the lock.')
    run(compose + ['build', 'ai-adapter'], env)
    run(compose + ['up', '-d', 'admin', 'mysql', 'redis', 'embeddings', 'ai-adapter', 'qdrant', 'dozzle'], env)
    wait(lambda: urllib.request.urlopen(app_url + '/api/health', timeout=5).close())
    run(['docker', 'exec', 'nomad_embeddings', 'ollama', 'pull', 'nomic-embed-text:v1.5'], env)
    import sys
    sys.path.insert(0, str(root / 'scripts'))
    os.environ.update(env)
    import maintenance as m
    volume = config['volumes']['qdrant-data']['name']
    binding = json.dumps({'Env': ['QDRANT__TELEMETRY_DISABLED=true'],
                          'HostConfig': {'Binds': [volume + ':/qdrant/storage'],
                                         'RestartPolicy': {'Name': 'unless-stopped'}}})
    # Keep Supply Depot from replacing the native Linux index volume.
    m.sql("UPDATE services SET installed=1,is_user_modified=1,container_config=CONVERT(0x"
          + binding.encode().hex() + " USING utf8mb4),container_image='" + config['services']['qdrant']['image']
          + "' WHERE service_name='nomad_qdrant'; UPDATE services SET installed=1,is_user_modified=1 "
            "WHERE service_name='nomad_ollama';")
    for name, value in [('chat.lastModel', args.chat_model), ('ai.tasksModel', args.tasks_model)]:
        if value:
            m.sql("INSERT INTO kv_store (`key`,value,created_at,updated_at) VALUES ('" + name
                  + "',CONVERT(0x" + value.encode().hex() + " USING utf8mb4),NOW(),NOW()) "
                    "ON DUPLICATE KEY UPDATE value=VALUES(value),updated_at=NOW();")
    run(compose + ['up', '-d', 'worker'], env)
    write_json(root / 'state/installation.json', {'source_revision': lock['source_revision'], 'image': lock['image'],
                                               'storage': json.loads((root / 'state/storage.json').read_text())})
    (root / 'state/INSTALL-INCOMPLETE').unlink()
    print('NOMAD is ready at ' + app_url + '. Add offline content through the app.')
    print('Start and stop it with: ' + str(root / 'nomad') + ' start|stop')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=lambda s: Path(s).expanduser().resolve(), required=True)
    parser.add_argument('--plan', action='store_true', help='Read-only preflight and pinned deployment plan')
    parser.add_argument('--prepare-only', action='store_true', help='Download tools/source and write configuration without starting a VM')
    parser.add_argument('--omlx-key-file', type=Path)
    parser.add_argument('--chat-model')
    parser.add_argument('--tasks-model')
    parser.add_argument('--cpus', type=int, default=4)
    parser.add_argument('--memory', type=int, default=8, help='VM memory in GiB')
    parser.add_argument('--disk', type=int, default=1000, help='Sparse VM disk limit in GiB')
    args = parser.parse_args()
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        parser.error('Requires an Apple Silicon Mac with Apple Command Line Tools installed.')
    if min(args.cpus, args.memory, args.disk) <= 0:
        parser.error('VM allocations must be positive.')
    lock = json.loads((PROFILE / 'release-lock.json').read_text())
    if '@sha256:' not in lock['image'] or len(lock['source_revision']) != 40:
        parser.error('A published image digest and matching source revision are required.')
    storage = volume_for(args.root)
    check_socket_path(args.root)
    fresh_root(args.root)
    if args.plan:
        print(json.dumps({'root': str(args.root), 'storage': storage, 'image': lock['image'],
                          'source_revision': lock['source_revision'], 'tools': lock['tools'],
                          'vm': {'cpus': args.cpus, 'memory_gib': args.memory, 'disk_limit_gib': args.disk},
                          'writes': False}, indent=2))
        return
    if not args.omlx_key_file:
        parser.error('--omlx-key-file is required; its contents stay private.')
    install(args, lock, storage)


if __name__ == '__main__':
    main()
