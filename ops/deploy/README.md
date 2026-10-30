# Deploying the denoise service on whiteale

The service listens on `127.0.0.1:8300` and is published as `https://denoise.metabolomics.us`
through nginx. Three pieces live outside this repository and need the operator: the nginx vhost,
the DNS record with its certificate, and the unit and config on whiteale. There are no secrets and
no environment variables: the YAML config is the only configuration.

> **Automatic deployment (current).** Production no longer follows sections 1-6 by hand. Every push
> to `main` that passes `gate` and `service-gate` runs the `deploy` step (`ops/ci/woodpecker-deploy.sh`),
> which asks the head `kvm-node-12` to deploy that commit to `kvm-node-7` and `kvm-node-8`. Everything
> privileged lives in the `denoise_deploy` and `denoise_nodes` roles and the nginx vhost of
> `metabolomics-us/fiehnlab-slurm-cluster`; its `docs/runbooks/denoise-service.md` lists the one-time
> operator steps (certificate, roles, Warewulf overlay, GitHub deploy key, Woodpecker secret
> `denoise_deploy_ssh_key`) and the rollback. This repo supplies data only: `requirements-service.lock`
> (hash-pinned; regenerate with `uv pip compile requirements-service.in --python-version 3.9
> --python-platform x86_64-manylinux_2_28 --generate-hashes --only-binary :all: -o requirements-service.lock`)
> and `ops/deploy/smoke.json` (expected fingerprint and a smoke request; update it in the same commit as
> any chemistry change). Sections 1-6 remain as a single-host fallback.

## 1. nginx vhost

This block belongs in the `nginx` role of `metabolomics-us/fiehnlab-slurm-cluster`. That role owns
whiteale's `conf.d` with purge enabled, so a hand-placed file will be deleted on the next run.

```nginx
server {
    listen 443 ssl;
    server_name denoise.metabolomics.us;

    # TLS lines (ssl_certificate, ssl_certificate_key, protocols) are the operator's to fill from
    # the house tls-common pattern. An :80 server for the ACME challenge plus the https redirect
    # belongs in the same role.

    # Larger than the service's own 1 MiB cap on purpose: the service, not nginx, answers an
    # oversized body with 413, and only the service's reply carries the CORS headers.
    client_max_body_size 2m;

    location / {
        proxy_pass http://127.0.0.1:8300;
        proxy_read_timeout 60s;
        proxy_set_header Host $host;
        # Overwrite, never append. The service trusts the FIRST X-Forwarded-For entry
        # (trusted_proxy: true) as the rate-limit key; $proxy_add_x_forwarded_for would keep
        # a client-supplied value in front and let any client choose its own key.
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

`$remote_addr` must be the real client address. After deploying, make a request from a known
machine and check that nginx's access log shows that machine, not the router. If NAT hides client
addresses, every request shares one rate-limit key and the limiter degenerates into a single
shared bucket for everyone.

`location /` also publishes `/docs` and `/openapi.json`. That is intended: they are the public
API documentation and expose nothing the API does not already offer.

## 2. DNS and TLS

Operator-owned. Create the DNS record for `denoise.metabolomics.us` pointing at whiteale's public
address, and issue a certificate for the name `denoise.metabolomics.us` (the same way the other
metabolomics.us vhosts get theirs). Nothing in this repository holds the certificate.

## 3. Install the unit and config on whiteale

```bash
sudo useradd --system --home-dir /opt/denoise-service --shell /usr/sbin/nologin denoise
sudo mkdir -p /opt/denoise-service /etc/denoise-service
sudo git clone https://github.com/metabolomics-us/impossible-mass-denoise.git /opt/denoise-service/app
sudo python3 -m venv /opt/denoise-service/venv
sudo /opt/denoise-service/venv/bin/pip install -r /opt/denoise-service/app/requirements.txt \
    --require-hashes -r /opt/denoise-service/app/requirements-service.lock
sudo cp /opt/denoise-service/app/denoise_service/config.example.yaml /etc/denoise-service/config.yaml
sudo cp /opt/denoise-service/app/ops/deploy/denoise-service.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now denoise-service
systemctl status denoise-service
curl -s http://127.0.0.1:8300/healthz
```

Keep `host: 127.0.0.1` and `trusted_proxy: true` in the config (nginx is the only client).
The unit runs as the unprivileged `denoise` user with a read-only file system and a 1 GiB memory
cap. To update, `git pull` in `/opt/denoise-service/app` and `sudo systemctl restart denoise-service`.

## 4. Smoke test from outside

```bash
curl -s https://denoise.metabolomics.us/healthz
curl -s -X POST https://denoise.metabolomics.us/v1/classify \
     -H 'Content-Type: application/json' \
     -d '{"mode":"neg","peaks":[[104.11,100],[86.06,45],[96.9691,10]]}'
```

The first returns the status and chemistry fingerprint; the second returns one entry per peak
with a `possible` flag and a `summary`. The conversion endpoint is checked the same way:

```bash
curl -s -X POST https://denoise.metabolomics.us/v1/convert \
     -H 'Content-Type: application/json' \
     -d '{"mode":"pos","text":"181.0707:100 100.5:20 85.0284:40"}'
```

It returns the input text in its own format with the impossible ions removed
(`181.0707:100 85.0284:40`) plus one `records` entry per spectrum.

## 5. Rollback

```bash
sudo systemctl stop denoise-service
```

Nothing else depends on the service. BinView degrades with a visible note while it is stopped.
Use `sudo systemctl disable denoise-service` as well to keep it down across reboots.

## 6. Operations notes

- The service keeps one worker on purpose. One process owns the formula cache, the rate limiter
  and the work queue; more workers would each hold their own copy and the limits would no longer
  mean what the config says. Do not add `--workers` or run a second instance behind nginx.
- The lookup cache is cleared by the service itself: on a timer (`cache_clear_seconds`, default
  3600) and after any job that leaves more than `max_cache_entries` (default 200000) entries.
  No cron job is needed. Work per request is bounded by `max_request_lookups` (default 200000)
  and each client may have `max_inflight_per_client` (default 4) jobs in flight.
- Access-log lines now go to stderr, so `journalctl -u denoise-service` shows one line per
  request with method, path, status, ion count and milliseconds. The service never logs spectrum
  contents. Client addresses are not in that line; nginx's access log is still where they live.
- `ExecStart` uses `python -B`: with `ProtectSystem=strict` the service cannot write
  `__pycache__`, and the write would fail silently (a startup cost only). `-B` is optional.
