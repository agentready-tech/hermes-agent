"""Synthetic external messaging wire; all routing and delivery remain native Hermes."""
import uuid


def install():
    from gateway.config import Platform
    from gateway.platforms.base import BasePlatformAdapter, SendResult
    from gateway.run import GatewayRunner

    class LocalWire(BasePlatformAdapter):
        def __init__(self, config):
            super().__init__(config, Platform.TELEGRAM)

        async def connect(self, *, is_reconnect=False):
            self._mark_connected()
            return True

        async def disconnect(self):
            self._mark_disconnected()

        async def send(self, chat_id, content, reply_to=None, metadata=None):
            return SendResult(success=True, message_id=uuid.uuid4().hex)

        async def edit_message(self, chat_id, message_id, content, *, finalize=False):
            return SendResult(success=True, message_id=message_id)

        async def send_typing(self, chat_id, metadata=None):
            return None

        async def get_chat_info(self, chat_id):
            return {'id': chat_id}

    original = GatewayRunner._instantiate_adapter

    def instantiate(self, platform, config):
        if platform == Platform.TELEGRAM:
            return LocalWire(config)
        return original(self, platform, config)

    GatewayRunner._instantiate_adapter = instantiate
