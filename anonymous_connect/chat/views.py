import logging
import random

import razorpay
from django.shortcuts import render, redirect
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required
from django.conf import settings
from django.views.decorators.http import require_POST

from .models import UserProfile, lastConnected, Status, Transaction

logger = logging.getLogger(__name__)


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

        if profile.is_premium:
            profile.pref_sex = request.POST.get('pref_sex', 'any')
            profile.pref_profession = request.POST.get('pref_profession', 'any')

        profile.save()

        # Check if the user clicked one of the "Connect" buttons
        mode = request.POST.get('connection_mode')
        if mode in ['chat', 'voice']:
            return redirect('match_user', mode=mode, name=profile.display_name)

    return render(request, 'chat/home.html', {'profile': profile})


def _find_and_pair_match(profile):
    """Mark the profile online, find a random partner, and pair both into a room.

    Returns the matched ``UserProfile`` and the shared room name, or
    ``(None, None)`` when nobody is currently available.
    """
    # Mark current user as searching.
    profile.status = Status.Online
    profile.save()

    # Look for others who are online. Exclude the current user.
    potential_matches = UserProfile.objects.filter(status=Status.Online).exclude(id=profile.id)

    if profile.is_premium:
        base_pool = potential_matches
        if profile.pref_sex != 'any':
            filtered = base_pool.filter(sex=profile.pref_sex)
            if filtered.exists():
                potential_matches = filtered
        if profile.pref_profession != 'any':
            filtered = potential_matches.filter(profession=profile.pref_profession)
            if filtered.exists():
                potential_matches = filtered

    match = potential_matches.order_by('?').first()
    if not match:
        return None, None

    # Create a unique room name based on Profile IDs (always consistent).
    room_name = f"room_{min(profile.id, match.id)}_{max(profile.id, match.id)}"

    # Record the pairing for the premium "Connect Last User" feature.
    profile.last_connected_session = match.session_id or (
        f"user_{match.user_id}" if match.user_id else None
    )
    match.last_connected_session = profile.session_id or (
        f"user_{profile.user_id}" if profile.user_id else None
    )
    if profile.session_id and match.session_id:
        lastConnected.objects.create(
            user_session_id=profile.session_id,
            last_user_session_id=match.session_id,
        )

    profile.active_room_name = room_name
    profile.status = Status.Busy
    match.active_room_name = room_name
    match.status = Status.Busy
    profile.save()
    match.save()

    return match, room_name


def match_user(request, mode, name):
    profile = get_user_profile(request)

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
            'timer': 300 if not profile.is_premium else 999999,
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
            'timer': 300 if not profile.is_premium else 999999,
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

    # Reset the daily reconnect counter at the start of a new calendar day so
    # the free allowance refreshes once per day.
    profile.reset_daily_counters_if_needed()

    # The previous partner has already left, so release any stale room state
    # before searching for a fresh match.
    if profile.active_room_name:
        profile.active_room_name = None
    profile.status = Status.Online
    profile.save()

    # Enforce the reconnect allowance. Free members may reconnect once; after
    # that they are formally invited to upgrade to premium.
    if not profile.is_premium and not profile.can_reconnect():
        messages.info(
            request,
            'You have reached the complimentary reconnect limit. '
            'Please upgrade to Premium to continue reconnecting with new participants.',
        )
        return redirect('premium_page')

    # Record this reconnect attempt against the user's daily allowance.
    profile.reconnect_count += 1
    profile.save(update_fields=['reconnect_count'])

    match, room_name = _find_and_pair_match(profile)

    if match:
        return render(request, 'chat/call.html', {
            'match': match,
            'match_name': match.display_name,
            'profession': match.profession,
            'mode': mode,
            'room_name': room_name,
            'name': profile.display_name,
            'timer': 300 if not profile.is_premium else 999999,
        })

    # Nobody is available right now -> fall into the searching state.
    return render(request, 'chat/home.html', {
        'profile': profile,
        'searching': True,
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
    if not profile.is_premium or not profile.last_connected_session:
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

# Premium membership price in INR. Razorpay expects the amount in paise.
PREMIUM_PRICE_INR = 500
PREMIUM_PRICE_PAISE = PREMIUM_PRICE_INR * 100


def _razorpay_client():
    if not (settings.RAZOR_KEY_ID and settings.RAZOR_KEY_SECRET):
        return None
    return razorpay.Client(auth=(settings.RAZOR_KEY_ID, settings.RAZOR_KEY_SECRET))


@login_required(login_url='login')
def premium_page(request):
    """Wallet and UPI/card payment page. Creates a server-side Razorpay order."""
    profile, _ = UserProfile.objects.get_or_create(user=request.user)

    payment = None
    client = _razorpay_client()
    if client is not None and not profile.is_premium:
        try:
            # Create an order server-side. Amount is in paise.
            payment = client.order.create({
                'amount': PREMIUM_PRICE_PAISE,
                'currency': 'INR',
                'payment_capture': '1',
            })
            # Record a pending transaction so we can reconcile the callback.
            Transaction.objects.create(
                user=request.user,
                amount=PREMIUM_PRICE_INR,
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
    profile.is_premium = True
    profile.wallet_balance += PREMIUM_PRICE_INR
    profile.save()

    if txn:
        txn.razorpay_payment_id = payment_id
        txn.status = 'Success'
        txn.save()
    else:
        Transaction.objects.create(
            user=request.user,
            amount=PREMIUM_PRICE_INR,
            razorpay_order_id=order_id,
            razorpay_payment_id=payment_id,
            status='Success',
        )

    messages.success(request, 'Welcome to Premium! Enjoy unlimited matching.')
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

    if request.method == 'POST':
        auth_method = request.POST.get('auth_method')  # 'mobile' or 'email'

        if auth_method == 'mobile':
            mobile_no = request.POST.get('mobile_no')
            return render(request, 'chat/verify_otp.html', {'mobile': mobile_no})

        elif auth_method == 'email':
            if _google_login_available():
                return redirect('/accounts/google/login/')
            return render(request, 'chat/login.html', {
                'google_available': False,
                'error': 'Google login is not configured yet.',
            })

    return render(request, 'chat/login.html', {
        'google_available': _google_login_available(),
    })


def send_otp(request):
    if request.method == 'POST':
        mobile = request.POST.get('mobile_no')
        otp = str(random.randint(100000, 999999))

        # Save OTP and Mobile in session (verify step reads these).
        request.session['otp'] = otp
        request.session['mobile'] = mobile

        # Integrate an SMS gateway here (e.g., Twilio). For now, log for dev.
        logger.info("OTP for %s is %s", mobile, otp)

        return render(request, 'chat/verify_otp.html', {'mobile': mobile})
    return redirect('login')


def verify_otp(request):
    if request.method == 'POST':
        user_otp = request.POST.get('otp')
        saved_otp = request.session.get('otp')
        mobile = request.session.get('mobile')

        if saved_otp and user_otp == saved_otp and mobile:
            # Get or create a user based on the mobile number.
            username = f"user_{mobile}"
            user, _ = User.objects.get_or_create(username=username)
            login(request, user, backend='django.contrib.auth.backends.ModelBackend')

            # Cleanup session.
            request.session.pop('otp', None)
            request.session.pop('mobile', None)
            return redirect('home')

        return render(request, 'chat/verify_otp.html', {'error': 'Invalid OTP', 'mobile': mobile})

    return redirect('login')


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
