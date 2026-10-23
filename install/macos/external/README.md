# External Colima profile

This fork tracks `caweis/project-nomad`. This optional profile captures a tested
Apple Silicon deployment with the Colima VM, databases, tools, caches, and content
on an external APFS drive. Docker Engine runs inside Colima; chat uses an existing
authenticated native oMLX server. Embeddings use a separate CPU Ollama container.
The stock Mac installer elsewhere in this repository uses a different layout.

## Files and deployment boundary

- `nomad` and `scripts/`: lifecycle, drive guard, consistent backups, isolated
  restore verification, controlled updates/rollback, storage reporting, and map repair.
- `compose.template.json`: pinned ARM64 services with external bind mounts and
  a native Linux Qdrant volume inside the externally stored VM disk.
- `.env.example`, `maintenance.example.json`, `model-map.example.json`: templates,
  with no deployed credentials, machine UUID, private server address, or model weights.

## Fresh installation

Install Apple's Command Line Tools and start an authenticated native oMLX server
first. Clone this fork onto the external APFS drive, then run a read-only preflight
with an unused installation folder:

```sh
python3 install/macos/external/bootstrap.py --root /Volumes/YOUR-DRIVE/project-nomad --plan
```

Install with the existing oMLX API key supplied in a private file:

```sh
python3 install/macos/external/bootstrap.py \
  --root /Volumes/YOUR-DRIVE/project-nomad \
  --omlx-key-file /path/to/private-key-file \
  --chat-model YOUR_EXISTING_CHAT_MODEL --tasks-model YOUR_EXISTING_TASK_MODEL
```

`release-lock.json` pins the published application digest, matching source revision,
Python base image, and official ARM64 Colima/Lima/Docker/Compose downloads with
SHA-256 checksums. Downloads, extracted tools, caches, configuration, credentials,
source and VM state stay under the selected external folder. No Homebrew install
or Docker Desktop is required. The installer verifies the external APFS UUID,
refuses a nonempty installation folder or a path too long for macOS VM sockets,
creates fresh secrets, and never changes
native oMLX configuration or loaded chat models. It downloads only the separate
CPU embedding model. Add library and map content through NOMAD after installation.

The VM defaults to VZ/virtiofs, 4 CPUs, 8 GiB RAM, and a sparse 1000 GiB disk limit;
`--cpus`, `--memory`, and `--disk` override these allocations. External free space is
a separate limit. No startup agent or automatic schedule is installed. A failed
installation leaves `state/INSTALL-INCOMPLETE` for diagnosis and refuses to silently
replace that partial installation on a rerun. Use another empty folder or inspect
and remove only the failed installation's generated data before retrying.

`--prepare-only` downloads and verifies tools/source and writes configuration for
inspection without starting a VM; it intentionally leaves the incomplete marker.
It is intended for installer verification, not for upgrading an existing install.
For an existing installation, use the operations commands below.

Keep deployment state outside this checkout. Under an external deployment root,
the scripts expect `bin/`, `runtime/lima/`, `runtime/colima/`, `docker/`, `storage/`,
`state/`, `downloads/`, `backups/`, `cache/`, `logs/`, `tmp/`, `source/`, `.env`,
and `compose.json`. `NOMAD_ROOT` selects that root; installed scripts otherwise
resolve it from their location. Copy the profile's `nomad` and `scripts/` there.
Copy `dockerignore.template` to the deployment root as `.dockerignore`; the adapter
build context must exclude runtime state, credentials, backups and downloaded data.
Avoid replacing an existing deployment's secrets, database paths or named volumes.

Record the intended volume as `state/storage.json`:

```json
{"mount": "/Volumes/YOUR-DRIVE", "uuid": "YOUR-APFS-VOLUME-UUID"}
```

Get that UUID with `diskutil info /Volumes/YOUR-DRIVE`. The guard rejects a missing,
different, or internal drive before any storage operations. Always stop NOMAD
before ejecting its drive. The VM cannot survive removal of its backing disk.

For a new deployment, expand `${NOMAD_ROOT}` and `${NOMAD_RELEASE_SOURCE}` in the
Compose template into absolute external paths and save the result as `compose.json`.
Keep credential substitutions (`${APP_KEY}`, `${DB_PASSWORD}`, etc.) as environment
references. The release source must match the pinned admin image's OCI revision;
see `release-lock.json` for the shipped revision/digest pair.
The adapter Dockerfile expects that source checkout at `<root>/source` when built.
Keep `.env` and the existing oMLX key at `state/omlx-key` private (mode 600).
Create `state/model-map.json` from the empty example if no mapping is needed.
The native API endpoint defaults to `host.lima.internal:8000` inside the VM.
The profile never reloads oMLX or manages its loaded chat models.

`host-system.py` captures static macOS/chip/core/unified-memory information at
startup and checks. System Details uses that host record, while CPU, RAM, swap
and allocation gauges remain clearly labelled as VM measurements. No stale host
usage figure is presented as live memory telemetry. The host record is deployment
data and is not committed.

Dozzle supplies the Service Logs & Metrics link on loopback port 9999. The
external template includes its pinned ARM64 image with shell/actions disabled;
logs and container metrics remain available without changing inference state.

The Qdrant volume is external to Compose: create it explicitly for a fresh
installation, or restore/migrate the existing index. Never point an existing
deployment at a new empty volume to resolve a configuration error.

## Recovery and updates

Copy `maintenance.example.json` to `state/maintenance.json` and set the separate
SMB share and mount. Finder/Keychain handles Windows authentication; the scripts
store no SMB password. A missing share does not become an internal fallback folder.

```sh
./nomad check
./nomad backup
./nomad backup-sync
./nomad test-restore /Volumes/YOUR-DRIVE/project-nomad/backups/BACKUP
./nomad update --check
./nomad update
./nomad rollback
```

Backups briefly pause application containers for a consistent snapshot, then resume
them before hashing and network copying. SQL, Qdrant, Redis, configuration, credentials and
content are backed up; VM images and downloadable embedding weights are excluded.
Transfers resume and verify SHA-256. `test-restore` uses isolated databases and
indexes, including a full vector/payload read audit. During an update, candidate
database migrations and seeders also run against the isolated restored database
before live changes. `restore` replaces live state and first makes a rescue backup. No automatic
backup/update schedule is enabled.

Updates pin an ARM64 image digest, check out its matching source, build a matching
adapter, prepare any compatibility frontend, create and test a recovery backup,
then apply and check readiness. The example tracks this fork's published image; `app_image_tag` and `source_repo` select a
different image/source pair together.

## Map repairs

The source build uses react-map-gl 8.1.3, whose MapLibre wrapper supports v6's
camera API. Vite explicitly publishes the matching `maplibre-gl-worker.mjs`.
The old image omitted that worker and paired MapLibre 6 with wrapper 8.1.0,
causing a blank map and broken camera movement respectively.

`map_frontend.py` rebuilds affected prebuilt images from their matching source
with the upstream wrapper fix. Build packages have pinned integrity checks;
compilation uses an isolated container without production credentials/network.
Generated assets and hashes stay in the deployment's release directory, outside Git.
To retain this fork's UI fixes while using the pinned upstream image, stage them
from a reviewed checkout before running a controlled update:

```sh
python3 install/macos/external/capture-frontend.py \
  --source-repo /path/to/this/checkout --root /Volumes/YOUR-DRIVE/project-nomad
```

The builder overlays those selected source files onto the candidate image's exact
source and includes their content hash in the build identity. They are backed up
with the deployment's scripts. Compatible upstream dependencies are retained;
older MapLibre wrappers receive the pinned fix. Compilation failures occur before
live changes, and a changed source overlay cannot reuse a stale asset cache.

The startup preload supplies a missing worker and reports virtiofs content capacity
using its fundamental fragment size. Readiness checks worker MIME/hash, glyphs,
sprites and a decoded regional vector tile. WebKit/Chromium checks also exercised
street labels, zooming, dragging, and saved camera state on the live deployment.

## Windows backup verification

Copy `Verify-NOMAD-Backups.cmd` and `.ps1` beside the backup directories on the
Windows PC and double-click the CMD file there. It checks the newest backup's
archive and database/index snapshots against its manifest using the Windows filesystem, then
saves `NOMAD-backup-hash-results.json` and `.txt` beside the script. This is useful
when diagnosing a disagreement between a Mac SMB read and the stored server file.
The script changes only those reports; it never repairs or replaces a backup.
A failed checksum must remain a failed backup until a valid copy is made.
Pass `-AllBackups` to the PowerShell script to check older generations too.

## Drive sleep and disconnects

If an external drive disconnects, preserve a recovery copy before repairing its
filesystem. Check the Mac disk-sleep timer with `pmset -g custom`; disable that
timer with `sudo pmset -a disksleep 0` using Mac administrator authentication.
Some Mac Studio versions do not show this control in System Settings. Disabling
the timer removes one possible trigger; cable, port, enclosure and filesystem
problems still need separate diagnosis. [Sabrent
troubleshooting](https://sabrent.com/blogs/troubleshooting/connection-and-power-problems)
also recommends disabling disk sleep when investigating connection drops.
