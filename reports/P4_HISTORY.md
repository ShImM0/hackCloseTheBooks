# P4: auditoría histórica congelada

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

Pendiente: `https://hackathon.usetalky.com/ejemplos` no es accesible con ninguna de las dos herramientas web disponibles y curl falla al resolver el dominio. No se ha leído ni se afirma conformidad con su contenido. Se solicitó al usuario que pegue los ejemplos. Hasta recibirlos se mantiene la documentación oficial del repositorio como fuente de verificación.
