# Denoise service automatic deployment — design

Date: 2026-10-30. Repos: `metabolomics-us/impossible-mass-denoise` (CI step, deploy scripts, node
unit) and `metabolomics-us/fiehnlab-slurm-cluster` (Ansible roles and nginx vhost).
Builds on the merged service (PR #36) and follows the existing FlashEIC deployment on whiteale.

## Intent

Every push to `main` of `impossible-mass-denoise` that passes its gates deploys the denoise service
to `https://denoise.metabolomics.us` with no manual step, so the public service and BinView always
run the code that CI tested. CI must never hold root or a production secret.

## Shape (copied from FlashEIC, then adapted)

- **Runs on `kvm-node-7` and `kvm-node-8`**, the same drained nodes FlashEIC uses (24 cores, 62 GB,
  Python 3.9.25 on both, port 8300 free). nginx on the head `kvm-node-12` terminates TLS and proxies
  to both over the private fabric (`192.168.5.26` and `192.168.5.27`, port 8300).
- **Trigger:** a `deploy` step in the repo's `.woodpecker.yaml`, push to `main` only, after `gate` and
  `service-gate`. It runs `ops/ci/woodpecker-deploy.sh`, which uses a Woodpecker secret SSH key to
  reach `denoise-deploy@kvm-node-12`. That key is forced onto one command, `deploy <sha>`, and is
  accepted only from the fabric `192.168.5.0/24`.
- **On the head,** a root-owned wrapper re-derives the commit from GitHub itself with its own
  read-only deploy key and refuses any SHA that is not the tip of `origin/main` (reports
  "superseded" instead). It then runs a script that is **owned by the Ansible role, not by the
  repo** (a deliberate departure from FlashEIC, see "Calls made after the builder check").
- **The role-owned deploy script** checks the commit out into a new release directory and builds
  the venv from the lock file as the unprivileged `denoise-deploy` user, then rolls node by node
  with a readiness gate between nodes, and rolls back automatically if the roll fails. Only the
  restart and probe of the nodes runs as root (`sudo ssh <node> systemctl ...`, root's key already
  reaches the compute nodes, as for FlashEIC). Nothing from the repo executes as root.
- **Release layout on shared BeeGFS** (my call): `/mnt/beegfs/opt/denoise/releases/<sha>/` holds a
  checkout and a venv built from the lock file; `current` is a symlink to the live release; the
  per-node config is `/mnt/beegfs/opt/denoise/etc/config.yaml` (YAML, no secrets, no environment
  variables). Rollback repoints `current` to the previous release and restarts.
- **Node unit** `denoise-service.service` runs `current/venv/bin/python -B -m denoise_service
  --config .../config.yaml`. Nodes boot stateless and have no local account database, so the unit
  is delivered by a Warewulf overlay (as FlashEIC does) and uses `DynamicUser=yes` rather than root
  (my call), plus the hardening already in `ops/deploy/denoise-service.service`.
- **Readiness gate** for a node: `/healthz` returns 200 with fingerprint `5aae4ec26694da4d` and both
  tables loaded, and a known spectrum POSTed to `/v1/classify` returns the expected verdicts.
- **nginx** (in the `nginx` role of `fiehnlab-slurm-cluster`, which owns whiteale's `conf.d` with
  purge on): server `denoise.metabolomics.us`, upstream `denoise_backend` with
  `hash $remote_addr consistent` over both nodes and passive ejection (`max_fails=2
  fail_timeout=30s`), `proxy_set_header X-Forwarded-For $remote_addr;` (overwrite),
  `client_max_body_size 2m`, `proxy_read_timeout 60s`, TLS from `tls-common.inc`, its own
  certificate. Names are prefixed `denoise_` to avoid colliding in nginx's global namespace.
- **Why hash on the client address, not round-robin:** the service's per-client rate limit and
  in-flight cap live in each process. Round-robin across two nodes would double a client's
  allowance. Hashing keeps each client on one node so the published limits hold.
- **Lock file** (my call): the repo gains `requirements-service.lock` (pinned versions from the
  tested environment). `service-gate` installs from it, and `deploy.sh` builds the release venv from
  the same file, so the deployed environment is the tested one. `requirements-service.txt` keeps its
  lower bounds as the human-edited source.
- **Credentials:** one new Woodpecker secret `denoise_deploy_ssh_key` (event: push only; its public
  half sits in the Ansible role defaults), and one read-only GitHub deploy key generated on the head.
  No other secrets, no passwords, no environment variables.

## Calls made after the builder check (mine, for review)

- **nginx host:** the TLS front is the head `kvm-node-12` (whiteale), which has the fabric address
  `192.168.5.31`; the wrapper, deploy key and transient units live on that same host.
- **Which CI agent deploys:** the same as FlashEIC. The repo's workflow already carries the label
  `cluster: hive`, and FlashEIC's `deploy` step reaches the head from hive with the same forced-command
  key restriction (`from="192.168.5.0/24"`). The first run proves it; if refused, pin the step to a
  whiteale agent label.
- **Root and the repo:** FlashEIC lets root on the head run the repo's own `deploy.sh`, so anyone who
  can merge to `main` runs code as root there. Here merges to `main` can also come from autonomous
  agents, so the privileged script lives in the Ansible role and reads only data from the repo
  (a lock file and a smoke-spectrum JSON). Package installation runs as `denoise-deploy`, and the lock
  file is hash-pinned (`pip install --require-hashes`) so a changed dependency cannot slip in.
- **Expected fingerprint and smoke spectrum** come from `ops/deploy/smoke.json` in the deployed
  commit (parsed as data), so a legitimate chemistry change bumps that file in its own commit instead
  of breaking every later deploy.
- **Bind address and trust:** the shared config binds `0.0.0.0` on the nodes (one file for both); the
  nodes are on the private fabric as FlashEIC's are. As with FlashEIC, any host on the fabric that can
  reach port 8300 could send its own `X-Forwarded-For`. No extra firewall rule is added (operator may
  add one to the overlay: allow 8300 only from `192.168.5.31`).
- **Venv portability:** the head (Rocky 9.8, Python 3.9.25) and both nodes (Python 3.9.25) match, so
  a venv built on the head runs on the nodes; the lock must resolve to manylinux wheels on 3.9.
- **DynamicUser on BeeGFS:** release files are created world-readable (`0755` directories, `0644`
  files); the dynamic user only reads them. The first roll verifies it on a node and fails loudly if not.
- **Pipeline status:** "superseded" (a newer commit exists) exits 0 and says so; a failed readiness gate
  or a rollback exits non-zero so the pipeline and the GitHub status go red. No extra notifier.

## Goals

- Merge to `main`, green gates, service updated on both nodes within minutes, with no operator action.
- A bad release never takes both nodes down: the second node is only touched after the first passes
  its readiness gate; a failed roll rolls back.
- Zero change to Ziyue's modules; nothing in CI gains root.

## Non-goals and anti-goals

- No deploys of anything but the tip of `main`. No deploy from a pull request or a fork.
- No change to FlashEIC, its nodes' configuration, or its nginx vhost.
- No active health checking in nginx (open-source 1.20 cannot); the deploy script is the gate.
- Anti-goal: a pipeline that reports success while a node still runs the old code.

## Operator-owned steps (not automated here)

DNS record and TLS certificate for `denoise.metabolomics.us`; applying the new Ansible roles and
the nginx role on the head; building the Warewulf overlay and assigning it to the two nodes;
registering the public half of the deploy key and the Woodpecker secret; adding the GitHub deploy
key to the repo (needs repo admin). The deliverable here is reviewed PRs and a README that lists
each step in order; nothing in production is changed by the agent.

## Constraints

- Whiteale nginx vhosts live only in the `nginx` role (purge is on); a hand-placed file is deleted.
- Compute nodes are stateless Warewulf nodes: nothing persists outside BeeGFS and the overlay.
- BeeGFS here has node-local `flock`; the release process must not depend on cross-node file locks.
- The service keeps exactly one worker process per node (its cache, limiter and queue are
  per-process by design).

## Left to the builder

Script structure, the transient-unit and log layout on the head (follow `flasheic_deploy`), whether
to generalise the `flasheic_deploy` role or add a parallel `denoise_deploy` role (a parallel role is
the safe default), exact smoke-spectrum values, release retention (keep the last N), and tests.
