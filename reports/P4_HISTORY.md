# P4: auditoría histórica

## Versión actual (2026-11-07, tarde)

SHA-256 de nonitem.py: `891e5fbee12e59acc164ce51d45a693fcf09ec57699d80af7ccca465bdb6961f`.
`uv run python scripts/p4_category_audit.py --years 2023 2024` (golden unido después de resolver).

Método: plantillas de texto de las categorías no detectadas en 2023; cada patrón nuevo se aceptó solo si en
2023 Y 2024, sobre TODOS los movimientos (con partidas incluidos), el 100 % pertenecía a una sola categoría.
REEMBOLSO se descartó (mezcla partidas APC y SUPPLIER_REFUND). Después se confirmó en val 2025-H1.

| año | movimientos | propuestas | correctas | pagos de partidas interceptados | precisión | recall sin partida |
|---|---:|---:|---:|---:|---:|---:|
| 2023 | 373702 | 27045 | 27045 | 0 | 100.00% | 75.32% |
| 2024 | 387139 | 27389 | 27389 | 0 | 100.00% | 74.84% |

Sin contar UNIDENTIFIED (no es de nonitem): 54.434 de 54.668 = 99,57 % de recall. Cambios de recall: CASH_POOL
25,7 → 100 %, FX_DEAL 62,8 → 100 %, LOAN_REPAYMENT 42,4 → 100 %, LOAN_DRAWDOWN 0 → 100 %, INSURANCE 69,1 → 100 %,
TAX_PAYMENT 76,7 → 100 %, SOCIAL_SECURITY 93,4 → 100 %, INTERCOMPANY 96,8 → 100 %, BANK_FEES 99,5 → 100 %.
Pendientes: SUPPLIER_REFUND 32,9 % (se confunde con reembolsos de abonos APC, que son partida) y CUSTOMER_REFUND 95 %.

Val completo (2025-01..06): 59,68 antes de estos cambios; resultados en `docs/SCOREBOARD.md`.

## Versión anterior (congelada)

El resolver se ejecutó antes de unir las etiquetas. No se modificaron expresiones ni umbrales tras ver los resultados.

SHA-256 de nonitem.py: `a6cfd81ddc7315b94780386693e97bcb1befe94e898df3157c22d4fb862d22ef`.

| año | movimientos | propuestas | correctas | pagos de partidas interceptados | precisión | recall sin partida |
|---|---:|---:|---:|---:|---:|---:|
| 2023 | 373702 | 24225 | 24225 | 0 | 100.00% | 67.47% |
| 2024 | 387139 | 24596 | 24596 | 0 | 100.00% | 67.21% |

Total: 760.841 movimientos, 48.821 propuestas correctas y cero falsas categorías. Esto es evidencia histórica, no garantía sobre FINAL. El recall incluye UNIDENTIFIED, categoría que nonitem no propone. Los agregados por banco/formato, entidad y mes están en `out/qa/p4_category_history.json`.

| categoría | propuestas | reales | recall |
|---|---:|---:|---:|
| BANK_FEES | 35446 | 35616 | 99.52% |
| BANK_REVERSAL | 1355 | 1356 | 99.93% |
| CARD_EXPENSES | 359 | 359 | 100.00% |
| CASH_POOL | 1612 | 6268 | 25.72% |
| CUSTOMER_ADVANCE | 1029 | 1029 | 100.00% |
| CUSTOMER_REFUND | 1273 | 1340 | 95.00% |
| FX_DEAL | 514 | 819 | 62.76% |
| INSURANCE | 76 | 110 | 69.09% |
| INTERCOMPANY | 1586 | 1638 | 96.83% |
| INTEREST_EXPENSE | 93 | 93 | 100.00% |
| INTEREST_INCOME | 151 | 151 | 100.00% |
| INTERNAL_TRANSFER | 954 | 954 | 100.00% |
| LEASING | 1416 | 1416 | 100.00% |
| LOAN_DRAWDOWN | 0 | 14 | 0.00% |
| LOAN_REPAYMENT | 179 | 422 | 42.42% |
| PAYROLL | 1948 | 1948 | 100.00% |
| SOCIAL_SECURITY | 381 | 408 | 93.38% |
| SUPPLIER_REFUND | 81 | 246 | 32.93% |
| TAX_PAYMENT | 368 | 480 | 76.67% |
| TAX_REFUND | 0 | 1 | 0.00% |
| UNIDENTIFIED | 0 | 17835 | 0.00% |

Las mayores carencias observadas son cash pool, préstamos y reembolsos de proveedores. No se han añadido reglas para recuperar estos ejemplos. Cualquier mejora requiere evidencia contable y una ventana de evaluación separada.

## Validación del pipeline actual

Nota oficial: **38.29**. Precisión de asignaciones 99.6911%; recall 24.9246%; exactitud por movimiento 54.1930%.

Ventana: enero–junio de 2025, 199.987 movimientos; el sample de junio ya se usó en desarrollo y no es un holdout intacto. Tiempo del pipeline: 143,7 segundos, con extracción de PDFs incluida. El resultado integra los módulos existentes de P1–P3; la nota no se atribuye a los cambios del validador.

## Contraste con ejemplos web

Resuelto: la página es una SPA y sus datos están en `https://hackathon.usetalky.com/examples.json` (36 ejemplos,
uno por segmento). Copia en `tests/p4_web_examples.json`, cubierta por `tests/p4_test.py` (clase WebExamples).
