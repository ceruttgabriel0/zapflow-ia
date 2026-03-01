import json
import logging
from typing import List, Dict
import redis.asyncio as aioredis
from ..config import settings

logger = logging.getLogger(__name__)

HISTORY_TTL = 60 * 60 * 24  # 24 horas
MAX_HISTORY = 20


class RedisService:
    def __init__(self):
        self._client = None

    async def get_client(self):
        if self._client is None:
            self._client = aioredis.from_url(
                settings.REDIS_URL,
                encoding="utf-8",
                decode_responses=True
            )
        return self._client

    async def is_bot_active(self, session_name: str, chat_id: str) -> bool:
        try:
            r = await self.get_client()
            return await r.exists(f"active:{session_name}:{chat_id}") == 1
        except Exception as e:
            logger.error(f"Redis is_bot_active error: {e}")
            return False

    async def activate_bot(self, session_name: str, chat_id: str):
        try:
            r = await self.get_client()
            await r.set(f"active:{session_name}:{chat_id}", "1", ex=60 * 60 * 24 * 7)
        except Exception as e:
            logger.error(f"Redis activate_bot error: {e}")

    async def deactivate_bot(self, session_name: str, chat_id: str):
        try:
            r = await self.get_client()
            await r.delete(f"active:{session_name}:{chat_id}")
        except Exception as e:
            logger.error(f"Redis deactivate_bot error: {e}")

    # ------------------------------------------------------------------
    # Debounce de mensagens
    # ------------------------------------------------------------------
    async def debounce_add(self, session_name: str, chat_id: str, text: str) -> int:
        """Acumula texto na chave de debounce e reseta TTL para 5 segundos.
        Retorna o número de mensagens acumuladas."""
        try:
            r = await self.get_client()
            key = f"debounce:{session_name}:{chat_id}"
            # Append texto com separador
            existing = await r.get(key)
            if existing:
                new_text = existing + "\n" + text
            else:
                new_text = text
            await r.set(key, new_text, ex=10)  # TTL 10s (margem para o sleep de 5s)
            msg_count = new_text.count("\n") + 1
            return msg_count
        except Exception as e:
            logger.error(f"Redis debounce_add error: {e}")
            return 1

    async def debounce_get(self, session_name: str, chat_id: str) -> str:
        """Retorna o texto acumulado do debounce."""
        try:
            r = await self.get_client()
            key = f"debounce:{session_name}:{chat_id}"
            return await r.get(key) or ""
        except Exception as e:
            logger.error(f"Redis debounce_get error: {e}")
            return ""

    async def debounce_clear(self, session_name: str, chat_id: str):
        """Limpa a chave de debounce."""
        try:
            r = await self.get_client()
            key = f"debounce:{session_name}:{chat_id}"
            await r.delete(key)
        except Exception as e:
            logger.error(f"Redis debounce_clear error: {e}")

    async def debounce_lock(self, session_name: str, chat_id: str, ttl: int = 10) -> bool:
        """Tenta adquirir lock para processar debounce. Retorna True se conseguiu."""
        try:
            r = await self.get_client()
            key = f"debounce_lock:{session_name}:{chat_id}"
            return await r.set(key, "1", ex=ttl, nx=True)
        except Exception as e:
            logger.error(f"Redis debounce_lock error: {e}")
            return False

    async def debounce_unlock(self, session_name: str, chat_id: str):
        """Libera o lock de debounce."""
        try:
            r = await self.get_client()
            key = f"debounce_lock:{session_name}:{chat_id}"
            await r.delete(key)
        except Exception as e:
            logger.error(f"Redis debounce_unlock error: {e}")

    async def ping(self) -> bool:
        try:
            r = await self.get_client()
            return await r.ping()
        except Exception:
            return False


redis_service = RedisService()
