"""
sergAI Backend - FastAPI Server
✅ Alur baru: single orchestrator RAGUnifiedModel (tanpa pilihan model dari frontend)
"""
import os
import time
import httpx

from urllib.parse import urlparse

from dotenv import load_dotenv

_BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_BASE, ".env"))

from fastapi import (
    FastAPI,
    HTTPException,
    BackgroundTasks,
    Query,
)

from fastapi.middleware.cors import CORSMiddleware

from fastapi.responses import (
    FileResponse,
    Response,
)

from fastapi.staticfiles import StaticFiles

from pydantic import BaseModel, Field

from typing import Optional, List, Dict

from config import settings
from models import ModelResponse
from models.rag_unified import RAGUnifiedModel

# Initialize FastAPI app
app = FastAPI(
    title="sergAI Backend API",
    description="API untuk chatbot BPS Kabupaten Serdang Bedagai",
    version="2.0.0"
)

@app.get("/api/publication-cover")
async def publication_cover_proxy(
    url: str = Query(...),
):
    try:
        parsed = urlparse(url)

        if (
            parsed.scheme != "https"
            or parsed.hostname != "webapi.bps.go.id"
            or parsed.path != "/cover.php"
        ):
            raise HTTPException(
                status_code=400,
                detail="URL cover tidak diizinkan.",
            )

        async with httpx.AsyncClient(
            timeout=20.0,
            follow_redirects=True,
        ) as client:
            response = await client.get(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "image/*,*/*;q=0.8",
                },
            )

        if response.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail="Cover publikasi gagal diambil.",
            )

        content_type = response.headers.get(
            "content-type",
            "",
        )

        if not content_type.startswith("image/"):
            raise HTTPException(
                status_code=502,
                detail="Respons bukan gambar.",
            )

        return Response(
            content=response.content,
            media_type=content_type,
            headers={
                "Cache-Control": "public, max-age=86400",
            },
        )

    except HTTPException:
        raise

    except Exception as exc:
        print(
            "[PUBLICATION COVER ERROR]",
            type(exc).__name__,
            exc,
        )

        raise HTTPException(
            status_code=502,
            detail="Cover publikasi tidak dapat dimuat.",
        )

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Request/Response schemas
class ChatRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=1000)
    user_id: Optional[str] = "anonymous"
    chat_history: Optional[List[Dict]] = Field(default_factory=list)
    context: Optional[Dict] = None

    # ID kandidat yang dipilih user saat backend mengembalikan
    # meta.type == "candidate_selection"
    selected_candidate_id: Optional[str] = None

class ChatResponse(BaseModel):
    answer: str
    sources: List[Dict] = []
    table: Optional[Dict] = None   # ✅ baru
    meta: Dict = {}
    timestamp: str
    success: bool

_FRONTEND_DIR = os.path.join(os.path.dirname(_BASE), "frontend")

# ============================================================
# ===== CUSTOM ROUTES (HARUS SEBELUM app.mount) ==============
# ============================================================

@app.get("/")
async def root_welcome():
    return FileResponse(os.path.join(_FRONTEND_DIR, "welcome.html"))

@app.get("/welcome")
async def welcome_page():
    return FileResponse(os.path.join(_FRONTEND_DIR, "welcome.html"))

@app.get("/chat")
async def chat_page():
    return FileResponse(os.path.join(_FRONTEND_DIR, "index.html"))

# ============================================================
# ===== API ROUTES ===========================================
# ============================================================

# ✅ Satu instance orchestrator (lazy load, dipakai semua request)
_rag_instance = None

def get_rag_model() -> RAGUnifiedModel:
    global _rag_instance
    if _rag_instance is None:
        print("🔄 Loading RAGUnifiedModel...")
        _rag_instance = RAGUnifiedModel()
    return _rag_instance

@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "model": get_rag_model().get_model_info(),
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
    }

@app.post("/api/chat", response_model=ChatResponse)
async def chat_endpoint(request: ChatRequest, background_tasks: BackgroundTasks):
    start_time = time.time()
    try:
        if not request.question.strip():
            raise HTTPException(status_code=400, detail="Pertanyaan tidak boleh kosong")

        # ✅ Tidak ada pilihan model: semua lewat RAGUnifiedModel
        model = get_rag_model()

        # Gabungkan context biasa dengan kandidat yang dipilih user.
        # rag_unified.py membaca selected_candidate_id dari context.
        request_context = dict(request.context or {})
        if request.selected_candidate_id:
            request_context["selected_candidate_id"] = request.selected_candidate_id

        response = await model.generate_response(
            question=request.question,
            chat_history=request.chat_history,
            context=request_context
        )

        elapsed = time.time() - start_time
        model_used = response.meta.get("model_used", "unknown")
        fallback = " (FALLBACK)" if response.meta.get("fallback") else ""
        print(f"✅ [{response.success}] {request.question[:50]}... ({elapsed:.2f}s) [Model: {model_used}{fallback}]")

        return ChatResponse(
            answer=response.answer,
            sources=[s.model_dump() for s in response.sources],
            table=response.table,          # ✅ baru
            meta=response.meta,   # ✅ termasuk model_used (hanya untuk log)
            timestamp=time.strftime("%H:%M:%S"),
            success=response.success
        )
    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Unexpected error: {str(e)}")
        return ChatResponse(
            answer="Maaf, terjadi kesalahan internal. Tim teknis telah diberitahu.",
            meta={"error": str(e)},
            timestamp=time.strftime("%H:%M:%S"),
            success=False
        )

@app.get("/api/bps/test")
async def test_bps_connection():
    import httpx
    if not settings.bps_api_key:
        return {"connected": False, "reason": "BPS_API_KEY not configured"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            url = f"{settings.bps_base_url}/domain?key={settings.bps_api_key}"
            response = await client.get(url)
            return {
                "connected": response.status_code == 200,
                "status_code": response.status_code,
                "domain_id": settings.bps_domain_id
            }
    except Exception as e:
        return {"connected": False, "error": str(e)}

# ============================================================
# ===== MOUNT FRONTEND (HARUS TERAKHIR) ======================
# ============================================================
app.mount("/", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=settings.port, reload=True)