#!/bin/bash
if [[ -z "${NOMAD_ROOT:-}" ]]; then
  if [[ -n "${BASH_VERSION:-}" ]]; then
    nomad_environment_source="${BASH_SOURCE[0]}"
  elif [[ -n "${ZSH_VERSION:-}" ]]; then
    nomad_environment_source="${(%):-%x}"
  else
    echo 'Set NOMAD_ROOT before sourcing this file in this shell.' >&2
    return 1 2>/dev/null || exit 1
  fi
  export NOMAD_ROOT="$(cd -- "$(dirname -- "$nomad_environment_source")/.." && pwd)"
  unset nomad_environment_source
fi
export NOMAD_ROOT
if ! /usr/bin/python3 "$NOMAD_ROOT/scripts/drive-guard.py"; then
  return 1 2>/dev/null || exit 1
fi
export PATH="$NOMAD_ROOT/bin:$NOMAD_ROOT/runtime/lima/bin:/opt/homebrew/bin:$PATH:/usr/bin:/bin:/usr/sbin:/sbin"
export COLIMA_HOME="$NOMAD_ROOT/runtime/colima"
export COLIMA_PROFILE=nomad
export COLIMA_CACHE_HOME="$NOMAD_ROOT/cache/colima"
export LIMA_HOME="$NOMAD_ROOT/runtime/colima/_lima"
export DOCKER_CONFIG="$NOMAD_ROOT/docker"
export DOCKER_CONTEXT=colima-nomad
export XDG_CACHE_HOME="$NOMAD_ROOT/cache"
export TMPDIR="$NOMAD_ROOT/tmp/"
export PIP_CACHE_DIR="$NOMAD_ROOT/cache/pip"
export PYTHONDONTWRITEBYTECODE=1
