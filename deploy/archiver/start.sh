#!/bin/sh
set -eu
umask 077

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
ENV_FILE=${PIXCHIVE_ENV_FILE:-$HERE/.env}
if [ -f "$ENV_FILE" ]; then
    # This file contains trusted, shell-compatible public metadata only.
    set -a
    . "$ENV_FILE"
    set +a
else
    ENV_FILE=/dev/null
fi
export PIXCHIVE_ROOT=${PIXCHIVE_ROOT:-/mnt/storage/pixchive}
export PIXCHIVE_CACHE_ROOT=${PIXCHIVE_CACHE_ROOT:-/cache/pixchive}

fail() { printf '%s\n' "$*" >&2; exit 1; }
compose() {
    docker compose --project-name pixchive --project-directory "$HERE" \
        --env-file "$ENV_FILE" -f "$HERE/compose.yml" "$@"
}

require_nfs() {
    filesystem=$(findmnt --noheadings --target "$1" --output FSTYPE) || fail 'Storage is not mounted.'
    case "$filesystem" in nfs|nfs4) ;; *) fail 'NFS TRIAL requires an actual NFS mount at every persistence path.' ;; esac
}

private_file() {
    [ -f "$1" ] && [ ! -L "$1" ] || fail "Required regular file missing: $1"
    [ "$(stat -c '%a' -- "$1")" = 600 ] || fail "Expected mode 600: $1"
    require_nfs "$1"
}

preflight() {
    [ "${PIXCHIVE_NFS_TRIAL:-}" = 1 ] || fail 'Set PIXCHIVE_NFS_TRIAL=1 to acknowledge the unsupported NFS TRIAL.'
    for image in "${PIXCHIVE_IMAGE:-}" "${PIXCHIVE_BROWSER_IMAGE:-}"; do
        case "$image" in
            ''|*:latest|*:local|*:main|*:edge) fail 'App and browser images need prepared fixed tags or digests.' ;;
            *@sha256:*) printf '%s\n' "$image" | grep -Eq '^.+@sha256:[0-9a-f]{64}$' || fail 'Invalid image digest.' ;;
            *:*) printf '%s\n' "$image" | grep -Eq '^.+:[A-Za-z0-9_][A-Za-z0-9_.-]*$' || fail 'Invalid fixed image tag.' ;;
            *) fail 'App and browser images need explicit fixed tags or digests.' ;;
        esac
    done
    case "$PIXCHIVE_ROOT" in /*) ;; *) fail 'PIXCHIVE_ROOT must be absolute.' ;; esac
    resolved=$(realpath -e -- "$PIXCHIVE_ROOT") || fail 'Storage root does not exist.'
    [ "$resolved" != / ] && [ "$resolved" = "$PIXCHIVE_ROOT" ] || fail 'Use a canonical, non-symlink storage root without a trailing slash.'
    require_nfs "$PIXCHIVE_ROOT"
    cache_resolved=$(realpath -e -- "$PIXCHIVE_CACHE_ROOT") || fail 'Prepare the host-local cache directory first.'
    [ "$cache_resolved" != / ] && [ "$cache_resolved" = "$PIXCHIVE_CACHE_ROOT" ] && [ ! -L "$PIXCHIVE_CACHE_ROOT" ] || fail 'Use a canonical, non-symlink local cache directory.'
    [ -d "$PIXCHIVE_CACHE_ROOT" ] && [ "$(stat -c '%a' -- "$PIXCHIVE_CACHE_ROOT")" = 700 ] || fail 'Cache directory must have mode 700.'
    cache_filesystem=$(findmnt --noheadings --target "$PIXCHIVE_CACHE_ROOT" --output FSTYPE) || fail 'Cache filesystem unavailable.'
    case "$cache_filesystem" in xfs|ext4|btrfs) ;; *) fail 'Cache requires a host-local XFS/ext4/btrfs filesystem.' ;; esac
    [ "$(stat -c '%a' -- "$PIXCHIVE_ROOT")" = 700 ] || fail 'Storage root must have mode 700.'
    for directory in data secrets browser-state redis backups security; do
        path=$PIXCHIVE_ROOT/$directory
        [ -d "$path" ] && [ ! -L "$path" ] || fail "Required directory missing: $path"
        [ "$(realpath -e -- "$path")" = "$path" ] || fail 'Persistence paths must stay under the storage root.'
        require_nfs "$path"
        [ "$(stat -c '%a' -- "$path")" = 700 ] || fail "Expected mode 700: $path"
        [ -r "$path" ] && [ -w "$path" ] && [ -x "$path" ] || fail "Storage directory is not accessible: $path"
    done
    private_file "$PIXCHIVE_ROOT/data/.pixchive-storage-id"
    private_file "$PIXCHIVE_ROOT/data/pixchive.db"
    [ -s "$PIXCHIVE_ROOT/data/pixchive.db" ] || fail 'Migrate the existing database before starting.'
    private_file "$PIXCHIVE_ROOT/secrets/credential.key"
    [ -s "$PIXCHIVE_ROOT/secrets/credential.key" ] || fail 'The original credential key is required.'
    if [ -e "$PIXCHIVE_ROOT/browser-state/auth-state.enc" ] || [ -L "$PIXCHIVE_ROOT/browser-state/auth-state.enc" ]; then
        private_file "$PIXCHIVE_ROOT/browser-state/auth-state.enc"
    fi
    for incomplete in .restore-incomplete pixchive.db.restoring pixchive.db.restoring-wal pixchive.db.restoring-shm pixchive.db.restoring-journal; do
        [ ! -e "$PIXCHIVE_ROOT/data/$incomplete" ] && [ ! -L "$PIXCHIVE_ROOT/data/$incomplete" ] || fail 'Incomplete restore: preserve this target and recover into a new empty target.'
    done
    [ -f "$PIXCHIVE_ROOT/security/seccomp_profile.json" ] && [ ! -L "$PIXCHIVE_ROOT/security/seccomp_profile.json" ] || fail 'Official seccomp profile missing.'
    cmp -s "$HERE/security/seccomp_profile.json" "$PIXCHIVE_ROOT/security/seccomp_profile.json" || fail 'Install the bundled official seccomp profile unchanged.'
    compose config --quiet
    printf '%s\n' 'NFS TRIAL preflight passed; this does not establish SQLite/WAL safety or application readiness.'
}

case "${1:-start}" in
    start)
        preflight
        compose up --detach --no-build --pull never
        ;;
    preflight) preflight ;;
    stop)
        # No mount check: graceful stop must remain possible during a storage outage.
        compose stop --timeout 60
        ;;
    status) compose ps ;;
    backup)
        preflight
        compose -f "$HERE/backup.compose.yml" run --rm --no-deps -T --pull never \
            web /app/.venv/bin/python /opt/pixchive-deploy/backup.py
        ;;
    *) fail 'Usage: sh start.sh [start|preflight|stop|status|backup]' ;;
esac
