"""
Registry das etapas — fonte única da ordem, das dependências e dos metadados.

A web e o runner descobrem as etapas por aqui; ninguém escreve a sequência `0a -> 8` à mão
(docs/03_ETAPAS.md §2).

A extração é uma etapa só (`5`), com estratégias plugáveis (janela, completa, visão) roteadas
por documento — ver `steps/e5_extract.py` e a ADR-010.

`params_model` é resolvido por import preguiçoso: importar as 12 etapas de uma vez arrastaria
pandas para dentro de qualquer uso do registry, inclusive o de só listar os nomes.
"""

import importlib
from dataclasses import dataclass, field
from types import ModuleType
from typing import Literal

Custo = Literal["gratis", "cpu", "gpu", "pago"]


@dataclass(frozen=True)
class StepDefinition:
    key: str
    titulo: str
    modulo: str                      # sem o prefixo `pesquisa_precos.steps.`
    depends_on: tuple[str, ...]
    custo: Custo
    precisa_gate: bool               # padrão do modo assistido
    recomputa_corpus: bool           # True = sempre recalcula o corpus inteiro, não só o novo
    capabilities: tuple[str, ...] = ()
    _cache: dict = field(default_factory=dict, repr=False, compare=False)

    def carregar(self) -> ModuleType:
        """Importa (uma vez) o módulo da etapa."""
        mod = self._cache.get("modulo")
        if mod is None:
            mod = self._cache["modulo"] = importlib.import_module(
                f"pesquisa_precos.steps.{self.modulo}")
        return mod

    @property
    def params_model(self):
        return self.carregar().Params

    @property
    def versao_codigo(self) -> str:
        """Bumpada manualmente no módulo da etapa ao mudar a lógica (ver 08_CONVENCOES §5.6)."""
        return self.carregar().CODE_VERSION


# `recomputa_corpus` distingue as etapas baratas de agregação — que precisam comparar itens
# novos contra os antigos ("mais barato por código" exige o corpus inteiro) — das caras, que
# só processam o inédito. É a regra que sustenta o custo do modo `atualizar`.
ETAPAS: tuple[StepDefinition, ...] = (
    StepDefinition("0a", "Obter catálogo CATMAT/CATSER", "e0a_catalogo",
                   (), "gratis", False, True),
    StepDefinition("1", "Gerar termos de busca", "e1_termos",
                   ("0a",), "pago", True, False, ("chat",)),
    StepDefinition("2", "Coletar no PNCP", "e2_collect",
                   ("1",), "gratis", True, False),
    StepDefinition("3", "Classificar itens", "e3_classify",
                   ("2",), "pago", True, False, ("chat",)),
    StepDefinition("4", "Cortar / definir escopo", "e4_cut",
                   ("3",), "gratis", True, True),
    # Fase 11 (ADR-019): `pdf` e `pareamento` sãa capacidades de primeira classe — é o que faz
    # o health check pré-play reprovar a etapa ANTES de começar quando o serviço está fora do
    # ar. ADR-021: `ocr` NÃO é declarado pela 5 e `embed` não é declarado pela 6a — os dois
    # rodam DENTRO dos serviços de `pdf` e `pareamento`, na máquina deles. Sondá-los daqui
    # reprovaria a etapa por um endereço que este processo nem usa.
    StepDefinition("5", "Extrair e enriquecer itens (download + OCR + LLM)", "e5_extract",
                   ("4",), "pago", False, False, ("pdf", "chat")),
    StepDefinition("6a", "Gerar pares + rejeitor híbrido", "e6a_pairs",
                   ("4", "5"), "gpu", False, True, ("matching",)),
    StepDefinition("6b", "Rerankear pares", "e6b_rerank",
                   ("6a",), "gpu", False, False, ("rerank",)),
    StepDefinition("6c", "Validar ambíguos (LLM)", "e6c_validate",
                   ("6b",), "pago", True, False, ("chat",)),
    StepDefinition("7", "Agrupar e ranquear", "e7_group",
                   ("6c",), "gratis", False, True),
    StepDefinition("8", "Exportar XLSX PLASEG", "e8_export",
                   ("7",), "gratis", False, True),
)

_BY_KEY = {e.key: e for e in ETAPAS}


def todas() -> tuple[StepDefinition, ...]:
    return ETAPAS


def obter(key: str) -> StepDefinition:
    try:
        return _BY_KEY[key]
    except KeyError:
        raise KeyError(
            f"step {key!r} desconhecida — use uma de: {', '.join(_BY_KEY)}") from None


def ordem() -> list[StepDefinition]:
    """Ordem topológica (dependências antes dos dependentes), estável na ordem declarada."""
    resolvidas: list[StepDefinition] = []
    prontas: set[str] = set()
    restantes = list(ETAPAS)
    while restantes:
        avancou = False
        for etapa in list(restantes):
            if all(d in prontas for d in etapa.depends_on):
                resolvidas.append(etapa)
                prontas.add(etapa.key)
                restantes.remove(etapa)
                avancou = True
        if not avancou:
            pendentes = ", ".join(e.key for e in restantes)
            raise ValueError(f"ciclo ou dependência inexistente no registry: {pendentes}")
    return resolvidas


def dependentes(key: str) -> list[str]:
    """Etapas que dependem (direta ou transitivamente) desta — quem fica `desatualizada`."""
    alvo = {key}
    saida: list[str] = []
    for etapa in ordem():
        if etapa.key != key and alvo.intersection(etapa.depends_on):
            alvo.add(etapa.key)
            saida.append(etapa.key)
    return saida
