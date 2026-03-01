import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from .database import engine, Base
from .routers import webhook
from .config import settings
from .routers import admin

# Configuração básica de logs
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Gerencia startup e shutdown da aplicação."""
    # --- STARTUP ---
    logger.info("Iniciando a aplicação...")
    try:
        # Importar models para que o Base.metadata conheça todas as tabelas
        from .models import followup  # noqa: F401
        from .models import appointment  # noqa: F401

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Tabelas do banco de dados verificadas/criadas com sucesso.")
    except Exception as e:
        logger.error(f"Erro ao inicializar o banco de dados: {e}")

    # Iniciar APScheduler
    try:
        from .services.scheduler_service import scheduler_service
        scheduler_service.start()
    except Exception as e:
        logger.error(f"Erro ao iniciar APScheduler: {e}")

    yield

    # --- SHUTDOWN ---
    try:
        from .services.scheduler_service import scheduler_service
        scheduler_service.shutdown()
    except Exception as e:
        logger.error(f"Erro ao encerrar APScheduler: {e}")

    logger.info("Aplicação encerrada.")


app = FastAPI(
    title="ZapFlow IA - Backend WhatsApp Multi-Tenant",
    description="Backend multi-tenant para automação de atendimento via WhatsApp com IA e Google Calendar",
    version="1.0.0",
    lifespan=lifespan,
)

# Inclusão de rotas
app.include_router(webhook.router)
app.include_router(admin.router)


@app.get("/")
async def root():
    return {
        "status": "online",
        "service": "ZapFlow IA Backend",
        "documentation": "/docs"
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
