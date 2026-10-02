"""
Unit and integration tests for the Anonymous Connect chat app.

Covers:
  * UserProfile model (helpers, premium/reconnect logic, signals)
  * lastConnected and Transaction models
  * get_user_profile helper (guest vs authenticated)
  * home / matchmaking views
  * hangup view
  * connect_last_user (premium gating)
  * premium_page + payment_success (Razorpay mocked, signature verification)
  * login / send_otp / verify_otp (OTP auth flow)
  * healthz health check
  * ChatConsumer WebSocket (chat relay, WebRTC signalling, call-ended)

Razorpay is always mocked so tests never touch the network.
"""
from datetime import timedelta
from unittest import mock

from django.test import TestCase, Client, override_settings
from django.urls import reverse
from django.utils import timezone
from django.contrib.auth.models import User

from .models import (
    UserProfile,
    lastConnected,
    Transaction,
    Status,
    PREMIUM_RECONNECT_LIMIT,
    FREE_RECONNECT_LIMIT,
)


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------

class UserProfileModelTests(TestCase):
    def test_signal_creates_profile_for_new_user(self):
        """A profile is auto-created via post_save signal when a User is made."""
        user = User.objects.create_user(username="alice", password="pw12345!")
        self.assertTrue(UserProfile.objects.filter(user=user).exists())
        self.assertEqual(user.profile.display_name, "Anonymous")

    def test_str_representation(self):
        profile = UserProfile.objects.create(session_id="sess-str", display_name="Bob")
        self.assertEqual(str(profile), "Bob's Profile")

    def test_default_status_is_offline(self):
        profile = UserProfile.objects.create(session_id="sess-default")
        self.assertEqual(profile.status, Status.Offline)

    def test_clear_user_entry_deletes_anonymous_profile(self):
        """Guest (session-only) profiles are removed entirely on hang up."""
        profile = UserProfile.objects.create(session_id="guest-1", status=Status.Busy)
        profile.clear_user_entry()
        self.assertFalse(UserProfile.objects.filter(session_id="guest-1").exists())

    def test_clear_user_entry_resets_registered_profile(self):
        """Registered users keep their row but live state is reset."""
        user = User.objects.create_user(username="carol", password="pw12345!")
        profile = user.profile
        profile.status = Status.Busy
        profile.active_room_name = "room_1_2"
        profile.save()

        profile.clear_user_entry()
        profile.refresh_from_db()
        self.assertEqual(profile.status, Status.Offline)
        self.assertIsNone(profile.active_room_name)
        self.assertTrue(UserProfile.objects.filter(user=user).exists())


class PremiumAndReconnectLogicTests(TestCase):
    def setUp(self):
        self.profile = UserProfile.objects.create(session_id="prem-1")

    def test_premium_active_false_when_not_premium(self):
        self.assertFalse(self.profile.premium_active)

    def test_premium_active_false_when_no_expiry(self):
        self.profile.is_premium = True
        self.profile.premium_expiry = None
        self.assertFalse(self.profile.premium_active)

    def test_premium_active_false_when_expired(self):
        self.profile.is_premium = True
        self.profile.premium_expiry = timezone.now() - timedelta(hours=1)
        self.assertFalse(self.profile.premium_active)

    def test_premium_active_true_when_valid(self):
        self.profile.is_premium = True
        self.profile.premium_expiry = timezone.now() + timedelta(hours=1)
        self.assertTrue(self.profile.premium_active)

    def test_reconnect_limit_depends_on_premium(self):
        self.assertEqual(self.profile.reconnect_limit, FREE_RECONNECT_LIMIT)
        self.profile.is_premium = True
        self.profile.premium_expiry = timezone.now() + timedelta(hours=1)
        self.assertEqual(self.profile.reconnect_limit, PREMIUM_RECONNECT_LIMIT)

    def test_can_reconnect_respects_limit(self):
        self.profile.reconnect_count = FREE_RECONNECT_LIMIT
        self.assertFalse(self.profile.can_reconnect())
        self.profile.reconnect_count = 0
        self.assertTrue(self.profile.can_reconnect())

    def test_reset_daily_counters_resets_on_new_day(self):
        self.profile.reconnect_count = 3
        self.profile.last_reset_date = timezone.localdate() - timedelta(days=1)
        self.profile.save()

        changed = self.profile.reset_daily_counters_if_needed()
        self.profile.refresh_from_db()
        self.assertTrue(changed)
        self.assertEqual(self.profile.reconnect_count, 0)
        self.assertEqual(self.profile.last_reset_date, timezone.localdate())

    def test_reset_daily_counters_noop_same_day(self):
        self.profile.reconnect_count = 2
        self.profile.last_reset_date = timezone.localdate()
        self.profile.save()

        changed = self.profile.reset_daily_counters_if_needed()
        self.assertFalse(changed)
        self.assertEqual(self.profile.reconnect_count, 2)

    def test_reset_lapses_expired_premium(self):
        self.profile.is_premium = True
        self.profile.premium_expiry = timezone.now() - timedelta(minutes=1)
        self.profile.last_reset_date = timezone.localdate()
        self.profile.save()

        self.profile.reset_daily_counters_if_needed()
        self.profile.refresh_from_db()
        self.assertFalse(self.profile.is_premium)
        self.assertIsNone(self.profile.premium_expiry)

    def test_next_midnight_is_future_and_aware(self):
        nm = UserProfile.next_midnight()
        self.assertTrue(timezone.is_aware(nm))
        self.assertGreater(nm, timezone.now())


class OtherModelTests(TestCase):
    def test_last_connected_str(self):
        rec = lastConnected.objects.create(user_session_id="a", last_user_session_id="b")
        self.assertEqual(str(rec), "a -> b")

    def test_transaction_str(self):
        user = User.objects.create_user(username="dave", password="pw12345!")
        txn = Transaction.objects.create(user=user, amount=500, status="Success")
        self.assertIn("dave", str(txn))
        self.assertIn("Success", str(txn))


# ---------------------------------------------------------------------------
# View tests: home / matchmaking
# ---------------------------------------------------------------------------

class HomeViewTests(TestCase):
    def setUp(self):
        self.client = Client()

    def test_home_get_creates_guest_profile(self):
        resp = self.client.get(reverse('home'))
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, 'chat/home.html')
        # A guest profile should now exist for this session.
        self.assertEqual(UserProfile.objects.count(), 1)

    def test_home_post_saves_preferences_and_redirects_to_match(self):
        resp = self.client.post(reverse('home'), {
            'display_name': 'Tester',
            'age': '27',
            'sex': 'male',
            'profession': 'engineer',
            'connection_mode': 'chat',
        })
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/match/chat/Tester/', resp.url)
        profile = UserProfile.objects.get(display_name='Tester')
        self.assertEqual(profile.age, 27)
        self.assertEqual(profile.sex, 'male')
        self.assertEqual(profile.profession, 'engineer')

    def test_home_post_invalid_age_stored_as_none(self):
        self.client.post(reverse('home'), {
            'display_name': 'NoAge',
            'age': 'notanumber',
            'connection_mode': '',
        })
        profile = UserProfile.objects.get(display_name='NoAge')
        self.assertIsNone(profile.age)

    def test_home_post_without_mode_stays_on_home(self):
        resp = self.client.post(reverse('home'), {
            'display_name': 'Stayer',
            'age': '30',
        })
        self.assertEqual(resp.status_code, 200)


class MatchmakingViewTests(TestCase):
    def test_match_with_no_one_shows_searching(self):
        client = Client()
        resp = client.get(reverse('match_user', args=['chat', 'Solo']))
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, 'chat/home.html')
        self.assertTrue(resp.context['searching'])

    def test_two_guests_get_matched_into_same_room(self):
        # First guest searches and waits (status online).
        c1 = Client()
        c1.get(reverse('match_user', args=['chat', 'One']))
        p1 = UserProfile.objects.get(status=Status.Online)
        self.assertEqual(p1.status, Status.Online)

        # Second guest searches -> should match with the first.
        c2 = Client()
        resp2 = c2.get(reverse('match_user', args=['chat', 'Two']))
        self.assertEqual(resp2.status_code, 200)
        self.assertTemplateUsed(resp2, 'chat/call.html')

        p1.refresh_from_db()
        matched = UserProfile.objects.exclude(id=p1.id).first()
        self.assertEqual(p1.status, Status.Busy)
        self.assertEqual(matched.status, Status.Busy)
        self.assertEqual(p1.active_room_name, matched.active_room_name)
        self.assertTrue(p1.active_room_name.startswith('room_'))

    def test_busy_user_rejoins_existing_room(self):
        client = Client()
        # Put the guest in a busy state with an active room.
        client.get(reverse('home'))  # create session + profile
        profile = UserProfile.objects.first()
        profile.status = Status.Busy
        profile.active_room_name = 'room_9_9'
        profile.save()

        resp = client.get(reverse('match_user', args=['voice', 'Rejoin']))
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, 'chat/call.html')
        self.assertEqual(resp.context['room_name'], 'room_9_9')


class HangupViewTests(TestCase):
    def test_hangup_clears_guest_and_redirects_home(self):
        client = Client()
        client.get(reverse('home'))
        self.assertEqual(UserProfile.objects.count(), 1)

        resp = client.get(reverse('hangup_view'))
        self.assertRedirects(resp, reverse('home'), fetch_redirect_response=False)
        # Guest profile is deleted on hangup.
        self.assertEqual(UserProfile.objects.count(), 0)


# ---------------------------------------------------------------------------
# View tests: connect_last_user (premium gating)
# ---------------------------------------------------------------------------

class ConnectLastUserTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username="premi", password="pw12345!")

    def test_requires_login(self):
        resp = self.client.get(reverse('connect_last_user'))
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login', resp.url)

    def test_non_premium_redirected_to_premium_page(self):
        self.client.force_login(self.user)
        resp = self.client.get(reverse('connect_last_user'))
        self.assertRedirects(resp, reverse('premium_page'), fetch_redirect_response=False)

    def test_premium_without_last_session_redirected_to_premium(self):
        self.client.force_login(self.user)
        p = self.user.profile
        p.is_premium = True
        p.last_connected_session = None
        p.save()
        resp = self.client.get(reverse('connect_last_user'))
        self.assertRedirects(resp, reverse('premium_page'), fetch_redirect_response=False)

    def test_premium_with_missing_partner_redirects_home(self):
        self.client.force_login(self.user)
        p = self.user.profile
        p.is_premium = True
        p.last_connected_session = 'nonexistent-session'
        p.save()
        resp = self.client.get(reverse('connect_last_user'))
        self.assertRedirects(resp, reverse('home'), fetch_redirect_response=False)

    def test_premium_with_valid_partner_renders_call(self):
        self.client.force_login(self.user)
        partner = UserProfile.objects.create(session_id='partner-xyz', display_name='Partner')
        p = self.user.profile
        p.is_premium = True
        p.last_connected_session = 'partner-xyz'
        p.save()
        resp = self.client.get(reverse('connect_last_user'))
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, 'chat/call.html')
        self.assertEqual(resp.context['match_name'], 'Partner')


# ---------------------------------------------------------------------------
# View tests: premium / payment (Razorpay mocked)
# ---------------------------------------------------------------------------

@override_settings(RAZOR_KEY_ID='rzp_test_key', RAZOR_KEY_SECRET='secret')
class PremiumPageTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username="payer", password="pw12345!")

    def test_premium_page_requires_login(self):
        resp = self.client.get(reverse('premium_page'))
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login', resp.url)

    @mock.patch('chat.views.razorpay.Client')
    def test_premium_page_creates_order_and_pending_txn(self, mock_client_cls):
        mock_client = mock_client_cls.return_value
        mock_client.order.create.return_value = {'id': 'order_ABC', 'amount': 50000, 'currency': 'INR'}

        self.client.force_login(self.user)
        resp = self.client.get(reverse('premium_page'))

        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, 'chat/payment.html')
        mock_client.order.create.assert_called_once()
        txn = Transaction.objects.get(razorpay_order_id='order_ABC')
        self.assertEqual(txn.status, 'Pending')
        self.assertEqual(txn.user, self.user)

    @mock.patch('chat.views.razorpay.Client')
    def test_premium_page_no_order_when_already_premium(self, mock_client_cls):
        self.client.force_login(self.user)
        p = self.user.profile
        p.is_premium = True
        p.save()
        resp = self.client.get(reverse('premium_page'))
        self.assertEqual(resp.status_code, 200)
        mock_client_cls.return_value.order.create.assert_not_called()


@override_settings(RAZOR_KEY_ID='rzp_test_key', RAZOR_KEY_SECRET='secret')
class PaymentSuccessTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.user = User.objects.create_user(username="buyer", password="pw12345!")
        self.client.force_login(self.user)

    def test_rejects_get_request(self):
        resp = self.client.get(reverse('payment_success'))
        self.assertEqual(resp.status_code, 405)  # require_POST

    def test_missing_fields_redirects_to_premium(self):
        resp = self.client.post(reverse('payment_success'), {})
        self.assertRedirects(resp, reverse('premium_page'), fetch_redirect_response=False)
        self.user.profile.refresh_from_db()
        self.assertFalse(self.user.profile.is_premium)

    @mock.patch('chat.views.razorpay.Client')
    def test_valid_signature_grants_premium(self, mock_client_cls):
        mock_client = mock_client_cls.return_value
        mock_client.utility.verify_payment_signature.return_value = True
        Transaction.objects.create(
            user=self.user, amount=500, razorpay_order_id='order_1', status='Pending'
        )

        resp = self.client.post(reverse('payment_success'), {
            'razorpay_order_id': 'order_1',
            'razorpay_payment_id': 'pay_1',
            'razorpay_signature': 'sig_1',
        })
        self.assertRedirects(resp, reverse('home'), fetch_redirect_response=False)

        profile = self.user.profile
        profile.refresh_from_db()
        self.assertTrue(profile.is_premium)
        self.assertEqual(profile.wallet_balance, 500)
        txn = Transaction.objects.get(razorpay_order_id='order_1')
        self.assertEqual(txn.status, 'Success')
        self.assertEqual(txn.razorpay_payment_id, 'pay_1')

    @mock.patch('chat.views.razorpay.Client')
    def test_invalid_signature_denies_premium(self, mock_client_cls):
        import razorpay as razorpay_module
        mock_client = mock_client_cls.return_value
        mock_client.utility.verify_payment_signature.side_effect = \
            razorpay_module.errors.SignatureVerificationError('bad sig')
        Transaction.objects.create(
            user=self.user, amount=500, razorpay_order_id='order_2', status='Pending'
        )

        resp = self.client.post(reverse('payment_success'), {
            'razorpay_order_id': 'order_2',
            'razorpay_payment_id': 'pay_2',
            'razorpay_signature': 'bad_sig',
        })
        self.assertRedirects(resp, reverse('premium_page'), fetch_redirect_response=False)

        profile = self.user.profile
        profile.refresh_from_db()
        self.assertFalse(profile.is_premium)
        txn = Transaction.objects.get(razorpay_order_id='order_2')
        self.assertEqual(txn.status, 'Failed')

    @mock.patch('chat.views.razorpay.Client')
    def test_duplicate_callback_does_not_double_credit(self, mock_client_cls):
        mock_client = mock_client_cls.return_value
        mock_client.utility.verify_payment_signature.return_value = True
        Transaction.objects.create(
            user=self.user, amount=500, razorpay_order_id='order_3',
            razorpay_payment_id='pay_3', status='Success',
        )
        profile = self.user.profile
        profile.is_premium = True
        profile.wallet_balance = 500
        profile.save()

        resp = self.client.post(reverse('payment_success'), {
            'razorpay_order_id': 'order_3',
            'razorpay_payment_id': 'pay_3',
            'razorpay_signature': 'sig_3',
        })
        self.assertRedirects(resp, reverse('home'), fetch_redirect_response=False)

        profile.refresh_from_db()
        # Wallet should NOT have been credited a second time.
        self.assertEqual(profile.wallet_balance, 500)


# ---------------------------------------------------------------------------
# View tests: auth / OTP
# ---------------------------------------------------------------------------

class LoginViewTests(TestCase):
    def setUp(self):
        self.client = Client()

    def test_login_page_renders(self):
        resp = self.client.get(reverse('login'))
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, 'chat/login.html')
        # Google is not configured in tests -> should be flagged unavailable.
        self.assertFalse(resp.context['google_available'])

    def test_authenticated_user_redirected_home(self):
        user = User.objects.create_user(username="loggedin", password="pw12345!")
        self.client.force_login(user)
        resp = self.client.get(reverse('login'))
        self.assertRedirects(resp, reverse('home'), fetch_redirect_response=False)

    def test_post_mobile_shows_otp_page(self):
        resp = self.client.post(reverse('login'), {
            'auth_method': 'mobile',
            'mobile_no': '+911234567890',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, 'chat/verify_otp.html')

    def test_post_email_without_google_shows_error(self):
        resp = self.client.post(reverse('login'), {'auth_method': 'email'})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.context['google_available'])


class OtpFlowTests(TestCase):
    def setUp(self):
        self.client = Client()

    def test_send_otp_sets_session_and_renders(self):
        resp = self.client.post(reverse('send_otp'), {'mobile_no': '+919999999999'})
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, 'chat/verify_otp.html')
        self.assertIn('otp', self.client.session)
        self.assertEqual(self.client.session['mobile'], '+919999999999')

    def test_send_otp_get_redirects_login(self):
        resp = self.client.get(reverse('send_otp'))
        self.assertRedirects(resp, reverse('login'), fetch_redirect_response=False)

    def test_verify_otp_success_logs_in_and_creates_user(self):
        # Seed the session with a known OTP.
        self.client.post(reverse('send_otp'), {'mobile_no': '+918888888888'})
        otp = self.client.session['otp']

        resp = self.client.post(reverse('verify_otp'), {'otp': otp})
        self.assertRedirects(resp, reverse('home'), fetch_redirect_response=False)
        self.assertTrue(User.objects.filter(username='user_+918888888888').exists())
        # Session OTP is cleaned up.
        self.assertNotIn('otp', self.client.session)

    def test_verify_otp_wrong_code_shows_error(self):
        self.client.post(reverse('send_otp'), {'mobile_no': '+917777777777'})
        resp = self.client.post(reverse('verify_otp'), {'otp': '000000'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['error'], 'Invalid OTP')
        self.assertFalse(User.objects.filter(username='user_+917777777777').exists())

    def test_verify_otp_get_redirects_login(self):
        resp = self.client.get(reverse('verify_otp'))
        self.assertRedirects(resp, reverse('login'), fetch_redirect_response=False)


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

class HealthCheckTests(TestCase):
    def test_healthz_returns_ok(self):
        resp = self.client.get(reverse('healthz'))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['status'], 'ok')
        self.assertTrue(resp.json()['database'])
