# Getting Your Server — Complete Beginner Walkthrough

This covers **Step 1 only**: getting a free server (a "VM") from Oracle and
being able to connect to it. Nothing about the app yet — just the machine.

Take your time. Nothing here can break your project; the code is safe on
GitHub either way.

---

## Before you start — what you'll need

- A **credit or debit card**. Oracle uses it to verify you're a real
  person. The Always Free resources genuinely don't charge it — but the
  card is required to sign up at all. Oracle may place a temporary ₹1-ish
  authorization hold that reverses itself.
- About **30–45 minutes**, mostly waiting on Oracle.
- An **email address**.

---

## Part 1 — Your SSH key

An SSH key is how you prove it's you when connecting to the server — like
a house key, but a file. Oracle asks for it *during* server creation, so
have it ready first.

### First: do you already have one?

```powershell
Get-Content "$env:USERPROFILE\.ssh\id_ed25519.pub"
```

**If that prints a line starting with `ssh-ed25519 AAAA...` — you already
have a key. Skip the rest of Part 1 and go to Part 2.** That line is what
you'll paste into Oracle. One key works for everything; you don't need a
separate one per server.

> ⚠️ **If `ssh-keygen` ever asks *"id_ed25519 already exists. Overwrite
> (y/n)?"* — answer `n`.** Overwriting permanently destroys your existing
> private key. If you use SSH with GitHub (this project does), that key is
> almost certainly your GitHub key, and overwriting it breaks your ability
> to push until you register a new one. There is no reason to overwrite.
>
> To check whether it's your GitHub key: `ssh -T git@github.com` — if it
> greets you by username, that's the one.

### Only if you got an error above (no key yet)

```powershell
ssh-keygen -t ed25519 -C "voiceagent-server"
```

It will ask three things:

1. **"Enter file in which to save the key"** → just press **Enter** (accepts
   the default location, `C:\Users\<you>\.ssh\id_ed25519`)
2. **"Enter passphrase"** → press **Enter** for none, or type one if you
   want extra safety (you'll type it each time you connect)
3. **"Enter same passphrase again"** → same as above

Now show your **public** key so you can copy it later:

```powershell
Get-Content "$env:USERPROFILE\.ssh\id_ed25519.pub"
```

It prints one long line starting with `ssh-ed25519 AAAA...`. **That whole
line is what you paste into Oracle.** Keep this window open.

> **Important:** the file ending in `.pub` is the *public* key — safe to
> share. The file *without* `.pub` is your private key. Never share, paste,
> or upload that one anywhere.

---

## Part 2 — Create the Oracle account

1. Go to **https://www.oracle.com/cloud/free/**
2. Click **Start for free**
3. Fill in your country and email, verify the email
4. **CHOOSE YOUR REGION CAREFULLY — THIS IS PERMANENT.** You cannot change
   it later without making a whole new account. Pick the one geographically
   closest to you (in India: Mumbai or Hyderabad). If you're flexible, a
   less busy region improves your odds in Part 3 — more on that below.
5. Enter your card details for verification
6. Wait. Account provisioning takes anywhere from 2 minutes to a few hours.
   You'll get an email when it's ready.

When it's done, sign in at **https://cloud.oracle.com**

---

## Part 3 — Create the server

1. In the Oracle console, click the **hamburger menu (☰)** top-left
2. Go to **Compute → Instances**
3. Click **Create instance**

Now fill in the form. These are the settings that matter:

### Name
Anything. `voiceagent` is fine.

### Image and shape — click "Edit"

**Image:** click *Change image* → select **Canonical Ubuntu** →
version **22.04** → Select image

**Shape:** click *Change shape* → **Ampere** tab → select
**VM.Standard.A1.Flex** → then set:
- **OCPUs:** `2`
- **Memory (GB):** `12`

> Why these numbers: Oracle's Always Free tier gives you 4 OCPUs and 24 GB
> total. Asking for 2/12 leaves headroom and — genuinely — improves your
> chance of getting capacity than asking for the whole allowance at once.

### Add SSH keys
Select **Paste public keys**, then paste that whole `ssh-ed25519 AAAA...`
line from Part 1.

### Then click **Create**.

---

## ⚠️ If you see "Out of host capacity"

**This is normal and it is not your fault.** Oracle's free ARM servers are
in very high demand and regularly unavailable. Almost everyone hits this.

What actually works:

- **Just retry.** Click Create again. And again. Capacity frees up
  constantly — many people succeed after 5–20 tries across a day.
- **Try at off-peak hours** for your region (very early morning is often
  better).
- **Lower the specs** — try 1 OCPU / 6 GB. Still enough to run this.
- **Be patient across days.** It's common to need 2–3 days of occasional
  retries.

**If it just won't work after a few days,** tell me and we'll switch to a
cheap paid alternative — Hetzner is about €4/month and takes 5 minutes with
no capacity games. Everything we built works identically there; nothing is
Oracle-specific. Don't burn a week fighting this.

---

## Part 4 — Get your server's address

Once the instance is created, you land on its details page.

Find **Public IP address** — something like `140.238.x.x`.

**Write it down.** You need it for everything that follows.

---

## Part 5 — Connect to it

Back in PowerShell:

```powershell
ssh ubuntu@YOUR_PUBLIC_IP
```

(replace `YOUR_PUBLIC_IP` with the real number, and note the username is
`ubuntu` for Ubuntu images)

First time, it asks *"Are you sure you want to continue connecting?"* →
type `yes` and Enter.

**If it works,** your prompt changes to something like
`ubuntu@voiceagent:~$`. You are now typing commands *on the server*. 🎉

**If it hangs or times out,** the firewall is blocking you — that's Part 2
of the main deploy guide (Oracle has two separate firewalls, which is the
classic beginner trap). Tell me and I'll walk you through it.

---

## Part 6 — Get a free domain name

You need a domain because browsers **refuse microphone access** on anything
that isn't HTTPS — and you can't get an HTTPS certificate for a bare IP
address. Without this, a phone literally cannot start a call. It's not
optional.

Free and takes 3 minutes:

1. Go to **https://www.duckdns.org**
2. Sign in with Google or GitHub (top of the page)
3. In the **"sub domain"** box, type a name — e.g. `nitya-voice`
4. Click **add domain**
5. In the **current ip** box for that domain, paste your server's public IP
6. Click **update ip**

You now own `nitya-voice.duckdns.org` (or whatever you picked), pointing at
your server. Free, permanent.

Check it worked — back in PowerShell:

```powershell
nslookup nitya-voice.duckdns.org
```

It should print your server's IP. If it doesn't, wait a minute and retry —
DNS takes a moment to propagate.

---

## ✅ You're done with Step 1 when you have:

- [ ] A server you can `ssh` into
- [ ] Its **public IP** written down
- [ ] A **domain** that resolves to that IP

**Bring me those two values (IP + domain)** and I'll walk you through the
rest — firewall, deploying the app, and testing a real call from your
phone. That part is mostly copy-paste.

---

## Quick reference

| What | Where |
|---|---|
| Oracle signup | https://www.oracle.com/cloud/free/ |
| Oracle console (after signup) | https://cloud.oracle.com |
| Free domain | https://www.duckdns.org |
| Your instances | Console → ☰ → Compute → Instances |
| Connect | `ssh ubuntu@YOUR_IP` |
