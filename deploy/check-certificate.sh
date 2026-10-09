#!/usr/bin/env bash
# Publishes the days left on the environment's TLS certificate (specs/004-pilot-deployment research
# R10; contracts/operations.md, "Host scripts"). codeatlas-cert-check.timer starts it daily at
# 06:00 UTC, in the pilot only.
#
# Usage: check-certificate.sh
#
# Reads the certificate that Caddy serves for CODEATLAS_HOSTNAME on this host, through
# `openssl s_client` to 127.0.0.1:443, and publishes the whole days until it expires as
# CertificateDaysLeft (negative once expired). The certificate-expiring alarm fires on a value
# below 14, and on a missing day.
#
# Output goes to standard output and to /var/log/codeatlas/check-certificate.log. Exit status: 0
# published; 1 the certificate could not be read, or the metric not published; 2 bad arguments or
# host configuration.
#
# CODEATLAS_ROOT (default /opt/codeatlas) and CODEATLAS_LOG_DIR (default /var/log/codeatlas) exist
# for tests.
set -euo pipefail

ROOT="${CODEATLAS_ROOT:-/opt/codeatlas}"
LOG_DIR="${CODEATLAS_LOG_DIR:-/var/log/codeatlas}"

stage="starting"

say() {
	printf '%s check-certificate: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

finish() {
	if [[ "$1" -ne 0 ]]; then
		say "failed while $stage; nothing was published"
	fi
}

# Sources the host's environment file and checks the names this script needs.
load_environment() {
	[[ -r "$ROOT/environment" ]] || {
		say "$ROOT/environment is missing"
		return 2
	}
	# shellcheck source=/dev/null
	. "$ROOT/environment"
	local name
	for name in ENVIRONMENT AWS_REGION CODEATLAS_HOSTNAME; do
		[[ -n "${!name:-}" ]] || {
			say "$name is not set in $ROOT/environment"
			return 2
		}
	done
}

main() {
	if [[ $# -ne 0 ]]; then
		echo "usage: check-certificate.sh" >&2
		return 2
	fi
	trap 'finish $?' EXIT
	load_environment

	local served end days
	stage="reading the certificate of $CODEATLAS_HOSTNAME"
	# s_client's own status says little (it can fail after the handshake); whether a certificate
	# came back decides.
	served="$(openssl s_client -servername "$CODEATLAS_HOSTNAME" -connect 127.0.0.1:443 \
		</dev/null 2>/dev/null || true)"
	# For example notAfter=2027-01-06 12:00:00Z.
	end="$(printf '%s\n' "$served" | openssl x509 -noout -enddate -dateopt iso_8601)"
	end="${end#notAfter=}"

	stage="computing the days left until $end"
	days="$(jq -nr --arg end "$end" '($end | sub(" "; "T") | fromdateiso8601) - now | . / 86400 | floor')"
	[[ "$days" =~ ^-?[0-9]+$ ]]
	say "$days days left on the certificate of $CODEATLAS_HOSTNAME (it ends $end)"

	stage="publishing CertificateDaysLeft"
	aws cloudwatch put-metric-data --region "$AWS_REGION" --namespace CodeAtlas \
		--metric-name CertificateDaysLeft --value "$days" --unit Count \
		--dimensions "Environment=$ENVIRONMENT"
	stage="done"
}

mkdir -p "$LOG_DIR"
main "$@" 2>&1 | tee -a "$LOG_DIR/check-certificate.log"
