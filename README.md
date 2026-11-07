# bankrec — hackathon Close The Books (Grupo Valtermia)

Conciliación bancaria por cascada de resolvers sobre un ledger de partidas abiertas.
Guía para el equipo y para Claude Code: **CLAUDE.md**. Notas de datos: **docs/DATA_NOTES.md**.
Reto: `bankrec-docs-and-masters/README.md`, `FORMAT.md`, `POLICIES.md`.

## Instalación

### Windows 11

1. Git for Windows (incluye Git Bash): https://git-scm.com/download/win
2. uv (PowerShell): `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`
3. Clonar e instalar:
   ```
   git clone https://github.com/ShImM0/hackCloseTheBooks.git
   cd hackCloseTheBooks
   uv sync
   ```
4. Crear `bankrec.local.toml` (no se sube) con la carpeta que contiene `train/`, `live/` (y `final/`):
   ```
   data_dir = "C:/Users/<yo>/Downloads/CTB"
   ```
   (alternativa: variable de entorno `BANKREC_DATA`).

### Linux

```
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone https://github.com/ShImM0/hackCloseTheBooks.git && cd hackCloseTheBooks
uv sync
echo 'data_dir = "/home/<yo>/CTB"' > bankrec.local.toml
```

## Comprobar que todo funciona

```
uv run python scripts/smoke.py                          # < 2 min, debe terminar en SMOKE VERDE
uv run python scripts/run_phase.py --phase val --months 1
uv run python scripts/eval.py --phase val --months 1
```

`uv run python scripts/run_phase.py --phase live` genera `out/live/submission.csv.gz` (validado). Solo P4 envía, a mano.
