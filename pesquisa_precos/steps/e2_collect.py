"""
Etapa 2 — Coleta larga no PNCP: busca por termo, filtro de homologados, explode em itens.

A lógica de busca vive em `core/collection/collect_pncp.py`; aqui fica a orquestração
resumível e o dedup de documento.

Para cada termo e cada tipo de documento (contrato, ata):
  - busca paginada no PNCP;
  - dedup por numeroControlePNCP — documento já visto não é reprocessado, só ganha o conceito
    atual em `conceitos_origem`;
  - documento novo: consulta os itens homologados e grava 1 linha por item.

A etapa não baixa PDF (ADR-011): guarda a capa (metadados + itens da API) e os identificadores
que a etapa 5 usa para listar os arquivos depois do corte da etapa 4.

Em `atualizar`, revisita todos os pares (termo, fonte) mas para de paginar ao cruzar o
watermark — a maior `data_atualizacao_pncp` já vista naquela busca. A busca do PNCP vem
ordenada por esse campo (a data de publicação não vem), então parar cedo é seguro.

Não reprocessar documento já visto: o dedup é o que segura o custo das etapas seguintes.
"""

import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

from pydantic import BaseModel, Field
from sqlalchemy import text as sa_text

from pesquisa_precos.core.collection import collect_pncp
from pesquisa_precos.core.collection.collect_pncp import FONTES
from pesquisa_precos.steps.base import (
    RunContext,
    Estimate,
    StepResult,
    subprogresso,
)

KEY = "2"
# 2.0.0 (Fase 13): sobrou só o banco. A coleta é a mesma (dedup por documento,
# parada por watermark); muda o destino — documento/item/documento_termo e os três
# checkpoints viram tabelas.
CODE_VERSION = "2.0.0"  # Fase 8/ADR-011: parou de baixar PDF (ver docstring do módulo)


class Params(BaseModel):
    conceitos: str | None = Field(None, description="Filtra conceitos (vírgula-separados)")
    ignorar_cache: bool = Field(False, description="Reprocessa termos já concluídos")
    atualizar: bool = Field(
        False, description="Rodada de atualização: revisita TODOS os termos, mas para de "
                           "paginar ao cruzar o watermark. Coleta só o novo.")
    limite_termos: int | None = Field(None, description="Máx. de termos por conceito (teste)")
    tam_pagina: int | None = Field(None, description="Tamanho da página da busca do PNCP")


# ── Coleta gravando no banco (Fase 10) ──────────────────────────────────────────────
#
# A lógica de coleta é a MESMA (mesmo `collect_pncp`, mesmo dedup por documento, mesma parada
# por watermark). O que muda é o destino de cada peça:
#
#   2_itens_coletados.csv       → documento + item        (duas tabelas, não uma linha larga)
#   2_conceitos_extra.csv       → documento_termo         (por documento, não por item)
#   checkpoints/2_progresso.csv → coleta_progresso
#   checkpoints/2_watermark.csv → collection_watermark
#   checkpoints/2_pendentes.csv → coleta_pendente
#
# O documento é gravado com TODOS os seus itens na mesma transação: um documento pela metade
# faria a etapa 4 cortar sobre um universo incompleto, e nada no schema denunciaria isso.

def _exigir_banco():
    from pesquisa_precos.db import session as db

    ok, detalhe = db.is_available()
    if not ok:
        raise SystemExit(f"Banco indisponível ({detalhe}). Confira DATABASE_URL no .env.")
    return db


def termos_do_banco(filtro: set[str] | None, limite: int | None) -> list[dict]:
    """Termos ativos + o id de cada um. Substitui a leitura de `1_conceitos_termos.csv`.

    Devolve `{'termo': str, 'termo_id': int, 'categoria': str}` — uma linha por termo, que é o
    formato que a etapa 1 já produzia ("um termo por linha"); o campo `conceito` do CSV era o
    próprio termo desde a reescrita da etapa 1.
    """
    db = _exigir_banco()
    with db.session() as s:
        linhas = s.execute(sa_text(
            "SELECT id, termo, coalesce(categoria, '') FROM termo "
            " WHERE active ORDER BY id")).all()
    termos = [{"termo_id": i, "termo": t, "categoria": c} for i, t, c in linhas]
    if filtro:
        termos = [t for t in termos if t["termo"] in filtro]
    if limite:
        termos = termos[:limite]
    if not termos:
        raise SystemExit("Nenhum termo active no banco — rode a etapa 1 antes.")
    return termos


def _linha_documento(linha: dict, data_atualizacao: str | None, n_itens: int) -> tuple:
    """Linha na ordem de `repo_documento.COLUNAS_DOC`, montada a partir de uma LINHA DE ITEM.

    Por que da linha de item e não do `identificar()` da busca: `COLUNAS_ITENS` já carrega
    todos os campos do documento (é uma tabela desnormalizada — foi assim que o CSV único
    funcionou até aqui), incluindo `url_pncp` e os dois sequenciais, que `identificar()` NÃO
    devolve (ele expõe o sequencial da ata como `_seq_ata`, key privada). Usar a linha é o
    mesmo caminho que a migração `m07` faz sobre o CSV — uma fonte só, já validada.

    `data_atualizacao_pncp` é a exceção: não está na linha de item, vem do resultado da busca.
    É o campo do watermark, então perdê-lo silenciosamente custaria uma re-varredura completa
    na próxima atualização.
    """
    return (
        linha.get("numeroControlePNCP"), linha.get("tipo_doc"),
        linha.get("orgao") or None, linha.get("orgao_cnpj") or None,
        linha.get("uf") or None, _inteiro(linha.get("ano")),
        _data(linha.get("data")), _data(linha.get("data_assinatura")),
        _data(linha.get("data_fim_vigencia")), data_atualizacao or None,
        linha.get("url_pncp") or None,
        # ADR-012: sem estes dois a etapa 5 não refaz `listar_arquivos()` e o documento só
        # pode ser rebaixado pela url pública.
        linha.get("numero_sequencial") or None, linha.get("numero_sequencial_ata") or None,
        n_itens,
    )


def _linha_item(linha: dict) -> tuple:
    """Linha na ordem de `repo_documento.COLUNAS_ITEM`, a partir de uma linha da etapa 2.

    `texto_hash` é calculado NA INGESTÃO (docs/02_SCHEMA.md §5) — nunca na hora de classificar.
    É ele que faz o dedup da etapa 3 ser uma consulta em vez de um agrupamento em memória.
    """
    from pesquisa_precos.core.text import texto_hash

    descricao = linha.get("descricao_api") or ""
    unidade = linha.get("unidade") or None
    return (
        linha.get("item_key"), linha.get("numeroControlePNCP"),
        _inteiro(linha.get("numeroItem")), descricao, unidade,
        _decimal(linha.get("quantidade")), _decimal(linha.get("preco_unitario")),
        _decimal(linha.get("preco_estimado")), linha.get("fornecedor") or None,
        _data(linha.get("data_resultado")), texto_hash(descricao, unidade),
    )


def _inteiro(v):
    try:
        return int(str(v).strip()) if str(v or "").strip() else None
    except (TypeError, ValueError):
        return None


def _decimal(v):
    from decimal import Decimal, InvalidOperation

    try:
        return Decimal(str(v).strip()) if str(v or "").strip() else None
    except (TypeError, ValueError, InvalidOperation):
        return None


def _data(v):
    """`YYYY-MM-DD` (aceita ISO com hora). Valor fora do formato vira NULL em vez de derrubar
    o lote inteiro — data ruim num documento não pode custar a coleta de outros mil."""
    from datetime import date

    s = str(v or "").strip()[:10]
    if len(s) != 10:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def gravar_documento_no_banco(db, linhas: list[dict], data_atualizacao: str | None,
                              termo_id: int | None) -> int:
    """Documento + seus itens + a ligação com o termo, numa transação só.

    Atômico de propósito: um documento gravado pela metade faria a etapa 4 cortar sobre um
    universo incompleto, e nada no schema denunciaria isso — `n_itens` bateria com o que
    entrou, não com o que existe no PNCP.
    """
    from pesquisa_precos.db.repos import documento as repo

    if not linhas:
        return 0
    nc = linhas[0].get("numeroControlePNCP")
    with db.raw_connection() as conn:
        repo.gravar_documentos(conn, [_linha_documento(linhas[0], data_atualizacao,
                                                       len(linhas))])
        repo.gravar_itens(conn, [_linha_item(x) for x in linhas])
        if termo_id is not None:
            repo.ligar_termos(conn, [(nc, termo_id)])
        conn.commit()
    return len(linhas)


def run(params: Params, ctx: RunContext) -> StepResult:
    """Etapa 2 sem tocar em disco (ADR-018). Estrutura idêntica à do caminho CSV."""
    db = _exigir_banco()
    from pesquisa_precos.db.repos import documento as repo
    from pesquisa_precos.db.repos import termo as repo_termo

    filtro = set(c.strip() for c in params.conceitos.split(",")) if params.conceitos else None
    termos = termos_do_banco(filtro, params.limite_termos)
    tarefas = [(t["termo"], fonte, t["termo_id"]) for t in termos for fonte in FONTES]

    with db.session() as s:
        # No --atualizar revisitamos TODOS os (termo, fonte): a parada por watermark é que
        # evita o retrabalho. Mesma decisão do caminho CSV.
        feitas = set() if (params.ignorar_cache or params.atualizar) \
            else repo.buscas_concluidas(s)
        if params.ignorar_cache:
            repo.limpar_progresso(s)
            s.commit()
        conhecidos = set() if params.ignorar_cache else repo.controles_conhecidos(s)
        watermark = repo_termo.watermarks(s)
        pendentes_docs = {} if params.ignorar_cache else repo.pendentes(s)

    pendentes = [t for t in tarefas if (t[2], t[1]) not in feitas]
    _modo = "[yellow]atualização (para no watermark)[/]" if params.atualizar else "full"
    ctx.log("info", f"[bold][2] Coleta no PNCP → banco ({_modo}):[/] {len(pendentes)} buscas "
                    f"(termo×fonte) a fazer (já feitas: {len(tarefas) - len(pendentes)}, "
                    f"fontes: {', '.join(FONTES)})")

    total_itens = total_docs = total_erros = resolvidos = 0
    tam_pagina_kw = {"tam_pagina": params.tam_pagina} if params.tam_pagina else {}

    # Revisita de pendentes (só no --atualizar), igual ao caminho CSV.
    if params.atualizar and pendentes_docs:
        ctx.log("info", f"[2] Revisitando [bold]{len(pendentes_docs)}[/] pendentes "
                        f"(sem_homologado)…")
        feitos_rev = 0
        ctx.progresso(0, len(pendentes_docs), descricao="pendentes · [green]0 resolvidos[/]")
        for ctrl, rec in list(pendentes_docs.items()):
            if ctx.cancelado():
                break
            try:
                linhas, status = collect_pncp.revisitar_pendente(
                    rec["base"], rec["tipo_doc"], "")
            except Exception as exc:  # noqa: BLE001
                ctx.erro_item(ctrl, exc, tipo=rec["tipo_doc"], name="revisita")
                feitos_rev += 1
                ctx.progresso(feitos_rev)
                continue
            if status == "ok":
                total_itens += gravar_documento_no_banco(
                    db, linhas, rec["base"].get("data_atualizacao_pncp"),
                    rec.get("termo_id"))
                with db.session() as s:
                    repo.remover_pendente(s, ctrl)
                    s.commit()
                conhecidos.add(ctrl)
                resolvidos += 1
                del pendentes_docs[ctrl]
            feitos_rev += 1
            ctx.progresso(feitos_rev,
                          descricao=f"pendentes · [green]{resolvidos} resolvidos[/]")
        ctx.log("info", f"[2] Pendentes resolvidos: [green]{resolvidos}[/]; "
                        f"ainda pendentes: {len(pendentes_docs)}")

    feitas_buscas = 0
    ctx.progresso(0, len(pendentes), descricao="buscas · [green]0 itens[/]")
    for termo, fonte, termo_id in pendentes:
        if ctx.cancelado():
            break
        subprogresso(ctx, processed=0, total=None,
                     descricao=f"[cyan]{termo[:24]}[/] ({fonte})")
        n_docs_busca = n_itens_busca = 0
        wm = watermark.get((termo_id, fonte))
        max_atu = wm or ""
        try:
            for r in collect_pncp.iter_resultados(
                    termo, fonte, on_total=lambda n: subprogresso(ctx, total=n),
                    **tam_pagina_kw):
                n_docs_busca += 1
                subprogresso(ctx, processed=n_docs_busca)
                atu = r.get("data_atualizacao_pncp") or ""
                if atu > max_atu:
                    max_atu = atu
                if params.atualizar and wm and atu and atu < wm:
                    break
                ctrl = r.get("numero_controle_pncp")
                if not ctrl:
                    continue
                if ctrl in conhecidos:
                    # Documento já coletado: só registra que ESTE termo também o encontrou.
                    with db.raw_connection() as conn:
                        repo.ligar_termos(conn, [(ctrl, termo_id)])
                        conn.commit()
                    continue
                linhas, status = collect_pncp.coletar_documento(r, fonte, termo)
                if status != "ok":
                    if status == "erro":
                        total_erros += 1
                        ctx.erro_item(ctrl, status, tipo=fonte, name=termo)
                    elif status == "sem_homologado":
                        base = collect_pncp.identificar(r, fonte)
                        with db.session() as s:
                            repo.gravar_pendente(s, ctrl, fonte, base, termo_id=termo_id,
                                                 data=base.get("data", ""))
                            s.commit()
                        pendentes_docs[ctrl] = {"tipo_doc": fonte, "base": base}
                    conhecidos.add(ctrl)   # marca visto (não reprocessa)
                    continue
                n = gravar_documento_no_banco(db, linhas, atu, termo_id)
                conhecidos.add(ctrl)
                total_docs += 1
                total_itens += n
                n_itens_busca += n
                ctx.progresso(feitas_buscas,
                              descricao=f"buscas · [green]{total_itens} itens[/]")
            # Progresso e watermark fecham JUNTOS, na mesma transação: marcar a busca como
            # concluída sem gravar o watermark faria a próxima atualização varrer do zero.
            with db.session() as s:
                repo.marcar_busca(s, termo_id, fonte, n_docs_busca, n_itens_busca)
                if max_atu:
                    repo_termo.gravar_watermark(s, termo_id, fonte, max_atu)
                s.commit()
            if max_atu:
                watermark[(termo_id, fonte)] = max_atu
        except Exception as exc:  # noqa: BLE001
            total_erros += 1
            ctx.log("erro", f"[red]erro[/] {termo} ({fonte}): {str(exc)[:80]}")
            ctx.erro_item(termo, exc, tipo=fonte, name=termo)
        finally:
            feitas_buscas += 1
            ctx.progresso(feitas_buscas)

    with db.session() as s:
        contagens = repo.contar(s)
    cor = "yellow" if total_erros else "green"
    ctx.log("info", f"[bold {cor}][2] Concluído.[/] {total_docs} documentos novos, "
                    f"[bold]{total_itens}[/] itens coletados, {total_erros} erros"
                    f"{f', {resolvidos} pendentes resolvidos' if resolvidos else ''}. "
                    f"→ banco ({contagens['documento']} documentos, {contagens['item']} itens)")

    return StepResult(
        processed=total_itens, erros=total_erros,
        metrics={"documentos_novos": total_docs, "itens_coletados": total_itens,
                  "pendentes_resolvidos": resolvidos,
                  "pendentes_restantes": len(pendentes_docs), **contagens},
        preview=[],
    )


def estimate(params: Params, ctx: RunContext) -> Estimate:
    """Quantas buscas (termo × fonte) faltam. Só HTTP: não gasta LLM."""
    from pesquisa_precos.db import session as db
    from pesquisa_precos.db.repos import documento as repo

    ok, detalhe = db.is_available()
    if not ok:
        return Estimate(detalhes={"aviso": f"banco indisponível: {detalhe}"})
    try:
        filtro = (set(c.strip() for c in params.conceitos.split(","))
                  if params.conceitos else None)
        termos = termos_do_banco(filtro, params.limite_termos)
    except SystemExit as e:
        return Estimate(detalhes={"aviso": str(e)})
    with db.session() as s:
        feitas = (set() if (params.ignorar_cache or params.atualizar)
                  else repo.buscas_concluidas(s))
        n_pendentes_doc = len(repo.pendentes(s))
    total = len(termos) * len(FONTES)
    faltam = sum(1 for t in termos for f in FONTES if (t["termo_id"], f) not in feitas)
    return Estimate(
        unidades=faltam, chamadas_llm=0,
        detalhes={"buscas_totais": total,
                  "já_feitas": total - faltam,
                  "modo": "atualização (para no watermark)" if params.atualizar
                          else "full",
                  "pendentes_sem_homologado": n_pendentes_doc},
    )
