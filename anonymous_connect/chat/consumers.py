import json
import time

from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async

from .models import UserProfile, Status
from activity_logging import Action, log_event


class ChatConsumer(AsyncWebsocketConsumer):
    """Handles message passing and WebRTC signalling once two users are matched."""

    async def connect(self):
        self.room_name = self.scope['url_route']['kwargs']['room_name']
        self.room_group_name = f'chat_{self.room_name}'

        # Identify the user (registered or guest).
        self.profile = await self.get_profile()

        # --- Authorization ---
        # Room names are predictable (room_<minId>_<maxId>), so without a check
        # any client could join an active call's room and receive its chat /
        # WebRTC signalling. Only allow the connection when this profile was
        # actually paired into THIS room (its active_room_name matches). Reject
        # everyone else before accepting the socket.
        if not await self.is_authorized_for_room():
            await self.close(code=4403)
            return

        # Mark when this session (call/room connection) began so we can report
        # how long the user stayed when they disconnect.
        self.connected_at = time.monotonic()

        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        self.accepted = True
        await self.accept()

        # --- Activity logging: a session (room connection) started. ---
        log_event(
            Action.SESSION_START,
            profile=self.profile,
            room=self.room_name,
        )

    async def disconnect(self, close_code):
        # If the socket was rejected during connect() (failed authorization),
        # it never joined the group or logged a session start, so there is
        # nothing to clean up or notify. Bail out early.
        if not getattr(self, 'accepted', False):
            return

        # --- Activity logging: session ended; report how long they stayed. ---
        duration_seconds = None
        if getattr(self, 'connected_at', None) is not None:
            duration_seconds = round(time.monotonic() - self.connected_at, 1)
        log_event(
            Action.SESSION_END,
            profile=getattr(self, 'profile', None),
            room=getattr(self, 'room_name', None),
            duration_seconds=duration_seconds,
            close_code=close_code,
        )

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
            # --- Activity logging: voice-call control events. ---
            # The chat->voice escalation buttons send these control messages, so
            # they mark voice-call activity (request/accept/reject/hangup).
            if msg_type in ('call-request', 'call-accept', 'call-reject', 'call-hangup'):
                log_event(
                    Action.VOICE_CALL_ATTEMPT,
                    profile=self.profile,
                    room=self.room_name,
                    kind='signal',
                    signal=msg_type,
                )

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
    def is_authorized_for_room(self):
        """True only if the current profile was paired into this exact room.

        Matchmaking (see chat.views._pair_profiles) sets ``active_room_name`` on
        both paired profiles to the shared room. We require that the connecting
        profile exists and its ``active_room_name`` equals the room it is trying
        to join, so a stranger cannot open an arbitrary ``ws/chat/<room>/`` and
        eavesdrop on someone else's call.
        """
        profile = self.profile
        if profile is None:
            return False
        # Re-read the current value from the DB rather than trusting a possibly
        # stale in-memory copy.
        fresh = UserProfile.objects.filter(pk=profile.pk).first()
        if not fresh:
            return False
        return fresh.active_room_name == self.room_name

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
