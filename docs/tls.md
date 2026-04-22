# TLS Setup for Parallax

## Why TLS matters

Parallax authenticates requests with an HMAC-signed bearer token. Even though the
signature prevents token forgery, the raw token still travels over the wire on every
request. Without TLS, anyone on the network path can capture that token and replay it
until it expires. Wrapping the connection in TLS ensures the token is encrypted in
transit and cannot be intercepted.

---

## Path A — Caddy reverse proxy (own VPS, ports 80/443 open)

Best for: a virtual machine or dedicated server with a public IP address where you
control the firewall and can open ports 80 and 443.

Caddy handles certificate issuance and renewal via Let's Encrypt automatically — no
certbot, no cron jobs.

**Steps**

1. Install Caddy (https://caddyserver.com/docs/install).

2. Copy and edit the template:

   ```bash
   cp deploy/Caddyfile /etc/caddy/Caddyfile
   # Replace {{PARALLAX_HOST}} with your real domain, e.g. parallax.example.com
   nano /etc/caddy/Caddyfile
   ```

3. Start Caddy:

   ```bash
   sudo caddy run --config /etc/caddy/Caddyfile
   ```

   Caddy will obtain a Let's Encrypt certificate on first start. Subsequent restarts
   reuse the cached cert and renew it automatically before expiry.

4. Verify:

   ```bash
   curl -I https://parallax.example.com/healthz
   # Expect HTTP 200 and Cache-Control: no-store
   ```

---

## Path B — Cloudflare Tunnel (no open ports, free tier)

Best for: a home lab behind NAT, a machine without a static IP, or any environment
where you cannot open inbound ports. The tunnel creates an outbound-only encrypted
connection from your server to Cloudflare's edge — no port forwarding required.

**Steps**

1. Install `cloudflared` (https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/installation/).

2. Authenticate with your Cloudflare account:

   ```bash
   cloudflared login
   ```

3. Create the tunnel and note the UUID it prints:

   ```bash
   cloudflared tunnel create parallax
   # Output: Created tunnel parallax with id <TUNNEL-UUID>
   ```

4. Fill the UUID into `deploy/cloudflared.yml` (two places) and copy it into place:

   ```bash
   cp deploy/cloudflared.yml /etc/cloudflared/config.yml
   # Also copy the credentials JSON that cloudflared wrote to ~/.cloudflared/<TUNNEL-UUID>.json
   sudo cp ~/.cloudflared/<TUNNEL-UUID>.json /etc/cloudflared/<TUNNEL-UUID>.json
   ```

5. Point your DNS hostname to the tunnel:

   ```bash
   cloudflared tunnel route dns parallax parallax.example.com
   ```

6. Run the tunnel:

   ```bash
   cloudflared tunnel run parallax
   ```

   To run as a persistent service: `sudo cloudflared service install`.

7. Verify:

   ```bash
   curl -I https://parallax.example.com/healthz
   ```

---

## When to pick which path

| Scenario | Recommended path |
|---|---|
| VPS / cloud VM with a public IP and open ports 80/443 | **Path A — Caddy** |
| Home lab, NAT'd router, no static IP, or no firewall control | **Path B — Cloudflare Tunnel** |
| You already use Cloudflare for DNS and want zero infrastructure overhead | **Path B — Cloudflare Tunnel** |
| You want the simplest possible setup with full control | **Path A — Caddy** |
