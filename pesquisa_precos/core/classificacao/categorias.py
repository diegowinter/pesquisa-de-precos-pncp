"""
Categorias da curadoria, como DADOS (separadas do texto do prompt em prompts.py).

Cada categoria é um dict {"id", "regra"}:
    - id    → identificador em minúsculas/ascii (o que o modelo deve responder e o que
            vai para a coluna `categoria`).
    - regra → o texto de inclusão/exclusão daquela categoria (2 espaços de indentação),
            renderizado logo abaixo do id no prompt.

Para ADICIONAR uma categoria nova: basta acrescentar um item na lista correspondente.
O `CATEGORIA_MAP_*` e o conjunto de ids são derivados automaticamente em prompts.py.
Mantenha "outros" sempre por ÚLTIMO (é o fallback).
"""

CATEGORIAS_MATERIAL = [
    {
        "id": "arma_fogo",
        "regra": """
    Use SOMENTE para a arma de fogo propriamente dita: pistola, revólver, fuzil,
    espingarda, carabina, submetralhadora, rifle, em qualquer calibre. Uma arma
    de fogo que dispara munição não letal continua sendo arma_fogo.
    Não use para cano, ferrolho, gatilho, carregador (magazine) avulso, mira,
    red dot, luneta, coldre, cinto tático, porta-carregador, estojo de limpeza,
    alvo, kit de manutenção, réplica, airsoft, simulacro. Tudo isso é "outros".
""",
    },
    {
        "id": "arma_nao_letal",
        "regra": """
    Use SOMENTE para um destes itens completos e funcionais: dispositivo elétrico
    incapacitante (taser), spray de pimenta (OC), spray lacrimogêneo (CS), bastão
    retrátil, tonfa policial, granada de efeito moral (luz e som / stun grenade).
    Não use para granadas explosivas ou letais, facas, algemas, escudos
    anti-tumulto não balísticos, recargas e cartuchos de spray, baterias e
    cartuchos de taser, peças ou acessórios destes equipamentos. Tudo isso é
    "outros". Qualquer outro dispositivo não letal fora desta lista também é "outros".
""",
    },
    {
        "id": "municao",
        "regra": """
    Use SOMENTE para munição completa de arma de fogo em qualquer calibre (9mm,
    .38, .40, .380, 5.56mm, 7.62mm, .308, .50, calibre 12, etc.), munição de
    elastômero (bala de borracha) e cartucho 40mm de gás lacrimogêneo ou impacto
    controlado.
    Não use para espoleta avulsa, estojo vazio, projétil avulso, pólvora avulsa,
    chumbinho ou BB para arma de pressão, alvos, caixas de munição vazias ou
    qualquer componente isolado. Tudo isso é "outros".
""",
    },
    {
        "id": "protecao_balistica",
        "regra": """
    Use para colete, escudo ou capacete de uso operacional em segurança pública ou defesa.
    Para COLETE ou ESCUDO é OBRIGATÓRIO haver menção explícita a balística (balístico, à
    prova de bala, antibala, blindado, ou nível NIJ/NBR II, IIIA, III, IV); sem essa menção,
    colete tático vai para vestuario_operacional e os demais para "outros".
    CAPACETE operacional (militar, policial, tático, antitumulto, balístico) entra SEMPRE
    aqui, com ou sem menção balística.
    Não use para capacete de obra, de motocicleta, de bombeiro civil, esportivo ou de
    bicicleta — isso é "outros". Capa/porta-colete vai para vestuario_operacional. Placa
    balística avulsa, painel balístico avulso e demais componentes são "outros" (peças).
""",
    },
    {
        "id": "equip_ti",
        "regra": """
    Use SOMENTE para o equipamento completo: computador desktop, workstation,
    notebook, laptop, tablet, impressora, scanner de documentos, servidor de rede,
    storage, NAS, câmera fotográfica digital profissional, filmadora digital.
    Não use para câmera de segurança ou CFTV, monitor avulso, projetor, teclado,
    mouse, toner, cartucho de tinta, papel, cabo, fonte, HD ou SSD avulso,
    memória RAM, placa, peça ou qualquer suprimento ou periférico. Tudo isso é
    "outros".
""",
    },
    {
        "id": "equip_comunicacao",
        "regra": """
    Use SOMENTE para o aparelho completo: rádio comunicador portátil (HT,
    walkie-talkie), rádio veicular, rádio base fixo (analógico ou digital),
    smartphone corporativo para comunicação operacional.
    Não use para bateria, carregador, fonte, antena avulsa, capa, fone, microfone,
    headset, PTT, cabo, suporte veicular, clip de cinto, ou qualquer acessório
    ou peça. Tudo isso é "outros". Telefone fixo convencional, central PABX,
    repetidor e estação repetidora também são "outros".
""",
    },
    {
        "id": "viatura",
        "regra": """
    Use SOMENTE para o veículo motorizado completo: sedan, SUV, picape, hatchback,
    motocicleta, van, micro-ônibus, veículo especial (comando móvel, cela,
    resgate, unidade móvel).
    Bicicleta NÃO é viatura, vai para a categoria bicicleta.
    Não use para pneu, peça, óleo, lubrificante, película, giroflex avulso,
    sirene avulsa, kit de caracterização, rádio veicular, acessório, equipamento
    embarcado ou qualquer componente. Tudo isso é "outros".
""",
    },
    {
        "id": "bicicleta",
        "regra": """
    Use SOMENTE para a bicicleta inteira destinada a uso operacional de segurança
    pública (ciclopatrulha, patrulhamento, ronda, guarda municipal, polícia),
    incluindo bicicleta elétrica para essa finalidade.
    Não use para bicicleta recreativa, ergométrica ou de uso civil genérico. Isso
    é "outros". Capacete, pneu, câmara, peça, suporte, bagageiro ou qualquer
    acessório também são "outros".
""",
    },
    {
        "id": "drone_rpas",
        "regra": """
    Use SOMENTE para o drone completo (multirrotor, asa fixa, com câmera térmica,
    4K ou zoom, com alto-falante ou holofote), sistema completo de drone ou
    estação de controle terrestre.
    Não use para aeromodelo recreativo, hélice avulsa, bateria avulsa, controle
    remoto avulso, gimbal, câmera avulsa, software isolado, peça ou qualquer
    componente. Tudo isso é "outros".
""",
    },
    {
        "id": "vestuario_operacional",
        "regra": """
    Use para vestuário e equipamento individual têxtil de uso operacional: capa ou
    porta-colete, colete tático SEM menção balística, farda/fardamento operacional,
    gandola, calça tática, coturno ou bota tática, luva tática, cinto de guarnição.
    Não use para colete balístico (é protecao_balistica), capacete (é capacete),
    uniforme social/administrativo, vestuário civil, insígnia, distintivo, brasão,
    bordado ou emblema avulso. Tudo isso é "outros".
""",
    },
    {
        "id": "outros",
        "regra": """
    Use para qualquer item que não se encaixe estritamente nas categorias acima.
    Inclui, por exemplo: acessórios em geral, peças, partes, componentes,
    suprimentos, baterias, carregadores, cabos, fontes, antenas avulsas, coldres,
    cintos, miras, lanternas táticas, ferramentas, mobiliário, material de
    escritório, fardamento e vestuário sem proteção balística, coturnos, algemas,
    escudos não balísticos, equipamentos médicos e hospitalares, câmeras de
    segurança e CFTV, monitores, projetores, telefones fixos, e qualquer outro
    item fora do escopo. EM CASO DE DÚVIDA, ESCOLHA "outros".
""",
    },
]

CATEGORIAS_SERVICO = [
    {
        "id": "servico_selecao",
        "regra": """
    Use para: organização e realização de concurso público (logística, elaboração de
    provas, aplicação, correção, divulgação de resultados), aplicação de teste de
    aptidão física (TAF), avaliação psicológica ou exame psicotécnico vinculado a
    processo seletivo.
    Não use para cursos de capacitação, treinamentos internos, consultoria de RH,
    perícia médica admissional nem exames médicos ocupacionais — classifique como outros.
""",
    },
    {
        "id": "servico_seguranca",
        "regra": """
    Use para: vigilância eletrônica (CFTV, câmeras de segurança, sensores), controle
    de acesso eletrônico (catracas, biometria, cartões de proximidade, cancelas
    automatizadas), monitoramento eletrônico (central de alarme, cerca elétrica),
    locação de sistema de CFTV com monitoramento incluso.
    Não use para vigilância humana presencial (vigilante com posto fixo), segurança
    armada ou desarmada presencial, escolta armada, transporte de valores, brigada
    de incêndio nem portaria ou recepção — classifique como outros.
    Não use para serviço de instalação ou manutenção de câmeras e alarmes
    — classifique como outros.
    Se o serviço combina vigilância humana e eletrônica, identifique qual é o
    componente principal; se não for claro, classifique como outros.
""",
    },
    {
        "id": "testes_proficiencia",
        "regra": """
    Use para serviços de aplicação de TESTES e AVALIAÇÕES de proficiência, aptidão ou
    capacidade (psicológica, psicotécnica, educacional, cognitiva), inclusive teste de
    aptidão física (TAF), quando contratados de forma avulsa (não a organização de um
    concurso público inteiro).
    Não use para a organização completa de concurso público (isso é servico_selecao),
    cursos de capacitação, treinamentos internos nem exames médicos ocupacionais — outros.
""",
    },
    {
        "id": "manutencao_controle_acesso",
        "regra": """
    Use para serviços de INSTALAÇÃO, MANUTENÇÃO ou locação de sistemas e equipamentos de
    CONTROLE DE ACESSO físico de pessoas: catracas, cancelas, torniquetes, leitores
    biométricos, fechaduras eletrônicas, portões e portas automatizados.
    Não use para monitoramento por CFTV/vigilância eletrônica (isso é servico_seguranca),
    controle de acesso lógico de TI/rede, nem para o fornecimento do equipamento sem o
    serviço associado — isso é outros.
""",
    },
    {
        "id": "outros",
        "regra": """
    Use para qualquer serviço que não se encaixe nas categorias acima: limpeza,
    manutenção predial, TI, treinamentos, transporte, consultoria, vigilância
    humana presencial, ou qualquer outro serviço.
    Em caso de dúvida, escolha outros.
""",
    },
]


# ── Metadados p/ a classificação por item PNCP (etapa 3) ────────────────────────
# Descrição curta + exemplos positivos/negativos por id de categoria. Usados para
# montar o prompt de classificação com few-shots (prompts.montar_prompt_classificar_item).
# Os negativos incluem armadilhas lexicais conhecidas (ex.: "porta"/"portaria").
META_CATEGORIAS = {
    "arma_fogo": {
        "descricao_curta": "Arma de fogo completa (pistola, revólver, fuzil, espingarda, carabina).",
        "exemplos_positivos": ["PISTOLA .40 GLOCK G22", "FUZIL 5.56MM"],
        "exemplos_negativos": ["CARREGADOR DE PISTOLA (peça)", "COLDRE PARA PISTOLA (acessório)"],
    },
    "arma_nao_letal": {
        "descricao_curta": "Arma não letal completa (taser, spray OC/CS, bastão/tonfa, granada de efeito moral).",
        "exemplos_positivos": ["ARMA DE CHOQUE TIPO TASER X26", "SPRAY DE PIMENTA OC 70G"],
        "exemplos_negativos": ["CARTUCHO DE TASER (recarga)", "ALGEMA DE AÇO"],
    },
    "municao": {
        "descricao_curta": "Munição completa de arma de fogo (qualquer calibre), bala de borracha, cartucho 40mm.",
        "exemplos_positivos": ["MUNIÇÃO 9MM EOPE", "CARTUCHO CALIBRE 12"],
        "exemplos_negativos": ["ESTOJO VAZIO CALIBRE .40", "CHUMBINHO 4.5MM (arma de pressão)"],
    },
    "protecao_balistica": {
        "descricao_curta": "Colete/escudo balístico (menção NIJ/NBR ou 'balístico') ou capacete operacional completo.",
        "exemplos_positivos": ["COLETE BALÍSTICO NÍVEL IIIA", "CAPACETE POLICIAL ANTITUMULTO"],
        "exemplos_negativos": ["COLETE TÁTICO (sem menção balística)", "CAPACETE DE MOTOCICLISTA"],
    },
    "vestuario_operacional": {
        "descricao_curta": "Vestuário/equipamento individual têxtil operacional (capa de colete, farda tática, coturno, cinto de guarnição).",
        "exemplos_positivos": ["CAPA PARA COLETE BALÍSTICO", "COTURNO TÁTICO MILITAR"],
        "exemplos_negativos": ["UNIFORME SOCIAL / ADMINISTRATIVO", "INSÍGNIA BORDADA (peça)"],
    },
    "equip_ti": {
        "descricao_curta": "Equipamento de TI completo (desktop, notebook, tablet, impressora, scanner, servidor).",
        "exemplos_positivos": ["NOTEBOOK CORE I7 16GB", "IMPRESSORA MULTIFUNCIONAL LASER"],
        "exemplos_negativos": ["TONER PARA IMPRESSORA", "MONITOR LED 24 (periférico avulso)"],
    },
    "equip_comunicacao": {
        "descricao_curta": "Aparelho de comunicação completo (rádio HT portátil, rádio veicular, rádio base).",
        "exemplos_positivos": ["RÁDIO COMUNICADOR HT DIGITAL", "RÁDIO VEICULAR VHF"],
        "exemplos_negativos": ["BATERIA PARA RÁDIO HT", "ANTENA VEICULAR (acessório)"],
    },
    "viatura": {
        "descricao_curta": "Veículo motorizado completo (sedan, SUV, picape, moto, van, veículo especial).",
        "exemplos_positivos": ["CAMINHONETE 4X4 CABINE DUPLA DIESEL", "MOTOCICLETA 300CC POLICIAL"],
        "exemplos_negativos": ["PNEU PARA VIATURA", "GIROFLEX / SIRENE AVULSA"],
    },
    "bicicleta": {
        "descricao_curta": "Bicicleta inteira para uso operacional (ciclopatrulha), incl. elétrica.",
        "exemplos_positivos": ["BICICLETA PARA CICLOPATRULHA ARO 29", "BICICLETA ELÉTRICA GUARDA MUNICIPAL"],
        "exemplos_negativos": ["CAPACETE DE CICLISTA", "BICICLETA ERGOMÉTRICA (academia)"],
    },
    "drone_rpas": {
        "descricao_curta": "Drone/RPAS completo (multirrotor ou asa fixa, com câmera), ou estação de controle.",
        "exemplos_positivos": ["DRONE COM CÂMERA TÉRMICA 4K", "RPAS MULTIRROTOR PROFISSIONAL"],
        "exemplos_negativos": ["BATERIA AVULSA PARA DRONE", "HÉLICE DE REPOSIÇÃO"],
    },
    "servico_selecao": {
        "descricao_curta": "Concurso público, TAF, avaliação psicológica vinculada a processo seletivo.",
        "exemplos_positivos": ["ORGANIZAÇÃO DE CONCURSO PÚBLICO", "APLICAÇÃO DE TESTE DE APTIDÃO FÍSICA"],
        "exemplos_negativos": ["CURSO DE CAPACITAÇÃO", "EXAME MÉDICO OCUPACIONAL"],
    },
    "servico_seguranca": {
        "descricao_curta": "Vigilância/monitoramento eletrônico (CFTV, controle de acesso, central de alarme).",
        "exemplos_positivos": ["MONITORAMENTO ELETRÔNICO POR CFTV", "CONTROLE DE ACESSO BIOMÉTRICO"],
        "exemplos_negativos": ["VIGILÂNCIA ARMADA PRESENCIAL", "INSTALAÇÃO DE CÂMERAS (serviço de instalação)"],
    },
    "testes_proficiencia": {
        "descricao_curta": "Aplicação avulsa de testes/avaliações de proficiência, aptidão ou capacidade (psico/educacional/TAF).",
        "exemplos_positivos": ["TESTE DE PROFICIÊNCIA PSICOLÓGICA", "AVALIAÇÃO DE CAPACIDADE FÍSICA (TAF)"],
        "exemplos_negativos": ["ORGANIZAÇÃO DE CONCURSO PÚBLICO (servico_selecao)", "CURSO DE CAPACITAÇÃO"],
    },
    "manutencao_controle_acesso": {
        "descricao_curta": "Instalação/manutenção/locação de controle de acesso físico (catraca, cancela, biometria, fechadura eletrônica).",
        "exemplos_positivos": ["MANUTENÇÃO DE CATRACA E CONTROLE DE ACESSO", "INSTALAÇÃO DE LEITOR BIOMÉTRICO DE ACESSO"],
        "exemplos_negativos": ["MONITORAMENTO CFTV (servico_seguranca)", "FORNECIMENTO DE CATRACA (sem serviço)"],
    },
    "outros": {
        "descricao_curta": "Qualquer item fora das categorias acima (peças, acessórios, suprimentos, itens genéricos).",
        "exemplos_positivos": ["PORTARIA Nº 123 - AQUISIÇÃO DE COMPUTADORES (documento, não item)", "CADEIRA DE ESCRITÓRIO"],
        "exemplos_negativos": [],
    },
}

# Ids válidos de categoria de conteúdo (exclui 'outros', que é descarte/fallback).
IDS_MATERIAL = [c["id"] for c in CATEGORIAS_MATERIAL if c["id"] != "outros"]
IDS_SERVICO = [c["id"] for c in CATEGORIAS_SERVICO if c["id"] != "outros"]
IDS_CONTEUDO = IDS_MATERIAL + IDS_SERVICO
