"""
Variações de grafia (etapa 1), definidas À MÃO e aplicadas DEPOIS do LLM.

Motivação: a busca do PNCP é por SUBSTRING. O mesmo objeto aparece nos editais com grafias
diferentes (pick-up / pickup / picape), com e sem hífen, com e sem acento, e com abreviações
(rádio / ht / transceptor). O LLM gera os termos "canônicos"; aqui garantimos, de forma
determinística e editável, que as variantes conhecidas entrem junto — maximizando recall.

Duas peças:
  - GRUPOS_VARIACAO: grupos de formas EQUIVALENTES do MESMO objeto. Se qualquer forma do grupo
    aparecer nos termos de um item, todas as formas do grupo são adicionadas.
  - categoria_por_grupo(nome_grupo): fallback GROSSEIRO nome_grupo(PNCP) → categoria (IDS_CONTEUDO),
    usado só quando o item ficou sem categoria e a maioria do PDM também está vazia.

A comparação é insensível a caixa e acento; a expansão preserva as formas como escritas aqui.
Nota: a duplicação acento/sem-acento genérica é feita no chamador (etapa 1) para TODO termo;
aqui ficam só as variações que não se obtêm apenas tirando acento (hífen, espaço, sinônimo de
grafia, abreviação).
"""

import unicodedata


def _norm(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", (s or "").strip().lower())
    sem_acento = "".join(c for c in nfkd if not unicodedata.combining(c))
    return " ".join(sem_acento.split())  # colapsa espaços


# Cada grupo = formas equivalentes do MESMO objeto (não sinônimos de objetos diferentes!).
GRUPOS_VARIACAO: list[set[str]] = [
    # ── veículos ────────────────────────────────────────────────────────────────
    {"pick-up", "pickup", "pick up", "picape", "caminhonete", "camionete"},
    {"micro-ônibus", "microônibus", "micro ônibus", "micro-onibus", "microonibus"},
    {"furgão", "furgao", "van"},
    {"automóvel", "automovel", "carro", "veículo", "veiculo"},
    {"motocicleta", "moto"},
    {"utilitário", "utilitario"},
    # ── armamento / não-letal ────────────────────────────────────────────────────
    {"revólver", "revolver"},
    {"espargidor", "spray de pimenta", "spray oc", "gás de pimenta", "gas de pimenta"},
    {"bastão retrátil", "bastao retratil", "tonfa", "cassetete"},
    {"aparelho de choque", "arma de choque", "taser", "dispositivo elétrico incapacitante"},
    # ── proteção ─────────────────────────────────────────────────────────────────
    {"colete balístico", "colete balistico", "colete à prova de bala", "colete a prova de bala",
     "colete à prova de tiro", "colete a prova de tiro", "colete antibala"},
    {"capacete balístico", "capacete balistico", "capacete à prova de bala", "capacete a prova de bala"},
    # ── comunicação ──────────────────────────────────────────────────────────────
    {"rádio", "radio", "rádio comunicador", "radio comunicador", "transceptor", "ht", "rádio ht", "radio ht"},
    {"celular", "smartphone", "telefone celular", "aparelho celular"},
    # ── TI ───────────────────────────────────────────────────────────────────────
    {"notebook", "laptop"},
    {"microcomputador", "computador", "desktop"},
    {"impressora multifuncional", "multifuncional"},
    {"disco rígido", "disco rigido", "hd", "hdd"},
    {"disco magnético", "disco magnetico"},
    # ── aéreo ────────────────────────────────────────────────────────────────────
    {"drone", "rpas", "vant", "veículo aéreo não tripulado", "veiculo aereo nao tripulado",
     "veículo teleguiado", "veiculo teleguiado"},
]

# Índice de busca: forma-normalizada → índice do grupo (montado uma vez).
_INDICE: dict[str, int] = {}
for _i, _g in enumerate(GRUPOS_VARIACAO):
    for _forma in _g:
        _INDICE[_norm(_forma)] = _i


# Termos vazios demais: identificam objetos de QUALQUER categoria (recall inútil, só ruído).
# Note que palavras como "veiculo" NÃO entram: são genéricas mas dentro de UMA categoria
# coerente (viatura), logo úteis. Aqui só ficam as verdadeiramente transversais.
TERMOS_GENERICOS: set[str] = {
    # objeto genérico transversal
    "equipamento", "aparelho", "dispositivo", "material", "sistema", "produto",
    "item", "unidade", "instrumento", "artefato", "maquina", "ferramenta",
    "acessorio", "componente", "conjunto", "kit", "mercadoria", "bem", "objeto",
    "utensilio", "peca", "modelo", "tipo",
    # nome de CATEGORIA/GRUPO (nunca vem sozinho num item real: é "pistola", não "armamento")
    "armamento", "aeronave", "aerosol", "explosivo", "explosivos", "municoes",
    "vestuario", "insignia", "insignias",
    # abstratos de serviço (casam com qualquer coisa)
    "pessoa", "pessoal", "profissional", "candidato", "avaliacao", "exame",
    "teste", "prova", "instalacao", "manutencao", "servico", "suporte",
    # substring perigosa (palavra curta/ambígua que casa com item não relacionado)
    "coletivo", "patrimonio", "seta", "seda", "choque", "eletrochoque",
    # "camara"/"câmara" é pt-PT p/ câmera, mas casa com "câmara dos vereadores/municipal/fria".
    "camara",
    # "camera"/"câmera" sozinho é polissêmico (webcam, câmera de notebook/celular, CFTV) e
    # estoura a janela de 10k. Os termos qualificados (câmera fotográfica/digital/vídeo,
    # filmadora, máquina fotográfica) ficam — só a palavra crua sai.
    "camera",
    # abreviações curtas: substring ubíqua (hd=high definition, pc=qualquer coisa, micro=…).
    "pc", "hd", "ht", "micro",
    # palavra crua polissêmica: o item fica coberto pelo termo QUALIFICADO
    # (capa colete, bastao retratil, disco rigido, spray de pimenta…).
    "capa", "bastao", "disco", "drive", "spray",
    # palavra comum em contrato público de OUTRA natureza (busca é por token → inunda):
    #   nas=preposição · servidor=funcionário público · cartucho=tinta/toner ·
    #   monitor=monitoria · esfera=esfera administrativa · passageiro=transporte ·
    #   vigilancia/monitoramento=vigilância patrimonial · recrutamento/vestibular=RH.
    "nas", "servidor", "cartucho", "monitor", "esfera", "esferas", "esferoide",
    "passageiro", "colete", "automotriz", "carroca",
    "vigilancia", "monitoramento", "recrutamento", "vestibular",
    # português de Portugal (não casa com editais BR) — o prompt já evita; aqui é a rede.
    "autocarro", "biciclo", "camioneta", "furgoneta", "furgonete", "monovolume", "scuter",
}


def e_generico(termo: str) -> bool:
    """True se o termo é uma palavra genérica transversal (deve ser descartado da busca)."""
    return _norm(termo) in TERMOS_GENERICOS


def expandir_variacoes(termos: set[str]) -> set[str]:
    """Se qualquer forma de um grupo aparecer em `termos`, adiciona todas as formas do grupo."""
    resultado = set(termos)
    grupos_ativos = {_INDICE[_norm(t)] for t in termos if _norm(t) in _INDICE}
    for i in grupos_ativos:
        resultado |= GRUPOS_VARIACAO[i]
    return resultado


# ── Fallback grosseiro: nome_grupo (PNCP) → categoria (IDS_CONTEUDO) ─────────────
# Só entra quando o item e todo o seu PDM ficaram sem categoria do LLM. Grupos ambíguos
# (ARMAMENTO, VESTUÁRIOS, EQUIPAMENTO DE COMBATE A INCÊNDIO) ficam de fora de propósito
# → o chamador cai em "outros".
_REGRAS_GRUPO = [
    ("veiculo", "viatura"),
    ("municao", "municao"),
    ("explosivo", "municao"),
    ("comunicac", "equip_comunicacao"),
    ("radiacao", "equip_comunicacao"),
    ("informatica", "equip_ti"),
    ("fotografic", "equip_ti"),
    ("aeronave", "drone_rpas"),
    ("combate a inc", "protecao_balistica"),
    ("resgate", "protecao_balistica"),
]


def categoria_por_grupo(nome_grupo: str) -> str:
    """Categoria grosseira a partir do nome_grupo do PNCP. '' se não houver match seguro."""
    g = _norm(nome_grupo)
    for key, cat in _REGRAS_GRUPO:
        if key in g:
            return cat
    return ""
