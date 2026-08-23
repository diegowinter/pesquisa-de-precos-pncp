"""
Configuração de bootstrap: o que a aplicação precisa saber antes de conseguir ler o banco.

Só duas variáveis, e nenhuma delas se troca pela tela:
  - `DATABASE_URL` — lida em `db/sessao.py`, não aqui;
  - `APP_SECRET_KEY` — a chave-mestra que cifra os segredos do banco (`db/segredo.py`).

Todo o resto (modelo, base_url, key de API, thresholds) vive no banco e se edita em
`/providers` ou no formulário da etapa. Ver ADR-022.
"""

from dotenv import load_dotenv

from pesquisa_precos.config.paths import RAIZ

load_dotenv(RAIZ / ".env")
