# Shared setup for the host script tests (specs/004-pilot-deployment research R18).
#
# Each test gets a temporary directory standing in for /opt/codeatlas (CODEATLAS_ROOT),
# /var/lib/codeatlas (CODEATLAS_DATA), and /var/log/codeatlas (CODEATLAS_LOG_DIR), and stub `aws`,
# `docker`, `curl`, and `codeatlas-compose` commands first on PATH. Every stub appends its command
# line to $STUB_LOG; the `aws` stub also saves each parameter that `ssm put-parameter` writes under
# $STUB_VALUES/<name>, with its type in <name>.type, and keeps S3 objects under $STUB_BUCKET/<key>.
# `codeatlas-compose exec` and `run` run the service's command from $STUB_CONTAINER_BIN when a stub
# of that name is there (host_scripts.bats installs them). A stub fails when its failure keyword is
# listed in $STUB_FAIL:
#   ssm (aws ssm), ecr-login (aws ecr get-login-password), docker-login, pull, migrate,
#   up (codeatlas-compose up), curl, s3-download (aws s3 cp from S3), s3-upload
#   (aws s3api put-object), metric (aws cloudwatch put-metric-data).

DEPLOY_DIR="$(cd "$BATS_TEST_DIRNAME/.." && pwd)"
export DEPLOY_DIR

# The scripts need jq (and put-secrets.sh needs openssl). The bats container image is Alpine
# without them; GitHub's Ubuntu runners include both.
ensure_tools() {
	local tool missing=()
	for tool in "$@"; do
		command -v "$tool" >/dev/null || missing+=("$tool")
	done
	[ ${#missing[@]} -eq 0 ] && return 0
	if command -v apk >/dev/null && [ "$(id -u)" = 0 ]; then
		apk add --no-cache --quiet "${missing[@]}" >/dev/null
		return 0
	fi
	echo "these tests need: ${missing[*]}" >&2
	return 1
}

setup_host() {
	export CODEATLAS_ROOT="$BATS_TEST_TMPDIR/opt"
	export CODEATLAS_DATA="$BATS_TEST_TMPDIR/data"
	export CODEATLAS_LOG_DIR="$BATS_TEST_TMPDIR/log"
	export STUB_LOG="$BATS_TEST_TMPDIR/calls.log"
	export STUB_FAIL=""
	export STUB_SSM_RESPONSE="$BATS_TEST_TMPDIR/parameters.json"
	export STUB_VALUES="$BATS_TEST_TMPDIR/values"
	export STUB_BUCKET="$BATS_TEST_TMPDIR/bucket"
	mkdir -p "$CODEATLAS_ROOT/releases" "$CODEATLAS_DATA/state" "$CODEATLAS_LOG_DIR" "$STUB_VALUES" \
		"$STUB_BUCKET"
	: >"$STUB_LOG"

	# Without root, the key file cannot be given to UID 1000; give it to the current user instead.
	if [ "$(id -u)" = 0 ]; then
		KEY_OWNER=1000
	else
		KEY_OWNER="$(id -u)"
		export CODEATLAS_KEY_OWNER="$KEY_OWNER"
	fi

	write_environment pilot
	install_stubs
}

# Writes the environment file that cloud-init writes on a real host.
write_environment() {
	cat >"$CODEATLAS_ROOT/environment" <<EOF
ENVIRONMENT=$1
AWS_REGION=us-east-1
REGISTRY=123456789012.dkr.ecr.us-east-1.amazonaws.com
BUCKET=codeatlas-123456789012-us-east-1
CODEATLAS_HOSTNAME=$1.example.dev
PARAMETERS_PATH=${2:-/codeatlas/$1}
EOF
}

install_stubs() {
	local bin="$BATS_TEST_TMPDIR/bin"
	mkdir -p "$bin"

	cat >"$bin/aws" <<'EOF'
#!/usr/bin/env bash
echo "aws $*" >>"$STUB_LOG"
fails() { case " $STUB_FAIL " in *" $1 "*) return 0 ;; esac; return 1; }
case "$1 $2" in
"ssm get-parameters-by-path")
	fails ssm && { echo "stub: ssm failed" >&2; exit 254; }
	case " $* " in
	*" --query "*) jq -r '[.Parameters[].Name] | join("\t")' "$STUB_SSM_RESPONSE" ;;
	*) cat "$STUB_SSM_RESPONSE" ;;
	esac
	;;
"ssm put-parameter")
	name="" type="" value=""
	while [ $# -gt 0 ]; do
		case "$1" in
		--name) name="${2##*/}"; shift ;;
		--type) type="$2"; shift ;;
		--value) value="$2"; shift ;;
		esac
		shift
	done
	case "$value" in
	file://*) cat "${value#file://}" >"$STUB_VALUES/$name" ;;
	*) printf '%s' "$value" >"$STUB_VALUES/$name" ;;
	esac
	echo "$type" >"$STUB_VALUES/$name.type"
	echo '{"Version": 1, "Tier": "Standard"}'
	;;
"ecr get-login-password")
	fails ecr-login && { echo "stub: ecr login failed" >&2; exit 255; }
	echo "stub-registry-password"
	;;
"s3 cp")
	shift 2
	paths=()
	while [ $# -gt 0 ]; do
		case "$1" in
		--region) shift ;;
		--*) ;;
		*) paths+=("$1") ;;
		esac
		shift
	done
	case "${paths[0]}" in
	s3://*)
		fails s3-download && { echo "stub: download failed" >&2; exit 1; }
		object="$STUB_BUCKET/${paths[0]#s3://*/}"
		[ -f "$object" ] || { echo "stub: (404) Not Found" >&2; exit 1; }
		cp "$object" "${paths[1]}"
		;;
	*)
		object="$STUB_BUCKET/${paths[1]#s3://*/}"
		mkdir -p "${object%/*}"
		cp "${paths[0]}" "$object"
		;;
	esac
	;;
"s3api put-object")
	key="" body="" if_none_match=""
	while [ $# -gt 0 ]; do
		case "$1" in
		--key) key="$2"; shift ;;
		--body) body="$2"; shift ;;
		--if-none-match) if_none_match="$2"; shift ;;
		esac
		shift
	done
	fails s3-upload && { echo "stub: upload failed" >&2; exit 254; }
	object="$STUB_BUCKET/$key"
	if [ -e "$object" ] && [ "$if_none_match" = "*" ]; then
		echo "stub: (PreconditionFailed) At least one of the pre-conditions you specified did not hold" >&2
		exit 254
	fi
	mkdir -p "${object%/*}"
	cp "$body" "$object"
	echo '{"ETag": "\"stub\""}'
	;;
"cloudwatch put-metric-data")
	fails metric && { echo "stub: put-metric-data failed" >&2; exit 254; }
	;;
esac
exit 0
EOF

	cat >"$bin/docker" <<'EOF'
#!/usr/bin/env bash
echo "IMAGE_TAG=${IMAGE_TAG:-} docker $*" >>"$STUB_LOG"
fails() { case " $STUB_FAIL " in *" $1 "*) return 0 ;; esac; return 1; }
case " $* " in
*" --password-stdin "*)
	cat >/dev/null
	fails docker-login && { echo "stub: login failed" >&2; exit 1; }
	;;
*" pull "*)
	fails pull && { echo "stub: pull failed" >&2; exit 1; }
	;;
*" run --rm migrate "*)
	fails migrate && { echo "stub: migration failed" >&2; exit 1; }
	;;
esac
exit 0
EOF

	cat >"$bin/codeatlas-compose" <<'EOF'
#!/usr/bin/env bash
echo "codeatlas-compose $*" >>"$STUB_LOG"
case " $STUB_FAIL " in *" up "*) [ "$1" = up ] && exit 1 ;; esac
case "$1" in
exec | run)
	# exec [-T] SERVICE COMMAND... and run [--rm] [-T] [-v VOLUME] SERVICE COMMAND...
	shift
	while [ $# -gt 0 ]; do
		case "$1" in
		-v | --volume | -e | --env | -u | --user | -w | --workdir) shift 2 ;;
		-*) shift ;;
		*) break ;;
		esac
	done
	shift
	if [ $# -gt 0 ] && [ -n "${STUB_CONTAINER_BIN:-}" ] && [ -x "$STUB_CONTAINER_BIN/$1" ]; then
		export PATH="$STUB_CONTAINER_BIN:$PATH"
		exec "$@"
	fi
	;;
esac
exit 0
EOF

	cat >"$bin/curl" <<'EOF'
#!/usr/bin/env bash
echo "curl $*" >>"$STUB_LOG"
case " $STUB_FAIL " in *" curl "*) exit 7 ;; esac
exit 0
EOF

	chmod +x "$bin"/*
	export PATH="$bin:$PATH"
}

# A complete production parameter set. Values hold characters that need quoting for Compose.
GEMINI_VALUE='secret-gemini-$HOME #7f3a'
VOYAGE_VALUE='secret-voyage-'"it's"'-\"q\"-\\'
CLIENT_SECRET_VALUE='secret-client-9b1c'
WEBHOOK_VALUE='secret-webhook-${X:-y}'
TOKEN_KEY_VALUE='secret-token-key-2d4e'
DB_PASSWORD_VALUE='0123456789abcdef0123456789abcdef0123456789abcdef'
PRIVATE_KEY_VALUE=$'-----BEGIN RSA PRIVATE KEY-----\nsecret-private-key-line\n-----END RSA PRIVATE KEY-----\n'

# Writes the response of `aws ssm get-parameters-by-path` for the given NAME=VALUE pairs, under
# the path in $1.
write_parameters() {
	local path="$1"
	shift
	local pair
	for pair in "$@"; do
		jq -n --arg name "$path/${pair%%=*}" --arg value "${pair#*=}" \
			'{Name: $name, Type: "SecureString", Value: $value}'
	done | jq -s '{Parameters: .}' >"$STUB_SSM_RESPONSE"
}

production_parameters() {
	write_parameters "${1:-/codeatlas/pilot}" \
		CODEATLAS_ENV=production \
		CODEATLAS_FAKE_EXTERNALS=0 \
		EMIT_METRICS=1 \
		"TOKEN_ENCRYPTION_KEY=$TOKEN_KEY_VALUE" \
		"POSTGRES_PASSWORD=$DB_PASSWORD_VALUE" \
		GITHUB_APP_ID=123456 \
		GITHUB_APP_SLUG=codeatlas-example \
		GITHUB_APP_CLIENT_ID=Iv1.0123456789abcdef \
		"GITHUB_APP_CLIENT_SECRET=$CLIENT_SECRET_VALUE" \
		"GITHUB_APP_PRIVATE_KEY=$PRIVATE_KEY_VALUE" \
		"GITHUB_WEBHOOK_SECRET=$WEBHOOK_VALUE" \
		"GEMINI_API_KEY=$GEMINI_VALUE" \
		"VOYAGE_API_KEY=$VOYAGE_VALUE"
}

fake_mode_parameters() {
	write_parameters "${1:-/codeatlas/loadtest}" \
		CODEATLAS_ENV=development \
		CODEATLAS_FAKE_EXTERNALS=1 \
		DAILY_QUESTION_LIMIT=100000 \
		PILOT_DAILY_QUESTION_LIMIT=100000 \
		PILOT_DAILY_REVIEW_LIMIT=100000 \
		RATE_LIMIT_PER_MINUTE=0 \
		"TOKEN_ENCRYPTION_KEY=$TOKEN_KEY_VALUE" \
		"POSTGRES_PASSWORD=$DB_PASSWORD_VALUE"
}

# Fails when grep finds a match. A bare `! grep` never fails a test, because errexit ignores
# negated commands.
refute_grep() {
	! grep "$@"
}

# Prints the octal mode and owner UID of a file.
mode_and_owner() {
	stat -c '%a %u' "$1"
}

# Fails when any secret value of the production set appears in the given text.
refute_secrets_in() {
	local value
	for value in "$GEMINI_VALUE" "$VOYAGE_VALUE" "$CLIENT_SECRET_VALUE" "$WEBHOOK_VALUE" \
		"$TOKEN_KEY_VALUE" "$DB_PASSWORD_VALUE" secret-private-key-line; do
		if [[ "$1" == *"$value"* ]]; then
			echo "a secret value appears in the output" >&2
			return 1
		fi
	done
}
