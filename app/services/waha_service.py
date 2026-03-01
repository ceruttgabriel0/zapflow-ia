import httpx
import logging
from ..config import settings

logger = logging.getLogger(__name__)


class WahaService:
    def __init__(self):
        self.base_url = settings.WAHA_API_URL

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if settings.WAHA_API_KEY:
            headers["X-Api-Key"] = settings.WAHA_API_KEY
        return headers

    async def send_text_message(self, session_name: str, chat_id: str, text: str) -> bool:
        """
        Envia uma mensagem de texto via WAHA API.
        chat_id deve estar no formato 'number@c.us' ou 'number@g.us'
        """
        url = f"{self.base_url}/api/sendText"
        payload = {
            "session": session_name,
            "chatId": chat_id,
            "text": text
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(url, json=payload, headers=self._headers())
                response.raise_for_status()
                logger.info(f"Mensagem enviada para {chat_id} (sessao: {session_name})")
                return True
        except httpx.HTTPStatusError as e:
            logger.error(f"Erro HTTP ao enviar mensagem: {e.response.status_code} - {e.response.text}")
            return False
        except Exception as e:
            logger.error(f"Erro ao enviar mensagem via WAHA: {e}")
            return False


# Singleton
waha_service = WahaService()
