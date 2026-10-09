#!/usr/bin/env bats
# release.sh: releasing a commit, checking it, rolling it back, and refreshing the configuration
# (research R7 and R18; contracts/operations.md, "Host scripts").

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

# Runs the commit's release.sh from its bundle, with any further arguments.
release() {
	local sha="$1"
	shift
	run "$CODEATLAS_ROOT/releases/$sha/release.sh" "$sha" "$@"
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
	# The check also asks for the web app and the version, through the local Caddy.
	grep -qE -- '--resolve pilot\.example\.dev:443:127\.0\.0\.1 https://pilot\.example\.dev/$' "$STUB_LOG"
	grep -qE -- '--resolve pilot\.example\.dev:443:127\.0\.0\.1 https://pilot\.example\.dev/version$' "$STUB_LOG"
	# The first release creates caddy with the rest; there is no previous Caddyfile to compare.
	refute_grep -q 'force-recreate' "$STUB_LOG"
	refute_grep -q 'in the environment of codeatlas-compose' "$STUB_LOG"
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

# A failed check after the switch (research R7, steps 5 and 6) rolls back to the previous release.

# Releases FIRST, then prepares SECOND's bundle (with a changed Caddyfile when $1 is given).
prepare_second_release() {
	make_bundle "$FIRST"
	release "$FIRST"
	[ "$status" -eq 0 ]
	make_bundle "$SECOND" "${1:-}"
	: >"$STUB_LOG"
}

# current, .env, and state/running name FIRST again, and state/previous is as before SECOND.
assert_back_on_first() {
	[ "$(readlink "$CODEATLAS_ROOT/current")" = "$CODEATLAS_ROOT/releases/$FIRST" ]
	grep -qxF "IMAGE_TAG=$FIRST" "$CODEATLAS_ROOT/.env"
	[ "$(cat "$CODEATLAS_DATA/state/running")" = "$FIRST" ]
	[ ! -e "$CODEATLAS_DATA/state/previous" ]
}

@test "a failed check starts the previous release again and exits 3" {
	prepare_second_release "# changed"
	STUB_BROKEN="$SECOND" release "$SECOND"
	[ "$status" -eq 3 ]
	assert_back_on_first
	grep -q " pilot $SECOND check failed, rolled back to $FIRST$" "$CODEATLAS_LOG_DIR/releases.log"

	# The Caddyfiles differ, so caddy is recreated after each start.
	ups="$(grep '^codeatlas-compose up' "$STUB_LOG" | tr '\n' ',')"
	[ "$ups" = "codeatlas-compose up -d --remove-orphans,codeatlas-compose up -d --force-recreate caddy,codeatlas-compose up -d --remove-orphans,codeatlas-compose up -d --force-recreate caddy," ]
	# Compose takes the tag from .env, not from the script's environment.
	refute_grep -q 'in the environment of codeatlas-compose' "$STUB_LOG"
	# The rollback waits until the previous release is ready: its /readyz is the last request.
	tail -n 1 "$STUB_LOG" | grep -qF 'https://pilot.example.dev/readyz'
	refute_grep -q 'codeatlas-compose stop' "$STUB_LOG"
	# Nothing is pulled or migrated for the rollback.
	[ "$(grep -c ' pull ' "$STUB_LOG")" -eq 1 ]
	[ "$(grep -c 'run --rm migrate' "$STUB_LOG")" -eq 1 ]
}

@test "the check needs the web app too, and a rollback keeps caddy when the Caddyfiles match" {
	prepare_second_release
	STUB_BROKEN="$SECOND:/" release "$SECOND"
	[ "$status" -eq 3 ]
	assert_back_on_first
	ups="$(grep '^codeatlas-compose up' "$STUB_LOG" | tr '\n' ',')"
	[ "$ups" = "codeatlas-compose up -d --remove-orphans,codeatlas-compose up -d --remove-orphans," ]
}

@test "the check needs /version to report the released commit" {
	prepare_second_release
	# The new release answers, but reports another commit.
	STUB_VERSION="$FIRST" release "$SECOND"
	[ "$status" -eq 3 ]
	assert_back_on_first
	[[ "$output" == *"expected $SECOND"* ]]
}

@test "--expect-version changes the version that the check expects" {
	prepare_second_release
	release "$SECOND" --expect-version wrong
	[ "$status" -eq 3 ]
	assert_back_on_first
	[[ "$output" == *"expected wrong"* ]]

	STUB_VERSION=v-custom release "$SECOND" --expect-version v-custom
	[ "$status" -eq 0 ]
	[ "$(readlink "$CODEATLAS_ROOT/current")" = "$CODEATLAS_ROOT/releases/$SECOND" ]
	[ "$(cat "$CODEATLAS_DATA/state/running")" = "$SECOND" ]
	[ "$(cat "$CODEATLAS_DATA/state/previous")" = "$FIRST" ]
}

@test "a failed check of the running commit released again returns to the previous commit" {
	prepare_second_release
	release "$SECOND"
	[ "$status" -eq 0 ]
	: >"$STUB_LOG"
	# The forced rollback: the check cannot pass, so the previous release starts without migrating.
	release "$SECOND" --expect-version rollback
	[ "$status" -eq 3 ]
	[ "$(readlink "$CODEATLAS_ROOT/current")" = "$CODEATLAS_ROOT/releases/$FIRST" ]
	grep -qxF "IMAGE_TAG=$FIRST" "$CODEATLAS_ROOT/.env"
	# The commit it left becomes the previous one, so releasing state/previous goes forward again.
	[ "$(cat "$CODEATLAS_DATA/state/running")" = "$FIRST" ]
	[ "$(cat "$CODEATLAS_DATA/state/previous")" = "$SECOND" ]
	[ "$(grep -c 'run --rm migrate' "$STUB_LOG")" -eq 1 ]
	grep -q " pilot $SECOND check failed, rolled back to $FIRST$" "$CODEATLAS_LOG_DIR/releases.log"
}

@test "a failed check on a host without a previous release stops the new services and exits 4" {
	make_bundle "$FIRST"
	STUB_BROKEN="$FIRST" release "$FIRST"
	[ "$status" -eq 4 ]
	grep -qxF "codeatlas-compose stop api worker web" "$STUB_LOG"
	[ "$(grep -c '^codeatlas-compose up' "$STUB_LOG")" -eq 1 ]
	[ ! -e "$CODEATLAS_DATA/state/running" ]
	[ ! -e "$CODEATLAS_DATA/state/previous" ]
	grep -q " pilot $FIRST check failed, no previous release, stopped the new services$" \
		"$CODEATLAS_LOG_DIR/releases.log"
}

@test "a failed check exits 4 when the previous release does not become ready either" {
	prepare_second_release
	STUB_BROKEN="$FIRST $SECOND" release "$SECOND"
	[ "$status" -eq 4 ]
	# The previous release is in place and started; nothing is stopped.
	[ "$(readlink "$CODEATLAS_ROOT/current")" = "$CODEATLAS_ROOT/releases/$FIRST" ]
	grep -qxF "IMAGE_TAG=$FIRST" "$CODEATLAS_ROOT/.env"
	[ "$(grep -c '^codeatlas-compose up -d --remove-orphans' "$STUB_LOG")" -eq 2 ]
	refute_grep -q 'codeatlas-compose stop' "$STUB_LOG"
	grep -q " pilot $SECOND check failed, rollback to $FIRST failed$" "$CODEATLAS_LOG_DIR/releases.log"
}

@test "a failing start after the switch rolls back too" {
	prepare_second_release
	# The stub fails every `up`, so the previous release cannot start either.
	STUB_FAIL=up release "$SECOND"
	[ "$status" -eq 4 ]
	[ "$(readlink "$CODEATLAS_ROOT/current")" = "$CODEATLAS_ROOT/releases/$FIRST" ]
	grep -qxF "IMAGE_TAG=$FIRST" "$CODEATLAS_ROOT/.env"
	[ "$(grep -c '^codeatlas-compose up -d --remove-orphans' "$STUB_LOG")" -eq 2 ]
	grep -q " pilot $SECOND .*rollback to $FIRST failed$" "$CODEATLAS_LOG_DIR/releases.log"
}

@test "a failed check exits 4 when the previous release's bundle is not on the host" {
	prepare_second_release
	release "$SECOND"
	[ "$status" -eq 0 ]
	# As on a replaced host, which has only the bundle it was given.
	rm -rf "${CODEATLAS_ROOT:?}/releases/$FIRST"
	: >"$STUB_LOG"
	STUB_BROKEN="$SECOND" release "$SECOND"
	[ "$status" -eq 4 ]
	grep -qxF "codeatlas-compose stop api worker web" "$STUB_LOG"
	[ "$(readlink "$CODEATLAS_ROOT/current")" = "$CODEATLAS_ROOT/releases/$SECOND" ]
}

# --refresh-config: new configuration for the running release, such as a replaced credential.

@test "--refresh-config renders the configuration again and recreates only api and worker" {
	make_bundle "$FIRST"
	release "$FIRST"
	[ "$status" -eq 0 ]
	cp "$CODEATLAS_ROOT/.env" "$BATS_TEST_TMPDIR/env.before"
	jq '(.Parameters[] | select(.Name | endswith("/GEMINI_API_KEY")) | .Value) = "secret-gemini-replaced"' \
		"$STUB_SSM_RESPONSE" >"$BATS_TEST_TMPDIR/replaced.json"
	mv "$BATS_TEST_TMPDIR/replaced.json" "$STUB_SSM_RESPONSE"
	: >"$STUB_LOG"

	run "$CODEATLAS_ROOT/current/release.sh" "$FIRST" --refresh-config
	[ "$status" -eq 0 ]
	grep -qF 'secret-gemini-replaced' "$CODEATLAS_ROOT/runtime/app.env"
	[[ "$output" != *secret-gemini-replaced* ]]
	grep -qxF "codeatlas-compose up -d --force-recreate api worker" "$STUB_LOG"
	[ "$(grep -c '^codeatlas-compose' "$STUB_LOG")" -eq 1 ]
	refute_grep -q 'get-login-password\| pull \|run --rm migrate' "$STUB_LOG"
	grep -qF 'https://pilot.example.dev/readyz' "$STUB_LOG"
	cmp -s "$CODEATLAS_ROOT/.env" "$BATS_TEST_TMPDIR/env.before"
	[ "$(readlink "$CODEATLAS_ROOT/current")" = "$CODEATLAS_ROOT/releases/$FIRST" ]
	[ "$(cat "$CODEATLAS_DATA/state/running")" = "$FIRST" ]
	grep -q " pilot $FIRST configuration refreshed$" "$CODEATLAS_LOG_DIR/releases.log"
}

@test "--refresh-config refuses a commit that is not running" {
	prepare_second_release
	release "$SECOND" --refresh-config
	[ "$status" -eq 2 ]
	refute_grep -q '^codeatlas-compose\|ssm get-parameters' "$STUB_LOG"
	[ "$(readlink "$CODEATLAS_ROOT/current")" = "$CODEATLAS_ROOT/releases/$FIRST" ]
}

@test "--refresh-config with a missing parameter exits 2 without recreating anything" {
	make_bundle "$FIRST"
	release "$FIRST"
	[ "$status" -eq 0 ]
	jq '.Parameters |= map(select(.Name | endswith("/GEMINI_API_KEY") | not))' \
		"$STUB_SSM_RESPONSE" >"$BATS_TEST_TMPDIR/partial.json"
	mv "$BATS_TEST_TMPDIR/partial.json" "$STUB_SSM_RESPONSE"
	: >"$STUB_LOG"
	release "$FIRST" --refresh-config
	[ "$status" -eq 2 ]
	[[ "$output" == *GEMINI_API_KEY* ]]
	refute_grep -q '^codeatlas-compose' "$STUB_LOG"
}

@test "--refresh-config exits 4 when the check fails afterward, without rolling back" {
	make_bundle "$FIRST"
	release "$FIRST"
	[ "$status" -eq 0 ]
	: >"$STUB_LOG"
	STUB_BROKEN="$FIRST" release "$FIRST" --refresh-config
	[ "$status" -eq 4 ]
	[ "$(grep -c '^codeatlas-compose' "$STUB_LOG")" -eq 1 ]
	[ "$(cat "$CODEATLAS_DATA/state/running")" = "$FIRST" ]
	grep -q " pilot $FIRST configuration refreshed, check failed$" "$CODEATLAS_LOG_DIR/releases.log"
}

@test "release.sh refuses unknown options and a missing --expect-version value" {
	make_bundle "$FIRST"
	release "$FIRST" --force
	[ "$status" -eq 2 ]
	release "$FIRST" --expect-version
	[ "$status" -eq 2 ]
	[ ! -e "$CODEATLAS_ROOT/current" ]
	refute_grep -q '^aws\|^codeatlas-compose' "$STUB_LOG"
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
