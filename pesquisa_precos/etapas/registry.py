"""
Registry das etapas — fonte única da ordem, das dependências e dos metadados.

CLI, API e runner descobrem as etapas por aqui; ninguém hardcoda a sequência `0a → 8`
(docs/03_ETAPAS.md §2). Antes da Fase 1 a sequência estava escrita à mão em `rodar.py`,
junto com uma tabela de "quem aceita --provedor" — dois lugares para esquecer de atualizar.

Uma divergência consciente em relação à tabela de docs/03_ETAPAS.md §2.1: lá a extração é
uma etapa só (`5`). Aqui ela aparece como **`5a` (parse/OCR) e `5b` (extração por item)**,
que é o que existe hoje em código e o que o usuário roda. A unificação em `5` com
estratégias intercambiáveis é entrega da Fase 8 (ADR-010) — antecipá-la aqui seria mudar
lógica de extração numa fase cujo objetivo é só extrair `executar()`.

`params_model` é resolvido por import preguiçoso: o registry é importado pela CLI e pelo
`main()` de cada etapa, e importar as 12 etapas de uma vez arrastaria pymupdf/pandas/torch
para dentro de um `--help`.
"""

import importlib
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Literal

from pesquisa_precos.config import paths

Custo = Literal["gratis", "cpu", "gpu", "pago"]


@dataclass(frozen=True)
class DefinicaoEtapa:
    chave: str
    titulo: str
    modulo: str                      # sem o prefixo `pesquisa_precos.etapas.`
    depende_de: tuple[str, ...]
    custo: Custo
    precisa_gate: bool               # padrão do modo assistido
    recomputa_corpus: bool           # True = sempre recalcula o corpus inteiro, não só o novo
    caminho_erros: Path | None = None
    _cache: dict = field(default_factory=dict, repr=False, compare=False)

    def carregar(self) -> ModuleType:
        """Importa (uma vez) o módulo da etapa."""
        mod = self._cache.get("modulo")
        if mod is None:
            mod = self._cache["modulo"] = importlib.import_module(
                f"pesquisa_precos.etapas.{self.modulo}")
        return mod

    @property
    def params_model(self):
        return self.carregar().Params

    @property
    def versao_codigo(self) -> str:
        """Bumpada manualmente no módulo da etapa ao mudar a lógica (ver 08_CONVENCOES §5.6)."""
        return self.carregar().VERSAO_CODIGO


# `recomputa_corpus` distingue as etapas baratas de agregação — que precisam comparar itens
# novos contra os antigos ("mais barato por código" exige o corpus inteiro) — das caras, que
# só processam o inédito. É a regra que sustenta o custo do modo `atualizar`.
ETAPAS: tuple[DefinicaoEtapa, ...] = (
    DefinicaoEtapa("0a", "Obter catálogo CATMAT/CATSER", "e0a_catalogo",
                   (), "gratis", False, True),
    DefinicaoEtapa("1", "Gerar termos de busca", "e1_termos",
                   ("0a",), "pago", True, False, paths.ERROS_1),
    DefinicaoEtapa("2", "Coletar no PNCP", "e2_coletar",
                   ("1",), "gratis", True, False, paths.ERROS_2),
    DefinicaoEtapa("3", "Classificar itens", "e3_classificar",
                   ("2",), "pago", True, False, paths.ERROS_3),
    DefinicaoEtapa("4", "Cortar / definir escopo", "e4_cortar",
                   ("3",), "gratis", True, True),
    DefinicaoEtapa("5a", "Parse + OCR dos PDFs", "e5a_ocr",
                   ("4",), "gpu", False, False),
    DefinicaoEtapa("5b", "Extrair e enriquecer itens", "e5b_extrair",
                   ("5a",), "pago", False, False, paths.ERROS_5),
    DefinicaoEtapa("6a", "Gerar pares + rejeitor híbrido", "e6a_pares",
                   ("4", "5b"), "gpu", False, True),
    DefinicaoEtapa("6b", "Rerankear pares", "e6b_rerank",
                   ("6a",), "gpu", False, False),
    DefinicaoEtapa("6c", "Validar ambíguos (LLM)", "e6c_validar",
                   ("6b",), "pago", True, False, paths.ERROS_6C),
    DefinicaoEtapa("7", "Agrupar e ranquear", "e7_agrupar",
                   ("6c",), "gratis", False, True),
    DefinicaoEtapa("8", "Exportar XLSX PLASEG", "e8_exportar",
                   ("7",), "gratis", False, True),
)

_POR_CHAVE = {e.chave: e for e in ETAPAS}


def todas() -> tuple[DefinicaoEtapa, ...]:
    return ETAPAS


def obter(chave: str) -> DefinicaoEtapa:
    try:
        return _POR_CHAVE[chave]
    except KeyError:
        raise KeyError(
            f"etapa {chave!r} desconhecida — use uma de: {', '.join(_POR_CHAVE)}") from None


def ordem() -> list[DefinicaoEtapa]:
    """Ordem topológica (dependências antes dos dependentes), estável na ordem declarada."""
    resolvidas: list[DefinicaoEtapa] = []
    prontas: set[str] = set()
    restantes = list(ETAPAS)
    while restantes:
        avancou = False
        for etapa in list(restantes):
            if all(d in prontas for d in etapa.depende_de):
                resolvidas.append(etapa)
                prontas.add(etapa.chave)
                restantes.remove(etapa)
                avancou = True
        if not avancou:
            pendentes = ", ".join(e.chave for e in restantes)
            raise ValueError(f"ciclo ou dependência inexistente no registry: {pendentes}")
    return resolvidas


def dependentes(chave: str) -> list[str]:
    """Etapas que dependem (direta ou transitivamente) desta — quem fica `desatualizada`."""
    alvo = {chave}
    saida: list[str] = []
    for etapa in ordem():
        if etapa.chave != chave and alvo.intersection(etapa.depende_de):
            alvo.add(etapa.chave)
            saida.append(etapa.chave)
    return saida
