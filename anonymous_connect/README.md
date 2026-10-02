# Anonymous Connect

A Django + Channels (WebSocket) web app that randomly pairs two people for a **real-time text chat** or a **peer-to-peer voice call** (WebRTC). Works for anonymous guests (session-based) and registered users (mobile OTP or Google email). Includes a Razorpay-backed "Premium" upgrade that unlocks match filtering, unlimited call duration, and a "reconnect last user" feature.

> This README is the single source of truth for the project. It is written so that an AI agent (or a new developer) can understand the entire system — architecture, data model, request/socket flow, and every file's role — without re-reading all the source. When you ask me to change something, point me here first.

---

## 1. TL;DR — What this app does

1. A user lands on `/`, fills a short profile form (name, age, sex, profession), and clicks **Connect Random Chat** or **Connect Voice Call**.
2. The server marks them `online` and looks for another `online` user. If found, both are put into a shared **room** and marked `busy`; the browser opens a WebSocket to that room.
3. **Chat mode:** messages are relayed through the server (Django Channels group). The UI shows **WhatsApp-style bubbles** — your messages on the right (green), the partner's on the left (white) — plus **sent/read ticks**, and a **Start Voice Call** button that escalates the text chat into a live voice call.
   **Voice mode:** the server only relays WebRTC signalling (SDP offer/answer + ICE candidates); the actual audio flows **peer-to-peer** between browsers.
4. A countdown timer limits free calls to **5 minutes**; premium users get "unlimited".
5. When either side hangs up or disconnects, the server tells the other side (`call_ended`) and resets both profiles so they are discoverable again.

---

## 2. Tech stack

| Layer | Technology |
|------|-----------|
| Web framework | Django 5.2.14 |
| Realtime / WebSockets | Django Channels 4.3.2 (ASGI) |
| ASGI server | Daphne 4.2.1 |
| Channel layer | In-memory (dev) / Redis via `channels_redis` (prod) |
| Database | SQLite (dev) / PostgreSQL via `psycopg2-binary` (prod) |
| Auth | Django auth + `django-allauth` (Google) + custom mobile-OTP |
| Payments | Razorpay (`razorpay` 2.0.1) |
| Static files | WhiteNoise (compressed manifest storage) |
| Voice | Browser **WebRTC** (STUN: `stun.l.google.com:19302`) |
| SMS (optional) | Twilio (dependency present; not yet wired into OTP) |
| Python | 3.10.4 (`runtime.txt`) |

---

## 3. Directory map

```
anonymous_connect/                 <- Django project ROOT (manage.py lives here)
├── manage.py
├── Procfile                       <- web: daphne ... ; release: migrate
├── runtime.txt                    <- python-3.10.4
├── requirements.txt
├── .env.example                   <- all supported environment variables
├── DEPLOYMENT.md
├── db.sqlite3                     <- dev database
│
├── anonymous_connect/             <- project config package
│   ├── settings.py                <- all configuration (env-driven)
│   ├── urls.py                    <- root URLconf (admin + include chat.urls)
│   ├── asgi.py                    <- ASGI entrypoint (HTTP + WebSocket routing)
│   └── wsgi.py                    <- WSGI entrypoint (unused in prod; ASGI is used)
│
├── chat/                          <- the ONE app that holds all app logic
│   ├── models.py                  <- UserProfile, lastConnected, Transaction + signals
│   ├── views.py                   <- all HTTP views (home, match, premium, auth, health)
│   ├── consumers.py               <- ChatConsumer (WebSocket: chat + WebRTC signalling)
│   ├── routing.py                 <- WebSocket URL -> consumer
│   ├── urls.py                    <- HTTP URL patterns for the app
│   ├── admin.py                   <- Django admin registration
│   ├── apps.py
│   ├── migrations/                <- 0001..0009 schema history
│   ├── tests.py / test_consumers.py
│   └── templates/chat/
│       ├── home.html              <- landing page + profile form + searching overlay
│       ├── call.html              <- the call/chat room (WebSocket + WebRTC client)
│       ├── login.html             <- mobile OTP + Google login choices
│       ├── verify_otp.html        <- OTP entry form
│       ├── payment.html           <- Razorpay premium checkout
│       └── test.html
│
└── static/chat/
    ├── css/style.css
    └── js/main.js                 <- modal helpers (voice logic actually lives in call.html)
```

Key mental model: **almost everything lives in the single `chat` app.** The project package only wires config and routing.

---

## 4. Data model (`chat/models.py`)

### `UserProfile` — the central model
One profile per person, whether logged in or anonymous.

- **Identity (one of two):**
  - `user` — `OneToOneField(User)`, nullable. Set for registered users.
  - `session_id` — unique `CharField`, nullable. Set for anonymous guests (the Django session key).
  - A profile created on `post_save` of `User` always has `user` set; a guest profile has only `session_id`.
- **Live call state:**
  - `status` — `online` / `busy` / `offline` (`Status` TextChoices). Drives matchmaking.
  - `active_room_name` — the room the user is currently in (e.g. `room_3_7`).
- **Profile details:** `display_name`, `age`, `sex` (`any/male/female/other`), `profession`.
- **Premium match preferences (only used while premium):** `pref_sex`, `pref_profession`, `pref_age_min`.
- **Premium & wallet:** `is_premium` (bool), `premium_expiry` (datetime), `wallet_balance` (decimal).
- **Reconnect tracking (designed to reset daily):** `reconnect_count`, `last_reset_date`.
- **History:** `last_connected_session` — session id/key of the last matched person (powers "Connect Last User").

**Helper methods you should know:**
- `premium_active` (property) — `True` only if `is_premium` AND `premium_expiry` is in the future. (Intended as the "real" premium check.)
- `reset_daily_counters_if_needed()` — zeroes `reconnect_count` and lapses premium past its expiry, once per calendar day.
- `reconnect_limit` / `can_reconnect()` — 5 reconnects for premium, 1 for free.
- `next_midnight()` — upcoming local midnight (for daily premium expiry).
- `clear_user_entry()` — **cleanup on hangup/disconnect.** For anonymous (session-only) profiles it **deletes the row**; for registered users it keeps the row but resets `status=offline` and clears `active_room_name`.

### `lastConnected`
A flat log of a pairing: `user_session_id -> last_user_session_id`. Written in `match_user` when both participants are session-based.

### `Transaction`
Razorpay payment record: `user`, `amount`, `razorpay_order_id`, `razorpay_payment_id`, `timestamp`, `status` (`Pending/Success/Failed`). Created as `Pending` when an order is made, flipped to `Success`/`Failed` after signature verification.

### Signals
`post_save` on `User` → auto-create/ensure a `UserProfile` exists for every registered user.

### Pricing constants (module-level)
- `PREMIUM_DAILY_PRICE_INR = 10`, `PREMIUM_RECONNECT_LIMIT = 5`, `FREE_RECONNECT_LIMIT = 1`.

> ⚠️ **Known inconsistency to be aware of before editing premium logic:** the *model layer* is designed around **daily** premium (`premium_expiry`, `premium_active`, daily counter resets, `PREMIUM_DAILY_PRICE_INR = 10`). But the *view layer* (`views.py`) charges a **flat ₹500** (`PREMIUM_PRICE_INR = 500`), sets `is_premium = True` **permanently** and never sets `premium_expiry`. Matchmaking checks the raw `profile.is_premium` flag, **not** `premium_active`, and `reset_daily_counters_if_needed()` is never called. So today premium is effectively a one-time permanent upgrade. If you want true daily premium, that gap in `views.py` is where to fix it.

---

## 5. URL / route map

### HTTP routes
Root URLconf (`anonymous_connect/urls.py`): `admin/` + `include('chat.urls')`. In `DEBUG`, static files are also served.

`chat/urls.py`:

| Path | View | Name | Purpose |
|------|------|------|---------|
| `/` | `home` | `home` | Landing page + profile form; POST triggers matchmaking |
| `/match/<mode>/<name>/` | `match_user` | `match_user` | Find/rejoin a partner; renders the call room |
| `/hangup/` | `hangup_view` | `hangup_view` | Reset profile, redirect home |
| `/connect-last/` | `connect_last_user` | `connect_last_user` | Premium: rejoin last partner |
| `/premium/` | `premium_page` | `premium_page` | Razorpay checkout page (login required) |
| `/payment/success/` | `payment_success` | `payment_success` | Verify signature, grant premium (POST, login required) |
| `/login/` | `custom_login` | `login` | Choose mobile OTP or Google |
| `/send-otp/` | `send_otp` | `send_otp` | Generate + "send" OTP (currently logged, not SMS) |
| `/verify-otp/` | `verify_otp` | `verify_otp` | Verify OTP, create/login `user_<mobile>` |
| `/accounts/...` | allauth | — | Google OAuth callbacks etc. |
| `/healthz/` | `healthz` | `healthz` | DB health probe (200/503 JSON) |

### WebSocket route
`chat/routing.py`: `ws/chat/<room_name>/` → `ChatConsumer`. The client builds this URL in `call.html` as `ws(s)://<host>/ws/chat/<room_name>/`.

---

## 6. End-to-end flow

### 6.1 Matchmaking (HTTP) — `home` → `match_user`
1. **`home` (GET):** resolves the profile via `get_user_profile()` (registered user → by `user`; guest → create a session + profile by `session_id`). Renders `home.html` with the form.
2. **`home` (POST):** saves form fields onto the profile. Premium users also save `pref_sex`/`pref_profession`. If a Connect button was pressed, `connection_mode` is `chat` or `voice` and the view **redirects to `match_user`**.
3. **`match_user`:**
   - If the profile is already `busy` with an `active_room_name`, it **rejoins** that room (renders `call.html`).
   - Otherwise sets `status=online`, then queries other `status=online` profiles (excluding self).
   - **Premium filtering:** if premium, narrows the pool by `pref_sex` and `pref_profession` — but only if the filter still leaves at least one candidate (otherwise it keeps the broader pool, so premium users still get matched).
   - Picks one at random (`order_by('?')`).
   - **If matched:** builds a deterministic `room_name = room_<minId>_<maxId>`, records `last_connected_session` on both (and a `lastConnected` row if both are session users), sets both to `busy` with the shared `active_room_name`, saves both, renders `call.html`.
   - **If no match:** renders `home.html` with `searching=True`. That page waits 3 seconds then redirects back to `match_user` to retry (simple polling-by-reload).
4. **Timer value passed to the template:** `300` seconds for free users, `999999` (shown as "Unlimited") for premium.

### 6.2 In the room (WebSocket + WebRTC) — `call.html` + `ChatConsumer`
When `call.html` loads it opens `ws(s)://<host>/ws/chat/<room_name>/`.

**Server side — `ChatConsumer` (`consumers.py`):**
- `connect()` — reads `room_name` from the URL, joins Channels group `chat_<room_name>`, resolves the profile, accepts the socket.
- `receive(text_data)` — inspects `type`:
  - `offer` / `answer` / `ice-candidate` and the voice-call control messages `call-request` / `call-accept` / `call-reject` / `call-hangup` → re-broadcast to the group as a `signal_message` **tagged with the sender's channel** (so the sender doesn't receive its own signal back).
  - `read_receipt` → re-broadcast as a `read_receipt` event (also sender-tagged, so only the original message sender receives it).
  - anything else → treated as a chat message; broadcast as `chat_message` with the sender's `display_name`, plus the `sender_id` and `message_id` carried through for bubble alignment and read ticks.
- Group handlers `chat_message`, `signal_message`, `read_receipt`, `call_ended` push JSON back down to each connected browser. `signal_message` and `read_receipt` skip the original sender.
- `disconnect()` — broadcasts `call_ended` to the group, then runs `perform_full_cleanup()` → `profile.clear_user_entry()` (frees/deletes the profile), then leaves the group.

**Client side — chat mode:**
- **Per-tab identity:** on load the page generates a random `sender_id` and stores it in `sessionStorage`. This is sent with every message so each browser can tell *its own* messages from the partner's — reliably, even when both users share the display name "Anonymous".
- **Send:** `{ type: 'chat', message: <text>, sender_id, message_id }`. The sender renders its own bubble **immediately** (right-aligned, green) with a single grey ✓ (sent); it does **not** re-render the server echo of its own message (detected by matching `sender_id`).
- **Receive:** a `chat_message` from the partner (different `sender_id`) is appended left-aligned (white) with the sender's name, and the receiver immediately sends a `read_receipt` back.
- **Read ticks:** when the original sender receives the `read_receipt` for a `message_id`, that message's tick upgrades from single grey ✓ (sent) to blue double ✓✓ (read).
- **Escalate to voice:** chat mode shows a **📞 Start Voice Call** button (see the voice-call handshake below).

**Chat → voice escalation handshake (chat mode):**
- Caller clicks *Start Voice Call* → sends `call-request`. A "Calling…" banner appears.
- Partner sees an **Accept / Decline** banner. On **Accept** it grabs the mic, replies `call-accept`, and creates the SDP **offer**. On **Decline** it replies `call-reject`.
- Both sides then exchange `offer` / `answer` / `ice-candidate` exactly like voice mode. Either side can end the live call with `call-hangup` (banner clears, mic released) without leaving the chat.
- All of these control messages (`call-request/accept/reject/hangup`) and the WebRTC signals are relayed by `ChatConsumer` to the **other** browser only (never echoed to the sender).

**Client side — voice mode (WebRTC):**
- On socket open, both peers call `startVoice()`: `getUserMedia({audio:true})`, create `RTCPeerConnection` with the Google STUN server, add the mic track.
- After ~800ms, whichever peer is in `signalingState === 'stable'` creates an **SDP offer**, sets it locally, and sends `{type:'offer', sdp}`.
- The other peer receives `offer` → sets remote description → creates an **answer** → sends `{type:'answer', sdp}`.
- Both exchange `ice-candidate` messages until the P2P audio connection is established; remote audio plays via the `<audio id="remoteAudio" autoplay>` element (present in **both** modes so chat can escalate to voice).
- **ICE buffering:** candidates that arrive before the remote description is set are queued and flushed once `setRemoteDescription` completes, which avoids a race that could drop early candidates.
- The server never sees the audio — only the signalling messages.

**Timer & hangup:**
- A JS countdown starts at the `timer` value. At `0` it calls `hangup()`. Values > 10000 render as "Unlimited".
- `hangup()` stops mic tracks, closes the peer connection and socket, and navigates to `/hangup/` → `hangup_view` resets the profile and redirects home.
- If the *other* side drops, this side receives `call_ended`, shows "Partner Disconnected", and redirects home after 3 seconds.

### 6.3 Authentication
- **Mobile OTP (custom):** `login.html` → POST to `send_otp` → a random 6-digit code is stored in the session and **logged** (no SMS gateway wired yet; Twilio is a dependency placeholder). `verify_otp` compares the entered code to the session code; on success it `get_or_create`s a user named `user_<mobile>` and logs them in.
- **Google (allauth):** `custom_login` routes the "email" method to `/accounts/google/login/` **only if** a Google `SocialApp` is configured (otherwise the button is disabled, avoiding a 500). Configure the Google provider via the Django admin / allauth.

### 6.4 Premium purchase (Razorpay)
1. `premium_page` (login required): if Razorpay keys exist and the user isn't premium, it creates a **server-side order** for ₹500 (`50000` paise) and records a `Pending` `Transaction`. Renders `payment.html`.
2. `payment.html` loads Razorpay Checkout. On success, the gateway returns `order_id`, `payment_id`, `signature`, which the page POSTs to `payment_success`.
3. `payment_success` (login required, POST): **verifies the signature server-side** with the Razorpay secret. On success it sets `is_premium=True`, credits `wallet_balance += 500`, and marks the transaction `Success` (guarding against double-crediting duplicate callbacks). On failure it marks the transaction `Failed` and never upgrades. **The client is never trusted to self-report success.**

---

## 7. ASGI wiring (`anonymous_connect/asgi.py`)
`ProtocolTypeRouter`:
- `http` → standard Django ASGI app.
- `websocket` → `SessionMiddlewareStack(AuthMiddlewareStack(URLRouter(chat.routing.websocket_urlpatterns)))`.

The session + auth middleware stacks are what let `ChatConsumer.get_profile()` read `scope['user']` and `scope['session']` to identify the connected person. `django.setup()` is called **before** importing Channels routing — order matters here.

---

## 8. Configuration (`settings.py`) — all env-driven

Everything meaningful is read from environment variables with safe local defaults, so the app runs with **zero setup** locally (SQLite + in-memory channel layer) and is deployment-ready via env vars. Full list in `.env.example`.

| Concern | Env var(s) | Default / behaviour |
|--------|-----------|---------------------|
| Secret key | `DJANGO_SECRET_KEY` | insecure dev key |
| Debug | `DJANGO_DEBUG` | `True` |
| Allowed hosts | `DJANGO_ALLOWED_HOSTS` | `*` |
| CSRF origins | `CSRF_TRUSTED_ORIGINS` | empty |
| Database | `DB_ENGINE=postgres` + `DB_NAME/USER/PASSWORD/HOST/PORT` | SQLite fallback |
| Channel layer | `REDIS_URL` | in-memory layer (single-process only) |
| Payments | `RAZOR_KEY_ID`, `RAZOR_KEY_SECRET` | empty → gateway disabled gracefully |
| Email | `EMAIL_HOST` (+ port/user/password/TLS) | console backend if unset |
| allauth email verify | `ACCOUNT_EMAIL_VERIFICATION` | `optional` in dev, `mandatory` in prod |
| HTTPS hardening | `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`, `SECURE_SSL_REDIRECT`, `SECURE_HSTS_*` | auto-applied when `DEBUG=False` |
| Logging | `DJANGO_LOG_LEVEL` | `INFO` to stdout |
| OTP SMS (optional) | `TWILIO_SID/TOKEN/FROM` | not yet wired |

**Important production notes:**
- The **in-memory channel layer only works with a single process.** For real multi-worker deployments you **must** set `REDIS_URL`, or matched users on different workers won't be able to message/signal each other.
- Static files are served by WhiteNoise with `CompressedManifestStaticFilesStorage`. Run `collectstatic` on deploy.
- `SITE_ID = 1` is required by allauth.

---

## 9. Running locally

```powershell
# from the project root (where manage.py is)
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser        # optional, for /admin
# Use the ASGI server so WebSockets work:
daphne -b 127.0.0.1 -p 8000 anonymous_connect.asgi:application
# (python manage.py runserver also works for dev since Channels is installed)
```

Then open http://127.0.0.1:8000/. To test matchmaking you need **two sessions** — use a second browser or an incognito window so they get distinct session keys.

> Voice calls require **HTTPS** (or `localhost`) because browsers only allow `getUserMedia` in secure contexts. On `localhost` it works; on a plain-HTTP remote host the mic will be blocked.

---

## 10. Deployment

- **Process model (`Procfile`):**
  - `web: daphne -b 0.0.0.0 -p $PORT anonymous_connect.asgi:application`
  - `release: python manage.py migrate --noinput`
- Set the production env vars from `.env.example` (especially `DJANGO_SECRET_KEY`, `DJANGO_DEBUG=False`, `DJANGO_ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, a Postgres `DB_*` set, and `REDIS_URL`).
- Run `collectstatic` as part of the build.
- See `DEPLOYMENT.md` for host-specific notes.
- `/healthz/` returns `200` when the DB is reachable, `503` otherwise — point your platform's health check here.

---

## 11. Admin (`/admin`)
Registered models: `UserProfile` (list/filter/search on premium, sex, profession, status; grouped fieldsets; `user` read-only), `Transaction` (payment log), `lastConnected`. Use the admin to configure the **Google `SocialApp`** needed for Google login.

---

## 12. Gotchas & current limitations (read before changing behaviour)

- **Premium is effectively permanent, not daily** — see the inconsistency note in §4. The daily-premium machinery exists in the model but is not invoked by the views.
- **Matchmaking is retry-by-reload**, not push. A searching user's browser reloads `match_user` every 3 seconds. There is no live "waiting pool" notification.
- **Single-process channel layer in dev** — cross-worker messaging needs Redis (§8).
- **OTP is not actually sent** — the 6-digit code is written to the application log only. Wire Twilio (already a dependency) in `send_otp` to send real SMS.
- **No real match "lock"** — two near-simultaneous searchers could in theory both pick the same partner; matching relies on quick DB status flips rather than a transaction/lock.
- **`main.js` voice code is a stub** — the real WebRTC implementation, chat bubbles, ticks, and the chat→voice handshake all live inline in `call.html`. Edit `call.html` for call/chat UI behaviour.
- **Read ticks are "delivered + read", not three-state** — a message shows single grey ✓ on send and blue double ✓✓ once the partner's browser receives it and returns a `read_receipt`. There is no separate "delivered but not yet seen" state, since the receipt is sent as soon as the message arrives in the room.
- **STUN only, no TURN** — voice uses only Google's public STUN server. Peers behind symmetric NATs/strict firewalls may fail to connect; add a TURN server for reliability.
- **`age`/`pref_age_min` are stored but not used in matching** — the age-group preference is modelled but not applied in `match_user`.

---

## 13. "I want to change X — where do I look?"

| Goal | File(s) |
|------|---------|
| Matchmaking logic / filters | `chat/views.py` → `match_user` |
| Call/chat UI, timer, WebRTC client | `chat/templates/chat/call.html` |
| WebSocket relay / signalling / cleanup | `chat/consumers.py` |
| Profile fields / premium rules | `chat/models.py` |
| Landing page & profile form | `chat/templates/chat/home.html` |
| Payment flow | `chat/views.py` (`premium_page`, `payment_success`) + `payment.html` |
| Login / OTP / Google | `chat/views.py` (`custom_login`, `send_otp`, `verify_otp`) + `login.html`, `verify_otp.html` |
| Routes (HTTP) | `chat/urls.py` |
| Routes (WebSocket) | `chat/routing.py` |
| Settings / env / channel layer / DB | `anonymous_connect/settings.py`, `.env.example` |
| ASGI / middleware stack | `anonymous_connect/asgi.py` |
| Deploy config | `Procfile`, `runtime.txt`, `DEPLOYMENT.md` |
```
