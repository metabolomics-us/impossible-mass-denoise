# Deploying the denoise service on whiteale

The service listens on `127.0.0.1:8300` and is published as `https://denoise.metabolomics.us`
through nginx. Three pieces live outside this repository and need the operator: the nginx vhost,
the DNS record with its certificate, and the unit and config on whiteale. There are no secrets and
no environment variables: the YAML config is the only configuration.

## 1. nginx vhost

This block belongs in the `nginx` role of `metabolomics-us/fiehnlab-slurm-cluster`. That role owns
whiteale's `conf.d` with purge enabled, so a hand-placed file will be deleted on the next run.

```nginx
server {
    listen 443 ssl;
    server_name denoise.metabolomics.us;

    # ssl_certificate / ssl_certificate_key: as issued for denoise.metabolomics.us (section 2)

    client_max_body_size 1m;

    location / {
        proxy_pass http://127.0.0.1:8300;
        proxy_read_timeout 30s;
        proxy_set_header Host $host;
        # Overwrite, never append. The service trusts the FIRST X-Forwarded-For entry
        # (trusted_proxy: true) as the rate-limit key; $proxy_add_x_forwarded_for would keep
        # a client-supplied value in front and let any client choose its own key.
        proxy_set_header X-Forwarded-For $remote_addr;
    }
}
```

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
    -r /opt/denoise-service/app/requirements-service.txt
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
with a `possible` flag and a `summary`.

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
- The lookup cache is cleared on a timer by the service itself (`cache_clear_seconds`, default
  3600). No cron job is needed.
- Access-log lines go to the `denoise_service.access` logger, and uvicorn's default logging does
  not print INFO from it. Request counts therefore come from nginx's access log. The service
  never logs spectrum contents.
