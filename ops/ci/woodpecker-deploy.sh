#!/bin/bash
# Ask the whiteale head to deploy this commit to denoise.metabolomics.us.
#
# CI holds no root and no production secret. It holds one SSH key (repo
# secret denoise_deploy_ssh_key, released to push events only) that the head
# accepts only from the cluster fabric and only as the forced command
# `deploy <sha>`. The head re-checks the request against GitHub on its own:
# it deploys only if the SHA is the tip of origin/main, reports "superseded"
# if main has moved on, and refuses anything else. The deploy script is owned
# by the fiehnlab-slurm-cluster role denoise_deploy, not by this repo: it
# reads only data from here (requirements-service.lock, ops/deploy/smoke.json),
# builds the venv as an unprivileged user, rolls kvm-node-7 then kvm-node-8
# behind a readiness probe, and restores the previous release on failure.
# It runs in a systemd unit of its own, so cancelling this pipeline never
# interrupts a roll halfway.
#
# The ssh options matter because a CI step here can see a real home
# directory: -F /dev/null and IdentitiesOnly make this key, and only this
# key, the one offered, so a green run proves this path and no other.
set -euo pipefail

: "${DENOISE_DEPLOY_SSH_KEY:?the deploy key secret was not released to this pipeline}"
: "${CI_COMMIT_SHA:?}"

# CI runs on hive, where neither the name kvm-node-12 nor the 192.168.5.x fabric
# resolves or routes; the head's public address, port 22, is reachable from there.
HEAD_HOST=128.120.143.172
# whiteale's (kvm-node-12) ssh ed25519 host key, read on the host itself 2026-10-30; pinned: a changed key must fail, not be learned.
HEAD_HOSTKEY='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHMt2xwn2FY7cK/wL365s/nKA8X41jyRJHRI3n9nRcwL'

dir=$(mktemp -d)
trap 'rm -rf "$dir"' EXIT
umask 077
printf '%s\n' "$DENOISE_DEPLOY_SSH_KEY" > "$dir/key"
printf '%s %s\n' "$HEAD_HOST" "$HEAD_HOSTKEY" > "$dir/known_hosts"

exec ssh -F /dev/null -i "$dir/key" -o IdentitiesOnly=yes -o BatchMode=yes \
    -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$dir/known_hosts" \
    -o ServerAliveInterval=30 -o ConnectTimeout=20 \
    "denoise-deploy@$HEAD_HOST" deploy "$CI_COMMIT_SHA"
