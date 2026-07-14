#!/usr/bin/env bash

set -euo pipefail

SOURCE_DIR="${HOME}/PycharmProjects"
BACKUP_VOLUME_DIR="/Volumes/Elements"
BACKUP_DIR="${BACKUP_VOLUME_DIR}/snapback"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SNAPBACK_SCRIPT="${SCRIPT_DIR}/snapback.py"

if [[ ! -d "${SOURCE_DIR}" ]]; then
    printf 'Source directory not found: %s\n' "${SOURCE_DIR}" >&2
    exit 1
fi

if [[ ! -d "${BACKUP_VOLUME_DIR}" ]]; then
    printf 'Backup volume is not mounted: %s\n' "${BACKUP_VOLUME_DIR}" >&2
    exit 1
fi

mkdir -p "${BACKUP_DIR}"

python3 "${SNAPBACK_SCRIPT}" backup \
    --src "${SOURCE_DIR}" \
    --dest "${BACKUP_DIR}" \
    --full
