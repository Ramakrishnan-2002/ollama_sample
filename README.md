# 🦙 Ollama Chat — Local AI with Web Search

A sleek, real-time chat interface for running local LLMs via **Ollama**, with optional **live DuckDuckGo web search**, persistent conversation history via **Redis**, and a modern dark-mode UI.

---

## ✨ Features

- **Local LLM Inference** — Runs entirely on your machine via Ollama (e.g. `llama3.2:3b`)
- **🔴 Live Web Search** — Toggle DuckDuckGo search to ground answers in real-time web data
- **💬 Persistent Chat History** — Redis-backed sessions survive page refreshes and server restarts
- **🎨 Modern Dark UI** — Gradient accents, syntax-highlighted code blocks with copy buttons, typing indicators, suggestion chips
- **📦 Dockerized** — One-command setup with Docker Compose
- **🔍 Redis Insight GUI** — Built-in visual Redis browser for debugging

---

## 🏗️ Architecture

```
┌─────────────┐      ┌──────────────┐      ┌─────────────┐
│   Browser   │◄────►│  FastAPI     │◄────►│   Redis     │
│(frontend/)  │      │  (backend/)  │      │  (History)  │
└─────────────┘      └──────┬───────┘      └─────────────┘
                            │
                            ▼
                     ┌──────────────┐
                     │    Ollama    │
                     │  (Local LLM) │
                     └──────────────┘
                            │
                            ▼ (when Web Search ON)
                     ┌──────────────┐
                     │  DuckDuckGo  │
                     │  (Web Search)│
                     └──────────────┘
```

---

## 📁 Project Structure

```
ollama_sample/
├── app/
│   ├── backend/
│   │   └── main.py              # FastAPI backend
│   └── frontend/
│       └── index.html           # Chat UI (single file)
├── venv/                        # Python virtual environment
├── dockercompose.yml            # Docker Compose stack
├── Dockerfile                   # API container image
├── env.example                  # Environment variables template
├── requirements.txt             # Python dependencies
└── .gitignore
```

---

## 🚀 Quick Start (Docker)

### Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/)
- [Ollama](https://ollama.com/) installed and running on your host machine
- A pulled model: `ollama pull llama3.2:3b`

### 1. Clone the repo

```bash
git clone https://github.com/yourusername/ollama-chat.git
cd ollama_sample
```

### 2. Configure environment

```bash
cp env.example .env
```

Edit `.env` if needed (defaults work for most setups):

```env
OLLAMA_API_URL=http://host.docker.internal:11434
MODEL_NAME=llama3.2:3b
REDIS_URL=redis://redis:6379/0
```

> On Windows with Docker Desktop, `host.docker.internal:11434` reaches your native Ollama install. If it fails, replace with your LAN IP (e.g. `192.168.1.xxx:11434`).

### 3. Start the stack

```bash
docker-compose -f dockercompose.yml --env-file .env up --build -d
```

| Service | URL | Purpose |
|---------|-----|---------|
| FastAPI Backend | http://localhost:8000 | Chat API + health checks |
| Redis | `redis://localhost:6379` | Session persistence |
| Redis Insight | http://localhost:5540 | Visual Redis browser |

### 4. Open the Chat UI

Open `app/frontend/index.html` directly in your browser, or serve it:

```bash
cd app/frontend
python -m http.server 8080
# Visit http://localhost:8080
```

---

## 🛠️ Manual Setup (No Docker)

### Prerequisites

- Python 3.11+
- Redis server running locally
- Ollama running locally

### 1. Install dependencies

```bash
python -m venv venv

# Windows
venv\Scripts\activate
# macOS/Linux
source venv/bin/activate

pip install -r requirements.txt
```

### 2. Start Redis

```bash
# Windows (WSL or native Redis)
redis-server

# Or via Docker
docker run -d -p 6379:6379 --name redis redis:7-alpine
```

### 3. Run the API

```bash
# Windows PowerShell
$env:OLLAMA_API_URL="http://localhost:11434"
$env:MODEL_NAME="llama3.2:3b"
$env:REDIS_URL="redis://localhost:6379/0"

uvicorn app.backend.main:app --host 0.0.0.0 --port 8000 --reload
```

> **Note:** If running manually, update `API_BASE_URL` in `app/frontend/index.html` to `http://127.0.0.1:8000`.

---

## 🔌 API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET`  | `/health` | Check Ollama + Redis + model status |
| `POST` | `/chat` | Stream a chat response |
| `POST` | `/reset` | Clear session history |

### Example Request

```bash
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{
    "message": "What is quantum computing?",
    "web_search": true,
    "session_id": "sess_abc123"
  }'
```

---

## 🔍 Verifying Redis Data

### Option 1: Redis Insight (GUI)

1. Open http://localhost:5540
2. Click **"Add Redis database"**
3. Connection URL: `redis://redis:6379`
4. Browse **Browser** tab → look for `chat:history:*` keys

### Option 2: CLI

```bash
# List all session keys
docker exec ollama-chat-redis redis-cli KEYS "chat:history:*"

# View a specific session's messages
docker exec ollama-chat-redis redis-cli LRANGE "chat:history:sess_xxx" 0 -1

# Check TTL (seconds until auto-expiry)
docker exec ollama-chat-redis redis-cli TTL "chat:history:sess_xxx"
```

---

## ⚙️ Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OLLAMA_API_URL` | `http://localhost:11434` | Ollama API endpoint |
| `MODEL_NAME` | `llama3.2:3b` | LLM model to use |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection string |
| `TIMEOUT` | `60` | Ollama request timeout (seconds) |
| `MAX_SEARCH_RESULTS` | `3` | DuckDuckGo results per query |
| `MAX_API_RETRIES` | `5` | Retry attempts for Ollama connection |
| `RETRY_DELAY` | `2` | Delay between retries (seconds) |
| `MAX_HISTORY` | `20` | Max conversation turns stored per session |
| `HISTORY_TTL` | `604800` | Session expiry in seconds (7 days) |

---

## 🐛 Troubleshooting

### "Could not connect to Ollama" from Docker

Docker containers cannot reach host `localhost`. Use `host.docker.internal` (Docker Desktop) or your LAN IP:

```powershell
# Find your LAN IP
ipconfig
# Use the IPv4 Address, e.g. 192.168.1.42
```

Update `OLLAMA_API_URL` in `.env`:
```env
OLLAMA_API_URL=http://192.168.1.42:11434
```

Also ensure Ollama listens on all interfaces:
```powershell
$env:OLLAMA_HOST="0.0.0.0"
ollama serve
```

### Redis Insight shows "No databases"

Use `redis://redis:6379` as the connection URL. Do **not** use `127.0.0.1` — that points to the container itself, not the Redis service.

### Frontend can't reach backend

Ensure `API_BASE_URL` in `app/frontend/index.html` matches your API URL:
```js
const API_BASE_URL = "http://127.0.0.1:8000";
```

---

## 📝 License

MIT License — feel free to use, modify, and distribute.

---

## 🙏 Acknowledgements

- [Ollama](https://ollama.com/) — Local LLM runtime
- [FastAPI](https://fastapi.tiangolo.com/) — Python web framework
- [Redis](https://redis.io/) — In-memory data store
- [DuckDuckGo Search](https://github.com/deedy5/duckduckgo-search) — Web search API
- [Marked.js](https://marked.js.org/) & [Highlight.js](https://highlightjs.org/) — Markdown rendering