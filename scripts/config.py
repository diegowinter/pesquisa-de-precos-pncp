"""
Configuração central da pipeline v2 (`.env`), com defaults e validação por etapa.

Carrega o `.env` da pasta do projeto (e, como fallback, o da raiz do repositório) e
expõe todas as variáveis da seção 1.5 do guia já resolvidas, com defaults sensatos.

Convenção de provedores de LLM:
  - `openrouter` (pago)  → OPENAI_API_KEY / OPENAI_BASE_URL / OPENAI_MODEL_PASS1|PASS2
  - `local` (LM Studio)  → LOCAL_API_KEY / LOCAL_BASE_URL / LOCAL_MODEL

`resolver_provedor(nome)` devolve o trio (model, base_url, api_key) para o `Curador`.
Cada etapa que precisa de LLM/embeddings/OCR chama `exigir(cfg, *chaves)` para falhar
cedo com mensagem clara se faltar configuração.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

_PKG_DIR = Path(__file__).resolve().parent.parent          # itens-contratos-atas-v2/
_REPO_DIR = _PKG_DIR.parent                                 # raiz do repositório

load_dotenv(_PKG_DIR / ".env")
load_dotenv(_REPO_DIR / ".env")


def _f(nome: str, default: float) -> float:
    try:
        return float(os.getenv(nome, default))
    except (TypeError, ValueError):
        return default


def _i(nome: str, default: int) -> int:
    try:
        return int(os.getenv(nome, default))
    except (TypeError, ValueError):
        return default


def carregar_config() -> dict:
    """Resolve toda a configuração da pipeline a partir do ambiente/.env."""
    return {
        # OpenRouter / OpenAI-compat (pago)
        "openrouter_api_key": os.getenv("OPENAI_API_KEY", ""),
        "openrouter_base_url": os.getenv("OPENAI_BASE_URL", "https://openrouter.ai/api/v1"),
        "model_pass1": os.getenv("OPENAI_MODEL_PASS1", ""),
        "model_pass2": os.getenv("OPENAI_MODEL_PASS2", ""),
        # Modelo de VISÃO (etapa 5_alt_a: extrai a tabela direto da imagem da página).
        "model_vision": os.getenv("OPENAI_MODEL_VISION", ""),
        # LM Studio (local, OpenAI-compatible)
        "local_base_url": os.getenv("LOCAL_BASE_URL", "http://localhost:1234/v1"),
        "local_model": os.getenv("LOCAL_MODEL", ""),
        "local_api_key": os.getenv("LOCAL_API_KEY", "lm-studio"),
        # OCR local
        "ocr_base_url": os.getenv("OCR_BASE_URL", "http://localhost:8000/v1"),
        "ocr_model": os.getenv("OCR_MODEL", ""),
        "ocr_api_key": os.getenv("OCR_API_KEY", "ocr"),
        # Servidor de GPU (embedder 6a + reranker 6b) — opcional, via --remoto.
        "gpu_base_url": os.getenv("GPU_BASE_URL", "http://localhost:8100"),
        "gpu_api_key": os.getenv("GPU_API_KEY", "gpu"),
        # Modelos em processo (sentence-transformers)
        "embedder_model": os.getenv("EMBEDDER_MODEL", "BAAI/bge-m3"),
        "reranker_model": os.getenv("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3"),
        # Thresholds calibráveis
        "rejeitor_threshold": _f("REJEITOR_THRESHOLD", 0.30),
        "rerank_t_aceita": _f("RERANK_T_ACEITA", 0.80),
        "rerank_t_rejeita": _f("RERANK_T_REJEITA", 0.30),
        "min_itens": _i("MIN_ITENS", 1),
        "top_n": _i("TOP_N", 5),
    }


def resolver_provedor(cfg: dict, provedor: str, forte: bool = False) -> dict:
    """
    Devolve {model, base_url, api_key} para o `Curador`, conforme o provedor pedido.

    provedor: 'local' (LM Studio) ou 'openrouter' (pago).
    forte:    quando 'openrouter', usa PASS2 (modelo caro) em vez de PASS1.
              (ignorado no 'local', que tem um único modelo.)
    """
    if provedor == "local":
        return {
            "model": cfg["local_model"],
            "base_url": cfg["local_base_url"],
            "api_key": cfg["local_api_key"],
        }
    if provedor == "openrouter":
        return {
            "model": cfg["model_pass2"] if forte else cfg["model_pass1"],
            "base_url": cfg["openrouter_base_url"],
            "api_key": cfg["openrouter_api_key"],
        }
    raise ValueError(f"Provedor desconhecido: {provedor!r} (use 'local' ou 'openrouter').")


# Validações por escopo: cada chave mapeia para as vars obrigatórias e uma dica.
_REQUISITOS = {
    "openrouter": (
        ("openrouter_api_key", "model_pass1", "model_pass2"),
        "OPENAI_API_KEY / OPENAI_MODEL_PASS1 / OPENAI_MODEL_PASS2 (provedor openrouter)",
    ),
    "local": (
        ("local_model",),
        "LOCAL_MODEL (LM Studio; confira também LOCAL_BASE_URL)",
    ),
    "ocr": (
        ("ocr_model",),
        "OCR_MODEL (servidor de OCR; confira também OCR_BASE_URL)",
    ),
}


def exigir(cfg: dict, *escopos: str) -> str | None:
    """
    Retorna uma mensagem de erro se faltar alguma var obrigatória p/ os escopos pedidos,
    senão None. Escopos: 'openrouter', 'local', 'ocr'.
    """
    faltando = []
    for escopo in escopos:
        chaves, dica = _REQUISITOS[escopo]
        if any(not cfg.get(k) for k in chaves):
            faltando.append(dica)
    if faltando:
        return "Configuração incompleta no .env:\n  - " + "\n  - ".join(faltando)
    return None
