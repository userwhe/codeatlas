#!/usr/bin/env bash
# Releases one commit on this host, checks it, and rolls it back on failure
# (specs/004-pilot-deployment research R7; contracts/operations.md, "Host layout" and "Host
# scripts").
#
# Usage: release.sh <sha> [--expect-version V] [--refresh-config]
#
# Runs from the commit's bundle, /opt/codeatlas/releases/<sha>/, extracted by the caller from
# s3://<bucket>/releases/<sha>/deploy.tar.gz. It:
#   1. renders the configuration (render-config.sh) and logs in to the registry;
#   2. with this bundle's compose.yml and IMAGE_TAG=<sha> in the environment (which overrides
#      /opt/codeatlas/.env), pulls only the api and web images and runs the migrations;
#   3. writes /opt/codeatlas/.env, records the running commit as the previous one, points
#      /opt/codeatlas/current at this bundle, and starts the new tag, recreating caddy when the
#      Caddyfile changed;
#   4. checks for up to 120 seconds, through Caddy, that /readyz and / return 200 and /version
#      reports the commit (or V, with --expect-version V), then records the running commit;
#   5. when the check fails, or anything fails after the switch, points current and .env back at
#      the release in state/previous, starts it (recreating caddy when the Caddyfiles differ), waits
#      for /readyz, and records it as running again. Nothing is migrated, so this also works when
#      the release added a migration, which the previous image's Alembic would not know. Without a
#      previous release on this host (the first release, or a replaced host without the previous
#      bundle), it stops the new api, worker, and web services instead.
#
# --refresh-config re-renders the configuration of the running commit and recreates api and worker
# (a restart would keep their old environment and key file), then runs the same check. <sha> must
# be the running commit; nothing is pulled or migrated, and .env and current stay as they are.
#
# Exit status: 0 released (or refreshed); 2 any failure before the switch (bad arguments, rendering,
# the registry login, a pull, or a migration), with the previous release still serving and .env,
# current, and the services untouched; 3 the check (or another step after the switch) failed and
# the previous release serves again; 4 the rollback failed, or there was no previous release on
# this host to return to, or the check failed after --refresh-config (which has nothing to roll
# back to). Each outcome is appended to /var/log/codeatlas/releases.log.
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
expected=""
refresh=false
stage="prepare"
# Set before the switch: the release to return to, and the state files as they were.
rollback_to=""
running_before=""
previous_before=""

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

# Records the outcome and exits with its status.
conclude() {
	stage="done"
	say "$2"
	record "$2"
	exit "$1"
}

usage() {
	echo "usage: release.sh <sha> [--expect-version V] [--refresh-config]" >&2
	exit 2
}

# Writes a file through a temporary name, so readers never see it half-written. It checks each
# step itself, because callers in a condition run without errexit.
write_file() {
	local target="$1" content="$2" temporary
	temporary="$(mktemp "$target.XXXXXX")" || return 1
	if ! { printf '%s\n' "$content" >"$temporary" && chmod 0644 "$temporary" &&
		mv -f "$temporary" "$target"; }; then
		rm -f "$temporary"
		return 1
	fi
}

# Writes Compose's interpolation variables with the given tag.
write_env() {
	write_file "$ROOT/.env" "# Written by release.sh: Compose's interpolation variables for codeatlas-compose.
ENVIRONMENT=$ENVIRONMENT
AWS_REGION=$AWS_REGION
REGISTRY=$REGISTRY
CODEATLAS_HOSTNAME=$CODEATLAS_HOSTNAME
IMAGE_TAG=$1"
}

# Points /opt/codeatlas/current at a bundle in one rename.
point_current() {
	ln -sfn "$1" "$ROOT/current.new" && mv -fT "$ROOT/current.new" "$ROOT/current"
}

# Prints the HTTP status of a path through this host's Caddy, or 000 when nothing answers.
status_of() {
	local code
	code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
		--resolve "$CODEATLAS_HOSTNAME:443:127.0.0.1" "https://$CODEATLAS_HOSTNAME$1" 2>/dev/null)" ||
		true
	printf '%s' "${code:-000}"
}

# Prints the commit that /version reports through this host's Caddy, or nothing.
reported_version() {
	curl -fsS --max-time 5 --resolve "$CODEATLAS_HOSTNAME:443:127.0.0.1" \
		"https://$CODEATLAS_HOSTNAME/version" 2>/dev/null | jq -r '.commit // empty' 2>/dev/null || true
}

# Succeeds once /readyz and / return 200 and /version reports $1, trying until the deadline.
passes_check() {
	local want="$1" deadline=$((SECONDS + READY_TIMEOUT)) ready web version observed
	say "checking /readyz, /, and /version (expecting $want) for up to $READY_TIMEOUT seconds"
	while :; do
		ready="$(status_of /readyz)"
		web="$(status_of /)"
		version="$(reported_version)"
		observed="/readyz $ready, / $web, /version ${version:-unavailable} (expected $want)"
		if [[ "$ready" == 200 && "$web" == 200 && "$version" == "$want" ]]; then
			return 0
		fi
		if ((SECONDS >= deadline)); then
			say "the check failed: $observed"
			return 1
		fi
		sleep "$READY_INTERVAL"
	done
}

# Succeeds once /readyz returns 200, trying until the deadline.
becomes_ready() {
	local deadline=$((SECONDS + READY_TIMEOUT))
	say "waiting for /readyz for up to $READY_TIMEOUT seconds"
	until [[ "$(status_of /readyz)" == 200 ]]; do
		if ((SECONDS >= deadline)); then
			say "not ready after $READY_TIMEOUT seconds"
			return 1
		fi
		sleep "$READY_INTERVAL"
	done
}

# Points current and .env at the given release, records it as running, and starts it. Each step is
# checked here, because this runs in a condition, without errexit.
start_previous() {
	local target="$1" target_bundle="$ROOT/releases/$1" previous_after="$previous_before"
	write_env "$target" || return 1
	point_current "$target_bundle" || return 1
	write_file "$DATA/state/running" "$target" || return 1
	# The previous release is the one that ran before the target: as before this release, unless
	# this was a release of the running commit again, which the target now replaces.
	if [[ -n "$running_before" && "$running_before" != "$target" ]]; then
		previous_after="$running_before"
	fi
	if [[ -n "$previous_after" ]]; then
		write_file "$DATA/state/previous" "$previous_after" || return 1
	else
		rm -f "$DATA/state/previous" || return 1
	fi
	codeatlas-compose up -d --remove-orphans || return 1
	if ! cmp -s "$bundle/Caddyfile" "$target_bundle/Caddyfile"; then
		say "the Caddyfiles differ; recreating caddy"
		codeatlas-compose up -d --force-recreate caddy || return 1
	fi
}

# Returns to the release in $rollback_to after a failure past the switch, and exits: 3 when it is
# ready again, 4 otherwise or when this host has no previous release.
roll_back() {
	local reason="$1"
	stage="rollback"
	if [[ -z "$rollback_to" || ! -f "$ROOT/releases/$rollback_to/compose.yml" ]]; then
		say "$reason, and this host has no previous release to return to; stopping the new services"
		codeatlas-compose stop api worker web || say "could not stop the new services"
		conclude 4 "$reason, no previous release, stopped the new services"
	fi
	say "$reason; rolling back to $rollback_to"
	if start_previous "$rollback_to" && becomes_ready; then
		conclude 3 "$reason, rolled back to $rollback_to"
	fi
	conclude 4 "$reason, rollback to $rollback_to failed"
}

finish() {
	local status="$1"
	set +e
	case "$stage" in
	done) ;;
	prepare)
		if [[ "$refresh" == true ]]; then
			conclude 2 "failed before recreating api and worker"
		fi
		conclude 2 "failed before the switch"
		;;
	refresh)
		conclude 4 "configuration refresh failed (exit status $status)"
		;;
	rollback)
		conclude 4 "rollback to $rollback_to failed (exit status $status)"
		;;
	*)
		roll_back "$stage failed (exit status $status)"
		;;
	esac
}
trap 'finish $?' EXIT

commit=""
while [[ $# -gt 0 ]]; do
	case "$1" in
	--expect-version)
		[[ $# -ge 2 && -n "$2" ]] || usage
		expected="$2"
		shift 2
		;;
	--refresh-config)
		refresh=true
		shift
		;;
	-*) usage ;;
	*)
		[[ -z "$commit" ]] || usage
		commit="$1"
		shift
		;;
	esac
done
[[ -n "$commit" ]] || usage
if [[ ! "$commit" =~ ^[0-9a-f]{40}$ ]]; then
	say "expected a full 40-character commit SHA"
	exit 2
fi
sha="$commit"
expected="${expected:-$sha}"

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
command -v jq >/dev/null || {
	say "jq is required"
	exit 2
}

if [[ "$refresh" == true ]]; then
	running="$(cat "$DATA/state/running" 2>/dev/null || true)"
	if [[ "$running" != "$sha" || "$(cd "$ROOT/current" 2>/dev/null && pwd -P)" != "$here" ]]; then
		say "--refresh-config works on the running release (${running:-none}), not $sha"
		exit 2
	fi
	say "refreshing the configuration of $sha on $ENVIRONMENT"
	"$bundle/render-config.sh" "$ENVIRONMENT"
	stage="refresh"
	codeatlas-compose up -d --force-recreate api worker
	if passes_check "$expected"; then
		conclude 0 "configuration refreshed"
	fi
	conclude 4 "configuration refreshed, check failed"
fi

say "releasing $sha to $ENVIRONMENT"
"$bundle/render-config.sh" "$ENVIRONMENT"

say "logging in to $REGISTRY"
aws ecr get-login-password --region "$AWS_REGION" |
	docker login --username AWS --password-stdin "$REGISTRY" >/dev/null

# The new bundle's Compose file, with the new tag; nothing else is pulled, so third-party images
# such as db and caddy are not refreshed and restarted by a release. IMAGE_TAG is set for these
# two commands only: in the environment it would override .env for codeatlas-compose, and so for a
# rollback.
compose_new=(docker compose --project-directory "$ROOT" -f "$bundle/compose.yml")
say "pulling the api and web images"
IMAGE_TAG="$sha" "${compose_new[@]}" pull --quiet api web
say "running the migrations"
IMAGE_TAG="$sha" "${compose_new[@]}" run --rm migrate

# The release to return to is the running one; releasing the running commit again (for example
# after changing a setting) keeps the previous one.
mkdir -p "$DATA/state"
running_before="$(cat "$DATA/state/running" 2>/dev/null || true)"
previous_before="$(cat "$DATA/state/previous" 2>/dev/null || true)"
if [[ -n "$running_before" && "$running_before" != "$sha" ]]; then
	rollback_to="$running_before"
else
	rollback_to="$previous_before"
fi
caddy_changed=false
if [[ -e "$ROOT/current/Caddyfile" ]] && ! cmp -s "$ROOT/current/Caddyfile" "$bundle/Caddyfile"; then
	caddy_changed=true
fi

# From here on, any failure rolls back.
stage="switch"
write_env "$sha"
if [[ -n "$rollback_to" ]]; then
	write_file "$DATA/state/previous" "$rollback_to"
fi
point_current "$bundle"

stage="start"
say "starting $sha"
codeatlas-compose up -d --remove-orphans
if [[ "$caddy_changed" == true ]]; then
	say "the Caddyfile changed; recreating caddy"
	codeatlas-compose up -d --force-recreate caddy
fi

stage="check"
if ! passes_check "$expected"; then
	roll_back "check failed"
fi

write_file "$DATA/state/running" "$sha"
stage="done"
say "released $sha"
record released
