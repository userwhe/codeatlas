#!/usr/bin/env bash
# Writes an environment's SSM parameters, from the developer's machine (specs/004-pilot-deployment
# research R5; contracts/operations.md, "SSM parameters").
#
# Usage:
#   put-secrets.sh <pilot|loadtest> [--github-app-private-key FILE] [--overwrite NAME]...
#   put-secrets.sh <pilot|loadtest> --set NAME=VALUE
#   put-secrets.sh <pilot|loadtest> --unset NAME
#
# Without --set or --unset, it writes every parameter of the environment that does not exist yet
# under /codeatlas/<environment>/:
#   - the mode settings (pilot: production with metrics; loadtest: development with fake
#     externals, high limits, and no rate limit);
#   - TOKEN_ENCRYPTION_KEY and POSTGRES_PASSWORD, generated;
#   - in the pilot, the GitHub App's settings and the model provider keys, prompted (secrets without
#     echo), and the App's private key from --github-app-private-key.
# Existing parameters are kept unless named with --overwrite, for a rotation; an overwritten
# TOKEN_ENCRYPTION_KEY is generated again. POSTGRES_PASSWORD is never overwritten here: the database
# keeps its password until ALTER ROLE changes it (docs/operations.md, "Replacing the database
# password").
#
# --set NAME=VALUE writes one optional limit or switch, and --unset NAME deletes it, for temporary
# changes; render-config.sh picks them up at the next release.
#
# Secrets are passed to the AWS CLI through temporary files (file://), never on the command line,
# and never printed. The drill writes nothing: it reads the pilot's parameters. Runs on bash 3.2.
set -euo pipefail

OPTIONAL_SETTINGS="DAILY_QUESTION_LIMIT DAILY_REVIEW_LIMIT PILOT_DAILY_QUESTION_LIMIT PILOT_DAILY_REVIEW_LIMIT RATE_LIMIT_PER_MINUTE EMIT_METRICS"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"

usage() {
	cat >&2 <<'EOF'
usage: put-secrets.sh <pilot|loadtest> [--github-app-private-key FILE] [--overwrite NAME]...
       put-secrets.sh <pilot|loadtest> --set NAME=VALUE
       put-secrets.sh <pilot|loadtest> --unset NAME
EOF
	exit 2
}

die() {
	printf 'put-secrets: %s\n' "$*" >&2
	exit 1
}

say() {
	printf 'put-secrets: %s\n' "$*"
}

is_optional_setting() {
	case " $OPTIONAL_SETTINGS " in
	*" $1 "*) return 0 ;;
	esac
	return 1
}

# Each parameter of an environment: NAME TYPE SOURCE [VALUE]. SOURCE is fixed (VALUE), prompt
# (shown while typed), secret (hidden while typed), key (the GitHub App's private key file),
# generate-key (a Fernet key), or generate-password.
specs_for() {
	case "$1" in
	pilot)
		cat <<'EOF'
CODEATLAS_ENV String fixed production
CODEATLAS_FAKE_EXTERNALS String fixed 0
EMIT_METRICS String fixed 1
TOKEN_ENCRYPTION_KEY SecureString generate-key
POSTGRES_PASSWORD SecureString generate-password
GITHUB_APP_ID String prompt
GITHUB_APP_SLUG String prompt
GITHUB_APP_CLIENT_ID String prompt
GITHUB_APP_CLIENT_SECRET SecureString secret
GITHUB_APP_PRIVATE_KEY SecureString key
GITHUB_WEBHOOK_SECRET SecureString secret
GEMINI_API_KEY SecureString secret
VOYAGE_API_KEY SecureString secret
EOF
		;;
	loadtest)
		cat <<'EOF'
CODEATLAS_ENV String fixed development
CODEATLAS_FAKE_EXTERNALS String fixed 1
DAILY_QUESTION_LIMIT String fixed 100000
PILOT_DAILY_QUESTION_LIMIT String fixed 100000
PILOT_DAILY_REVIEW_LIMIT String fixed 100000
RATE_LIMIT_PER_MINUTE String fixed 0
TOKEN_ENCRYPTION_KEY SecureString generate-key
POSTGRES_PASSWORD SecureString generate-password
EOF
		;;
	esac
}

[[ $# -ge 1 ]] || usage
environment="$1"
shift
case "$environment" in
pilot | loadtest) ;;
drill)
	die "the drill reads the pilot's parameters (parameters_path = \"/codeatlas/pilot\" in drill.tfvars); there is nothing to write"
	;;
*) usage ;;
esac
path="/codeatlas/$environment"
specs="$(specs_for "$environment")"

key_file=""
overwrite=" "
set_pair=""
unset_name=""
while [[ $# -gt 0 ]]; do
	case "$1" in
	--github-app-private-key)
		[[ $# -ge 2 ]] || usage
		key_file="$2"
		shift 2
		;;
	--overwrite)
		[[ $# -ge 2 ]] || usage
		overwrite="$overwrite$2 "
		shift 2
		;;
	--set)
		[[ $# -ge 2 && -z "$set_pair" ]] || usage
		set_pair="$2"
		shift 2
		;;
	--unset)
		[[ $# -ge 2 && -z "$unset_name" ]] || usage
		unset_name="$2"
		shift 2
		;;
	*) usage ;;
	esac
done

if [[ -n "$set_pair" || -n "$unset_name" ]]; then
	[[ -z "$key_file" && "$overwrite" == " " ]] || usage
	[[ -z "$set_pair" || -z "$unset_name" ]] || usage
fi

if [[ -n "$set_pair" ]]; then
	[[ "$set_pair" == *=* ]] || usage
	name="${set_pair%%=*}"
	value="${set_pair#*=}"
	is_optional_setting "$name" || die "--set takes one of: $OPTIONAL_SETTINGS"
	[[ "$value" =~ ^[0-9]+$ ]] || die "$name takes a non-negative integer"
	aws ssm put-parameter --region "$REGION" --name "$path/$name" --type String \
		--value "$value" --overwrite >/dev/null
	say "set $path/$name=$value; release the running commit again to apply it"
	exit 0
fi

if [[ -n "$unset_name" ]]; then
	is_optional_setting "$unset_name" || die "--unset takes one of: $OPTIONAL_SETTINGS"
	aws ssm delete-parameter --region "$REGION" --name "$path/$unset_name"
	say "deleted $path/$unset_name; release the running commit again to apply it"
	exit 0
fi

for name in $overwrite; do
	if [[ "$name" == POSTGRES_PASSWORD ]]; then
		die "POSTGRES_PASSWORD changes in the database only through ALTER ROLE; follow \"Replacing the database password\" in docs/operations.md"
	fi
	printf '%s\n' "$specs" | grep -q "^$name " || die "$name is not a parameter of $environment"
done

if [[ -n "$key_file" ]]; then
	[[ -f "$key_file" && -r "$key_file" ]] || die "cannot read $key_file"
	grep -q -- '-----BEGIN .*PRIVATE KEY-----' "$key_file" || die "$key_file is not a PEM private key"
	[[ "$(wc -c <"$key_file" | tr -d ' ')" -le 4096 ]] || die "$key_file is larger than a standard parameter (4 KB)"
	key_file="$(cd "$(dirname "$key_file")" && pwd)/$(basename "$key_file")"
fi

existing=" $(aws ssm get-parameters-by-path --region "$REGION" --path "$path" --recursive \
	--query 'Parameters[].Name' --output text | tr '\t\n' '  ') "

say "writing missing parameters under $path in $REGION"
umask 077
workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT

# Collect every value first, so an interrupted prompt writes nothing.
planned=()
# The list is read on descriptor 3, so the prompts read the terminal.
while read -r name type source fixed_value <&3; do
	[[ -n "$name" ]] || continue
	replace=false
	if [[ "$existing" == *" $path/$name "* ]]; then
		if [[ "$overwrite" != *" $name "* ]]; then
			say "kept $name (exists; --overwrite $name replaces it)"
			continue
		fi
		replace=true
	fi

	value_file="$workdir/$name"
	case "$source" in
	fixed)
		printf '%s' "$fixed_value" >"$value_file"
		;;
	generate-key)
		openssl rand -base64 32 | tr '+/' '-_' | tr -d '\n' >"$value_file"
		;;
	generate-password)
		openssl rand -hex 24 | tr -d '\n' >"$value_file"
		;;
	prompt | secret)
		value=""
		if [[ "$source" == secret ]]; then
			read -r -s -p "$name (hidden): " value || true
			echo >&2
		else
			read -r -p "$name: " value || true
		fi
		[[ -n "$value" ]] || die "$name is empty; nothing was written"
		printf '%s' "$value" >"$value_file"
		value=""
		;;
	key)
		[[ -n "$key_file" ]] || die "$name needs --github-app-private-key FILE; nothing was written"
		value_file="$key_file"
		;;
	esac
	planned+=("$name $type $replace $value_file")
done 3<<EOF
$specs
EOF

if [[ ${#planned[@]} -eq 0 ]]; then
	say "every parameter of $environment exists; nothing to write"
	exit 0
fi

for entry in "${planned[@]}"; do
	read -r name type replace value_file <<EOF
$entry
EOF
	extra=""
	[[ "$replace" == true ]] && extra="--overwrite"
	# shellcheck disable=SC2086 # $extra is empty or one flag
	aws ssm put-parameter --region "$REGION" --name "$path/$name" --type "$type" \
		--value "file://$value_file" $extra >/dev/null
	if [[ "$replace" == true ]]; then
		say "replaced $name ($type)"
	else
		say "wrote $name ($type)"
	fi
done
say "done; release the running commit again to apply changed values"
