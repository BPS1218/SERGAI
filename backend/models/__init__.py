"""
Models package - Modular AI Model Interface
"""
from .base import BaseModel, ModelResponse
from .gemini import GeminiModel
from .openai import OpenAIModel 
from .rag import RAGModel
from .rag_dynamic import RAGDynamicModel

# Factory function untuk load model
def get_model(model_name: str = "gemini"):
    """
    Factory: Return instance model berdasarkan nama
    Tambah model baru di sini tanpa ubah kode lain
    """
    models = {
        "gemini": GeminiModel,
        "openai": OpenAIModel,      
        "rag": RAGModel,
        "rag_dynamic": RAGDynamicModel,            # Nanti  
    }
    
    if model_name not in models:
        raise ValueError(f"Model '{model_name}' tidak terdaftar")
    
    return models[model_name]()

__all__ = ["BaseModel", "ModelResponse", "GeminiModel", "OpenAIModel", "RAGModel", "get_model"]