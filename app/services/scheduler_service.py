import logging
from datetime import datetime, timedelta, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select, update
from ..database import AsyncSessionLocal
from ..models.client import Client
from ..models.followup import FollowUp
from ..models.appointment import Appointment
from ..models.message import Message
from .waha_service import waha_service
from .redis_service import redis_service

logger = logging.getLogger(__name__)

# Cadência de follow-ups: attempt -> timedelta até o próximo
FOLLOWUP_DELAYS = {
    1: timedelta(minutes=10),
    2: timedelta(minutes=30),
    3: timedelta(hours=1),
    4: timedelta(hours=4),
    5: timedelta(hours=24),
    6: timedelta(hours=48),
    7: timedelta(hours=72),
}
MAX_FOLLOWUP_ATTEMPT = 7

# Lembretes de appointment em minutos antes do horário
REMINDER_THRESHOLDS = [
    ("reminder_4h_sent", 240),
    ("reminder_2h_sent", 120),
    ("reminder_1h_sent", 60),
    ("reminder_30min_sent", 30),
]


class SchedulerService:
    def __init__(self):
        self._scheduler: AsyncIOScheduler | None = None

    def start(self):
        """Inicia o APScheduler com os jobs de follow-up e lembretes."""
        try:
            self._scheduler = AsyncIOScheduler(timezone="America/Sao_Paulo")

            self._scheduler.add_job(
                self.check_followups,
                IntervalTrigger(minutes=1),
                id="check_followups",
                name="Verificar follow-ups pendentes",
                replace_existing=True,
            )

            self._scheduler.add_job(
                self.check_reminders,
                IntervalTrigger(minutes=5),
                id="check_reminders",
                name="Verificar lembretes de agendamentos",
                replace_existing=True,
            )

            self._scheduler.start()
            logger.info("✅ APScheduler iniciado com jobs: check_followups (1min), check_reminders (5min)")
        except Exception as e:
            logger.error(f"Erro ao iniciar APScheduler: {e}")

    def shutdown(self):
        """Para o scheduler graciosamente."""
        try:
            if self._scheduler and self._scheduler.running:
                self._scheduler.shutdown(wait=False)
                logger.info("APScheduler encerrado.")
        except Exception as e:
            logger.error(f"Erro ao encerrar APScheduler: {e}")

    # ------------------------------------------------------------------
    # FOLLOW-UPS
    # ------------------------------------------------------------------
    async def check_followups(self):
        """Verifica e envia follow-ups cujo scheduled_at já passou."""
        try:
            now = datetime.now(timezone.utc)
            async with AsyncSessionLocal() as db:
                stmt = select(FollowUp).where(
                    FollowUp.status == "pending",
                    FollowUp.scheduled_at <= now,
                )
                result = await db.execute(stmt)
                followups = result.scalars().all()

                for fu in followups:
                    try:
                        await self._send_followup(db, fu)
                    except Exception as e:
                        logger.error(f"Erro ao processar follow-up {fu.id}: {e}")

        except Exception as e:
            logger.error(f"Erro no job check_followups: {e}")

    async def _send_followup(self, db, fu: FollowUp):
        """Envia um follow-up individual."""
        from .openai_service import generate_ai_response

        # 1. Buscar client
        client = await db.get(Client, fu.client_id)
        if not client or not client.is_active:
            fu.status = "cancelled"
            await db.commit()
            return

        # 2. Verificar se bot ainda está ativo para este contato
        is_active = await redis_service.is_bot_active(fu.session_name, fu.chat_id)
        if not is_active:
            fu.status = "cancelled"
            await db.commit()
            return

        # 3. Buscar histórico de mensagens
        stmt = select(Message).where(
            Message.client_id == fu.client_id,
            Message.contact_number == fu.contact_number,
        ).order_by(Message.timestamp.desc()).limit(10)
        result = await db.execute(stmt)
        history = result.scalars().all()
        history.reverse()

        # 4. Montar prompt para gerar follow-up
        is_final = fu.attempt == MAX_FOLLOWUP_ATTEMPT
        if is_final:
            followup_instruction = (
                f"Você está enviando a ÚLTIMA mensagem de follow-up (tentativa {fu.attempt} de {MAX_FOLLOWUP_ATTEMPT}) para este contato. "
                "O contato não respondeu às mensagens anteriores. "
                "Encerre o contato de forma educada e profissional, de acordo com o tom do system_prompt. "
                "Informe que o atendimento será encerrado mas que o contato pode retornar quando quiser."
            )
        else:
            followup_instruction = (
                f"Você está enviando um follow-up (tentativa {fu.attempt} de {MAX_FOLLOWUP_ATTEMPT}) para este contato que parou de responder. "
                "Gere uma mensagem curta, natural e amigável de acompanhamento baseada no contexto da conversa. "
                "Não seja repetitivo. Varie o tom conforme o system_prompt."
            )

        messages = [{"role": "system", "content": client.system_prompt}]
        for msg in history:
            messages.append({"role": msg.role, "content": msg.content})

        messages.append({
            "role": "system",
            "content": f"[INSTRUÇÃO INTERNA — FOLLOW-UP] {followup_instruction}"
        })

        try:
            data = await generate_ai_response(messages=messages)
            ai_text = data["choices"][0]["message"].get("content", "")
        except Exception as e:
            logger.error(f"Erro ao gerar resposta de follow-up: {e}")
            return

        if not ai_text:
            logger.warning(f"Follow-up {fu.id}: IA retornou resposta vazia.")
            return

        # 5. Enviar via WAHA
        sent = await waha_service.send_text_message(
            session_name=fu.session_name,
            chat_id=fu.chat_id,
            text=ai_text,
        )

        if not sent:
            logger.error(f"Falha ao enviar follow-up {fu.id} via WAHA.")
            return

        # 6. Salvar mensagem no banco
        assistant_msg = Message(
            client_id=fu.client_id,
            contact_number=fu.contact_number,
            role="assistant",
            content=ai_text,
        )
        db.add(assistant_msg)

        # 7. Marcar follow-up como enviado
        fu.status = "sent"
        fu.sent_at = datetime.now(timezone.utc)
        await db.commit()

        logger.info(f"✅ Follow-up {fu.id} (tentativa {fu.attempt}) enviado para {fu.contact_number}")

        # 8. Se for o último, desativar bot
        if is_final:
            await redis_service.deactivate_bot(fu.session_name, fu.chat_id)
            logger.info(f"🔴 Bot desativado para {fu.session_name}:{fu.chat_id} após follow-up final.")
        else:
            # 9. Agendar próximo follow-up
            next_attempt = fu.attempt + 1
            next_delay = FOLLOWUP_DELAYS.get(next_attempt, timedelta(hours=24))
            next_scheduled = datetime.now(timezone.utc) + next_delay

            next_fu = FollowUp(
                client_id=fu.client_id,
                contact_number=fu.contact_number,
                chat_id=fu.chat_id,
                session_name=fu.session_name,
                scheduled_at=next_scheduled,
                attempt=next_attempt,
                status="pending",
            )
            db.add(next_fu)
            await db.commit()
            logger.info(f"📅 Próximo follow-up (tentativa {next_attempt}) agendado para {next_scheduled}")

    # ------------------------------------------------------------------
    # LEMBRETES DE AGENDAMENTO
    # ------------------------------------------------------------------
    async def check_reminders(self):
        """Verifica e envia lembretes de appointments próximos."""
        try:
            now = datetime.now(timezone.utc)
            async with AsyncSessionLocal() as db:
                # Buscar appointments futuros dentro das próximas 5h (cobre o maior threshold)
                max_window = now + timedelta(hours=5)
                stmt = select(Appointment).where(
                    Appointment.start_time > now,
                    Appointment.start_time <= max_window,
                )
                result = await db.execute(stmt)
                appointments = result.scalars().all()

                for appt in appointments:
                    try:
                        await self._check_appointment_reminders(db, appt, now)
                    except Exception as e:
                        logger.error(f"Erro ao processar lembrete para appointment {appt.id}: {e}")

        except Exception as e:
            logger.error(f"Erro no job check_reminders: {e}")

    async def _check_appointment_reminders(self, db, appt: Appointment, now: datetime):
        """Verifica e envia lembretes para um appointment específico."""
        from .openai_service import generate_ai_response

        minutes_until = (appt.start_time - now).total_seconds() / 60

        for field_name, threshold_minutes in REMINDER_THRESHOLDS:
            already_sent = getattr(appt, field_name)
            if already_sent:
                continue

            # Enviar lembrete quando estiver dentro da janela do threshold
            # (threshold - 5min de margem para o intervalo do job)
            if minutes_until <= threshold_minutes:
                # Buscar client
                client = await db.get(Client, appt.client_id)
                if not client or not client.is_active:
                    continue

                # Gerar mensagem de lembrete via IA
                time_label = self._format_time_label(threshold_minutes)
                start_formatted = appt.start_time.strftime("%d/%m/%Y às %H:%M")

                messages = [
                    {"role": "system", "content": client.system_prompt},
                    {
                        "role": "system",
                        "content": (
                            f"[INSTRUÇÃO INTERNA — LEMBRETE DE AGENDAMENTO] "
                            f"Envie um lembrete amigável e curto para o contato {appt.contact_name}. "
                            f"Ele tem um agendamento marcado para {start_formatted} (faltam {time_label}). "
                            f"Use o tom do system_prompt. Seja breve e objetivo."
                        ),
                    },
                ]

                try:
                    data = await generate_ai_response(messages=messages)
                    ai_text = data["choices"][0]["message"].get("content", "")
                except Exception as e:
                    logger.error(f"Erro ao gerar lembrete: {e}")
                    continue

                if not ai_text:
                    continue

                # Enviar via WAHA
                sent = await waha_service.send_text_message(
                    session_name=appt.session_name,
                    chat_id=appt.chat_id,
                    text=ai_text,
                )

                if sent:
                    setattr(appt, field_name, True)
                    await db.commit()
                    logger.info(f"🔔 Lembrete ({time_label}) enviado para {appt.contact_name} (appointment {appt.id})")
                else:
                    logger.error(f"Falha ao enviar lembrete para appointment {appt.id}")

    @staticmethod
    def _format_time_label(minutes: int) -> str:
        """Formata minutos em label legível."""
        if minutes >= 60:
            hours = minutes // 60
            return f"{hours}h"
        return f"{minutes}min"


# ------------------------------------------------------------------
# Funções auxiliares para uso externo (webhook)
# ------------------------------------------------------------------
async def schedule_followup(client_id: int, contact_number: str, chat_id: str, session_name: str):
    """Agenda o primeiro follow-up (4h a partir de agora). Cancela pendentes existentes."""
    try:
        async with AsyncSessionLocal() as db:
            # Cancelar follow-ups pendentes existentes
            await db.execute(
                update(FollowUp).where(
                    FollowUp.client_id == client_id,
                    FollowUp.contact_number == contact_number,
                    FollowUp.session_name == session_name,
                    FollowUp.status == "pending",
                ).values(status="cancelled")
            )

            # Criar novo follow-up
            scheduled_at = datetime.now(timezone.utc) + FOLLOWUP_DELAYS[1]
            new_fu = FollowUp(
                client_id=client_id,
                contact_number=contact_number,
                chat_id=chat_id,
                session_name=session_name,
                scheduled_at=scheduled_at,
                attempt=1,
                status="pending",
            )
            db.add(new_fu)
            await db.commit()
            logger.info(f"📅 Follow-up agendado para {contact_number} em {scheduled_at}")
    except Exception as e:
        logger.error(f"Erro ao agendar follow-up: {e}")


async def cancel_pending_followups(client_id: int, contact_number: str, session_name: str):
    """Cancela todos os follow-ups pendentes de um contato."""
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(
                update(FollowUp).where(
                    FollowUp.client_id == client_id,
                    FollowUp.contact_number == contact_number,
                    FollowUp.session_name == session_name,
                    FollowUp.status == "pending",
                ).values(status="cancelled")
            )
            await db.commit()
            logger.info(f"❌ Follow-ups pendentes cancelados para {contact_number}")
    except Exception as e:
        logger.error(f"Erro ao cancelar follow-ups: {e}")


async def save_appointment(
    client_id: int,
    contact_number: str,
    chat_id: str,
    session_name: str,
    contact_name: str,
    start_time: datetime,
    end_time: datetime,
    gcal_event_id: str = None,
):
    """Salva um appointment no banco de dados."""
    try:
        async with AsyncSessionLocal() as db:
            appt = Appointment(
                client_id=client_id,
                contact_number=contact_number,
                chat_id=chat_id,
                session_name=session_name,
                contact_name=contact_name,
                start_time=start_time,
                end_time=end_time,
                gcal_event_id=gcal_event_id,
            )
            db.add(appt)
            await db.commit()
            logger.info(f"💾 Appointment salvo para {contact_name} em {start_time}")
    except Exception as e:
        logger.error(f"Erro ao salvar appointment: {e}")


# Singleton
scheduler_service = SchedulerService()
