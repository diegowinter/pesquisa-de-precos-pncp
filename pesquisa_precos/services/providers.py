"""
Camada de serviço do CRUD de provedores (Fase 14, ADR-022 — bloco 2).

A Fase 7 criou `provider`/`provider_capability` e a resolução por capacidade, mas nenhuma rota
escrevia nessas tabelas: só dava para popular por SQL na mão, e por isso a configuração real da
aplicação continuou num `.env` editado a dedo. Este módulo é o que faz a promessa da ADR-014
("model, provedor, URL da GPU é config, não código") chegar ao operador.

Regra que atravessa o arquivo inteiro: **a chave de API entra, nunca sai.** `gravar_api_key`
cifra e grava; nada aqui devolve a chave em claro — quem precisa dela é `providers.resolver`,
para montar o adapter, e ele a lê do repo direto. A tela recebe `has_api_key`/`api_key_last4`,
que não reconstroem nada.
"""

from __future__ import annotations

from typing import Any

from pesquisa_precos.db import secret as seg
from pesquisa_precos.db import session as db
from pesquisa_precos.db.repos import execution as repo

CAPACIDADES = ("chat", "embed", "rerank", "extract", "matching")


class ProvedorInexistente(RuntimeError):
    """`name` não está cadastrado — 404 na API/web."""


class InvalidProvider(ValueError):
    """Formulário incompleto ou incoerente (nome/base_url vazios, capacidade desconhecida)."""


class FallbackProibido(ValueError):
    """Tentativa de apontar fallback em `embed` (ADR-006 §2). A trava existe em três camadas
    — aqui, no repo e no resolver — porque trocar de provedor de embedding no meio mistura
    espaços vetoriais sem levantar exceção nenhuma: é o tipo de bug que só aparece meses
    depois, como resultado ruim."""


def _validar(name: str, base_url: str, capabilities: list[str]) -> None:
    if not (name or "").strip():
        raise InvalidProvider("informe o name do provider")
    if not (base_url or "").strip():
        raise InvalidProvider(
            "informe a base_url. Desde a ADR-021 não existe caminho em processo: um provider "
            "sem endereço não é 'roda aqui', é configuração incompleta.")
    if not capabilities:
        raise InvalidProvider("selecione ao menos uma capability")
    desconhecidas = [c for c in capabilities if c not in CAPACIDADES]
    if desconhecidas:
        raise InvalidProvider(f"capability desconhecida: {', '.join(desconhecidas)}")


def listar() -> list[dict[str, Any]]:
    """Providers cadastrados + as capacidades que cada um atende. Sem chave em claro e sem
    sondagem ao vivo (para o probe, ver `saude_provedores` em `services.execution`)."""
    with db.session() as sessao:
        return repo.listar_provedores(sessao)


def obter(name: str) -> dict[str, Any] | None:
    for p in listar():
        if p["name"] == name:
            return p
    return None


def salvar(name: str, capabilities: list[str], base_url: str, *,
           default_model: str | None = None, batch_size: int | None = None,
           rpm_limit: int | None = None, cost_in_per_mtok: float | None = None,
           cost_out_per_mtok: float | None = None,
           cost_usd_per_call: float | None = None, active: bool = True,
           api_key: str | None = None) -> None:
    """Cria ou atualiza um provedor. `api_key` vazio/None **não apaga** a chave existente — o
    campo do formulário vem sempre em branco (nunca se preenche com o valor atual, que a tela
    não conhece), então tratar branco como "apagar" destruiria a chave a cada edição de
    `base_url`. Para remover de propósito existe `limpar_api_key`."""
    name = name.strip()
    base_url = base_url.strip()
    _validar(name, base_url, capabilities)
    if api_key:
        # Falha ANTES do INSERT se a chave-mestra não estiver no ambiente: gravar o provedor e
        # perder a chave em silêncio seria o pior dos dois mundos.
        seg.key_id_atual()
    with db.session() as sessao:
        repo.upsert_provedor(
            sessao, name, capabilities, base_url, default_model=default_model,
            batch_size=batch_size, rpm_limit=rpm_limit,
            cost_in_per_mtok=cost_in_per_mtok, cost_out_per_mtok=cost_out_per_mtok,
            cost_usd_per_call=cost_usd_per_call, active=active)
        if api_key:
            repo.gravar_api_key(sessao, name, api_key)


def gravar_api_key(name: str, api_key: str) -> None:
    if not (api_key or "").strip():
        raise InvalidProvider("key vazia — para remover a key use 'limpar'")
    if obter(name) is None:
        raise ProvedorInexistente(f"provider {name!r} não existe")
    with db.session() as sessao:
        repo.gravar_api_key(sessao, name, api_key.strip())


def limpar_api_key(name: str) -> None:
    if obter(name) is None:
        raise ProvedorInexistente(f"provider {name!r} não existe")
    with db.session() as sessao:
        repo.limpar_api_key(sessao, name)


def definir_ativo(name: str, active: bool) -> None:
    p = obter(name)
    if p is None:
        raise ProvedorInexistente(f"provider {name!r} não existe")
    salvar(name, list(p["capabilities"]), p["base_url"],
           default_model=p.get("default_model"), batch_size=p.get("batch_size"),
           rpm_limit=p.get("rpm_limit"), cost_in_per_mtok=p.get("cost_in_per_mtok"),
           cost_out_per_mtok=p.get("cost_out_per_mtok"),
           cost_usd_per_call=p.get("cost_usd_per_call"), active=active)


def apontar(capability: str, provider: str, model: str | None = None,
            fallback: str | None = None) -> None:
    """Quem atende cada capability. É esta linha que o `resolver` lê — cadastrar um provedor
    sem apontá-lo não muda nada no comportamento das etapas."""
    if capability not in CAPACIDADES:
        raise InvalidProvider(f"capability desconhecida: {capability!r}")
    if capability == "embed" and fallback:
        raise FallbackProibido(
            "fallback é proibido na capability 'embed' (ADR-006): trocar de provider no meio "
            "mistura espaços vetoriais. Falhar e parar a step é o comportamento correto.")
    if obter(provider) is None:
        raise ProvedorInexistente(f"provider {provider!r} não existe")
    with db.session() as sessao:
        repo.apontar_capacidade(sessao, capability, provider, model or None, fallback or None)


def testar(name: str) -> dict[str, Any]:
    """Sondagem HTTP leve contra a `base_url` do provedor — o botão "testar agora" da tela.
    Não gasta e não chama o modelo: só prova que o endereço responde (um 401 conta como
    saudável; credencial errada a etapa acusa na hora, com mensagem clara)."""
    from pesquisa_precos.providers import health

    p = obter(name)
    if p is None:
        raise ProvedorInexistente(f"provider {name!r} não existe")
    capabilities = list(p["capabilities"])
    # Só `matching` é serviço do companion e responde `/health`; o resto fala o
    # `/models` da convenção OpenAI-compatible, `extract` inclusive (ADR-023).
    sondar = (health.sondar_health
              if "matching" in capabilities else health.sondar_url)
    resultado = sondar(p["base_url"])
    with db.session() as sessao:
        repo.atualizar_status_provedor(sessao, name, resultado["healthy"],
                                       resultado["latency_ms"], resultado["message"])
    return {"provider": name, **resultado}


def diagnostico_chave_mestra() -> dict[str, Any]:
    """Para a tela dizer, em vez de explodir, que `APP_SECRET_KEY` não está no ambiente — sem
    ela não é possível gravar nem ler chave de provedor (ADR-022)."""
    if not seg.configurada():
        return {"configurada": False, "key_id": None, "variavel": seg.VAR_CHAVE}
    return {"configurada": True, "key_id": seg.key_id_atual(), "variavel": seg.VAR_CHAVE}


def keys_a_recifrar() -> list[str]:
    """Providers cujo `api_key_key_id` não é o da chave-mestra atual — o que falta re-cifrar
    depois de uma rotação de `APP_SECRET_KEY`."""
    if not seg.configurada():
        return []
    atual = seg.key_id_atual()
    return [p["name"] for p in listar()
            if p.get("has_api_key") and p.get("api_key_key_id") not in (atual, None)]


def recifrar_tudo() -> dict[str, Any]:
    """Re-cifra com a chave-mestra atual toda linha que ainda está numa anterior. Requer
    `APP_SECRET_KEY_ANTIGA` no ambiente durante a janela.

    Devolve `{"recifradas": n, "falharam": [nomes]}`. Uma linha que não decifra **não aborta as
    outras**: ela pode ter sido cifrada por uma chave-mestra que já não existe (duas rotações
    sem re-cifrar no meio, ou um restore de dump antigo), e nesse caso a chave dela está
    perdida de qualquer forma — deixar isso bloquear a rotação das demais transformaria um
    problema de uma linha num problema de todas. Quem falha aparece na tela para ser
    recadastrado, que é a única saída real.
    """
    from sqlalchemy import text

    recifradas, falharam = 0, []
    with db.session() as sessao:
        linhas = sessao.execute(
            text("SELECT name, api_key_encrypted FROM provider "
                 "WHERE api_key_encrypted IS NOT NULL")).mappings().all()
        for linha in linhas:
            blob = bytes(linha["api_key_encrypted"])
            if seg.key_id_do_blob(blob) == seg.key_id_atual():
                continue
            try:
                novo_blob = seg.recifrar(blob, context=linha["name"])
            except seg.SegredoInvalido:
                falharam.append(linha["name"])
                continue
            sessao.execute(
                text("UPDATE provider SET api_key_encrypted = :b, api_key_key_id = :k, "
                     "  updated_at = now() WHERE name = :n"),
                {"n": linha["name"], "b": novo_blob, "k": seg.key_id_atual()})
            recifradas += 1
    return {"recifradas": recifradas, "falharam": falharam}
