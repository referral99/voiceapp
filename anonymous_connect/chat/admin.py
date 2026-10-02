from django.contrib import admin
from .models import UserProfile, Transaction, lastConnected


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    """
    Control panel for managing anonymous users, their premium status, and wallet balances.
    """
    # Display these columns in the admin list view
    list_display = ('display_name', 'user', 'age', 'sex', 'profession', 'is_premium', 'wallet_balance', 'status')

    # Add filters on the right sidebar for quick sorting
    list_filter = ('is_premium', 'sex', 'profession', 'status')

    # Search functionality to find users by username or profession
    search_fields = ('display_name', 'user__username', 'profession', 'user__email')

    # Group fields for a cleaner layout when editing a user
    fieldsets = (
        ('Basic Information', {
            'fields': ('user', 'session_id', 'display_name', 'age', 'sex', 'profession')
        }),
        ('Matchmaking Preferences (Premium)', {
            'fields': ('pref_sex', 'pref_profession'),
            'description': 'These filters apply only during random matching for premium users.'
        }),
        ('Account & Wallet', {
            'fields': ('is_premium', 'wallet_balance')
        }),
        ('Real-Time Status', {
            'fields': ('status', 'active_room_name')
        }),
    )

    # Prevents editing the linked user directly from the profile admin for safety
    readonly_fields = ('user',)


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    """
    Log of all UPI/Razorpay wallet additions.
    """
    list_display = ('user', 'amount', 'status', 'timestamp', 'razorpay_order_id')
    list_filter = ('status', 'timestamp')
    search_fields = ('user__username', 'razorpay_order_id', 'razorpay_payment_id')
    readonly_fields = ('timestamp',)


@admin.register(lastConnected)
class LastConnectedAdmin(admin.ModelAdmin):
    list_display = ('user_session_id', 'last_user_session_id')
