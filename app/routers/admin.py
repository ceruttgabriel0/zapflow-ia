import asyncio
import random
import logging
from typing import List, Optional
from fastapi import APIRouter, Depends, BackgroundTasks, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from ..database import get_db
from ..models.client import Client
from ..services.waha_service import waha_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])


# --- Schemas ---

class ClientCreate(BaseModel):
    name: str
    waha_session_name: str
    system_prompt: str
    gcal_calendar_id: str = ""

class ClientUpdate(BaseModel):
    name: Optional[str] = None
    system_prompt: Optional[str] = None
    gcal_calendar_id: Optional[str] = None
    is_active: Optional[bool] = None

class BroadcastRequest(BaseModel):
    client_id: int
    numbers: List[str]
    message_template: str


# --- Endpoints de Clientes (Tenants) ---

@router.post("/clients", status_code=201)
async def create_client(data: ClientCreate, db: AsyncSession = Depends(get_db)):
    """Cadastra um novo cliente (empresa) no sistema."""
    client = Client(
        name=data.name,
        waha_session_name=data.waha_session_name,
        system_prompt=data.system_prompt,
        gcal_calendar_id=data.gcal_calendar_id
    )
    db.add(client)
    await db.commit()
    await db.refresh(client)
    return {"id": client.id, "name": client.name, "session": client.waha_session_name}


@router.get("/clients")
async def list_clients(db: AsyncSession = Depends(get_db)):
    """Lista todos os clientes cadastrados."""
    result = await db.execute(select(Client))
    clients = result.scalars().all()
    return [
        {
            "id": c.id,
            "name": c.name,
            "waha_session_name": c.waha_session_name,
            "gcal_calendar_id": c.gcal_calendar_id,
            "is_active": c.is_active
        }
        for c in clients
    ]


@router.put("/clients/{client_id}")
async def update_client(client_id: int, data: ClientUpdate, db: AsyncSession = Depends(get_db)):
    """Atualiza dados de um cliente (ex: mudar system_prompt, ativar/desativar)."""
    result = await db.execute(select(Client).where(Client.id == client_id))
    client = result.scalar_one_or_none()
    if not client:
        raise HTTPException(status_code=404, detail="Cliente nao encontrado")

    if data.name is not None:
        client.name = data.name
    if data.system_prompt is not None:
        client.system_prompt = data.system_prompt
    if data.gcal_calendar_id is not None:
        client.gcal_calendar_id = data.gcal_calendar_id
    if data.is_active is not None:
        client.is_active = data.is_active

    await db.commit()
    return {"message": "Cliente atualizado com sucesso"}


@router.delete("/clients/{client_id}")
async def delete_client(client_id: int, db: AsyncSession = Depends(get_db)):
    """Remove um cliente do sistema."""
    result = await db.execute(select(Client).where(Client.id == client_id))
    client = result.scalar_one_or_none()
    if not client:
        raise HTTPException(status_code=404, detail="Cliente nao encontrado")
    await db.delete(client)
    await db.commit()
    return {"message": "Cliente removido com sucesso"}


# --- Broadcast ---

async def run_broadcast(session_name: str, numbers: List[str], message_template: str):
    for number in numbers:
        chat_id = f"{number}@c.us" if "@c.us" not in number else number
        success = await waha_service.send_text_message(
            session_name=session_name,
            chat_id=chat_id,
            text=message_template
        )
        if success:
            logger.info(f"Broadcast enviado para {number}")
        else:
            logger.error(f"Falha no broadcast para {number}")

        # Delay aleatorio para evitar banimento (15 a 45 seg)
        delay = random.randint(15, 45)
        await asyncio.sleep(delay)


@router.post("/broadcast")
async def start_broadcast(
    request: BroadcastRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db)
):
    """Dispara mensagem em massa para uma lista de numeros."""
    result = await db.execute(
        select(Client).where(Client.id == request.client_id, Client.is_active == True)
    )
    client = result.scalar_one_or_none()
    if not client:
        raise HTTPException(status_code=404, detail="Cliente nao encontrado ou inativo")

    background_tasks.add_task(
        run_broadcast,
        session_name=client.waha_session_name,
        numbers=request.numbers,
        message_template=request.message_template
    )
    return {"message": f"Broadcast iniciado para {len(request.numbers)} contatos."}
