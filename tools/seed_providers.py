"""
Semeia `provedor`/`capacidade_provedor` a partir do `.env` — a ponte de mão única da Fase 14
(ADR-022, bloco 4).

Roda UMA vez, na virada. Depois dela, quem configura provedor é a tela `/provedores`, e o
`.env` fica só com `DATABASE_URL` e `APP_SECRET_KEY`.

Por que aqui e não numa migração Alembic (como a ADR-022 previa): semear depende de duas coisas
que uma migração não deveria exigir — o `.env` de origem e a chave-mestra no ambiente. Migração
que precisa de segredo para rodar quebra em qualquer máquina que não seja a do operador, e o
`downgrade` dela não teria como desfazer a cifra. `tools/` é exatamente o lugar de script
de apoio pontual (ver CLAUDE.md), e este é pontual por definição.

    uv run python -m tools.seed_providers --conferir   # não escreve nada
    uv run python -m tools.seed_providers              # grava

É IDEMPOTENTE: reexecutar atualiza as mesmas linhas (o `upsert` é por nome) e não duplica nada.
Só toca no que o `.env` descreve; provedor que você já cadastrou pela tela e não existe no
`.env` fica intacto.
"""

from __future__ import annotations

import os
import sys

# Mesma proteção das etapas: o console do Windows abre em cp1252 e as setas/acentos deste
# script explodem com UnicodeEncodeError antes de imprimir qualquer coisa útil.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

from pesquisa_precos.db import secret as seg
from pesquisa_precos.db import session as db
from pesquisa_precos.services import providers as servico


def _env(nome: str, default: str = "") -> str:
    return (os.getenv(nome, default) or "").strip()


def plano() -> tuple[list[dict], list[dict], list[str]]:
    """(provedores a gravar, apontamentos a fazer, avisos). Puro: não escreve nada."""
    provedores: list[dict] = []
    apontamentos: list[dict] = []
    avisos: list[str] = []

    # ── chat: OpenRouter ────────────────────────────────────────────────────────────
    # Só PASS1 vira provedor. PASS2 é o modelo caro, e a ADR-004 já o tirou do caminho
    # padrão: semeá-lo criaria um provedor pronto para ser apontado por engano, contra a
    # restrição de custo registrada no CLAUDE.md. Se um dia houver orçamento, cadastra-se
    # pela tela, conscientemente.
    if _env("OPENAI_BASE_URL") and _env("OPENAI_MODEL_PASS1"):
        provedores.append({
            "nome": "openrouter", "capacidades": ["chat"],
            "base_url": _env("OPENAI_BASE_URL"),
            "modelo_padrao": _env("OPENAI_MODEL_PASS1"),
            # `or None`: no `.env` o default de CUSTO_USD_CHAMADA_* era `0.0`, e ali significava
            # "não informado". No banco `0.0` significa GRÁTIS — semeá-lo assim faria a
            # estimativa jurar que uma etapa paga custa zero. Provedor pago sem preço informado
            # tem de virar NULL, para o `estimar()` responder "não estimado".
            "custo_usd_chamada": _float("CUSTO_USD_CHAMADA_PASS1") or None,
            "api_key": _env("OPENAI_API_KEY") or None})
        apontamentos.append({"capacidade": "chat", "provedor": "openrouter"})
        if _env("OPENAI_MODEL_PASS2"):
            avisos.append(
                f"OPENAI_MODEL_PASS2 ({_env('OPENAI_MODEL_PASS2')}) NÃO foi semeado: é o modelo "
                f"caro (ADR-004), e não há orçamento para ele neste projeto. Cadastre-o pela "
                f"tela se um dia houver.")

    # ── chat: LM Studio local ───────────────────────────────────────────────────────
    if _env("LOCAL_BASE_URL") and _env("LOCAL_MODEL"):
        provedores.append({
            "nome": "lm_studio", "capacidades": ["chat"],
            "base_url": _env("LOCAL_BASE_URL"), "modelo_padrao": _env("LOCAL_MODEL"),
            # A GPU caseira não custa dinheiro. `0.0` é diferente de `None`: significa
            # "grátis, e eu sei disso", não "não informado".
            "custo_usd_chamada": 0.0,
            "api_key": _env("LOCAL_API_KEY") or None})
        # NÃO aponta `chat` para cá: quem atende `chat` é decisão do operador, e apontar dois
        # provedores para a mesma capacidade em sequência faria o último vencer em silêncio.

    # ── embed + rerank: serviço de GPU ──────────────────────────────────────────────
    if _env("GPU_BASE_URL"):
        provedores.append({
            "nome": "gpu_caseira", "capacidades": ["embed", "rerank"],
            "base_url": _env("GPU_BASE_URL"),
            "modelo_padrao": _env("EMBEDDER_MODEL"),
            "api_key": _env("GPU_API_KEY") or None})
        # `modelo` por capacidade: embed e rerank são modelos diferentes no mesmo serviço.
        apontamentos.append({"capacidade": "embed", "provedor": "gpu_caseira",
                             "modelo": _env("EMBEDDER_MODEL")})
        apontamentos.append({"capacidade": "rerank", "provedor": "gpu_caseira",
                             "modelo": _env("RERANKER_MODEL")})

    # ── pdf e pareamento: serviços do companion ─────────────────────────────────────
    for capacidade, prefixo in (("pdf", "PDF"), ("pareamento", "PAREAMENTO")):
        base = _env(f"{prefixo}_BASE_URL")
        if not base:
            avisos.append(
                f"{prefixo}_BASE_URL está vazia — a capacidade `{capacidade}` vai ficar sem "
                f"provedor, e a etapa que a declara não roda. Suba o serviço de "
                f"`pncp-servicos-locais` e cadastre-o em /provedores.")
            continue
        provedores.append({
            "nome": f"service_{capacidade}", "capacidades": [capacidade], "base_url": base,
            "api_key": _env(f"{prefixo}_API_KEY") or None})
        apontamentos.append({"capacidade": capacidade, "provedor": f"service_{capacidade}"})

    return provedores, apontamentos, avisos


def _float(nome: str) -> float | None:
    valor = _env(nome)
    try:
        return float(valor) if valor else None
    except ValueError:
        return None


def run(conferir: bool = False) -> int:
    provedores, apontamentos, avisos = plano()

    print("Provedores a cadastrar a partir do .env:\n")
    for p in provedores:
        chave = "com chave" if p.get("api_key") else "SEM chave"
        print(f"  {p['nome']:<18} {'/'.join(p['capacidades']):<16} {p['base_url']}")
        print(f"  {'':18} modelo={p.get('modelo_padrao') or '—'}  ({chave})")
    print("\nApontamentos (capacidade → provedor):\n")
    for a in apontamentos:
        modelo = f"  modelo={a['modelo']}" if a.get("modelo") else ""
        print(f"  {a['capacidade']:<12} → {a['provedor']}{modelo}")
    if avisos:
        print("\nAvisos:\n")
        for aviso in avisos:
            print(f"  ⚠ {aviso}")

    if conferir:
        print("\n--conferir: nada foi gravado.")
        return 0

    if not seg.configurada():
        print(f"\n✖ {seg.VAR_CHAVE} não está no ambiente — sem ela não dá para cifrar as "
              f"chaves de API. Gere uma com:\n"
              f"    uv run python -c \"from pesquisa_precos.db import secret; "
              f"print(segredo.gerar_chave_mestra())\"")
        return 1

    ok, detalhe = db.is_available()
    if not ok:
        print(f"\n✖ Banco indisponível ({detalhe}).")
        return 1

    for p in provedores:
        servico.salvar(p["nome"], p["capacidades"], p["base_url"],
                       modelo_padrao=p.get("modelo_padrao"),
                       custo_usd_chamada=p.get("custo_usd_chamada"),
                       api_key=p.get("api_key"))
    for a in apontamentos:
        servico.apontar(a["capacidade"], a["provedor"], a.get("modelo"))

    print(f"\n✔ {len(provedores)} provedores e {len(apontamentos)} apontamentos gravados.")
    print("  Confira em /provedores e apague do .env o que já migrou.")
    return 0


if __name__ == "__main__":
    sys.exit(executar(conferir="--conferir" in sys.argv))
