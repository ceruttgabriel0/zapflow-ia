# ZapFlow IA

> **Backend multi-tenant para automação de atendimento via WhatsApp com IA e Google Calendar**

![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=flat&logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-009688?style=flat&logo=fastapi&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-15+-4169E1?style=flat&logo=postgresql&logoColor=white)
![Redis](https://img.shields.io/badge/Redis-7+-DC382D?style=flat&logo=redis&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-ready-2496ED?style=flat&logo=docker&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green?style=flat)

---

## O que é o ZapFlow IA?

O ZapFlow IA é um backend de automação de atendimento via WhatsApp. Ele conecta um número de WhatsApp a modelos de IA (OpenAI / OpenRouter) e permite atender clientes de forma automática, com contexto de conversa, suporte a mídia (áudio e imagem), follow-ups automáticos e integração com Google Calendar para agendamentos.

O sistema é **multi-tenant**: uma única instância do backend atende múltiplas empresas simultaneamente, cada uma com seu próprio número de WhatsApp, prompt de IA personalizado e agenda separada.

```
Cliente → WhatsApp → WAHA → ZapFlow IA → IA (OpenAI/OpenRouter) → Resposta
                                       ↕
                              PostgreSQL + Redis
                                       ↕
                              Google Calendar (opcional)
```

---

## Funcionalidades

- **Webhook para WAHA**: recebe mensagens em tempo real via `POST /webhook/waha`
- **Suporte a texto, áudio e imagem**: transcrição de áudio e análise de imagem via IA multimodal
- **Multi-tenant**: múltiplas empresas em uma só instância, cada uma com seu `system_prompt`
- **Debounce de mensagens**: agrupa mensagens enviadas rapidamente (5s) antes de processar
- **Handoff humano**: quando o atendente responde manualmente, o bot pausa automaticamente
- **Follow-ups automáticos**: reengajamento quando o lead para de responder
- **Lembretes de agenda**: integração com Google Calendar para notificações via WhatsApp
- **Fallback de IA**: se o provedor principal falhar, usa automaticamente o secundário
- **API REST completa**: endpoints de administração para gerenciar clientes e disparar mensagens em massa
- **Docker pronto**: sobe todo o ambiente com um único comando

---

## Stack

| Camada         | Tecnologia                        |
|----------------|------------------------------------|
| API            | FastAPI + Uvicorn                 |
| Banco de dados | PostgreSQL + SQLAlchemy Async     |
| Cache / Estado | Redis (asyncio)                   |
| Agendamento    | APScheduler                       |
| IA             | OpenRouter / OpenAI               |
| WhatsApp       | WAHA (conector WhatsApp)          |
| Agenda         | Google Calendar API               |
| Infraestrutura | Docker + Docker Compose           |

---

## Pré-requisitos

- **Docker + Docker Compose** (recomendado) **ou** Python 3.11+ com PostgreSQL e Redis rodando localmente
- Uma instância do [WAHA](https://waha.devlike.pro/) rodando
- Chave de API da [OpenAI](https://platform.openai.com) ou [OpenRouter](https://openrouter.ai)
- (Opcional) Service account do Google Cloud para integração com Google Calendar

---

## Instalação com Docker (recomendado)

### 1. Clone o repositório

```bash
git clone https://github.com/seu-usuario/zapflow-ia.git
cd zapflow-ia
```

### 2. Configure o ambiente

```bash
cp .env.example .env
```

Edite o `.env` e preencha as variáveis:

```env
DATABASE_URL=postgresql+asyncpg://postgres:postgres@db:5432/whatsapp_ia
POSTGRES_USER=postgres
POSTGRES_PASSWORD=sua_senha_aqui
POSTGRES_DB=whatsapp_ia

AI_PROVIDER=openrouter
AI_PROVIDER_FALLBACK=openai
OPENROUTER_API_KEY=sua_chave_aqui
OPENAI_API_KEY=sua_chave_aqui

REDIS_URL=redis://redis:6379/0

WAHA_API_URL=http://waha:3000
WAHA_API_KEY=sua_chave_waha_aqui

GOOGLE_SERVICE_ACCOUNT_FILE=app/service_account.json
```

### 3. Suba os serviços

```bash
docker compose up -d
```

### 4. Acesse

- API: [http://localhost:8000](http://localhost:8000)
- Swagger: [http://localhost:8000/docs](http://localhost:8000/docs)

---

## Instalação local (sem Docker)

```bash
python -m venv .venv
source .venv/bin/activate      # Linux/macOS
# .venv\Scripts\activate       # Windows

pip install -r requirements.txt

cp .env.example .env
# edite o .env

uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

---

## Configurando o WAHA

1. Acesse o painel do WAHA e crie uma sessão
2. Escaneie o QR Code com o número de WhatsApp da empresa
3. Vá em **Settings → Webhook** e configure:
   - **URL**: `http://SEU_HOST:8000/webhook/waha`
   - **Eventos**: marque `message` e `message.any`
4. Salve

> Em ambiente local, use o [ngrok](https://ngrok.com) para expor o `localhost` ao WAHA.

---

## Endpoints

### Healthcheck

```
GET /
```

### Webhook

```
POST /webhook/waha
```
Endpoint que recebe todos os eventos do WAHA. Configure no painel do WAHA.

### Administração

| Método | Endpoint              | Descrição                        |
|--------|-----------------------|-----------------------------------|
| POST   | `/admin/clients`      | Cadastra novo cliente/empresa     |
| GET    | `/admin/clients`      | Lista todos os clientes           |
| PUT    | `/admin/clients/{id}` | Atualiza dados de um cliente      |
| DELETE | `/admin/clients/{id}` | Remove um cliente                 |
| POST   | `/admin/broadcast`    | Dispara mensagem em massa         |

#### Exemplo: criar um cliente

```bash
curl -X POST http://localhost:8000/admin/clients \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Empresa X",
    "waha_session_name": "empresa_x",
    "system_prompt": "Você é a assistente virtual da Empresa X. Atenda com educação e objetividade.",
    "gcal_calendar_id": ""
  }'
```

---

## Como o fluxo funciona

1. Cliente manda mensagem no WhatsApp
2. WAHA entrega para `POST /webhook/waha`
3. O sistema identifica a empresa pelo `session_name`
4. Aguarda 5s de debounce (caso o cliente mande mais mensagens em sequência)
5. Salva a mensagem no PostgreSQL
6. Monta o histórico da conversa e chama a IA com o `system_prompt` da empresa
7. Salva a resposta e envia de volta via WAHA
8. Agenda um follow-up automático

**Handoff humano:** se o atendente responder manualmente (`fromMe=true`), o bot pausa para aquele contato. Retoma quando o contato responder novamente.

---

## Ativar o bot por conversa

O bot é ativado quando o contato envia a mensagem gatilho. O padrão é:

```
Teste Robo
```

Para alterar, edite `app/routers/webhook.py`:

```python
TRIGGER_MESSAGE = "Olá"  # mude para a frase que preferir
```

---

## Estrutura do projeto

```
zapflow-ia/
├── app/
│   ├── main.py                  # Entrypoint FastAPI
│   ├── config.py                # Configurações via Pydantic Settings
│   ├── database.py              # Setup SQLAlchemy async
│   ├── models/
│   │   ├── client.py            # Clientes (multi-tenant)
│   │   ├── message.py           # Mensagens
│   │   ├── appointment.py       # Agendamentos
│   │   └── followup.py          # Follow-ups
│   ├── routers/
│   │   ├── webhook.py           # Recebe eventos do WAHA
│   │   └── admin.py             # Endpoints de administração
│   └── services/
│       ├── openai_service.py    # Integração OpenAI / OpenRouter
│       ├── waha_service.py      # Envio de mensagens via WAHA
│       ├── redis_service.py     # Estado do bot e debounce
│       ├── scheduler_service.py # Follow-ups (APScheduler)
│       └── gcal_service.py      # Google Calendar
├── .env.example                 # Modelo de variáveis de ambiente
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── LICENSE
```

---

## Variáveis de ambiente

| Variável                      | Descrição                                   |
|-------------------------------|---------------------------------------------|
| `DATABASE_URL`                | URL de conexão PostgreSQL                   |
| `POSTGRES_USER`               | Usuário do banco                            |
| `POSTGRES_PASSWORD`           | Senha do banco                              |
| `POSTGRES_DB`                 | Nome do banco                               |
| `AI_PROVIDER`                 | Provedor principal (`openrouter`/`openai`)  |
| `AI_PROVIDER_FALLBACK`        | Provedor de fallback                        |
| `OPENROUTER_API_KEY`          | Chave API do OpenRouter                     |
| `OPENAI_API_KEY`              | Chave API da OpenAI                         |
| `REDIS_URL`                   | URL do Redis                                |
| `WAHA_API_URL`                | URL base do WAHA                            |
| `WAHA_API_KEY`                | API Key do WAHA                             |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | Caminho do JSON da service account Google   |

> ⚠️ **Nunca suba o `.env` ou o `service_account.json` para o GitHub.**

---

## Segurança

- Todas as credenciais ficam no `.env`, que está no `.gitignore`
- O `.env.example` contém apenas placeholders
- O `service_account.json` também está no `.gitignore`
- Em produção, use variáveis de ambiente do servidor ou um secrets manager

---

## Contribuindo

1. Faça um fork do repositório
2. Crie uma branch: `git checkout -b feature/minha-feature`
3. Commit: `git commit -m 'feat: adiciona minha feature'`
4. Push: `git push origin feature/minha-feature`
5. Abra um Pull Request

---

## Licença

Este projeto está licenciado sob a [MIT License](LICENSE).
