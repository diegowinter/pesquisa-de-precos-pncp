"""Ícones e rótulos de `step_status` (docs/06_API_E_WEB.md §4.1) — usados pelos templates do
grafo e da tela de step. Central para não espalhar o mapeamento em cada `.html`."""

ICONE_STEP = {
    "not_started": "○",
    "running": "▶",
    "awaiting_approval": "⏸",
    "finished": "✓",
    "outdated": "⚠",
    "failed": "✗",
    "cancelled": "⊘",
    "skipped": "⊘",
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
