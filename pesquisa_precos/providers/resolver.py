"""
Resolução de provedor por capacidade — de onde vem "quem atende chat/embed/rerank/pdf/
pareamento agora".

**Uma fonte só: o banco** (`provedor` + `capacidade_provedor`, docs/02_SCHEMA.md §10),
configurado pela tela `/provedores`. Até a Fase 14 havia uma segunda, o `.env`, usada enquanto
`capacidade_provedor` estivesse vazia. Ela saiu na ADR-022, pela mesma razão que a ADR-020
tirou o `--fonte csv` e a ADR-021 tirou o caminho em processo: **dois caminhos para o mesmo
resultado divergem em silêncio**, e o do `.env` era o default de quem não configurou nada — ou
seja, o modo em que um erro de configuração vira, sem erro nenhum, a etapa rodando com o modelo
errado e a conta chegando depois.

Capacidade sem linha em `capacidade_provedor` agora levanta `CapacidadeNaoConfigurada`, e a
etapa para antes de começar. Não sobrou modo degradado — é a mesma dureza que a ADR-020 impôs
com `DATABASE_URL`.

`criar_chat`/`criar_embed`/`criar_rerank`/`criar_pdf`/`criar_pareamento` devolvem o adapter já
pronto (satisfaz o `Protocol` correspondente). `Provedores` é o objeto que `ctx.provedores`
expõe (docs/03_ETAPAS.md §1), resolvido e instanciado sob demanda — montar o cliente custa, e
uma etapa que só usa `chat` não deve pagar pelo resto — e cacheado pela vida do contexto.
"""

from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING


from pesquisa_precos.providers.protocolos import InfoProvedor

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


CAPACIDADES = ("chat", "embed", "rerank", "pdf", "pareamento")


class CapacidadeNaoConfigurada(RuntimeError):
    """Nenhum provedor aponta para esta capacidade (Fase 14, ADR-022). Antes isso caía para o
    `.env`; hoje é erro de configuração, resolvido na tela `/provedores`."""


class FallbackProibidoEmbedError(ValueError):
    """`capacidade_provedor.embed` tem `fallback` preenchido (ADR-006 §2) — bug de
    configuração: trocar de provedor de embedding no meio mistura espaços vetoriais em
    silêncio. `db.repos.execucao.apontar_capacidade` já recusa gravar isto; esta é a segunda
    trava, para o caso de a linha ter sido editada direto no banco."""


def _api_key_de(linha: dict, default: str = "") -> str:
    """A chave de API de um provedor cadastrado, em claro — só para montar o adapter.

    Este é o ÚNICO ponto do código que devolve segredo em claro. Nada acima daqui — service,
    API, template — pode chamar `db.segredo.decifrar`, e
    `tests/test_segredo.py::test_so_o_resolver_decifra` guarda a regra.

    O `api_key_ref` (nome de env var) que existia antes da ADR-022 sumiu junto com o caminho
    `.env`: a chave mora no banco, cifrada.
    """
    blob = linha.get("api_key_cifrada")
    if not blob:
        return default
    from pesquisa_precos.db import segredo as seg
    return seg.decifrar(bytes(blob), contexto=linha["provedor"])


def _como_float(v: Any) -> float | None:
    return None if v is None else float(v)


@dataclass
class ResolucaoCapacidade:
    """O que `resolver_capacidade` devolve: info + a chave de API já resolvida (nunca o
    `api_key_ref` cru) + de onde veio (log/diagnóstico — dashboard de provedores)."""

    info: InfoProvedor
    api_key: str
    origem: str  # sempre 'banco' desde a ADR-022; mantido para log/diagnóstico


def resolver_capacidade(capacidade: str, cfg: dict | None = None, *,
                        sessao: "Session | None" = None, provedor: str | None = None,
                        forte: bool = False, remoto: bool | None = None
                        ) -> ResolucaoCapacidade:
    """Resolve UMA capacidade pelo banco. Levanta `CapacidadeNaoConfigurada` se ninguém a
    atende.

    `cfg`/`provedor`/`forte`/`remoto` sobrevivem na assinatura sem efeito, como `fraco` na 6c
    depois da ADR-004 e `remoto` nas 6a/6b depois da ADR-021: o banco é a fonte de verdade de
    "qual modelo, qual endereço" (ADR-014), e é isso que permite trocar de provedor pela tela
    sem mudar código. Serão removidos quando os `Params` que os carregam forem.
    """
    if capacidade not in CAPACIDADES:
        raise ValueError(f"capacidade desconhecida: {capacidade!r} "
                         f"(use {'/'.join(CAPACIDADES)})")
    if sessao is None:
        from pesquisa_precos.db import sessao as db
        with db.sessao() as propria:
            return resolver_capacidade(capacidade, sessao=propria)

    from pesquisa_precos.db.repos import execucao as repo
    linha = repo.capacidade_provedor_info(sessao, capacidade)
    if linha is None:
        raise CapacidadeNaoConfigurada(
            f"nenhum provedor ATIVO atende a capacidade `{capacidade}`. Cadastre um em "
            f"/provedores e aponte-o para esta capacidade. (Até a Fase 14 isto caía para o "
            f"`.env`; a ADR-022 removeu esse caminho — ver o docstring de `resolver.py`.)")

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
        custo_usd_chamada=_como_float(linha.get("custo_usd_chamada")),
        fallback_provedor=fallback,
    )
    return ResolucaoCapacidade(info=info, api_key=_api_key_de(linha), origem="banco")


def criar_chat(cfg: dict | None = None, *, sessao: "Session | None" = None, provedor: str | None = None,
               forte: bool = False, curador_kwargs: dict | None = None):
    from pesquisa_precos.providers.adaptadores import ChatAdapter

    r = resolver_capacidade("chat", cfg, sessao=sessao, provedor=provedor, forte=forte)
    return ChatAdapter(r.info, api_key=r.api_key, curador_kwargs=curador_kwargs)


def _exigir_servico(r, capacidade: str):
    """Capacidade sem `base_url` é erro de configuração, não motivo para rodar aqui.

    Desde a ADR-021 não existe caminho em processo: embedding, rerank, OCR, parse de PDF e
    BM25 rodam nos serviços de `pncp-servicos-locais`. Cair silenciosamente num modo local
    faria o servidor que orquestra tentar carregar torch/PyMuPDF — exatamente o trabalho que
    ele não deve fazer — e só descobriríamos pelo `MemoryError` ou pela conta.
    """
    if not r.info.base_url:
        raise CapacidadeNaoConfigurada(
            f"provedor `{r.info.nome}` atende `{capacidade}` mas está sem base_url. Corrija em "
            f"/provedores, apontando para o serviço correspondente do repositório "
            f"`pncp-servicos-locais`.")
    return r


def criar_embed(cfg: dict | None = None, *, sessao: "Session | None" = None, remoto: bool | None = None,
                cache_path: str | None = None):
    from pesquisa_precos.providers.adaptadores import EmbedGpuCaseiraAdapter

    r = _exigir_servico(resolver_capacidade("embed", cfg, sessao=sessao, remoto=remoto), "embed")
    return EmbedGpuCaseiraAdapter(r.info, api_key=r.api_key, cache_path=cache_path)


def criar_rerank(cfg: dict | None = None, *, sessao: "Session | None" = None, remoto: bool | None = None,
                 batch: int | None = None):
    from pesquisa_precos.providers.adaptadores import RerankGpuCaseiraAdapter

    r = _exigir_servico(resolver_capacidade("rerank", cfg, sessao=sessao, remoto=remoto), "rerank")
    if batch:
        r.info = InfoProvedor(**{**r.info.__dict__, "batch_size": batch})
    return RerankGpuCaseiraAdapter(r.info, api_key=r.api_key)


def criar_pdf(cfg: dict | None = None, *, sessao: "Session | None" = None):
    """Capacidade `pdf` (ADR-019/ADR-021). Este processo baixa os arquivos; o serviço faz o
    parse, a rasterização e o OCR."""
    from pesquisa_precos.providers.adaptadores import PdfRemotoAdapter

    r = _exigir_servico(resolver_capacidade("pdf", cfg, sessao=sessao), "pdf")
    return PdfRemotoAdapter(r.info, api_key=r.api_key)


def criar_pareamento(cfg: dict | None = None, *, sessao: "Session | None" = None):
    """Capacidade `pareamento` (ADR-019/ADR-021) — BM25 + embedding + corte, no serviço."""
    from pesquisa_precos.providers.adaptadores import PareamentoRemotoAdapter

    r = _exigir_servico(resolver_capacidade("pareamento", cfg, sessao=sessao), "pareamento")
    return PareamentoRemotoAdapter(r.info, api_key=r.api_key)


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

    def resolucao_opcional(self, capacidade: str, **overrides) -> "ResolucaoCapacidade | None":
        """Como `resolucao`, mas devolve `None` em vez de levantar quando ninguém atende a
        capacidade.

        É o que `estimar()` usa. Estimativa é PREVIEW: ela roda quando o operador abre a tela
        da etapa, antes de qualquer play, e nesse momento é normal a configuração ainda estar
        incompleta. Derrubar a tela por isso esconderia justamente os números que o operador
        foi ali ver — e o gate de verdade (`checar_saude_previa`) continua no play, onde
        faltar provedor TEM de barrar.
        """
        try:
            return self.resolucao(capacidade, **overrides)
        except CapacidadeNaoConfigurada:
            return None

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

