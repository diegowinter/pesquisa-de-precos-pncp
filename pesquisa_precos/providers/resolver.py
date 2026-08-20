"""
Resolução de provedor por capacidade (Fase 7, ADR-006) — de onde vem "quem atende chat/embed/
rerank/ocr agora".

Duas fontes, nesta ordem:
  1. **Banco** (`provedor` + `capacidade_provedor`, docs/02_SCHEMA.md §10) — o caminho normal
     depois que o operador configura provedores pela interface (ADR-014: "modelo, provedor,
     URL da GPU" é config, não código). Editável sem deploy.
  2. **`.env`** (`config.settings`) — usado enquanto `capacidade_provedor` está vazia, que é o
     estado de hoje (o acervo real ainda não foi migrado; `provedor`/`capacidade_provedor`
     existem no schema mas ninguém os populou). Garante que as etapas continuam rodando
     exatamente como rodavam antes da Fase 7 existir — nada quebra por a interface ainda não
     ter sido usada.

`criar_chat`/`criar_embed`/`criar_rerank`/`criar_ocr` devolvem o adapter já pronto (satisfaz o
`Protocol` correspondente). `Provedores` é o objeto que `ctx.provedores` expõe
(docs/03_ETAPAS.md §1: `ctx.provedores.chat` / `.embed` / `.rerank` / `.ocr`), resolvido e
instanciado sob demanda — importar torch/sentence-transformers custa caro, então só acontece
quando a capacidade é de fato usada — e cacheado pela vida do contexto.
"""

from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

import os

from pesquisa_precos.providers.protocolos import InfoProvedor

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


class FallbackProibidoEmbedError(ValueError):
    """`capacidade_provedor.embed` tem `fallback` preenchido (ADR-006 §2) — bug de
    configuração: trocar de provedor de embedding no meio mistura espaços vetoriais em
    silêncio. `db.repos.execucao.apontar_capacidade` já recusa gravar isto; esta é a segunda
    trava, para o caso de a linha ter sido editada direto no banco."""


def _api_key_de(api_key_ref: str | None, default: str = "") -> str:
    """`provedor.api_key_ref` guarda o NOME da env var, nunca a chave (§5.10 CONVENÇÕES)."""
    if not api_key_ref:
        return default
    return os.getenv(api_key_ref, default)


def _como_float(v: Any) -> float | None:
    return None if v is None else float(v)


@dataclass
class ResolucaoCapacidade:
    """O que `resolver_capacidade` devolve: info + a chave de API já resolvida (nunca o
    `api_key_ref` cru) + de onde veio (log/diagnóstico — dashboard de provedores)."""

    info: InfoProvedor
    api_key: str
    origem: str  # 'banco' | 'env'


def resolver_capacidade(capacidade: str, cfg: dict, *, sessao: "Session | None" = None,
                        provedor: str | None = None, forte: bool = False,
                        remoto: bool | None = None) -> ResolucaoCapacidade:
    """Resolve UMA capacidade — banco primeiro, `.env` como herança enquanto o banco está vazio.

    `provedor`/`forte`/`remoto` só têm efeito na resolução via `.env` (são os overrides que as
    etapas já aceitam por CLI hoje: `--provedor`, `--forte`, `--remoto`). Resolução via banco
    ignora os três: o banco já é a fonte de verdade de "qual modelo, qual GPU" (ADR-014) — é
    isso que permite trocar de provedor pela interface sem mudar código.
    """
    if sessao is not None:
        from pesquisa_precos.db.repos import execucao as repo
        linha = repo.capacidade_provedor_info(sessao, capacidade)
        if linha is not None:
            fallback = linha.get("fallback")
            if capacidade == "embed" and fallback:
                raise FallbackProibidoEmbedError(
                    f"capacidade_provedor.embed aponta fallback={fallback!r} — proibido "
                    f"(ADR-006): falhar e parar a etapa é o comportamento correto em 'embed'.")
            info = InfoProvedor(
                nome=linha["provedor"], capacidade=capacidade, base_url=linha["base_url"],
                modelo=linha.get("modelo") or linha.get("modelo_padrao") or "",
                batch_size=linha.get("batch_size") or 32, rpm_limite=linha.get("rpm_limite"),
                custo_in_por_mtok=_como_float(linha.get("custo_in_por_mtok")),
                custo_out_por_mtok=_como_float(linha.get("custo_out_por_mtok")),
                fallback_provedor=fallback,
            )
            api_key = _api_key_de(linha.get("api_key_ref"))
            return ResolucaoCapacidade(info=info, api_key=api_key, origem="banco")
    return _resolver_via_env(capacidade, cfg, provedor=provedor, forte=forte, remoto=remoto)


def _resolver_via_env(capacidade: str, cfg: dict, *, provedor: str | None, forte: bool,
                      remoto: bool | None) -> ResolucaoCapacidade:
    """Espelha o comportamento pré-Fase-7 (`config.settings.resolver_provedor` + as chaves de
    GPU/OCR soltas em `cfg`) — é o que garante que uma instalação sem `provedor` cadastrado no
    banco continua funcionando como sempre funcionou."""
    from pesquisa_precos.config.settings import resolver_provedor

    if capacidade == "chat":
        nome = provedor or ("openrouter" if forte else "local")
        p = resolver_provedor(cfg, nome, forte=forte)
        # `.env` só guarda preço POR CHAMADA (Fase 3, sem detalhe de tokens) — não dá para
        # derivar um preço por Mtok sem inventar um tamanho médio de prompt/resposta, então
        # `custo_in_por_mtok`/`custo_out_por_mtok` ficam `None` (só o banco os preenche).
        info = InfoProvedor(nome=nome, capacidade="chat", base_url=p["base_url"],
                            modelo=p["model"])
        return ResolucaoCapacidade(info=info, api_key=p["api_key"], origem="env")

    if capacidade in ("embed", "rerank"):
        modelo_cfg = "embedder_model" if capacidade == "embed" else "reranker_model"
        if remoto:
            info = InfoProvedor(nome="gpu_caseira", capacidade=capacidade,
                                base_url=cfg["gpu_base_url"], modelo=cfg[modelo_cfg])
            return ResolucaoCapacidade(info=info, api_key=cfg["gpu_api_key"], origem="env")
        info = InfoProvedor(nome="local", capacidade=capacidade, base_url="",
                            modelo=cfg[modelo_cfg])
        return ResolucaoCapacidade(info=info, api_key="", origem="env")

    if capacidade == "ocr":
        info = InfoProvedor(nome="ocr_local", capacidade="ocr", base_url=cfg["ocr_base_url"],
                            modelo=cfg["ocr_model"])
        return ResolucaoCapacidade(info=info, api_key=cfg["ocr_api_key"], origem="env")

    if capacidade in ("pdf", "pareamento"):
        # `base_url` vazio = roda em processo (comportamento pré-Fase-11). O adapter é quem
        # decide, para que a etapa não precise saber onde o processamento acontece.
        base = cfg[f"{capacidade}_base_url"]
        info = InfoProvedor(nome="remoto" if base else "local", capacidade=capacidade,
                            base_url=base, modelo="")
        return ResolucaoCapacidade(info=info, api_key=cfg[f"{capacidade}_api_key"],
                                   origem="env")

    raise ValueError(f"capacidade desconhecida: {capacidade!r} "
                     f"(use chat/embed/rerank/ocr/pdf/pareamento)")


def criar_chat(cfg: dict, *, sessao: "Session | None" = None, provedor: str | None = None,
               forte: bool = False, curador_kwargs: dict | None = None):
    from pesquisa_precos.providers.adaptadores import ChatAdapter

    r = resolver_capacidade("chat", cfg, sessao=sessao, provedor=provedor, forte=forte)
    return ChatAdapter(r.info, api_key=r.api_key, curador_kwargs=curador_kwargs)


def criar_embed(cfg: dict, *, sessao: "Session | None" = None, remoto: bool | None = None,
                cache_path: str | None = None):
    from pesquisa_precos.providers.adaptadores import EmbedGpuCaseiraAdapter, EmbedProcessoAdapter

    r = resolver_capacidade("embed", cfg, sessao=sessao, remoto=remoto)
    if r.info.nome == "local" or not r.info.base_url:
        return EmbedProcessoAdapter(r.info, cache_path=cache_path)
    return EmbedGpuCaseiraAdapter(r.info, api_key=r.api_key, cache_path=cache_path)


def criar_rerank(cfg: dict, *, sessao: "Session | None" = None, remoto: bool | None = None,
                 batch: int | None = None):
    from pesquisa_precos.providers.adaptadores import RerankGpuCaseiraAdapter, RerankProcessoAdapter

    r = resolver_capacidade("rerank", cfg, sessao=sessao, remoto=remoto)
    if batch:
        r.info = InfoProvedor(**{**r.info.__dict__, "batch_size": batch})
    if r.info.nome == "local" or not r.info.base_url:
        return RerankProcessoAdapter(r.info)
    return RerankGpuCaseiraAdapter(r.info, api_key=r.api_key)


def criar_pdf(cfg: dict, *, sessao: "Session | None" = None):
    """Capacidade `pdf` (ADR-019). Sem `PDF_BASE_URL`, cai no adapter em processo — que é o
    único que ainda importa PyMuPDF, e por isso vive atrás de um import tardio."""
    from pesquisa_precos.providers.adaptadores import PdfEmProcessoAdapter, PdfRemotoAdapter

    r = resolver_capacidade("pdf", cfg, sessao=sessao)
    if not r.info.base_url:
        return PdfEmProcessoAdapter(r.info, cfg=cfg)
    return PdfRemotoAdapter(r.info, api_key=r.api_key)


def criar_pareamento(cfg: dict, *, sessao: "Session | None" = None):
    """Capacidade `pareamento` (ADR-019). Sem `PAREAMENTO_BASE_URL`, roda em processo."""
    from pesquisa_precos.providers.adaptadores import (
        PareamentoEmProcessoAdapter,
        PareamentoRemotoAdapter,
    )

    r = resolver_capacidade("pareamento", cfg, sessao=sessao)
    if not r.info.base_url:
        return PareamentoEmProcessoAdapter(r.info, cfg=cfg)
    return PareamentoRemotoAdapter(r.info, api_key=r.api_key)


def criar_ocr(cfg: dict, *, sessao: "Session | None" = None):
    from pesquisa_precos.providers.adaptadores import OcrLocalAdapter

    r = resolver_capacidade("ocr", cfg, sessao=sessao)
    return OcrLocalAdapter(r.info, api_key=r.api_key)


@dataclass
class Provedores:
    """`ctx.provedores` (docs/03_ETAPAS.md §1). Cada capacidade é resolvida e instanciada na
    PRIMEIRA vez que a etapa acessa o atributo, não na construção — importar
    sentence-transformers/torch para uma etapa que só usa `chat` seria desperdício.
    """

    _cfg: dict
    _sessao: "Session | None" = None
    _cache: dict = field(default_factory=dict, repr=False, compare=False)

    def resolucao(self, capacidade: str, **overrides) -> ResolucaoCapacidade:
        """Resolve (banco → `.env`) SEM instanciar o adapter — para quem só precisa saber
        "quem vai atender isso" (nome do provedor p/ log, decisão de comportamento por
        provedor como o `reasoning_effort` da etapa 3) sem pagar o custo de montar o cliente."""
        return resolver_capacidade(capacidade, self._cfg, sessao=self._sessao, **overrides)

    @property
    def chat(self):
        if "chat" not in self._cache:
            self._cache["chat"] = criar_chat(self._cfg, sessao=self._sessao)
        return self._cache["chat"]

    @property
    def embed(self):
        if "embed" not in self._cache:
            self._cache["embed"] = criar_embed(self._cfg, sessao=self._sessao)
        return self._cache["embed"]

    @property
    def rerank(self):
        if "rerank" not in self._cache:
            self._cache["rerank"] = criar_rerank(self._cfg, sessao=self._sessao)
        return self._cache["rerank"]

    @property
    def ocr(self):
        if "ocr" not in self._cache:
            self._cache["ocr"] = criar_ocr(self._cfg, sessao=self._sessao)
        return self._cache["ocr"]

    @property
    def pdf(self):
        if "pdf" not in self._cache:
            self._cache["pdf"] = criar_pdf(self._cfg, sessao=self._sessao)
        return self._cache["pdf"]

    @property
    def pareamento(self):
        if "pareamento" not in self._cache:
            self._cache["pareamento"] = criar_pareamento(self._cfg, sessao=self._sessao)
        return self._cache["pareamento"]

    # ── instâncias NÃO cacheadas ──────────────────────────────────────────────
    # Etapas com concorrência (3, 1) precisam de um cliente HTTP por thread — compartilhar
    # um único cliente serializa as chamadas e mata a concorrência (comentário original na
    # etapa 3). `.chat`/`.embed`/... acima bastam para etapas sequenciais; estas variantes
    # `novo_*` resolvem a MESMA capacidade (banco → `.env`, ADR-006) mas sempre instanciam de
    # novo, para o chamador guardar num `threading.local()` como já fazia antes da Fase 7.

    def novo_chat(self, *, provedor: str | None = None, forte: bool = False,
                  curador_kwargs: dict | None = None):
        return criar_chat(self._cfg, sessao=self._sessao, provedor=provedor, forte=forte,
                          curador_kwargs=curador_kwargs)

    def novo_embed(self, *, remoto: bool | None = None, cache_path: str | None = None):
        return criar_embed(self._cfg, sessao=self._sessao, remoto=remoto, cache_path=cache_path)

    def novo_rerank(self, *, remoto: bool | None = None, batch: int | None = None):
        return criar_rerank(self._cfg, sessao=self._sessao, remoto=remoto, batch=batch)

    def novo_ocr(self):
        return criar_ocr(self._cfg, sessao=self._sessao)
