"""Rebuild affected map frontends from the image's exact source, in isolation."""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import urllib.request
import uuid

ROOT = Path(os.environ.get('NOMAD_ROOT', Path(__file__).resolve().parents[1]))
WRAPPER_VERSION = '8.1.3'
WRAPPER_INTEGRITY = 'sha512-5Ntvp8YP94SBRaJXLolw6Q57CtrPdWYxdCIo+JeI63hM9BPEJ1efLcPgfSAAUhzXanDeorTE91GsZAgoXbnB3g=='


def run(*args, stdout=subprocess.PIPE):
    result = subprocess.run([str(a) for a in args], stdout=stdout, stderr=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors='replace')[-3000:])
    return result.stdout


def download(url, integrity, filename):
    destination = ROOT / 'downloads' / filename
    algorithm, digest = integrity.split('-', 1)
    def valid(path):
        return path.exists() and base64.b64encode(hashlib.new(algorithm, path.read_bytes()).digest()).decode() == digest
    if not valid(destination):
        temporary = destination.with_suffix('.partial')
        with urllib.request.urlopen(url, timeout=60) as response, temporary.open('wb') as output:
            shutil.copyfileobj(response, output)
        if not valid(temporary):
            raise RuntimeError('Frontend build dependency failed integrity verification: ' + filename)
        temporary.replace(destination)
    return destination


def prepare(image, source):
    source = Path(source)
    metadata = json.loads(run('docker', 'image', 'inspect', image))[0]
    revision = metadata['Config'].get('Labels', {}).get('org.opencontainers.image.revision')
    actual = run('git', '-C', source, 'rev-parse', 'HEAD').decode().strip()
    if actual != revision:
        raise RuntimeError('Map frontend source does not match the app image')
    versions = json.loads(run('docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'node', image,
        '-e', "console.log(JSON.stringify({map:require('/app/node_modules/maplibre-gl/package.json').version,"
        "wrapper:require('/app/node_modules/@vis.gl/react-maplibre/package.json').version}))"))
    wrapper = tuple(int(n) for n in versions['wrapper'].split('.'))
    overlay_root = ROOT / 'scripts/frontend-overrides'
    overlays = sorted(p for p in overlay_root.rglob('*') if p.is_file()) if overlay_root.exists() else []
    overlay_hash = hashlib.sha256()
    for p in overlays:
        relative = p.relative_to(overlay_root)
        if not (str(relative).startswith(('inertia/', 'types/')) or str(relative) == 'vite.config.ts'):
            raise RuntimeError('Unexpected frontend override path: ' + str(relative))
        overlay_hash.update(str(relative).encode() + b'\0' + p.read_bytes())
    overlay_id = overlay_hash.hexdigest()[:16] if overlays else None
    needs_wrapper_fix = int(versions['map'].split('.')[0]) >= 6 and wrapper < (8, 1, 3)
    if not needs_wrapper_fix and not overlays:
        return None  # An upstream compatible pair needs no local frontend overlay.
    if needs_wrapper_fix and (wrapper < (8, 1, 0) or int(versions['map'].split('.')[0]) != 6):
        raise RuntimeError('Untested map dependency pair; leaving the live frontend intact')
    destination = source.parent / ('frontend-maplibre-' + WRAPPER_VERSION + ('-' + overlay_id if overlay_id else ''))
    record = destination / 'build.json'
    if record.exists():
        cached = json.loads(record.read_text())
        if cached['image_id'] == metadata['Id'] and all(
            (destination / name).is_file() and hashlib.sha256((destination / name).read_bytes()).hexdigest() == digest
            for name, digest in cached['sha256'].items()
        ):
            return destination / 'assets'
        raise RuntimeError('Cached map frontend failed verification; preserve it and rebuild into a new directory')
    package = download('https://registry.npmjs.org/@vis.gl/react-maplibre/-/react-maplibre-8.1.3.tgz',
                       WRAPPER_INTEGRITY, 'vis.gl-react-maplibre-8.1.3.tgz')
    lock = json.loads((source / 'admin/package-lock.json').read_text())
    tsconfig = lock['packages']['node_modules/@adonisjs/tsconfig']
    config_package = download(tsconfig['resolved'], tsconfig['integrity'],
                              'adonisjs-tsconfig-' + tsconfig['version'] + '.tgz')
    name = 'nomad-map-build-' + uuid.uuid4().hex[:10]
    stage = destination.with_name(destination.name + '.tmp-' + uuid.uuid4().hex[:8])
    stage.mkdir(parents=True)
    log = ROOT / 'logs' / ('map-frontend-build-' + revision[:12] + '.log')
    try:
        run('docker', 'run', '-d', '--name', name, '--network', 'none', '--entrypoint', 'sleep', image, '600')
        for directory in ['inertia', 'types']:
            run('docker', 'cp', source / 'admin' / directory, name + ':/app/' + directory)
        for p in overlays:
            run('docker', 'cp', p, name + ':/app/' + str(p.relative_to(overlay_root)))
        run('docker', 'cp', package, name + ':/tmp/map-wrapper.tgz')
        run('docker', 'cp', config_package, name + ':/tmp/tsconfig.tgz')
        command = (('tar -xzf /tmp/map-wrapper.tgz -C node_modules/@vis.gl/react-maplibre --strip-components=1 && '
                    if needs_wrapper_fix else '') +
                   'mkdir -p node_modules/@adonisjs/tsconfig && '
                   'tar -xzf /tmp/tsconfig.tgz -C node_modules/@adonisjs/tsconfig --strip-components=1 && '
                   'NODE_ENV=production node node_modules/vite/bin/vite.js build ' +
                   ('--config vite.config.ts ' if (overlay_root / 'vite.config.ts').exists() else '') + '&& '
                   'cp node_modules/maplibre-gl/dist/maplibre-gl-worker.mjs public/assets/')
        with log.open('wb') as output:
            run('docker', 'exec', name, 'sh', '-c', command, stdout=output)
        run('docker', 'cp', name + ':/app/public/assets', stage / 'assets')
        checksums = {str(p.relative_to(stage)): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in (stage / 'assets').rglob('*') if p.is_file()}
        (stage / 'build.json').write_text(json.dumps({'image_id': metadata['Id'], 'source_revision': revision,
            'maplibre': versions['map'], 'original_wrapper': versions['wrapper'],
            'wrapper': WRAPPER_VERSION if needs_wrapper_fix else versions['wrapper'],
            'overlay_id': overlay_id, 'sha256': checksums}, indent=2) + '\n')
        stage.rename(destination)
        return destination / 'assets'
    finally:
        subprocess.run(['docker', 'rm', '-f', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if stage.exists():
            shutil.rmtree(stage)
