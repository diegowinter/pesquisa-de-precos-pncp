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
  - Poda incremental: códigos marcados 'removido' em data/0a_catalogo_delta.csv (etapa 0a)
    são descartados da exportação final.

Entrada: data/7_itens_agrupados.csv + data/0a_catalogo_filtrado.csv. Saída: data/8_itens_plaseg.xlsx.
Uso: python 8_exportar_plaseg.py
"""

import argparse
import os
import re
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

import pandas as pd
from openpyxl import Workbook

DATA = Path(__file__).resolve().parent / "data"
ENTRADA = DATA / "7_itens_agrupados.csv"
CATALOGO = DATA / "0a_catalogo_filtrado.csv"
SAIDA = DATA / "8_itens_plaseg.xlsx"
SAIDA_CSV = DATA / "8_itens_plaseg.csv"  # mesma tabela em CSV (p/ a visualização web / bundle)
SAIDA_NOVOS = DATA / "8_itens_plaseg_novos.xlsx"
SAIDA_NOVOS_CSV = DATA / "8_itens_plaseg_novos.csv"
# Snapshot das chaves (codigo, controle, item) do último export --novos. Só o modo --novos o
# lê e atualiza; o export completo (padrão) NÃO o toca, para não "consumir" o delta sem querer.
SNAPSHOT = DATA / "8_export_snapshot.csv"

COLUNAS_PLASEG = [
    "Codigo CATMAT/CATSER", "Material/Servico", "Tipo", "Nome", "Descricao Base", "Descricao Especifica",
    "Orgao", "CNPJ Orgao", "UF", "Num Controle PNCP", "Seq Compra", "Seq Ata", "Ano",
    "Item", "Unidade", "Quantidade", "Valor Homologado", "Valor Estimado",
    "Fornecedor", "Data Resultado", "Origem", "Fim de Vigencia",
]

# {cnpj}-{tipo}-{seq}/{ano}[-{seqAta}]  → captura seq (compra/contrato), ano e o sequencial da ata.
_CTRL = re.compile(r"-(\d+)-0*(\d+)/(\d{4})(?:-0*(\d+))?$")


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


def carregar_catalogo() -> dict:
    """codigo → {tipo, nome, descricao} do catálogo (para Tipo/Nome/Descrição Base)."""
    cat = pd.read_csv(CATALOGO, dtype=str, encoding="utf-8-sig").fillna("")
    out = {}
    for _, r in cat.iterrows():
        out[r["codigo"]] = {"tipo": r.get("tipo", ""), "nome_pdm": r.get("nome_pdm", ""),
                            "descricao": r.get("descricao", ""), "nome_classe": r.get("nome_classe", "")}
    return out


def carregar_removidos() -> set:
    """Códigos marcados 'removido' no delta do catálogo (0a) — podados da exportação final."""
    delta = DATA / "0a_catalogo_delta.csv"
    if not (delta.exists() and delta.stat().st_size > 0):
        return set()
    d = pd.read_csv(delta, dtype=str, encoding="utf-8-sig").fillna("")
    return set(d.loc[d["status"] == "removido", "codigo"])


def _chave(linha: dict) -> tuple:
    """Identidade de uma linha do export: (código, nº controle PNCP, item)."""
    return (linha["Codigo CATMAT/CATSER"], linha["Num Controle PNCP"], linha["Item"])


def carregar_snapshot() -> set:
    """Chaves do último export --novos (vazio na 1ª vez → tudo é novo)."""
    if not (SNAPSHOT.exists() and SNAPSHOT.stat().st_size > 0):
        return set()
    s = pd.read_csv(SNAPSHOT, dtype=str, encoding="utf-8-sig").fillna("")
    return set(zip(s["codigo"], s["numeroControlePNCP"], s["numeroItem"]))


def salvar_snapshot(chaves: set) -> None:
    """Grava (atômico) as chaves do export atual como novo baseline do --novos."""
    df = pd.DataFrame(sorted(chaves), columns=["codigo", "numeroControlePNCP", "numeroItem"])
    tmp = SNAPSHOT.with_suffix(".csv.tmp")
    df.to_csv(tmp, index=False, encoding="utf-8-sig")
    os.replace(tmp, SNAPSHOT)


def escrever_export(linhas_csv: list, saida_xlsx: Path, saida_csv: Path) -> None:
    """Grava a lista de linhas (dicts com as COLUNAS_PLASEG) em XLSX + CSV."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Itens PLASEG"
    ws.append(COLUNAS_PLASEG)
    for linha in linhas_csv:
        ws.append([linha[c] for c in COLUNAS_PLASEG])
    wb.save(saida_xlsx)
    pd.DataFrame(linhas_csv, columns=COLUNAS_PLASEG).to_csv(saida_csv, index=False, encoding="utf-8-sig")


def montar_linhas(df: pd.DataFrame, catmap: dict) -> list:
    """Constrói as linhas do export (dicts na ordem das COLUNAS_PLASEG)."""
    linhas_csv = []
    for _, row in df.iterrows():
        cat = catmap.get(row.get("codigo", ""), {})
        tipo = "Serviço" if str(cat.get("tipo", "")).lower().startswith("serv") else "Material"
        descricao_base = cat.get("descricao", "") or row.get("nome_catalogo", "")
        nome = nome_antes_virgula(descricao_base, cat.get("nome_pdm", ""))
        seq_compra, seq_ata, ano_ctrl = parse_controle(row.get("numeroControlePNCP", ""))
        linha = [
            row.get("codigo", ""),
            tipo,
            cat.get("nome_classe", ""),
            nome,
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


def main():
    ap = argparse.ArgumentParser(description="Etapa 8 — export PLASEG")
    ap.add_argument("--novos", action="store_true",
                    help="Exporta só os itens NOVOS desde o último export --novos "
                         "(compara com 8_export_snapshot.csv e o avança). O export completo não o toca.")
    args = ap.parse_args()
    if not ENTRADA.exists():
        raise SystemExit(f"{ENTRADA} ausente. Rode a etapa 7 antes.")
    df = pd.read_csv(ENTRADA, dtype=str, encoding="utf-8").fillna("")
    removidos = carregar_removidos()
    if removidos:
        n0 = len(df)
        df = df[~df["codigo"].isin(removidos)].copy()
        print(f"[8] Poda: {n0 - len(df)} linhas de {len(removidos)} códigos removidos do catálogo.")
    catmap = carregar_catalogo()
    linhas_csv = montar_linhas(df, catmap)

    if args.novos:
        prev = carregar_snapshot()
        novos = [l for l in linhas_csv if _chave(l) not in prev]
        escrever_export(novos, SAIDA_NOVOS, SAIDA_NOVOS_CSV)
        base = "primeira execução (sem snapshot) — tudo é novo" if not prev \
            else f"baseline anterior: {len(prev)} linhas"
        print(f"[8] NOVOS: {len(novos)} de {len(linhas_csv)} linhas ({base}) → {SAIDA_NOVOS}")
        print(f"[8] CSV novos → {SAIDA_NOVOS_CSV}")
        salvar_snapshot({_chave(l) for l in linhas_csv})
        print(f"[8] Snapshot avançado ({len(linhas_csv)} chaves) → {SNAPSHOT.name}")
    else:
        escrever_export(linhas_csv, SAIDA, SAIDA_CSV)
        print(f"[8] Exportadas {len(linhas_csv)} linhas → {SAIDA}")
        print(f"[8] CSV para a web → {SAIDA_CSV}")


if __name__ == "__main__":
    main()
