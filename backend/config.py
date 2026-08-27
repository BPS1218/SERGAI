"""
backend/config.py
Konfigurasi terpusat sergAI
✅ Semua model & sumber data bisa diganti lewat .env tanpa mengubah kode
"""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ===== Server =====
    port: int = 8000
    environment: str = "development"

    # ===== API KEYS =====
    gemini_api_key: str = ""
    openai_api_key: str = ""
    bps_api_key: str = ""

    # ===== MODEL (✅ ganti kapan saja via .env) =====
    gemini_model: str = "gemini-3.1-flash-lite-preview"
    openai_model: str = "gpt-5.6-luna"  # ✅ Bisa diganti tanpa ubah kode

    # ===== BPS WebAPI (untuk RAG Dynamic) =====
    bps_base_url: str = "https://webapi.bps.go.id/v1/api"
    bps_domain_id: str = "1218"  # Kab. Serdang Bedagai

    # ===== SPREADSHEET DATABASE (gantikan database.xlsx) =====
    # URL: https://docs.google.com/spreadsheets/d/1xHHdwIWTYnQFG0FEYAZDgUlf3IeSKctAyn5k5erutrI
    # Kolom: no, var_id, title, definisi, interpretasi, ...
    database_spreadsheet_id: str = "1xHHdwIWTYnQFG0FEYAZDgUlf3IeSKctAyn5k5erutrI"

    # ===== SPREADSHEET REKAP PRIORITAS =====
    # URL: https://docs.google.com/spreadsheets/d/1ma_E_E1C9gxyVX8Jnnb3cHmqPrRJ_GXF_ZTuvWUQrKg
    # Kolom: Judul Tabel, definisi, interpretasi, Sumber Tabel, Link, Sheet
    rekap_spreadsheet_id: str = "1ma_E_E1C9gxyVX8Jnnb3cHmqPrRJ_GXF_ZTuvWUQrKg"
    rekap_sheet_name: str = ""  # Nama tab rekap; kosongkan untuk sheet pertama

    # ===== PERILAKU =====
    max_history: int = 10
    timeout_seconds: int = 45   # Timeout LLM
    cache_ttl: int = 3600       # 1 jam

    class Config:
        env_file = ".env"
        case_sensitive = False
        extra = "ignore"


settings = Settings()

# Print warning kalau key kosong (tidak crash)
if not settings.gemini_api_key:
    print("⚠️ GEMINI_API_KEY belum diatur - fallback ke OpenAI aktif")
if not settings.openai_api_key:
    print("⚠️ OPENAI_API_KEY belum diatur - fallback LLM tidak aktif")
if not settings.bps_api_key:
    print("⚠️ BPS_API_KEY belum diatur - fitur data BPS real-time tidak aktif")