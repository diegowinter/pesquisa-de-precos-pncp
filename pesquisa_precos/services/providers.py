"""
Camada de serviço do CRUD de provedores (Fase 14, ADR-022 — bloco 2).

A Fase 7 criou `provedor`/`capacidade_provedor` e a resolução por capacidade, mas nenhuma rota
escrevia nessas tabelas: só dava para popular por SQL na mão, e por isso a configuração real da
aplicação continuou num `.env` editado a dedo. Este módulo é o que faz a promessa da ADR-014
("modelo, provedor, URL da GPU é config, não código") chegar ao operador.

Regra que atravessa o arquivo inteiro: **a chave de API entra, nunca sai.** `gravar_api_key`
cifra e grava; nada aqui devolve a chave em claro — quem precisa dela é `providers.resolver`,
para montar o adapter, e ele a lê do repo direto. A tela recebe `tem_api_key`/`api_key_last4`,
que não reconstroem nada.
"""

from __future__ import annotations

from typing import Any

from pesquisa_precos.db import secret as seg
from pesquisa_precos.db import session as db
from pesquisa_precos.db.repos import execution as repo

CAPACIDADES = ("chat", "embed", "rerank", "pdf", "pareamento")


class ProvedorInexistente(RuntimeError):
    """`nome` não está cadastrado — 404 na API/web."""


class ProvedorInvalido(ValueError):
    """Formulário incompleto ou incoerente (nome/base_url vazios, capacidade desconhecida)."""


class FallbackProibido(ValueError):
    """Tentativa de apontar fallback em `embed` (ADR-006 §2). A trava existe em três camadas
    — aqui, no repo e no resolver — porque trocar de provedor de embedding no meio mistura
    espaços vetoriais sem levantar exceção nenhuma: é o tipo de bug que só aparece meses
    depois, como resultado ruim."""


def _validar(nome: str, base_url: str, capacidades: list[str]) -> None:
    if not (nome or "").strip():
        raise ProvedorInvalido("informe o nome do provedor")
    if not (base_url or "").strip():
        raise ProvedorInvalido(
            "informe a base_url. Desde a ADR-021 não existe caminho em processo: um provedor "
            "sem endereço não é 'roda aqui', é configuração incompleta.")
    if not capacidades:
        raise ProvedorInvalido("selecione ao menos uma capacidade")
    desconhecidas = [c for c in capacidades if c not in CAPACIDADES]
    if desconhecidas:
        raise ProvedorInvalido(f"capacidade desconhecida: {', '.join(desconhecidas)}")


def listar() -> list[dict[str, Any]]:
    """Provedores cadastrados + as capacidades que cada um atende. Sem chave em claro e sem
    sondagem ao vivo (para o probe, ver `saude_provedores` em `services.execution`)."""
    with db.session() as sessao:
        return repo.listar_provedores(sessao)


def obter(nome: str) -> dict[str, Any] | None:
    for p in listar():
        if p["nome"] == nome:
            return p
    return None


def salvar(nome: str, capacidades: list[str], base_url: str, *,
           modelo_padrao: str | None = None, batch_size: int | None = None,
           rpm_limite: int | None = None, custo_in_por_mtok: float | None = None,
           custo_out_por_mtok: float | None = None,
           custo_usd_chamada: float | None = None, ativo: bool = True,
           api_key: str | None = None) -> None:
    """Cria ou atualiza um provedor. `api_key` vazio/None **não apaga** a chave existente — o
    campo do formulário vem sempre em branco (nunca se preenche com o valor atual, que a tela
    não conhece), então tratar branco como "apagar" destruiria a chave a cada edição de
    `base_url`. Para remover de propósito existe `limpar_api_key`."""
    nome = nome.strip()
    base_url = base_url.strip()
    _validar(nome, base_url, capacidades)
    if api_key:
        # Falha ANTES do INSERT se a chave-mestra não estiver no ambiente: gravar o provedor e
        # perder a chave em silêncio seria o pior dos dois mundos.
        seg.key_id_atual()
    with db.session() as sessao:
        repo.upsert_provedor(
            sessao, nome, capacidades, base_url, modelo_padrao=modelo_padrao,
            batch_size=batch_size, rpm_limite=rpm_limite,
            custo_in_por_mtok=custo_in_por_mtok, custo_out_por_mtok=custo_out_por_mtok,
            custo_usd_chamada=custo_usd_chamada, ativo=ativo)
        if api_key:
            repo.gravar_api_key(sessao, nome, api_key)


def gravar_api_key(nome: str, api_key: str) -> None:
    if not (api_key or "").strip():
        raise ProvedorInvalido("chave vazia — para remover a chave use 'limpar'")
    if obter(nome) is None:
        raise ProvedorInexistente(f"provedor {nome!r} não existe")
    with db.session() as sessao:
        repo.gravar_api_key(sessao, nome, api_key.strip())


def limpar_api_key(nome: str) -> None:
    if obter(nome) is None:
        raise ProvedorInexistente(f"provedor {nome!r} não existe")
    with db.session() as sessao:
        repo.limpar_api_key(sessao, nome)


def definir_ativo(nome: str, ativo: bool) -> None:
    p = obter(nome)
    if p is None:
        raise ProvedorInexistente(f"provedor {nome!r} não existe")
    salvar(nome, list(p["capacidades"]), p["base_url"],
           modelo_padrao=p.get("modelo_padrao"), batch_size=p.get("batch_size"),
           rpm_limite=p.get("rpm_limite"), custo_in_por_mtok=p.get("custo_in_por_mtok"),
           custo_out_por_mtok=p.get("custo_out_por_mtok"),
           custo_usd_chamada=p.get("custo_usd_chamada"), ativo=ativo)


def apontar(capacidade: str, provedor: str, modelo: str | None = None,
            fallback: str | None = None) -> None:
    """Quem atende cada capacidade. É esta linha que o `resolver` lê — cadastrar um provedor
    sem apontá-lo não muda nada no comportamento das etapas."""
    if capacidade not in CAPACIDADES:
        raise ProvedorInvalido(f"capacidade desconhecida: {capacidade!r}")
    if capacidade == "embed" and fallback:
        raise FallbackProibido(
            "fallback é proibido na capacidade 'embed' (ADR-006): trocar de provedor no meio "
            "mistura espaços vetoriais. Falhar e parar a etapa é o comportamento correto.")
    if obter(provedor) is None:
        raise ProvedorInexistente(f"provedor {provedor!r} não existe")
    with db.session() as sessao:
        repo.apontar_capacidade(sessao, capacidade, provedor, modelo or None, fallback or None)


def testar(nome: str) -> dict[str, Any]:
    """Sondagem HTTP leve contra a `base_url` do provedor — o botão "testar agora" da tela.
    Não gasta e não chama o modelo: só prova que o endereço responde (um 401 conta como
    saudável; credencial errada a etapa acusa na hora, com mensagem clara)."""
    from pesquisa_precos.providers import health

    p = obter(nome)
    if p is None:
        raise ProvedorInexistente(f"provedor {nome!r} não existe")
    capacidades = list(p["capacidades"])
    sondar = (health.sondar_health
              if any(c in ("pdf", "pareamento") for c in capacidades) else health.sondar_url)
    resultado = sondar(p["base_url"])
    with db.session() as sessao:
        repo.atualizar_status_provedor(sessao, nome, resultado["saudavel"],
                                       resultado["latencia_ms"], resultado["mensagem"])
    return {"provedor": nome, **resultado}


def diagnostico_chave_mestra() -> dict[str, Any]:
    """Para a tela dizer, em vez de explodir, que `APP_SECRET_KEY` não está no ambiente — sem
    ela não é possível gravar nem ler chave de provedor (ADR-022)."""
    if not seg.configurada():
        return {"configurada": False, "key_id": None, "variavel": seg.VAR_CHAVE}
    return {"configurada": True, "key_id": seg.key_id_atual(), "variavel": seg.VAR_CHAVE}


def chaves_a_recifrar() -> list[str]:
    """Provedores cujo `api_key_key_id` não é o da chave-mestra atual — o que falta re-cifrar
    depois de uma rotação de `APP_SECRET_KEY`."""
    if not seg.configurada():
        return []
    atual = seg.key_id_atual()
    return [p["nome"] for p in listar()
            if p.get("tem_api_key") and p.get("api_key_key_id") not in (atual, None)]


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
            text("SELECT nome, api_key_cifrada FROM provedor "
                 "WHERE api_key_cifrada IS NOT NULL")).mappings().all()
        for linha in linhas:
            blob = bytes(linha["api_key_cifrada"])
            if seg.key_id_do_blob(blob) == seg.key_id_atual():
                continue
            try:
                novo_blob = seg.recifrar(blob, contexto=linha["nome"])
            except seg.SegredoInvalido:
                falharam.append(linha["nome"])
                continue
            sessao.execute(
                text("UPDATE provedor SET api_key_cifrada = :b, api_key_key_id = :k, "
                     "  atualizado_em = now() WHERE nome = :n"),
                {"n": linha["nome"], "b": novo_blob, "k": seg.key_id_atual()})
            recifradas += 1
    return {"recifradas": recifradas, "falharam": falharam}
