import logging
import random
import time

import razorpay
from django.shortcuts import render, redirect
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required
from django.conf import settings
from django.db import connection, transaction
from django.core.mail import send_mail
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
from django.views.decorators.http import require_POST

from .models import (
    UserProfile,
    lastConnected,
    Status,
    Transaction,
    PREMIUM_DAILY_PRICE_INR,
    PREMIUM_DAILY_PRICE_USD,
)

from activity_logging import Action, log_event

logger = logging.getLogger(__name__)


def name_from_email(email):
    """Build a human-friendly name from the local part of an email address.

    Examples:
        "john.doe@gmail.com"   -> "John Doe"
        "jane_smith99@x.com"   -> "Jane Smith"
        "mkumar@company.in"    -> "Mkumar"

    Trailing digits are stripped and common separators (., _, -, +) are turned
    into spaces so each word can be capitalised. Falls back to "Friend" when the
    local part has no usable letters.
    """
    local = (email or '').split('@')[0]
    # Replace common separators with spaces.
    for sep in ('.', '_', '-', '+'):
        local = local.replace(sep, ' ')
    # Split into words, drop trailing digits from each word, and title-case.
    words = []
    for word in local.split():
        cleaned = word.rstrip('0123456789')
        cleaned = cleaned or word  # keep original if it was all digits
        if cleaned:
            words.append(cleaned.capitalize())
    return ' '.join(words) if words else 'Friend'


def profile_display_context(request):
    """Logged-in display info for the top-right profile widget.

    Returns the user's name, email, and the uppercase initial used for the
    avatar bubble. Empty for anonymous guests so the template can hide it.
    """
    if not request.user.is_authenticated:
        return {}
    user = request.user
    name = (user.first_name or '').strip() or name_from_email(user.email) or user.username
    email = user.email or ''
    initial = (name[:1] or email[:1] or '?').upper()
    return {
        'account_name': name,
        'account_email': email,
        'account_initial': initial,
    }


def get_user_profile(request):
    """Bulletproof helper to get profile for a registered User or a Guest."""
    if request.user.is_authenticated:
        profile, _ = UserProfile.objects.get_or_create(user=request.user)
    else:
        if not request.session.session_key:
            request.session.create()
        profile, _ = UserProfile.objects.get_or_create(session_id=request.session.session_key)
    return profile


# --- DASHBOARD & MATCHMAKING LOGIC ---

def home(request):
    profile = get_user_profile(request)

    if request.method == "POST":
        # Capture form details
        profile.display_name = request.POST.get('display_name', 'Anonymous')
        age = request.POST.get('age')
        profile.age = int(age) if age and age.isdigit() else None
        profile.sex = request.POST.get('sex', 'any')
        profile.profession = request.POST.get('profession', 'any')

        if profile.premium_active:
            profile.pref_sex = request.POST.get('pref_sex', 'any')
            profile.pref_profession = request.POST.get('pref_profession', 'any')
            pref_age_min = request.POST.get('pref_age_min')
            profile.pref_age_min = int(pref_age_min) if pref_age_min and pref_age_min.isdigit() else 0

        profile.save()

        # --- Activity logging: record which preferences the user selected. ---
        # Only log fields the user actually submitted so the report reflects
        # real selections rather than defaults.
        if request.POST.get('age'):
            log_event(Action.AGE_SELECTED, request=request, profile=profile,
                      age=profile.age)
        if request.POST.get('sex'):
            log_event(Action.GENDER_SELECTED, request=request, profile=profile,
                      gender=profile.sex)
        if request.POST.get('profession'):
            log_event(Action.PROFESSION_SELECTED, request=request, profile=profile,
                      profession=profile.profession)

        # Check if the user clicked one of the "Connect" buttons
        mode = request.POST.get('connection_mode')
        if mode in ['chat', 'voice']:
            return redirect('match_user', mode=mode, name=profile.display_name)

    else:
        # GET request -> this is a site visit. Record IP + date/time.
        log_event(Action.VISIT, request=request, profile=profile)

    context = {'profile': profile}
    context.update(profile_display_context(request))
    return render(request, 'chat/home.html', context)


def _session_token(profile):
    """The identifier stored in ``last_connected_session`` for a profile."""
    return profile.session_id or (f"user_{profile.user_id}" if profile.user_id else None)


def _pair_profiles(profile, match):
    """Put two profiles into a shared room and record the pairing.

    Returns the shared room name.
    """
    # Create a unique room name based on Profile IDs (always consistent).
    room_name = f"room_{min(profile.id, match.id)}_{max(profile.id, match.id)}"

    # Record the pairing for the "reconnect with last user" features.
    profile.last_connected_session = _session_token(match)
    match.last_connected_session = _session_token(profile)
    if profile.session_id and match.session_id:
        lastConnected.objects.create(
            user_session_id=profile.session_id,
            last_user_session_id=match.session_id,
        )

    profile.active_room_name = room_name
    profile.status = Status.Busy
    match.active_room_name = room_name
    match.status = Status.Busy
    # Write only the columns that actually changed to keep the UPDATE small.
    profile.save(update_fields=[
        'active_room_name', 'status', 'last_connected_session',
    ])
    match.save(update_fields=[
        'active_room_name', 'status', 'last_connected_session',
    ])

    return room_name


def _find_last_partner_if_searching(profile):
    """Return the previous partner's profile only if they are searching again.

    Guests keep the same ``session_id`` across page loads, so even after their
    old profile row was cleaned up on disconnect, a returning guest can be found
    again by the session token stored in ``last_connected_session``. We only
    reconnect them when they are currently ``online`` (i.e. searching too).
    """
    token = profile.last_connected_session
    if not token:
        return None

    if token.startswith('user_'):
        try:
            partner = UserProfile.objects.filter(user_id=int(token[len('user_'):])).first()
        except ValueError:
            partner = None
    else:
        partner = UserProfile.objects.filter(session_id=token).first()

    if not partner or partner.id == profile.id:
        return None
    if partner.status != Status.Online:
        return None
    return partner


# How many online candidates to sample when picking a random partner. Keeping
# this small turns matchmaking into an index range-scan + tiny in-Python random
# pick instead of a full-table ``ORDER BY RANDOM()`` sort, which scales badly as
# the online pool grows.
_MATCH_SAMPLE_SIZE = 25


def _apply_premium_filters(queryset, profile):
    """Narrow the candidate pool by the premium member's preferences.

    Each filter is applied only when it still leaves at least one candidate, so
    a premium member is never stranded with zero matches. ``.exists()`` is used
    (cheap ``LIMIT 1``) rather than evaluating the whole queryset.
    """
    if profile.pref_sex and profile.pref_sex != 'any':
        filtered = queryset.filter(sex=profile.pref_sex)
        if filtered.exists():
            queryset = filtered
    if profile.pref_profession and profile.pref_profession != 'any':
        filtered = queryset.filter(profession=profile.pref_profession)
        if filtered.exists():
            queryset = filtered
    if profile.pref_age_min and profile.pref_age_min > 0:
        # "Age greater than": the candidate's age must meet the minimum.
        # Candidates with no age recorded are excluded by age__gte.
        filtered = queryset.filter(age__gte=profile.pref_age_min)
        if filtered.exists():
            queryset = filtered
    return queryset


def _find_and_pair_match(profile, prefer_last_partner=False):
    """Mark the profile online, find a partner, and pair both into a room.

    When ``prefer_last_partner`` is set, the previous partner is chosen first if
    they are also searching again; otherwise a random online user is picked.

    Concurrency: the whole "pick a candidate and claim it" step runs inside a
    single DB transaction and locks the chosen rows with ``select_for_update``.
    ``skip_locked`` means two users searching at the same time never fight over
    (or block on) the same candidate - each simply skips rows another matcher is
    already claiming. This removes the previous race where two people could both
    match the same partner.

    Returns the matched ``UserProfile`` and the shared room name, or
    ``(None, None)`` when nobody is currently available.
    """
    # Mark current user as searching (only the one column needs writing).
    profile.status = Status.Online
    profile.save(update_fields=['status'])

    # Prefer the last partner when asked and they are searching again.
    if prefer_last_partner:
        last_partner = _find_last_partner_if_searching(profile)
        if last_partner:
            room_name = _pair_profiles(profile, last_partner)
            return last_partner, room_name

    with transaction.atomic():
        # Base pool: everyone currently online except this user.
        candidates = (
            UserProfile.objects
            .filter(status=Status.Online)
            .exclude(id=profile.id)
        )

        if profile.premium_active:
            candidates = _apply_premium_filters(candidates, profile)

        # Pull a bounded window of candidate ids and pick one at random in
        # Python. This avoids ``ORDER BY RANDOM()`` over the full table while
        # still giving a non-deterministic partner. ``order_by('id')`` keeps the
        # range scan on the primary-key / status index.
        candidate_ids = list(
            candidates.order_by('id').values_list('id', flat=True)[:_MATCH_SAMPLE_SIZE]
        )
        if not candidate_ids:
            return None, None

        chosen_id = random.choice(candidate_ids)

        # Re-fetch the chosen row and claim it atomically. On backends that
        # support row locking (Postgres) we use ``select_for_update`` with
        # ``skip_locked`` so two users searching simultaneously never grab the
        # same partner and never block on each other. SQLite (used in dev/tests)
        # doesn't support this and serialises writes anyway, so we fall back to
        # a plain locked-by-transaction read there.
        match_qs = UserProfile.objects.filter(id=chosen_id, status=Status.Online)
        if connection.features.has_select_for_update:
            match_qs = match_qs.select_for_update(
                skip_locked=connection.features.has_select_for_update_skip_locked,
            )
        match = match_qs.first()
        if not match:
            # Someone claimed it between the sample and the lock - treat as no
            # match this round; the caller's polling will retry.
            return None, None

        room_name = _pair_profiles(profile, match)

    return match, room_name


def _call_timer(profile):
    """Call-duration limit (seconds). Premium gets effectively unlimited."""
    return 999999 if profile.premium_active else 300


def match_user(request, mode, name):
    profile = get_user_profile(request)

    # --- Activity logging: a call attempt (text or voice depending on mode). ---
    _action = Action.VOICE_CALL_ATTEMPT if mode == 'voice' else Action.MSG_CALL_ATTEMPT
    log_event(_action, request=request, profile=profile, mode=mode, kind='match')

    # Lapse expired premium / reset daily counters before matching.
    profile.reset_daily_counters_if_needed()

    # Already in an active room -> rejoin it.
    if profile.status == Status.Busy and profile.active_room_name:
        active_partner = UserProfile.objects.filter(
            active_room_name=profile.active_room_name
        ).exclude(id=profile.id).first()
        active_partner_name = active_partner.display_name if active_partner else 'Anonymous'
        active_partner_profession = active_partner.profession if active_partner else 'any'
        return render(request, 'chat/call.html', {
            'room_name': profile.active_room_name,
            'match_name': active_partner_name,
            'profession': active_partner_profession,
            'mode': mode,
            'name': name,
            'timer': _call_timer(profile),
        })

    match, room_name = _find_and_pair_match(profile)

    if match:
        return render(request, 'chat/call.html', {
            'match': match,
            'match_name': match.display_name,
            'profession': match.profession,
            'mode': mode,
            'room_name': room_name,
            'name': profile.display_name,
            'timer': _call_timer(profile),
        })

    # If no match, stay on home but show a "Searching" state.
    return render(request, 'chat/home.html', {
        'profile': profile,
        'searching': True,
        'mode': mode,
        'name': profile.display_name,
    })


def reconnect_user(request, mode, name):
    """Reconnect to a new partner after the current one disconnected.

    Non-premium members are permitted a single reconnect per day; any further
    attempt redirects them to the premium upgrade page. Premium members get the
    higher reconnect allowance defined on the model.
    """
    profile = get_user_profile(request)

    # ``polling`` requests are the automatic retries from the searching overlay.
    # The reconnect allowance was already spent on the first request, so these
    # must not be counted or re-checked again - they only keep looking for the
    # previous partner (or any partner) to become available.
    is_polling = request.GET.get('polling') == '1'

    # --- Activity logging: reconnect = another call attempt. Skip automatic
    # polling retries so each record is a real user-initiated attempt. ---
    if not is_polling:
        _action = Action.VOICE_CALL_ATTEMPT if mode == 'voice' else Action.MSG_CALL_ATTEMPT
        log_event(_action, request=request, profile=profile, mode=mode, kind='reconnect')

    # Reset the daily reconnect counter at the start of a new calendar day so
    # the free allowance refreshes once per day.
    profile.reset_daily_counters_if_needed()

    # Release any stale room state before searching again.
    if profile.active_room_name:
        profile.active_room_name = None
    profile.status = Status.Online
    profile.save()

    if not is_polling:
        # Enforce the reconnect allowance. Free members may reconnect once; after
        # that they are formally invited to upgrade to premium.
        if not profile.premium_active and not profile.can_reconnect():
            messages.info(
                request,
                'You have reached the complimentary reconnect limit. '
                'Please upgrade to Premium to continue reconnecting with new participants.',
            )
            return redirect('premium_page')

        # Record this reconnect attempt against the user's daily allowance.
        profile.reconnect_count += 1
        profile.save(update_fields=['reconnect_count'])

    # Prefer the previous partner if they are searching again; otherwise fall
    # back to a fresh random match.
    match, room_name = _find_and_pair_match(profile, prefer_last_partner=True)

    if match:
        return render(request, 'chat/call.html', {
            'match': match,
            'match_name': match.display_name,
            'profession': match.profession,
            'mode': mode,
            'room_name': room_name,
            'name': profile.display_name,
            'timer': _call_timer(profile),
        })

    # Nobody is available right now -> keep polling through the reconnect path
    # so we continue to prefer the previous partner.
    return render(request, 'chat/home.html', {
        'profile': profile,
        'searching': True,
        'reconnecting': True,
        'mode': mode,
        'name': profile.display_name,
    })


def hangup_view(request):
    profile = get_user_profile(request)
    profile.clear_user_entry()
    return redirect('home')


@login_required
def connect_last_user(request):
    """Premium feature to reconnect with the last person."""
    profile = get_user_profile(request)
    profile.reset_daily_counters_if_needed()
    if not profile.premium_active or not profile.last_connected_session:
        return redirect('premium_page')

    last_user_profile = UserProfile.objects.filter(
        session_id=profile.last_connected_session
    ).first()
    if not last_user_profile:
        return redirect('home')

    room_name = f"room_{min(profile.id, last_user_profile.id)}_{max(profile.id, last_user_profile.id)}"
    return render(request, 'chat/call.html', {
        'match': last_user_profile,
        'match_name': last_user_profile.display_name,
        'profession': last_user_profile.profession,
        'room_name': room_name,
        'mode': 'chat',
        'name': profile.display_name,
        'timer': 999999,
    })


# --- WALLET & PREMIUM LOGIC ---

# Premium is a DAILY pass that expires at the next local midnight.
# Prices come from the model so the whole app stays consistent.
# Indian users pay ₹10/day in INR; everyone else pays $1/day in USD.
# Razorpay expects the amount in the currency's smallest unit:
#   INR -> paise (₹10  = 1000 paise)
#   USD -> cents  ($1   = 100 cents)
PREMIUM_PRICE_INR = PREMIUM_DAILY_PRICE_INR
PREMIUM_PRICE_PAISE = PREMIUM_PRICE_INR * 100
PREMIUM_PRICE_USD = PREMIUM_DAILY_PRICE_USD
PREMIUM_PRICE_CENTS = PREMIUM_PRICE_USD * 100


def _razorpay_client():
    if not (settings.RAZOR_KEY_ID and settings.RAZOR_KEY_SECRET):
        return None
    return razorpay.Client(auth=(settings.RAZOR_KEY_ID, settings.RAZOR_KEY_SECRET))


def _client_ip(request):
    """Best-effort client IP, honouring the first proxy hop if present."""
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if forwarded:
        # X-Forwarded-For is "client, proxy1, proxy2"; the first entry is the
        # original client.
        return forwarded.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '')


def _is_india_request(request):
    """Decide whether the request originates from India.

    Pricing differs by country, so we detect the user's country as cheaply and
    robustly as possible:

    1. Trust a country hint set by a CDN / proxy if present
       (Cloudflare's ``CF-IPCountry``, App Engine's ``X-AppEngine-Country``).
       These are the most reliable in production behind such a proxy.
    2. Otherwise fall back to a free IP-geolocation lookup.
    3. If detection fails for any reason, default to India (INR) so existing
       Indian users keep their familiar ₹10 price and the page never breaks.

    Returns ``True`` for India, ``False`` for every other country.
    """
    # 1. CDN / proxy supplied country code (ISO 3166-1 alpha-2).
    for header in ('HTTP_CF_IPCOUNTRY', 'HTTP_X_APPENGINE_COUNTRY'):
        country = (request.META.get(header) or '').strip().upper()
        if country and country not in ('XX', 'T1'):  # XX/T1 = unknown/Tor
            return country == 'IN'

    # 2. IP geolocation fallback. Private/loopback IPs (local dev) can't be
    #    resolved, so treat those as India to keep the default behaviour.
    ip = _client_ip(request)
    if not ip or ip.startswith(('127.', '10.', '192.168.', '::1')):
        return True

    try:
        import requests

        resp = requests.get(f'https://ipapi.co/{ip}/country/', timeout=2)
        if resp.ok:
            country = (resp.text or '').strip().upper()
            if len(country) == 2:
                return country == 'IN'
    except Exception as exc:  # noqa: BLE001 - never let geo lookup break payment
        logger.warning("Country detection failed for %s: %s", ip, exc)

    # 3. Safe default.
    return True


@login_required(login_url='login')
def premium_page(request):
    """Wallet and UPI/card payment page. Creates a server-side Razorpay order."""
    profile, _ = UserProfile.objects.get_or_create(user=request.user)

    # --- Activity logging: landing here means the user clicked "Premium". ---
    log_event(Action.PREMIUM_CLICK, request=request, profile=profile,
              already_premium=profile.premium_active)

    # Lapse any expired premium so the page reflects reality.
    profile.reset_daily_counters_if_needed()

    # Pick currency by country: India -> INR (₹10), everyone else -> USD ($1).
    is_india = _is_india_request(request)
    if is_india:
        currency = 'INR'
        amount_minor = PREMIUM_PRICE_PAISE   # paise
        display_price = PREMIUM_PRICE_INR
        display_symbol = '₹'
    else:
        currency = 'USD'
        amount_minor = PREMIUM_PRICE_CENTS   # cents
        display_price = PREMIUM_PRICE_USD
        display_symbol = '$'

    payment = None
    client = _razorpay_client()
    if client is not None and not profile.premium_active:
        try:
            # Create an order server-side. Amount is in the currency's smallest
            # unit (paise for INR, cents for USD).
            payment = client.order.create({
                'amount': amount_minor,
                'currency': currency,
                'payment_capture': '1',
            })
            # Record a pending transaction so we can reconcile the callback.
            Transaction.objects.create(
                user=request.user,
                amount=display_price,
                razorpay_order_id=payment['id'],
                status='Pending',
            )
        except Exception as exc:  # noqa: BLE001 - surface gateway errors gracefully
            logger.error("Razorpay order creation failed: %s", exc)
            payment = None

    return render(request, 'chat/payment.html', {
        'profile': profile,
        'payment': payment,
        'razorpay_key': settings.RAZOR_KEY_ID,
        'premium_price': display_price,
        'premium_symbol': display_symbol,
        'premium_currency': currency,
        'is_india': is_india,
        # Both prices for the dual "₹10 / $1 per day" messaging.
        'premium_price_inr': PREMIUM_PRICE_INR,
        'premium_price_usd': PREMIUM_PRICE_USD,
        'premium_active': profile.premium_active,
    })


@login_required
@require_POST
def payment_success(request):
    """Verify the Razorpay payment signature server-side before granting premium.

    NEVER trust the client: only upgrade the account once the signature
    computed with our secret matches what Razorpay returned.
    """
    order_id = request.POST.get('razorpay_order_id')
    payment_id = request.POST.get('razorpay_payment_id')
    signature = request.POST.get('razorpay_signature')

    client = _razorpay_client()
    if client is None or not (order_id and payment_id and signature):
        messages.error(request, 'Payment could not be verified. Please contact support.')
        return redirect('premium_page')

    try:
        client.utility.verify_payment_signature({
            'razorpay_order_id': order_id,
            'razorpay_payment_id': payment_id,
            'razorpay_signature': signature,
        })
    except razorpay.errors.SignatureVerificationError:
        logger.warning("Razorpay signature verification FAILED for order %s", order_id)
        # --- Activity logging: payment attempt failed verification. ---
        log_event(Action.PREMIUM_PAYMENT_FAILED, request=request,
                  order_id=order_id, reason='signature_verification_failed')
        # Mark the transaction as failed if we know about it.
        Transaction.objects.filter(razorpay_order_id=order_id).update(status='Failed')
        messages.error(request, 'Payment verification failed. You have not been charged for premium.')
        return redirect('premium_page')

    # Signature is valid -> grant premium exactly once for this order.
    txn = Transaction.objects.filter(razorpay_order_id=order_id).first()
    if txn and txn.status == 'Success':
        # Already processed (e.g. duplicate callback) - don't credit twice.
        return redirect('home')

    profile, _ = UserProfile.objects.get_or_create(user=request.user)
    # Grant premium until the next local midnight (a daily pass).
    profile.is_premium = True
    profile.premium_expiry = UserProfile.next_midnight()
    profile.save(update_fields=['is_premium', 'premium_expiry'])

    if txn:
        txn.razorpay_payment_id = payment_id
        txn.status = 'Success'
        txn.save()
    else:
        # No pending row to reconcile (shouldn't normally happen). Fall back to
        # the country-based price for this request so the recorded amount is
        # still correct for global vs Indian users.
        fallback_amount = (
            PREMIUM_PRICE_INR if _is_india_request(request) else PREMIUM_PRICE_USD
        )
        Transaction.objects.create(
            user=request.user,
            amount=fallback_amount,
            razorpay_order_id=order_id,
            razorpay_payment_id=payment_id,
            status='Success',
        )

    # --- Activity logging: a successful premium payment. ---
    log_event(Action.PREMIUM_PAID, request=request, profile=profile,
              order_id=order_id, payment_id=payment_id,
              amount=(txn.amount if txn else None))

    messages.success(request, 'Premium unlocked for today! Enjoy filtered, unlimited matching.')
    return redirect('home')


def _google_login_available():
    """True only if a Google SocialApp is configured, so the login page
    doesn't 500 when OAuth hasn't been set up yet."""
    try:
        from allauth.socialaccount.models import SocialApp
        return SocialApp.objects.filter(provider='google').exists()
    except Exception:  # noqa: BLE001
        return False


def custom_login(request):
    # If already logged in, don't show the page.
    if request.user.is_authenticated:
        return redirect('home')

    # Email OTP is the active login method. Mobile OTP is temporarily disabled
    # (see the commented-out mobile flow below) and Google remains available.
    return render(request, 'chat/login.html', {
        'google_available': _google_login_available(),
    })


# --- Email OTP login (active) -------------------------------------------------

# How long an emailed OTP stays valid, in seconds. Email can take a minute or
# two to arrive, so give the user a realistic 10-minute window to enter it.
EMAIL_OTP_TTL_SECONDS = 600


def send_email_otp(request):
    """Generate a one-time code and email it to the user.

    Reuses the same session + ``login()`` pattern as the old mobile flow so the
    rest of the app (matchmaking, calling, premium) keeps working unchanged.
    Uses Django's configured email backend: console in dev, SMTP in prod.
    """
    if request.method != 'POST':
        return redirect('login')

    email = (request.POST.get('email') or '').strip().lower()

    # Validate the address before sending anything.
    try:
        validate_email(email)
    except ValidationError:
        return render(request, 'chat/login.html', {
            'google_available': _google_login_available(),
            'error': 'Please enter a valid email address.',
        })

    otp = str(random.randint(100000, 999999))

    # Save OTP, email, and the issue time in the session (the verify step reads
    # these). The timestamp is used to enforce the expiry window.
    request.session['email_otp'] = otp
    request.session['email'] = email
    request.session['email_otp_ts'] = time.time()
    # Force the session to be written now and ensure a session cookie exists, so
    # the OTP reliably survives the round-trip to the verify request. Without an
    # explicit save, an anonymous session created only in-memory here can be
    # lost before verify runs, which makes a correct code look "incorrect".
    request.session.modified = True
    request.session.save()

    # Always log the OTP to the server console so a developer can read it even if
    # the recipient's inbox is slow, the code lands in spam, or SMTP silently
    # drops it. This log line is the ground truth for debugging delivery.
    logger.info("Email OTP for %s is %s (backend=%s)", email, otp, settings.EMAIL_BACKEND)

    # Deliver the code. If sending fails (e.g. SMTP misconfigured), fall back to
    # logging it so development logins still work.
    sent = True
    try:
        delivered = send_mail(
            subject='Your SpeakFluent verification code',
            message=f'Your SpeakFluent verification code is {otp}. It expires shortly.',
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[email],
            fail_silently=False,
        )
        # send_mail returns the number of messages delivered. If it reports 0 the
        # backend accepted the call but sent nothing, so treat that as a failure.
        if not delivered:
            logger.error("Email OTP to %s reported 0 messages delivered", email)
            sent = False
        else:
            logger.info("Email OTP to %s dispatched via SMTP", email)
    except Exception as exc:  # noqa: BLE001 - don't block login if email fails
        logger.error("Email OTP send failed for %s: %s", email, exc)
        logger.info("OTP for %s is %s (email send failed)", email, otp)
        sent = False

    return render(request, 'chat/verify_otp.html', {
        'email': email,
        'email_sent': sent,
    })


def verify_email_otp(request):
    if request.method == 'POST':
        # Normalize the entered code: keep digits only so spaces, dashes, or a
        # stray trailing newline from autofill don't cause a false mismatch.
        raw_input = request.POST.get('otp') or ''
        user_otp = ''.join(ch for ch in raw_input if ch.isdigit())
        saved_otp = request.session.get('email_otp')
        email = request.session.get('email')
        issued_at = request.session.get('email_otp_ts')

        logger.info(
            "Verify OTP: entered=%r saved=%r email=%r age=%ss",
            user_otp,
            saved_otp,
            email,
            None if issued_at is None else round(time.time() - issued_at, 1),
        )

        # If there's no OTP in the session, the code was never issued in this
        # session or the session was lost. Tell the user to request a new code
        # rather than showing a misleading "invalid code" message.
        if not saved_otp or not email:
            return render(request, 'chat/verify_otp.html', {
                'error': 'Your session expired. Please request a new code.',
                'email': email,
                'expired': True,
            })

        # Reject expired codes. If the OTP is older than the TTL, clear it so a
        # stale code can't be reused and ask the user to request a new one.
        is_expired = (
            issued_at is None
            or (time.time() - issued_at) > EMAIL_OTP_TTL_SECONDS
        )
        if is_expired:
            request.session.pop('email_otp', None)
            request.session.pop('email_otp_ts', None)
            return render(request, 'chat/verify_otp.html', {
                'error': 'This code has expired. Please request a new one.',
                'email': email,
                'expired': True,
            })

        if user_otp == saved_otp:
            # Get or create a user based on the email address. The username is
            # namespaced so it never collides with other login methods.
            username = f"email_{email}"
            # Derive a friendly display name from the local part of the email
            # (the text before "@"), e.g. "john.doe123@gmail.com" -> "John Doe".
            display_name = name_from_email(email)
            user, created = User.objects.get_or_create(
                username=username,
                defaults={'email': email, 'first_name': display_name},
            )
            fields_to_update = []
            if not user.email:
                user.email = email
                fields_to_update.append('email')
            # Backfill the name for users created before this feature existed.
            if not user.first_name:
                user.first_name = display_name
                fields_to_update.append('first_name')
            if fields_to_update:
                user.save(update_fields=fields_to_update)

            # Keep the matchmaking display name in sync with the email-derived
            # name so the "connecting" screens show the real name by default.
            profile, _ = UserProfile.objects.get_or_create(user=user)
            if not profile.display_name or profile.display_name == 'Anonymous':
                profile.display_name = display_name
                profile.save(update_fields=['display_name'])

            login(request, user, backend='django.contrib.auth.backends.ModelBackend')

            # Cleanup session.
            request.session.pop('email_otp', None)
            request.session.pop('email', None)
            request.session.pop('email_otp_ts', None)
            return redirect('home')

        logger.warning("OTP mismatch for %s: entered=%r expected=%r", email, user_otp, saved_otp)
        return render(request, 'chat/verify_otp.html', {
            'error': 'That code is incorrect. Please check and try again.',
            'email': email,
        })

    return redirect('login')


# --- Mobile OTP login (temporarily disabled) ---------------------------------
# The phone/SMS OTP flow is paused for now. Email OTP (above) is the active free
# login method. Kept here so it can be re-enabled later without rewriting it.
#
# def _send_sms_otp(mobile, otp):
#     """Send the OTP over SMS via Twilio.
#
#     Returns True if the SMS was dispatched. Returns False when Twilio is not
#     configured, so the caller can fall back to logging the OTP in development.
#     """
#     if not (settings.TWILIO_SID and settings.TWILIO_TOKEN and settings.TWILIO_FROM):
#         return False
#     try:
#         from twilio.rest import Client
#
#         client = Client(settings.TWILIO_SID, settings.TWILIO_TOKEN)
#         client.messages.create(
#             body=f"Your Anonymous Connect verification code is {otp}.",
#             from_=settings.TWILIO_FROM,
#             to=mobile,
#         )
#         return True
#     except Exception as exc:  # noqa: BLE001 - don't block login if SMS fails
#         logger.error("Twilio OTP send failed for %s: %s", mobile, exc)
#         return False
#
#
# def send_otp(request):
#     if request.method == 'POST':
#         mobile = request.POST.get('mobile_no')
#         if not mobile:
#             return render(request, 'chat/login.html', {
#                 'google_available': _google_login_available(),
#                 'error': 'Please enter a valid mobile number.',
#             })
#
#         otp = str(random.randint(100000, 999999))
#
#         # Save OTP and Mobile in session (verify step reads these).
#         request.session['otp'] = otp
#         request.session['mobile'] = mobile
#
#         # Try to deliver via Twilio; fall back to logging in dev when Twilio
#         # credentials are not configured.
#         sent = _send_sms_otp(mobile, otp)
#         if not sent:
#             logger.info("OTP for %s is %s (SMS gateway not configured)", mobile, otp)
#
#         return render(request, 'chat/verify_otp.html', {
#             'mobile': mobile,
#             'sms_sent': sent,
#         })
#     return redirect('login')
#
#
# def verify_otp(request):
#     if request.method == 'POST':
#         user_otp = request.POST.get('otp')
#         saved_otp = request.session.get('otp')
#         mobile = request.session.get('mobile')
#
#         if saved_otp and user_otp == saved_otp and mobile:
#             # Get or create a user based on the mobile number.
#             username = f"user_{mobile}"
#             user, _ = User.objects.get_or_create(username=username)
#             login(request, user, backend='django.contrib.auth.backends.ModelBackend')
#
#             # Cleanup session.
#             request.session.pop('otp', None)
#             request.session.pop('mobile', None)
#             return redirect('home')
#
#         return render(request, 'chat/verify_otp.html', {'error': 'Invalid OTP', 'mobile': mobile})
#
#     return redirect('login')


# --- Health check ---

def healthz(request):
    """Lightweight health/readiness probe for cloud platforms and load balancers.

    Verifies the database connection is reachable. Returns 200 when healthy.
    """
    from django.db import connection
    from django.http import JsonResponse

    try:
        connection.ensure_connection()
        db_ok = True
    except Exception as exc:  # noqa: BLE001
        logger.error("Health check DB failure: %s", exc)
        db_ok = False

    status_code = 200 if db_ok else 503
    return JsonResponse({'status': 'ok' if db_ok else 'unhealthy', 'database': db_ok}, status=status_code)


# --- WebRTC TURN credentials ---

# Fallback used when Cloudflare TURN isn't configured: STUN only. STUN works for
# peers on permissive networks but fails behind symmetric NAT / strict
# firewalls, which is exactly why we prefer TURN in production.
_STUN_ONLY_ICE_SERVERS = [{'urls': ['stun:stun.l.google.com:19302']}]


def ice_servers(request):
    """Return short-lived WebRTC ICE servers (STUN + Cloudflare TURN) as JSON.

    The TURN key ID and API token are long-term secrets that MUST stay on the
    server. We call Cloudflare's Realtime API here to mint a short-lived TURN
    credential for this specific call, then hand the resulting ``iceServers``
    list to the browser. If TURN isn't configured or the API call fails, we
    gracefully fall back to STUN-only so calls still work on good networks.
    """
    from django.http import JsonResponse

    key_id = getattr(settings, 'CLOUDFLARE_TURN_KEY_ID', '')
    api_token = getattr(settings, 'CLOUDFLARE_TURN_API_TOKEN', '')
    ttl = getattr(settings, 'CLOUDFLARE_TURN_TTL', 86400)

    # No TURN configured -> STUN only.
    if not (key_id and api_token):
        return JsonResponse({'iceServers': _STUN_ONLY_ICE_SERVERS})

    try:
        import requests

        resp = requests.post(
            f'https://rtc.live.cloudflare.com/v1/turn/keys/{key_id}/credentials/generate-ice-servers',
            headers={
                'Authorization': f'Bearer {api_token}',
                'Content-Type': 'application/json',
            },
            json={'ttl': ttl},
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()
        ice = data.get('iceServers')
        if ice:
            # Cloudflare may return a single object or a list; normalise to list.
            if isinstance(ice, dict):
                ice = [ice]
            return JsonResponse({'iceServers': ice})
        logger.error("Cloudflare TURN response missing iceServers: %s", data)
    except Exception as exc:  # noqa: BLE001 - never let TURN break the call page
        logger.error("Cloudflare TURN credential generation failed: %s", exc)

    # Any failure -> safe STUN-only fallback.
    return JsonResponse({'iceServers': _STUN_ONLY_ICE_SERVERS})


# --- Legal / policy pages (required for Razorpay activation) ---
# These are static informational pages. Their links live in the shared footer
# (chat/_policy_footer.html) so every public page exposes them, which is what
# the Razorpay activation review checks for.

def privacy_policy(request):
    return render(request, 'chat/legal/privacy_policy.html')


def terms_conditions(request):
    return render(request, 'chat/legal/terms_conditions.html')


def refund_policy(request):
    return render(request, 'chat/legal/refund_policy.html')


def contact_us(request):
    return render(request, 'chat/legal/contact_us.html')


def service_fulfillment(request):
    return render(request, 'chat/legal/service_fulfillment.html')
