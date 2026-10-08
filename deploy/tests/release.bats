#!/usr/bin/env bats
# release.sh: releasing a commit by hand (research R7 and R18; contracts/operations.md, "Host
# scripts").

load helpers

FIRST=1111111111111111111111111111111111111111
SECOND=2222222222222222222222222222222222222222

setup_file() {
	ensure_tools jq
}

setup() {
	setup_host
	export CODEATLAS_READY_TIMEOUT=1 CODEATLAS_READY_INTERVAL=1
	production_parameters
}

# Copies deploy/ into /opt/codeatlas/releases/<sha>/, as the caller extracts the bundle there.
# A second argument is appended to the bundle's Caddyfile, to change it.
make_bundle() {
	local bundle="$CODEATLAS_ROOT/releases/$1"
	mkdir -p "$bundle"
	cp "$DEPLOY_DIR"/*.sh "$DEPLOY_DIR/compose.yml" "$DEPLOY_DIR/Caddyfile" "$bundle/"
	if [ -n "${2:-}" ]; then
		printf '%s\n' "$2" >>"$bundle/Caddyfile"
	fi
}

release() {
	run "$CODEATLAS_ROOT/releases/$1/release.sh" "$1"
}

@test "a first release renders, pulls, migrates, and switches to the commit" {
	make_bundle "$FIRST"
	release "$FIRST"
	[ "$status" -eq 0 ]

	[ "$(readlink "$CODEATLAS_ROOT/current")" = "$CODEATLAS_ROOT/releases/$FIRST" ]
	grep -qxF "IMAGE_TAG=$FIRST" "$CODEATLAS_ROOT/.env"
	grep -qxF 'ENVIRONMENT=pilot' "$CODEATLAS_ROOT/.env"
	grep -qxF 'AWS_REGION=us-east-1' "$CODEATLAS_ROOT/.env"
	grep -qxF 'REGISTRY=123456789012.dkr.ecr.us-east-1.amazonaws.com' "$CODEATLAS_ROOT/.env"
	grep -qxF 'CODEATLAS_HOSTNAME=pilot.example.dev' "$CODEATLAS_ROOT/.env"
	[ "$(cat "$CODEATLAS_DATA/state/running")" = "$FIRST" ]
	[ ! -e "$CODEATLAS_DATA/state/previous" ]
	[ -s "$CODEATLAS_ROOT/runtime/app.env" ]
	grep -q " pilot $FIRST released$" "$CODEATLAS_LOG_DIR/releases.log"

	bundle="$CODEATLAS_ROOT/releases/$FIRST"
	compose="docker compose --project-directory $CODEATLAS_ROOT -f $bundle/compose.yml"
	grep -qxF "aws ecr get-login-password --region us-east-1" "$STUB_LOG"
	grep -qF "docker login --username AWS --password-stdin 123456789012.dkr.ecr.us-east-1.amazonaws.com" "$STUB_LOG"
	grep -qxF "IMAGE_TAG=$FIRST $compose pull --quiet api web" "$STUB_LOG"
	grep -qxF "IMAGE_TAG=$FIRST $compose run --rm migrate" "$STUB_LOG"
	grep -qxF "codeatlas-compose up -d --remove-orphans" "$STUB_LOG"
	grep -qF -- "--resolve pilot.example.dev:443:127.0.0.1 https://pilot.example.dev/readyz" "$STUB_LOG"
	# The first release creates caddy with the rest; there is no previous Caddyfile to compare.
	refute_grep -q 'force-recreate' "$STUB_LOG"
}

@test "the steps run in order: render, login, pull, migrate, switch, wait" {
	make_bundle "$FIRST"
	release "$FIRST"
	[ "$status" -eq 0 ]
	order="$(grep -oE 'ssm get-parameters-by-path|ecr get-login-password|pull --quiet|run --rm migrate|up -d --remove-orphans|/readyz' "$STUB_LOG" | tr '\n' ',')"
	[ "$order" = "ssm get-parameters-by-path,ecr get-login-password,pull --quiet,run --rm migrate,up -d --remove-orphans,/readyz," ]
}

@test "a second release records the previous commit and keeps caddy when the Caddyfile is unchanged" {
	make_bundle "$FIRST"
	release "$FIRST"
	[ "$status" -eq 0 ]
	make_bundle "$SECOND"
	: >"$STUB_LOG"
	release "$SECOND"
	[ "$status" -eq 0 ]

	[ "$(readlink "$CODEATLAS_ROOT/current")" = "$CODEATLAS_ROOT/releases/$SECOND" ]
	grep -qxF "IMAGE_TAG=$SECOND" "$CODEATLAS_ROOT/.env"
	[ "$(cat "$CODEATLAS_DATA/state/running")" = "$SECOND" ]
	[ "$(cat "$CODEATLAS_DATA/state/previous")" = "$FIRST" ]
	grep -qxF "codeatlas-compose up -d --remove-orphans" "$STUB_LOG"
	refute_grep -q 'force-recreate' "$STUB_LOG"
}

@test "a changed Caddyfile recreates caddy after the other services start" {
	make_bundle "$FIRST"
	release "$FIRST"
	[ "$status" -eq 0 ]
	make_bundle "$SECOND" "# changed"
	: >"$STUB_LOG"
	release "$SECOND"
	[ "$status" -eq 0 ]

	ups="$(grep '^codeatlas-compose up' "$STUB_LOG" | tr '\n' ',')"
	[ "$ups" = "codeatlas-compose up -d --remove-orphans,codeatlas-compose up -d --force-recreate caddy," ]
}

@test "releasing the running commit again keeps the previous commit" {
	make_bundle "$FIRST"
	release "$FIRST"
	make_bundle "$SECOND"
	release "$SECOND"
	release "$SECOND"
	[ "$status" -eq 0 ]
	[ "$(cat "$CODEATLAS_DATA/state/running")" = "$SECOND" ]
	[ "$(cat "$CODEATLAS_DATA/state/previous")" = "$FIRST" ]
}

# A failure before the switch exits 2 and leaves .env, current, the state files, and the running
# services alone.
assert_failed_before_switch() {
	[ "$status" -eq 2 ]
	cmp -s "$CODEATLAS_ROOT/.env" "$BATS_TEST_TMPDIR/env.before"
	[ "$(readlink "$CODEATLAS_ROOT/current")" = "$CODEATLAS_ROOT/releases/$FIRST" ]
	[ "$(cat "$CODEATLAS_DATA/state/running")" = "$FIRST" ]
	[ ! -e "$CODEATLAS_DATA/state/previous" ]
	refute_grep -q '^codeatlas-compose' "$STUB_LOG"
	grep -q " pilot $SECOND failed before the switch" "$CODEATLAS_LOG_DIR/releases.log"
}

prepare_failing_release() {
	make_bundle "$FIRST"
	release "$FIRST"
	[ "$status" -eq 0 ]
	cp "$CODEATLAS_ROOT/.env" "$BATS_TEST_TMPDIR/env.before"
	make_bundle "$SECOND" "# changed"
	: >"$STUB_LOG"
}

@test "a failing registry login exits 2 without touching the running release" {
	prepare_failing_release
	STUB_FAIL=ecr-login release "$SECOND"
	assert_failed_before_switch
	refute_grep -q ' pull ' "$STUB_LOG"
}

@test "a failing pull exits 2 without touching the running release" {
	prepare_failing_release
	STUB_FAIL=pull release "$SECOND"
	assert_failed_before_switch
	refute_grep -q 'run --rm migrate' "$STUB_LOG"
}

@test "a failing migration exits 2 without touching the running release" {
	prepare_failing_release
	STUB_FAIL=migrate release "$SECOND"
	assert_failed_before_switch
}

@test "a failing configuration render exits 2 without touching the running release" {
	prepare_failing_release
	jq '.Parameters |= map(select(.Name | endswith("/GEMINI_API_KEY") | not))' \
		"$STUB_SSM_RESPONSE" >"$BATS_TEST_TMPDIR/partial.json"
	mv "$BATS_TEST_TMPDIR/partial.json" "$STUB_SSM_RESPONSE"
	release "$SECOND"
	assert_failed_before_switch
	[[ "$output" == *GEMINI_API_KEY* ]]
	refute_grep -q 'get-login-password' "$STUB_LOG"
}

@test "release.sh refuses to run outside the commit's bundle" {
	make_bundle "$FIRST"
	run "$CODEATLAS_ROOT/releases/$FIRST/release.sh" "$SECOND"
	[ "$status" -eq 2 ]
	[ ! -e "$CODEATLAS_ROOT/current" ]
	refute_grep -q '^aws\|^codeatlas-compose' "$STUB_LOG"
}

@test "release.sh refuses an abbreviated commit" {
	make_bundle "$FIRST"
	run "$CODEATLAS_ROOT/releases/$FIRST/release.sh" 1111111
	[ "$status" -eq 2 ]
	[ ! -e "$CODEATLAS_ROOT/current" ]
}

@test "release.sh never prints a secret value" {
	make_bundle "$FIRST"
	release "$FIRST"
	[ "$status" -eq 0 ]
	refute_secrets_in "$output"
	refute_secrets_in "$(cat "$STUB_LOG" "$CODEATLAS_LOG_DIR/releases.log")"
}
