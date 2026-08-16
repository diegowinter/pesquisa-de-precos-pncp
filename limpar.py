"""
Limpeza com escopos explícitos (nunca apaga tudo por default).

  --etapa N   apaga as saídas/checkpoints da etapa N EM DIANTE (o encadeamento: limpar a 3
              invalida 4-8).
  --arquivos  esvazia data/arquivos/ (PDFs baixados).
  --tudo      tudo, EXCETO os ativos caros/curados/manuais: data/0a_*,
              data/1_conceitos_termos.csv e data/6_rotulos_acumulados.csv (com confirmação).

Uso: python limpar.py --etapa 3 | python limpar.py --arquivos | python limpar.py --tudo
"""

import argparse
import glob
import os
import shutil
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

from pesquisa_precos.config import paths

DATA = paths.DATA

# Saídas e checkpoints por etapa (globs relativos a data/). Etapa 6 cobre 6a/6b/6c.
POR_ETAPA = {
    1: ["1_conceitos_termos.csv", "checkpoints/1_*", "erros/1_*"],
    2: ["2_itens_coletados.csv", "checkpoints/2_*", "erros/2_*"],
    3: ["3_itens_classificados.csv", "erros/3_*"],
    4: ["4_itens_sobreviventes.csv", "4_relatorio_corte.csv"],
    5: ["5_itens_enriquecidos.csv", "checkpoints/5_*", "erros/5_*"],
    6: ["6a_pares_candidatos.csv", "6b_pares_rerankeados.csv", "6c_pares_validados.csv",
        "checkpoints/6a_*", "erros/6c_*"],  # 6_rotulos_acumulados.csv é preservado
    7: ["7_itens_agrupados.csv", "7_relatorio_grupos.csv"],
    8: ["8_itens_plaseg.xlsx", "8_itens_plaseg.csv"],
}
ETAPA_MAX = max(POR_ETAPA)

# Ativos preservados no --tudo (caros de reconstruir / curados / manuais).
PRESERVAR = {"1_conceitos_termos.csv", "6_rotulos_acumulados.csv"}


def _apagar(padrao: str) -> int:
    n = 0
    for caminho in glob.glob(str(DATA / padrao)):
        if os.path.isdir(caminho):
            shutil.rmtree(caminho, ignore_errors=True)
        elif os.path.exists(caminho):
            os.remove(caminho)
        n += 1
    return n


def limpar_etapas(inicio: int) -> None:
    for etapa in range(inicio, ETAPA_MAX + 1):
        for padrao in POR_ETAPA.get(etapa, []):
            n = _apagar(padrao)
            if n:
                print(f"  etapa {etapa}: {padrao} ({n})")


def limpar_arquivos() -> None:
    pasta = DATA / "arquivos"
    if pasta.exists():
        shutil.rmtree(pasta, ignore_errors=True)
        pasta.mkdir(parents=True, exist_ok=True)
        print("  data/arquivos/ esvaziado")


def limpar_tudo() -> None:
    resp = input("Apagar TUDO exceto 0a_*, 1_conceitos_termos.csv e 6_rotulos_acumulados.csv? (digite 'sim'): ")
    if resp.strip().lower() != "sim":
        raise SystemExit("Cancelado.")
    for item in DATA.iterdir():
        nome = item.name
        if nome.startswith("0a_") or nome in PRESERVAR:
            continue
        if item.is_dir():
            if nome in ("checkpoints", "erros", "arquivos"):
                shutil.rmtree(item, ignore_errors=True)
                item.mkdir(parents=True, exist_ok=True)
            else:
                shutil.rmtree(item, ignore_errors=True)
        else:
            item.unlink()
        print(f"  removido: {nome}")
    # preserva o rótulo de 6 mesmo dentro de checkpoints? (fica na raiz de data)


def main():
    ap = argparse.ArgumentParser(description="Limpeza escopada da pipeline v3")
    ap.add_argument("--etapa", type=int, help="Apaga da etapa N em diante (1-8)")
    ap.add_argument("--arquivos", action="store_true", help="Esvazia data/arquivos/")
    ap.add_argument("--tudo", action="store_true", help="Tudo, exceto ativos preservados")
    args = ap.parse_args()

    if not (args.etapa or args.arquivos or args.tudo):
        ap.error("Informe um escopo: --etapa N, --arquivos ou --tudo.")
    if args.tudo:
        limpar_tudo()
        return
    if args.etapa:
        if not 1 <= args.etapa <= ETAPA_MAX:
            ap.error(f"--etapa deve estar entre 1 e {ETAPA_MAX}")
        print(f"Limpando etapas {args.etapa}..{ETAPA_MAX}:")
        limpar_etapas(args.etapa)
    if args.arquivos:
        limpar_arquivos()
    print("Concluído.")


if __name__ == "__main__":
    main()
