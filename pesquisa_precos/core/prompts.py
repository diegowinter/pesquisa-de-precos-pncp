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

from pesquisa_precos.core.classification.categories import CATEGORIAS_MATERIAL as _DEF_MATERIAL
from pesquisa_precos.core.classification.categories import CATEGORIAS_SERVICO as _DEF_SERVICO
from pesquisa_precos.core.classification.categories import META_CATEGORIAS as _META
from pesquisa_precos.core.classification.categories import IDS_CONTEUDO as _IDS_CONTEUDO

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
            "service_seguranca|monitoramento eletrônico por câmeras CFTV com central de alarme",
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
# Etapa 5 (ADR-023) — duas chamadas, uma por documento e uma por item.
#
# A primeira recebe o PDF INTEIRO como anexo e devolve a tabela de itens em TEXTO, "as it
# is": cada documento tem as colunas que tem — uns trazem fornecedor e marca, outros só
# descrição/quantidade/preço. Impor um esquema fixo aqui era exatamente o que fazia o modelo
# inventar coluna vazia. A segunda casa UM item da API contra esse texto, que é curto e já
# está limpo — bem menos margem para alucinar do que o documento cru.
# --------------------------------------------------------------------------------------

def montar_prompt_extrair_tabela_documento() -> str:
    """Instrução que acompanha o PDF anexo. Sem placeholder: o documento não vai no texto."""
    return (
        "Observe esse documento (contrato ou ata de registro de preços) e retorne a TABELA "
        "DE ITENS, com as informações que estiverem nela. APENAS A TABELA DE ITENS.\n\n"
        "REGRAS:\n"
        "- Transcreva a tabela COMO ELA É. Use as colunas que o documento tiver — se houver "
        "fornecedor, marca ou modelo, traga; se não houver, NÃO crie a coluna.\n"
        "- NÃO invente nenhum valor, item ou coluna. Nada além do que está no documento.\n"
        "- Copie os NÚMEROS exatamente como aparecem ('1.234,56' continua '1.234,56'): não "
        "converta separador decimal, não arredonde, não complete casas.\n"
        "- Copie a descrição de cada item integralmente, com as especificações técnicas.\n"
        "- Traga TODOS os itens do documento, inclusive os de páginas seguintes.\n"
        "- Ignore cláusulas, preâmbulo, assinaturas, rodapés e texto corrido.\n"
        "- A tabela pode estar como imagem digitalizada — leia-a mesmo assim.\n"
        "- Se o documento NÃO tiver tabela de itens, responda exatamente: SEM_TABELA\n\n"
        "Responda com a tabela em markdown e mais nada — sem introdução, sem comentário, "
        "sem resumo."
    )


def montar_prompt_casar_item_tabela(item_api: dict, tabela_texto: str) -> str:
    """Casa UM item da API do PNCP contra a tabela já extraída do documento.

    A entrada é curta e só contém itens: a chamada não vê o documento inteiro, que é o
    ponto do desenho de duas passadas.
    """
    numero = item_api.get("numeroItem", "")
    descricao_api = item_api.get("descricao_api", "")
    return (
        "Você recebe UM item da API do PNCP e a TABELA DE ITENS extraída do documento da "
        "ata/contrato. Diga qual linha da tabela é o MESMO item — casando por número do "
        "item e/ou descrição — ou que não há correspondência.\n\n"
        "ITEM DA API (referência, descrição pobre):\n"
        f"  Número do item: {numero}\n"
        f"  Descrição: {descricao_api}\n\n"
        "TABELA DO DOCUMENTO:\n"
        f"{tabela_texto}\n\n"
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
        'Responda SOMENTE com JSON puro: {"encontrado": true, "descricao_completa": "...", '
        '"preco_unitario": "", "quantidade": "", "fornecedor": ""}'
    )
