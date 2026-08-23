"""
Etapa 8 — Export XLSX no formato PLASEG (aba "Itens PLASEG").

Schema (definido com o cliente):
  Código CATMAT/CATSER | Material/Serviço | Nome | Descrição Base | Descrição Específica | + params PNCP
  - Código        = codigo do catálogo (CATMAT p/ material, CATSER p/ serviço).
  - Tipo          = "Material" ou "Serviço" (coluna `tipo` do catálogo).
  - Nome          = nome do CATMAT ANTES da 1ª vírgula (o núcleo, sem as características).
  - Descrição Base= descrição CATMAT completa (núcleo + características).
  - Desc. Específica = descrição ENRIQUECIDA do item PNCP (descricao_final: PDF quando houver,
                    senão a da API).
  - Params PNCP preservados p/ rastreio: órgão, CNPJ, UF, nº controle, sequencial compra,
    sequencial ata, ano, item, unidade, quantidade, valor homologado, valor estimado,
    fornecedor, data do resultado.
  - Origem = "Ata" ou "Contrato" (do tipo_doc).
  - Fim de Vigencia = Ata → data final da ata; Contrato → assinatura + 1 ano (calculada).
  - Poda incremental: códigos desativados no catálogo (`catalogo_item.active = false`, que a
    0a mantém) são descartados da exportação final.

Entrada: `grupo_item` do run indicado (default: o último que produziu ranking), com catálogo,
item, documento e enriquecido no mesmo SELECT.
Saída: uma linha em `export`, com o XLSX em `export.conteudo` (ADR-018) — a interface serve o
download de lá. A etapa NÃO escreve arquivo. O baseline do `--novos` é `export_snapshot`.
Chave de resumo: nenhuma — recomputa o corpus inteiro.

NÃO fazer: deixar o export completo tocar o snapshot do `--novos` (isso "consumiria" o delta
sem querer). E a primeira execução de `--novos` sem snapshot marca TUDO como novo — isso é
esperado, a correção é semear o snapshot a partir do último export oficial (m16).

Uso: pela interface web — `--novos` é a caixa "só o delta desde o último export".
"""

import re
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

import pandas as pd
from openpyxl import Workbook
from pydantic import BaseModel, Field

from pesquisa_precos.steps.base import RunContext, Estimate, StepResult

KEY = "8"
# 2.0.0 (Fase 13): o caminho CSV saiu — nada é gravado em disco (ADR-018/ADR-020). O formato
# do XLSX nunca mudou; o que mudou é onde ele vive.
CODE_VERSION = "2.0.0"

# NOME do arquivo oferecido no download (`export.nome_arquivo`), não um caminho: a etapa não
# escreve em disco. O usuário salva onde quiser, a partir da interface.
NOME_COMPLETO = "8_itens_plaseg.xlsx"
NOME_NOVOS = "8_itens_plaseg_novos.xlsx"

COLUNAS_PLASEG = [
    "Codigo CATMAT/CATSER", "Material/Servico", "Tipo", "Nome", "Descricao Base", "Descricao Especifica",
    "Orgao", "CNPJ Orgao", "UF", "Num Controle PNCP", "Seq Compra", "Seq Ata", "Ano",
    "Item", "Unidade", "Quantidade", "Valor Homologado", "Valor Estimado",
    "Fornecedor", "Data Resultado", "Origem", "Fim de Vigencia",
]

# {cnpj}-{tipo}-{seq}/{ano}[-{seqAta}]  → captura seq (compra/contrato), ano e o sequencial da ata.
_CTRL = re.compile(r"-(\d+)-0*(\d+)/(\d{4})(?:-0*(\d+))?$")


class Params(BaseModel):
    novos: bool = Field(
        False, description="Exporta só os itens NOVOS desde o último export --novos "
                           "(compara com o snapshot anterior e o avança).")
    run_id: int | None = Field(
        None, description="Run a exportar (default: o último com ranking em grupo_item)")


def parse_controle(nc: str) -> tuple[str, str, str]:
    """numeroControlePNCP → (seq_compra_ou_contrato, seq_ata, ano)."""
    m = _CTRL.search(nc or "")
    if not m:
        return "", "", ""
    _tipo, seq, ano, ata = m.groups()
    return str(int(seq)), (str(int(ata)) if ata else ""), ano


def nome_antes_virgula(descricao: str, nome_pdm: str) -> str:
    """Núcleo do CATMAT: texto antes da 1ª vírgula da descrição (fallback nome_pdm)."""
    if descricao and "," in descricao:
        return descricao.split(",", 1)[0].strip()
    return (nome_pdm or descricao or "").strip()


def formatar_data(valor) -> str:
    if not valor or pd.isna(valor):
        return ""
    parte = str(valor).split("T")[0].split(" ")[0]
    p = parte.split("-")
    return f"{p[2]}/{p[1]}/{p[0]}" if len(p) == 3 else str(valor)


def origem_documento(row) -> str:
    """Ata (registro de preços) ou Contrato, a partir do tipo_doc do PNCP."""
    return "Contrato" if str(row.get("tipo_doc", "")).lower() == "contrato" else "Ata"


def fim_vigencia(row) -> str:
    """Fim de vigência: Ata → data final da ata; Contrato → assinatura + 1 ano (calculada)."""
    if str(row.get("tipo_doc", "")).lower() == "contrato":
        assinatura = row.get("data_assinatura")
        if not assinatura or pd.isna(assinatura):
            return ""
        try:
            d = pd.to_datetime(str(assinatura).split("T")[0]) + pd.DateOffset(years=1)
            return d.strftime("%d/%m/%Y")
        except (ValueError, TypeError):
            return ""
    return formatar_data(row.get("data_fim_vigencia", ""))


def formatar_valor_br(valor) -> str:
    if valor is None or pd.isna(valor) or str(valor).strip() == "":
        return ""
    try:
        num = float(str(valor).replace(",", "."))
        decimal = f"{num:.2f}".split(".")[1]
        return f"{int(num):,}".replace(",", ".") + f",{decimal}"
    except (ValueError, TypeError):
        return str(valor)


def _chave(linha: dict) -> tuple:
    """Identidade de uma linha do export: (código, nº controle PNCP, item)."""
    return (linha["Codigo CATMAT/CATSER"], linha["Num Controle PNCP"], linha["Item"])


# ── Fonte: banco ────────────────────────────────────────────────────────────────────

def _quantidade_texto(valor) -> str:
    """Quantidade como o caminho CSV a entrega: repr de float ('1.0', '2420.0').

    Vem do banco como `Decimal('2420.0000')`; escrever assim mudaria a célula do XLSX em
    relação ao último export oficial, e o critério de aceite da fase é justamente comparar os
    dois. Preço não passa por aqui — ele vai para `formatar_valor_br`, que já normaliza.
    """
    if valor is None or valor == "":
        return ""
    try:
        return str(float(valor))
    except (TypeError, ValueError):
        return str(valor)


def carregar_do_banco(params: Params, ctx: RunContext) -> tuple[pd.DataFrame, dict, int]:
    """(linhas agrupadas, catálogo por código, run_id) a partir de `grupo_item`.

    O catálogo sai do MESMO SELECT (join com `catalogo_item`), então não há segunda consulta
    nem chance de o export usar uma versão do catálogo diferente da que produziu o ranking.
    """
    from pesquisa_precos.db import session as db
    from pesquisa_precos.db.repos import grupo as repo_grupo

    ok, detalhe = db.is_available()
    if not ok:
        raise SystemExit(f"Banco indisponível ({detalhe}). Confira DATABASE_URL no .env.")
    with db.session() as s:
        run_id = params.run_id or repo_grupo.ultimo_run_com_grupos(s)
        if run_id is None:
            raise SystemExit("Nenhum run com ranking em grupo_item. Rode a etapa 7 antes.")
        linhas = repo_grupo.linhas_do_run(s, run_id)
    if not linhas:
        raise SystemExit(f"run #{run_id} não tem linhas em grupo_item.")

    catmap = {
        r["codigo"]: {"tipo": r["tipo"], "nome_pdm": r["nome_pdm"] or "",
                      "descricao": r["descricao_catalogo"] or "",
                      "nome_classe": r["nome_classe"] or "", "active": r["active"]}
        for r in linhas
    }
    df = pd.DataFrame(linhas).rename(columns={
        "numero_controle_pncp": "numeroControlePNCP",
        "numero_item": "numeroItem",
    })
    df["quantidade"] = df["quantidade"].map(_quantidade_texto)
    # `montar_linhas` trata tudo como texto (o caminho CSV lê com dtype=str). Preço é
    # convertido lá dentro por `formatar_valor_br`, então string aqui é seguro.
    for coluna in df.columns:
        if coluna != "quantidade":
            df[coluna] = df[coluna].map(lambda v: "" if v is None else str(v))
    ctx.log("info", f"[8] Fonte: banco (run #{run_id}) — {len(df)} linhas agrupadas.")
    return df, catmap, run_id


def carregar_snapshot(params: Params | None = None) -> set:
    """Chaves do último export --novos (vazio na 1ª vez → tudo é novo).

    A PK de `export_snapshot` inclui o `tipo`, mas ele é descartado aqui: a identidade de uma
    linha do export é (código, nº controle, item). Incluir o tipo mudaria o delta sem motivo.
    """
    from pesquisa_precos.db import session as db
    from pesquisa_precos.db.repos import grupo as repo_grupo
    with db.session() as s:
        return {(codigo, nc, str(numero))
                for _tipo, codigo, nc, numero in repo_grupo.snapshot(s)}


def salvar_snapshot(chaves: set, params: Params | None = None,
                    catmap: dict | None = None, export_id: int | None = None) -> None:
    """Grava as chaves do export atual como novo baseline do --novos.

    A transação dá a atomicidade: um snapshot gravado pela metade faria o próximo `--novos`
    reportar milhares de linhas velhas como novidade.
    """
    from pesquisa_precos.db import session as db
    from pesquisa_precos.db.repos import grupo as repo_grupo
    catmap = catmap or {}
    completas = []
    for codigo, nc, numero in chaves:
        tipo = (catmap.get(codigo, {}).get("tipo") or "material")
        try:
            completas.append((tipo, codigo, nc, int(numero)))
        except (TypeError, ValueError):
            continue  # item sem número utilizável não identifica linha nenhuma
    with db.raw_connection() as conn:
        repo_grupo.avancar_snapshot(conn, completas, export_id, substituir=True)


def montar_xlsx(linhas_csv: list) -> bytes:
    """XLSX em MEMÓRIA (ADR-018): o export vive no banco, não em disco.

    `openpyxl` escreve em qualquer file-like, então isto é o mesmo `wb.save()` de sempre com
    um `BytesIO` no lugar do caminho — nenhuma mudança de formato.
    """
    import io

    wb = Workbook()
    ws = wb.active
    ws.title = "Itens PLASEG"
    ws.append(COLUNAS_PLASEG)
    for linha in linhas_csv:
        ws.append([linha[c] for c in COLUNAS_PLASEG])
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def montar_linhas(df: pd.DataFrame, catmap: dict) -> list:
    """Constrói as linhas do export (dicts na ordem das COLUNAS_PLASEG)."""
    linhas_csv = []
    for _, row in df.iterrows():
        cat = catmap.get(row.get("codigo", ""), {})
        tipo = "Serviço" if str(cat.get("tipo", "")).lower().startswith("serv") else "Material"
        descricao_base = cat.get("descricao", "") or row.get("nome_catalogo", "")
        name = nome_antes_virgula(descricao_base, cat.get("nome_pdm", ""))
        seq_compra, seq_ata, ano_ctrl = parse_controle(row.get("numeroControlePNCP", ""))
        linha = [
            row.get("codigo", ""),
            tipo,
            cat.get("nome_classe", ""),
            name,
            descricao_base,
            row.get("descricao_final", ""),
            row.get("orgao", ""),
            row.get("orgao_cnpj", ""),
            row.get("uf", ""),
            row.get("numeroControlePNCP", ""),
            seq_compra,
            seq_ata,
            row.get("ano", "") or ano_ctrl,
            row.get("numeroItem", ""),
            row.get("unidade", ""),
            row.get("quantidade", ""),
            formatar_valor_br(row.get("preco_unitario", "")),
            formatar_valor_br(row.get("preco_estimado", "")),
            row.get("fornecedor", ""),
            formatar_data(row.get("data_resultado", "")),
            origem_documento(row),
            fim_vigencia(row),
        ]
        linhas_csv.append(dict(zip(COLUNAS_PLASEG, linha)))
    return linhas_csv


def carregar_entrada(params: Params, ctx: RunContext) -> tuple[pd.DataFrame, dict, int | None]:
    """(linhas agrupadas, catálogo por código, run_id) — tudo de `grupo_item`."""
    return carregar_do_banco(params, ctx)


def podar_removidos(df: pd.DataFrame, catmap: dict, params: Params,
                    ctx: RunContext) -> tuple[pd.DataFrame, int]:
    """Tira do export os códigos removidos do catálogo. Devolve (df podado, nº de códigos).

    A marca é `catalogo_item.active = false`, que a 0a mantém a partir do delta do catálogo.
    """
    removidos = {c for c, dados in catmap.items() if not _verdadeiro(dados.get("active"))}
    if not removidos:
        return df, 0
    n0 = len(df)
    df = df[~df["codigo"].isin(removidos)].copy()
    ctx.log("info", f"[8] Poda: {n0 - len(df)} linhas de {len(removidos)} códigos "
                    f"removidos do catálogo.")
    return df, len(removidos)


def _verdadeiro(valor) -> bool:
    """`active` chega como bool (banco) ou como a string 'True'/'False' (após o cast a texto)."""
    if isinstance(valor, str):
        return valor.strip().lower() in ("true", "t", "1")
    return bool(valor)


def estimate(params: Params, ctx: RunContext) -> Estimate:
    """Sem LLM: conta as linhas que sairiam do export (e quantas seriam podadas)."""
    try:
        df, catmap, run_id = carregar_entrada(params, ctx)
    except SystemExit as exc:
        return Estimate(detalhes={"aviso": str(exc)})
    podado, _ = podar_removidos(df, catmap, params, ctx)
    detalhes = {"run_id": run_id, "linhas_agrupadas": len(df),
                "podadas_por_catalogo_removido": len(df) - len(podado)}
    if params.novos:
        prev = carregar_snapshot(params)
        detalhes["snapshot_anterior"] = (
            f"{len(prev)} chaves" if prev else "ausente — a 1ª execução marca TUDO como novo")
    return Estimate(unidades=len(podado), chamadas_llm=0, cost_usd=0.0, detalhes=detalhes)


def registrar_export(params: Params, run_id: int | None, tipo: str, nome_arquivo: str,
                     linhas: list) -> int | None:
    """Uma linha em `export` por export gerado — é o registro E o arquivo.

    É o que permite responder "qual arquivo saiu de qual run" sem depender do nome, que é
    sempre o mesmo.
    """
    if run_id is None:
        return None
    import hashlib

    from pesquisa_precos.db import session as db
    from pesquisa_precos.db.repos import grupo as repo_grupo
    codigos = {l["Codigo CATMAT/CATSER"] for l in linhas}
    # ADR-018 §2: o XLSX vai para `export.conteudo`. `arquivo` fica NULL — não existe arquivo
    # em disco para o caminho apontar.
    conteudo = montar_xlsx(linhas)
    with db.session() as s:
        return repo_grupo.registrar_export(
            s, run_id, tipo, None, len(linhas), len(codigos),
            hashlib.sha1(conteudo).hexdigest(),
            conteudo=conteudo, nome_arquivo=nome_arquivo)


def run(params: Params, ctx: RunContext) -> StepResult:
    df, catmap, run_id = carregar_entrada(params, ctx)
    df, n_codigos_removidos = podar_removidos(df, catmap, params, ctx)
    linhas_csv = montar_linhas(df, catmap)

    if params.novos:
        prev = carregar_snapshot(params)
        novos = [l for l in linhas_csv if _chave(l) not in prev]
        base = "primeira execução (sem snapshot) — tudo é novo" if not prev \
            else f"baseline anterior: {len(prev)} linhas"
        ctx.log("info", f"[8] NOVOS: {len(novos)} de {len(linhas_csv)} linhas ({base}) "
                        f"→ {NOME_NOVOS}")
        export_id = registrar_export(params, run_id, "novos", NOME_NOVOS, novos)
        # O snapshot avança com as chaves do export COMPLETO, não das novas: ele é o retrato
        # do que já foi entregue, e só as novas deixaria o delta se repetir para sempre.
        salvar_snapshot({_chave(l) for l in linhas_csv}, params, catmap, export_id)
        ctx.log("info", f"[8] Snapshot avançado ({len(linhas_csv)} chaves).")
        return StepResult(
            processed=len(novos), erros=0,
            metrics={"linhas_novas": len(novos), "linhas_no_export": len(linhas_csv),
                      "baseline_anterior": len(prev), "run_id": run_id},
            preview=novos[:50],
        )

    registrar_export(params, run_id, "completo", NOME_COMPLETO, linhas_csv)
    ctx.log("info", f"[8] Exportadas {len(linhas_csv)} linhas → {NOME_COMPLETO} "
                    f"(baixe pela tela de exports)")
    return StepResult(
        processed=len(linhas_csv), erros=0,
        metrics={"linhas_no_export": len(linhas_csv),
                  "codigos_podados": n_codigos_removidos, "run_id": run_id},
        preview=linhas_csv[:50],
    )
