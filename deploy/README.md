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

## Why a VM and not Render/Vercel

Worth stating, because it's the obvious question: this backend **self-hosts
its WebRTC media path** (pipecat `SmallWebRTCTransport` / aiortc), so it
needs inbound **UDP** — for the audio stream itself, and for the TURN relay
on 3478/5349/49160-49200. Render and Vercel are HTTP/TCP only; coturn
cannot run there at all, and Vercel's serverless functions can't hold a
multi-minute call open. A project that offloads voice to a managed provider
(Vapi, Retell, Daily, LiveKit Cloud) *could* use that stack — this one
deliberately doesn't.

The frontend alone could happily live on Vercel later. It's kept on the VM
here so frontend and API share one origin: relative API paths work
unchanged, CORS is a non-issue, and the Task 2.5 refresh cookie needs no
`SameSite=none` handling.

## Step 1 — Get a VM

**Currently targeting Google Cloud** (free trial credit). For a full
click-by-click walkthrough including the console links, see
[`SERVER_SETUP_GUIDE.md`](./SERVER_SETUP_GUIDE.md).

Settings that matter:
- **Image:** Ubuntu 22.04 LTS
- **Machine type:** `e2-medium` — 2 vCPU, **4 GB RAM**. 2 GB is too tight
  once the Smart Turn ONNX model and a process-per-call (Task 2.4) are
  both resident. Go higher if you plan to run local Whisper/Piper (Task 2.2).
- **Region:** `asia-south1` (Mumbai). Latency is a feature here — a US
  region adds ~250ms to every exchange in a voice app.
- **Tick "Allow HTTP traffic" and "Allow HTTPS traffic"** at creation
- **Promote the external IP to static** afterwards. GCP's default IP
  changes on restart, which silently breaks both DNS and the TLS
  certificate at some random later point.

> Oracle Cloud was the original target (free forever, ARM). Their signup
> rejected the account despite the card verifying — a common and opaque
> failure. Kept as a fallback if their support ticket resolves; nothing
> below is Oracle- or GCP-specific.

## Step 2 — Open the firewall

On GCP this is **one layer** — its Ubuntu images don't ship restrictive
local iptables rules, unlike Oracle's.

The HTTP/HTTPS checkboxes at VM creation cover 80/443. Add one more rule
for the voice relay: **VPC network → Firewall → Create firewall rule**

| Field | Value |
|---|---|
| Name | `allow-turn` |
| Targets | All instances in the network |
| Source IPv4 ranges | `0.0.0.0/0` |
| TCP ports | `3478,5349` |
| UDP ports | `3478,5349,49160-49200` |

The UDP range must match `min-port`/`max-port` in `turnserver.conf`.

> On a host that *does* have a local firewall (Oracle, or any VM where you
> enabled ufw/iptables yourself), open the same ports there too — forgetting
> that second layer is the classic time-waster, since everything above
> appears configured while traffic is silently dropped.

## Step 3 — Point a domain at it

Caddy needs a real domain to get a certificate. Free options: DuckDNS, or
any domain you own. Create an **A record** → the VM's public IP, and wait
for it to resolve (`nslookup yourdomain`) before step 5, or Caddy's
certificate request will fail.

## Step 4 — Install Docker on the VM

Connect first. On GCP the simplest route is the **SSH button** on the VM
row in the console — it opens a browser terminal already logged in, no key
setup at all.

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
