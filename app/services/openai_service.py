import io
import json
import base64
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from openai import AsyncOpenAI
from ..config import settings
from .gcal_service import gcal_service
from ..models.message import Message
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuração de providers — adicione novos providers aqui
# ---------------------------------------------------------------------------
PROVIDER_CONFIG = {
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1/chat/completions",
        "model": "z-ai/glm-5",
        "api_key_field": "OPENROUTER_API_KEY",
        "extra_headers": {
            "HTTP-Referer": "http://localhost",
            "X-Title": "zapflow-ia-backend",
        },
    },
    "openai": {
        "base_url": "https://api.openai.com/v1/chat/completions",
        "model": "gpt-4o",
        "api_key_field": "OPENAI_API_KEY",
        "extra_headers": {},
    },
}


# ---------------------------------------------------------------------------
# Chamada HTTP a um provider específico
# ---------------------------------------------------------------------------
async def _call_provider(provider: str, messages: list, tools: list = None, tool_choice: str = None) -> dict:
    """Faz chamada a um provider específico. Lança exceção se falhar."""
    if provider not in PROVIDER_CONFIG:
        raise ValueError(f"Provider '{provider}' inválido.")

    config = PROVIDER_CONFIG[provider]
    api_key = getattr(settings, config["api_key_field"], "")

    if not api_key:
        raise ValueError(f"API key não configurada para '{provider}'.")

    if len(messages) > 1:
        system_msgs = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]
        messages = system_msgs + non_system[-10:]

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        **config["extra_headers"],
    }

    payload = {
        "model": config["model"],
        "messages": messages,
        "max_tokens": 600,
        "temperature": 0.7,
    }

    if tools:
        payload["tools"] = tools
    if tool_choice:
        payload["tool_choice"] = tool_choice

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(config["base_url"], headers=headers, json=payload)
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code} do provider '{provider}': {response.text[:300]}")
        return response.json()


# ---------------------------------------------------------------------------
# Função pública — com fallback automático
# ---------------------------------------------------------------------------
async def generate_ai_response(messages: list, tools: list = None, tool_choice: str = None) -> dict:
    """Chama o provider principal. Se falhar, tenta o fallback automaticamente."""
    primary = settings.AI_PROVIDER.lower()
    fallback = settings.AI_PROVIDER_FALLBACK.lower()

    try:
        logger.info(f"Chamando provider: {primary}")
        return await _call_provider(primary, messages, tools, tool_choice)
    except Exception as e:
        logger.warning(f"Provider '{primary}' falhou: {e}. Tentando fallback '{fallback}'...")
        try:
            return await _call_provider(fallback, messages, tools, tool_choice)
        except Exception as e2:
            logger.error(f"Fallback '{fallback}' também falhou: {e2}")
            raise RuntimeError(f"Todos os providers falharam.")


# ---------------------------------------------------------------------------
# Classe de serviço — mantém toda a lógica de negócio existente
# ---------------------------------------------------------------------------
class OpenAIService:
    def __init__(self):
        # AsyncOpenAI permanece apenas para Whisper (transcrição)
        if settings.OPENAI_API_KEY:
            self.whisper_client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        else:
            self.whisper_client = None

    async def transcribe_audio(self, audio_b64: str) -> str:
        """Transcreve audio usando Whisper. Recebe base64, retorna texto."""
        if not self.whisper_client:
            logger.error("OPENAI_API_KEY não configurada — transcrição indisponível.")
            return "[Transcrição indisponível: OPENAI_API_KEY não configurada]"
        try:
            audio_bytes = base64.b64decode(audio_b64)
            audio_file = io.BytesIO(audio_bytes)
            audio_file.name = "audio.ogg"
            transcription = await self.whisper_client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file,
                language="pt"
            )
            return transcription.text
        except Exception as e:
            logger.error(f"Erro ao transcrever audio: {e}")
            return "[Nao foi possivel transcrever o audio]"

    async def get_response(
        self,
        db: AsyncSession,
        client_id: int,
        contact_number: str,
        system_prompt: str,
        calendar_id: str,
        user_message: str,
        session_name: str = "",
        chat_id: str = "",
        media_url: str = None,
        media_type: str = None
    ) -> str:
        # 1. Recuperar historico (ultimas 10 mensagens)
        stmt = select(Message).where(
            Message.client_id == client_id,
            Message.contact_number == contact_number
        ).order_by(Message.timestamp.desc()).limit(10)

        result = await db.execute(stmt)
        history = result.scalars().all()
        history.reverse()

        messages = [{"role": "system", "content": system_prompt}]
        for msg in history:
            messages.append({"role": msg.role, "content": msg.content})

        # 2. Montar mensagem do usuario (com imagem se houver)
        if media_type == "image" and media_url:
            user_content = [
                {"type": "text", "text": user_message},
                {"type": "image_url", "image_url": {"url": media_url}}
            ]
        else:
            user_content = user_message

        messages.append({"role": "user", "content": user_content})

        # 3. Definir ferramentas
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "check_availability",
                    "description": "Consulta horarios ocupados em uma data especifica no Google Calendar.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "date": {"type": "string", "description": "Data no formato YYYY-MM-DD"}
                        },
                        "required": ["date"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "book_appointment",
                    "description": "Agenda um compromisso no Google Calendar.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "contact_name": {"type": "string", "description": "Nome do cliente"},
                            "contact_number": {"type": "string", "description": "Numero de telefone"},
                            "date": {"type": "string", "description": "Data no formato YYYY-MM-DD"},
                            "time": {"type": "string", "description": "Horario no formato HH:MM"},
                            "duration_minutes": {"type": "integer", "description": "Duracao em minutos (padrao 60)"}
                        },
                        "required": ["contact_name", "contact_number", "date", "time"]
                    }
                }
            }
        ]

        try:
            # 4. Primeira chamada ao provider via generate_ai_response
            data = await generate_ai_response(
                messages=messages,
                tools=tools,
                tool_choice="auto",
            )

            assistant_msg = data["choices"][0]["message"]
            tool_calls = assistant_msg.get("tool_calls")

            # 5. Processar chamadas de ferramentas se houver
            if tool_calls:
                messages.append(assistant_msg)

                for tool_call in tool_calls:
                    function_name = tool_call["function"]["name"]
                    args = json.loads(tool_call["function"]["arguments"])
                    tool_result = ""

                    if function_name == "check_availability":
                        if not gcal_service.is_configured():
                            tool_result = "O sistema de agenda não está configurado no momento. Informe ao cliente que a disponibilidade será verificada manualmente pelo especialista."
                        else:
                            busy_slots = await gcal_service.get_free_busy(calendar_id, args["date"])
                            if busy_slots:
                                tool_result = f"Horarios ocupados em {args['date']}: {json.dumps(busy_slots)}"
                            else:
                                tool_result = f"Nenhum horario ocupado em {args['date']}. Dia totalmente livre."

                    elif function_name == "book_appointment":
                        if not gcal_service.is_configured():
                            tool_result = "O agendamento será confirmado manualmente pelo especialista. Informe ao cliente que entraremos em contato para confirmar."
                        else:
                            duration = args.get("duration_minutes", 60)
                            start_iso = f"{args['date']}T{args['time']}:00"
                            start_dt = datetime.fromisoformat(start_iso).replace(tzinfo=timezone.utc)
                            end_dt = start_dt + timedelta(minutes=duration)
                            start_str = start_dt.strftime("%Y-%m-%dT%H:%M:00Z")
                            end_str = end_dt.strftime("%Y-%m-%dT%H:%M:00Z")

                            event_id = await gcal_service.create_event(
                                calendar_id=calendar_id,
                                summary=f"Agendamento: {args['contact_name']}",
                                description=f"WhatsApp: {args['contact_number']}",
                                start_time=start_str,
                                end_time=end_str
                            )

                            if event_id:
                                tool_result = "Agendamento realizado com sucesso!"
                                # Salvar appointment no banco
                                try:
                                    from .scheduler_service import save_appointment
                                    await save_appointment(
                                        client_id=client_id,
                                        contact_number=contact_number,
                                        chat_id=chat_id,
                                        session_name=session_name,
                                        contact_name=args['contact_name'],
                                        start_time=start_dt,
                                        end_time=end_dt,
                                        gcal_event_id=event_id,
                                    )
                                except Exception as e:
                                    logger.error(f"Erro ao salvar appointment no banco: {e}")
                            else:
                                tool_result = "Erro ao realizar o agendamento."

                    messages.append({
                        "tool_call_id": tool_call["id"],
                        "role": "tool",
                        "name": function_name,
                        "content": tool_result
                    })

                # Segunda chamada (sem tools) para obter resposta final
                final_data = await generate_ai_response(messages=messages)
                return final_data["choices"][0]["message"]["content"]

            return assistant_msg.get("content", "")

        except Exception as e:
            logger.error(f"Erro na IA ({settings.AI_PROVIDER}): {e}")
            return "Desculpe, tive um problema tecnico. Tente novamente em instantes."


# Singleton
openai_service = OpenAIService()
