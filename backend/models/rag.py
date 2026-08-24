"""
backend/models/rag.py
Implementasi RAG untuk BPS WebAPI (Excel-Based)
✅ Menggunakan Pandas untuk parsing Excel
✅ Dynamic Search + Year Filtering
✅ Memory Cache (Metadata & Excel Content)
✅ Pre-check conversational & prompt dari base.py
"""
import re
import io
import time
from typing import Optional, List, Dict
import httpx
import pandas as pd
from google import genai

from config import settings
from .base import BaseModel, ModelResponse, Source


class RAGModel(BaseModel):
    def __init__(self):
        if not settings.bps_api_key:
            raise RuntimeError("BPS_API_KEY tidak ditemukan di config/.env")
        
        self.api_key = settings.bps_api_key
        self.domain_id = "1218"  # Kab. Serdang Bedagai
        self.base_url = "https://webapi.bps.go.id/v1/api"
        self.lang = "ind"
        
        #  Memory Cache
        self._content_cache = {}  # {table_id: {"text": str, "expiry": float}}
        self._metadata_cache = None
        self._metadata_expiry = 0
        self.cache_ttl = 3600  # 1 jam
        
        # ⚙️ Init Gemini Client
        self.client = genai.Client(api_key=settings.gemini_api_key)
        self.model_name = "gemini-3.1-flash-lite-preview"

    def get_model_info(self) -> Dict:
        return {
            "name": "RAG BPS Excel",
            "model": "bps-rag-v1",
            "provider": "BPS WebAPI (Excel) + Gemini",
            "capabilities": ["dynamic_search", "year_aware", "excel_parser", "source_citation"]
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
                    answer="Halo! 👋 Saya adalah **sergAI** (Smart Engagement for Responsive Government Assistant Intelligence), asisten virtual resmi BPS Kabupaten Serdang Bedagai.\n\nSaya siap membantu Anda mengakses data statistik seperti:\n• 📊 PDRB & perekonomian daerah\n• 👥 Jumlah penduduk & demografi\n•  Tingkat kemiskinan & IPM\n• 🌾 Produksi pertanian & perikanan\n\nSilakan tanyakan data spesifik yang Anda butuhkan!",
                    sources=[Source(
                        name="BPS Kabupaten Serdang Bedagai",
                        url="https://serdangbedagaikab.bps.go.id"
                    )],
                    meta={"provider": "rag", "type": "conversational"},
                    success=True
                )

            # 1. Cari table_id (Hanya untuk pertanyaan data)
            table_id, matched_title = await self._search_table_id_dynamic(question)
            
            if not table_id:
                return ModelResponse(
                    answer="Maaf, saya belum menemukan data statistik yang sesuai di database BPS Kabupaten Serdang Bedagai.",
                    success=True
                )

            print(f"✅ Match found: Table ID {table_id}")

            # 2. Ambil Context (Download & Parse Excel)
            table_context = await self._get_table_context(table_id)
            if not table_context:
                return ModelResponse(
                    answer="Gagal memuat data Excel dari BPS. Silakan coba lagi.",
                    success=False
                )

            # 3. Generate Prompt: Gunakan base prompt + inject konteks tabel
            base_prompt = self._format_system_prompt(context=None)
            prompt = f"""{base_prompt}

📋 KONTEKS TABEL SPESIFIK:
• Judul Tabel: {matched_title}
• Data tersedia dalam format: [Kolom: Nilai] dipisahkan dengan " | "
• Baris dengan 🔹 adalah data agregat (Total/Kabupaten), utamakan ini untuk pertanyaan umum.

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
                meta={"table_id": table_id, "provider": "rag"},
                success=True
            )
            
        except Exception as e:
            print(f"❌ RAG Error: {type(e).__name__}: {str(e)}")
            return ModelResponse(
                answer="Terjadi kendala teknis saat memproses data.",
                error=str(e),
                success=False
            )

    # =================  SEARCH LOGIC (SAME AS BEFORE) =================

    async def _search_table_id_dynamic(self, question: str) -> tuple[Optional[str], Optional[str]]:
        user_keywords = self._extract_keywords(question)
        user_year = self._extract_year(question)
        
        all_tables = await self._fetch_all_metadata()
        if not all_tables: return None, None

        candidates = []
        for table in all_tables:
            title = table.get("title", "").lower()
            table_year = self._extract_year(title)
            
            # Score: Keyword overlap
            keyword_score = sum(1 for kw in user_keywords if kw in title)
            if keyword_score == 0: continue
            
            # Filter Tahun
            if user_year and table_year != user_year: continue
            
            candidates.append({
                "table_id": str(table["table_id"]),
                "title": table["title"],
                "excel_url": table.get("excel", ""),
                "year": table_year,
                "score": keyword_score
            })

        if not candidates: return None, None

        # Prioritas: Skor tertinggi, lalu Tahun terbaru
        candidates.sort(key=lambda x: (-x["score"], -int(x["year"]) if x["year"] else 0))
        best = candidates[0]
        return best["table_id"], best["title"]

    def _extract_keywords(self, text: str) -> List[str]:
        stop_words = {"yang", "di", "dan", "atau", "dengan", "untuk", "pada", "dalam", "adalah", "berapa", "tahun"}
        tokens = re.findall(r'[a-zA-Z0-9]+', text.lower())
        return [t for t in tokens if len(t) >= 3 and t not in stop_words and not t.isdigit()]

    def _extract_year(self, text: str) -> Optional[str]:
        match = re.search(r'\b(19|20)\d{2}\b', text)
        return match.group(0) if match else None

    async def _fetch_all_metadata(self) -> List[Dict]:
        """Fetch metadata /list/ endpoint (hanya sekali per sesi)"""
        now = time.time()
        if self._metadata_cache is not None and now < self._metadata_expiry:
            return self._metadata_cache

        all_tables = []
        page = 1
        max_pages = 20

        async with httpx.AsyncClient(timeout=15) as client:
            while page <= max_pages:
                url = f"{self.base_url}/list/model/statictable/lang/{self.lang}/domain/{self.domain_id}/key/{self.api_key}/"
                try:
                    res = await client.get(url, params={"page": page})
                    res.raise_for_status()
                    data = res.json()
                    
                    data_payload = data.get("data", [])
                    if len(data_payload) < 2: break
                        
                    pagination = data_payload[0]
                    tables = data_payload[1]
                    
                    all_tables.extend(tables)
                    
                    # Jika sudah dapat semua (berdasarkan total pages di meta), stop
                    if page >= pagination.get("pages", 1): break
                    page += 1
                    
                except Exception: break

        if all_tables:
            self._metadata_cache = all_tables
            self._metadata_expiry = now + self.cache_ttl
            
        return all_tables

    # ================= 📦 EXCEL DOWNLOAD & PARSE =================

    async def _get_table_context(self, table_id: str) -> Optional[str]:
        """Ambil context dari cache atau download & parse Excel"""
        now = time.time()
        if table_id in self._content_cache and now < self._content_cache[table_id]["expiry"]:
            return self._content_cache[table_id]["text"]

        # Cari Excel URL dari metadata cache
        meta = await self._fetch_all_metadata()
        target_meta = next((t for t in meta if str(t["table_id"]) == table_id), None)
        if not target_meta or not target_meta.get("excel"):
            return None

        excel_url = target_meta["excel"]
        
        # Download Excel
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.get(excel_url)
            if res.status_code != 200: return None
            excel_bytes = res.content

        # Parse Excel -> Context
        context_text = self._parse_excel(excel_bytes)
        
        self._content_cache[table_id] = {"text": context_text, "expiry": now + self.cache_ttl}
        return context_text

    def _parse_excel(self, excel_bytes: bytes) -> str:
        try:
            import pandas as pd
            from io import BytesIO
            
            # 1. Baca dengan skiprows=3 (handle judul tabel BPS)
            df = None
            for engine in ['openpyxl', 'xlrd', None]:
                try:
                    df = pd.read_excel(BytesIO(excel_bytes), skiprows=range(0, 3), engine=engine)
                    break
                except Exception:
                    continue
                    
            if df is None or df.empty:
                return "️ File Excel tidak mengandung tabel yang valid."
                
            # 2. Bersihkan & validasi
            df = df.dropna(how='all').dropna(axis=1, how='all').reset_index(drop=True)
            if len(df) < 2 or len(df.columns) < 2:
                return "⚠️ Tabel terlalu kecil atau kosong."
                
            # Bersihkan nama kolom
            df.columns = [
                str(c).strip() if str(c).strip().lower() not in ["nan", "unnamed", ""] else f"Kolom_{i+1}"
                for i, c in enumerate(df.columns)
            ]
            df = df.fillna("")
            
            # 3. Format Context
            lines = ["📊 Data BPS:"]
            priority_rows = []
            other_rows = []
            
            for _, row in df.iterrows():
                parts = [f"{col}: {val}" for col, val in row.items() 
                         if str(val).strip() not in ["", "nan"]]
                if not parts: continue
                
                row_text = " | ".join(parts)
                first_col = str(row.iloc[0]).lower() if len(row) > 0 else ""
                
                # Deteksi agregat lebih luas
                is_aggregate = any(kw in row_text.lower() for kw in [
                    "jumlah", "total", "kabupaten", "provinsi", "serdang bedagai", "kab. serdang"
                ]) or any(kw in first_col for kw in ["jumlah", "total", "kabupaten", "serdang"])
                
                if is_aggregate:
                    priority_rows.append("🔹 " + row_text)
                else:
                    other_rows.append("• " + row_text)
            
            # 4. Susun Context (Agregat dulu, lalu sample awal+akhir)
            lines.extend(priority_rows[:5])
            if len(other_rows) <= 10:
                lines.extend(other_rows)
            else:
                lines.extend(other_rows[:6])
                lines.extend(other_rows[-3:])  # Jaga baris bawah (biasanya total)
                lines.append(f"️ ... dan {len(other_rows)-9} baris lainnya tersedia.")
                
            lines.append("📖 Sumber: BPS WebAPI v1 - https://serdangbedagaikab.bps.go.id")
            return "\n".join(lines)
            
        except Exception as e:
            print(f"❌ Excel Parse Error: {type(e).__name__}: {e}")
            return f"️ Gagal memproses file Excel: {str(e)}"
        
    # ================= 🤖 PROMPT (SUDAK DIPANGKAS - PAKAI BASE) =================
    # Method _build_strict_prompt dihapus karena sekarang pakai self._format_system_prompt() dari base.py
    
    def _extract_response_text(self, response) -> str:
        try:
            if hasattr(response, 'text') and response.text: return response.text
            if hasattr(response, 'candidates') and response.candidates:
                return response.candidates[0].content.parts[0].text
            return str(response)
        except Exception:
            return "Maaf, terjadi kesalahan saat memformat jawaban."