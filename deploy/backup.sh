#!/usr/bin/env bash
# Takes the nightly backup (specs/004-pilot-deployment research R12; contracts/operations.md,
# "Host scripts"). codeatlas-backup.timer starts it at 03:30 UTC, in the pilot only.
#
# Usage: backup.sh
#
#   1. In the db container, one psql session starts a REPEATABLE READ transaction, exports its
#      snapshot, and runs pg_dump with that snapshot into /var/lib/codeatlas/backup/codeatlas.dump
#      (the container's /backup). psql counts a `\!` command as successful whatever its exit
#      status, so the session checks SHELL_ERROR and raises an error when pg_dump failed, which
#      ends it. In the same transaction, it then writes the row count of every table in the public
#      schema and the Alembic revision to counts.json, so the counts describe exactly the dump.
#   2. pg_restore --list must read the dump. Its size and SHA-256 and the counts go into
#      manifest.json, built by `python -m codeatlas.ops backup-manifest` in the api image.
#   3. The dump, then the manifest, are uploaded to s3://$BUCKET/backups/<UTC date>/ with
#      If-None-Match: *, so an existing backup is never overwritten; the instance role allows no
#      other upload there. A second backup on the same UTC date therefore fails.
#   4. BackupCompleted 1 is published. Any failure exits non-zero before that, and the missing
#      metric fires the backup-missing alarm.
# The staged files are deleted when the script ends, whatever the outcome.
#
# Output goes to standard output and to /var/log/codeatlas/backup.log. Exit status: 0 backed up;
# 1 any failure (no metric published); 2 bad arguments or host configuration.
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
	printf '%s backup: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

finish() {
	local status="$1"
	rm -f "$STAGING/codeatlas.dump" "$STAGING/counts.json" "$STAGING/manifest.json"
	if [[ "$status" -ne 0 ]]; then
		say "failed while $stage; no backup was recorded"
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
	for name in ENVIRONMENT AWS_REGION BUCKET; do
		[[ -n "${!name:-}" ]] || {
			say "$name is not set in $ROOT/environment"
			return 2
		}
	done
}

upload() {
	aws s3api put-object --region "$AWS_REGION" --bucket "$BUCKET" --key "$2" --body "$1" \
		--if-none-match '*' >/dev/null
}

main() {
	if [[ $# -ne 0 ]]; then
		echo "usage: backup.sh" >&2
		return 2
	fi
	trap 'finish $?' EXIT
	load_environment

	local prefix dump manifest bytes sha256
	prefix="backups/$(date -u +%Y-%m-%d)"
	dump="$STAGING/codeatlas.dump"
	manifest="$STAGING/manifest.json"
	mkdir -p "$STAGING"

	stage="dumping the database"
	say "dumping the database and counting its rows in one snapshot"
	codeatlas-compose exec -T db psql -X -q -v ON_ERROR_STOP=1 -U codeatlas -d codeatlas <<'SQL'
BEGIN ISOLATION LEVEL REPEATABLE READ;
SELECT pg_export_snapshot() AS snapshot \gset
\setenv CODEATLAS_SNAPSHOT :snapshot
\! pg_dump -Fc --snapshot="$CODEATLAS_SNAPSHOT" -f /backup/codeatlas.dump -U codeatlas codeatlas
\if :SHELL_ERROR
DO $$BEGIN RAISE EXCEPTION 'pg_dump failed'; END$$;
\endif
SELECT json_build_object(
  'alembic_revision', (SELECT version_num FROM public.alembic_version),
  'row_counts', (
    SELECT coalesce(json_object_agg(c.relname, (xpath('/row/n/text()', query_to_xml(
      format('SELECT count(*) AS n FROM public.%I', c.relname), false, true, '')))[1]::text::bigint
      ORDER BY c.relname), '{}')
    FROM pg_class AS c JOIN pg_namespace AS s ON s.oid = c.relnamespace
    WHERE s.nspname = 'public' AND c.relkind IN ('r', 'p')
  )
) \g (format=unaligned tuples_only=on) /backup/counts.json
COMMIT;
SQL

	stage="checking the dump"
	codeatlas-compose exec -T db pg_restore --list /backup/codeatlas.dump >/dev/null
	bytes="$(wc -c <"$dump" | tr -d ' ')"
	sha256="$(sha256sum "$dump" | cut -d ' ' -f 1)"
	say "the dump has $bytes bytes, SHA-256 $sha256"

	stage="building the manifest"
	codeatlas-compose run --rm -T -v "$STAGING:/backup:ro" api python -m codeatlas.ops \
		backup-manifest --counts /backup/counts.json --dump-key "$prefix/codeatlas.dump" \
		--bytes "$bytes" --sha256 "$sha256" >"$manifest"
	jq -e --arg sha256 "$sha256" '.dump.sha256 == $sha256' "$manifest" >/dev/null || {
		say "manifest.json is not the manifest of this dump"
		return 1
	}

	stage="uploading to s3://$BUCKET/$prefix/"
	upload "$dump" "$prefix/codeatlas.dump"
	upload "$manifest" "$prefix/manifest.json"
	say "uploaded $prefix/codeatlas.dump and $prefix/manifest.json"

	stage="publishing BackupCompleted"
	aws cloudwatch put-metric-data --region "$AWS_REGION" --namespace CodeAtlas \
		--metric-name BackupCompleted --value 1 --unit Count --dimensions "Environment=$ENVIRONMENT"
	stage="done"
	say "backup completed"
}

mkdir -p "$LOG_DIR"
main "$@" 2>&1 | tee -a "$LOG_DIR/backup.log"
