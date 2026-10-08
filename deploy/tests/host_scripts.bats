#!/usr/bin/env bats
# backup.sh, restore.sh, and check-certificate.sh (research R10, R12, and R18; contracts/operations.md,
# "Host scripts").
#
# Besides the stubs of helpers.bash, the commands that run inside the containers (psql, pg_dump,
# pg_restore, and python in the api image) are stubs in $STUB_CONTAINER_BIN, which the
# codeatlas-compose stub runs for `exec` and `run`; they read and write the containers' /backup in
# $CODEATLAS_DATA/backup. An `openssl` stub stands in for the TLS client. More failure keywords for
# $STUB_FAIL: pg_dump, pg_restore, backup-manifest, verify-restore, s_client.

load helpers

# The snapshot that the psql stub exports, and the counts it writes.
SNAPSHOT=00000003-0000001B-1
COUNTS='{"alembic_revision" : "0004", "row_counts" : { "alembic_version" : 1, "users" : 6 }}'

setup_file() {
	ensure_tools jq
}

setup() {
	setup_host
	install_container_stubs
	install_openssl_stub
	BACKUP="$CODEATLAS_DATA/backup"
	mkdir -p "$BACKUP"
	TODAY="$(date -u +%Y-%m-%d)"
}

install_container_stubs() {
	export STUB_CONTAINER_BIN="$BATS_TEST_TMPDIR/container-bin"
	export STUB_SNAPSHOT="$SNAPSHOT" STUB_COUNTS="$COUNTS"
	mkdir -p "$STUB_CONTAINER_BIN"
	local bin="$STUB_CONTAINER_BIN"

	# Acts on the script lines backup.sh relies on as psql 17 does: `\setenv` with the exported
	# snapshot; `\!`, which sets SHELL_ERROR and never fails the session; `\if :SHELL_ERROR` to
	# `\endif`; an exception, which ends the session with status 3 only under ON_ERROR_STOP; and the
	# `\g` that writes the counts file.
	cat >"$bin/psql" <<'EOF'
#!/usr/bin/env bash
echo "psql $*" >>"$STUB_LOG"
script="$(cat)"
printf '%s\n' "$script" >"$BATS_TEST_TMPDIR/psql-script.sql"
stop_on_error=false
case " $* " in *" ON_ERROR_STOP=1 "*) stop_on_error=true ;; esac
shell_error=false skipping=false
while IFS= read -r line; do
	case "$line" in
	'\if :SHELL_ERROR'*) [ "$shell_error" = true ] || skipping=true; continue ;;
	'\endif'*) skipping=false; continue ;;
	esac
	[ "$skipping" = true ] && continue
	case "$line" in
	'\setenv '*' :snapshot'*)
		name="${line#'\setenv '}"
		export "${name%% *}=$STUB_SNAPSHOT"
		;;
	'\! '*)
		if sh -c "${line#'\! '}"; then shell_error=false; else shell_error=true; fi
		;;
	*'RAISE EXCEPTION'*)
		echo "ERROR:  ${line}" >&2
		[ "$stop_on_error" = true ] && exit 3
		;;
	*'\g '*'/backup/counts.json'*)
		printf '%s\n' "$STUB_COUNTS" >"$CODEATLAS_DATA/backup/counts.json"
		;;
	esac
done <<<"$script"
exit 0
EOF

	cat >"$bin/pg_dump" <<'EOF'
#!/usr/bin/env bash
echo "pg_dump $*" >>"$STUB_LOG"
case " $STUB_FAIL " in *" pg_dump "*) echo "pg_dump: error: stub failure" >&2; exit 1 ;; esac
output=""
while [ $# -gt 0 ]; do
	[ "$1" = -f ] && { output="$2"; shift; }
	shift
done
printf 'PGDMP stub dump of snapshot %s\n' "$CODEATLAS_SNAPSHOT" >"$CODEATLAS_DATA/backup/${output#/backup/}"
EOF

	cat >"$bin/pg_restore" <<'EOF'
#!/usr/bin/env bash
echo "pg_restore $*" >>"$STUB_LOG"
case " $STUB_FAIL " in *" pg_restore "*) echo "pg_restore: error: stub failure" >&2; exit 1 ;; esac
archive="${*: -1}"
[ -f "$CODEATLAS_DATA/backup/${archive#/backup/}" ] ||
	{ echo "pg_restore: error: could not open input file" >&2; exit 1; }
case " $* " in *" --list "*) echo "; Archive created by the stub" ;; esac
exit 0
EOF

	# `python -m codeatlas.ops backup-manifest` and `verify-restore` in the api image.
	cat >"$bin/python" <<'EOF'
#!/usr/bin/env bash
echo "python $*" >>"$STUB_LOG"
[ "$1 $2" = "-m codeatlas.ops" ] || exit 2
shift 2
fails() { case " $STUB_FAIL " in *" $1 "*) return 0 ;; esac; return 1; }
case "$1" in
backup-manifest)
	fails backup-manifest && { echo "error: invalid counts file" >&2; exit 1; }
	shift
	while [ $# -gt 0 ]; do
		case "$1" in
		--counts) counts="$CODEATLAS_DATA/backup/${2#/backup/}" ;;
		--dump-key) key="$2" ;;
		--bytes) bytes="$2" ;;
		--sha256) sha256="$2" ;;
		esac
		shift 2
	done
	jq -c --arg key "$key" --argjson bytes "$bytes" --arg sha256 "$sha256" \
		'{created_at: "2026-10-08T03:30:12Z", release: "stub-release",
		alembic_revision, dump: {key: $key, bytes: $bytes, sha256: $sha256}, row_counts}' "$counts"
	;;
verify-restore)
	[ -f "$CODEATLAS_DATA/backup/${2#/backup/}" ] || { echo "error: no manifest" >&2; exit 1; }
	fails verify-restore && { echo "users: 6 in the backup, 5 restored" >&2; exit 1; }
	echo "The restored database matches the backup."
	;;
*) exit 2 ;;
esac
EOF

	chmod +x "$bin"/*
}

install_openssl_stub() {
	local bin="$BATS_TEST_TMPDIR/bin"
	cat >"$bin/openssl" <<'EOF'
#!/usr/bin/env bash
echo "openssl $*" >>"$STUB_LOG"
input="$(cat)"
case "$1" in
s_client)
	case " $STUB_FAIL " in *" s_client "*) echo "connect:errno=111" >&2; exit 1 ;; esac
	printf 'CONNECTED(00000003)\n-----BEGIN CERTIFICATE-----\nstub\n-----END CERTIFICATE-----\n'
	;;
x509)
	case "$input" in
	*'BEGIN CERTIFICATE'*) echo "notAfter=$STUB_CERT_END" ;;
	*) echo "Could not find certificate from <stdin>" >&2; exit 1 ;;
	esac
	;;
*) exit 1 ;;
esac
EOF
	chmod +x "$bin/openssl"
}

sha256_of() {
	sha256sum "$1" | cut -d ' ' -f 1
}

# --- backup.sh ---

backup() {
	run "$DEPLOY_DIR/backup.sh" "$@"
}

# Fails when a backup was uploaded or BackupCompleted was published.
refute_backup_published() {
	refute_grep -q 's3api put-object' "$STUB_LOG"
	refute_grep -q 'put-metric-data' "$STUB_LOG"
}

@test "backup dumps and counts in one snapshot, uploads both files without overwriting, and publishes BackupCompleted" {
	backup
	[ "$status" -eq 0 ]

	# One psql session in the db container, stopping at the first error.
	grep -qxF 'codeatlas-compose exec -T db psql -X -q -v ON_ERROR_STOP=1 -U codeatlas -d codeatlas' "$STUB_LOG"
	script="$(cat "$BATS_TEST_TMPDIR/psql-script.sql")"
	grep -qxF 'BEGIN ISOLATION LEVEL REPEATABLE READ;' <<<"$script"
	grep -qF 'SELECT pg_export_snapshot() AS snapshot \gset' <<<"$script"
	grep -qF '\g (format=unaligned tuples_only=on) /backup/counts.json' <<<"$script"
	grep -qxF 'COMMIT;' <<<"$script"
	# pg_dump runs from the session with the exported snapshot.
	grep -qxF "pg_dump -Fc --snapshot=$SNAPSHOT -f /backup/codeatlas.dump -U codeatlas codeatlas" "$STUB_LOG"

	grep -qxF 'pg_restore --list /backup/codeatlas.dump' "$STUB_LOG"

	bucket_dir="$STUB_BUCKET/backups/$TODAY"
	[ "$(cat "$bucket_dir/codeatlas.dump")" = "PGDMP stub dump of snapshot $SNAPSHOT" ]
	bytes="$(wc -c <"$bucket_dir/codeatlas.dump" | tr -d ' ')"
	sha256="$(sha256_of "$bucket_dir/codeatlas.dump")"
	grep -qxF "codeatlas-compose run --rm -T -v $BACKUP:/backup:ro api python -m codeatlas.ops backup-manifest --counts /backup/counts.json --dump-key backups/$TODAY/codeatlas.dump --bytes $bytes --sha256 $sha256" "$STUB_LOG"
	[ "$(jq -r '.dump.sha256' "$bucket_dir/manifest.json")" = "$sha256" ]
	[ "$(jq -r '.dump.key' "$bucket_dir/manifest.json")" = "backups/$TODAY/codeatlas.dump" ]
	[ "$(jq -r '.row_counts.users' "$bucket_dir/manifest.json")" = 6 ]
	[ "$(jq -r '.alembic_revision' "$bucket_dir/manifest.json")" = 0004 ]

	for name in codeatlas.dump manifest.json; do
		grep -qxF "aws s3api put-object --region us-east-1 --bucket codeatlas-123456789012-us-east-1 --key backups/$TODAY/$name --body $BACKUP/$name --if-none-match *" "$STUB_LOG"
	done
	grep -qxF 'aws cloudwatch put-metric-data --region us-east-1 --namespace CodeAtlas --metric-name BackupCompleted --value 1 --unit Count --dimensions Environment=pilot' "$STUB_LOG"

	# The staged files are gone, and the run is in its log file.
	[ -z "$(ls -A "$BACKUP")" ]
	grep -q "backup: uploaded backups/$TODAY/" "$CODEATLAS_LOG_DIR/backup.log"
}

@test "backup runs its steps in order and publishes the metric last" {
	backup
	[ "$status" -eq 0 ]
	order="$(grep -oE '^(psql|pg_dump|pg_restore --list|python -m codeatlas.ops backup-manifest|aws s3api put-object .* --key [^ ]+|aws cloudwatch put-metric-data)' "$STUB_LOG" |
		sed -E 's/.* --key backups\/[0-9-]+\//put /' | tr '\n' ',')"
	[ "$order" = "psql,pg_dump,pg_restore --list,python -m codeatlas.ops backup-manifest,put codeatlas.dump,put manifest.json,aws cloudwatch put-metric-data," ]
}

@test "backup publishes no metric and exits non-zero when the dump step fails" {
	STUB_FAIL=pg_dump backup
	[ "$status" -ne 0 ]
	# psql ended the session at the failed pg_dump, and the script stopped there.
	refute_grep -q '^pg_restore' "$STUB_LOG"
	refute_grep -q 'backup-manifest' "$STUB_LOG"
	refute_backup_published
	[ -z "$(ls -A "$BACKUP")" ]
	grep -q 'backup: failed while dumping the database' "$CODEATLAS_LOG_DIR/backup.log"
}

@test "backup uploads nothing when pg_restore cannot read the dump" {
	STUB_FAIL=pg_restore backup
	[ "$status" -ne 0 ]
	refute_backup_published
	[ -z "$(ls -A "$BACKUP")" ]
}

@test "backup uploads nothing when the manifest cannot be built" {
	STUB_FAIL=backup-manifest backup
	[ "$status" -ne 0 ]
	refute_backup_published
}

@test "backup publishes no metric when the upload fails" {
	STUB_FAIL=s3-upload backup
	[ "$status" -ne 0 ]
	refute_grep -q 'put-metric-data' "$STUB_LOG"
}

@test "backup never overwrites an existing backup of the same date" {
	mkdir -p "$STUB_BUCKET/backups/$TODAY"
	echo "earlier backup" >"$STUB_BUCKET/backups/$TODAY/codeatlas.dump"
	backup
	[ "$status" -ne 0 ]
	[ "$(cat "$STUB_BUCKET/backups/$TODAY/codeatlas.dump")" = "earlier backup" ]
	[ ! -e "$STUB_BUCKET/backups/$TODAY/manifest.json" ]
	refute_grep -q 'put-metric-data' "$STUB_LOG"
}

@test "backup takes no arguments" {
	backup now
	[ "$status" -eq 2 ]
	refute_grep -q '^codeatlas-compose\|^aws' "$STUB_LOG"
}

# --- restore.sh ---

DATE=2026-10-07

# Puts a backup of $DATE in the bucket, with a manifest that names the dump's SHA-256, or the
# given one.
put_backup() {
	local dir="$STUB_BUCKET/backups/$DATE"
	mkdir -p "$dir"
	echo "PGDMP stub dump" >"$dir/codeatlas.dump"
	jq -n --arg sha256 "${1:-$(sha256_of "$dir/codeatlas.dump")}" --arg key "backups/$DATE/codeatlas.dump" \
		'{created_at: "2026-10-07T03:30:12Z", release: "stub-release", alembic_revision: "0004",
		dump: {key: $key, bytes: 16, sha256: $sha256}, row_counts: {users: 6}}' >"$dir/manifest.json"
}

restore() {
	run "$DEPLOY_DIR/restore.sh" "$@"
}

@test "restore downloads, checks, stops api and worker, restores, verifies, and leaves them stopped" {
	put_backup
	restore "$DATE"
	[ "$status" -eq 0 ]

	grep -qxF "aws s3 cp --region us-east-1 --only-show-errors s3://codeatlas-123456789012-us-east-1/backups/$DATE/manifest.json $BACKUP/manifest.json" "$STUB_LOG"
	grep -qxF "aws s3 cp --region us-east-1 --only-show-errors s3://codeatlas-123456789012-us-east-1/backups/$DATE/codeatlas.dump $BACKUP/codeatlas.dump" "$STUB_LOG"
	grep -qxF 'codeatlas-compose stop api worker' "$STUB_LOG"
	grep -qxF 'pg_restore --clean --if-exists --no-owner --exit-on-error -U codeatlas -d codeatlas /backup/codeatlas.dump' "$STUB_LOG"
	grep -qxF "codeatlas-compose run --rm -T -v $BACKUP:/backup:ro api python -m codeatlas.ops verify-restore /backup/manifest.json" "$STUB_LOG"

	order="$(grep -oE '^(aws s3 cp|codeatlas-compose stop|pg_restore|python -m codeatlas.ops verify-restore)' "$STUB_LOG" | tr '\n' ',')"
	[ "$order" = "aws s3 cp,aws s3 cp,codeatlas-compose stop,pg_restore,python -m codeatlas.ops verify-restore," ]
	# Nothing starts api or worker again.
	refute_grep -qE '^codeatlas-compose (up|start|restart)' "$STUB_LOG"
	[[ "$output" == *"codeatlas-compose up -d api worker"* ]]
	grep -q "restore: restored backups/$DATE" "$CODEATLAS_LOG_DIR/restore.log"
}

@test "restore refuses a dump whose checksum does not match" {
	put_backup 0000000000000000000000000000000000000000000000000000000000000000
	restore "$DATE"
	[ "$status" -ne 0 ]
	[[ "$output" == *"SHA-256"* ]]
	refute_grep -q '^codeatlas-compose' "$STUB_LOG"
	refute_grep -q '^pg_restore' "$STUB_LOG"
}

@test "restore stops before the services when the backup cannot be downloaded" {
	restore "$DATE"
	[ "$status" -ne 0 ]
	refute_grep -q '^codeatlas-compose' "$STUB_LOG"
}

@test "restore fails without verifying when pg_restore fails" {
	put_backup
	STUB_FAIL=pg_restore restore "$DATE"
	[ "$status" -ne 0 ]
	refute_grep -q 'verify-restore' "$STUB_LOG"
	grep -q 'restore: failed while restoring' "$CODEATLAS_LOG_DIR/restore.log"
}

@test "restore fails when the restored database does not match the manifest" {
	put_backup
	STUB_FAIL=verify-restore restore "$DATE"
	[ "$status" -ne 0 ]
	[[ "$output" == *"users: 6 in the backup, 5 restored"* ]]
}

@test "restore refuses a date that is not YYYY-MM-DD" {
	restore 2026-10-7
	[ "$status" -eq 2 ]
	restore
	[ "$status" -eq 2 ]
	refute_grep -q '^aws\|^codeatlas-compose' "$STUB_LOG"
}

# --- check-certificate.sh ---

# Sets the stub certificate's end date to now plus the given number of days and one hour, in the
# format of `openssl x509 -enddate -dateopt iso_8601`.
certificate_ends_in_days() {
	STUB_CERT_END="$(jq -nr --argjson days "$1" 'now + $days * 86400 + 3600 | strftime("%Y-%m-%d %H:%M:%SZ")')"
	export STUB_CERT_END
}

check_certificate() {
	run "$DEPLOY_DIR/check-certificate.sh" "$@"
}

@test "check-certificate publishes the days left computed from the certificate's end date" {
	certificate_ends_in_days 30
	check_certificate
	[ "$status" -eq 0 ]
	grep -qxF 'openssl s_client -servername pilot.example.dev -connect 127.0.0.1:443' "$STUB_LOG"
	grep -qxF 'openssl x509 -noout -enddate -dateopt iso_8601' "$STUB_LOG"
	grep -qxF 'aws cloudwatch put-metric-data --region us-east-1 --namespace CodeAtlas --metric-name CertificateDaysLeft --value 30 --unit Count --dimensions Environment=pilot' "$STUB_LOG"
	grep -q 'check-certificate: 30 days left' "$CODEATLAS_LOG_DIR/check-certificate.log"
}

@test "check-certificate publishes a value below the alarm threshold as it is" {
	certificate_ends_in_days 3
	check_certificate
	[ "$status" -eq 0 ]
	grep -qF -- '--metric-name CertificateDaysLeft --value 3 ' "$STUB_LOG"
}

@test "check-certificate publishes nothing and exits non-zero when the certificate cannot be read" {
	certificate_ends_in_days 30
	STUB_FAIL=s_client check_certificate
	[ "$status" -ne 0 ]
	refute_grep -q 'put-metric-data' "$STUB_LOG"
}
