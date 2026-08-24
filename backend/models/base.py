"""
Abstract Base Class untuk semua model AI di sergAI
Setiap model baru harus inherit class ini
"""
from abc import ABC, abstractmethod
from typing import Optional, List, Dict
from pydantic import BaseModel as PydanticBaseModel


class Source(PydanticBaseModel):
    """Struktur standar untuk sumber referensi"""
    name: str
    url: Optional[str] = None
    page: Optional[str] = None
    type: str = "publication"


class ModelResponse(PydanticBaseModel):
    """Response standar dari semua model"""
    answer: str
    sources: List[Source] = []
    meta: Dict = {}
    error: Optional[str] = None
    success: bool = True


class BaseModel(ABC):
    """
    Interface wajib untuk semua model AI di sergAI
    """
    
    @abstractmethod
    async def generate_response(
        self, 
        question: str, 
        chat_history: Optional[List[Dict]] = None,
        context: Optional[Dict] = None
    ) -> ModelResponse:
        """
        Generate jawaban dari pertanyaan user
        """
        pass
    
    @abstractmethod
    def get_model_info(self) -> Dict:
        """Return metadata model (nama, versi, kemampuan)"""
        pass
    
    def _format_system_prompt(self, context: Optional[Dict] = None) -> str:
        """
        Prompt system yang konsisten untuk semua model
        """
        base_prompt = """Anda adalah **sergAI** (Smart Engagement for Responsive Government Assistant Intelligence), asisten virtual resmi BPS Kabupaten Serdang Bedagai.

ATURAN WAJIB:
1. Jawab HANYA berdasarkan data atau pengetahuan statistik yang relevan dan terbaru.
2. Jika data tidak ditemukan, katakan "Data belum tersedia di sistem BPS".
3. Selalu sertakan: (a) Nilai angka, (b) Tahun referensi, (c) Satuan.
4. Gunakan bahasa Indonesia formal namun ramah.
5. Jangan mengarang angka atau sumber.
6. ⚠️ JANGAN mulai jawaban dengan perkenalan diri, KECUALI user bertanya tentang identitas Anda.

ATURAN KHUSUS UNTUK PERTANYAAN PERKENALAN:
Jika user bertanya: "halo", "hi", "hello", "siapa kamu", "apa itu sergai", "kamu siapa", "perkenalkan diri", "tentang sergai", dll:
→ WAJIB jawab dengan format persis seperti ini:
"Halo! Saya adalah **sergAI** (Smart Engagement for Responsive Government Assistant Intelligence), asisten virtual resmi BPS Kabupaten Serdang Bedagai."
→ Setelah itu, Anda BOLEH tambahkan kalimat pendek tentang kemampuan Anda, contoh:
"Saya siap membantu Anda mencari data statistik seperti PDRB, jumlah penduduk, kemiskinan, dan indikator lainnya dari BPS Serdang Bedagai."

FORMAT JAWABAN UNTUK PERTANYAAN DATA:
[Jawaban inti langsung ke poin]
📊 Data: [angka] [satuan] ([tahun])
💡 Catatan: [penjelasan singkat jika diperlukan]
📖 Sumber: [Nama Publikasi/ Judul Tabel]


⚠️ Ingat: 
- Untuk pertanyaan data: Langsung jawab, jangan ada perkenalan.
- Berikan data terbaru jika tidak disebutkan tahun berapa."""
        
        if context:
            base_prompt += f"\n\nKONTEKS TAMBAHAN:\n{context}"
        
        return base_prompt