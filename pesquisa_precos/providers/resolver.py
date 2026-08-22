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


def _api_key_de(linha: dict, default: str = "") -> str:
    """A chave de API de um provedor cadastrado, em claro — só para montar o adapter.

    Duas origens, nesta ordem (Fase 14, ADR-022):
      1. `provedor.api_key_cifrada` — o caminho novo: a chave mora no banco, cifrada em
         AES-GCM, e `db.segredo` a decifra com a chave-mestra do ambiente.
      2. `provedor.api_key_ref` — herança pré-ADR-022: o NOME de uma variável de ambiente,
         com o valor no `.env`. Continua funcionando enquanto o bloco 4 da Fase 14 (seed +
         migração de conteúdo) não roda, para que a virada não seja tudo-ou-nada.

    Este é o ÚNICO ponto do código que devolve segredo em claro. Nada acima daqui — service,
    API, template — pode chamar `db.segredo.decifrar`.
    """
    blob = linha.get("api_key_cifrada")
    if blob:
        from pesquisa_precos.db import segredo as seg
        return seg.decifrar(bytes(blob), contexto=linha["provedor"])
    ref = linha.get("api_key_ref")
    if not ref:
        return default
    return os.getenv(ref, default)


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
            api_key = _api_key_de(linha)
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
        # `remoto` sobreviveu como parâmetro por compatibilidade das etapas; hoje só existe
        # o serviço de GPU (ADR-021), então ele não escolhe mais nada.
        info = InfoProvedor(nome="gpu_caseira", capacidade=capacidade,
                            base_url=cfg["gpu_base_url"], modelo=cfg[modelo_cfg])
        return ResolucaoCapacidade(info=info, api_key=cfg["gpu_api_key"], origem="env")

    if capacidade in ("pdf", "pareamento"):
        base = cfg[f"{capacidade}_base_url"]
        info = InfoProvedor(nome="remoto", capacidade=capacidade, base_url=base, modelo="")
        return ResolucaoCapacidade(info=info, api_key=cfg[f"{capacidade}_api_key"],
                                   origem="env")

    raise ValueError(f"capacidade desconhecida: {capacidade!r} "
                     f"(use chat/embed/rerank/pdf/pareamento)")


def criar_chat(cfg: dict, *, sessao: "Session | None" = None, provedor: str | None = None,
               forte: bool = False, curador_kwargs: dict | None = None):
    from pesquisa_precos.providers.adaptadores import ChatAdapter

    r = resolver_capacidade("chat", cfg, sessao=sessao, provedor=provedor, forte=forte)
    return ChatAdapter(r.info, api_key=r.api_key, curador_kwargs=curador_kwargs)


def _exigir_servico(r, capacidade: str, variavel: str):
    """Capacidade sem `base_url` é erro de configuração, não motivo para rodar aqui.

    Desde a ADR-021 não existe caminho em processo: embedding, rerank, OCR, parse de PDF e
    BM25 rodam nos serviços de `pncp-servicos-locais`. Cair silenciosamente num modo local
    faria o servidor que orquestra tentar carregar torch/PyMuPDF — exatamente o trabalho que
    ele não deve fazer — e só descobriríamos pelo `MemoryError` ou pela conta.
    """
    if not r.info.base_url:
        raise SystemExit(
            f"Capacidade `{capacidade}` sem endereço de serviço. Defina {variavel} no .env "
            f"(ou configure o provedor pela tela /provedores) apontando para o serviço "
            f"correspondente do repositório `pncp-servicos-locais`.")
    return r


def criar_embed(cfg: dict, *, sessao: "Session | None" = None, remoto: bool | None = None,
                cache_path: str | None = None):
    from pesquisa_precos.providers.adaptadores import EmbedGpuCaseiraAdapter

    r = _exigir_servico(resolver_capacidade("embed", cfg, sessao=sessao, remoto=remoto),
                        "embed", "GPU_BASE_URL")
    return EmbedGpuCaseiraAdapter(r.info, api_key=r.api_key, cache_path=cache_path)


def criar_rerank(cfg: dict, *, sessao: "Session | None" = None, remoto: bool | None = None,
                 batch: int | None = None):
    from pesquisa_precos.providers.adaptadores import RerankGpuCaseiraAdapter

    r = _exigir_servico(resolver_capacidade("rerank", cfg, sessao=sessao, remoto=remoto),
                        "rerank", "GPU_BASE_URL")
    if batch:
        r.info = InfoProvedor(**{**r.info.__dict__, "batch_size": batch})
    return RerankGpuCaseiraAdapter(r.info, api_key=r.api_key)


def criar_pdf(cfg: dict, *, sessao: "Session | None" = None):
    """Capacidade `pdf` (ADR-019/ADR-021). Este processo baixa os arquivos; o serviço faz o
    parse, a rasterização e o OCR."""
    from pesquisa_precos.providers.adaptadores import PdfRemotoAdapter

    r = _exigir_servico(resolver_capacidade("pdf", cfg, sessao=sessao), "pdf", "PDF_BASE_URL")
    return PdfRemotoAdapter(r.info, api_key=r.api_key)


def criar_pareamento(cfg: dict, *, sessao: "Session | None" = None):
    """Capacidade `pareamento` (ADR-019/ADR-021) — BM25 + embedding + corte, no serviço."""
    from pesquisa_precos.providers.adaptadores import PareamentoRemotoAdapter

    r = _exigir_servico(resolver_capacidade("pareamento", cfg, sessao=sessao),
                        "pareamento", "PAREAMENTO_BASE_URL")
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

