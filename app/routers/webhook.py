import logging
import asyncio
import base64
import httpx
from fastapi import APIRouter, Request, BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from ..database import AsyncSessionLocal
from ..models.client import Client
from ..models.message import Message
from ..services.waha_service import waha_service
from ..services.openai_service import openai_service
from ..services.redis_service import redis_service
from ..services.scheduler_service import schedule_followup, cancel_pending_followups
from ..config import settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhook", tags=["webhook"])

TRIGGER_MESSAGE = "Teste Robo"
DEBOUNCE_SECONDS = 5


async def process_message(session_name: str, chat_id: str, user_text: str, media_url: str = None, media_type: str = None):
    """
    Processa a mensagem recebida:
    1. Busca o cliente (tenant) pelo nome da sessão WAHA
    2. Salva a mensagem do usuário no banco
    3. Chama a OpenAI (com histórico + mídia se houver)
    4. Salva a resposta da IA no banco
    5. Envia a resposta via WAHA
    6. Cancela follow-ups pendentes e agenda novo follow-up
    """
    async with AsyncSessionLocal() as db:
        try:
            # 1. Buscar o cliente pelo session_name
            stmt = select(Client).where(
                Client.waha_session_name == session_name,
                Client.is_active == True
            )
            result = await db.execute(stmt)
            client = result.scalar_one_or_none()

            if not client:
                logger.warning(f"Sessão '{session_name}' não encontrada ou inativa. Ignorando mensagem.")
                return

            contact_number = chat_id.split("@")[0]

            # 2. Cancelar follow-ups pendentes (o lead respondeu)
            try:
                await cancel_pending_followups(client.id, contact_number, session_name)
            except Exception as e:
                logger.error(f"Erro ao cancelar follow-ups: {e}")

            # 3. Salvar mensagem do usuário
            user_msg = Message(
                client_id=client.id,
                contact_number=contact_number,
                role="user",
                content=user_text
            )
            db.add(user_msg)
            await db.commit()

            # 4. Chamar a OpenAI com contexto completo
            ai_response = await openai_service.get_response(
                db=db,
                client_id=client.id,
                contact_number=contact_number,
                system_prompt=client.system_prompt,
                calendar_id=client.gcal_calendar_id,
                user_message=user_text,
                session_name=session_name,
                chat_id=chat_id,
                media_url=media_url,
                media_type=media_type
            )

            # 5. Salvar resposta da IA
            assistant_msg = Message(
                client_id=client.id,
                contact_number=contact_number,
                role="assistant",
                content=ai_response
            )
            db.add(assistant_msg)
            await db.commit()

            # 6. Enviar resposta via WAHA
            await waha_service.send_text_message(
                session_name=session_name,
                chat_id=chat_id,
                text=ai_response
            )

            # 7. Agendar próximo follow-up (4h depois)
            try:
                await schedule_followup(client.id, contact_number, chat_id, session_name)
            except Exception as e:
                logger.error(f"Erro ao agendar follow-up: {e}")

        except Exception as e:
            logger.error(f"Erro ao processar mensagem para sessão '{session_name}': {e}")
            await db.rollback()


async def download_media(url: str) -> str:
    """Baixa mídia do WAHA e retorna como base64."""
    try:
        headers = {}
        if settings.WAHA_API_KEY:
            headers["X-Api-Key"] = settings.WAHA_API_KEY
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            return base64.b64encode(response.content).decode("utf-8")
    except Exception as e:
        logger.error(f"Erro ao baixar mídia: {e}")
        return None


async def debounce_and_process(session_name: str, chat_id: str, media_url: str = None, media_type: str = None):
    """
    Espera DEBOUNCE_SECONDS sem nova mensagem antes de processar.
    Usa lock Redis para evitar processamentos duplicados.
    """
    try:
        await asyncio.sleep(DEBOUNCE_SECONDS)

        # Tentar adquirir lock — só um processamento por vez por contato
        locked = await redis_service.debounce_lock(session_name, chat_id)
        if not locked:
            return  # Outro processamento já está rodando

        try:
            # Pegar texto acumulado
            accumulated = await redis_service.debounce_get(session_name, chat_id)
            if not accumulated:
                return  # Nada para processar

            # Limpar debounce
            await redis_service.debounce_clear(session_name, chat_id)

            logger.info(f"[{session_name}] Debounce concluído para {chat_id}: {accumulated[:100]}")

            # Processar mensagem acumulada
            await process_message(
                session_name=session_name,
                chat_id=chat_id,
                user_text=accumulated,
                media_url=media_url,
                media_type=media_type,
            )
        finally:
            await redis_service.debounce_unlock(session_name, chat_id)

    except Exception as e:
        logger.error(f"Erro no debounce_and_process: {e}")


@router.post("/waha")
async def waha_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Recebe eventos do WAHA e despacha para processamento em background.
    Suporta: texto, áudio (transcrição via Whisper), imagem (visão via GPT-4o).

    Lógica de ativação:
    - Gatilho "Teste Robo" ativa o bot para aquela conversa
    - Após ativado, o bot responde a TODAS as mensagens daquela conversa
    - Se o humano (dono do número) enviar uma mensagem (fromMe=True),
      o bot desativa automaticamente para aquela conversa

    Debounce:
    - Mensagens são acumuladas por 5 segundos antes de processar
    - Se o contato enviar várias mensagens rápidas, elas são concatenadas
    """
    try:
        data = await request.json()
        event_type = data.get("event")
        payload = data.get("payload", {})

        # Processar apenas eventos de mensagem
        if event_type not in ("message", "message.any"):
            return {"status": "ignored", "reason": "not_a_message_event"}

        session_name = data.get("session", "default")
        chat_id = payload.get("from")

        if not chat_id:
            return {"status": "ignored", "reason": "no_chat_id"}

        # Se o humano respondeu, desativa o bot para este contato
        if payload.get("fromMe", False):
            await redis_service.deactivate_bot(session_name, chat_id)
            # Cancelar follow-ups pendentes também
            try:
                contact_number = chat_id.split("@")[0]
                await cancel_pending_followups(0, contact_number, session_name)
            except Exception:
                pass
            logger.info(f"🛑 Humano assumiu conversa com {chat_id} — bot desativado")
            return {"status": "ignored", "reason": "human_took_over"}

        msg_type = payload.get("type", "text")
        user_text = ""
        media_url = None
        media_type = None

        # --- Texto ---
        if msg_type == "text":
            user_text = payload.get("body", "").strip()
            if not user_text:
                return {"status": "ignored", "reason": "empty_text"}

        # --- Áudio (transcrição via Whisper) ---
        elif msg_type in ("audio", "ptt"):
            media_info = payload.get("media", {}) or payload.get("_data", {})
            media_url = media_info.get("url") or payload.get("mediaUrl")
            if media_url:
                audio_b64 = await download_media(media_url)
                if audio_b64:
                    transcription = await openai_service.transcribe_audio(audio_b64)
                    user_text = f"[Áudio transcrito]: {transcription}"
                else:
                    user_text = "[Áudio recebido, mas não foi possível transcrever]"
            media_url = None  # Já processamos

        # --- Imagem ---
        elif msg_type == "image":
            media_info = payload.get("media", {}) or payload.get("_data", {})
            media_url = media_info.get("url") or payload.get("mediaUrl")
            caption = payload.get("caption") or payload.get("body") or ""
            user_text = caption if caption else "[Imagem enviada]"
            media_type = "image"

        # --- Documento / outros ---
        else:
            user_text = payload.get("body") or f"[{msg_type} recebido]"

        logger.info(f"[{session_name}] Mensagem de {chat_id} ({msg_type}): {user_text[:80]}")

        # ---------------------------------------------------------------
        # VERIFICAR GATILHO / CONVERSA ATIVA (via Redis)
        # ---------------------------------------------------------------
        is_active = await redis_service.is_bot_active(session_name, chat_id)

        if not is_active:
            if TRIGGER_MESSAGE.lower() in user_text.lower():
                await redis_service.activate_bot(session_name, chat_id)
                logger.info(f"🤖 Bot ATIVADO para {session_name}:{chat_id}")
            else:
                logger.info(f"[{session_name}] Ignorado — bot não ativo para {chat_id}")
                return {"status": "ignored", "reason": "bot_not_active"}

        # ---------------------------------------------------------------
        # DEBOUNCE: acumular mensagens por 5 segundos
        # ---------------------------------------------------------------
        msg_count = await redis_service.debounce_add(session_name, chat_id, user_text)
        logger.info(f"[{session_name}] Debounce: {msg_count} mensagem(ns) acumulada(s) para {chat_id}")

        # Agendar processamento com delay de 5 segundos
        # Se chegar outra mensagem, o timer será "renovado" pelo acúmulo no Redis
        asyncio.create_task(
            debounce_and_process(
                session_name=session_name,
                chat_id=chat_id,
                media_url=media_url,
                media_type=media_type,
            )
        )

        return {"status": "success", "message": "debouncing"}

    except Exception as e:
        logger.error(f"Erro no webhook: {e}")
        return {"status": "error", "message": str(e)}
