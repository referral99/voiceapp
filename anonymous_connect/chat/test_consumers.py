"""
Async WebSocket tests for ChatConsumer using Channels' WebsocketCommunicator.

These exercise:
  * connect / accept handshake
  * chat message broadcast to both peers in a room
  * WebRTC signalling relay (offer/answer/ice-candidate) with no echo to sender
  * call_ended notification on disconnect
"""
import json

from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from django.test import TransactionTestCase, override_settings

from chat import routing


# Force the in-memory channel layer so tests don't require Redis.
IN_MEMORY_LAYER = {
    'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'},
}


def build_application():
    return URLRouter(routing.websocket_urlpatterns)


@override_settings(CHANNEL_LAYERS=IN_MEMORY_LAYER)
class ChatConsumerTests(TransactionTestCase):
    async def _connect(self, room='room_1_2'):
        communicator = WebsocketCommunicator(build_application(), f"/ws/chat/{room}/")
        # The consumer reads scope['session'] / scope['user'] defensively; provide
        # minimal scope so get_profile() returns None cleanly (sender -> Anonymous).
        communicator.scope['session'] = None
        communicator.scope['user'] = None
        connected, _ = await communicator.connect()
        self.assertTrue(connected)
        return communicator

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
