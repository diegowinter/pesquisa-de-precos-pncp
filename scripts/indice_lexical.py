"""
Índice léxico BM25 por categoria (etapa 6a).

Corpus = descrições finais dos itens PNCP; query = nome+descrição do item de catálogo.
Tokenização compartilhada: lowercase + remoção de acento (unidecode) + split alfanumérico.
Sem estado global — o script 6a constrói um índice por categoria e pontua as queries.
"""

import re
import unicodedata

from rank_bm25 import BM25Okapi

_RE_ALFANUM = re.compile(r"[0-9a-z]+")


def _sem_acento(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def tokenizar(texto: str) -> list[str]:
    """lowercase + sem acento + split alfanumérico (tokens de 1+ char)."""
    return _RE_ALFANUM.findall(_sem_acento(str(texto)).lower())


class IndiceBM25:
    """Envelopa um BM25Okapi sobre um corpus fixo (uma categoria)."""

    def __init__(self, corpus: list[str]):
        self._corpus_tokens = [tokenizar(t) for t in corpus]
        # BM25Okapi quebra com corpus vazio; guardamos o estado para pontuar como 0.
        self._vazio = not any(self._corpus_tokens)
        self._bm25 = None if self._vazio else BM25Okapi(self._corpus_tokens)

    def pontuar(self, query: str):
        """Devolve um array de scores BM25 (um por doc do corpus) para a query."""
        import numpy as np
        if self._vazio:
            return np.zeros(len(self._corpus_tokens), dtype=float)
        return self._bm25.get_scores(tokenizar(query))
