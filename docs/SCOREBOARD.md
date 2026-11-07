# Scoreboard

| fecha-hora | quién | cambio | sample | val (2025-01..06) | LIVE (plataforma) |
|---|---|---|---:|---:|---:|
| 2026-11-07 10:35 | P1 | esqueleto (solo fallback: abono→UNIDENTIFIED, cargo→BANK_FEES) | 2.80 | 2.29 | — |
| 2026-11-07 10:35 | P1 | starter oficial `out/live/starter.csv.gz` (validado, 33.577 casados) | — | — | pendiente (P4) |
| 2026-11-07 | P4 | categorías conservadoras + router orientativo + QA + preparación manual; sample visto en desarrollo | 3.25 | no disponible: TRAIN vacío | no enviado: LIVE vacío |
| 2026-11-07 11:06 | P1 | medición equipo en 707fc7c (nonitem + router + docs_dd) | 18.98 | 17.00 (P 0.998 R 0.132) | — |
| 2026-11-07 11:35 | P1 | refs + parties + ledger FIFO + text_ref + amount_match (1:1 importe exacto) | 39.31 | 38.30 (P 0.997 R 0.249) | — |
| 2026-11-07 | P4 | auditoría 2023/2024 sin cambiar reglas; val completo + comprobación de ids conocidos | — | 38.29 (P 0.9969 R 0.2492) | sin envío confirmado |
| 2026-11-07 12:10 | P1 | text_ref.by_ref: referencia + tercero sin importe exacto (+ exceptions P3) | — | 44.35 (P 0.988 R 0.276) | — |
| 2026-11-07 11:48 | P1 | envío LIVE `envio_live_1148_09b5ddf.csv.gz` (main 09b5ddf; val 60.04) | — | 60.04 | **59.67** |
| 2026-11-07 12:20 | P1 | ranker LightGBM (último de la cascada, entrena con < ctx.start) | — | 61.23 (P 0.995 R 0.456) | — |
| 2026-11-07 12:00 | P1 | anuladas fuera (§5.7), sustitutas por alias, N43 cortado, prefijos estrictos, ref de otra factura bloquea amount_match | — | 62.04 (P 0.995 R 0.460) | — |
| 2026-11-07 12:08 | P1 | main c4f389b completo (incluye exceptions_amounts de P3); fichero `out/live/envio_live_1201_c4f389b.csv.gz` | — | 63.39 (P 0.995 R 0.464) | **62.79** |
| 2026-11-07 12:40 | P4 | medición tras merge fd7f56d (docs_ap + amount_match FX) | 55.26 | 59.68 (P 0.9956 R 0.4472; excF1 0.283) | — |
| 2026-11-07 13:10 | P4 | nonitem: patrones auditados 2023/24 (cash pool por maestro, préstamos, divisa, impuestos, seguros, TGSS); cascada estándar, base fd7f56d (anterior a 63.39 de P1) | 55.36 | 59.90 (P 0.9956 R 0.4472; txnAcc 0.7789; excF1 0.283) | — |
| 2026-11-07 13:10 | P4 | + `nonitem_unmatched` al final de la cascada, base fd7f56d (`scripts/p4_run.py`; falta registrarlo, P1) | — | 60.36 (txnAcc 0.7764; excF1 0.311; UNIDENTIFIED F1 0.19→0.47) | no enviado; LIVE válido, 392.949 filas, 2 min 28 s |
| 2026-11-07 12:35 | P1 | fallback: UNIDENTIFIED sin excepción si cliente reconocido o lote; guarda OVERPAYMENT (efectivo > 1,35×saldo) | — | 63.76 (excF1 0.398; UNIDENTIFIED F1 0.28→0.44, OVERPAYMENT P 0.03→0.11) | — |

Comprobación del ledger: partidas abiertas reconstruidas a 2025-07-01 desde TRAIN (total − Σ golden, RETURNED suma,
DUPLICATE_PAYMENT no resta) vs `live/open_items_at_start`: 100 % de los 325.419 ids presentes, 99,70 % con importe
exacto (967 difieren). Sin filtro sobran 237 k partidas cerradas fuera de banco (pre-2021-07, anuladas+abono,
restos): `ledger.open_at` solo conserva las que siguen vivas después del corte.


P4: baseline local sample 2.80 → 3.25; smoke verde y siete pruebas de regresión pasan.
El sample se utilizó para desarrollo, no es holdout. Se retiraron reglas ambiguas aunque redujeran la nota sample.
Estado actualizado: TRAIN/LIVE disponibles y ruta local configurada. Las referencias anteriores a datos vacíos describen el estado inicial. Los ficheros LIVE preparados siguen sin envío confirmado.
Detalle y estrategia de evaluación sin fuga: `reports/P4_QA.md`.

- 2026-11-07T10:04:27.771974+00:00 · P4 · LIVE PREPARADO, NO ENVIADO: `out/live/prepared_20261107T100427771974Z_34942b0cce96.csv.gz` · SHA-256 `34942b0cce969d3cc222b765832c55f8d453f5ad5ed34563b2fad4e72645088e` · 203083 movimientos. Nota pendiente de envío manual.

- 2026-11-07T10:13:27.597934+00:00 · P4 · LIVE PREPARADO, NO ENVIADO: `out/live/prepared_20261107T101327597934Z_aad15d82163d.csv.gz` · SHA-256 `aad15d82163d71fcd2786ac2e13a3c90c604951ccc635eaf8631ed4d9920f944` · 203083 movimientos. Nota pendiente de envío manual.
