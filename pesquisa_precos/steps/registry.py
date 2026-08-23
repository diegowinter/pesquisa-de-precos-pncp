"""
Registry das etapas — fonte única da ordem, das dependências e dos metadados.

CLI, API e runner descobrem as etapas por aqui; ninguém hardcoda a sequência `0a → 8`
(docs/03_ETAPAS.md §2). Antes da Fase 1 a sequência estava escrita à mão em `rodar.py`,
junto com uma tabela de "quem aceita --provedor" — dois lugares para esquecer de atualizar.

Fase 8 (ADR-010): a extração é uma etapa só (`5`), com estratégias plugáveis (`janela`|
`completa`|`visao`|`auto`) roteadas por documento — `etapas/e5_extract.py`. Antes da Fase 8
ela existia como `5a` (parse/OCR) + `5b` (extração por janela), que a Fase 1 só extraiu
`executar()` de, sem mudar lógica; a unificação (e o download que passou da etapa 2 para
esta) é o que a Fase 8 entrega.

`params_model` é resolvido por import preguiçoso: o registry é importado pela CLI e pelo
`main()` de cada etapa, e importar as 12 etapas de uma vez arrastaria pymupdf/pandas/torch
para dentro de um `--help`.
"""

import importlib
from dataclasses import dataclass, field
from types import ModuleType
from typing import Literal

Custo = Literal["gratis", "cpu", "gpu", "pago"]


@dataclass(frozen=True)
class DefinicaoEtapa:
    chave: str
    titulo: str
    modulo: str                      # sem o prefixo `pesquisa_precos.steps.`
    depende_de: tuple[str, ...]
    custo: Custo
    precisa_gate: bool               # padrão do modo assistido
    recomputa_corpus: bool           # True = sempre recalcula o corpus inteiro, não só o novo
    # Capacidades (Fase 7: 'chat'|'embed'|'rerank'|'ocr') que a etapa consome — é o que
    # `runner.launcher` sonda ANTES de subir o subprocesso (health check pré-play,
    # docs/04_FASES.md §Fase 7 item 6). Etapa sem capacidade paga (custo='gratis') não tem
    # nenhuma.
    capacidades: tuple[str, ...] = ()
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
ETAPAS: tuple[DefinicaoEtapa, ...] = (
    DefinicaoEtapa("0a", "Obter catálogo CATMAT/CATSER", "e0a_catalogo",
                   (), "gratis", False, True),
    DefinicaoEtapa("1", "Gerar termos de busca", "e1_termos",
                   ("0a",), "pago", True, False, ("chat",)),
    DefinicaoEtapa("2", "Coletar no PNCP", "e2_collect",
                   ("1",), "gratis", True, False),
    DefinicaoEtapa("3", "Classificar itens", "e3_classify",
                   ("2",), "pago", True, False, ("chat",)),
    DefinicaoEtapa("4", "Cortar / definir escopo", "e4_cut",
                   ("3",), "gratis", True, True),
    # Fase 11 (ADR-019): `pdf` e `pareamento` são capacidades de primeira classe — é o que faz
    # o health check pré-play reprovar a etapa ANTES de começar quando o serviço está fora do
    # ar. ADR-021: `ocr` NÃO é declarado pela 5 e `embed` não é declarado pela 6a — os dois
    # rodam DENTRO dos serviços de `pdf` e `pareamento`, na máquina deles. Sondá-los daqui
    # reprovaria a etapa por um endereço que este processo nem usa.
    DefinicaoEtapa("5", "Extrair e enriquecer itens (download + OCR + LLM)", "e5_extract",
                   ("4",), "pago", False, False, ("pdf", "chat")),
    DefinicaoEtapa("6a", "Gerar pares + rejeitor híbrido", "e6a_pairs",
                   ("4", "5"), "gpu", False, True, ("pareamento",)),
    DefinicaoEtapa("6b", "Rerankear pares", "e6b_rerank",
                   ("6a",), "gpu", False, False, ("rerank",)),
    DefinicaoEtapa("6c", "Validar ambíguos (LLM)", "e6c_validate",
                   ("6b",), "pago", True, False, ("chat",)),
    DefinicaoEtapa("7", "Agrupar e ranquear", "e7_group",
                   ("6c",), "gratis", False, True),
    DefinicaoEtapa("8", "Exportar XLSX PLASEG", "e8_export",
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
