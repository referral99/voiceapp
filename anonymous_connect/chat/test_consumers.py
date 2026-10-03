"""
Async WebSocket tests for ChatConsumer using Channels' WebsocketCommunicator.

These exercise:
  * connect / accept handshake
  * chat message broadcast to both peers in a room
  * WebRTC signalling relay (offer/answer/ice-candidate) with no echo to sender
  * call_ended notification on disconnect
"""
import json

from channels.db import database_sync_to_async
from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from django.test import TransactionTestCase, override_settings

from chat import routing
from chat.models import UserProfile, Status


# Force the in-memory channel layer so tests don't require Redis.
IN_MEMORY_LAYER = {
    'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'},
}


def build_application():
    return URLRouter(routing.websocket_urlpatterns)


@override_settings(CHANNEL_LAYERS=IN_MEMORY_LAYER)
class ChatConsumerTests(TransactionTestCase):
    @database_sync_to_async
    def _make_profile(self, session_id, room):
        """Create a guest profile already paired into ``room``.

        The consumer now authorizes connections by checking that the profile's
        ``active_room_name`` matches the room being joined, so tests must set up
        a profile that is legitimately a member of that room.
        """
        return UserProfile.objects.create(
            session_id=session_id,
            active_room_name=room,
            status=Status.Busy,
        )

    class _FakeSession:
        """Minimal stand-in for a Django session with a fixed key."""
        def __init__(self, key):
            self.session_key = key

    async def _connect(self, room='room_1_2', session_id=None):
        # Each connection needs its own guest profile that is a member of the
        # room. Derive a unique session id per connection so two peers in the
        # same room are distinct profiles.
        if session_id is None:
            session_id = f"sess_{room}_{id(object())}"
        await self._make_profile(session_id, room)

        communicator = WebsocketCommunicator(build_application(), f"/ws/chat/{room}/")
        communicator.scope['user'] = None
        communicator.scope['session'] = self._FakeSession(session_id)
        connected, _ = await communicator.connect()
        self.assertTrue(connected)
        return communicator

    async def test_unauthorized_connection_rejected(self):
        """A client with no profile / not a member of the room is rejected."""
        communicator = WebsocketCommunicator(build_application(), "/ws/chat/room_99_100/")
        communicator.scope['user'] = None
        communicator.scope['session'] = None
        connected, _ = await communicator.connect()
        self.assertFalse(connected)

    async def test_wrong_room_connection_rejected(self):
        """A profile paired into one room cannot join a different room."""
        await self._make_profile('sess_mismatch', 'room_1_2')
        communicator = WebsocketCommunicator(build_application(), "/ws/chat/room_3_4/")
        communicator.scope['user'] = None
        communicator.scope['session'] = self._FakeSession('sess_mismatch')
        connected, _ = await communicator.connect()
        self.assertFalse(connected)

    async def test_connect_accepts(self):
        comm = await self._connect()
        await comm.disconnect()

    async def test_chat_message_broadcast_to_both_peers(self):
        a = await self._connect('room_5_6')
        b = await self._connect('room_5_6')

        await a.send_to(text_data=json.dumps({'type': 'chat', 'message': 'hello'}))

        resp_a = await a.receive_from()
        resp_b = await b.receive_from()
        data_a = json.loads(resp_a)
        data_b = json.loads(resp_b)

        self.assertEqual(data_a['type'], 'chat_message')
        self.assertEqual(data_a['message'], 'hello')
        self.assertEqual(data_b['message'], 'hello')
        self.assertEqual(data_b['sender'], 'Anonymous')

        await a.disconnect()
        await b.disconnect()

    async def test_webrtc_signal_relayed_to_peer_not_sender(self):
        a = await self._connect('room_7_8')
        b = await self._connect('room_7_8')

        offer = {'type': 'offer', 'sdp': {'type': 'offer', 'sdp': 'v=0...'}}
        await a.send_to(text_data=json.dumps(offer))

        # Peer B should receive the offer.
        resp_b = await b.receive_from()
        self.assertEqual(json.loads(resp_b)['type'], 'offer')

        # Sender A should NOT receive its own signal back.
        self.assertTrue(await a.receive_nothing())

        await a.disconnect()
        await b.disconnect()

    async def test_disconnect_sends_call_ended_to_peer(self):
        a = await self._connect('room_3_4')
        b = await self._connect('room_3_4')

        await a.disconnect()

        resp_b = await b.receive_from()
        data = json.loads(resp_b)
        self.assertEqual(data['type'], 'call_ended')
        self.assertIn('disconnected', data['message'])

        await b.disconnect()
