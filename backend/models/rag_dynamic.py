"""
backend/models/rag_dynamic.py
Implementasi RAG untuk BPS WebAPI Dynamic Tables (JSON-based)
✅ Mapping var_id via database.xlsx
✅ Fallback tahun otomatis (2025 -> 2020)
✅ Decoder composite key dinamis
✅ Pre-check conversational & prompt dari base.py
"""
import re
import time
import os
import pandas as pd
from typing import Optional, List, Dict, Tuple
import httpx
from google import genai

from config import settings
from .base import BaseModel, ModelResponse, Source


class RAGDynamicModel(BaseModel):
    def __init__(self):
        if not settings.bps_api_key:
            raise RuntimeError("BPS_API_KEY tidak ditemukan di config/.env")
        
        self.api_key = settings.bps_api_key
        self.domain_id = "1218"  # Kab. Serdang Bedagai
        self.base_url = "https://webapi.bps.go.id/v1/api"
        self.lang = "ind"
        
        # 🧠 Memory Cache
        self._content_cache = {} 
        self._db_cache = None # Cache data database.xlsx
        self._metadata_cache = {} # Cache metadata per variabel
        self.cache_ttl = 3600  # 1 jam
        
        # ⚙️ Init Gemini Client
        self.client = genai.Client(api_key=settings.gemini_api_key)
        self.model_name = "gemini-3.1-flash-lite-preview" # Sesuaikan dengan model yang Anda pakai

    def get_model_info(self) -> Dict:
        return {
            "name": "RAG BPS Dynamic",
            "model": "bps-rag-dynamic-v1",
            "provider": "BPS WebAPI (Dynamic JSON) + Gemini",
            "capabilities": ["keyword_mapping", "auto_year_fallback", "composite_key_decode"]
        }

    async def generate_response(
        self, question: str, chat_history: Optional[List[Dict]] = None, context: Optional[Dict] = None
    ) -> ModelResponse:
        try:
            # 🗣️ PRE-CHECK: Handle pertanyaan conversational/meta SEBELUM search RAG
            conversational_keywords = [
                "halo", "hi", "hello", "siapa kamu", "apa itu sergai", 
                "kamu siapa", "bisa bantu", "test", "apa kabar", "terima kasih", "makasih"
            ]
            q_lower = question.lower().strip()
            
            # Jika pertanyaan pendek & mengandung keyword conversational, jawab langsung
            if len(q_lower) < 60 and any(kw in q_lower for kw in conversational_keywords):
                return ModelResponse(
                    answer="Halo! 👋 Saya adalah **sergAI** (Smart Engagement for Responsive Government Assistant Intelligence), asisten virtual resmi BPS Kabupaten Serdang Bedagai.\n\nSaya siap membantu Anda mengakses data statistik seperti:\n• 📊 PDRB & perekonomian daerah\n• 👥 Jumlah penduduk & demografi\n• 📉 Tingkat kemiskinan & IPM\n• 🌾 Produksi pertanian & perikanan\n\nSilakan tanyakan data spesifik yang Anda butuhkan!",
                    sources=[Source(
                        name="BPS Kabupaten Serdang Bedagai",
                        url="https://serdangbedagaikab.bps.go.id"
                    )],
                    meta={"provider": "rag_dynamic", "type": "conversational"},
                    success=True
                )

            # 1. Mapping Keyword ke Variable ID (via Database Excel)
            var_id, matched_title = await self._match_keyword_to_var(question)
            
            if not var_id:
                return ModelResponse(
                    answer="Maaf, saya belum menemukan data statistik yang sesuai di database BPS Kabupaten Serdang Bedagai.",
                    success=True
                )

            print(f"✅ Match found: Variable ID {var_id} - '{matched_title}'")

            # 2. Extract Tahun dari Pertanyaan
            user_year = self._extract_year(question)
            
            # 3. Ambil Context (Fetch Data dengan Logic Fallback Tahun)
            table_context, used_year_label = await self._get_dynamic_context_with_fallback(var_id, user_year)
            
            if not table_context:
                if user_year:
                    msg = f"Maaf, data untuk tahun {user_year} belum tersedia di sistem BPS saat ini."
                else:
                    msg = "Maaf, data untuk variabel ini belum tersedia untuk periode 2020-2025."
                return ModelResponse(answer=msg, success=True)

            # 4. Generate Prompt: Gunakan base prompt + inject konteks tabel
            base_prompt = self._format_system_prompt(context=None)
            prompt = f"""{base_prompt}

📋 KONTEKS DATA DINAMIS:
• Judul Data: {matched_title}
• Tahun Referensi: {used_year_label}
[KONTEKS DATA]
{table_context}

[PERTANYAAN USER]
{question}

Jawaban:"""
            
            response = await self.client.aio.models.generate_content(
                model=self.model_name,
                contents=prompt
            )
            
            answer = self._extract_response_text(response)
            
            return ModelResponse(
                answer=answer.strip(),
                sources=[Source(
                    name="BPS Kabupaten Serdang Bedagai", 
                    url=f"https://serdangbedagaikab.bps.go.id",
                    page=matched_title
                )],
                meta={"variable_id": var_id, "provider": "rag_dynamic"},
                success=True
            )
            
        except Exception as e:
            print(f"❌ RAG Dynamic Error: {type(e).__name__}: {str(e)}")
            return ModelResponse(
                answer="Terjadi kendala teknis saat memproses data dinamis.",
                error=str(e),
                success=False
            )

    # ================= 🔍 KEYWORD MAPPING (DATABASE) =================

    async def _match_keyword_to_var(self, question: str) -> Tuple[Optional[str], Optional[str]]:
        """Load database.xlsx dan cari baris yang match dengan keyword pertanyaan"""
        if self._db_cache is None:
            self._db_cache = self._load_database_excel()
        
        if self._db_cache.empty:
            return None, None

        q = question.lower()
        keywords = [k for k in re.findall(r'\b\w+\b', q) if len(k) > 3] # Ambil kata > 3 huruf
        
        best_match = None
        best_score = 0
        
        for _, row in self._db_cache.iterrows():
            title = str(row.get('title', '')).lower()
            var_id = str(row.get('var_id', ''))
            
            # Hitung score: berapa banyak keyword user yang ada di title
            score = sum(1 for kw in keywords if kw in title)
            
            # Bonus score jika title mengandung tahun yang disebut user
            user_year = self._extract_year(question)
            if user_year and user_year in title:
                score += 2
                
            if score > best_score:
                best_score = score
                best_match = (var_id, row.get('title'))
        
        # Minimal score 1 agar tidak asal ambil
        if best_score > 0:
            return best_match
        return None, None

    def _load_database_excel(self) -> pd.DataFrame:
        """Load database.xlsx dari root folder project"""
        try:
            # Asumsi file ada di root project (satu level di atas backend)
            # Atau sesuaikan path sesuai struktur folder Anda
            path = os.path.join(os.path.dirname(__file__), "..", "..", "database.xlsx")
            if not os.path.exists(path):
                # Fallback cari di folder yang sama dengan backend
                path = os.path.join(os.path.dirname(__file__), "database.xlsx")
            
            df = pd.read_excel(path)
            print(f"💾 Loaded database.xlsx with {len(df)} variables.")
            return df
        except Exception as e:
            print(f"❌ Gagal load database.xlsx: {e}")
            return pd.DataFrame()

    def _extract_year(self, text: str) -> Optional[str]:
        match = re.search(r'\b(20\d{2})\b', text) # Cari tahun 2000-2099
        return match.group(1) if match else None

    # ================= 📦 FETCH DATA WITH FALLBACK =================

    async def _get_dynamic_context_with_fallback(self, var_id: str, user_year: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
        """
        Logic Inti:
        1. Jika user sebut tahun -> Cek cuma tahun itu.
        2. Jika tidak -> Loop 125 (2025) s/d 120 (2020).
        """
        
        # Tentukan list tahun yang akan dicek
        if user_year:
            # User spesifik -> Cek hanya tahun tersebut
            # Mapping tahun ke kode: 2020=120, ..., 2025=125
            try:
                y = int(user_year)
                if 2010 <= y <= 2025:
                    years_to_check = [str(y - 2000 + 100)] # 2020 -> 120
                else:
                    return None, None # Tahun di luar range
            except:
                return None, None
        else:
            # User tidak spesifik -> Loop turun dari 2025 ke 2020
            years_to_check = ["125", "124", "123", "122", "121", "120"]

        for year_code in years_to_check:
            json_data = await self._fetch_json_data(var_id, year_code)
            
            # Cek apakah datacontent ada dan tidak kosong
            if json_data and json_data.get("datacontent"):
                # Konversi kode tahun kembali ke label (125 -> 2025)
                actual_year = str(int(year_code) - 120 + 2020)
                context = self._parse_dynamic_json(json_data, actual_year)
                return context, actual_year

        return None, None

    async def _fetch_json_data(self, var_id: str, year_code: str) -> Optional[Dict]:
        """Fetch raw JSON dari WebAPI"""
        url = f"{self.base_url}/list/model/data/lang/ind/domain/{self.domain_id}/var/{var_id}/th/{year_code}/key/{self.api_key}/"
        
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                res = await client.get(url)
                res.raise_for_status()
                data = res.json()
                if data.get("status") == "OK":
                    return data
            except Exception as e:
                print(f"⚠️ Fetch error var={var_id} th={year_code}: {e}")
        return None

    # ================= 🧩 DECODE & PARSE =================

    def _parse_dynamic_json(self, json_data: Dict, used_year_label: str) -> str:
        """Decode composite key dan format jadi teks"""
        try:
            # Metadata
            var_info = json_data.get("var", [{}])[0]
            vervars = {str(v["val"]): v["label"] for v in json_data.get("vervar", [])}
            turtahun = {str(t["val"]): t["label"] for t in json_data.get("turtahun", [])}
            
            # Cari ID variabel dan tahun di response (sebagai anchor)
            var_id_str = str(var_info.get("val"))
            tahun_id_str = str([t["val"] for t in json_data.get("tahun", []) if t["label"] == used_year_label][0])
            
            datacontent = json_data.get("datacontent", {})
            unit = var_info.get("unit", "")
            
            lines = [f"📊 {var_info.get('label', 'Data')} (Tahun {used_year_label})"]
            
            # Struktur Data: Baris (Row) -> Sub-kategori (Column) -> Value
            rows_data = {} 
            
            for key, value in datacontent.items():
                key_str = str(key)
                
                # --- DECODER LOGIC ---
                # Kita tahu format key: [vervar_id][var_id][turvar_id][tahun_id][turtahun_id]
                # Kita tahu var_id_str dan tahun_id_str. Kita cari posisinya.
                
                var_idx = key_str.find(var_id_str)
                tahun_idx = key_str.find(tahun_id_str)
                
                if var_idx == -1 or tahun_idx == -1: continue # Skip jika format tidak sesuai
                
                # vervar_id adalah bagian sebelum var_id
                vervar_id = key_str[:var_idx]
                
                # turtahun_id adalah bagian setelah tahun_id
                # Biasanya tahun_id ada di akhir atau sebelum turtahun_id
                # Asumsi struktur: ...[tahun_id][turtahun_id]
                remaining_after_tahun = key_str[tahun_idx + len(tahun_id_str):]
                turtahun_id = remaining_after_tahun if remaining_after_tahun else "0"
                
                # Map ke Label
                row_label = vervars.get(vervar_id, f"Kode {vervar_id}")
                col_label = turtahun.get(turtahun_id, "")
                
                # Format Value
                val_str = f"{value:,.2f}" if isinstance(value, float) else f"{value:,}"
                
                if row_label not in rows_data:
                    rows_data[row_label] = []
                
                # Simpan: (Label Kolom, Nilai)
                rows_data[row_label].append((col_label, val_str))

            # Format Output Text
            priority_rows = []
            other_rows = []
            
            for row_label, items in rows_data.items():
                # Urutkan item berdasarkan key kolom agar rapi
                items.sort(key=lambda x: x[0])
                
                parts = [f"{row_label}"]
                for col, val in items:
                    if col: # Jika ada label kolom (misal Triwulan)
                        parts.append(f"{col}: {val} {unit}".strip())
                    else:
                        parts.append(f"{val} {unit}".strip())
                
                row_text = " | ".join(parts)
                
                # Deteksi Agregat
                if any(kw in row_label.upper() for kw in ["TOTAL", "JUMLAH", "SERDANG", "KAB"]):
                    priority_rows.append("🔹 " + row_text)
                else:
                    other_rows.append("• " + row_text)
            
            lines.extend(priority_rows)
            lines.extend(other_rows)
            
            lines.append(f"📖 Sumber: BPS WebAPI v1 - Last Update: {json_data.get('last_update', 'N/A')}")
            return "\n".join(lines)
            
        except Exception as e:
            print(f"❌ Parse Error: {e}")
            return "⚠️ Gagal memproses format data JSON."

    # ================= 🤖 PROMPT (SUDAH DIPANGKAS - PAKAI BASE) =================
    # Method _build_strict_prompt dihapus karena sekarang pakai self._format_system_prompt() dari base.py
    
    def _extract_response_text(self, response) -> str:
        try:
            if hasattr(response, 'text') and response.text: return response.text
            if hasattr(response, 'candidates') and response.candidates:
                return response.candidates[0].content.parts[0].text
            return str(response)
        except Exception:
            return "Maaf, terjadi kesalahan saat memformat jawaban."