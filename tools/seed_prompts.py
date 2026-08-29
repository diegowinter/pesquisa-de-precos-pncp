"""
Semeia no banco os prompts da pipeline ativa (Fase 6, docs/04_FASES.md — "prompts migrados de
`core/prompts.py` para o banco, com versão ativa e histórico").

Grava a versão 1, ATIVA, dos três prompts que a Curador de fato usa nas etapas com custo de
LLM (3, 5, 6c — os mesmos citados em docs/02_SCHEMA.md §10 como exemplo de `prompt.name`):
'classificar_item', 'casar_item_tabela', 'comparar_par'. O prompt de extração da etapa 5
(`extrair_tabela_documento`) NÃO entra aqui: ele não tem placeholder nenhum — o documento vai
como anexo — e semeá-lo só criaria uma versão para manter em sincronia à toa. O TEXTO gravado aqui é idêntico ao
hardcoded em `core/prompts.py`, só reescrito como template `str.format()` — rodar este script
não muda nenhum resultado: antes dele, `providers/llm_curador.py` usa o hardcoded (nenhuma
`prompt_version` ativa no banco); depois, usa o texto do banco, byte a byte igual.

Placeholders de cada template (ver `core/prompts_resolver.py` e `providers/llm_curador.py`
para quem os preenche):
  classificar_item   → {bloco_categorias} {descricao} {ctx_unidade}
  casar_item_tabela  → {numero} {descricao_api} {tabela_texto}
  comparar_par       → {texto_catalogo} {texto_item}

Chaves `{`/`}` literais do JSON de exemplo pedido na resposta vêm escapadas (`{{`/`}}`) — é
sintaxe de `str.format`, não do prompt em si.

Uso:
  uv run python -m tools.seed_prompts            # semeia (não sobrescreve se já ativo)
  uv run python -m tools.seed_prompts --forcar    # sobrescreve o texto da versão 1
"""

import argparse
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

from rich.console import Console

from pesquisa_precos.db import session as db
from pesquisa_precos.db.repos import execution as repo

console = Console()

TEMPLATE_CLASSIFICAR_ITEM = (
    "Você classifica um item de contrato/ata do PNCP nas categorias de segurança "
    "pública abaixo, para fins de pesquisa de preço.\n\n"
    "REGRAS:\n"
    "- O item deve ser o PRODUTO-FIM completo e funcional. Peças, acessórios, "
    "componentes, suprimentos e recargas NÃO entram em nenhuma categoria.\n"
    "- Pode pertencer a MAIS DE UMA categoria (multi-label) se for genuinamente ambíguo.\n"
    "- Se não se encaixar em NENHUMA categoria de conteúdo, devolva lista vazia.\n"
    "- CUIDADO com armadilhas lexicais: palavras como 'portaria', 'porta', 'radiador' "
    "não têm relação com as categorias; classifique pelo SENTIDO, não pela grafia.\n\n"
    "CATEGORIAS:\n"
    "{bloco_categorias}\n\n"
    "EXEMPLOS:\n"
    '  "PISTOLA .40 CAL, ACABAMENTO EM POLÍMERO" → {{"categorias": ["arma_fogo"], "confianca": "alta"}}\n'
    '  "COLETE BALÍSTICO NÍVEL III-A" → {{"categorias": ["protecao_balistica"], "confianca": "alta"}}\n'
    '  "CARREGADOR AVULSO PARA PISTOLA" → {{"categorias": [], "confianca": "alta"}}\n'
    '  "PORTARIA Nº 123 - AQUISIÇÃO DE COMPUTADORES" → {{"categorias": [], "confianca": "alta"}}\n'
    '  "CADEIRA GIRATÓRIA DE ESCRITÓRIO" → {{"categorias": [], "confianca": "alta"}}\n'
    '  "RÁDIO COMUNICADOR HT DIGITAL VHF" → {{"categorias": ["equip_comunicacao"], "confianca": "alta"}}\n\n'
    "ITEM A CLASSIFICAR:\n"
    "  Descrição: {descricao}{ctx_unidade}\n\n"
    'Responda SOMENTE com JSON puro: {{"categorias": [...], "confianca": "alta|media|baixa"}}'
)

TEMPLATE_CASAR_ITEM_TABELA = (
    "Você recebe UM item da API do PNCP e a TABELA DE ITENS extraída do documento da "
    "ata/contrato. Diga qual linha da tabela é o MESMO item — casando por número do "
    "item e/ou descrição — ou que não há correspondência.\n\n"
    "ITEM DA API (referência, descrição pobre):\n"
    "  Número do item: {numero}\n"
    "  Descrição: {descricao_api}\n\n"
    "TABELA DO DOCUMENTO:\n"
    "{tabela_texto}\n\n"
    "REGRAS:\n"
    "- Case pelo SENTIDO do objeto, não pela grafia. O número do item ajuda, mas a "
    "descrição manda: se o número aponta uma linha de objeto claramente diferente, "
    "confie na descrição.\n"
    "- COPIE preco_unitario e quantidade da LINHA escolhida, exatamente como estão na "
    "tabela. NÃO converta separador decimal e NÃO arredonde.\n"
    "- descricao_completa é a descrição do item COMO ESTÁ na tabela, com as "
    "especificações técnicas, sem preço, marca nem fornecedor.\n"
    "- fornecedor só se a tabela tiver essa informação; senão, string vazia.\n"
    "- Se NENHUMA linha for o mesmo item, encontrado=false e os demais campos vazios. "
    "NÃO invente correspondência.\n\n"
    'Responda SOMENTE com JSON puro: {{"encontrado": true, "descricao_completa": "...", '
    '"preco_unitario": "", "quantidade": "", "fornecedor": ""}}'
)

TEMPLATE_COMPARAR_PAR = (
    "Você compara um item de catálogo (CATMAT/CATSER) com um item real de contrato/ata "
    "do PNCP para decidir se são o MESMO item para fins de pesquisa de preço.\n\n"
    "CRITÉRIO: mesmo TIPO de item conta como 'sim' — variações de marca, model, redação "
    "ou nível de detalhe equivalentes são o mesmo item. Itens de natureza ou finalidade "
    "distinta contam como 'não', mesmo que relacionados (ex.: arma vs. coldre da arma).\n\n"
    "ITEM DO CATÁLOGO (referência):\n"
    "  {texto_catalogo}\n\n"
    "ITEM DO PNCP (descrição enriquecida):\n"
    "  {texto_item}\n\n"
    'Responda SOMENTE com JSON puro: {{"mesmo_item": "sim|nao", "justificativa": "até 15 palavras"}}'
)

PROMPTS = (
    ("classificar_item", "Etapa 3 — classifica um item PNCP em 0+ categorias de conteúdo.",
     TEMPLATE_CLASSIFICAR_ITEM),
    ("casar_item_tabela", "Etapa 5 — casa um item da API contra a tabela do documento.",
     TEMPLATE_CASAR_ITEM_TABELA),
    ("comparar_par", "Etapa 6c — decide se catálogo e item PNCP são o mesmo item.",
     TEMPLATE_COMPARAR_PAR),
)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--forcar", action="store_true",
                    help="sobrescreve o texto da versão 1 mesmo se ela já existir")
    args = ap.parse_args()

    with db.session() as sessao:
        for name, descricao, template in PROMPTS:
            existente = repo.prompt_versao_ativa(sessao, name)
            if existente is not None and not args.forcar:
                console.print(f"[dim]'{name}' já tem versão ativa (id={existente}) — pulado "
                              f"(use --forcar para sobrescrever a v1)[/]")
                continue
            versao_id = repo.upsert_prompt(sessao, name, descricao, "chat", template,
                                           versao=1, ativa=True)
            console.print(f"[green]'{name}'[/] semeado — prompt_version.id={versao_id}")


if __name__ == "__main__":
    main()
