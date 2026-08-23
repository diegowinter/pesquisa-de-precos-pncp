"""Reescreve o miolo da etapa 3: contadores nomeados no lugar de listas de um elemento."""
import pathlib

NOVO = '''class _Acumulador:
    """Contadores e lote pendente da classificação, compartilhados entre as threads.

    Substitui as listas de um elemento (`n_ok = [0]`) que serviam de célula mutável. O lock
    protege tanto os contadores quanto o lote, que é gravado quando enche.
    """

    TAMANHO_LOTE = 500

    def __init__(self, db, repo):
        self._db = db
        self._repo = repo
        self._lock = threading.Lock()
        self.ok = 0
        self.erros = 0
        self.lote: list[tuple] = []

    def registra_erro(self) -> None:
        with self._lock:
            self.erros += 1

    def registra_ok(self, linha: tuple) -> None:
        """Guarda a linha classificada; grava quando o lote enche."""
        with self._lock:
            self.ok += 1
            self.lote.append(linha)
            cheio = len(self.lote) >= self.TAMANHO_LOTE
        if cheio:
            self.flush()

    def flush(self) -> None:
        """Grava o lote acumulado. Em lote, e não por texto: uma transação por item seria
        mais lenta que a própria chamada de LLM que estamos economizando."""
        with self._lock:
            if not self.lote:
                return
            pendente, self.lote = self.lote, []
        with self._db.raw_connection() as conn:
            self._repo.gravar(conn, pendente)
            conn.commit()


def _rodar(params: Params, ctx: RunContext, resolucao_chat,
           prompts_ativos: dict, reasoning_kw: dict) -> StepResult:
    from pesquisa_precos.db.repos import classification as repo

    ok_banco, detalhe = db.is_available()
    if not ok_banco:
        raise SystemExit(f"Banco indisponível ({detalhe}). Confira DATABASE_URL no .env.")

    with db.session() as s:
        n_textos, n_itens_pend = repo.contar_pendentes(s)
        tarefas = repo.textos_pendentes(s, params.limite)
    if not tarefas:
        with db.session() as s:
            n_recomputadas = repo.recomputar_item_categoria(s)
            s.commit()
        ctx.log("info", "[3] Nada a classificar (todo texto já está em texto_classificacao).")
        return StepResult(metrics={"textos_ja_classificados": 0,
                                   "item_categoria_recomputadas": n_recomputadas})

    n_itens = sum(g["n_itens"] for g in tarefas)
    limite_txt = f" — rodando só {len(tarefas)} (limite)" if params.limite else ""
    ctx.log("info", f"[bold][3] Dedup: {n_itens_pend} itens -> {n_textos} textos únicos[/]"
                    f"{limite_txt} · classificando {len(tarefas)} textos "
                    f"({n_itens} itens), concorrência: {params.concurrency}")

    # Um Curador por thread: compartilhar um cliente HTTP serializaria as chamadas.
    local = threading.local()

    def curador():
        if not hasattr(local, "instancia"):
            local.instancia = ctx.providers.novo_chat(
                curador_kwargs={"prompts_ativos": prompts_ativos, **reasoning_kw}).curador
        return local.instancia

    nome_provedor = resolucao_chat.info.name
    model = getattr(resolucao_chat.info, "model", None) or nome_provedor
    acumulador = _Acumulador(db, repo)

    def classificar(grupo):
        return curador().classificar_categoria(grupo["descricao"], grupo.get("unidade") or "")

    def ao_classificar(grupo, res):
        conf = res.get("confianca", "")
        if conf == "erro":
            # Texto com erro não entra na tabela: entrar o marcaria como pago sem ter sido
            # classificado, e o retry nunca mais o encontraria.
            acumulador.registra_erro()
            ctx.erro_item(grupo["texto_hash"], res.get("_erro"), name=grupo["descricao"])
            return
        # `confianca` é `real` no banco e palavra no LLM; a escala ordinal é declarada em
        # `repo.CONFIANCA_ORDINAL`, a mesma que a migração usa.
        acumulador.registra_ok((
            grupo["texto_hash"], grupo["descricao"], grupo.get("unidade"),
            res["categorias"], repo.confianca_para_real(conf),
            res.get("_prompt_versao_id"), model, nome_provedor, None))

    def ao_falhar(grupo, exc):
        acumulador.registra_erro()
        ctx.erro_item(grupo["texto_hash"], exc, name=grupo["descricao"])

    ctx.progresso(0, len(tarefas), descricao="classificando")
    try:
        executar_paralelo(tarefas, classificar, concurrency=params.concurrency,
                          on_result=ao_classificar, on_error=ao_falhar,
                          on_progress=lambda f, t: ctx.progresso(f, t))
    finally:
        acumulador.flush()   # o que já foi pago é gravado mesmo se a etapa cair no meio

    with db.session() as s:
        n_recomputadas = repo.recomputar_item_categoria(s)
        s.commit()
        contagens = repo.contar(s)

    cor = "yellow" if acumulador.erros else "green"
    ctx.log("info", f"[bold {cor}][3] Concluído.[/] {acumulador.erros} erros. "
                    f"-> texto_classificacao ({contagens.get('texto_classificacao', 0)} textos), "
                    f"item_categoria (+{n_recomputadas})")

    return StepResult(
        processed=acumulador.ok, errors=acumulador.erros,
        metrics={"textos_unicos": len(tarefas), "itens_afetados": n_itens,
                 "item_categoria_recomputadas": n_recomputadas,
                 "dedup": f"{n_itens_pend / max(n_textos, 1):.1f}x"},
        preview=[{"descricao": g["descricao"][:200], "itens": g["n_itens"]}
                 for g in tarefas[:30]],
    )
'''


def main() -> None:
    p = pathlib.Path("pesquisa_precos/steps/e3_classify.py")
    t = p.read_text(encoding="utf-8")
    ini = t.index("def _rodar(params: Params, ctx: RunContext, resolucao_chat,")
    p.write_text(t[:ini] + NOVO, encoding="utf-8")
    print("ok")


if __name__ == "__main__":
    main()
