# Notas de datos (Fase 0)

Fuente de verdad: `bankrec-docs-and-masters/{README,FORMAT,POLICIES,DATA_DICTIONARY}.md` y `starter/bankrec_score.py`
(el mismo scorer del servidor). Si algo de aquí los contradice, mandan ellos.

## Dónde está cada cosa

| Qué | Ruta |
|---|---|
| Docs, maestros, sample, starter | `bankrec-docs-and-masters/` (en el repo) |
| train/, live/ (y final/ a las 18:00) | `BANKREC_DATA` o `bankrec.local.toml` (fuera del repo) |
| Maestros FINAL | `final/masters_final/` (llegan con FINAL) |

Cada carpeta de fase: `bank_transactions.csv.gz`, `ar_invoices`, `ap_invoices`, `credit_notes` (.csv.gz),
`open_items_at_start.csv.gz` (solo live/final), `golden_allocations.csv.gz` (solo train/sample),
`bank_statements/<cuenta>/*.{n43,sta,xml}`, `direct_debit_batches/`, `card_settlements/`, `cheque_deposits/`,
`payment_runs/`, `remittance_advices/<ent>/RA_*.{pdf,csv,xlsx,txt}` + `index.csv`, `pdfs/<ent>/*.pdf` + `index.csv`.
Hay ficheros AppleDouble `._*` por todo el árbol: ignorarlos siempre.

## Tamaños

| | movimientos | partidas |
|---|---|---|
| TRAIN 2021-07..2025-06 | 1.489.931 (golden 5,40 M filas, 5,26 M pares) | ARI 3,96 M · API 1,53 M · CN 150 k |
| sample 2025-06 | 8.499 (golden 36.574 filas) | ARI 50.775 (incluye abiertas antiguas) |
| LIVE 2025-07..12 | 203.083 | abiertas al inicio 325.419 (ARI 229.534, ARC 25.434, API 68.356, APC 2.095) + ARI 487 k, API 199 k, CN 19,5 k emitidas |
| val = TRAIN 2025-01..06 | ~200 k | reconstruidas (ledger.open_at) |

## Respuestas

- **¿El golden trae segmento?** Sí: `segment` (≈100 valores) y `difficulty` (1-3). El scorer da precisión por segmento.
- **Signo**: `amount` del envío SIEMPRE positivo y en la divisa de la partida (abonos ARC/APC también). En el banco:
  - ARC netos en cobros: banco +; ARC pagados por transferencia nuestra: banco − (325 casos).
  - APC netos en nuestros pagos: banco −; APC reembolsados por el proveedor: banco + (segmento `ap_supplier_refund`).
  - RETURNED: ARI con banco − (devolución de recibo/cheque); API con banco + (transferencia rechazada). Amount positivo.
- **Cómo puntúa el scorer** (`bankrec_score.py`):
  - Allocation F1 sobre pares (txn, item): TP si el par existe en golden y |amount − golden| ≤ max(0,01; 0,5 %).
    Precisión = TP / pares enviados, recall = TP / pares golden. **No mira la excepción.**
  - Transaction accuracy: movimiento con partidas → mismo nº de filas, todas en tolerancia y con la MISMA excepción
    por fila. Movimiento sin partida → una fila con misma category y exception.
  - Exception macro-F1: **por fila** (clave txn+item o txn+vacío), media de F1 de las clases presentes en el golden.
    TP de excepción NO exige importe en tolerancia (solo que el par exista con esa excepción). Una excepción en fila
    sin partida solo es TP si el golden de ese movimiento no tiene partidas. 40.911 movimientos de TRAIN mezclan
    excepciones distintas entre sus filas.
- **Categorías** (21): BANK_FEES, INTEREST_INCOME, INTEREST_EXPENSE, PAYROLL, SOCIAL_SECURITY, TAX_PAYMENT,
  TAX_REFUND, LOAN_DRAWDOWN, LOAN_REPAYMENT, LEASING, FX_DEAL, CASH_POOL, INTERCOMPANY, INTERNAL_TRANSFER,
  CUSTOMER_ADVANCE, CUSTOMER_REFUND, SUPPLIER_REFUND, CARD_EXPENSES, INSURANCE, BANK_REVERSAL, UNIDENTIFIED.
  En TRAIN sin partida: BANK_FEES 70.152, UNIDENTIFIED 34.653 (siempre con exception UNIDENTIFIED), CASH_POOL 12.576,
  PAYROLL 3.897, INTERCOMPANY 3.187, LEASING 2.832, BANK_REVERSAL 2.664, CUSTOMER_REFUND 2.611, CUSTOMER_ADVANCE 2.008,
  INTERNAL_TRANSFER 1.936, FX_DEAL 1.551, resto < 1.000. Ninguna categoría lleva excepción salvo UNIDENTIFIED.
- **open_items_at_start.csv.gz**: `item_id, item_type, entity, party_id, document_number, issue_date, due_date,
  currency, total_amount, open_amount`. NO trae nombre, payment_terms, customer_po ni payment_method: se recuperan de
  los ficheros de partidas de las fases anteriores por item_id (phase.py). 30.827 ARI y 1.264 API con open < total.

## Excepciones en TRAIN (filas)

vacía 4,97 M · FX_DIFFERENCE 113.508 · SHORT_DISCOUNT 100.174 · PARTIAL 91.664 · UNIDENTIFIED 34.653 ·
SHORT_WITHHOLDING 33.818 · RETURNED 30.358 · SHORT_DISPUTE 20.238 · DUPLICATE_PAYMENT 4.927 · OVERPAYMENT 2.838 ·
SHORT_BANK_CHARGES 1.130. Todas las clases cuentan igual en el macro-F1 (20 % de la nota): las raras valen mucho.

## Segmentos de TRAIN (top 30 por movimientos)

| segmento | movs | pares | dif. |
|---|---:|---:|---:|
| ar_ref_noise | 312.717 | 676.604 | 2,6 |
| ap_payment_multi | 174.406 | 598.314 | 2,0 |
| ar_1to1 | 141.038 | 141.038 | 1,0 |
| ap_payment_single | 139.376 | 139.376 | 1,0 |
| ar_same_amount_fifo | 49.049 | 49.049 | 3,0 |
| nonitem_bank_fees | 46.984 | 0 | 1,0 |
| ar_1toN_text | 46.644 | 259.925 | 2,0 |
| ar_short_discount | 42.704 | 48.081 | 2,0 |
| ap_direct_debit | 41.335 | 41.335 | 2,8 |
| ar_same_amount_month | 33.429 | 33.429 | 3,0 |
| ap_paid_by_holding | 33.363 | 82.599 | 2,0 |
| ar_instalment | 33.008 | 33.008 | 3,0 |
| ar_partial | 27.750 | 27.750 | 2,0 |
| ar_dd_returned | 26.397 | 26.397 | 2,0 |
| unidentified | 24.299 | 0 | 2,0 |
| ar_1toN_credit_note | 23.466 | 229.078 | 2,0 |
| nonitem_bank_fees_intl | 23.168 | 0 | 1,0 |
| ap_credit_note_netted | 18.475 | 90.243 | 2,0 |
| ap_partial | 18.131 | 83.124 | 2,0 |
| ap_supplier_refund | 17.795 | 17.795 | 2,6 |
| ar_cheque_deposit | 15.231 | 42.284 | 2,0 |
| ar_n_to_m | 13.904 | 50.776 | 3,0 |
| nonitem_cash_pool | 12.576 | 0 | 1,0 |
| ar_dd_batch | 12.505 | 713.554 | 1,0 |
| ap_factored | 10.934 | 26.777 | 2,0 |
| unidentified_lookalike | 10.354 | 0 | 3,0 |
| ar_short_withholding | 10.148 | 35.160 | 2,6 |
| ar_card_settlement | 10.051 | 147.622 | 1,0 |
| ar_1toN_remittance | 9.423 | 388.502 | 2,9 |
| ap_fx | 8.604 | 333.754 | 2,0 |

Siguen: ar_dispute_settlement, ar_dd_represented_transfer, ar_short_dispute (688 k pares), ar_retention, ap_confirming,
ar_fx, ar_discount_late, ar_third_party_confirming, ar_duplicate_payment, ar_retention_release, nonitem_*, ar_overpayment,
ar_third_party_agency, customer_refund, ar_replaced_invoice, customer_advance, ar_advance_deduction, ar_third_party_parent,
ap_returned_transfer, ar_new_iban, ar_short_bank_charges…

## Correcciones al briefing inicial (mandan los .md)

- 17 sociedades en **15 países** (16 en FINAL con ZA01), **12 divisas**, 13 bancos, **47 cuentas** en el maestro.
- FINAL: un fichero **rechazado por validación no cuenta** → se puede corregir y volver a subir. La nota se revela
  a las **20:30**. FINAL trae `final/masters_final/`. Nueva filial ZA01 (ZAR, facturas "KWI…", renombrada 2026-04-01);
  IT01 cambia de cuenta de cobros el 2026-02-01 (las dos valen); los pagos se retrasan en 2026.
- Lo que **invalida** el fichero: bank_txn_id que no es de la fase, item_id con patrón inválido, par duplicado,
  filas de partida + fila de categoría en el mismo movimiento, más de una fila sin partida por movimiento,
  amount negativo/no numérico, category/exception fuera de lista, > 60 MB o > 3 M filas.
- Lo que **NO invalida**: item_id con patrón válido pero inexistente (es solo un FP), importes sin 2 decimales,
  orden de columnas, `;` como separador, BOM, CRLF. Usamos 2 decimales por estilo, no porque lo exija.
- Un movimiento que falte no invalida: cuenta como fallo.
- Drift: Banco del Moncayo pasa de N43 a camt.053 el 2025-10-01; un gran cliente cambia el layout de sus avisos en
  sep-2025; Banca Ribagorza repite días entre ficheros (la tabla normalizada ya está deduplicada).
- "Pagos de holding": ES00 paga facturas de proveedores de ES01, ES02 y PT01 (las partidas son de la filial).
- Mes de servicio: "MANT. MAYO 25" paga la factura de ese mes (emitida a primeros del mes siguiente), no FIFO.
- UNIDENTIFIED ≈ 2,3 % de los movimientos: el fallback "todo abono sin resolver = UNIDENTIFIED" hunde la precisión
  de esa clase. P4 debe afinarlo.

## Trampas técnicas

- polars infiere `account_number`, `our_po`, `bank_subcode` como enteros y pierde ceros: `load.py` lee todo como texto.
- `python -I` ignora `PYTHONIOENCODING`: los CLI hacen `sys.stdout.reconfigure(encoding="utf-8")`.
- El commit inicial guardó el material del reto con conversión CRLF→LF (PDFs y N43 alterados). Corregido con
  `.gitattributes` (`bankrec-docs-and-masters/** -text`).

## Ledger y fase val

- Reconstrucción a 2025-07-01 (total − Σ golden; RETURNED suma; DUPLICATE_PAYMENT no resta) vs
  `live/open_items_at_start`: 100 % de los 325.419 ids, 99,70 % con importe exacto. Los 967 distintos son ARI-2021
  con cobros previos a TRAIN que el golden no ve (solo afecta a val).
- Sin filtro sobran 237 k partidas cerradas fuera de banco: 222 k emitidas antes de 2021-07 (cobradas antes de
  TRAIN), 12,7 k facturas anuladas + su ARC "Anulación de factura", 1,9 k restos de ARI parcialmente cobradas.
  `ledger.open_at` solo conserva las que siguen vivas tras el corte (golden posterior o abiertas en LIVE).
- El conjunto de candidatas de val es algo optimista: no contiene las partidas cerradas fuera de banco durante
  2025-H1, que el fichero oficial de LIVE sí contendría.
- Partidas destino de RETURNED/DUPLICATE_PAYMENT ya cerradas antes del corte: 429 pares en val (219 RETURNED,
  39 DUPLICATE_PAYMENT), máx. 174 días de antigüedad → `ctx.items` incluye las cerradas de los últimos 240 días.
- Oráculo (golden como resolver) por el pipeline completo en val: 100,00. El pipeline no pierde nada.
