// The pinned app build omits MapLibre 6's separate worker from public/assets.
// Publish the worker supplied by the SAME installed dependency, so recreations
// and upgrades retain offline map rendering without a stale vendored version.
// Leave a worker emitted by an upstream fixed build intact.
const fs = require('node:fs');
const path = require('node:path');
const mapWorker = '/app/node_modules/maplibre-gl/dist/maplibre-gl-worker.mjs';
const publicWorker = '/app/public/assets/maplibre-gl-worker.mjs';
if (fs.existsSync(mapWorker) && !fs.existsSync(publicWorker)) {
  fs.mkdirSync(path.dirname(publicWorker), { recursive: true });
  const temporary = publicWorker + '.' + process.pid + '.tmp';
  fs.copyFileSync(mapWorker, temporary);
  fs.renameSync(temporary, publicWorker);
}

// systeminformation omits virtiofs mounts on Linux. NOMAD's content-capacity
// selector needs the actual bind mount, while '/' remains the VM/index budget.
const { execFile } = require('node:child_process');
const { promisify } = require('node:util');
const execute = promisify(execFile);
const si = require('/app/node_modules/systeminformation');
const original = si.fsSize.bind(si);
const originalOsInfo = si.osInfo.bind(si);
si.osInfo = async function (...args) {
  const info = await originalOsInfo(...args);
  try {
    // Legacy pinned images carry this through osInfo; fork builds also expose
    // an explicit host field. Never replace VM memory/CPU usage with host RAM.
    const host = JSON.parse(fs.readFileSync('/app/storage/host-system.json', 'utf8'));
    if (host.os?.platform === 'darwin' && Number.isFinite(host.memoryTotal) && host.memoryTotal > 0) {
      info.nomadHost = host;
    }
  } catch (_) {}
  return info;
};
si.fsSize = async function (...args) {
  const entries = await original(...args);
  try {
    // virtiofs reports a 1 MiB preferred I/O size but counts 4 KiB fragments.
    // Node's statfs omits f_frsize; GNU stat %S exposes that fundamental size.
    const { stdout } = await execute('stat', ['-f', '-c', '%S %b %a %f', '/app/storage']);
    const [fragment, blocks, availableBlocks, freeBlocks] = stdout.trim().split(/\s+/).map(Number);
    if (![fragment, blocks, availableBlocks, freeBlocks].every(Number.isFinite) || fragment <= 0) return entries;
    const size = blocks * fragment;
    const available = availableBlocks * fragment;
    const used = (blocks - freeBlocks) * fragment;
    const content = {
      fs: 'External content', type: 'virtiofs', mount: '/app/storage',
      size, used, available, use: size ? 100 * used / size : 0, rw: true,
    };
    const index = entries.findIndex(entry => entry.mount === '/app/storage');
    if (index < 0) entries.push(content); else entries[index] = content;
  } catch (_) {
    // Keep the original filesystem inventory if the bind mount is unavailable.
  }
  return entries;
};
