import json

from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async

from .models import UserProfile, Status


class ChatConsumer(AsyncWebsocketConsumer):
    """Handles message passing and WebRTC signalling once two users are matched."""

    async def connect(self):
        self.room_name = self.scope['url_route']['kwargs']['room_name']
        self.room_group_name = f'chat_{self.room_name}'

        # Identify the user (registered or guest).
        self.profile = await self.get_profile()

        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        # Notify the OTHER user that the call is ending.
        if hasattr(self, 'room_group_name'):
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'call_ended',
                    'message': 'The other user has disconnected.',
                },
            )

            # Perform database cleanup.
            profile = await self.get_profile()
            if profile:
                await self.perform_full_cleanup(profile)

            await self.channel_layer.group_discard(
                self.room_group_name,
                self.channel_name,
            )

    async def receive(self, text_data):
        data = json.loads(text_data)
        msg_type = data.get('type')

        # Relay WebRTC signalling + voice-call control messages untouched.
        # These are peer-to-peer coordination messages that must reach the
        # OTHER browser only (never echo back to the sender).
        if msg_type in (
            'offer', 'answer', 'ice-candidate',
            'call-request', 'call-accept', 'call-reject', 'call-hangup',
        ):
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'signal_message',
                    'payload': data,
                    'sender_channel': self.channel_name,
                },
            )
            return

        # Read receipt: the recipient tells the sender their message was seen.
        # Relay to the group but skip the original sender (handled client-side
        # via sender_id comparison).
        if msg_type == 'read_receipt':
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'read_receipt',
                    'message_id': data.get('message_id'),
                    'sender_channel': self.channel_name,
                },
            )
            return

        # Otherwise treat it as a chat message.
        name = self.profile.display_name if self.profile else "Anonymous"
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'chat_message',
                'message': data.get('message'),
                'sender': name,
                'sender_id': data.get('sender_id'),
                'message_id': data.get('message_id'),
            },
        )

    # --- Group event handlers ---

    async def chat_message(self, event):
        await self.send(text_data=json.dumps({
            'type': 'chat_message',
            'message': event['message'],
            'sender': event['sender'],
            'sender_id': event.get('sender_id'),
            'message_id': event.get('message_id'),
        }))

    async def signal_message(self, event):
        # Don't echo the signal back to the original sender.
        if event.get('sender_channel') == self.channel_name:
            return
        await self.send(text_data=json.dumps(event['payload']))

    async def read_receipt(self, event):
        # Don't echo the receipt back to the reader; only the original
        # message sender needs it.
        if event.get('sender_channel') == self.channel_name:
            return
        await self.send(text_data=json.dumps({
            'type': 'read_receipt',
            'message_id': event.get('message_id'),
        }))

    async def call_ended(self, event):
        await self.send(text_data=json.dumps({
            'type': 'call_ended',
            'message': event['message'],
        }))

    # --- Database helpers ---

    @database_sync_to_async
    def get_profile(self):
        user = self.scope.get("user")
        try:
            if user is not None and user.is_authenticated:
                return UserProfile.objects.get(user=user)
            session = self.scope.get("session")
            session_key = session.session_key if session else None
            if not session_key:
                return None
            return UserProfile.objects.get(session_id=session_key)
        except UserProfile.DoesNotExist:
            return None

    @database_sync_to_async
    def perform_full_cleanup(self, profile):
        """Reset the room and status so the user is discoverable again."""
        try:
            fresh = UserProfile.objects.filter(pk=profile.pk).first()
            if fresh:
                fresh.clear_user_entry()
        except UserProfile.DoesNotExist:
            pass
