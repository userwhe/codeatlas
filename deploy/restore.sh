#!/usr/bin/env bash
# Restores a nightly backup into this host's database (specs/004-pilot-deployment research R12;
# contracts/operations.md, "Host scripts"). Run by hand, after a release of the backup's commit:
# in the recovery exercise (drill) and after losing the pilot's data volume (docs/operations.md).
#
# Usage: restore.sh <UTC date, YYYY-MM-DD>
#
#   1. Downloads s3://$BUCKET/backups/<date>/manifest.json and codeatlas.dump into
#      /var/lib/codeatlas/backup, and checks the dump's SHA-256 against the manifest. Nothing on
#      the host changes until this check passes.
#   2. Stops api and worker, then runs pg_restore --clean --if-exists --no-owner --exit-on-error
#      in the db container, replacing every object that the dump holds.
#   3. Runs `python -m codeatlas.ops verify-restore` in the api image, which compares the restored
#      row counts and Alembic revision with the manifest.
# It leaves api and worker stopped, for the operator to check the result and start them.
#
# Output goes to standard output and to /var/log/codeatlas/restore.log. Exit status: 0 restored and
# verified; 1 a failure (download, checksum, restore, or verification); 2 bad arguments or host
# configuration.
#
# CODEATLAS_ROOT (default /opt/codeatlas), CODEATLAS_DATA (default /var/lib/codeatlas), and
# CODEATLAS_LOG_DIR (default /var/log/codeatlas) exist for tests.
set -euo pipefail

ROOT="${CODEATLAS_ROOT:-/opt/codeatlas}"
DATA="${CODEATLAS_DATA:-/var/lib/codeatlas}"
LOG_DIR="${CODEATLAS_LOG_DIR:-/var/log/codeatlas}"
STAGING="$DATA/backup"

stage="starting"

say() {
	printf '%s restore: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

finish() {
	if [[ "$1" -ne 0 ]]; then
		say "failed while $stage"
	fi
}

# Sources the host's environment file and checks the names this script needs.
load_environment() {
	[[ -r "$ROOT/environment" ]] || {
		say "$ROOT/environment is missing"
		return 2
	}
	# shellcheck source=/dev/null
	. "$ROOT/environment"
	local name
	for name in AWS_REGION BUCKET; do
		[[ -n "${!name:-}" ]] || {
			say "$name is not set in $ROOT/environment"
			return 2
		}
	done
}

download() {
	aws s3 cp --region "$AWS_REGION" --only-show-errors "s3://$BUCKET/$1" "$2"
}

main() {
	if [[ $# -ne 1 || ! "$1" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
		echo "usage: restore.sh <UTC date, YYYY-MM-DD>" >&2
		return 2
	fi
	trap 'finish $?' EXIT
	load_environment

	local prefix="backups/$1" dump="$STAGING/codeatlas.dump" manifest="$STAGING/manifest.json"
	local expected actual release running
	mkdir -p "$STAGING"

	stage="downloading s3://$BUCKET/$prefix/"
	say "downloading s3://$BUCKET/$prefix/"
	download "$prefix/manifest.json" "$manifest"
	download "$prefix/codeatlas.dump" "$dump"

	stage="checking the dump"
	expected="$(jq -r '.dump.sha256 // ""' "$manifest")"
	actual="$(sha256sum "$dump" | cut -d ' ' -f 1)"
	if [[ -z "$expected" || "$actual" != "$expected" ]]; then
		say "the dump's SHA-256 ($actual) does not match the manifest's (${expected:-none}); nothing was changed"
		return 1
	fi
	release="$(jq -r '.release // "unknown"' "$manifest")"
	say "the dump matches its manifest: taken $(jq -r '.created_at // "at an unknown time"' "$manifest") on commit $release"
	running="$(cat "$DATA/state/running" 2>/dev/null || true)"
	if [[ -n "$running" && "$running" != "$release" ]]; then
		say "note: this host runs $running, not the backup's commit"
	fi

	stage="stopping api and worker"
	codeatlas-compose stop api worker

	stage="restoring"
	say "restoring into the database"
	codeatlas-compose exec -T db pg_restore --clean --if-exists --no-owner --exit-on-error \
		-U codeatlas -d codeatlas /backup/codeatlas.dump

	stage="verifying"
	codeatlas-compose run --rm -T -v "$STAGING:/backup:ro" api python -m codeatlas.ops \
		verify-restore /backup/manifest.json

	rm -f "$dump" "$manifest"
	stage="done"
	say "restored $prefix and verified it; api and worker are stopped"
	say "check the data, then start them: sudo codeatlas-compose up -d api worker (only api in the drill)"
}

mkdir -p "$LOG_DIR"
main "$@" 2>&1 | tee -a "$LOG_DIR/restore.log"
