"""
m06 — Watermark da coleta: `checkpoints/2_watermark.csv` → `coleta_watermark`.

Colunas: `termo, tipo_doc, data_max`.

O que este watermark é (e por que ele não é o que parece): `data_atualizacao_pncp` é o campo
REAL que a API do PNCP usa para ordenar, e ele muda quando um documento é atualizado. A v2
nunca o salvou, então o watermark do acervo foi RECONSTRUÍDO de forma conservadora a partir de
`max(data_publicacao_pncp)` por (termo, tipo_doc) — sempre ≤ o watermark real, portanto nunca
pula documento; no máximo re-varre um pouco a mais. A semeadura foi feita uma vez só, por
`ferramentas/semear_watermark_v2.py`, e **não deve ser refeita**.

Aqui só transportamos esse valor para o banco. O termo é resolvido por `termo_norm` — termos do
checkpoint que não existem mais em `termo` são contados, não inventados.

Uso: python -m migracao.m06_watermark
"""

from pesquisa_precos.config import paths
from pesquisa_precos.db import sessao as db
from pesquisa_precos.db.repos import termo as repo
from migracao._comum import Relatorio, cabecalho, console, existe, ler_csv, timestamp

from pesquisa_precos.core.textos import normalizar_termo


def migrar() -> Relatorio:
    rel = Relatorio("m06 — watermark")
    if not existe(paths.CK_2_WATERMARK):
        rel.aviso(f"{paths.CK_2_WATERMARK.name} ausente — nada a migrar. A primeira "
                  f"`--atualizar` do sistema novo vai varrer tudo de novo (não pula nada).")
        return rel

    with db.sessao() as s:
        por_norm = repo.id_por_norm(s)
        for r in ler_csv(paths.CK_2_WATERMARK):
            rel.mais("linhas lidas")
            termo_id = por_norm.get(normalizar_termo(r.get("termo", "")))
            if termo_id is None:
                rel.mais("termos não encontrados (descartados)")
                continue
            marca = timestamp(r.get("data_max"))
            if marca is None:
                rel.mais("data_max ilegível")
                continue
            tipo_doc = (r.get("tipo_doc") or "").strip()
            if tipo_doc not in ("contrato", "ata"):
                rel.mais("tipo_doc inválido")
                continue
            repo.gravar_watermark(s, termo_id, tipo_doc, marca)
            rel.mais("gravados")

        _, _, n = repo.contar(s)
        rel.mais("coleta_watermark no banco", n)
    return rel


def main() -> None:
    cabecalho("m06 — watermark", paths.CK_2_WATERMARK, "coleta_watermark")
    console.print(f"  banco  : {db.url_banco()}")
    migrar().imprimir()


if __name__ == "__main__":
    main()
