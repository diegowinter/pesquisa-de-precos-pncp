"""
Resolução de provedor por capacidade — de onde vem "quem atende chat/embed/rerank/pdf/
pareamento agora".

**Uma fonte só: o banco** (`provider` + `provider_capability`, docs/02_SCHEMA.md §10),
configurado pela tela `/providers`. Até a Fase 14 havia uma segunda, o `.env`, usada enquanto
`provider_capability` estivesse vazia. Ela saiu na ADR-022, pela mesma razão que a ADR-020
tirou o `--fonte csv` e a ADR-021 tirou o caminho em processo: **dois caminhos para o mesmo
resultado divergem em silêncio**, e o do `.env` era o default de quem não configurou nada — ou
seja, o modo em que um erro de configuração vira, sem erro nenhum, a etapa rodando com o modelo
errado e a conta chegando depois.

Capacidade sem linha em `provider_capability` agora levanta `CapabilityNotConfigured`, e a
etapa para antes de começar. Não sobrou modo degradado — é a mesma dureza que a ADR-020 impôs
com `DATABASE_URL`.

`criar_chat`/`criar_embed`/`criar_rerank`/`criar_pdf`/`create_matching` devolvem o adapter já
pronto (satisfaz o `Protocol` correspondente). `Provedores` é o objeto que `ctx.providers`
expõe (docs/03_ETAPAS.md §1), resolvido e instanciado sob demanda — montar o cliente custa, e
uma etapa que só usa `chat` não deve pagar pelo resto — e cacheado pela vida do contexto.
"""

from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING


from pesquisa_precos.providers.protocolos import ProviderInfo

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


CAPACIDADES = ("chat", "embed", "rerank", "pdf", "matching")


class CapabilityNotConfigured(RuntimeError):
    """Nenhum provedor aponta para esta capacidade (Fase 14, ADR-022). Antes isso caía para o
    `.env`; hoje é erro de configuração, resolvido na tela `/providers`."""


class FallbackProibidoEmbedError(ValueError):
    """`provider_capability.embed` tem `fallback` preenchido (ADR-006 §2) — bug de
    configuração: trocar de provedor de embedding no meio mistura espaços vetoriais em
    silêncio. `db.repos.execution.apontar_capacidade` já recusa gravar isto; esta é a segunda
    trava, para o caso de a linha ter sido editada direto no banco."""


def _api_key_de(linha: dict, default: str = "") -> str:
    """A chave de API de um provedor cadastrado, em claro — só para montar o adapter.

    Este é o ÚNICO ponto do código que devolve segredo em claro. Nada acima daqui — service,
    API, template — pode chamar `db.secret.decifrar`, e
    `tests/test_segredo.py::test_so_o_resolver_decifra` guarda a regra.

    O `api_key_ref` (nome de env var) que existia antes da ADR-022 sumiu junto com o caminho
    `.env`: a chave mora no banco, cifrada.
    """
    blob = linha.get("api_key_encrypted")
    if not blob:
        return default
    from pesquisa_precos.db import secret as seg
    return seg.decifrar(bytes(blob), context=linha["provider"])


def _como_float(v: Any) -> float | None:
    return None if v is None else float(v)


@dataclass
class ResolucaoCapacidade:
    """O que `resolver_capacidade` devolve: info + a chave de API já resolvida (nunca o
    `api_key_ref` cru) + de onde veio (log/diagnóstico — dashboard de provedores)."""

    info: ProviderInfo
    api_key: str
    source: str  # sempre 'banco' desde a ADR-022; mantido para log/diagnóstico


def resolver_capacidade(capability: str, *,
                        sessao: "Session | None" = None) -> ResolucaoCapacidade:
    """Resolve UMA capacidade pelo banco. Levanta `CapabilityNotConfigured` se ninguém a
    atende.

    Qual modelo e qual endereço atendem cada capacidade é decisão do banco (ADR-014) — é o
    que permite trocar de provedor pela tela sem mudar código.
    """
    if capability not in CAPACIDADES:
        raise ValueError(f"capability desconhecida: {capability!r} "
                         f"(use {'/'.join(CAPACIDADES)})")
    if sessao is None:
        from pesquisa_precos.db import session as db
        with db.session() as propria:
            return resolver_capacidade(capability, sessao=propria)

    from pesquisa_precos.db.repos import execution as repo
    linha = repo.capacidade_provedor_info(sessao, capability)
    if linha is None:
        raise CapabilityNotConfigured(
            f"nenhum provider ATIVO atende a capability `{capability}`. Cadastre um em "
            f"/providers e aponte-o para esta capability.")

    fallback = linha.get("fallback")
    if capability == "embed" and fallback:
        raise FallbackProibidoEmbedError(
            f"provider_capability.embed aponta fallback={fallback!r} — proibido "
            f"(ADR-006): falhar e parar a etapa é o comportamento correto em 'embed'.")
    info = ProviderInfo(
        name=linha["provider"], capability=capability, base_url=linha["base_url"],
        model=linha.get("model") or linha.get("default_model") or "",
        batch_size=linha.get("batch_size") or 32, rpm_limit=linha.get("rpm_limit"),
        cost_in_per_mtok=_como_float(linha.get("cost_in_per_mtok")),
        cost_out_per_mtok=_como_float(linha.get("cost_out_per_mtok")),
        cost_usd_per_call=_como_float(linha.get("cost_usd_per_call")),
        fallback_provider=fallback,
    )
    return ResolucaoCapacidade(info=info, api_key=_api_key_de(linha), source="banco")


def criar_chat(*, sessao: "Session | None" = None, curador_kwargs: dict | None = None):
    from pesquisa_precos.providers.adaptadores import ChatAdapter

    r = resolver_capacidade("chat", sessao=sessao)
    return ChatAdapter(r.info, api_key=r.api_key, curador_kwargs=curador_kwargs)


def _exigir_servico(r, capability: str):
    """Capacidade sem `base_url` é erro de configuração, não motivo para rodar aqui.

    Desde a ADR-021 não existe caminho em processo: embedding, rerank, OCR, parse de PDF e
    BM25 rodam nos serviços de `pncp-servicos-locais`. Cair silenciosamente num modo local
    faria o servidor que orquestra tentar carregar torch/PyMuPDF — exatamente o trabalho que
    ele não deve fazer — e só descobriríamos pelo `MemoryError` ou pela conta.
    """
    if not r.info.base_url:
        raise CapabilityNotConfigured(
            f"provider `{r.info.name}` atende `{capability}` mas está sem base_url. Corrija em "
            f"/providers, apontando para o serviço correspondente do repositório "
            f"`pncp-servicos-locais`.")
    return r


def criar_embed(*, sessao: "Session | None" = None):
    from pesquisa_precos.providers.adaptadores import EmbedGpuCaseiraAdapter

    r = _exigir_servico(resolver_capacidade("embed", sessao=sessao), "embed")
    return EmbedGpuCaseiraAdapter(r.info, api_key=r.api_key)


def criar_rerank(*, sessao: "Session | None" = None, batch: int | None = None):
    from pesquisa_precos.providers.adaptadores import RerankGpuCaseiraAdapter

    r = _exigir_servico(resolver_capacidade("rerank", sessao=sessao), "rerank")
    if batch:
        r.info = ProviderInfo(**{**r.info.__dict__, "batch_size": batch})
    return RerankGpuCaseiraAdapter(r.info, api_key=r.api_key)


def criar_pdf(*, sessao: "Session | None" = None):
    """Capacidade `pdf` (ADR-019/ADR-021). Este processo baixa os arquivos; o serviço faz o
    parse, a rasterização e o OCR."""
    from pesquisa_precos.providers.adaptadores import PdfRemotoAdapter

    r = _exigir_servico(resolver_capacidade("pdf", sessao=sessao), "pdf")
    return PdfRemotoAdapter(r.info, api_key=r.api_key)


def create_matching(*, sessao: "Session | None" = None):
    """Capacidade `pareamento` (ADR-019/ADR-021) — BM25 + embedding + corte, no serviço."""
    from pesquisa_precos.providers.adaptadores import PareamentoRemotoAdapter

    r = _exigir_servico(resolver_capacidade("matching", sessao=sessao), "matching")
    return PareamentoRemotoAdapter(r.info, api_key=r.api_key)


@dataclass
class Providers:
    """`ctx.providers` (docs/03_ETAPAS.md §1). Cada capacidade é resolvida e instanciada na
    PRIMEIRA vez que a etapa acessa o atributo, não na construção — importar
    sentence-transformers/torch para uma etapa que só usa `chat` seria desperdício.
    """

    _sessao: "Session | None" = None
    _cache: dict = field(default_factory=dict, repr=False, compare=False)

    def resolucao(self, capability: str) -> ResolucaoCapacidade:
        """Resolve a capacidade SEM instanciar o adapter — para quem só precisa saber quem vai
        atendê-la (nome do provedor para log, ou uma decisão de comportamento por provedor como
        o `reasoning_effort` da etapa 3) sem pagar o custo de montar o cliente."""
        return resolver_capacidade(capability, sessao=self._sessao)

    def resolucao_opcional(self, capability: str) -> "ResolucaoCapacidade | None":
        """Como `resolucao`, mas devolve `None` em vez de levantar quando ninguém atende a
        capability.

        É o que `estimar()` usa. Estimate é PREVIEW: ela roda quando o operador abre a tela
        da etapa, antes de qualquer play, e nesse momento é normal a configuração ainda estar
        incompleta. Derrubar a tela por isso esconderia justamente os números que o operador
        foi ali ver — e o gate de verdade (`checar_saude_previa`) continua no play, onde
        faltar provedor TEM de barrar.
        """
        try:
            return self.resolucao(capability)
        except CapabilityNotConfigured:
            return None

    @property
    def chat(self):
        if "chat" not in self._cache:
            self._cache["chat"] = criar_chat(sessao=self._sessao)
        return self._cache["chat"]

    @property
    def embed(self):
        if "embed" not in self._cache:
            self._cache["embed"] = criar_embed(sessao=self._sessao)
        return self._cache["embed"]

    @property
    def rerank(self):
        if "rerank" not in self._cache:
            self._cache["rerank"] = criar_rerank(sessao=self._sessao)
        return self._cache["rerank"]

    @property
    def pdf(self):
        if "pdf" not in self._cache:
            self._cache["pdf"] = criar_pdf(sessao=self._sessao)
        return self._cache["pdf"]

    @property
    def matching(self):
        if "matching" not in self._cache:
            self._cache["matching"] = create_matching(sessao=self._sessao)
        return self._cache["matching"]

    # ── instâncias NÃO cacheadas ──────────────────────────────────────────────
    # Etapas com concorrência (3, 1) precisam de um cliente HTTP por thread — compartilhar
    # um único cliente serializa as chamadas e mata a concorrência (comentário original na
    # etapa 3). `.chat`/`.embed`/... acima bastam para etapas sequenciais; estas variantes
    # `novo_*` resolvem a MESMA capacidade (banco → `.env`, ADR-006) mas sempre instanciam de
    # novo, para o chamador guardar num `threading.local()` como já fazia antes da Fase 7.

    def novo_chat(self, *, curador_kwargs: dict | None = None):
        return criar_chat(sessao=self._sessao, curador_kwargs=curador_kwargs)

    def novo_embed(self):
        return criar_embed(sessao=self._sessao)

    def novo_rerank(self, *, batch: int | None = None):
        return criar_rerank(sessao=self._sessao, batch=batch)

