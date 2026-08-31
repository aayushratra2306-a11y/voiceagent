# Deployment (Task 2.3)

Backend + TURN relay + HTTPS, on one VM. This is what makes the bot
reachable by someone on a network you don't control — the actual point of
task 2.3.

## Why each piece exists

| Piece | Why it's needed |
|---|---|
| **Caddy** | Automatic HTTPS. Browsers refuse microphone access on non-HTTPS origins, so without a real certificate a phone literally cannot start a call. Also serves the frontend and proxies the API from **one origin**, so relative URLs, CORS, and the refresh cookie all keep working exactly as they do in dev. |
| **coturn** | The TURN relay. STUN (already configured) is enough on normal home/office networks. TURN is what makes calls work from corporate firewalls, symmetric NAT, and some mobile carriers — where the two sides can never reach each other directly. |
| **backend** | The FastAPI app. One uvicorn worker on purpose — Task 2.4 already gives each *call* its own process, and the in-memory call registry assumes a single API process. |

## Step 1 — Get a VM

Oracle Cloud **Always Free** tier is the target: an Ampere A1 (ARM) shape
with up to 4 cores / 24 GB RAM, free indefinitely.

**Expect friction:** Oracle's free ARM capacity is frequently exhausted in
popular regions ("Out of host capacity"). If you hit that repeatedly:
retry over a few days, pick a different region at signup (it can't be
changed later), or fall back to a cheap paid VM elsewhere — this setup
runs anywhere with Docker. A 2 GB RAM box is enough with cloud providers;
give it 4 GB+ if you plan to run local Whisper/Piper (Task 2.2).

Settings that matter:
- **Image:** Ubuntu 22.04 or 24.04
- **Shape:** VM.Standard.A1.Flex (ARM), 2+ OCPU, 8+ GB RAM
- **Add your SSH public key** during creation — you can't easily add it later
- **Note the public IP**

## Step 2 — Open the firewall (both layers)

Oracle has *two* firewalls and forgetting the second is the classic
time-waster: the cloud **Security List** AND the VM's own **iptables**.

In the Oracle console (VCN → Security Lists → default → Add Ingress Rules),
source `0.0.0.0/0`:

| Port(s) | Protocol | For |
|---|---|---|
| 80, 443 | TCP | HTTP/HTTPS (Caddy, Let's Encrypt) |
| 3478 | TCP + UDP | TURN |
| 5349 | TCP + UDP | TURN over TLS |
| 49160–49200 | UDP | TURN relay range (matches `turnserver.conf`) |

Then on the VM itself — Oracle's Ubuntu images ship with restrictive
iptables rules that will silently drop everything above otherwise:

```bash
sudo iptables -I INPUT -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT -p tcp --dport 443 -j ACCEPT
sudo iptables -I INPUT -p tcp --dport 3478 -j ACCEPT
sudo iptables -I INPUT -p udp --dport 3478 -j ACCEPT
sudo iptables -I INPUT -p tcp --dport 5349 -j ACCEPT
sudo iptables -I INPUT -p udp --dport 5349 -j ACCEPT
sudo iptables -I INPUT -p udp --dport 49160:49200 -j ACCEPT
sudo netfilter-persistent save
```

## Step 3 — Point a domain at it

Caddy needs a real domain to get a certificate. Free options: DuckDNS, or
any domain you own. Create an **A record** → the VM's public IP, and wait
for it to resolve (`nslookup yourdomain`) before step 5, or Caddy's
certificate request will fail.

## Step 4 — Install Docker on the VM

```bash
sudo apt update && sudo apt install -y docker.io docker-compose-v2 git
sudo usermod -aG docker $USER
newgrp docker
```

## Step 5 — Deploy

```bash
git clone git@github.com:aayushratra2306-a11y/voiceagent.git
cd voiceagent

cp deploy/.env.example deploy/.env
nano deploy/.env          # fill in everything — see comments in the file

# Build the frontend (Caddy serves the static output)
sudo apt install -y nodejs npm
cd frontend && npm install && npm run build && cd ..

docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml logs -f
```

## Step 6 — Allow the server's IP in MongoDB Atlas

Atlas blocks by IP. Add the VM's public IP under **Network Access**, or the
backend will start and then fail every database call.

## Step 7 — Verify (this is the actual deliverable)

1. `https://yourdomain/health` returns `{"status":"ok"}`
2. Open the app in a browser, log in, hold a normal conversation
3. **From a phone on mobile data — not your wifi** — do the same. This is
   the test that matters: a genuinely different network is where the TURN
   relay either works or doesn't, and your own wifi will never tell you.

To confirm TURN is actually being used rather than silently skipped:
```bash
docker compose -f deploy/docker-compose.yml logs coturn | grep -i "allocat"
```
Relay allocations appear there when a client actually needs the relay.

## Note on the CORS setting

`backend/main.py` still allows `localhost:5173`/`localhost:8080` origins.
That's harmless here (frontend and API share one origin behind Caddy, so
no CORS check is involved) but worth tightening to the real domain before
this is used by anyone but you.
