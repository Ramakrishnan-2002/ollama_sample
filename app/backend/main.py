import os
import json
import asyncio
import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from ddgs import DDGS
import redis.asyncio as redis

# ─── Config ──────────────────────────────────────────────────────────
OLLAMA_API_URL = os.getenv("OLLAMA_API_URL", "http://localhost:11434")
MODEL_NAME     = os.getenv("MODEL_NAME", "llama3.2:3b")
REDIS_URL      = os.getenv("REDIS_URL", "redis://localhost:6379/0")
TIMEOUT        = int(os.getenv("TIMEOUT", "60"))
MAX_SEARCH_RES = int(os.getenv("MAX_SEARCH_RESULTS", "3"))
MAX_API_RETRY  = int(os.getenv("MAX_API_RETRIES", "5"))
RETRY_DELAY    = int(os.getenv("RETRY_DELAY", "2"))
MAX_HISTORY    = int(os.getenv("MAX_HISTORY", "20"))
HISTORY_TTL    = int(os.getenv("HISTORY_TTL", "604800"))

# ─── FastAPI ─────────────────────────────────────────────────────────
app = FastAPI(title="Ollama Chat API Service")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Redis ───────────────────────────────────────────────────────────
redis_client = redis.from_url(REDIS_URL, decode_responses=True)


def perform_websearch(query: str, max_results: int = 3) -> str:
    try:
        results = DDGS().text(query, max_results=max_results)
        print(f"\n[DEBUG] Web Search: '{query}' -> {len(results)} results\n")
        if not results:
            return ""
        ctx = "\n---LIVE WEB SEARCH CONTEXT---\n"
        for idx, item in enumerate(results, start=1):
            title = item.get("title", "No Title")
            body  = item.get("body", "No Description")
            url   = item.get("href", "")
            ctx  += f"[{idx}] {title}\nSnippet: {body}\nURL: {url}\n\n"
        ctx += "---END OF SEARCH CONTEXT---\n"
        return ctx
    except Exception as e:
        print(f"[ERROR] Web search failed: {e}")
        return ""


async def check_model_available(client: httpx.AsyncClient, model_name: str) -> bool:
    try:
        r = await client.get(f"{OLLAMA_API_URL}/api/tags", timeout=10)
        r.raise_for_status()
        models = [m["name"] for m in r.json().get("models", [])]
        return any(model_name == m or m.startswith(model_name) for m in models)
    except httpx.RequestError as e:
        print(f"[ERROR] Model check failed: {e}")
        return False


# ─── Redis helpers ───────────────────────────────────────────────────
async def get_history(session_id: str) -> list[dict]:
    key = f"chat:history:{session_id}"
    raw = await redis_client.lrange(key, 0, -1)
    return [json.loads(item) for item in raw]


async def add_history(session_id: str, user_msg: str, assistant_msg: str):
    key   = f"chat:history:{session_id}"
    entry = json.dumps({"user": user_msg, "assistant": assistant_msg})
    await redis_client.rpush(key, entry)
    await redis_client.ltrim(key, -MAX_HISTORY, -1)
    await redis_client.expire(key, HISTORY_TTL)


async def clear_history(session_id: str):
    await redis_client.delete(f"chat:history:{session_id}")


# ─── Endpoints ───────────────────────────────────────────────────────
@app.get("/health")
async def health_check():
    try:
        await redis_client.ping()
        redis_ok = True
    except Exception as e:
        redis_ok = False
        print(f"[ERROR] Redis ping failed: {e}")

    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(f"{OLLAMA_API_URL}/api/tags", timeout=10)
            r.raise_for_status()
            loaded = await check_model_available(client, MODEL_NAME)
            return JSONResponse({
                "ok": True,
                "model": MODEL_NAME,
                "model_loaded": loaded,
                "redis_connected": redis_ok,
            })
        except httpx.RequestError as e:
            return JSONResponse({
                "ok": False,
                "model_loaded": False,
                "redis_connected": redis_ok,
                "error": str(e),
            })


@app.post("/reset")
async def reset_history(request: Request):
    body = await request.json()
    session_id = body.get("session_id", "default")
    await clear_history(session_id)
    return JSONResponse({
        "ok": True,
        "message": "Conversation history reset.",
        "session_id": session_id,
    })


@app.post("/chat")
async def chat(request: Request):
    body       = await request.json()
    user_msg   = (body.get("message") or "").strip()
    web_search = body.get("web_search", False)
    session_id = body.get("session_id", "default")

    if not user_msg:
        return JSONResponse(
            {"ok": False, "error": "Message is required."},
            status_code=400,
        )

    search_ctx = ""
    if web_search:
        search_ctx = await asyncio.to_thread(perform_websearch, user_msg, MAX_SEARCH_RES)

    history = await get_history(session_id)
    context_text = ""
    for entry in history:
        context_text += f"User: {entry['user']}\nAssistant: {entry['assistant']}\n"

    if search_ctx:
        full_prompt = (
            "System: You are an intelligent AI assistant with live internet access. "
            "Use the following real-time web search results to answer accurately.\n"
            f"{search_ctx}\n"
            f"{context_text}User: {user_msg}\nAssistant:"
        )
    else:
        full_prompt = (
            "System: You are an intelligent AI assistant. "
            "Use the conversation history to answer accurately.\n"
            f"{context_text}User: {user_msg}\nAssistant:"
        )

    async def generate():
        url     = f"{OLLAMA_API_URL}/api/generate"
        payload = {
            "model": MODEL_NAME,
            "prompt": full_prompt,
            "stream": True,
            "keep_alive": "5m",
            "options": {"num_predict": 512, "temperature": 0.7},
        }
        full_response = ""

        for attempt in range(1, MAX_API_RETRY + 1):
            try:
                async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                    async with client.stream("POST", url, json=payload) as resp:
                        if resp.status_code != 200:
                            raw = await resp.aread()
                            try:
                                err = json.loads(raw).get("error", raw.decode())
                            except Exception:
                                err = raw.decode(errors="ignore")
                            yield f"[ERROR] Ollama returned {resp.status_code}: {err}"
                            return

                        async for line in resp.aiter_lines():
                            if not line:
                                continue
                            chunk = json.loads(line)
                            token = chunk.get("response", "")
                            full_response += token
                            yield token
                            if chunk.get("done"):
                                await add_history(session_id, user_msg, full_response)
                                return

            except httpx.ConnectError:
                if attempt < MAX_API_RETRY:
                    await asyncio.sleep(RETRY_DELAY * attempt)
                    continue
                yield "[Error: Could not connect to Ollama. Make sure Ollama is running!]"
                return
            except httpx.TimeoutException:
                yield "[Error: Request timed out]"
                return
            except Exception as err:
                yield f"[Unexpected error: {err}]"
                return

    return StreamingResponse(generate(), media_type="text/plain")