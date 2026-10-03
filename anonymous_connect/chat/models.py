from django.db import models
from django.contrib.auth.models import User
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.utils import timezone


class Status(models.TextChoices):
    Online = 'online', 'Online'
    Busy = 'busy', 'Busy'
    Offline = 'offline', 'Offline'


# Age-group buckets used for the premium "Age Greater Than" preference.
# The stored value is the minimum age; 0 means "any age".
AGE_GROUP_CHOICES = [
    (0, 'Any'),
    (18, 'Greater than 18'),
    (21, 'Greater than 21'),
    (25, 'Greater than 25'),
    (30, 'Greater than 30'),
    (40, 'Greater than 40'),
    (50, 'Greater than 50'),
]

# Daily premium pricing and the number of reconnects each tier gets.
# Indian users are charged in INR; everyone else (global users) in USD.
PREMIUM_DAILY_PRICE_INR = 10      # ₹10 per day for users in India
PREMIUM_DAILY_PRICE_USD = 1       # $1 per day for global (non-Indian) users
PREMIUM_RECONNECT_LIMIT = 5
FREE_RECONNECT_LIMIT = 1


class UserProfile(models.Model):
    # Link to Django's built-in User (Username, Email, Password)
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile', null=True, blank=True)
    session_id = models.CharField(max_length=100, unique=True, null=True, blank=True)
    display_name = models.CharField(max_length=50, default="Anonymous")

    active_room_name = models.CharField(max_length=255, null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, null=True, blank=True, default=Status.Offline)

    # --- Basic User Details ---
    age = models.PositiveIntegerField(null=True, blank=True)
    sex_choices = [
        ('any', 'Any'),
        ('male', 'Male'),
        ('female', 'Female'),
        ('other', 'Other'),
    ]
    sex = models.CharField(max_length=10, choices=sex_choices, default='any')

    profession = models.CharField(max_length=100, default='any')

    # --- Premium Matchmaking Preferences ---
    # These are only utilized while premium is active.
    pref_sex = models.CharField(max_length=10, choices=sex_choices, default='any')
    pref_profession = models.CharField(max_length=100, default='any')
    # Minimum age of the desired match ("Age Greater Than"). 0 means "any".
    pref_age_min = models.PositiveIntegerField(default=0)

    # --- Premium & Wallet Status ---
    is_premium = models.BooleanField(default=False)
    # Daily premium expires at the next midnight. When this timestamp passes,
    # premium is considered inactive until the user pays again.
    premium_expiry = models.DateTimeField(null=True, blank=True)
    wallet_balance = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)

    # --- Reconnect tracking (resets daily at midnight) ---
    reconnect_count = models.PositiveIntegerField(default=0)
    last_reset_date = models.DateField(null=True, blank=True)

    # --- Connection History ---
    # Stores the session id of the last person this profile connected with,
    # used by the premium "Connect Last User" feature.
    last_connected_session = models.CharField(max_length=100, null=True, blank=True)

    # --- Premium & daily-reset helpers ---

    @property
    def premium_active(self):
        """True only when premium was paid for and hasn't expired at midnight."""
        if not self.is_premium:
            return False
        if self.premium_expiry is None:
            return False
        if timezone.now() >= self.premium_expiry:
            return False
        return True

    def reset_daily_counters_if_needed(self, save=True):
        """Reset the reconnect counter (and lapse premium) on a new calendar day."""
        today = timezone.localdate()
        changed = False

        if self.last_reset_date != today:
            self.reconnect_count = 0
            self.last_reset_date = today
            changed = True

        # Lapse premium once its expiry has passed.
        if self.is_premium and (self.premium_expiry is None or timezone.now() >= self.premium_expiry):
            self.is_premium = False
            self.premium_expiry = None
            changed = True

        if changed and save and self.pk:
            self.save(update_fields=[
                'reconnect_count', 'last_reset_date', 'is_premium', 'premium_expiry',
            ])
        return changed

    @property
    def reconnect_limit(self):
        return PREMIUM_RECONNECT_LIMIT if self.premium_active else FREE_RECONNECT_LIMIT

    def can_reconnect(self):
        return self.reconnect_count < self.reconnect_limit

    @staticmethod
    def next_midnight():
        """The upcoming local midnight as an aware datetime."""
        tomorrow = timezone.localdate() + timezone.timedelta(days=1)
        naive_midnight = timezone.datetime.combine(tomorrow, timezone.datetime.min.time())
        return timezone.make_aware(naive_midnight, timezone.get_current_timezone())

    def clear_user_entry(self):
        """Reset the profile so the user becomes discoverable again.

        For anonymous (session-only) profiles we remove the row entirely on
        hang up. For registered users we keep the row but reset live state.
        """
        if self.user_id is None and self.session_id:
            UserProfile.objects.filter(session_id=self.session_id).delete()
        else:
            self.status = Status.Offline
            self.active_room_name = None
            self.save(update_fields=['status', 'active_room_name'])

    def __str__(self):
        return f"{self.display_name}'s Profile"


class lastConnected(models.Model):
    """A simple record of a pairing between two session ids."""
    user_session_id = models.CharField(max_length=100, null=True, blank=True)
    last_user_session_id = models.CharField(max_length=100, null=True, blank=True)

    def __str__(self):
        return f"{self.user_session_id} -> {self.last_user_session_id}"


# --- Signals to automatically create/save Profile when User is created ---

@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    if created:
        UserProfile.objects.get_or_create(user=instance)


@receiver(post_save, sender=User)
def save_user_profile(sender, instance, **kwargs):
    # Ensure a profile always exists for a registered user.
    profile, _ = UserProfile.objects.get_or_create(user=instance)
    if not kwargs.get('created', False):
        profile.save()


class Transaction(models.Model):
    """Tracks wallet deposits via UPI/Razorpay"""
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    razorpay_order_id = models.CharField(max_length=100, blank=True, null=True)
    razorpay_payment_id = models.CharField(max_length=100, blank=True, null=True)
    timestamp = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, default='Pending')  # Pending, Success, Failed

    def __str__(self):
        return f"{self.user.username} - {self.amount} - {self.status}"
