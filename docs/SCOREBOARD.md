# Scoreboard

| fecha-hora | quién | cambio | sample | val (2025-01..06) | LIVE (plataforma) |
|---|---|---|---:|---:|---:|
| 2026-11-07 10:35 | P1 | esqueleto (solo fallback: abono→UNIDENTIFIED, cargo→BANK_FEES) | 2.80 | 2.29 | — |
| 2026-11-07 10:35 | P1 | starter oficial `out/live/starter.csv.gz` (validado, 33.577 casados) | — | — | pendiente (P4) |
| 2026-11-07 | P4 | categorías conservadoras + router orientativo + QA + preparación manual; sample visto en desarrollo | 3.25 | no disponible: TRAIN vacío | no enviado: LIVE vacío |
| 2026-11-07 11:06 | P1 | medición equipo en 707fc7c (nonitem + router + docs_dd) | 18.98 | 17.00 (P 0.998 R 0.132) | — |

Comprobación del ledger: partidas abiertas reconstruidas a 2025-07-01 desde TRAIN (total − Σ golden, RETURNED suma,
DUPLICATE_PAYMENT no resta) vs `live/open_items_at_start`: 100 % de los 325.419 ids presentes, 99,70 % con importe
exacto (967 difieren). Sin filtro sobran 237 k partidas cerradas fuera de banco (pre-2021-07, anuladas+abono,
restos): `ledger.open_at` solo conserva las que siguen vivas después del corte.


P4: baseline local sample 2.80 → 3.25; smoke verde y siete pruebas de regresión pasan.
El sample se utilizó para desarrollo, no es holdout. Se retiraron reglas ambiguas aunque redujeran la nota sample.
No se ha podido comprobar la fila histórica del starter LIVE: el artefacto no existe en este checkout.
Detalle y estrategia de evaluación sin fuga: `reports/P4_QA.md`.

- 2026-11-07T10:04:27.771974+00:00 · P4 · LIVE PREPARADO, NO ENVIADO: `out/live/prepared_20261107T100427771974Z_34942b0cce96.csv.gz` · SHA-256 `34942b0cce969d3cc222b765832c55f8d453f5ad5ed34563b2fad4e72645088e` · 203083 movimientos. Nota pendiente de envío manual.

- 2026-11-07T10:13:27.597934+00:00 · P4 · LIVE PREPARADO, NO ENVIADO: `out/live/prepared_20261107T101327597934Z_aad15d82163d.csv.gz` · SHA-256 `aad15d82163d71fcd2786ac2e13a3c90c604951ccc635eaf8631ed4d9920f944` · 203083 movimientos. Nota pendiente de envío manual.
