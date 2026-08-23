"""
Estratégia `completa` (Fase 8, ADR-010) — uma chamada com o texto do documento inteiro →
lista estruturada de itens (`documento_extracao.itens_json`), depois uma chamada barata por
item para casar contra a linha certa. Amortiza melhor que a `janela` em documentos com muitos
itens (a `janela` paga uma chamada cara POR item; aqui uma chamada cara serve o documento
inteiro).

Requisitos herdados da `janela`, obrigatórios (docs/02_SCHEMA.md §6.2) — a implementação
anterior (`etapas/e5_alt_b_casar.py`, pré-Fase-8) validava só por preço e não derivava
`doc_status`; NÃO é referência válida:
  1. confirmação por quantidade (`strategies.base.validar_extracao`, igual à `janela`);
  2. banda de sanidade de preço (idem);
  3. `doc_status` derivado do documento inteiro (idem, calculado por quem chama);
  4. chunking por página COM OVERLAP para documentos grandes (abaixo) — 5,6% do acervo passa
     de 40k tokens; truncar em silêncio faz item sumir sem erro.
"""

# Aproximação char↔token (pt-BR, texto de contrato): ~4 chars/token. Não é medição exata —
# é só o suficiente para decidir QUANDO dividir; a estimativa de custo real (llm_chamada) usa
# tokens de verdade, retornados pelo provedor.
CHARS_POR_TOKEN = 4
CHUNK_MAX_TOKENS = 12_000              # teto de entrada por chamada de `completa`
CHUNK_MAX_CHARS = CHUNK_MAX_TOKENS * CHARS_POR_TOKEN
CHUNK_OVERLAP_CHARS = 2_000            # overlap entre chunks — evita cortar um item ao meio


def dividir_em_chunks(texto: str, max_chars: int = CHUNK_MAX_CHARS,
                      overlap: int = CHUNK_OVERLAP_CHARS) -> list[str]:
    """Divide `texto` em pedaços de até `max_chars`, com `overlap` chars de sobreposição entre
    pedaços consecutivos. Um item cuja linha caia perto de uma fronteira de chunk ainda
    aparece inteiro em pelo menos um dos dois — é o que evita "item sumir sem erro" que o
    truncamento simples causaria (ver docstring do módulo)."""
    if len(texto) <= max_chars:
        return [texto] if texto else []
    chunks = []
    ini = 0
    n = len(texto)
    while ini < n:
        fim = min(n, ini + max_chars)
        chunks.append(texto[ini:fim])
        if fim >= n:
            break
        ini = fim - overlap
    return chunks


def _chave_dedup(linha: dict) -> tuple[str, str]:
    return (str(linha.get("numero_item", "")).strip(), str(linha.get("descricao", "")).strip()[:80])


def extrair_tabela(curador, texto_doc: str) -> list[dict]:
    """Extrai a tabela de itens do documento inteiro, em chunks com overlap. Concatena os
    resultados de todos os chunks e deduplica por (numero_item, início da descrição) — a
    região de overlap tende a produzir a mesma linha duas vezes."""
    vistas: set[tuple[str, str]] = set()
    tabela: list[dict] = []
    for chunk in dividir_em_chunks(texto_doc):
        for linha in curador.extrair_tabela_texto(chunk):
            if not (linha.get("descricao") or "").strip():
                continue
            key = _chave_dedup(linha)
            if key in vistas:
                continue
            vistas.add(key)
            tabela.append(linha)
    return tabela
