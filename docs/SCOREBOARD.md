# Scoreboard — Envíos Efectivos (LIVE)

> Pesos del juez: **Allocation F1 50 %** · **Transaction accuracy 30 %** · **Exception macro-F1 20 %**. La 4.ª métrica ("Exactitud ponderada por importe") es informativa, no puntúa.

## 1. Resumen de Envíos Efectivos al Juez

| # | Fecha-Hora | Autor | Commit / Archivo | Cambios acumulados en el envío | Val Local (2025-01..06) | LIVE Total (/100) | Allocation F1 (50 %)<br>`Total (P / R)` | Transaction Accuracy (30 %) | Exception Macro-F1 (20 %) | Exactitud ponderada por importe | Volumen<br>`Movs / Filas / Sin resp.` |
| -: | ---------------- | -- | ----------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------: | --------: | ----------------------------------------- | -------: | -------: | -------: | ----------------------------------- |
| 0 | 2026-11-07 10:04 | P1 | `prepared_20261107T100427771974Z_34942b0cce96.csv.gz` | Envío base (solo `nonitem`) | — | **2.84** | `0,0 %`<br>*(P `0,0 %` / R `0,0 %`)* | `9,0 %` | `0,7 %` | `49,2 %` | `203.083` / `203.083` / `0` |
| 1 | 2026-11-07 11:48 | P1 | `09b5ddf`<br>`envio_live_1148_09b5ddf.csv.gz` | Base `nonitem` + `router` + `docs_dd` + `docs_ap` + `refs` + `parties` + `ledger` FIFO + `text_ref` + `amount_match` (1:1 y FX) | 60.04<br>*(P 99.6% · R 44.7% · excF1 28.3%)* | **59.67** | `61,2 %`<br>*(P `99,6 %` / R `44,2 %`)* | `77,7 %` | `28,8 %` | `63,4 %` | `203.083` / `392.949` / `0` |
| 2 | 2026-11-07 12:01 | P1 | `c4f389b`<br>`envio_live_1201_c4f389b.csv.gz` | + `text_ref.by_ref` (ref+tercero sin importe exacto) + `exceptions_amounts` (P3) + ranker LightGBM + anuladas fuera (§5.7), sustitutas por alias, N43 cortado, prefijos estrictos, bloqueo por ref de otra factura | 63.39<br>*(P 99.5% · R 46.4%)* | **62.79** | `62,3 %`<br>*(P `99,5 %` / R `45,4 %`)* | `80,9 %` | `36,8 %` | `63,8 %` | `203.083` / `393.292` / `0` |
| 3 | 2026-11-07 12:24 | P1 | `2b8c623` (`d145a0d`)<br>`envio_live_1224_2b8c623.csv.gz` | + fallback `UNIDENTIFIED` sin excepción (cliente reconocido o lote) + guarda `OVERPAYMENT` (> 1,35×saldo) + `nonitem_unmatched` (P4) al final de la cascada | 65.87<br>*(P 99.5% · R 49.3% · excF1 41.0% · UNID F1 55.0%)* | **65.09** | `64,7 %`<br>*(P `99,5 %` / R `47,9 %`)* | `82,0 %` | `40,7 %` | `80,4 %` | `203.083` / `411.137` / `0` |
| 4 | 2026-11-07 12:28 | P4 | `e7c2b23`<br>`envio_live_1228_e7c2b23_p4.csv.gz` | #2 + `docs_card` (P2) + `nonitem` P4 (patrones auditados 2023/24, cash pool por maestro) + `nonitem_unmatched` al final con `scripts/p4_run.py`; sin el fallback/guarda OVERPAYMENT de `d145a0d` ni DUPLICATE_PAYMENT | 65.54 | **64.93** ▼0.16 | `64,7 %`<br>*(P `99,5 %` / R `47,9 %`)* | `81,8 %` | `40,3 %` | `80,4 %` | `203.083` / `411.137` / `0` |
| 5 | 2026-11-07 13:26 | P1 | `4861321`<br>`envio_live_1326_4861321.csv.gz` | #3 + DUPLICATE_PAYMENT (amount_match.duplicate) + guarda OVERPAYMENT a +10 % + subset_sum (P2) | 70.50 | **69.36** ▲4.27 vs #3 | `67,2 %`<br>*(P `89,2 %` / R `53,9 %`)* | `86,7 %` | `48,9 %` | `81,9 %` | `203.083` / `488.316` / `0` |
| 6 | 2026-11-07 13:57 | P4 | `1eed3ec`<br>`envio_live_1357_1eed3ec.csv.gz` | #5 + cheques/contracargos (docs_card_chq, P2) + P4: UNIDENTIFIED solo sin tercero, SUPPLIER/CUSTOMER_REFUND | 71.38<br>*(P 90.3% · R 56.2% · excF1 51.5% · UNID F1 85.9%)* | **70.23** 🏆 ▲0.87 vs #5 | `67,9 %`<br>*(P `89,3 %` / R `54,7 %`)* | `87,5 %` | `50,3 %` | `82,0 %` | `203.083` / `493.101` / `0` |
| 7 | 2026-11-07 15:08 | P1 | `c21c984`<br>`envio_live_1508_c21c984.csv.gz` | #6 + `docs_remit` nuevo (P2, 160 k pares en val) + `multi_ref` (P2) + `multi_ref_fill` (P1, textos cortados) | 85.73<br>*(P 95.4% · R 79.1% · excF1 73.3%)* | **84.12** 🏆 ▲13.89 vs #6 | `85,5 %`<br>*(P `94,8 %` / R `77,8 %`)* | `92,2 %` | `68,7 %` | `88,7 %` | `203.083` / `645.577` / `0` |

---

## 2. Matriz Comparativa por Segmento (LIVE)

> **Nota**: segmentos y movimientos tal como los muestra el juez (`203.083` movimientos totales). Valor = `Exactitud` por segmento. `n/v` = fila tapada por la barra de navegación en la captura. `Δ 7–6` = Envío #7 menos Envío #6 (mejor anterior; solo cambios).

|  # | Segmento | Movimientos | Base *(2.84)* | #1 *(59.67)* | #2 *(62.79)* | #3 *(65.09)* | #4 *(64.93)* | #5 *(69.36)* | #6 *(70.23)* | #7 *(84.12)* | Δ 7–6 |
| -: | ---------------------------- | ------: | ------: | ------: | ------: | ------: | ------: | ------: | ------: | ------: | ----: |
|  1 | `ar_ref_noise` | ~42.000 | 0,0 % | 73,1 % | 73,2 % | 73,2 % | 73,2 % | 83,9 % | 83,9 % | 88,9 % | **+5,0** |
|  2 | `ap_payment_multi` | ~25.000 | 0,0 % | 97,9 % | 98,2 % | 98,2 % | 98,2 % | 98,2 % | 98,2 % | 98,2 % |  |
|  3 | `ar_1to1` | ~19.000 | 0,0 % | 99,7 % | 99,7 % | 99,7 % | 99,7 % | 99,1 % | 99,0 % | 99,2 % | +0,2 |
|  4 | `ap_payment_single` | ~18.000 | 0,0 % | 99,5 % | 99,6 % | 99,6 % | 99,6 % | 99,6 % | 99,6 % | 99,6 % |  |
|  5 | `nonitem_bank_fees` | ~6.700 | 100,0 % | 100,0 % | 100,0 % | 100,0 % | 100,0 % | 100,0 % | 100,0 % | 100,0 % |  |
|  6 | `ar_1toN_text` | ~6.500 | 0,0 % | 0,0 % | 0,0 % | 0,0 % | 0,0 % | 33,5 % | 34,1 % | 80,7 % | **+46,6** |
|  7 | `ar_same_amount_fifo` | ~6.400 | 0,0 % | 96,2 % | 96,3 % | 96,3 % | 96,3 % | 96,1 % | 96,2 % | 96,1 % | −0,1 |
|  8 | `ap_direct_debit` | ~5.600 | 0,0 % | 100,0 % | 100,0 % | 100,0 % | 100,0 % | 100,0 % | 100,0 % | 100,0 % |  |
|  9 | `ar_short_discount` | ~5.500 | 0,0 % | 50,3 % | 90,1 % | 90,1 % | 90,1 % | 89,4 % | 89,5 % | 90,8 % | +1,3 |
| 10 | `ap_paid_by_holding` | ~4.800 | 0,0 % | 99,2 % | 99,4 % | 99,4 % | 99,4 % | 99,4 % | 99,4 % | 99,4 % |  |
| 11 | `ar_instalment` | ~4.700 | 0,0 % | 76,4 % | 79,3 % | 81,3 % | 75,3 % | 92,1 % | 89,0 % | 93,5 % | **+4,5** |
| 12 | `ar_same_amount_month` | ~4.400 | 0,0 % | 99,8 % | 99,9 % | 99,9 % | 99,9 % | 99,6 % | 99,7 % | 99,7 % |  |
| 13 | `ar_partial` | ~3.900 | 0,0 % | 74,8 % | 76,5 % | 77,5 % | 76,4 % | 97,7 % | 98,0 % | 98,0 % |  |
| 14 | `ar_dd_returned` | ~3.500 | 0,0 % | 95,7 % | 95,7 % | 95,7 % | 95,7 % | 95,7 % | 95,6 % | 95,7 % | +0,1 |
| 15 | `ar_1toN_credit_note` | ~3.300 | 0,0 % | 0,0 % | 0,0 % | 0,0 % | 0,0 % | 14,3 % | 14,4 % | 45,0 % | **+30,6** |
| 16 | `nonitem_bank_fees_intl` | ~3.300 | 100,0 % | 100,0 % | 100,0 % | 100,0 % | 100,0 % | 100,0 % | 100,0 % | 100,0 % |  |
| 17 | `ap_credit_note_netted` | ~2.700 | 0,0 % | 97,8 % | 98,1 % | 98,1 % | 98,1 % | 98,1 % | 98,1 % | 98,1 % |  |
| 18 | `ap_partial` | ~2.700 | 0,0 % | 75,3 % | 75,7 % | 75,7 % | 75,7 % | 75,9 % | 76,0 % | 76,0 % |  |
| 19 | `ap_supplier_refund` | ~2.600 | 0,0 % | 0,0 % | 98,9 % | 98,9 % | 98,9 % | 98,9 % | 98,9 % | 98,9 % |  |
| 20 | `ar_cheque_deposit` | ~2.000 | 0,0 % | 0,0 % | 0,0 % | 0,0 % | 0,0 % | 0,0 % | 96,0 % | 96,0 % |  |
| 21 | `ar_n_to_m` | ~1.800 | 0,0 % | 0,0 % | 0,0 % | 0,0 % | 0,0 % | 3,4 % | 3,3 % | 17,4 % | **+14,1** |
| 22 | `ar_dd_batch` | ~1.600 | 0,0 % | 94,4 % | 94,5 % | 94,3 % | 94,5 % | 92,6 % | 92,2 % | 100,0 % | **+7,8** |
| 23 | `nonitem_cash_pool` | ~1.600 | 25,5 % | 25,5 % | 25,5 % | 100,0 % | 100,0 % | 100,0 % | 100,0 % | 100,0 % |  |
| 24 | `ap_factored` | ~1.400 | 0,0 % | 95,1 % | 95,3 % | 95,3 % | 95,3 % | 95,3 % | 95,3 % | 95,3 % |  |
| 25 | `ar_short_withholding` | ~1.400 | 0,0 % | 36,2 % | 84,4 % | 84,3 % | 84,4 % | 83,8 % | 83,8 % | 85,8 % | **+2,0** |
| 26 | `ap_fx` | ~1.300 | 0,0 % | 70,8 % | 70,9 % | 70,9 % | 70,9 % | 70,9 % | 70,9 % | 70,9 % |  |
| 27 | `ar_card_settlement` | ~1.300 | 0,0 % | 0,0 % | 0,0 % | 100,0 % | 100,0 % | 100,0 % | 100,0 % | 100,0 % |  |
| 28 | `ar_dispute_settlement` | ~1.300 | 0,0 % | 33,4 % | 33,4 % | 33,4 % | 33,4 % | 33,2 % | 33,2 % | 80,2 % | **+47,0** |
| 29 | `ar_1toN_remittance` | ~1.200 | 0,0 % | 0,0 % | 0,0 % | 0,0 % | 0,0 % | 1,2 % | 1,2 % | 88,2 % | **+87,0** |
| 30 | `ar_short_dispute` | ~1.100 | 0,0 % | 0,0 % | 0,0 % | 0,0 % | 0,0 % | 0,0 % | 0,0 % | 67,5 % | **+67,5** |
| 31 | `ar_retention` | ~1.000 | 0,0 % | 43,7 % | 56,3 % | 58,6 % | 57,7 % | 97,5 % | 97,5 % | 97,8 % | +0,3 |
| 32 | `ar_dd_represented_transfer` | ~980 | 0,0 % | 94,6 % | 94,8 % | 94,8 % | 94,7 % | 94,7 % | 94,8 % | 94,8 % |  |
| 33 | `ap_confirming` | ~850 | 0,0 % | 80,1 % | 80,1 % | 80,1 % | 80,1 % | 80,1 % | 79,8 % | 80,1 % | +0,3 |
| 34 | `ar_discount_late` | ~830 | 0,0 % | 31,3 % | 53,1 % | 53,1 % | 53,1 % | 53,1 % | 53,1 % | 56,1 % | **+3,0** |
| 35 | `ar_fx` | ~760 | 0,0 % | 24,3 % | 24,9 % | 25,7 % | 25,1 % | 26,9 % | 27,0 % | 38,1 % | **+11,1** |
| 36 | `ar_retention_release` | ~720 | 0,0 % | 99,4 % | 99,3 % | 99,3 % | 99,3 % | 97,8 % | 98,2 % | 97,9 % | −0,3 |
| 37 | `ar_third_party_confirming` | ~690 | 0,0 % | 92,5 % | 99,4 % | 99,4 % | 99,6 % | 91,7 % | 95,9 % | 96,4 % | +0,5 |
| 38 | `ar_duplicate_payment` | ~590 | 0,0 % | 0,0 % | 0,0 % | 0,0 % | 0,0 % | 98,6 % | 98,8 % | 98,8 % |  |
| 39 | `customer_refund` | ~390 | 95,6 % | 95,6 % | 95,6 % | 95,6 % | 95,6 % | 95,6 % | 98,5 % | 98,5 % |  |
| 40 | `nonitem_intercompany` | ~380 | 96,5 % | 96,5 % | 96,5 % | 100,0 % | 100,0 % | 100,0 % | 100,0 % | 100,0 % |  |
| 41 | `ar_third_party_agency` | ~350 | 0,0 % | 63,6 % | 65,6 % | 65,6 % | 65,6 % | 76,8 % | 77,7 % | 83,1 % | **+5,4** |
| 42 | `ar_overpayment` | ~340 | 0,0 % | 46,0 % | 55,1 % | 55,1 % | 57,5 % | 58,4 % | 61,3 % | 59,2 % | **−2,1** |
| 43 | `nonitem_bank_reversal` | ~330 | 100,0 % | 100,0 % | 100,0 % | 100,0 % | 100,0 % | 100,0 % | 100,0 % | 100,0 % |  |
| 44 | `ar_new_iban` | ~290 | 0,0 % | 70,3 % | 70,3 % | 70,3 % | 70,3 % | 89,7 % | 91,4 % | 94,5 % | **+3,1** |
| 45 | `customer_advance` | ~280 | 100,0 % | 100,0 % | 100,0 % | 100,0 % | 100,0 % | 100,0 % | 100,0 % | 100,0 % |  |
| 46 | `ar_advance_deduction` | ~260 | 0,0 % | 57,0 % | 59,7 % | 61,6 % | 42,2 % | 94,3 % | 76,4 % | 93,2 % | **+16,8** |
| 47 | `ar_replaced_invoice` | ~260 | 0,0 % | 59,9 % | 77,8 % | 77,8 % | 76,7 % | 82,5 % | 82,5 % | 88,7 % | **+6,2** |
| 48 | `nonitem_fx_deal` | ~210 | 62,7 % | 62,7 % | 62,7 % | 100,0 % | 100,0 % | 100,0 % | n/v | 100,0 % |  |
| 49 | `ar_third_party_parent` | ~190 | 0,0 % | 16,0 % | 16,0 % | 16,0 % | 16,0 % | 25,1 % | 23,0 % | 77,0 % | **+54,0** |
| 50 | `ap_returned_transfer` | ~170 | n/v | n/v | 93,7 % | 93,7 % | 93,7 % | 93,7 % | 93,7 % | 93,7 % |  |
| 51 | `ar_short_bank_charges` | ~150 | 0,0 % | n/v | 70,1 % | 72,1 % | 69,4 % | 85,0 % | 81,0 % | 77,6 % | **−3,4** |
| 52 | `nonitem_card_expenses` | ~97 | 100,0 % | 100,0 % | 100,0 % | 100,0 % | 100,0 % | 100,0 % | 100,0 % | n/v |  |
| 53 | `ar_cheque_returned` | ~72 | 0,0 % | 0,0 % | 0,0 % | n/v | 0,0 % | 0,0 % | 95,8 % | 95,8 % |  |
| 54 | `ap_duplicate_payment` | ~70 | 0,0 % | 0,0 % | 0,0 % | 0,0 % | n/v | n/v | 85,7 % | 85,7 % |  |
| 55 | `ap_duplicate_refund` | ~67 | 34,3 % | 34,3 % | 34,3 % | 34,3 % | 34,3 % | 34,3 % | 86,6 % | 86,6 % |  |
| 56 | `ar_credit_note_paid_out` | ~43 | 0,0 % | 0,0 % | 0,0 % | 0,0 % | 0,0 % | 0,0 % | 0,0 % | 0,0 % |  |
| 57 | `nonitem_insurance` | ~32 | 81,2 % | 81,2 % | 81,2 % | 100,0 % | 100,0 % | 100,0 % | 100,0 % | 100,0 % |  |
| 58 | `ar_card_chargeback` | ~31 | 0,0 % | 0,0 % | 0,0 % | 0,0 % | 0,0 % | 0,0 % | 100,0 % | 100,0 % |  |
| 59 | `nonitem_interest_expense` | ~28 | 100,0 % | 100,0 % | 100,0 % | 100,0 % | 100,0 % | 100,0 % | 100,0 % | 100,0 % |  |
| 60 | `ar_1to1_remittance` | ~5 | 0,0 % | 100,0 % | 100,0 % | 100,0 % | 100,0 % | 100,0 % | 100,0 % | 100,0 % |  |


## 3. Mediciones locales en val (2025-01..06, sin envío)

> CLAUDE.md: cada cambio se mide con `scripts/eval.py --phase val`. A/B = misma base, solo cambia lo indicado.

| Fecha-Hora | Autor | Base | Cambio medido | Val A → B | Detalle |
| ---------------- | -- | -------- | ------------------------------------------------ | ------------: | ---------------------------------------- |
| 2026-11-07 13:47 | P4 | `8141660` | P4 (`1eed3ec`): UNIDENTIFIED solo sin tercero identificable + SUPPLIER_REFUND/CUSTOMER_REFUND + informe de fallos y avisos de registro | 70.41 → **70.68** (+0.27) | UNIDENTIFIED F1 0.722 → 0.860 (P 0.62 → 0.91, R 0.86 → 0.82); excF1 +0.012; SUPPLIER_REFUND recall 38 → 87 %; CUSTOMER_REFUND 344/344; comprobado en 2024 (`reports/P4_HISTORY.md`) |
| 2026-11-07 14:01 | P4 | `1eed3ec` | main completo (incluye `docs_card` cheques/lockbox de P2) | — → **71.38** | UNIDENTIFIED F1 0.859; txnAcc 0.881; precisión de asignación 0.903 (`subset_sum` 61 % de acierto: ver `reports/P4_QA.md`) |

- 2026-11-07 15:00 · P1 · val main c21c984 (docs_remit de P2 nuevo) = **85.73** (P 0.954 R 0.791; excF1 0.733). docs_remit 160.305 pares, 100 % correctos.
- 2026-11-07 15:10 · P1 · val main 9f7b6c5 (+ 1d8f290 de P4) = 85.63 (−0.10 frente a c21c984; P 0.954→0.951). Envío LIVE desde c21c984.
- 2026-11-07 15:30 · P1 · val main d1d399d (+ 001d6c2 docs_ap FX de P2 + 1d8f290 de P4) = **87.96** (P 0.955 R 0.853; txnAcc 0.934; excF1 0.746). ap_fx: faltan 12 filas FX (antes 44 k pares). DUPLICATE_PAYMENT P 0.28: docs_remit 1.117 y docs_ap 297 filas falsas.
