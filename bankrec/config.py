"""Rutas y constantes del proyecto. Dueño: P1.

Dos raíces:
- DOCS_DIR (en el repo): bankrec-docs-and-masters/ con masters/, sample/ y starter/.
- data_dir() (fuera del repo): carpeta con train/, live/ y final/. Se toma de la variable de entorno
  BANKREC_DATA, si no de bankrec.local.toml (data_dir = "C:/Users/yo/CTB"), y si no ./data.
"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = REPO_ROOT / "bankrec-docs-and-masters"
STARTER_DIR = DOCS_DIR / "starter"
OUT_DIR = REPO_ROOT / "out"

EXCEPTIONS = [
    "PARTIAL", "OVERPAYMENT", "SHORT_DISCOUNT", "SHORT_BANK_CHARGES", "SHORT_WITHHOLDING",
    "SHORT_DISPUTE", "DUPLICATE_PAYMENT", "UNIDENTIFIED", "RETURNED", "FX_DIFFERENCE",
]
HEADER = ["bank_txn_id", "item_id", "amount", "category", "exception"]

# Fases: carpeta de datos y ventana de fechas de los movimientos [desde, hasta)
PHASES = {
    "sample": ("sample", "2025-06-01", "2025-07-01"),
    "val": ("train", "2025-01-01", "2025-07-01"),   # imita LIVE con TRAIN; modelos solo con datos < 2025-01
    "live": ("live", "2025-07-01", "2026-01-01"),
    "final": ("final", "2026-01-01", "2026-07-01"),
}
# Fases cuyos ficheros de partidas preceden a cada fase (para recuperar atributos de partidas abiertas)
PRIOR_FOLDERS = {"sample": [], "val": [], "live": ["train"], "final": ["train", "live"]}


def data_dir() -> Path:
    env = os.environ.get("BANKREC_DATA")
    if env:
        return Path(env)
    local = REPO_ROOT / "bankrec.local.toml"
    if local.exists():
        with open(local, "rb") as fh:
            cfg = tomllib.load(fh)
        if cfg.get("data_dir"):
            return Path(cfg["data_dir"])
    return REPO_ROOT / "data"


def folder_dir(folder: str) -> Path:
    """Carpeta física de train/live/final/sample."""
    return DOCS_DIR / "sample" if folder == "sample" else data_dir() / folder


def masters_dir(phase: str) -> Path:
    """FINAL trae maestros actualizados en final/masters_final/."""
    if phase == "final":
        p = data_dir() / "final" / "masters_final"
        if p.exists():
            return p
    return DOCS_DIR / "masters"


def cache_dir() -> Path:
    p = data_dir() / "_cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


def out_dir(phase: str) -> Path:
    p = OUT_DIR / phase
    p.mkdir(parents=True, exist_ok=True)
    return p
