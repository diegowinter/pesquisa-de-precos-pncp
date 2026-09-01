"""Gera `web/templates/_icons.svg` a partir do pacote npm `lucide-static` (ISC).

Não roda no fluxo normal: o sprite está commitado. Rode só para acrescentar um ícone —
adicione o nome em NOMES e execute com o pacote já baixado:

    npm pack lucide-static@1.38.0 && tar xzf lucide-static-1.38.0.tgz
    uv run python tools/gerar_sprite_icones.py package/icons

O sprite é INCLUÍDO no `base.html` (não servido como estático): `<use href>` externo tem
histórico ruim de suporte, e 6 KB inline sai mais barato que uma requisição a mais.
"""

from __future__ import annotations

import sys
from pathlib import Path

NOMES = """layers circle-dollar-sign download git-compare list-checks message-square-text
sliders-horizontal settings plug bell circle circle-play pause check triangle-alert x ban
user log-out refresh-cw play file-spreadsheet""".split()

DESTINO = Path(__file__).resolve().parent.parent / "pesquisa_precos/web/templates/_icons.svg"
ATRIBUTOS = ('viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
             'stroke-linecap="round" stroke-linejoin="round"')


def main(origem: Path) -> None:
    linhas = ['<svg xmlns="http://www.w3.org/2000/svg" style="display:none">',
              "<!-- Ícones Lucide v1.38.0 (ISC). Sprite gerado; ver "
              "tools/gerar_sprite_icones.py. -->"]
    for nome in NOMES:
        svg = (origem / f"{nome}.svg").read_text(encoding="utf-8")
        corpo = svg[svg.index(">", svg.index("<svg")) + 1:svg.rindex("</svg>")]
        linhas.append(f'<symbol id="i-{nome}" {ATRIBUTOS}>{" ".join(corpo.split())}</symbol>')
    linhas.append("</svg>")
    DESTINO.write_text("\n".join(linhas) + "\n", encoding="utf-8")
    print(f"{len(NOMES)} ícones → {DESTINO}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
