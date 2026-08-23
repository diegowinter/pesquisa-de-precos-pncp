"""
Semeia no banco os prompts da pipeline ativa (Fase 6, docs/04_FASES.md — "prompts migrados de
`core/prompts.py` para o banco, com versão ativa e histórico").

Grava a versão 1, ATIVA, dos três prompts que a Curador de fato usa nas etapas com custo de
LLM (3, 5b, 6c — os mesmos citados em docs/02_SCHEMA.md §10 como exemplo de `prompt.name`):
'classificar_item', 'extrair_item_pdf', 'comparar_par'. O TEXTO gravado aqui é idêntico ao
hardcoded em `core/prompts.py`, só reescrito como template `str.format()` — rodar este script
não muda nenhum resultado: antes dele, `providers/llm_curador.py` usa o hardcoded (nenhuma
`prompt_version` ativa no banco); depois, usa o texto do banco, byte a byte igual.

Placeholders de cada template (ver `core/prompts_resolver.py` e `providers/llm_curador.py`
para quem os preenche):
  classificar_item   → {bloco_categorias} {descricao} {ctx_unidade}
  extrair_item_pdf   → {numero} {descricao_api} {janela_texto}
  comparar_par       → {texto_catalogo} {texto_item}

Chaves `{`/`}` literais do JSON de exemplo pedido na resposta vêm escapadas (`{{`/`}}`) — é
sintaxe de `str.format`, não do prompt em si.

Uso:
  uv run python -m tools.seed_prompts            # semeia (não sobrescreve se já active)
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

TEMPLATE_EXTRAIR_ITEM_PDF = (
    "Você extrai os dados de UM item específico do texto de um contrato/ata (PDF).\n\n"
    "O item procurado, conforme a API do PNCP:\n"
    "  Número do item: {numero}\n"
    "  Descrição (API, pobre): {descricao_api}\n\n"
    "No texto abaixo, localize ESSE item (pelo número e/ou pela descrição) e extraia:\n"
    "  - descricao_completa: a descrição rica do item como aparece no documento "
    "(especificações técnicas, sem valores/marca/fornecedor);\n"
    "  - preco_unitario: valor unitário (número, ponto decimal);\n"
    "  - quantidade: quantidade (número);\n"
    "  - encontrado: true se localizou o item com confiança, false caso contrário.\n\n"
    "NÃO invente. Se não encontrar o item no texto, devolva encontrado=false e os "
    "demais campos nulos. NÃO liste outros itens.\n\n"
    "TEXTO DO DOCUMENTO:\n"
    "{janela_texto}\n\n"
    'Responda SOMENTE com JSON puro: '
    '{{"descricao_completa": "...", "preco_unitario": 0.0, "quantidade": 0, "encontrado": true}}'
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
    ("extrair_item_pdf", "Etapa 5b — extração guiada de um item a partir do texto do PDF.",
     TEMPLATE_EXTRAIR_ITEM_PDF),
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
