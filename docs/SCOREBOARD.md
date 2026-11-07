# Scoreboard

| fecha-hora | quién | cambio | sample | val (2025-01..06) | LIVE (plataforma) |
|---|---|---|---:|---:|---:|
| 2026-11-07 10:35 | P1 | esqueleto (solo fallback: abono→UNIDENTIFIED, cargo→BANK_FEES) | 2.80 | 2.29 | — |
| 2026-11-07 10:35 | P1 | starter oficial `out/live/starter.csv.gz` (validado, 33.577 casados) | — | — | pendiente (P4) |
| 2026-11-07 | P4 | categorías conservadoras + router orientativo + QA + preparación manual; sample visto en desarrollo | 3.25 | no disponible: TRAIN vacío | no enviado: LIVE vacío |

Comprobación del ledger: partidas abiertas reconstruidas a 2025-07-01 desde TRAIN (total − Σ golden, RETURNED suma,
DUPLICATE_PAYMENT no resta) vs `live/open_items_at_start`: 100 % de los 325.419 ids presentes, 99,70 % con importe
exacto (967 difieren). Sin filtro sobran 237 k partidas cerradas fuera de banco (pre-2021-07, anuladas+abono,
restos): `ledger.open_at` solo conserva las que siguen vivas después del corte.


P4: baseline local sample 2.80 → 3.25; smoke verde y siete pruebas de regresión pasan.
El sample se utilizó para desarrollo, no es holdout. Se retiraron reglas ambiguas aunque redujeran la nota sample.
No se ha podido comprobar la fila histórica del starter LIVE: el artefacto no existe en este checkout.
Detalle y estrategia de evaluación sin fuga: `reports/P4_QA.md`.
