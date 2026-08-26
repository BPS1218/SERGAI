"""
sergAI Backend - FastAPI Server
Main entry point untuk API chatbot
"""
import os
from dotenv import load_dotenv

_BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_BASE, ".env"))   # muat backend/.env bila ada

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Optional, List, Dict
import time

from config import settings
from models import get_model, ModelResponse

# Initialize FastAPI app
app = FastAPI(
    title="sergAI Backend API",
    description="API untuk chatbot BPS Kabupaten Serdang Bedagai",
    version="1.0.0"
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
    model: Optional[str] = None

class ChatResponse(BaseModel):
    answer: str
    sources: List[Dict] = []
    meta: Dict = {}
    timestamp: str
    success: bool

_FRONTEND_DIR = os.path.join(os.path.dirname(_BASE), "frontend")

# ============================================================
# ===== CUSTOM ROUTES (HARUS SEBELUM app.mount) ==============
# ============================================================

@app.get("/")
async def root_welcome():
    """Root URL → tampilkan halaman welcome (identitas)"""
    return FileResponse(os.path.join(_FRONTEND_DIR, "welcome.html"))

@app.get("/welcome")
async def welcome_page():
    """Alias untuk halaman welcome"""
    return FileResponse(os.path.join(_FRONTEND_DIR, "welcome.html"))

@app.get("/chat")
async def chat_page():
    """Halaman chat utama (setelah isi identitas)"""
    return FileResponse(os.path.join(_FRONTEND_DIR, "index.html"))

# ============================================================
# ===== API ROUTES ===========================================
# ============================================================

# Lazy load model
_model_instance = None
_last_active_model = None

def get_active_model():
    """Lazy load model instance berdasarkan settings.active_model"""
    global _model_instance, _last_active_model
    current_model = settings.active_model
    if _model_instance is None or _last_active_model != current_model:
        print(f"🔄 Loading model: {current_model}")
        _model_instance = get_model(current_model)
        _last_active_model = current_model
    return _model_instance

@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "model": get_active_model().get_model_info(),
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
    }

@app.post("/api/chat", response_model=ChatResponse)
async def chat_endpoint(request: ChatRequest, background_tasks: BackgroundTasks):
    start_time = time.time()
    try:
        if not request.question.strip():
            raise HTTPException(status_code=400, detail="Pertanyaan tidak boleh kosong")
        
        target_model = request.model or settings.active_model
        if target_model not in ["gemini", "openai", "rag", "rag_dynamic"]:
            target_model = "gemini"
            
        model = get_model(target_model)
        response = await model.generate_response(
            question=request.question,
            chat_history=request.chat_history,
            context=request.context
        )
        
        elapsed = time.time() - start_time
        print(f"✅ [{response.success}] {request.question[:50]}... ({elapsed:.2f}s) [Model: {target_model}]")
        
        return ChatResponse(
            answer=response.answer,
            sources=[s.model_dump() for s in response.sources],
            meta={**response.meta, "requested_model": target_model},
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
    """Test koneksi ke BPS WebAPI"""
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