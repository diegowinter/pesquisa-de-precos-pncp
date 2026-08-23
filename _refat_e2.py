"""Quebra o `run()` da etapa 2 em partes nomeadas. Auxiliar temporário da limpeza."""
import pathlib

NOVO = '''class _Busca(NamedTuple):
    """Uma unidade de trabalho da etapa: um termo procurado numa das fontes."""

    termo: str
    fonte: str
    termo_id: int


class _Totais:
    """Contadores da execução inteira, compartilhados entre as fases."""

    def __init__(self) -> None:
        self.itens = 0
        self.documentos = 0
        self.erros = 0
        self.resolvidos = 0


def _revisitar_pendentes(db, repo, pendentes_docs: dict, conhecidos: set,
                         totais: _Totais, ctx: RunContext) -> None:
    """Tenta de novo os documentos que vieram sem item homologado numa coleta anterior.

    O PNCP publica a homologação depois da capa, então um documento que hoje não tem item
    pode ter amanhã. Só roda em rodada de atualização.
    """
    ctx.log("info", f"[2] Revisitando [bold]{len(pendentes_docs)}[/] pendentes "
                    f"(sem_homologado)...")
    feitos = 0
    ctx.progresso(0, len(pendentes_docs), descricao="pendentes · [green]0 resolvidos[/]")
    for ctrl, rec in list(pendentes_docs.items()):
        if ctx.cancelado():
            break
        try:
            linhas, status = collect_pncp.revisitar_pendente(rec["base"], rec["tipo_doc"], "")
        except Exception as exc:  # noqa: BLE001
            ctx.erro_item(ctrl, exc, tipo=rec["tipo_doc"], name="revisita")
            feitos += 1
            ctx.progresso(feitos)
            continue
        if status == "ok":
            totais.itens += gravar_documento_no_banco(
                db, linhas, rec["base"].get("data_atualizacao_pncp"), rec.get("termo_id"))
            with db.session() as s:
                repo.remover_pendente(s, ctrl)
                s.commit()
            conhecidos.add(ctrl)
            totais.resolvidos += 1
            del pendentes_docs[ctrl]
        feitos += 1
        ctx.progresso(feitos, descricao=f"pendentes · [green]{totais.resolvidos} resolvidos[/]")
    ctx.log("info", f"[2] Pendentes resolvidos: [green]{totais.resolvidos}[/]; "
                    f"ainda pendentes: {len(pendentes_docs)}")


def _coletar_busca(busca: _Busca, params: Params, db, repo, repo_termo, *,
                   conhecidos: set, pendentes_docs: dict, watermark: dict,
                   totais: _Totais, tam_pagina_kw: dict, ctx: RunContext) -> None:
    """Percorre uma busca (termo x fonte) e grava o que for novo.

    Numa rodada de atualização, para de paginar assim que cruza o watermark daquela busca —
    é isso que evita revarrer o acervo inteiro.
    """
    subprogresso(ctx, processed=0, total=None,
                 descricao=f"[cyan]{busca.termo[:24]}[/] ({busca.fonte})")
    n_docs = n_itens = 0
    wm = watermark.get((busca.termo_id, busca.fonte))
    max_atu = wm or ""

    for r in collect_pncp.iter_resultados(
            busca.termo, busca.fonte, on_total=lambda n: subprogresso(ctx, total=n),
            **tam_pagina_kw):
        n_docs += 1
        subprogresso(ctx, processed=n_docs)
        atu = r.get("data_atualizacao_pncp") or ""
        if atu > max_atu:
            max_atu = atu
        if params.atualizar and wm and atu and atu < wm:
            break
        ctrl = r.get("numero_controle_pncp")
        if not ctrl:
            continue
        if ctrl in conhecidos:
            # Documento já coletado: só registra que este termo também o encontrou.
            with db.raw_connection() as conn:
                repo.ligar_termos(conn, [(ctrl, busca.termo_id)])
                conn.commit()
            continue

        linhas, status = collect_pncp.coletar_documento(r, busca.fonte, busca.termo)
        if status != "ok":
            if status == "erro":
                totais.erros += 1
                ctx.erro_item(ctrl, status, tipo=busca.fonte, name=busca.termo)
            elif status == "sem_homologado":
                base = collect_pncp.identificar(r, busca.fonte)
                with db.session() as s:
                    repo.gravar_pendente(s, ctrl, busca.fonte, base,
                                         termo_id=busca.termo_id, data=base.get("data", ""))
                    s.commit()
                pendentes_docs[ctrl] = {"tipo_doc": busca.fonte, "base": base}
            conhecidos.add(ctrl)   # visto: não reprocessa
            continue

        n = gravar_documento_no_banco(db, linhas, atu, busca.termo_id)
        conhecidos.add(ctrl)
        totais.documentos += 1
        totais.itens += n
        n_itens += n

    # Progresso e watermark fecham JUNTOS, na mesma transação: marcar a busca como concluída
    # sem gravar o watermark faria a próxima atualização varrer do zero.
    with db.session() as s:
        repo.marcar_busca(s, busca.termo_id, busca.fonte, n_docs, n_itens)
        if max_atu:
            repo_termo.gravar_watermark(s, busca.termo_id, busca.fonte, max_atu)
        s.commit()
    if max_atu:
        watermark[(busca.termo_id, busca.fonte)] = max_atu


def run(params: Params, ctx: RunContext) -> StepResult:
    db = _exigir_banco()
    from pesquisa_precos.db.repos import documento as repo
    from pesquisa_precos.db.repos import termo as repo_termo

    filtro = set(c.strip() for c in params.conceitos.split(",")) if params.conceitos else None
    termos = termos_do_banco(filtro, params.limite_termos)
    tarefas = [_Busca(t["termo"], fonte, t["termo_id"]) for t in termos for fonte in FONTES]

    with db.session() as s:
        # Numa atualização revisitamos todos os pares (termo, fonte): quem evita o retrabalho
        # é a parada por watermark, não o registro de busca concluída.
        feitas = set() if (params.ignorar_cache or params.atualizar) \\
            else repo.buscas_concluidas(s)
        if params.ignorar_cache:
            repo.limpar_progresso(s)
            s.commit()
        conhecidos = set() if params.ignorar_cache else repo.controles_conhecidos(s)
        watermark = repo_termo.watermarks(s)
        pendentes_docs = {} if params.ignorar_cache else repo.pendentes(s)

    pendentes = [t for t in tarefas if (t.termo_id, t.fonte) not in feitas]
    modo = "[yellow]atualização (para no watermark)[/]" if params.atualizar else "full"
    ctx.log("info", f"[bold][2] Coleta no PNCP -> banco ({modo}):[/] {len(pendentes)} buscas "
                    f"(termo x fonte) a fazer (já feitas: {len(tarefas) - len(pendentes)}, "
                    f"fontes: {', '.join(FONTES)})")

    totais = _Totais()
    tam_pagina_kw = {"tam_pagina": params.tam_pagina} if params.tam_pagina else {}

    if params.atualizar and pendentes_docs:
        _revisitar_pendentes(db, repo, pendentes_docs, conhecidos, totais, ctx)

    feitas_buscas = 0
    ctx.progresso(0, len(pendentes), descricao="buscas · [green]0 itens[/]")
    for busca in pendentes:
        if ctx.cancelado():
            break
        try:
            _coletar_busca(busca, params, db, repo, repo_termo, conhecidos=conhecidos,
                           pendentes_docs=pendentes_docs, watermark=watermark,
                           totais=totais, tam_pagina_kw=tam_pagina_kw, ctx=ctx)
        except Exception as exc:  # noqa: BLE001
            totais.erros += 1
            ctx.log("erro", f"[red]erro[/] {busca.termo} ({busca.fonte}): {str(exc)[:80]}")
            ctx.erro_item(busca.termo, exc, tipo=busca.fonte, name=busca.termo)
        finally:
            feitas_buscas += 1
            ctx.progresso(feitas_buscas,
                          descricao=f"buscas · [green]{totais.itens} itens[/]")

    with db.session() as s:
        contagens = repo.contar(s)
    cor = "yellow" if totais.erros else "green"
    ctx.log("info", f"[bold {cor}][2] Concluído.[/] {totais.documentos} documentos novos, "
                    f"[bold]{totais.itens}[/] itens coletados, {totais.erros} erros"
                    f"{f', {totais.resolvidos} pendentes resolvidos' if totais.resolvidos else ''}. "
                    f"-> banco ({contagens['documento']} documentos, {contagens['item']} itens)")

    return StepResult(
        processed=totais.itens, errors=totais.erros,
        metrics={"documentos_novos": totais.documentos, "itens_coletados": totais.itens,
                 "pendentes_resolvidos": totais.resolvidos,
                 "pendentes_restantes": len(pendentes_docs), **contagens},
        preview=[],
    )
'''


def main() -> None:
    p = pathlib.Path("pesquisa_precos/steps/e2_collect.py")
    t = p.read_text(encoding="utf-8")
    ini = t.index("def run(params: Params, ctx: RunContext) -> StepResult:")
    fim = t.index("def estimate(", ini)
    p.write_text(t[:ini] + NOVO + "\n\n" + t[fim:], encoding="utf-8")
    print("ok")


if __name__ == "__main__":
    main()
