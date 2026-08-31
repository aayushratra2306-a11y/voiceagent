# Getting Your Server — Beginner Walkthrough (Google Cloud)

**Step 1 of deployment**: getting a VM running and reachable. The app comes
after — see [`README.md`](./README.md).

About 20 minutes. Nothing here touches your project code, and you can
delete the VM at any point — you're only billed while it exists.

> **Why Google Cloud?** Oracle's free tier was tried first (free forever,
> ARM) but their signup rejected the account despite the card verifying —
> a common, opaque failure. GCP's free trial gives ₹28k / $300 credit for
> 90 days, which is far more than this needs.
>
> **Why a VM at all, rather than Render + Vercel?** This backend
> self-hosts its WebRTC media path, so it needs inbound UDP and a TURN
> relay. Render and Vercel are HTTP/TCP only — coturn can't run there.
> See the top of `README.md` for the longer version.

---

## Step 1 — Protect your credit

### ⚠️ Do NOT click "Activate full account"

That banner offers to convert you to a **paid** account, where charges
continue after credits run out. **Stay on the free trial** — on the trial
you cannot be surprise-billed. If credit runs out or 90 days pass,
resources simply stop.

### Set a budget alert

1. Go to https://console.cloud.google.com/billing/budgets
2. **Create budget** → name it `voiceagent-watch` → Next
3. Target amount: `5000` (₹) → Finish

You'll get an email if spend ever approaches that. For reference the VM
below runs ~₹2,500/month, so roughly ₹7,500 across the full 90 days.

---

## Step 2 — Create the VM

Go to https://console.cloud.google.com/compute/instances → **Create
instance**.

> First visit will ask you to **enable the Compute Engine API** — click
> Enable and wait a minute. Normal, happens once.

| Field | Value | Why |
|---|---|---|
| Name | `voiceagent` | |
| Region | `asia-south1 (Mumbai)` | Latency is a feature in a voice app — a US region adds ~250ms to every exchange |
| Zone | `asia-south1-a` | Any is fine |
| Machine type | E2 → `e2-medium` | 2 vCPU / **4 GB**. 2 GB is too tight for the turn-detection model plus a process per call |
| Boot disk | Change → Ubuntu → **Ubuntu 22.04 LTS** → 30 GB | |
| Firewall | ✅ Allow HTTP traffic<br>✅ Allow HTTPS traffic | Tick both now, save a debugging session later |

Click **Create**. Ready in under a minute.

---

## Step 3 — Lock the IP address in place

By default GCP gives a **temporary** IP that **changes on every restart**.
Once a domain points at it, that silently breaks your site and your HTTPS
certificate at some random future moment. Two minutes now avoids it.

1. Go to https://console.cloud.google.com/networking/addresses/list
2. Find the `voiceagent` row — it says **Ephemeral**
3. **⋮** menu at the end of the row → **Promote to static**
4. Name it `voiceagent-ip` → confirm

**Write down that IP.** Everything below needs it.

> A static IP is free while attached to a running VM. GCP charges a small
> amount for reserved IPs sitting unused — so if you delete the VM, release
> the IP too.

---

## Step 4 — Open the voice-relay ports

The HTTP/HTTPS checkboxes covered the website. TURN needs its own ports, or
calls from restrictive networks fail with no useful error.

1. Go to https://console.cloud.google.com/networking/firewalls/list
2. **Create firewall rule**

| Field | Value |
|---|---|
| Name | `allow-turn` |
| Targets | All instances in the network |
| Source IPv4 ranges | `0.0.0.0/0` |
| Protocols and ports | **Specified protocols and ports**:<br>✅ TCP → `3478,5349`<br>✅ UDP → `3478,5349,49160-49200` |

Create.

> Unlike Oracle, GCP's Ubuntu images have no restrictive local firewall —
> so this is the only firewall layer to configure.

---

## Step 5 — Connect

Easiest route: on https://console.cloud.google.com/compute/instances click
the **SSH** button on the `voiceagent` row. A terminal opens in a browser
window, already logged in — no keys, no usernames.

You'll get a prompt like `you@voiceagent:~$`. You're now on the server.

Sanity check:

```bash
free -h         # should show ~4 GB total
lsb_release -a  # should say Ubuntu 22.04
```

> Prefer your own terminal? Add your SSH public key under the VM's
> **Edit → SSH Keys**, then `ssh USERNAME@YOUR_IP`. Browser SSH is simpler
> and works fine for everything in the deploy steps.

---

## Step 6 — Get a free domain

Required, not optional: browsers **refuse microphone access** on non-HTTPS
origins, and you can't get a certificate for a bare IP. Without a domain, a
phone literally cannot start a call.

1. Go to https://www.duckdns.org
2. Sign in with Google
3. **sub domain** box → type a name, e.g. `nitya-voice` → **add domain**
4. Paste your static IP into that domain's **current ip** box → **update ip**

Confirm from PowerShell on your PC:

```powershell
nslookup nitya-voice.duckdns.org
```

Should print your server's IP. If not, wait a minute — DNS propagation.

---

## ✅ Done when you have

- [ ] A VM you can open a browser SSH session into
- [ ] Its **static IP** written down
- [ ] A **domain** resolving to that IP
- [ ] The `allow-turn` firewall rule created

Then continue with [`README.md`](./README.md) — Docker, deploying the app,
the HTTPS certificate, and testing a real call from a phone.

---

## Quick reference

| What | Where |
|---|---|
| Your VMs | https://console.cloud.google.com/compute/instances |
| IP addresses | https://console.cloud.google.com/networking/addresses/list |
| Firewall rules | https://console.cloud.google.com/networking/firewalls/list |
| Credit balance | https://console.cloud.google.com/billing |
| Free domain | https://www.duckdns.org |
| Connect | The **SSH** button on the VM row |
