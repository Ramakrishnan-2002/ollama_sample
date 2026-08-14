import os
import json
import asyncio
import io
import httpx
from typing import List
from fastapi import FastAPI, Request, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

# Correct import for DuckDuckGo search library
from ddgs import DDGS

import redis.asyncio as redis
import chromadb
import pypdf
import docx


# ─── Config ──────────────────────────────────────────────────────────
OLLAMA_API_URL = os.getenv("OLLAMA_API_URL", "http://localhost:11434")
MODEL_NAME     = os.getenv("MODEL_NAME", "llama3.2:3b")
EMBED_MODEL    = os.getenv("EMBED_MODEL", "nomic-embed-text")
REDIS_URL      = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CHROMA_PATH    = os.getenv("CHROMA_PATH", "/data/chroma_db")
TIMEOUT        = int(os.getenv("TIMEOUT", "60"))
MAX_SEARCH_RES = int(os.getenv("MAX_SEARCH_RESULTS", "3"))
MAX_API_RETRY  = int(os.getenv("MAX_API_RETRIES", "5"))
RETRY_DELAY    = int(os.getenv("RETRY_DELAY", "2"))
MAX_HISTORY    = int(os.getenv("MAX_HISTORY", "20"))
HISTORY_TTL    = int(os.getenv("HISTORY_TTL", "604800"))

# Ensure directory structure exists for ChromaDB
os.makedirs(CHROMA_PATH, exist_ok=True)

# ─── FastAPI Setup ───────────────────────────────────────────────────
app = FastAPI(title="Ollama Chat API Service with RAG")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Point to your frontend folder
frontend_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), "../frontend"))

# Serve static assets (CSS/JS)
app.mount("/static", StaticFiles(directory=frontend_dir), name="static")

# Serve index.html at root route
@app.get("/")
async def read_index():
    index_path = os.path.join(frontend_dir, "index.html")
    if not os.path.exists(index_path):
        raise HTTPException(status_code=404, detail="index.html not found.")
    return FileResponse(index_path)

# ─── Redis & ChromaDB Setup ──────────────────────────────────────────
redis_client = redis.from_url(REDIS_URL, decode_responses=True, protocol=2)

chroma_client = chromadb.PersistentClient(path=CHROMA_PATH)
collection = chroma_client.get_or_create_collection(
    name="documents",
    metadata={"hnsw:space": "cosine"}
)

# ─── RAG & Embeddings Helpers ────────────────────────────────────────
async def get_embedding(text: str, prefix: str = "") -> List[float]:
    """Generates embeddings via Ollama using nomic-embed-text with appropriate prefixes."""
    prompt_text = f"{prefix}{text}" if prefix else text
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.post(
                f"{OLLAMA_API_URL}/api/embeddings",
                json={"model": EMBED_MODEL, "prompt": prompt_text}
            )
            resp.raise_for_status()
            return resp.json()["embedding"]
        except Exception as e:
            print(f"[ERROR] Embedding generation failed: {e}")
            raise HTTPException(status_code=500, detail=f"Embedding error: {str(e)}")

def extract_text_from_file(file_bytes: bytes, filename: str) -> str:
    """Extracts text content from PDF, DOCX, TXT, or MD files."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    try:
        if ext == "pdf":
            reader = pypdf.PdfReader(io.BytesIO(file_bytes))
            return "\n".join([page.extract_text() for page in reader.pages if page.extract_text()])
        elif ext in ["docx", "doc"]:
            doc = docx.Document(io.BytesIO(file_bytes))
            return "\n".join([p.text for p in doc.paragraphs if p.text])
        else:  # txt, md, etc.
            return file_bytes.decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"[ERROR] Failed to extract text from {filename}: {e}")
        return ""

def chunk_text(text: str, chunk_size: int = 800, overlap: int = 100) -> List[str]:
    """Splits raw text into overlapping chunks for embedding."""
    chunks = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        chunks.append(text[start:end])
        start += chunk_size - overlap
    return chunks

# ─── Web Search Helper ───────────────────────────────────────────────
def perform_websearch(query: str, max_results: int = 3) -> str:
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        
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

# ─── Redis Helpers ───────────────────────────────────────────────────
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

# ─── RAG Document Management Endpoints ────────────────────────────────
@app.post("/upload")
async def upload_document(file: UploadFile = File(...)):
    contents = await file.read()
    text = extract_text_from_file(contents, file.filename)
    
    if not text.strip():
        raise HTTPException(status_code=400, detail="Could not extract readable text from file.")

    # Remove previous chunks for this file if re-uploaded
    collection.delete(where={"source": file.filename})

    chunks = chunk_text(text)
    embeddings = []
    ids = []
    metadatas = []

    for idx, chunk in enumerate(chunks):
        # nomic-embed-text recommended prefix for documents
        emb = await get_embedding(chunk, prefix="search_document: ")
        embeddings.append(emb)
        ids.append(f"{file.filename}_{idx}")
        metadatas.append({"source": file.filename, "chunk": idx})

    collection.add(
        ids=ids,
        embeddings=embeddings,
        documents=chunks,
        metadatas=metadatas
    )

    return JSONResponse({
        "ok": True,
        "filename": file.filename,
        "chunks_indexed": len(chunks)
    })

@app.get("/documents")
async def list_documents():
    """Returns currently indexed unique documents in ChromaDB."""
    data = collection.get(include=["metadatas"])
    sources = list(set(m.get("source") for m in data.get("metadatas", []) if m and "source" in m))
    # FIX #1: Added "ok": True so the frontend recognizes the response
    return JSONResponse({"ok": True, "documents": sources})

@app.post("/delete-doc")
async def delete_document(request: Request):
    body = await request.json()
    # FIX #2: Changed from body.get("filename") to body.get("source")
    # because the frontend sends { source: filename }
    source = body.get("source")
    if source:
        collection.delete(where={"source": source})
        return JSONResponse({"ok": True, "message": f"Deleted {source}"})
    return JSONResponse({"ok": False, "error": "Source required."}, status_code=400)

# ─── System & Chat Endpoints ──────────────────────────────────────────
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
            llm_loaded = await check_model_available(client, MODEL_NAME)
            embed_loaded = await check_model_available(client, EMBED_MODEL)
            return JSONResponse({
                "ok": True,
                "model": MODEL_NAME,
                "model_loaded": llm_loaded,
                "embed_model_loaded": embed_loaded,
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
    
    # 1. Clear chat history from Redis
    await clear_history(session_id)
    
    # 2. Clear ALL documents from ChromaDB
    try:
        all_data = collection.get(include=["metadatas"])
        if all_data and all_data.get("ids"):
            collection.delete(ids=all_data["ids"])
    except Exception as e:
        print(f"[ERROR] Failed to clear documents on reset: {e}")
    
    return JSONResponse({
        "ok": True,
        "message": "Conversation and documents reset.",
        "session_id": session_id,
    })

@app.post("/chat")
async def chat(request: Request):
    body       = await request.json()
    user_msg   = (body.get("message") or "").strip()
    web_search = body.get("web_search", False)
    use_rag    = body.get("use_rag", False)
    session_id = body.get("session_id", "default")

    if not user_msg:
        return JSONResponse(
            {"ok": False, "error": "Message is required."},
            status_code=400,
        )

    # 1. RAG Context Retrieval
    rag_ctx = ""
    if use_rag:
        try:
            query_emb = await get_embedding(user_msg, prefix="search_query: ")
            results = collection.query(query_embeddings=[query_emb], n_results=5)
            retrieved_docs = results.get("documents", [[]])[0]
            if retrieved_docs:
                rag_ctx = "\n---RETRIEVED DOCUMENT CONTEXT---\n"
                for idx, doc in enumerate(retrieved_docs, start=1):
                    rag_ctx += f"Excerpt [{idx}]: {doc}\n\n"
                rag_ctx += "---END OF DOCUMENT CONTEXT---\n"
        except Exception as e:
            print(f"[ERROR] RAG retrieval failed: {e}")

    # 2. Web Search Context Retrieval
    search_ctx = ""
    if web_search:
        search_ctx = await asyncio.to_thread(perform_websearch, user_msg, MAX_SEARCH_RES)

    # 3. Conversation History Retrieval
    history = await get_history(session_id)
    context_text = ""
    for entry in history:
        context_text += f"User: {entry['user']}\nAssistant: {entry['assistant']}\n"

    # 4. Construct System Prompt
    sys_instruction = "System: You are an intelligent AI assistant."
    if use_rag:
        sys_instruction += (
            " Answer the user's question using ONLY the provided document excerpts below. "
            "If the answer is not in the documents, state that clearly."
        )

    full_prompt = (
        f"{sys_instruction}\n"
        f"{rag_ctx}"
        f"{search_ctx}"
        f"{context_text}User: {user_msg}\nAssistant:"
    )

    # 5. LLM Response Generation Stream
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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)