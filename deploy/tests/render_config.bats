#!/usr/bin/env bats
# render-config.sh: SSM parameters and derived settings to runtime/ (research R5 and R18;
# contracts/operations.md, "SSM parameters").

load helpers

setup_file() {
	ensure_tools jq
}

setup() {
	REAL_DOCKER="$(command -v docker || true)"
	setup_host
	RUNTIME="$CODEATLAS_ROOT/runtime"
}

render() {
	run "$DEPLOY_DIR/render-config.sh" "$@"
}

@test "a production set writes app.env with the derived settings and escaped values" {
	production_parameters
	render pilot
	[ "$status" -eq 0 ]

	app_env="$(cat "$RUNTIME/app.env")"
	grep -qxF 'APP_ORIGIN="https://pilot.example.dev"' <<<"$app_env"
	grep -qxF "DATABASE_URL=\"postgresql+psycopg://codeatlas:$DB_PASSWORD_VALUE@db:5432/codeatlas\"" <<<"$app_env"
	grep -qxF 'METRICS_ENVIRONMENT="pilot"' <<<"$app_env"
	grep -qxF 'GITHUB_APP_PRIVATE_KEY_PATH="/run/secrets/github-app.pem"' <<<"$app_env"
	grep -qxF 'CODEATLAS_ENV="production"' <<<"$app_env"
	grep -qxF 'EMIT_METRICS="1"' <<<"$app_env"
	# `$` doubled, so Compose does not interpolate it; `"` and `\` escaped; ` #` kept.
	grep -qxF 'GEMINI_API_KEY="secret-gemini-$$HOME #7f3a"' <<<"$app_env"
	grep -qxF 'GITHUB_WEBHOOK_SECRET="secret-webhook-$${X:-y}"' <<<"$app_env"
	grep -qxF 'VOYAGE_API_KEY="secret-voyage-it'"'"'s-\\\"q\\\"-\\\\"' <<<"$app_env"

	# The key and the database password are not settings of the application.
	refute_grep -q '^GITHUB_APP_PRIVATE_KEY=' <<<"$app_env"
	refute_grep -q '^POSTGRES_PASSWORD=' <<<"$app_env"
}

@test "a production set writes db.env and the private key with restricted modes" {
	production_parameters
	render pilot
	[ "$status" -eq 0 ]

	[ "$(mode_and_owner "$RUNTIME/app.env")" = "600 $(id -u)" ]
	[ "$(mode_and_owner "$RUNTIME/db.env")" = "600 $(id -u)" ]
	[ "$(mode_and_owner "$RUNTIME/github-app.pem")" = "400 $KEY_OWNER" ]

	grep -qxF 'POSTGRES_USER="codeatlas"' "$RUNTIME/db.env"
	grep -qxF 'POSTGRES_DB="codeatlas"' "$RUNTIME/db.env"
	grep -qxF "POSTGRES_PASSWORD=\"$DB_PASSWORD_VALUE\"" "$RUNTIME/db.env"
	[ "$(cat "$RUNTIME/github-app.pem"; echo .)" = "$PRIVATE_KEY_VALUE." ]
}

@test "render-config never prints a value" {
	production_parameters
	render pilot
	[ "$status" -eq 0 ]
	refute_secrets_in "$output"
}

@test "every value is written quoted" {
	production_parameters
	render pilot
	[ "$status" -eq 0 ]
	while IFS= read -r line; do
		[[ "$line" =~ ^[A-Z][A-Z0-9_]*=\".*\"$ ]] || { echo "unquoted: ${line%%=*}"; false; }
	done <"$RUNTIME/app.env"
	while IFS= read -r line; do
		[[ "$line" =~ ^[A-Z][A-Z0-9_]*=\".*\"$ ]] || { echo "unquoted: ${line%%=*}"; false; }
	done <"$RUNTIME/db.env"
}

@test "a missing production parameter fails naming it and keeps the previous files" {
	production_parameters
	render pilot
	[ "$status" -eq 0 ]
	cp "$RUNTIME/app.env" "$BATS_TEST_TMPDIR/app.env.before"

	jq '.Parameters |= map(select(.Name | test("/(GEMINI_API_KEY|GITHUB_APP_PRIVATE_KEY)$") | not))' \
		"$STUB_SSM_RESPONSE" >"$BATS_TEST_TMPDIR/partial.json"
	mv "$BATS_TEST_TMPDIR/partial.json" "$STUB_SSM_RESPONSE"
	render pilot
	[ "$status" -ne 0 ]
	[[ "$output" == *GEMINI_API_KEY* ]]
	[[ "$output" == *GITHUB_APP_PRIVATE_KEY* ]]
	[[ "$output" != *VOYAGE_API_KEY* ]]
	refute_secrets_in "$output"
	cmp -s "$RUNTIME/app.env" "$BATS_TEST_TMPDIR/app.env.before"
	[ -z "$(find "$RUNTIME" -name '.*' -type f)" ]
}

@test "the always-required parameters are required in fake mode too" {
	fake_mode_parameters /codeatlas/pilot
	jq '.Parameters |= map(select(.Name | endswith("/POSTGRES_PASSWORD") | not))' \
		"$STUB_SSM_RESPONSE" >"$BATS_TEST_TMPDIR/partial.json"
	mv "$BATS_TEST_TMPDIR/partial.json" "$STUB_SSM_RESPONSE"
	render pilot
	[ "$status" -ne 0 ]
	[[ "$output" == *POSTGRES_PASSWORD* ]]
	[ ! -e "$RUNTIME/app.env" ]
}

@test "a fake-mode set without GitHub parameters succeeds and forces EMIT_METRICS=0" {
	write_environment loadtest
	fake_mode_parameters
	render loadtest
	[ "$status" -eq 0 ]

	app_env="$(cat "$RUNTIME/app.env")"
	grep -qxF 'APP_ORIGIN="https://loadtest.example.dev"' <<<"$app_env"
	grep -qxF 'METRICS_ENVIRONMENT="loadtest"' <<<"$app_env"
	grep -qxF 'EMIT_METRICS="0"' <<<"$app_env"
	grep -qxF 'CODEATLAS_FAKE_EXTERNALS="1"' <<<"$app_env"
	grep -qxF 'RATE_LIMIT_PER_MINUTE="0"' <<<"$app_env"
	refute_grep -q '^GITHUB_APP_PRIVATE_KEY_PATH=' <<<"$app_env"
	# An empty key file keeps Compose from creating a directory at the mount's source.
	[ "$(mode_and_owner "$RUNTIME/github-app.pem")" = "400 $KEY_OWNER" ]
	[ ! -s "$RUNTIME/github-app.pem" ]
}

@test "the drill reads the pilot's parameters but keeps its own origin and no metrics" {
	write_environment drill /codeatlas/pilot
	production_parameters /codeatlas/pilot
	render drill
	[ "$status" -eq 0 ]

	grep -q -- '--path /codeatlas/pilot ' "$STUB_LOG"
	app_env="$(cat "$RUNTIME/app.env")"
	grep -qxF 'APP_ORIGIN="https://drill.example.dev"' <<<"$app_env"
	grep -qxF 'METRICS_ENVIRONMENT="drill"' <<<"$app_env"
	grep -qxF 'EMIT_METRICS="0"' <<<"$app_env"
	[ "$(grep -c '^EMIT_METRICS=' <<<"$app_env")" -eq 1 ]
}

@test "a parameter cannot replace a derived setting" {
	write_environment loadtest
	fake_mode_parameters
	jq '.Parameters += [{Name: "/codeatlas/loadtest/APP_ORIGIN", Value: "http://elsewhere"}]' \
		"$STUB_SSM_RESPONSE" >"$BATS_TEST_TMPDIR/extra.json"
	mv "$BATS_TEST_TMPDIR/extra.json" "$STUB_SSM_RESPONSE"
	render loadtest
	[ "$status" -eq 0 ]
	[ "$(grep -c '^APP_ORIGIN=' "$RUNTIME/app.env")" -eq 1 ]
	grep -qxF 'APP_ORIGIN="https://loadtest.example.dev"' "$RUNTIME/app.env"
}

@test "a database password that does not fit in a URL is refused" {
	write_environment loadtest
	fake_mode_parameters
	jq '(.Parameters[] | select(.Name | endswith("/POSTGRES_PASSWORD")) | .Value) = "pass@word"' \
		"$STUB_SSM_RESPONSE" >"$BATS_TEST_TMPDIR/bad.json"
	mv "$BATS_TEST_TMPDIR/bad.json" "$STUB_SSM_RESPONSE"
	render loadtest
	[ "$status" -ne 0 ]
	[[ "$output" == *POSTGRES_PASSWORD* ]]
	[[ "$output" != *pass@word* ]]
}

@test "an environment other than the host's is refused" {
	production_parameters
	render loadtest
	[ "$status" -ne 0 ]
	[[ "$output" == *pilot* ]]
	[ ! -e "$RUNTIME/app.env" ]
}

@test "a failing parameter read leaves the previous files" {
	production_parameters
	render pilot
	[ "$status" -eq 0 ]
	cp "$RUNTIME/app.env" "$BATS_TEST_TMPDIR/app.env.before"
	STUB_FAIL=ssm render pilot
	[ "$status" -ne 0 ]
	cmp -s "$RUNTIME/app.env" "$BATS_TEST_TMPDIR/app.env.before"
}

@test "Compose reads every value back exactly" {
	if [ -z "$REAL_DOCKER" ] || ! "$REAL_DOCKER" compose version >/dev/null 2>&1; then
		skip "needs docker compose"
	fi
	production_parameters
	render pilot
	[ "$status" -eq 0 ]

	printf 'services:\n  t:\n    image: scratch\n    env_file: ./runtime/app.env\n' \
		>"$CODEATLAS_ROOT/compose.yml"
	"$REAL_DOCKER" compose --project-directory "$CODEATLAS_ROOT" -f "$CODEATLAS_ROOT/compose.yml" \
		config --format json >"$BATS_TEST_TMPDIR/config.json"
	# `config` prints a Compose file, so it writes each `$` of a value as `$$`.
	read_back() {
		jq -r --arg name "$1" '.services.t.environment[$name] | gsub("\\$\\$"; "$")' \
			"$BATS_TEST_TMPDIR/config.json"
	}
	[ "$(read_back GEMINI_API_KEY)" = "$GEMINI_VALUE" ]
	[ "$(read_back VOYAGE_API_KEY)" = "$VOYAGE_VALUE" ]
	[ "$(read_back GITHUB_WEBHOOK_SECRET)" = "$WEBHOOK_VALUE" ]
	[ "$(read_back GITHUB_APP_CLIENT_SECRET)" = "$CLIENT_SECRET_VALUE" ]
}
