# 🚀 Deploy to Render + use on your phone

Everything in this folder is ready to push to GitHub and deploy on Render —
no database, no build step, no third-party Python packages.

**What you end up with:** a private HTTPS URL like `https://fno-screener.onrender.com`
that you open on your phone, tap *Add to Home Screen*, and it behaves like an app —
live prices, picks, and a phone-friendly layout.

---

## 0 · What you need

| Thing | Where |
|---|---|
| GitHub account | github.com |
| Render account (free tier is fine) | render.com — sign in with GitHub |
| Your Upstox app's **API key + secret** (optional, for one-click login) | account.upstox.com/developer/apps |
| A password you'll use to protect the app | make one up |

---

## 1 · Put the code on GitHub

Create an empty **private** repo on GitHub (e.g. `fno-screener`), then from this folder:

```bash
git init
git add .
git commit -m "F&O live screener"
git branch -M main
git remote add origin https://github.com/<your-username>/fno-screener.git
git push -u origin main
```

Or without a terminal: on the empty repo page click **uploading an existing file** and drag
in every file/folder **except** `runtime/`, `cache/`, `logs/` and `__pycache__/`
(the `.gitignore` already excludes them if you use git).

> ⚠️ **Never commit `runtime/token.txt` or `runtime/credentials.json`.** They hold your
> Upstox access token and API secret. The included `.gitignore` blocks them.

---

## 2 · Deploy on Render

### Option A — Blueprint (easiest, uses the included `render.yaml`)

1. Render dashboard → **New +** → **Blueprint**.
2. Pick your `fno-screener` repo → **Connect**.
3. Render reads `render.yaml`, shows a service named `fno-screener`. Click **Apply**.
4. When it asks for the env vars marked `sync: false` (see step 3), fill them in.

### Option B — Manual web service

1. Render → **New +** → **Web Service** → pick the repo.
2. Settings:
   - **Runtime:** Python 3
   - **Build Command:** *(leave blank, or)* `echo "no build step"`
   - **Start Command:** `python3 -u server.py`
   - **Health Check Path:** `/api/health`
   - **Instance Type:** Free
3. **Create Web Service**. First deploy takes ~1–2 minutes.

---

## 3 · Environment variables (Render → your service → Environment)

| Key | Required | Value |
|---|---|---|
| `APP_PASSWORD` | **Yes — always** | The password you'll type on your phone. Without it, **anyone with the URL can see your screener.** |
| `UPSTOX_ACCESS_TOKEN` | Optional | Today's token. Skips the paste step after each restart. |
| `PYTHON_VERSION` | Optional | `3.12.6` |
| `CACHE_DIR` / `LOGS_DIR` / `RUNTIME_DIR` | Optional | Only if you attach a disk (step 7) |

Save → Render redeploys automatically.

---

## 4 · First run on your phone

1. Open your Render URL, e.g. `https://fno-screener.onrender.com`
2. **Enter your `APP_PASSWORD`** → the screener loads (stays signed in for 30 days).
3. ⚙ **Settings → Connect Upstox**:
   - **Easiest:** open `account.upstox.com/developer/apps`, click **Generate**, copy the token,
     paste it into the app → **Go live**.
   - **Smoother daily:** paste your **API key + secret** and save them; the app then shows the
     exact **Redirect URI** to register (`https://your-app.onrender.com/callback`) and a
     **Login with Upstox** button. From then on it's tap → approve → token saved automatically.
4. Wait for the warm-up banner to reach 100% (110 daily-candle calls, ~2–4 minutes on a cold
   start). Picks appear as coverage grows — you don't have to wait for all 210.

---

## 5 · Install it as a phone app

**iPhone (Safari):** open the URL → **Share** → **Add to Home Screen** → *Add*.
**Android (Chrome):** open the URL → **⋮** menu → **Add to Home screen** / **Install app**.

You get the ⚡ icon, a full-screen app (no browser chrome), notch-safe layout, and the
screen re-syncs the moment you reopen it. Pull up Settings to change scan depth, score floor,
ban list, etc. — all changes apply live.

---

## 6 · The daily routine (30 seconds a day)

Upstox tokens expire every night around **03:30 IST** — that's an Upstox rule, not the app's.

1. Open the app in the morning. If the token is dead you'll see a red banner saying so
   (the app stops calling the API the moment it's rejected — no silent failures).
2. Tap **Settings → Connect Upstox** → paste today's token → **Go live**.
   *(If you set up the API key + secret, just tap **Login with Upstox** → approve.)*
3. Warm-up finishes in ~2–4 minutes; be there by 09:10 so it's ready before 09:15.

---

## 7 · Nice-to-have: a persistent disk (paid plans)

On the free tier the container's filesystem is wiped on every sleep/restart, which means:

- the token, warm-up cache and pick logs disappear → re-paste the token and re-warm after a sleep,
- a cold start takes ~30–60s to wake plus the warm-up.

Attach a 1 GB disk (Render → service → **Disks** → Add, mount at `/var/data`), then set:

```
CACHE_DIR   = /var/data/cache
LOGS_DIR    = /var/data/logs
RUNTIME_DIR = /var/data/runtime
```

Now your token, credentials, warm-up data and pick history survive restarts and redeploys.
*(The app falls back to its own folder automatically if a path isn't writable, so nothing breaks
if you set these without attaching a disk.)*

---

## 8 · Free-tier behaviour worth knowing

| Behaviour | What to do |
|---|---|
| Service **sleeps after ~15 min** of no traffic | Open the app ~09:10; the first load also wakes it |
| Cold start ~30–60s + warm-up ~2–4 min | Open early; or upgrade to Starter (~$7/mo) for always-on |
| Data only moves while it's awake | It's a screener, not a broker — you always see the *latest* poll, never stale data pretending to be live |
| **Market closed = still useful** | It keeps the last session's levels and says so on every card (`NEXT SESSION`) |

---

## 9 · Security checklist

- ✅ `APP_PASSWORD` set (login is required; cookie is HttpOnly + SameSite, good for 30 days)
- ✅ Repo is **private**, and `runtime/` never committed
- ✅ `/robots.txt` disallows crawling
- ✅ Your API secret is only ever sent to `api.upstox.com`
- ⚠️ Anyone with the URL **and** the password gets in — use a password you don't reuse

---

## 10 · Troubleshooting

| Symptom | Fix |
|---|---|
| "Application failed to respond" on first load | Cold start — wait 60s and reload |
| Red banner: token invalid/expired | Generate a fresh token and paste it (step 6) |
| "Demo data" banner | No token stored — step 4 |
| Warm-up stuck part-way | It's rate-limited by Upstox (~80 calls/min). Wait; it resumes on its own |
| Picks look stale after your phone slept | The app re-fetches on reopen; check the clock pill reads the current time |
| Redirect URI error (`UDAPI100070`) | Register `https://your-app.onrender.com/callback` **exactly**, including `https` and `/callback` |
| Want the app on your desk too | `python3 server.py` locally, or just open the Render URL there |

---

## 11 · Updating the deployed app

```bash
git add .
git commit -m "tweak scoring"
git push
```

Render redeploys automatically (`autoDeploy: true` in `render.yaml`).
