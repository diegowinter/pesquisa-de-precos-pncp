"""
Semeia `provider`/`provider_capability` a partir do `.env`.

Roda uma vez, na virada da ADR-022. Depois dela, quem configura provedor é a tela
`/provedores`, e o `.env` fica só com `DATABASE_URL` e `APP_SECRET_KEY`.

Ficou aqui e não numa migração Alembic porque semear depende do `.env` de origem e da
chave-mestra no ambiente: uma migração que exige segredo para rodar quebra em qualquer máquina
que não seja a do operador, e o `downgrade` dela não desfaria a cifra.

    uv run python -m tools.seed_providers --conferir   # não escreve nada
    uv run python -m tools.seed_providers              # grava

Reexecutar é seguro: o upsert é por nome, então as mesmas linhas são atualizadas e nada
duplica. Provedor que já existe na tela e não aparece no `.env` fica intacto.
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


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or "").strip()


def plano() -> tuple[list[dict], list[dict], list[str]]:
    """(provedores a gravar, apontamentos a fazer, avisos). Puro: não escreve nada."""
    provedores: list[dict] = []
    apontamentos: list[dict] = []
    avisos: list[str] = []

    # ── chat: OpenRouter ────────────────────────────────────────────────────────────
    # Só PASS1 vira provider. PASS2 é o modelo caro, e a ADR-004 já o tirou do caminho
    # padrão: semeá-lo criaria um provedor pronto para ser apontado por engano, contra a
    # restrição de custo registrada no CLAUDE.md. Se um dia houver orçamento, cadastra-se
    # pela tela, conscientemente.
    if _env("OPENAI_BASE_URL") and _env("OPENAI_MODEL_PASS1"):
        provedores.append({
            "name": "openrouter", "capabilities": ["chat"],
            "base_url": _env("OPENAI_BASE_URL"),
            "default_model": _env("OPENAI_MODEL_PASS1"),
            # `or None`: no `.env` o default de CUSTO_USD_CHAMADA_* era `0.0`, e ali significava
            # "não informado". No banco `0.0` significa GRÁTIS — semeá-lo assim faria a
            # estimativa jurar que uma etapa paga custa zero. Provedor pago sem preço informado
            # tem de virar NULL, para o `estimar()` responder "não estimado".
            "cost_usd_per_call": _float("CUSTO_USD_CHAMADA_PASS1") or None,
            "api_key": _env("OPENAI_API_KEY") or None})
        apontamentos.append({"capability": "chat", "provider": "openrouter"})
        if _env("OPENAI_MODEL_PASS2"):
            avisos.append(
                f"OPENAI_MODEL_PASS2 ({_env('OPENAI_MODEL_PASS2')}) NÃO foi semeado: é o model "
                f"caro (ADR-004), e não há orçamento para ele neste projeto. Cadastre-o pela "
                f"tela se um dia houver.")

    # ── chat: LM Studio local ───────────────────────────────────────────────────────
    if _env("LOCAL_BASE_URL") and _env("LOCAL_MODEL"):
        provedores.append({
            "name": "lm_studio", "capabilities": ["chat"],
            "base_url": _env("LOCAL_BASE_URL"), "default_model": _env("LOCAL_MODEL"),
            # A GPU caseira não custa dinheiro. `0.0` é diferente de `None`: significa
            # "grátis, e eu sei disso", não "não informado".
            "cost_usd_per_call": 0.0,
            "api_key": _env("LOCAL_API_KEY") or None})
        # NÃO aponta `chat` para cá: quem atende `chat` é decisão do operador, e apontar dois
        # provedores para a mesma capacidade em sequência faria o último vencer em silêncio.

    # ── embed + rerank: serviço de GPU ──────────────────────────────────────────────
    if _env("GPU_BASE_URL"):
        provedores.append({
            "name": "gpu_caseira", "capabilities": ["embed", "rerank"],
            "base_url": _env("GPU_BASE_URL"),
            "default_model": _env("EMBEDDER_MODEL"),
            "api_key": _env("GPU_API_KEY") or None})
        # `model` por capacidade: embed e rerank são modelos diferentes no mesmo serviço.
        apontamentos.append({"capability": "embed", "provider": "gpu_caseira",
                             "model": _env("EMBEDDER_MODEL")})
        apontamentos.append({"capability": "rerank", "provider": "gpu_caseira",
                             "model": _env("RERANKER_MODEL")})

    # ── pareamento: o único serviço do companion que ainda tem cliente (ADR-023) ────
    for capability, prefixo in (("matching", "PAREAMENTO"),):
        base = _env(f"{prefixo}_BASE_URL")
        if not base:
            avisos.append(
                f"{prefixo}_BASE_URL está vazia — a capability `{capability}` vai ficar sem "
                f"provider, e a etapa que a declara não roda. Suba o serviço de "
                f"`pncp-servicos-locais` e cadastre-o em /provedores.")
            continue
        provedores.append({
            "name": f"service_{capability}", "capabilities": [capability], "base_url": base,
            "api_key": _env(f"{prefixo}_API_KEY") or None})
        apontamentos.append({"capability": capability, "provider": f"service_{capability}"})

    return provedores, apontamentos, avisos


def _float(name: str) -> float | None:
    valor = _env(name)
    try:
        return float(valor) if valor else None
    except ValueError:
        return None


def run(conferir: bool = False) -> int:
    provedores, apontamentos, avisos = plano()

    print("Providers a cadastrar a partir do .env:\n")
    for p in provedores:
        key = "com key" if p.get("api_key") else "SEM key"
        print(f"  {p['name']:<18} {'/'.join(p['capabilities']):<16} {p['base_url']}")
        print(f"  {'':18} model={p.get('default_model') or '—'}  ({key})")
    print("\nApontamentos (capability → provider):\n")
    for a in apontamentos:
        model = f"  model={a['model']}" if a.get("model") else ""
        print(f"  {a['capability']:<12} → {a['provider']}{model}")
    if avisos:
        print("\nAvisos:\n")
        for aviso in avisos:
            print(f"  AVISO: {aviso}")

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
        servico.salvar(p["name"], p["capabilities"], p["base_url"],
                       default_model=p.get("default_model"),
                       cost_usd_per_call=p.get("cost_usd_per_call"),
                       api_key=p.get("api_key"))
    for a in apontamentos:
        servico.apontar(a["capability"], a["provider"], a.get("model"))

    print(f"\n✔ {len(provedores)} provedores e {len(apontamentos)} apontamentos gravados.")
    print("  Confira em /provedores e apague do .env o que já migrou.")
    return 0


if __name__ == "__main__":
    sys.exit(run(conferir="--conferir" in sys.argv))
