#!/usr/bin/env bash
# Renders the host's runtime configuration from the environment's SSM parameters
# (specs/004-pilot-deployment research R5; contracts/operations.md, "SSM parameters").
#
# Usage: render-config.sh <environment>
#
# Reads every parameter under PARAMETERS_PATH (from /opt/codeatlas/environment) and writes, under
# /opt/codeatlas/runtime/:
#   app.env         (0600) every parameter except GITHUB_APP_PRIVATE_KEY and POSTGRES_PASSWORD, plus
#                   the derived APP_ORIGIN, DATABASE_URL, METRICS_ENVIRONMENT, and, when the key
#                   exists, GITHUB_APP_PRIVATE_KEY_PATH; EMIT_METRICS is forced to 0 outside the pilot
#   db.env          (0600) the database's user, name, and password
#   github-app.pem  (0400, owned by the containers' UID 1000) the GitHub App's private key, or an
#                   empty file in fake mode, so the Compose mount always has a file as its source
#
# Values are written double-quoted with `\`, `"`, and `$` escaped (`$` as `$$`), which Compose reads
# back exactly: no interpolation and no cut at ` #`. Single quotes cannot hold every value, because
# Compose's parser has no way to write a `'` that follows a `\` inside them.
#
# Each file is written to a temporary name and renamed, so a failure leaves the previous files.
# Prints parameter names, never values. Exits 1 naming each missing required parameter.
#
# CODEATLAS_ROOT (default /opt/codeatlas) and CODEATLAS_KEY_OWNER (default 1000) exist for tests.
#
# The jq programs below are single-quoted on purpose.
# shellcheck disable=SC2016
set -euo pipefail

ROOT="${CODEATLAS_ROOT:-/opt/codeatlas}"
KEY_OWNER="${CODEATLAS_KEY_OWNER:-1000}"
KEY_PATH_IN_CONTAINER=/run/secrets/github-app.pem

ALWAYS_REQUIRED=(CODEATLAS_ENV CODEATLAS_FAKE_EXTERNALS TOKEN_ENCRYPTION_KEY POSTGRES_PASSWORD)
PRODUCTION_REQUIRED=(
	GITHUB_APP_ID GITHUB_APP_SLUG GITHUB_APP_CLIENT_ID GITHUB_APP_CLIENT_SECRET
	GITHUB_APP_PRIVATE_KEY GITHUB_WEBHOOK_SECRET GEMINI_API_KEY VOYAGE_API_KEY
)

die() {
	printf 'render-config: %s\n' "$*" >&2
	exit 1
}

if [[ $# -ne 1 ]]; then
	echo "usage: render-config.sh <environment>" >&2
	exit 1
fi
requested="$1"

[[ -r "$ROOT/environment" ]] || die "$ROOT/environment is missing"
# shellcheck source=/dev/null
. "$ROOT/environment"
for name in ENVIRONMENT AWS_REGION CODEATLAS_HOSTNAME PARAMETERS_PATH; do
	[[ -n "${!name:-}" ]] || die "$name is not set in $ROOT/environment"
done
[[ "$requested" == "$ENVIRONMENT" ]] ||
	die "this host runs the $ENVIRONMENT environment, not $requested"
command -v jq >/dev/null || die "jq is required"

response="$(aws ssm get-parameters-by-path --region "$AWS_REGION" --path "$PARAMETERS_PATH" \
	--recursive --with-decryption --output json)"

# One object from name to value, with the path removed from each name.
params="$(printf '%s' "$response" | jq -c --arg prefix "${PARAMETERS_PATH%/}/" \
	'[.Parameters[] | {key: (.Name | ltrimstr($prefix)), value: .Value}] | from_entries')"

# Runs a jq filter over the parameters.
query() {
	printf '%s' "$params" | jq "$@"
}

invalid="$(query -r '[keys[] | select(test("^[A-Z][A-Z0-9_]*$") | not)] | join(", ")')"
[[ -z "$invalid" ]] || die "parameters with invalid names under $PARAMETERS_PATH: $invalid"

required=("${ALWAYS_REQUIRED[@]}")
if [[ "$(query -r '.CODEATLAS_ENV // ""')" == production ]]; then
	required+=("${PRODUCTION_REQUIRED[@]}")
fi
missing="$(query -r '[$ARGS.positional[] as $name | select((.[$name] // "") == "") | $name]
	| join(", ")' --args "${required[@]}")"
[[ -z "$missing" ]] || die "missing required parameters under $PARAMETERS_PATH: $missing"

multiline="$(query -r '[to_entries[] | select(.key != "GITHUB_APP_PRIVATE_KEY")
	| select(.value | test("[\r\n]")) | .key] | join(", ")')"
[[ -z "$multiline" ]] || die "parameters with line breaks in their values: $multiline"

# The password goes into DATABASE_URL, and Alembic's configuration treats `%` as special, so only
# characters that need no escaping in a URL are accepted. put-secrets.sh generates hexadecimal.
query -e '.POSTGRES_PASSWORD | test("^[A-Za-z0-9._~-]+$")' >/dev/null ||
	die "POSTGRES_PASSWORD may contain only letters, digits, '.', '_', '~', and '-'"

has_key="$(query -r 'has("GITHUB_APP_PRIVATE_KEY")')"
emit_metrics_forced=true
[[ "$ENVIRONMENT" == pilot ]] && emit_metrics_forced=false

# A Compose env_file line: NAME="value" with `\`, `"`, and `$` escaped.
JQ_QUOTE='def quote: "\"" + (gsub("\\\\"; "\\\\") | gsub("\""; "\\\"") | gsub("\\$"; "$$")) + "\"";'

app_env="$(query -r \
	--arg origin "https://$CODEATLAS_HOSTNAME" \
	--arg environment "$ENVIRONMENT" \
	--arg key_path "$KEY_PATH_IN_CONTAINER" \
	--argjson has_key "$has_key" \
	--argjson emit_metrics_forced "$emit_metrics_forced" \
	"$JQ_QUOTE"'
	. as $p
	| (["GITHUB_APP_PRIVATE_KEY", "POSTGRES_PASSWORD", "APP_ORIGIN", "DATABASE_URL",
		"GITHUB_APP_PRIVATE_KEY_PATH", "METRICS_ENVIRONMENT"]
		+ (if $emit_metrics_forced then ["EMIT_METRICS"] else [] end)) as $derived
	| (to_entries | sort_by(.key) | .[] | select(.key as $k | ($derived | index($k)) == null)
		| "\(.key)=\(.value | quote)"),
	"APP_ORIGIN=\($origin | quote)",
	"DATABASE_URL=\("postgresql+psycopg://codeatlas:\($p.POSTGRES_PASSWORD)@db:5432/codeatlas" | quote)",
	"METRICS_ENVIRONMENT=\($environment | quote)",
	(if $emit_metrics_forced then "EMIT_METRICS=\("0" | quote)" else empty end),
	(if $has_key then "GITHUB_APP_PRIVATE_KEY_PATH=\($key_path | quote)" else empty end)
	')"

db_env="$(query -r "$JQ_QUOTE"'
	"POSTGRES_USER=\("codeatlas" | quote)",
	"POSTGRES_DB=\("codeatlas" | quote)",
	"POSTGRES_PASSWORD=\(.POSTGRES_PASSWORD | quote)"
	')"

umask 077
runtime="$ROOT/runtime"
mkdir -p "$runtime"
chmod 0700 "$runtime"

temporary=()
cleanup() {
	if [[ ${#temporary[@]} -gt 0 ]]; then
		rm -f "${temporary[@]}"
	fi
}
trap cleanup EXIT

new_temporary() {
	local file
	file="$(mktemp "$runtime/.$1.XXXXXX")"
	temporary+=("$file")
	printf '%s' "$file"
}

app_file="$(new_temporary app.env)"
printf '%s\n' "$app_env" >"$app_file"
chmod 0600 "$app_file"

db_file="$(new_temporary db.env)"
printf '%s\n' "$db_env" >"$db_file"
chmod 0600 "$db_file"

key_file="$(new_temporary github-app.pem)"
query -j '.GITHUB_APP_PRIVATE_KEY // ""' >"$key_file"
chown "$KEY_OWNER" "$key_file"
chmod 0400 "$key_file"

mv -f "$app_file" "$runtime/app.env"
mv -f "$db_file" "$runtime/db.env"
mv -f "$key_file" "$runtime/github-app.pem"
temporary=()

settings="$(printf '%s\n' "$app_env" | wc -l | tr -d ' ')"
if [[ "$has_key" == true ]]; then
	key_state="the GitHub App's private key"
else
	key_state="no private key (fake mode)"
fi
echo "render-config: wrote $runtime: app.env ($settings settings), db.env, $key_state"
