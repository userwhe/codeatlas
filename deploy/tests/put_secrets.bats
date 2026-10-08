#!/usr/bin/env bats
# put-secrets.sh: writing an environment's SSM parameters (research R5; contracts/operations.md,
# "SSM parameters").

load helpers

setup_file() {
	ensure_tools jq openssl
}

setup() {
	setup_host
	no_parameters
}

no_parameters() {
	echo '{"Parameters": []}' >"$STUB_SSM_RESPONSE"
}

existing_parameters() {
	local name
	for name in "$@"; do
		jq -n --arg name "/codeatlas/$ENVIRONMENT_NAME/$name" '{Name: $name, Value: "existing"}'
	done | jq -s '{Parameters: .}' >"$STUB_SSM_RESPONSE"
}

value_of() {
	cat "$STUB_VALUES/$1"
}

type_of() {
	cat "$STUB_VALUES/$1.type"
}

put_secrets() {
	run "$DEPLOY_DIR/put-secrets.sh" "$@"
}

write_key_file() {
	KEY_FILE="$BATS_TEST_TMPDIR/github-app.pem"
	printf '%s' "$PRIVATE_KEY_VALUE" >"$KEY_FILE"
}

# The pilot's prompts, in order: the App's ID, slug, and client ID (shown), then the client
# secret, the webhook secret, and the two provider keys (hidden).
pilot_answers() {
	printf '%s\n' 123456 codeatlas-example Iv1.0123456789abcdef "$CLIENT_SECRET_VALUE" \
		"$WEBHOOK_VALUE" "$GEMINI_VALUE" "$VOYAGE_VALUE"
}

@test "loadtest writes its mode settings and generated keys, and no GitHub or provider secret" {
	put_secrets loadtest
	[ "$status" -eq 0 ]

	[ "$(value_of CODEATLAS_ENV)" = development ]
	[ "$(value_of CODEATLAS_FAKE_EXTERNALS)" = 1 ]
	[ "$(value_of DAILY_QUESTION_LIMIT)" = 100000 ]
	[ "$(value_of PILOT_DAILY_QUESTION_LIMIT)" = 100000 ]
	[ "$(value_of PILOT_DAILY_REVIEW_LIMIT)" = 100000 ]
	[ "$(value_of RATE_LIMIT_PER_MINUTE)" = 0 ]
	[ "$(type_of CODEATLAS_ENV)" = String ]

	[[ "$(value_of TOKEN_ENCRYPTION_KEY)" =~ ^[A-Za-z0-9_-]{43}=$ ]]
	[[ "$(value_of POSTGRES_PASSWORD)" =~ ^[0-9a-f]{48}$ ]]
	[ "$(type_of TOKEN_ENCRYPTION_KEY)" = SecureString ]
	[ "$(type_of POSTGRES_PASSWORD)" = SecureString ]

	[ -z "$(find "$STUB_VALUES" -name 'GITHUB_*' -o -name '*_API_KEY')" ]
	[[ "$output" != *"$(value_of TOKEN_ENCRYPTION_KEY)"* ]]
	[[ "$output" != *"$(value_of POSTGRES_PASSWORD)"* ]]
	refute_grep -qF "$(value_of POSTGRES_PASSWORD)" "$STUB_LOG"
}

@test "the pilot prompts for its credentials and never shows or passes a secret on the command line" {
	write_key_file
	put_secrets pilot --github-app-private-key "$KEY_FILE" < <(pilot_answers)
	[ "$status" -eq 0 ]

	[ "$(value_of CODEATLAS_ENV)" = production ]
	[ "$(value_of CODEATLAS_FAKE_EXTERNALS)" = 0 ]
	[ "$(value_of EMIT_METRICS)" = 1 ]
	[ "$(value_of GITHUB_APP_ID)" = 123456 ]
	[ "$(type_of GITHUB_APP_ID)" = String ]
	[ "$(value_of GITHUB_APP_SLUG)" = codeatlas-example ]
	[ "$(value_of GITHUB_APP_CLIENT_ID)" = Iv1.0123456789abcdef ]
	[ "$(value_of GITHUB_APP_CLIENT_SECRET)" = "$CLIENT_SECRET_VALUE" ]
	[ "$(value_of GITHUB_WEBHOOK_SECRET)" = "$WEBHOOK_VALUE" ]
	[ "$(value_of GEMINI_API_KEY)" = "$GEMINI_VALUE" ]
	[ "$(value_of VOYAGE_API_KEY)" = "$VOYAGE_VALUE" ]
	[ "$(value_of GITHUB_APP_PRIVATE_KEY)" = "$(printf '%s' "$PRIVATE_KEY_VALUE")" ]
	for name in GITHUB_APP_CLIENT_SECRET GITHUB_WEBHOOK_SECRET GEMINI_API_KEY VOYAGE_API_KEY \
		GITHUB_APP_PRIVATE_KEY TOKEN_ENCRYPTION_KEY POSTGRES_PASSWORD; do
		[ "$(type_of "$name")" = SecureString ]
	done

	refute_secrets_in "$output"
	refute_secrets_in "$(cat "$STUB_LOG")"
	# The temporary value files are removed; the key file is the developer's own.
	[ "$(grep -c 'file://' "$STUB_LOG")" -eq 13 ]
	while read -r file; do
		[ "$file" = "$KEY_FILE" ] || [ ! -e "$file" ]
	done < <(grep -o 'file://[^ ]*' "$STUB_LOG" | sed 's|^file://||')
}

@test "the pilot needs the private key file before anything is written" {
	put_secrets pilot < <(pilot_answers)
	[ "$status" -ne 0 ]
	[[ "$output" == *--github-app-private-key* ]]
	refute_grep -q 'put-parameter' "$STUB_LOG"
}

@test "an empty answer writes nothing" {
	write_key_file
	put_secrets pilot --github-app-private-key "$KEY_FILE" < <(printf '123456\n\n')
	[ "$status" -ne 0 ]
	[[ "$output" == *GITHUB_APP_SLUG* ]]
	refute_grep -q 'put-parameter' "$STUB_LOG"
}

@test "existing parameters are kept unless --overwrite names them" {
	ENVIRONMENT_NAME=loadtest existing_parameters CODEATLAS_ENV TOKEN_ENCRYPTION_KEY POSTGRES_PASSWORD
	put_secrets loadtest
	[ "$status" -eq 0 ]
	[ ! -e "$STUB_VALUES/CODEATLAS_ENV" ]
	[ ! -e "$STUB_VALUES/TOKEN_ENCRYPTION_KEY" ]
	[ ! -e "$STUB_VALUES/POSTGRES_PASSWORD" ]
	[ "$(value_of CODEATLAS_FAKE_EXTERNALS)" = 1 ]
	[[ "$output" == *"kept TOKEN_ENCRYPTION_KEY"* ]]
	refute_grep -q -- '--overwrite' "$STUB_LOG"
}

@test "--overwrite generates a new token encryption key and replaces the parameter" {
	ENVIRONMENT_NAME=loadtest existing_parameters CODEATLAS_ENV CODEATLAS_FAKE_EXTERNALS \
		DAILY_QUESTION_LIMIT PILOT_DAILY_QUESTION_LIMIT PILOT_DAILY_REVIEW_LIMIT \
		RATE_LIMIT_PER_MINUTE TOKEN_ENCRYPTION_KEY POSTGRES_PASSWORD
	put_secrets loadtest --overwrite TOKEN_ENCRYPTION_KEY
	[ "$status" -eq 0 ]
	[[ "$(value_of TOKEN_ENCRYPTION_KEY)" =~ ^[A-Za-z0-9_-]{43}=$ ]]
	[ "$(grep -c 'put-parameter' "$STUB_LOG")" -eq 1 ]
	grep -q 'put-parameter .*/TOKEN_ENCRYPTION_KEY .*--overwrite' "$STUB_LOG"
}

@test "--overwrite POSTGRES_PASSWORD is refused with a pointer to the operations guide" {
	put_secrets loadtest --overwrite POSTGRES_PASSWORD
	[ "$status" -ne 0 ]
	[[ "$output" == *"ALTER ROLE"* ]]
	[[ "$output" == *docs/operations.md* ]]
	refute_grep -q 'put-parameter' "$STUB_LOG"
}

@test "--overwrite refuses a name that is not a parameter of the environment" {
	put_secrets loadtest --overwrite GEMINI_API_KEY
	[ "$status" -ne 0 ]
	refute_grep -q 'put-parameter' "$STUB_LOG"
}

@test "--set writes one optional setting and --unset deletes it" {
	put_secrets pilot --set PILOT_DAILY_QUESTION_LIMIT=2
	[ "$status" -eq 0 ]
	[ "$(value_of PILOT_DAILY_QUESTION_LIMIT)" = 2 ]
	[ "$(type_of PILOT_DAILY_QUESTION_LIMIT)" = String ]
	grep -q 'put-parameter .*/codeatlas/pilot/PILOT_DAILY_QUESTION_LIMIT .*--overwrite' "$STUB_LOG"
	[ "$(grep -c 'put-parameter' "$STUB_LOG")" -eq 1 ]

	put_secrets pilot --unset PILOT_DAILY_QUESTION_LIMIT
	[ "$status" -eq 0 ]
	grep -q 'ssm delete-parameter .*--name /codeatlas/pilot/PILOT_DAILY_QUESTION_LIMIT' "$STUB_LOG"
}

@test "--set and --unset take only the optional limits and switches, with integer values" {
	put_secrets pilot --set GEMINI_API_KEY=1
	[ "$status" -ne 0 ]
	put_secrets pilot --set RATE_LIMIT_PER_MINUTE=abc
	[ "$status" -ne 0 ]
	put_secrets pilot --unset TOKEN_ENCRYPTION_KEY
	[ "$status" -ne 0 ]
	refute_grep -q 'put-parameter\|delete-parameter' "$STUB_LOG"
}

@test "the drill writes nothing" {
	put_secrets drill
	[ "$status" -ne 0 ]
	[[ "$output" == */codeatlas/pilot* ]]
	refute_grep -q '^aws' "$STUB_LOG"
}
