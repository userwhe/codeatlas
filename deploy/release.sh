#!/usr/bin/env bash
# Releases one commit on this host (specs/004-pilot-deployment research R7, steps 1 to 4;
# contracts/operations.md, "Host layout" and "Host scripts").
#
# Usage: release.sh <sha>
#
# Runs from the commit's bundle, /opt/codeatlas/releases/<sha>/, extracted by the caller from
# s3://<bucket>/releases/<sha>/deploy.tar.gz. It:
#   1. renders the configuration (render-config.sh) and logs in to the registry;
#   2. with this bundle's compose.yml and IMAGE_TAG=<sha> in the environment (which overrides
#      /opt/codeatlas/.env), pulls only the api and web images and runs the migrations;
#   3. writes /opt/codeatlas/.env, records the running commit as the previous one, points
#      /opt/codeatlas/current at this bundle, and starts the new tag, recreating caddy when the
#      Caddyfile changed;
#   4. waits up to 120 seconds for /readyz through Caddy, then records the running commit.
#
# Exit status: 0 released; 2 any failure before the switch (steps 1 and 2, or bad arguments), with
# the previous release still serving and .env, current, and the services untouched; 1 started but
# not ready (roll back by hand by releasing the previous commit from its bundle).
#
# CODEATLAS_ROOT (default /opt/codeatlas), CODEATLAS_DATA (default /var/lib/codeatlas),
# CODEATLAS_LOG_DIR (default /var/log/codeatlas), CODEATLAS_READY_TIMEOUT (default 120 seconds),
# and CODEATLAS_READY_INTERVAL (default 2 seconds) exist for tests.
set -euo pipefail

ROOT="${CODEATLAS_ROOT:-/opt/codeatlas}"
DATA="${CODEATLAS_DATA:-/var/lib/codeatlas}"
LOG_DIR="${CODEATLAS_LOG_DIR:-/var/log/codeatlas}"
READY_TIMEOUT="${CODEATLAS_READY_TIMEOUT:-120}"
READY_INTERVAL="${CODEATLAS_READY_INTERVAL:-2}"

sha=""
stage="prepare"

say() {
	printf 'release: %s\n' "$*"
}

# Appends one line to releases.log: time, environment, commit, outcome.
record() {
	{
		mkdir -p "$LOG_DIR" &&
			printf '%s %s %s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "${ENVIRONMENT:-unknown}" \
				"${sha:-none}" "$1" >>"$LOG_DIR/releases.log"
	} || say "could not write $LOG_DIR/releases.log"
}

finish() {
	case "$stage" in
	released) ;;
	prepare)
		say "failed before the switch; the previous release is still serving"
		record "failed before the switch"
		exit 2
		;;
	*)
		say "failed after the switch ($stage); release the previous commit by hand to roll back"
		record "failed after the switch ($stage)"
		exit 1
		;;
	esac
}
trap finish EXIT

# Writes a file through a temporary name, so readers never see it half-written.
write_file() {
	local target="$1" content="$2" temporary
	temporary="$(mktemp "$target.XXXXXX")"
	printf '%s\n' "$content" >"$temporary"
	chmod 0644 "$temporary"
	mv -f "$temporary" "$target"
}

if [[ $# -ne 1 ]]; then
	echo "usage: release.sh <sha>" >&2
	exit 2
fi
sha="$1"
if [[ ! "$sha" =~ ^[0-9a-f]{40}$ ]]; then
	sha=""
	say "expected a full 40-character commit SHA"
	exit 2
fi

bundle="$ROOT/releases/$sha"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
if [[ ! -d "$bundle" || "$here" != "$(cd "$bundle" && pwd -P)" ]]; then
	say "run release.sh from its bundle, $bundle/"
	exit 2
fi

[[ -r "$ROOT/environment" ]] || {
	say "$ROOT/environment is missing"
	exit 2
}
set -a
# shellcheck source=/dev/null
. "$ROOT/environment"
set +a
for name in ENVIRONMENT AWS_REGION REGISTRY CODEATLAS_HOSTNAME; do
	[[ -n "${!name:-}" ]] || {
		say "$name is not set in $ROOT/environment"
		exit 2
	}
done

say "releasing $sha to $ENVIRONMENT"
"$bundle/render-config.sh" "$ENVIRONMENT"

say "logging in to $REGISTRY"
aws ecr get-login-password --region "$AWS_REGION" |
	docker login --username AWS --password-stdin "$REGISTRY" >/dev/null

# The new bundle's Compose file, with the new tag; nothing else is pulled, so third-party images
# such as db and caddy are not refreshed and restarted by a release.
export IMAGE_TAG="$sha"
compose_new=(docker compose --project-directory "$ROOT" -f "$bundle/compose.yml")
say "pulling the api and web images"
"${compose_new[@]}" pull --quiet api web
say "running the migrations"
"${compose_new[@]}" run --rm migrate

stage="switch"
caddy_changed=false
if [[ -e "$ROOT/current/Caddyfile" ]] && ! cmp -s "$ROOT/current/Caddyfile" "$bundle/Caddyfile"; then
	caddy_changed=true
fi

write_file "$ROOT/.env" "# Written by release.sh: Compose's interpolation variables for codeatlas-compose.
ENVIRONMENT=$ENVIRONMENT
AWS_REGION=$AWS_REGION
REGISTRY=$REGISTRY
CODEATLAS_HOSTNAME=$CODEATLAS_HOSTNAME
IMAGE_TAG=$sha"

# Releasing the running commit again (for example after changing a setting) keeps the previous one.
mkdir -p "$DATA/state"
if [[ -s "$DATA/state/running" && "$(cat "$DATA/state/running")" != "$sha" ]]; then
	write_file "$DATA/state/previous" "$(cat "$DATA/state/running")"
fi

ln -sfn "$bundle" "$ROOT/current.new"
mv -fT "$ROOT/current.new" "$ROOT/current"

stage="start"
say "starting $sha"
codeatlas-compose up -d --remove-orphans
if [[ "$caddy_changed" == true ]]; then
	say "the Caddyfile changed; recreating caddy"
	codeatlas-compose up -d --force-recreate caddy
fi

stage="readiness check"
say "waiting for https://$CODEATLAS_HOSTNAME/readyz"
deadline=$((SECONDS + READY_TIMEOUT))
until curl -fsS -o /dev/null --max-time 5 --resolve "$CODEATLAS_HOSTNAME:443:127.0.0.1" \
	"https://$CODEATLAS_HOSTNAME/readyz" 2>/dev/null; do
	if ((SECONDS >= deadline)); then
		say "not ready after $READY_TIMEOUT seconds"
		exit 1
	fi
	sleep "$READY_INTERVAL"
done

write_file "$DATA/state/running" "$sha"
stage="released"
record released
say "released $sha"
