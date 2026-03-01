import os
import asyncio
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from ..config import settings

logger = logging.getLogger(__name__)


class GCalService:
    def __init__(self):
        self._service = None
        self._configured: Optional[bool] = None

    def is_configured(self) -> bool:
        """Verifica se o service account está configurado e acessível."""
        if self._configured is not None:
            return self._configured
        try:
            file_path = settings.GOOGLE_SERVICE_ACCOUNT_FILE
            if not file_path or not os.path.isfile(file_path):
                logger.warning(f"Google Calendar não configurado: arquivo '{file_path}' não encontrado.")
                self._configured = False
                return False
            # Tentar importar as dependências
            from google.oauth2 import service_account  # noqa: F401
            self._configured = True
            return True
        except ImportError:
            logger.warning("Google Calendar não configurado: dependências não instaladas.")
            self._configured = False
            return False
        except Exception as e:
            logger.warning(f"Google Calendar não configurado: {e}")
            self._configured = False
            return False

    def _get_service(self):
        """Inicializa o servico do Google Calendar de forma lazy (so quando necessario)."""
        if self._service is None:
            try:
                from google.oauth2 import service_account
                from googleapiclient.discovery import build

                scopes = ["https://www.googleapis.com/auth/calendar"]
                creds = service_account.Credentials.from_service_account_file(
                    settings.GOOGLE_SERVICE_ACCOUNT_FILE, scopes=scopes
                )
                self._service = build("calendar", "v3", credentials=creds)
            except Exception as e:
                logger.error(f"Erro ao inicializar Google Calendar: {e}")
                raise
        return self._service

    async def get_free_busy(self, calendar_id: str, date_str: str) -> List[Dict[str, str]]:
        """
        Lista horarios ocupados para um determinado dia.
        Executa a chamada sincrona em thread separada para nao bloquear o event loop.
        """
        if not self.is_configured():
            return []

        def _sync_call():
            service = self._get_service()
            start_of_day = f"{date_str}T00:00:00Z"
            end_of_day = f"{date_str}T23:59:59Z"
            body = {
                "timeMin": start_of_day,
                "timeMax": end_of_day,
                "items": [{"id": calendar_id}]
            }
            result = service.freebusy().query(body=body).execute()
            return result.get("calendars", {}).get(calendar_id, {}).get("busy", [])

        try:
            loop = asyncio.get_event_loop()
            busy_slots = await loop.run_in_executor(None, _sync_call)
            return busy_slots
        except Exception as e:
            logger.error(f"Erro ao consultar disponibilidade (Calendar: {calendar_id}): {e}")
            return []

    async def create_event(
        self,
        calendar_id: str,
        summary: str,
        description: str,
        start_time: str,
        end_time: str
    ) -> Optional[str]:
        """
        Cria um evento no Google Calendar.
        start_time e end_time no formato ISO 8601 (ex: 2025-10-27T10:00:00Z)
        Retorna o event_id se criado com sucesso, ou None em caso de falha.
        """
        if not self.is_configured():
            return None

        def _sync_call():
            service = self._get_service()
            event = {
                "summary": summary,
                "description": description,
                "start": {"dateTime": start_time, "timeZone": "America/Sao_Paulo"},
                "end": {"dateTime": end_time, "timeZone": "America/Sao_Paulo"},
            }
            created = service.events().insert(calendarId=calendar_id, body=event).execute()
            return created.get("id")

        try:
            loop = asyncio.get_event_loop()
            event_id = await loop.run_in_executor(None, _sync_call)
            return event_id
        except Exception as e:
            logger.error(f"Erro ao criar evento (Calendar: {calendar_id}): {e}")
            return None


# Singleton
gcal_service = GCalService()
