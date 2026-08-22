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

from dotenv import load_dotenv

from pesquisa_precos.config.paths import RAIZ

# O `.env` sempre morou na raiz do projeto. Antes da Fase 0 ela era deduzida da profundidade
# deste arquivo (`parent.parent`); agora vem de `paths.RAIZ`, que não muda quando um módulo é
# movido de lugar. Carregar do lugar errado degradaria em silêncio: `carregar_config()` tem
# default para tudo, então a pipeline rodaria com modelo/URL/threshold errados sem avisar.
# O segundo load é o fallback histórico para um `.env` um nível acima (herdado de quando este
# projeto era uma subpasta de `itens-via-script`). `load_dotenv` não sobrescreve o que já foi
# definido, então o `.env` da raiz continua tendo precedência.
load_dotenv(RAIZ / ".env")
load_dotenv(RAIZ.parent / ".env")


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
        # ── Serviços de `pncp-servicos-locais` (ADR-019/ADR-021) ─────────────────────
        # Este processo baixa, orquestra e grava no banco; GPU e CPU intensiva ficam do outro
        # lado de um HTTP. Vazio NÃO é mais "roda aqui": é erro de configuração, e a etapa
        # para antes de começar (`providers/resolver._exigir_servico`).
        "gpu_base_url": os.getenv("GPU_BASE_URL", "http://localhost:8100"),
        "gpu_api_key": os.getenv("GPU_API_KEY", "gpu"),
        "pdf_base_url": os.getenv("PDF_BASE_URL", ""),
        "pdf_api_key": os.getenv("PDF_API_KEY", "pdf"),
        "pareamento_base_url": os.getenv("PAREAMENTO_BASE_URL", ""),
        "pareamento_api_key": os.getenv("PAREAMENTO_API_KEY", "pareamento"),
        # `OCR_*` NÃO mora aqui: quem chama o OCR é o serviço de `pdf`, na máquina dele, e é
        # no `.env` DE LÁ que ele se configura.
        # Nomes de modelo, para log e para a tela de provedores — quem carrega são os serviços.
        "embedder_model": os.getenv("EMBEDDER_MODEL", "BAAI/bge-m3"),
        "reranker_model": os.getenv("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3"),
        # Thresholds calibráveis
        "rejeitor_threshold": _f("REJEITOR_THRESHOLD", 0.30),
        "rerank_t_aceita": _f("RERANK_T_ACEITA", 0.80),
        "rerank_t_rejeita": _f("RERANK_T_REJEITA", 0.30),
        "min_itens": _i("MIN_ITENS", 1),
        "top_n": _i("TOP_N", 5),
        # Preço médio de UMA chamada, por modelo, em USD. Só existe para o `estimar` das
        # etapas: sem número, ele responde "não estimado" em vez de inventar. A medição real
        # (tokens por chamada, gravados em `llm_chamada`) é entrega da Fase 3 — até lá quem
        # sabe o preço é o operador, e ele o informa aqui.
        "custo_usd_chamada_pass1": _f("CUSTO_USD_CHAMADA_PASS1", 0.0),
        "custo_usd_chamada_pass2": _f("CUSTO_USD_CHAMADA_PASS2", 0.0),
    }


def custo_por_chamada(cfg: dict, provedor: str, forte: bool = False) -> float | None:
    """USD por chamada de LLM, ou None quando não configurado (ver `carregar_config`).

    O provedor `local` (LM Studio na GPU caseira) não custa dinheiro: devolve 0.0.
    """
    if provedor == "local":
        return 0.0
    chave = "custo_usd_chamada_pass2" if forte else "custo_usd_chamada_pass1"
    valor = cfg.get(chave) or 0.0
    return valor or None


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
}


def exigir(cfg: dict, *escopos: str) -> str | None:
    """
    Retorna uma mensagem de erro se faltar alguma var obrigatória p/ os escopos pedidos,
    senão None. Escopos: 'openrouter', 'local'.
    """
    faltando = []
    for escopo in escopos:
        chaves, dica = _REQUISITOS[escopo]
        if any(not cfg.get(k) for k in chaves):
            faltando.append(dica)
    if faltando:
        return "Configuração incompleta no .env:\n  - " + "\n  - ".join(faltando)
    return None
