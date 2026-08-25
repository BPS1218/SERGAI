"""
sergAi Backend - Konfigurasi Global
Load environment variables & export settings
"""
import os
from pathlib import Path
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # Server
    port: int = 8000
    environment: str = "development"
    
    # Gemini API
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.1-flash-lite-preview"
    
    # OpenAI API (Opsional)
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    
    active_model: str = "gemini"

    # BPS WebAPI
    bps_base_url: str = "https://webapi.bps.go.id/v1/api"
    bps_domain_id: str = "1218"
    bps_api_key: str = ""
    
    # Chat config
    max_history: int = 10
    timeout_seconds: int = 30
    
    class Config:
        env_file = ".env"
        case_sensitive = False
        extra = "ignore"

# Instance global (tanpa validate ketat)
settings = Settings()

# Print warning kalau key kosong (tapi tidak crash)
if not settings.gemini_api_key:
    print("⚠️ GEMINI_API_KEY belum diatur - beberapa fitur tidak aktif")
if not settings.bps_api_key:
    print("⚠️ BPS_API_KEY belum diatur - fitur data real-time tidak aktif")