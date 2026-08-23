"""
m05 — Termos: `1_conceitos_termos.csv` → `termo` + `termo_codigo`.

Colunas do CSV: `conceito, categoria, termos, codigos_catalogo, origem`.

**`conceito` é hoje idêntico a `termos`** — o conceito como entidade separada não existe mais e
não deve ser recriado (docs/02_SCHEMA.md §3, docs/05_MIGRACAO.md §m05). Usamos `termos` como o
termo e ignoramos `conceito`.

`codigos_catalogo` vem separado por '|' (verificado no arquivo real, não assumido). Cada código
vira uma linha em `termo_codigo`, resolvendo o `tipo` pelo catálogo — códigos que não existem
mais no catálogo filtrado são contados e descartados, não migrados com FK quebrada.

`termo_norm` é `core.text.normalizar_termo`: minúsculo e espaços colapsados, **com acento
preservado**. Isso diverge de docs/05_MIGRACAO.md §m05, que manda dobrar o acento aqui também —
e a divergência é deliberada: medido no acervo, dobrar acento colapsa os 499 termos em 338,
porque a etapa 1 gera de propósito o par com/sem acento de TODO termo
(`core/classificacao/variacoes.py`). A busca do PNCP é sensível a acento, então "ambulancia" e
"ambulância" são duas buscas com resultados diferentes. Seguir o documento apagaria 161 termos
em silêncio, e o sintoma — coleta trazendo menos documentos — só apareceria meses depois.

Uso: python -m migracao.m05_termos
"""

from pesquisa_precos.config import paths
from pesquisa_precos.db import session as db
from pesquisa_precos.db.repos import catalogo as repo_cat
from pesquisa_precos.db.repos import termo as repo
from migracao._comum import Relatorio, cabecalho, console, existe, ler_csv


def separar_codigos(valor: str) -> list[str]:
    """O separador real do arquivo é '|'. A vírgula é aceita como tolerância porque o campo
    já foi gravado de duas formas ao longo da v1/v2, e um separador errado não daria erro —
    daria um código gigante que simplesmente não casaria com o catálogo."""
    bruto = (valor or "").replace(",", "|")
    return [c.strip() for c in bruto.split("|") if c.strip()]


def migrar() -> Relatorio:
    rel = Relatorio("m05 — termos")
    if not existe(paths.E1_TERMOS):
        raise SystemExit(f"{paths.E1_TERMOS} ausente. Rode a etapa 1 antes.")

    with db.session() as s:
        tipo_de, ambiguos = repo_cat.tipo_do_codigo(s)
        if ambiguos:
            rel.aviso(f"{len(ambiguos)} códigos existem nos DOIS tipos "
                      f"(ex.: {', '.join(ambiguos[:3])}) — a ligação usou o primeiro tipo.")

        for r in ler_csv(paths.E1_TERMOS):
            termo_txt = (r.get("termos") or "").strip()
            if not termo_txt:
                rel.mais("linhas sem termo")
                continue
            rel.mais("linhas lidas")

            termo_id = repo.upsert(s, termo_txt, (r.get("categoria") or "").strip(),
                                   (r.get("origem") or "").strip())
            if termo_id is None:
                rel.mais("termos vazios após normalizar")
                continue

            pares = []
            for codigo in separar_codigos(r.get("codigos_catalogo", "")):
                tipo = tipo_de.get(codigo)
                if tipo is None:
                    rel.mais("codigos fora do catálogo (descartados)")
                    continue
                pares.append((tipo, codigo))
            rel.mais("ligações termo×código", repo.ligar_codigos(s, termo_id, pares))

        n_termos, n_ligacoes, _ = repo.contar(s)
        rel.mais("termos no banco", n_termos)
        rel.mais("termo_codigo no banco", n_ligacoes)
        if rel.contadores.get("linhas lidas", 0) > n_termos:
            rel.aviso(f"{rel.contadores['linhas lidas'] - n_termos} linhas do CSV colapsaram "
                      f"em termos já existentes (dedup por termo_norm) — esperado.")
    return rel


def main() -> None:
    cabecalho("m05 — termos", paths.E1_TERMOS, "termo, termo_codigo")
    console.print(f"  banco  : {db.database_url()}")
    migrar().imprimir()


if __name__ == "__main__":
    main()
