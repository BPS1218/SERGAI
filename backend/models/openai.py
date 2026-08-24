"""
Implementasi OpenAI API untuk sergAI
Menggunakan Chat Completions API (Stable)
"""
import asyncio
from typing import Optional, List, Dict
from openai import AsyncOpenAI  # ✅ Async untuk FastAPI

from config import settings
from .base import BaseModel, ModelResponse, Source

class OpenAIModel(BaseModel):
    def __init__(self):
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY tidak ditemukan di config/.env")
        
        # ✅ Gunakan AsyncOpenAI agar tidak blocking event loop FastAPI
        self.client = AsyncOpenAI(api_key=settings.openai_api_key)
        self.model_name = settings.openai_model  # "gpt-4o-mini"
    
    def get_model_info(self) -> Dict:
        return {
            "name": "OpenAI",
            "model": self.model_name,
            "provider": "OpenAI",
            "capabilities": ["text_generation", "multilingual", "reasoning"]
        }
    
    async def generate_response(
        self,
        question: str,
        chat_history: Optional[List[Dict]] = None,
        context: Optional[Dict] = None
    ) -> ModelResponse:
        try:
            system_prompt = self._format_system_prompt(context)
            messages = self._build_messages(system_prompt, question, chat_history)
            
            # ✅ Call Chat Completions API (bukan Responses API)
            response = await self.client.chat.completions.create(
                model=self.model_name,          # "gpt-4o-mini"
                messages=messages,              # List[Dict] dengan role & content
                temperature=0.1,                # Rendah untuk jawaban faktual
                max_tokens=1024,                # Batasi output agar hemat token
            )
            
            answer = response.choices[0].message.content.strip()
            
            return ModelResponse(
                answer=answer,
                sources=[],
                meta={"model": self.model_name, "provider": "openai"},
                success=True
            )
            
        except Exception as e:
            print(f"❌ OpenAI API Error: {str(e)}")
            return ModelResponse(
                answer="Maaf, terjadi kendala saat memproses pertanyaan. Silakan coba lagi.",
                error=str(e),
                success=False
            )
    
    def _build_messages(
        self, 
        system_prompt: str, 
        question: str, 
        history: Optional[List[Dict]]
    ) -> List[Dict]:
        """Build messages array format Chat Completions API"""
        messages = [{"role": "system", "content": system_prompt}]
        
        if history:
            for msg in history[-settings.max_history:]:
                messages.append({"role": msg["role"], "content": msg["content"]})
        
        messages.append({"role": "user", "content": question})
        return messages