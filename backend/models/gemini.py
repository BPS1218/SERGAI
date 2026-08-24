"""
Implementasi Gemini API untuk sergAI
Menggunakan library google-genai resmi
"""
import asyncio
from typing import Optional, List, Dict

from google import genai
from google.genai import types

from config import settings
from .base import BaseModel, ModelResponse, Source

class GeminiModel(BaseModel):
    """Gemini API implementation"""
    
    def __init__(self):
        if not settings.gemini_api_key:
            raise RuntimeError("GEMINI_API_KEY tidak ditemukan di config")
        
        self.client = genai.Client()  # ✅ Baca API key dari env otomatis
        self.model_name = settings.gemini_model
    
    def get_model_info(self) -> Dict:
        return {
            "name": "Gemini",
            "model": self.model_name,
            "provider": "Google AI",
            "capabilities": ["text_generation", "multilingual", "reasoning"]
        }
    
    async def generate_response(
        self,
        question: str,
        chat_history: Optional[List[Dict]] = None,
        context: Optional[Dict] = None
    ) -> ModelResponse:
        """Generate response menggunakan Gemini API"""
        try:
            # 1. Siapkan prompt system
            system_prompt = self._format_system_prompt(context)
            
            # 2. Format chat history untuk Gemini
            contents = self._build_contents(system_prompt, question, chat_history)
            
            # 3. Call Gemini API (async via run_in_executor)
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None, 
                lambda: self.client.models.generate_content(
                    model=self.model_name,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        temperature=0.2,
                        max_output_tokens=1024,
                    )
                )
            )
            
            # 4. Parse response
            answer = response.text.strip()
            
            # ✅ KEMBALIKAN RESPONSE: sources dikosongkan karena sudah ada di dalam teks answer
            return ModelResponse(
                answer=answer,
                sources=[],  # ✅ Kosong, sumber sudah di-generate Gemini di dalam answer
                meta={"model": self.model_name, "provider": "google-genai"},
                success=True
            )
            
        except Exception as e:
            print(f"❌ Gemini API Error: {str(e)}")
            return ModelResponse(
                answer="Maaf, terjadi kendala saat memproses pertanyaan. Silakan coba lagi.",
                error=str(e),
                success=False
            )
    
    def _build_contents(
        self, 
        system_prompt: str, 
        question: str, 
        history: Optional[List[Dict]]
    ) -> List:
        """Build contents array untuk Gemini API"""
        contents = []
        
        # System prompt sebagai instruksi awal
        contents.append({
            "role": "user",
            "parts": [{"text": f"SYSTEM: {system_prompt}"}]
        })
        contents.append({
            "role": "model", 
            "parts": [{"text": "Dimengerti. Saya siap membantu sebagai asisten BPS Sergai."}]
        })
        
        # Chat history (jika ada)
        if history:
            for msg in history[-settings.max_history:]:
                role = "user" if msg["role"] == "user" else "model"
                contents.append({
                    "role": role,
                    "parts": [{"text": msg["content"]}]
                })
        
        # Current question
        contents.append({
            "role": "user",
            "parts": [{"text": question}]
        })
        
        return contents