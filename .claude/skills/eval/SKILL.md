---
name: eval
description: Cómo medir un cambio en bankrec - smoke.py antes de cada push, run_phase + eval.py sobre val con informe por segmento y volcado de fallos. Úsala tras tocar cualquier resolver, exceptions.py o el ledger.
---

# Medir un cambio

1. **Smoke (siempre antes de push, < 2 min)**: `uv run python scripts/smoke.py` → debe acabar en `SMOKE VERDE`.
   Corre el pipeline sobre `sample` (2025-06), valida el fichero como el servidor y lo puntúa con el scorer oficial.
2. **Val (imita LIVE)**: TRAIN 2025-01..06 con partidas abiertas reconstruidas al 2025-01-01.
   - Rápido: `uv run python scripts/run_phase.py --phase val --months 1` y `uv run python scripts/eval.py --phase val --months 1`
   - Completo: sin `--months` (≈ 200 k movimientos).
   - Aislar un resolver: `--disable nombre` en run_phase para ver cuánto aporta.
3. **Leer el informe**: primera línea `SCORE … | allocF1 (P, R) | txnAcc | excF1 | amtAcc`. Luego segmentos
   ordenados por nº de fallos (n × (1 − acc)): ataca los de arriba que sean de tu rol (ver CLAUDE.md).
   - P baja → asignas pares de más (FP). R baja → te dejas pares.
   - excF1 es por fila y por clase: una clase rara mal predicha pesa como una común.
4. **Ver fallos**: `uv run python scripts/eval.py --phase val --months 1 --segment ar_1to1 --dump 30`
   → `out/val/failures_ar_1to1.md` (movimiento, golden, predicción, resolver y razón del audit).
   Para patrones a escala usa el subagente `golden-auditor`.
5. Apunta la nota en `docs/SCOREBOARD.md` (fecha-hora, quién, cambio, val, sample).

No vuelques ficheros grandes al contexto: lee el .md de fallos o agrega con duckdb/polars.

## Antes de aceptar una mejora (anti-overfitting, detalle en CLAUDE.md)

- El código de la cascada no lee `ctx.golden` ni `segment`/`difficulty`; el histórico es `booking_date < ctx.start`.
- Ningún id, nombre, IBAN ni importe concreto sacado de `failures_*.md` aparece en el código.
- La regla cita POLICIES.md o un patrón repetido en varias entidades y años.
- Confirmada con los 6 meses de val (no solo `--months 1`) y sin hundir otros segmentos.
- `sample` no cuenta como validación independiente: está dentro de val.
