# ollama run llama3.2:3b -> To run model

from fastapi import FastAPI, Request
from ddgs import DDGS
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
import asyncio, httpx, json

OLLAMA_API_URL = "http://localhost:11434"
MODEL_NAME = "llama3.2:3b"
TIMEOUT = 60  # seconds
MAX_RETRIES = 5  # Maximum number of search results to return
RETRY_DELAY = 2  # seconds

app = FastAPI(title="Ollama Chat API Service")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

HISTORY: list[dict]=[]

def perform_websearch(query: str, max_results: int= 3):
    """Perform a live web search using DuckDuckGo and formats context for llama."""
    try:
        results = DDGS().text(query, max_results=max_results)
        print(f"\n[DEBUG] Web Search Triggered for: '{query}'")
        print(f"[DEBUG] Found {len(results)} results from the web!\n")
        if not results:
            return ""
        search_context = "\n ---LIVE WEB SEARCH CONTEXT---\n"
        for idx, item in enumerate(results, start=1):
            title = item.get("title", "No Title")
            body = item.get("body", "No Description")
            url = item.get("href", "")
            search_context += f"[{idx}] {title}\n Snippet {body}\n URL: {url}\n\n"
        search_context += "---END OF SEARCH CONTEXT---\n"
        return search_context
    except Exception as e:
        print(f"Error during web search: {e}")
        return ""

async def check_model_available(client: httpx.AsyncClient, model_name: str) -> bool:
    """Checks if the requested model is pulled and available in Ollama."""
    try:
        response = await client.get(f"{OLLAMA_API_URL}/api/tags", timeout= 10)
        response.raise_for_status()
        models = [m["name"] for m in response.json().get("models", [])]
        return any(model_name == m or m.startswith(model_name) for m in models)
    except httpx.RequestError as e:
        print(f"Error checking model availability: {e}")
        return False

@app.get("/health")
async def health_check():
    """Health check endpoint to verify connection to Ollama and GPU availability."""
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(f"{OLLAMA_API_URL}/api/tags", timeout=10)
            response.raise_for_status()
            loaded = await check_model_available(client, MODEL_NAME)
            return JSONResponse({"ok" : True, "model": MODEL_NAME, "model_loaded": loaded})
        except httpx.RequestError as e:
            return JSONResponse({"ok": False, "model_loaded": False})


@app.post("/reset")
async def reset_history():
    """Reset the conversation history."""
    global HISTORY
    HISTORY.clear()
    return JSONResponse({"ok": True, "message": "Conversation history reset."})

@app.post("/chat")
async def chat(request: Request):
    """Main Sreaming endpoint with live web search and automated GPU offloading"""
    body = await request.json()
    user_message = (body.get("message") or "").strip()
    web_search= body.get("web_search", False)
    if not user_message:
        return JSONResponse({"ok": False, "error": "Message is required."}, status_code=400)

    #1. Fetch live web search context if web_search is enabled
    search_context = ""
    if web_search:
        search_context = await asyncio.to_thread(perform_websearch, user_message, MAX_RETRIES)

    #2. Build conversation history
    context_text=""
    for entry in HISTORY:
        context_text += f"User: {entry['user']}\n Assistant: {entry['assistant']}\n"

    #if search context is available, append it to the context_text
    if search_context:
        full_prompt = (
            f"System: You are an intelligent AI assistant with live internet access. "
            f"Use the following real-time web search results to answer the user's question accurately.\n"
            f"{search_context}\n"
            f"{context_text}User: {user_message}\nAssistant:")
    else:
        full_prompt = (
            f"System: You are an intelligent AI assistant. "
            f"Use the following conversation history to answer the user's question accurately.\n"
            f"{context_text}User: {user_message}\nAssistant:")

    async def generate():
        url = f"{OLLAMA_API_URL}/api/generate"
        payload = {
            "model": MODEL_NAME,
            "prompt": full_prompt,
            "stream": True,
            "keep_alive": "5m",  # 👈 CHANGED: Keeps model in RTX 3050 VRAM for 5 mins for INSTANT replies
            "options": {"num_predict": 512, "temperature": 0.7},
        }
        full_response = ""
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                    async with client.stream("POST", url, json = payload) as response:
                        if response.status_code != 200:
                            raw = await response.aread()
                            try:
                                err = json.loads(raw).get("error", raw.decode())
                            except Exception:
                                err = raw.decode(errors="ignore")
                            yield f"data: [ERROR] Ollama API returned status {response.status_code}: {err}\n\n"
                            return
                        async for line in response.aiter_lines():
                            if not line:
                                continue
                            chunk = json.loads(line)
                            token = chunk.get("response", "")
                            full_response += token
                            yield token
                            if chunk.get("done"):
                                HISTORY.append(
                                    {
                                        "user": user_message,
                                        "assistant": full_response
                                    }
                                )
                                return 
            except httpx.ConnectError:
                if attempt < MAX_RETRIES:
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



