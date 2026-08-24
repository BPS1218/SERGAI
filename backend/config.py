"""
sergAi Backend - Konfigurasi Global
Load environment variables & export settings
"""
import os
from pathlib import Path
from dotenv import load_dotenv
from pydantic_settings import BaseSettings

# Load .env file
BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")


class Settings(BaseSettings):
    # Server
    port: int = int(os.getenv("PORT", "8000"))
    environment: str = os.getenv("ENVIRONMENT", "development")
    
    # Gemini API
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_model: str = "gemini-3.1-flash-lite-preview"  # Model gratis
    
    # OpenAI API (Opsional)
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    # 🔧 FORCE HARDCODE - Untuk testing (JANGAN commit ke Git!)
    # openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"  # 
    
    active_model: str = os.getenv("ACTIVE_MODEL", "gemini")

    # BPS WebAPI
    bps_base_url: str = "https://webapi.bps.go.id/v1/api"
    bps_domain_id: str = os.getenv("BPS_DOMAIN_ID", "1218")
    bps_api_key: str = os.getenv("BPS_API_KEY", "")
    
    # Chat config
    max_history: int = 10
    timeout_seconds: int = 30
    
    # Validation
    def validate(self):
        if not self.gemini_api_key:
            raise ValueError("⚠️ GEMINI_API_KEY belum diatur di .env")
        if not self.bps_api_key:
            print("⚠️ BPS_API_KEY belum diatur - fitur data real-time tidak aktif")
        return self

# Instance global
settings = Settings().validate()