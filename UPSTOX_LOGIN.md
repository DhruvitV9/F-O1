# 🔑 Getting your Upstox access token — all three ways

You do **not** need a redirect URL to paste a token. Pick whichever line below suits you.
Everything in this app works with any of them.

---

## ✅ Way 1 — Copy the token from the dashboard (fastest, ~30 seconds)

No redirect URL involved at all.

1. Open **https://account.upstox.com/developer/apps** and log in.
2. Click your app (or **Create app** if you don't have one yet — name it anything, put your
   registered redirect URI in the box, pick your market-data plan).
3. On the app page click **Generate** → an access token appears.
4. Copy it and paste it into the screener: **⚙ Settings → Connect Upstox → “Go live”**.
   (Or paste it in the chat here and I'll wire it up for you.)

That token is valid for the rest of the trading day, then dies around 3:30 AM IST.

---

## 🔁 Way 2 — One-click login (register a redirect URL)

If you'd rather click a button each morning than copy-paste tokens:

### Register one of these as your app's **Redirect URI**

| Redirect URI | When it works |
|---|---|
| `http://localhost:8000/callback` | **Recommended.** You run the app on your own computer (`python3 server.py`). |
| `https://8000-ivoclh55whf1gsrcidz8r.e2b.app/callback` | Only while *this* hosted preview stays alive. It's also access-protected, so Upstox's redirect may bounce — see the note at the bottom. |

Upstox requires an **exact match** — same `http`/`https`, same host, same `/callback` path,
no trailing slash. Register both if you're unsure; Upstox accepts multiple.

### Then in the screener

1. **⚙ Settings → Connect Upstox → step 2**: paste your **API key** (client id) and
   **API secret** from the app page → **Save**.
2. The app shows you the exact Redirect URI to register, and unlocks the
   **Login with Upstox →** button.
3. Click it → log in and approve on Upstox → the browser returns to `/callback` → the token
   is exchanged and saved automatically → the screener switches to live on its own.

If you're viewing the app through a sandboxed preview, the popup may be blocked — click
**copy login link** and open it in a normal browser tab instead.

---

## 🧠 Way 3 — The trick: any redirect URL works, you just need the `code`

Whatever redirect URI your app is registered with, after you log in Upstox sends the browser to:

```
<your-redirect-uri>?code=ABCDEF&state=xyz
```

That code is all the app needs — the secret exchange happens server-side here. So:

1. Start the login (from the app, or open the login link the app generates).
2. Approve on Upstox.
3. The browser lands on your redirect URI. It may be a **404 page** or your own website —
   doesn't matter.
4. **Copy the whole URL from the address bar** (or just the `code=` value).
5. Paste it into **⚙ Settings → Connect Upstox → “paste the code / the whole redirect URL here”**
   → **Finish login**.

This is the most forgiving route: it works with any app configuration, and it's exactly why
your registered redirect URL mostly doesn't matter for a personal tool.

> Rules to respect: the code is **single-use**, **short-lived**, and must be exchanged
> **server-to-server with your client_secret** (this app does that for you). Codes and secrets
> are never logged, and are stored only in `runtime/` on the machine running the screener.

---

## Note about the hosted preview URL

I tested `https://8000-ivoclh55whf1gsrcidz8r.e2b.app/callback` from outside the sandbox and the
hosting layer replies **“Missing Traffic Access Token”** — the preview URL is access-protected,
so a plain browser redirect from Upstox will not land on it.

**So if you want the login flow, register `http://localhost:8000/callback` and run the app on
your own machine.** The token-paste route (Way 1 / Way 3) has no such problem and works with
the hosted preview too.

---

## Quick reference

| What | Value |
|---|---|
| Developer dashboard | https://account.upstox.com/developer/apps |
| Login dialog | `https://api.upstox.com/v2/login/authorization/dialog?response_type=code&client_id=…&redirect_uri=…` |
| Token exchange (server-side) | `POST https://api.upstox.com/v2/login/authorization/token` |
| This app's callback | `/callback` on whatever host you're browsing (auto-detected) |
| Day-to-day | New token every morning; the app tells you the moment it expires |
