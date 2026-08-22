"""
Etapa 6a — Geração de pares (catálogo × item, mesma categoria) + rejeitor híbrido BM25+embedding.

Produto (codigo_catalogo × item_pncp) RESTRITO à mesma categoria; item multi-label pareia em
todas as suas categorias; SEM dedup de pares (regra de negócio). Para cada par mede-se um
score léxico (BM25 normalizado por categoria) e um score semântico (cosseno bge-m3). O par
sobrevive se max(bm25_norm, cosseno) >= piso — basta um sinal dizer "pode ser".

Entradas: data/4_itens_sobreviventes.csv, data/5_itens_enriquecidos.csv (opcional),
          data/0a_catalogo_filtrado.csv (+ categoria do código via etapa 1).
Saída: data/6a_pares_candidatos.csv (par_key, codigo, item_key, categoria, score_bm25,
       score_cosseno, sobreviveu). Chave de resumo: nenhuma — recomputa o corpus inteiro.
GPU: o embedder roda sozinho. Embeddings são cacheados por hash de texto em
data/checkpoints/6a_emb_cache.parquet — numa atualização só os textos NOVOS (itens/códigos
novos) vão à GPU; o BM25 e o corte são recomputados frescos (baratos, na CPU).

⚠ NÃO fazer: aplicar o corte top-K + piso DEPOIS de materializar o produto cartesiano num
DataFrame. Um `aplicar_corte` pós-hoc com groupby().rank() já causou MemoryError real com
~33M linhas; o corte é feito em streaming, por código, direto nas matrizes numpy. O arquivo
data/6a_pares_candidatos_PRECORTE.csv (3,7 GB) é o fóssil desse bug.

"""

import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

from sqlalchemy import text as sa_text
from pydantic import BaseModel, Field

from pesquisa_precos.etapas.base import ContextoExecucao, Estimativa, ResultadoEtapa

CHAVE = "6a"
VERSAO_CODIGO = "2.0.0"


class Params(BaseModel):
    sem_embedding: bool = Field(False, description="Só BM25 (pula o embedder/GPU)")
    remoto: bool = Field(
        False, description="Usa o servidor de GPU (GPU_BASE_URL) para o embedder")
    top_k: int = Field(
        100, ge=1, description="Por código, mantém só os K itens PNCP mais similares")
    piso: float = Field(
        0.40, ge=0.0, le=1.0,
        description="Score efetivo mínimo (max bm25/cosseno) p/ o par sobreviver")


# ── Pareamento no banco + capacidade `pareamento` (Fases 10 e 11) ───────────────────
#
# Duas mudanças na mesma passada, porque tocam o mesmo corpo:
#   Fase 10 — catálogo e itens vêm do banco; os pares vão para a tabela `par`;
#   Fase 11 — BM25 + cosseno + corte saem do processo e viram a capacidade `pareamento`.
#
# O corte em streaming NÃO ficou para trás na mudança: ele agora vive em
# `core/pareamento/motor.py`, que roda tanto no serviço quanto em processo. O aviso do
# `MemoryError` de ~33M linhas vale igual dos dois lados.

def _exigir_banco():
    from pesquisa_precos.db import sessao as db

    ok, detalhe = db.esta_disponivel()
    if not ok:
        raise SystemExit(f"Banco indisponível ({detalhe}). Confira DATABASE_URL no .env.")
    return db


def carregar_do_banco() -> tuple[list[dict], list[dict]]:
    """(catálogo, itens) no formato que `motor.parear` consome.

    O item entra com a `descricao_final` da etapa 5 quando existe, e com a descrição da API
    quando não — mesma precedência do caminho CSV. Só entram itens `manter`: item marcado
    `descartar` pela extração não deve ocupar espaço no produto cartesiano.
    """
    db = _exigir_banco()
    with db.sessao() as s:
        cat = [{"codigo": c, "texto": f"{n or ''} {d or ''}".strip(), "categoria": cat_}
               for c, n, d, cat_ in s.execute(sa_text(
                   "SELECT codigo, nome_pdm, descricao, categoria FROM catalogo_item "
                   " WHERE ativo AND coalesce(categoria, '') <> ''")).all()]
        itens = [{"item_key": k, "descricao_final": d, "categoria": cat_}
                 for k, d, cat_ in s.execute(sa_text("""
                     SELECT i.item_key,
                            coalesce(NULLIF(e.descricao_final, ''), i.descricao_api),
                            ic.categoria
                       FROM item i
                       JOIN item_categoria ic USING (item_key)
                       LEFT JOIN item_enriquecido e USING (item_key)
                      WHERE i.sobrevivente
                        AND coalesce(e.destino::text, 'manter') <> 'descartar'
                 """)).all()]
    if not cat:
        raise SystemExit("catalogo_item sem categoria — rode a etapa 1 antes.")
    if not itens:
        raise SystemExit("Nenhum item sobrevivente com categoria — rode as etapas 3 e 4 antes.")
    return cat, itens


def executar(params: Params, ctx: ContextoExecucao) -> ResultadoEtapa:
    db = _exigir_banco()
    from pesquisa_precos.db.repos import catalogo as repo_cat
    from pesquisa_precos.db.repos import par as repo_par

    catalogo, itens = carregar_do_banco()
    ctx.log("info", f"[6a] {len(catalogo)} códigos × {len(itens)} itens-categoria "
                    f"(produto restrito à mesma categoria)")

    provedor = ctx.provedores.pareamento
    onde = "serviço externo" if getattr(provedor.info, "base_url", "") else "em processo"
    ctx.log("info", f"[6a] pareamento: {onde}")

    pares = provedor.parear(catalogo, itens, piso=params.piso, top_k=params.top_k)
    ctx.log("info", f"[6a] {len(pares)} pares sobreviventes (piso={params.piso}, "
                    f"top_k={params.top_k})")

    # `par.tipo` é parte da PK do catálogo (o código só é único DENTRO do tipo); o motor não
    # conhece essa distinção, então o tipo é resolvido aqui, uma vez, para todos os pares.
    with db.sessao() as s:
        tipo_por_codigo, ambiguos = repo_cat.tipo_do_codigo(s)
    if ambiguos:
        ctx.log("aviso", f"[yellow][6a] {len(ambiguos)} códigos existem nos DOIS tipos — "
                         f"o par vai para o primeiro encontrado.[/]")

    lote = [(p["par_key"], tipo_por_codigo.get(p["codigo"], "material"), p["codigo"],
             p["item_key"], p["categoria"], p["score_bm25"], p["score_cosseno"], True, None)
            for p in pares]
    with db.conexao_bruta() as conn:
        gravados = repo_par.gravar_candidatos(conn, lote)
        conn.commit()

    with db.sessao() as s:
        contagens = repo_par.contar(s)
    ctx.log("info", f"[bold green][6a] Gravados {gravados} pares[/] → tabela `par` "
                    f"({contagens})")

    return ResultadoEtapa(
        processados=len(pares), erros=0,
        metricas={"sobreviventes": len(pares), "codigos": len(catalogo),
                  "itens_categoria": len(itens), **contagens},
        preview=pares[:50],
    )


def estimar(params: Params, ctx: ContextoExecucao) -> Estimativa:
    """Custo é GPU/CPU, não token. A unidade útil é o par candidato (produto por categoria)."""
    from collections import Counter

    from pesquisa_precos.db import sessao as db

    ok, detalhe = db.esta_disponivel()
    if not ok:
        return Estimativa(detalhes={"aviso": f"banco indisponível: {detalhe}"})
    try:
        catalogo, itens = carregar_do_banco()
    except SystemExit as e:
        return Estimativa(detalhes={"aviso": str(e)})
    # O produto é POR CATEGORIA: somar `len(cat) * len(itens)` daria um número
    # astronomicamente maior que o real e assustaria à toa.
    n_cod = Counter(c["categoria"] for c in catalogo)
    n_itn = Counter(i["categoria"] for i in itens)
    comuns = set(n_cod) & set(n_itn)
    pares = sum(n_cod[c] * n_itn[c] for c in comuns)
    return Estimativa(
        unidades=pares, chamadas_llm=0, custo_usd=0.0,
        detalhes={"categorias": len(comuns),
                  "codigos": sum(n_cod[c] for c in comuns),
                  "itens_explodidos": sum(n_itn[c] for c in comuns),
                  "teto_de_sobreviventes (top-K)": sum(n_cod[c] for c in comuns) * params.top_k},
    )

