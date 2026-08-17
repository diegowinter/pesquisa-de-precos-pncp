"""
Prompts da curadoria de itens de segurança pública.

Isolado da camada de LLM (`llm_curador.py`) para facilitar ajuste/versão dos textos.
As CATEGORIAS ficam como dados em `scripts/categorias.py`; aqui elas são renderizadas
dentro do prompt e os mapas/ids são derivados automaticamente (adicionar categoria = só
editar o array em categorias.py). Classificador CONSERVADOR (itens-cat-v2/v3).

Os construtores aceitam `com_justificativa`:
  True  → saída "id_categoria|justificativa" (registro final)
  False → saída só com o id (economia de tokens de saída — usado na 1ª rodada)
"""

import json

from pesquisa_precos.core.classificacao.categorias import CATEGORIAS_MATERIAL as _DEF_MATERIAL
from pesquisa_precos.core.classificacao.categorias import CATEGORIAS_SERVICO as _DEF_SERVICO
from pesquisa_precos.core.classificacao.categorias import META_CATEGORIAS as _META
from pesquisa_precos.core.classificacao.categorias import IDS_CONTEUDO as _IDS_CONTEUDO

# ── Derivados dos arrays de categorias (única fonte da verdade: categorias.py) ───
# Conjunto de ids válidos e mapa de reconhecimento (id em maiúsculas → id).
# O `parse_resposta` (llm_curador.py) remove acentos antes do lookup, então não é
# preciso cadastrar variantes acentuadas aqui.
CATEGORIAS_MATERIAL = {c["id"] for c in _DEF_MATERIAL}
CATEGORIAS_SERVICO = {c["id"] for c in _DEF_SERVICO}
CATEGORIA_MAP_MATERIAL = {c["id"].upper(): c["id"] for c in _DEF_MATERIAL}
CATEGORIA_MAP_SERVICO = {c["id"].upper(): c["id"] for c in _DEF_SERVICO}


def _render_categorias(definicoes) -> str:
    """Monta o bloco 'id\\n  regra\\n\\n' de cada categoria, na ordem do array."""
    return "".join(f"{c['id']}\n{c['regra'].strip(chr(10))}\n\n" for c in definicoes)


def _instrucao_saida(com_justificativa: bool, exemplo: str) -> str:
    """Instrução final do prompt: id|justificativa (default) ou só o id (economia de tokens)."""
    if com_justificativa:
        return (
            "Responda SOMENTE no formato: id_categoria|justificativa em até 15 palavras\n"
            f"Exemplo: {exemplo}"
        )
    id_exemplo = exemplo.split("|", 1)[0]
    return (
        "Responda SOMENTE com o id da categoria, sem nenhum outro texto.\n"
        f"Exemplo: {id_exemplo}"
    )


def montar_prompt_material(row, com_justificativa: bool = True) -> str:
    nome_classe = row.get("nomeClasse", "") or ""
    nome_pdm = row.get("nomePdm", "") or ""
    descricao = row.get("descricaoItem", "") or ""
    return (
        "Você é um classificador CONSERVADOR de itens de segurança pública.\n"
        "Leia o item abaixo, analise TODOS os campos (Classe, PDM e Descrição) em conjunto,\n"
        "e escolha EXATAMENTE UMA categoria da lista.\n\n"
        "REGRA-ZERO (LEIA PRIMEIRO):\n"
        "  O item deve ser o PRODUTO-FIM em si, completo e funcional. Acessórios, peças,\n"
        "  partes, componentes, suprimentos, baterias, carregadores, cabos, fontes, antenas\n"
        "  avulsas, suportes, capas, estojos, coldres, miras, lanternas, pneus, hélices,\n"
        "  recargas e similares devem SEMPRE ser classificados como \"outros\", mesmo quando\n"
        "  descritos como de arma, de rádio, de viatura, de drone, etc.\n"
        "  Se a Descrição indicar peça, acessório, componente ou suprimento, ignore o\n"
        "  nome da Classe e classifique como \"outros\".\n"
        "  Em qualquer dúvida, prefira \"outros\". É melhor classificar como \"outros\" do que\n"
        "  inflar uma categoria específica.\n\n"
        "CATEGORIAS:\n\n"
        + _render_categorias(_DEF_MATERIAL)
        + "ITEM A CLASSIFICAR:\n"
        f"  Classe: {nome_classe}\n"
        f"  PDM: {nome_pdm}\n"
        f"  Descrição: {descricao}\n\n"
        + _instrucao_saida(
            com_justificativa,
            "arma_fogo|pistola semiautomática calibre 9mm de uso policial",
        )
    )


def montar_prompt_busca(nome: str, descricao: str, categoria: str = "") -> str:
    """
    Prompt p/ gerar um termo de busca textual otimizado para o endpoint de busca do PNCP,
    a partir de uma linha do catálogo já curado (nome + descrição + categoria).

    Objetivo: nem genérico demais (traz ruído de itens não relacionados) nem específico
    demais (vira uma busca que não bate com o objeto de nenhuma contratação real — códigos
    de referência, part numbers e marcas hiperespecíficas normalmente não aparecem no texto
    de um edital/contrato).
    """
    contexto_categoria = f"  Categoria: {categoria}\n" if categoria else ""
    return (
        "Você monta termos de busca textual para o portal do PNCP (Portal Nacional de "
        "Contratações Públicas), a partir de um item de catálogo (CATMAT/CATSER) já "
        "confirmado como relevante.\n\n"
        "O termo deve ser CURTO (de 2 a 5 palavras) e deve funcionar como uma busca de "
        "texto livre no objeto de contratos/atas de registro de preço reais. Para isso:\n"
        "  - Mantenha o núcleo do nome do item (o que ele É).\n"
        "  - Acrescente NO MÁXIMO 1 ou 2 detalhes discriminantes da descrição — só os que\n"
        "    plausivelmente apareceriam escritos no objeto de uma contratação real (ex.:\n"
        "    calibre, cilindrada/potência do motor, capacidade, voltagem, material).\n"
        "  - NÃO inclua códigos de referência, part numbers, marcas/modelos "
        "hiperespecíficos,\n"
        "    medidas exatas de engenharia ou qualquer detalhe raro demais para aparecer "
        "num edital.\n"
        "  - NÃO deixe o termo genérico demais (só o nome cru) nem específico demais "
        "(uma frase\n"
        "    inteira com todas as especificações).\n\n"
        "ITEM DO CATÁLOGO:\n"
        f"  Nome: {nome}\n"
        f"{contexto_categoria}"
        f"  Descrição: {descricao}\n\n"
        "Responda SOMENTE com o termo de busca, sem aspas e sem nenhum outro texto.\n"
        "Exemplo: viatura com cela motor 2.0"
    )


def montar_prompt_extrair_itens(texto: str) -> str:
    """
    Prompt p/ extrair os itens de uma ata de registro de preços (texto já extraído do PDF
    por `parsear_pdfs.py`), pensado para um modelo PEQUENO (7B, ex. OPENAI_MODEL_VISION):
    instruções curtas, diretas, uma regra por linha, e um formato de saída rígido com
    exemplo, para reduzir a chance do modelo divagar ou inventar campos extras.
    """
    return (
        "Tarefa: extrair a lista de ITENS de uma ata de registro de preços.\n\n"
        "REGRAS:\n"
        "1. Cada item vira UMA linha, em texto corrido (sem quebras de linha internas).\n"
        "2. Inclua no texto do item: nome/descrição, especificações técnicas e unidade\n"
        "   de medida/quantidade, se aparecerem no documento.\n"
        "3. NÃO inclua: valor unitário, valor total, marca, fornecedor, CNPJ, prazo de\n"
        "   entrega, garantia ou qualquer outro dado que não descreva o item em si.\n"
        "4. NÃO invente itens. Se o documento não tiver itens, responda apenas: NENHUM\n"
        "5. NÃO escreva nada além da lista de itens (sem introdução, sem comentários,\n"
        "   sem resumo, sem markdown).\n\n"
        "FORMATO DE SAÍDA (siga exatamente):\n"
        "Item 1: <texto corrido do item 1>\n"
        "Item 2: <texto corrido do item 2>\n"
        "...\n"
        "Item N: <texto corrido do item N>\n\n"
        "EXEMPLO:\n"
        "Item 1: Colete balístico nível IIIA, tamanho G, cor preta, com placa frontal e "
        "traseira\n"
        "Item 2: Viatura policial caminhonete 4x4, motor a diesel, cabine dupla, unidade\n\n"
        "TEXTO DA ATA:\n"
        f"{texto}\n\n"
        "Agora extraia os itens seguindo exatamente o formato acima."
    )


def montar_prompt_comparar_item(descricao_pncp: str, objeto_compra: str, nome_catalogo: str, descricao_catalogo: str) -> str:
    """
    Prompt p/ decidir se um item real de contrato/ata do PNCP é o MESMO tipo de item que um
    item de catálogo (CATMAT/CATSER) que originou a busca. Saída binária (sim/nao), econômica.
    """
    contexto_objeto = f"  Objeto da contratação: {objeto_compra}\n" if objeto_compra else ""
    return (
        "Você compara um item de contrato/ata real do PNCP com um item de catálogo "
        "(CATMAT/CATSER) para decidir se REALMENTE são o mesmo tipo de item.\n\n"
        "Considere equivalentes variações de redação, marca, sigla ou nível de detalhe — "
        "o que importa é se descrevem o MESMO produto/serviço em essência. Um item mais "
        "genérico ou mais específico que o catálogo, mas do mesmo tipo de produto, ainda "
        "conta como match. NÃO é match se for um tipo de item diferente (categoria, "
        "finalidade ou natureza distintas), mesmo que relacionado.\n\n"
        "ITEM DO CONTRATO/ATA (PNCP):\n"
        f"  Descrição do item: {descricao_pncp}\n"
        f"{contexto_objeto}\n"
        "ITEM DO CATÁLOGO (referência que originou a busca):\n"
        f"  Nome: {nome_catalogo}\n"
        f"  Descrição: {descricao_catalogo}\n\n"
        "Responda SOMENTE com uma palavra: sim ou nao.\n"
        "Exemplo: sim"
    )


def montar_prompt_servico(row, com_justificativa: bool = True) -> str:
    nome_classe = row.get("nomeClasse", "") or ""
    nome_subclasse = row.get("nomeSubclasse", "") or ""
    nome_servico = row.get("nomeServico", "") or ""
    return (
        "Você é um classificador de serviços públicos de segurança.\n"
        "Leia o serviço abaixo, analise TODOS os campos (Classe, Subclasse e Serviço) em conjunto,\n"
        "e escolha EXATAMENTE UMA categoria da lista.\n\n"
        "CATEGORIAS:\n\n"
        + _render_categorias(_DEF_SERVICO)
        + "SERVIÇO A CLASSIFICAR:\n"
        f"  Classe: {nome_classe}\n"
        f"  Subclasse: {nome_subclasse}\n"
        f"  Serviço: {nome_servico}\n\n"
        + _instrucao_saida(
            com_justificativa,
            "servico_seguranca|monitoramento eletrônico por câmeras CFTV com central de alarme",
        )
    )


# ══════════════════════════════════════════════════════════════════════════════
# Prompts da pipeline v2 (etapas 1, 3, 5, 6c). Todos pedem JSON puro na saída — o
# `Curador` faz strip de cercas markdown + retry em JSONDecodeError.
# ══════════════════════════════════════════════════════════════════════════════

def _bloco_categorias_classificacao() -> str:
    """Lista id + descrição curta + exemplos por categoria de CONTEÚDO (sem 'outros')."""
    linhas = []
    for cid in _IDS_CONTEUDO:
        m = _META.get(cid, {})
        pos = "; ".join(m.get("exemplos_positivos", []))
        neg = "; ".join(m.get("exemplos_negativos", []))
        linhas.append(
            f"- {cid}: {m.get('descricao_curta', '')}\n"
            f"    SIM: {pos}\n"
            f"    NÃO: {neg}"
        )
    return "\n".join(linhas)


def montar_prompt_classificar_item(descricao: str, unidade: str = "") -> str:
    """
    Etapa 3 — classifica um item real do PNCP em zero, uma ou mais categorias de conteúdo.
    Multi-label permitido; lista vazia = nenhuma (o item morre aqui). Inclui few-shots e
    um caso de armadilha lexical ('PORTARIA ...' → nenhuma).
    """
    ctx_unidade = f"\n  Unidade: {unidade}" if unidade else ""
    return (
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
        f"{_bloco_categorias_classificacao()}\n\n"
        "EXEMPLOS:\n"
        '  \"PISTOLA .40 CAL, ACABAMENTO EM POLÍMERO\" → {\"categorias\": [\"arma_fogo\"], \"confianca\": \"alta\"}\n'
        '  \"COLETE BALÍSTICO NÍVEL III-A\" → {\"categorias\": [\"protecao_balistica\"], \"confianca\": \"alta\"}\n'
        '  \"CARREGADOR AVULSO PARA PISTOLA\" → {\"categorias\": [], \"confianca\": \"alta\"}\n'
        '  \"PORTARIA Nº 123 - AQUISIÇÃO DE COMPUTADORES\" → {\"categorias\": [], \"confianca\": \"alta\"}\n'
        '  \"CADEIRA GIRATÓRIA DE ESCRITÓRIO\" → {\"categorias\": [], \"confianca\": \"alta\"}\n'
        '  \"RÁDIO COMUNICADOR HT DIGITAL VHF\" → {\"categorias\": [\"equip_comunicacao\"], \"confianca\": \"alta\"}\n\n'
        "ITEM A CLASSIFICAR:\n"
        f"  Descrição: {descricao}{ctx_unidade}\n\n"
        'Responda SOMENTE com JSON puro: {"categorias": [...], "confianca": "alta|media|baixa"}'
    )


def montar_prompt_extrair_item_pdf(janela_texto: str, item_api: dict) -> str:
    """
    Etapa 5.2 — extração GUIADA de um único item a partir do texto do PDF. Nunca "liste
    todos"; pede só o item que casa com `item_api` (numeroItem + descricao_api). Reduz
    alucinação e permite validação âncora por preço/quantidade a jusante.
    """
    numero = item_api.get("numeroItem", "")
    descricao_api = item_api.get("descricao_api", "")
    return (
        "Você extrai os dados de UM item específico do texto de um contrato/ata (PDF).\n\n"
        "O item procurado, conforme a API do PNCP:\n"
        f"  Número do item: {numero}\n"
        f"  Descrição (API, pobre): {descricao_api}\n\n"
        "No texto abaixo, localize ESSE item (pelo número e/ou pela descrição) e extraia:\n"
        "  - descricao_completa: a descrição rica do item como aparece no documento "
        "(especificações técnicas, sem valores/marca/fornecedor);\n"
        "  - preco_unitario: valor unitário (número, ponto decimal);\n"
        "  - quantidade: quantidade (número);\n"
        "  - encontrado: true se localizou o item com confiança, false caso contrário.\n\n"
        "NÃO invente. Se não encontrar o item no texto, devolva encontrado=false e os "
        "demais campos nulos. NÃO liste outros itens.\n\n"
        "TEXTO DO DOCUMENTO:\n"
        f"{janela_texto}\n\n"
        'Responda SOMENTE com JSON puro: '
        '{"descricao_completa": "...", "preco_unitario": 0.0, "quantidade": 0, "encontrado": true}'
    )


def montar_prompt_comparar_par(texto_catalogo: str, texto_item: str) -> str:
    """
    Etapa 6c — decide se o item do PNCP e o item de catálogo são o MESMO item para fins
    de pesquisa de preço. Ponto de partida: prompt de comparação da v1, com critério
    explícito de equivalência de marca/modelo.
    """
    return (
        "Você compara um item de catálogo (CATMAT/CATSER) com um item real de contrato/ata "
        "do PNCP para decidir se são o MESMO item para fins de pesquisa de preço.\n\n"
        "CRITÉRIO: mesmo TIPO de item conta como 'sim' — variações de marca, modelo, redação "
        "ou nível de detalhe equivalentes são o mesmo item. Itens de natureza ou finalidade "
        "distinta contam como 'não', mesmo que relacionados (ex.: arma vs. coldre da arma).\n\n"
        "ITEM DO CATÁLOGO (referência):\n"
        f"  {texto_catalogo}\n\n"
        "ITEM DO PNCP (descrição enriquecida):\n"
        f"  {texto_item}\n\n"
        'Responda SOMENTE com JSON puro: {"mesmo_item": "sim|nao", "justificativa": "até 15 palavras"}'
    )


def montar_prompt_termos_item(nome: str, descricao: str, tipo: str = "material",
                              nome_grupo: str = "") -> str:
    """
    Etapa 1 (nova) — gera termos de busca GENÉRICOS direto de UM item do catálogo, para a
    busca por substring do PNCP. O objetivo é achar o MESMO objeto escrito de formas diferentes,
    sem puxar objetos distintos: por isso os termos descrevem só o OBJETO, nunca os atributos.
    """
    contexto = f"  Nome: {nome}\n" if nome.strip() else ""
    if nome_grupo.strip():
        contexto += f"  Grupo: {nome_grupo}\n"
    contexto += f"  Descrição: {descricao}\n"

    if (tipo or "").strip().lower() == "servico":
        alvo = (
            "Este é um item de SERVIÇO (ex.: instalação, manutenção, locação, fornecimento). "
            "Gere termos para a ATIVIDADE-FIM / OBJETO do serviço, IGNORANDO os verbos/ações "
            "(instalação, manutenção, locação, fornecimento, reparo). O termo TEM de ser "
            "ESPECÍFICO, nunca uma palavra guarda-chuva: use 'segurança eletrônica' ou 'cftv' "
            "(não 'vigilância' nem 'monitoramento'), 'concurso público' ou 'processo seletivo' "
            "(não 'recrutamento' nem 'vestibular'), 'controle de acesso' (não 'acesso'). "
            "Serviços da MESMA natureza devem receber o MESMO termo específico."
        )
    else:
        alvo = "Gere termos de busca genéricos que identifiquem o OBJETO PRINCIPAL deste item."

    return (
        alvo + "\n\n"
        "REGRAS:\n"
        "- Cada termo deve ter 1 palavra (no MÁXIMO 2, só quando 1 não identifica o objeto).\n"
        "- Use o substantivo genérico e formas equivalentes do MESMO objeto: sinônimos e "
        "variações de grafia comuns em editais (ex.: pick-up, pickup, picape).\n"
        "- NÃO inclua atributos (motor, diesel, direção, potência, cor, calibre, capacidade, "
        "quantidade de portas, ar-condicionado, voltagem) — eles puxam objetos DIFERENTES.\n"
        "- NÃO use o NOME DA CATEGORIA/GRUPO nem palavras guarda-chuva (armamento, aeronave, "
        "munição, vestuário, aerosol) — um item real é 'pistola', não 'armamento'; é 'drone', "
        "não 'aeronave'. Dê sempre o OBJETO concreto.\n"
        "- NÃO use palavras abstratas nem de ação (pessoa, pessoal, profissional, avaliação, "
        "exame, prova, instalação, manutenção, serviço) — casam com qualquer coisa na busca.\n"
        "- CUIDADO com palavras que também significam algo COMUM em contratos públicos de outra "
        "natureza: NUNCA use sozinhos 'servidor' (=funcionário público), 'cartucho' (=tinta/"
        "toner), 'monitor', 'passageiro', 'vigilância', 'esfera', 'colete'. Qualifique "
        "('servidor de rede', 'colete balístico') ou escolha outro termo.\n"
        "- Prefira o termo QUALIFICADO à palavra crua ambígua: 'câmera fotográfica' (não "
        "'câmera'), 'disco rígido' (não 'disco'), 'bastão retrátil' (não 'bastão'). Evite "
        "abreviações curtas (pc, hd, ht) e palavras que sozinhas designam muitos objetos "
        "(capa, disco, spray, câmera, leitor, servidor, cartucho) — sempre com qualificador.\n"
        "- Use português do BRASIL, NUNCA de Portugal: 'ônibus' (não 'autocarro'), 'celular' "
        "(não 'telemóvel'), 'caminhonete' (não 'camioneta'/'furgoneta'), 'furgão' (não "
        "'furgoneta'/'furgonete'), 'bicicleta' (não 'biciclo'), 'scooter' (não 'scuter'), "
        "'minivan' (não 'monovolume').\n"
        "- Prefira o mais GENÉRICO possível DENTRO do objeto. 3 a 8 termos. Minúsculas, "
        "com acentuação natural.\n\n"
        "EXEMPLOS:\n"
        '  "VEÍCULO PICK-UP, MOTOR DIESEL, DIREÇÃO HIDRÁULICA, CABINE DUPLA, 4 PORTAS, '
        'AR-CONDICIONADO" →\n'
        '     {"termos": ["veículo", "automóvel", "carro", "pickup", "pick-up", "picape", '
        '"cabine dupla"]}\n'
        '  "PISTOLA .40, POLÍMERO, 15 TIROS, COR PRETA" →\n'
        '     {"termos": ["pistola", "arma de fogo"]}\n'
        '  "RÁDIO TRANSCEPTOR PORTÁTIL VHF DIGITAL 5W" →\n'
        '     {"termos": ["rádio", "transceptor", "rádio comunicador"]}\n'
        '  "PRESTAÇÃO DE SERVIÇO DE VIGILÂNCIA E SEGURANÇA - ELETRÔNICA" →\n'
        '     {"termos": ["segurança eletrônica", "cftv", "monitoramento eletrônico"]}\n\n'
        "ITEM:\n" + contexto + "\n"
        'Responda SOMENTE com JSON puro: {"termos": ["termo1", "termo2", ...]}'
    )


# --------------------------------------------------------------------------------------
# Etapa 5-ALT — extração da tabela de itens direto do PDF (visão) + casamento estruturado.
# Alternativa ao par 5a(OCR)+5b(janela de texto): 5_alt_a manda a IMAGEM da página a um
# modelo de visão e recebe a tabela "as it is"; 5_alt_b casa o item da API contra essa
# tabela LIMPA (JSON estruturado), sem texto corrido em volta — muito menos alucinação.
# --------------------------------------------------------------------------------------

def montar_prompt_extrair_tabela_pdf() -> str:
    """
    Etapa 5_alt_a — instrução (texto) que acompanha a IMAGEM de UMA página de PDF enviada
    a um modelo de visão. Pede a tabela de itens/preços EXATAMENTE como está na página,
    sem normalizar nem inventar. Cada documento tem um layout diferente: transcreva o que
    houver, não force um formato de origem.
    """
    return (
        "Você recebe a IMAGEM de uma página de uma Ata de Registro de Preços / contrato "
        "público (PDF). Extraia a TABELA DE ITENS desta página EXATAMENTE como aparece — "
        "transcreva, não interprete nem normalize.\n\n"
        "REGRAS:\n"
        "- Cada linha da tabela de itens vira um objeto. Só itens de produto/serviço com "
        "preço; ignore cabeçalhos, rodapés, cláusulas, assinaturas e texto corrido.\n"
        "- Copie os NÚMEROS como estão na página (ex.: '1.234,56' fica '1.234,56'); NÃO "
        "converta separador decimal, NÃO arredonde, NÃO complete casas.\n"
        "- Copie a descrição do item integralmente, como escrita (especificações inclusas).\n"
        "- Se um campo não existir na página, use string vazia. NÃO invente valores.\n"
        "- Se a página NÃO tiver tabela de itens, devolva lista vazia e tem_tabela=false.\n"
        "- A tabela pode ser uma IMAGEM embutida na página — leia-a mesmo assim.\n\n"
        'Responda SOMENTE com JSON puro:\n'
        '{"tem_tabela": true, "itens": [{"numero_item": "", "descricao": "", '
        '"unidade": "", "quantidade": "", "preco_unitario": "", "preco_total": "", '
        '"fornecedor": ""}]}'
    )


def montar_prompt_extrair_tabela_texto(texto: str) -> str:
    """
    Etapa 5 — estratégia `completa` (Fase 8, ADR-010): extrai a tabela de itens de UM CHUNK
    de texto já parseado do documento (nativo/OCR), em vez da imagem da página (essa é a
    `visao`). Mesmo contrato de saída de `montar_prompt_extrair_tabela_pdf`, para que
    `casar_item_tabela` sirva às duas estratégias sem distinção.
    """
    return (
        "Você recebe um TRECHO do texto de um contrato/ata de registro de preços (PDF já "
        "convertido para texto). Extraia a TABELA DE ITENS deste trecho EXATAMENTE como "
        "aparece — transcreva, não interprete nem normalize.\n\n"
        "REGRAS:\n"
        "- Cada item/linha da tabela vira um objeto. Só itens de produto/serviço com preço; "
        "ignore cláusulas, assinaturas e texto corrido que não seja tabela/lista de itens.\n"
        "- Copie os NÚMEROS como estão no texto (ex.: '1.234,56' fica '1.234,56'); NÃO "
        "converta separador decimal, NÃO arredonde, NÃO complete casas.\n"
        "- Copie a descrição do item integralmente, como escrita (especificações inclusas).\n"
        "- Se um campo não existir no trecho, use string vazia. NÃO invente valores.\n"
        "- Se este trecho NÃO tiver tabela/lista de itens, devolva lista vazia.\n"
        "- Este é só um PEDAÇO do documento — pode não ter todos os itens; não invente os "
        "que faltam.\n\n"
        "TRECHO DO DOCUMENTO:\n"
        f"{texto}\n\n"
        'Responda SOMENTE com JSON puro:\n'
        '{"itens": [{"numero_item": "", "descricao": "", "unidade": "", "quantidade": "", '
        '"preco_unitario": "", "preco_total": "", "fornecedor": ""}]}'
    )


def montar_prompt_casar_item_tabela(item_api: dict, linhas: list[dict]) -> str:
    """
    Etapa 5_alt_b — casa UM item da API do PNCP contra a tabela LIMPA extraída do PDF
    (lista de linhas estruturadas de 5_alt_a). Como a entrada é estruturada e curta, o
    modelo escolhe a linha certa (ou nenhuma) com pouca margem para alucinar.
    """
    numero = item_api.get("numeroItem", "")
    descricao_api = item_api.get("descricao_api", "")
    linhas_fmt = json.dumps(
        [{"idx": i, **{k: l.get(k, "") for k in
                        ("numero_item", "descricao", "unidade", "quantidade",
                         "preco_unitario", "preco_total", "fornecedor")}}
         for i, l in enumerate(linhas)],
        ensure_ascii=False,
    )
    return (
        "Você recebe UM item da API do PNCP e a TABELA de itens extraída do PDF da ata "
        "(linhas já estruturadas). Diga QUAL linha da tabela é o mesmo item — casando "
        "por número do item e/ou descrição — ou que não há correspondência.\n\n"
        "ITEM DA API (referência, descrição pobre):\n"
        f"  Número do item: {numero}\n"
        f"  Descrição: {descricao_api}\n\n"
        "TABELA DO PDF (uma linha por objeto, campo 'idx' identifica a linha):\n"
        f"{linhas_fmt}\n\n"
        "REGRAS:\n"
        "- Case pelo SENTIDO do objeto, não pela grafia. O número do item ajuda, mas a "
        "descrição manda: se o número aponta uma linha de objeto claramente diferente, "
        "confie na descrição.\n"
        "- COPIE preco_unitario e quantidade da LINHA escolhida, exatamente como estão.\n"
        "- Se NENHUMA linha for o mesmo item, encontrado=false e idx=-1.\n\n"
        'Responda SOMENTE com JSON puro: {"encontrado": true, "idx": 0, '
        '"descricao_completa": "...", "preco_unitario": "", "quantidade": ""}'
    )
