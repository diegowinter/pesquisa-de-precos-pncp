"""
Fingerprint de etapa (ADR-009) — a alternativa ao grafo de invalidação.

`fingerprint = sha256(versao_codigo || params_efetivos || fingerprints_das_dependências)`.
Comparar o valor gravado da última execução concluída de uma etapa contra o recalculado agora
responde sozinho "esta etapa está desatualizada?" — sem manter à mão um grafo do tipo "refazer
a 3 invalida 4→8", que é frágil e cresce rápido.

Duas coisas importantes que este módulo NÃO faz:
  - não decide o que fazer com uma etapa desatualizada. Invalidar nunca apaga (ADR-009): quem
    lê `esta_desatualizada()` marca `⚠` na UI/CLI e deixa o usuário decidir.
  - não olha `run_id`. O fingerprint de uma dependência é o da sua última execução CONCLUÍDA em
    QUALQUER run — `atualizar` é incremental entre runs, então a execução que vale é sempre a
    mais recente da etapa, não a do run corrente (ver `execucao.ultimo_fingerprint_concluido`).
"""

import hashlib
import json

from sqlalchemy.orm import Session

from pesquisa_precos.db.repos import execucao as repo
from pesquisa_precos.etapas import registry


def calcular(versao_codigo: str, params_efetivos: dict, fingerprints_dependencias: list[str]) -> str:
    """Puro — não toca o banco. Separado de `calcular_para_etapa` para ser testável sem banco."""
    payload = {
        "versao_codigo": versao_codigo,
        "params": params_efetivos,
        # ordenado: a ORDEM das dependências no registry não deve mudar o fingerprint,
        # só o CONJUNTO de valores muda.
        "deps": sorted(fingerprints_dependencias),
    }
    bruto = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(bruto).hexdigest()


def calcular_para_etapa(sessao: Session, chave: str, params_efetivos: dict) -> str:
    """Resolve `versao_codigo` (do registry, bumpada manualmente — 08_CONVENCOES §5.6) e as
    fingerprints das dependências diretas, e calcula o fingerprint desta execução."""
    definicao = registry.obter(chave)
    deps_fp = [repo.ultimo_fingerprint_concluido(sessao, dep) or "" for dep in definicao.depende_de]
    return calcular(definicao.versao_codigo, params_efetivos, deps_fp)


def esta_desatualizada(sessao: Session, chave: str, params_efetivos: dict) -> bool:
    """`True` só quando a etapa JÁ rodou concluída antes e o fingerprint recalculado agora
    diverge do gravado — ou seja: código, params ou alguma dependência mudaram desde a última
    vez. Etapa que nunca rodou não é "desatualizada", é `nao_iniciada` (outro estado)."""
    gravado = repo.ultimo_fingerprint_concluido(sessao, chave)
    if gravado is None:
        return False
    atual = calcular_para_etapa(sessao, chave, params_efetivos)
    return gravado != atual
