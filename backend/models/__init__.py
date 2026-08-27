"""
Models package - Modular AI Model Interface
"""
from .base import BaseModel, ModelResponse
from .gemini import GeminiModel
from .openai import OpenAIModel
from .rag_unified import RAGUnifiedModel

def get_model(model_name: str = "rag_unified"):
    """Factory: return instance model berdasarkan nama"""
    models = {
        "gemini": GeminiModel,
        "openai": OpenAIModel,
        "rag_unified": RAGUnifiedModel,
    }
    if model_name not in models:
        raise ValueError(f"Model '{model_name}' tidak terdaftar")
    return models[model_name]()

__all__ = [
    "BaseModel", "ModelResponse",
    "GeminiModel", "OpenAIModel",
    "RAGUnifiedModel",
    "get_model",
]