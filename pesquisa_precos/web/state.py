"""Ícones e rótulos de `step_status` (docs/06_API_E_WEB.md §4.1) — usados pelos templates do
grafo e da tela de step. Central para não espalhar o mapeamento em cada `.html`."""

# Nomes do sprite Lucide (`templates/_icons.svg`), renderizados pelo macro `icone()`.
ICONE_STEP = {
    "not_started": "circle",
    "running": "circle-play",
    "awaiting_approval": "pause",
    "finished": "check",
    "outdated": "triangle-alert",
    "failed": "x",
    "cancelled": "ban",
    "skipped": "ban",
}

CLASSE_STEP = {
    "not_started": "nao-iniciada",
    "running": "running",
    "awaiting_approval": "gate",
    "finished": "finished",
    "outdated": "outdated",
    "failed": "failed",
    "cancelled": "cancelled",
    "skipped": "cancelled",
}

# O identificador é inglês (banco/enum), mas o operador lê português (CLAUDE.md §Idioma).
ROTULO_STEP = {
    "not_started": "não iniciada",
    "running": "executando",
    "awaiting_approval": "aguardando aprovação",
    "finished": "concluída",
    "outdated": "desatualizada",
    "failed": "falhou",
    "cancelled": "cancelada",
    "skipped": "pulada",
}

ROTULO_ACAO = {
    "update": "atualizar",
    "resume": "retomar",
    "redo": "refazer",
}


# Navegação do cabeçalho: grupos com rótulo, para o operador enxergar que telas existem.
# Cada item é (href, rótulo, ícone do sprite); ativo = a rota atual bate com o href.
NAV = (
    ("Execução", (
        ("/runs", "Runs", "layers"),
        ("/cost", "Custo", "circle-dollar-sign"),
        ("/exports", "Exports", "file-spreadsheet"),
        ("/diff", "Diff entre runs", "git-compare"),
    )),
    ("Dados", (
        ("/catalog", "Allow-list", "list-checks"),
        ("/prompts", "Prompts", "message-square-text"),
        ("/recalibrate", "Recalibrar", "sliders-horizontal"),
    )),
    ("Ajustes", (
        ("/config", "Configuração", "settings"),
        ("/providers", "Provedores", "plug"),
        ("/notifications", "Notificações", "bell"),
    )),
)
