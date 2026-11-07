# Notas de P3 (PDFs y excepciones)

Ficheros: `bankrec/{pdf_text,pdf_facts,exceptions}.py` + herramienta `bankrec/exceptions_oracle.py`.

## pdf_text / pdf_facts

- `uv run python -m bankrec.pdf_text train live` precalienta la caché (`<data>/_cache/pdf_text_<carpeta>.parquet`;
  train 104 k PDFs en 27 s con 16 núcleos, live 3,5 s). Después `prepare` tarda 0,1 s. La caché se recalcula si cambia
  el nº de filas del `index.csv`.
- `ctx.shared["pdf_text"]`: folder, file, document_type, document_number, entity, party_id, document_date, text.
  Solo tipos invoice, credit_note, supplier_credit_note, proforma, bank_account_notice (los avisos de remesa en PDF son de P2).
- `ctx.shared["pdf_facts"]`: (document_type, document_number, entity, party_id, document_date, fact, value).
  Unir con `ctx.items` por `(entity, document_number)`. Hechos y formato de `value` en el docstring de `pdf_facts.py`
  (`instalment_plan, retention, advance, replaces, cancels_invoice, service_month, rebate, supplier_cn, new_iban,
  proforma_total`). Cobertura en TRAIN: 100 % de los PDFs con la frase correspondiente.
- **Los PDFs solo existen para un subconjunto de documentos** (≈2 % de las facturas): los "especiales" (plazos,
  retención, anticipo, sustituidas, rappel, avisos). No hay PDF de la mayoría de las cuotas de mantenimiento: el mes
  de servicio sale de `issue_date` (se factura a primeros del mes siguiente: emitida el 2024-11-01 → servicio 2024-10).

## exceptions.finalize

Contrato con los resolvers (también en el docstring):
- Alloc con `exception != ""` → fila decidida por el resolver (SHORT_DISPUTE de un aviso, RETURNED de un lote): se
  conserva con su importe. Las demás filas del movimiento se explican con el efectivo que sobra tras esas.
- Alloc sin excepción: `amount_cents` = **efectivo realmente aplicado a esa partida** si el resolver lo sabe (aviso de
  remesa: neto de descuento/retención) o el saldo abierto si no. `finalize` lo convierte en importe de salida
  (saldo completo en SHORT_*/FX/OVERPAYMENT, lo aplicado en PARTIAL) y decide la excepción.
- Tarjeta web (WEB_CARD) y recibos domiciliados (INSTALLER_DD) nunca llevan excepción salvo RETURNED.
- RETURNED: cobro con signo contrario al de la factura; DUPLICATE_PAYMENT: partida con saldo 0.

### Medido con el golden como resolver perfecto (`exceptions_oracle`, val 2025-01..06, 181 k movimientos)

| modo | acierto por movimiento | macro-F1 excepciones |
|---|---:|---:|
| resolver solo da partidas (importe = saldo) | 0,9934 | 0,864 |
| + el resolver pasa SHORT_DISPUTE y el efectivo neto de los avisos (`--lock SHORT_DISPUTE --explicit`) | 0,9988 | 0,994 |

Sin aviso de remesa se pierden SHORT_DISPUTE (no hay forma de verlo en el banco) y los descuentos/retenciones de
cobros con >16 facturas (subconjunto no identificable por suma). Eso depende de `docs_remit` (P2).

### Reglas aprendidas del golden (por si las necesita otro resolver)

- Descuento: dentro de plazo en TRAIN llega hasta **plazo + 9 días** (no +3) en cobros de varias facturas; en cobros
  de una factura el corte es exacto (≤ plazo → SHORT_DISCOUNT, ≥ plazo+10 → PARTIAL por total − descuento).
- Retención en origen: MX 1,25 % o 10 %; BR y MA 1,5 % o 10 % del total de la factura. En un movimiento con retención
  **todas** las facturas llevan SHORT_WITHHOLDING (salvo las en disputa) y los abonos van vacíos (100 % en val: 4.330 filas).
- Parciales en AR de varias partidas: fracciones del saldo 0,4/0,5/0,6 (PARTIAL) y 0,7/0,8/0,9/0,95/0,97
  (SHORT_DISPUTE); en AP 0,3/0,5/0,7.
- Plazos: la excepción vacía va al plazo "último" del plan (el de importe distinto) aunque se cobre antes que otros;
  el cobro del importe a pagar tras retención y el del neto tras anticipo son PARTIAL aunque la liberación ya se cobrase.
  Con plazos iguales sin excepción el cobro que cierra el saldo o llega tras el último vencimiento (95,7 % en TRAIN; el
  orden real de pago se desvía en un 5 %); `ar_instalment` queda en 98,9 % en el arnés.
- Cobros con partidas en varias divisas (pagos AP desde una cuenta): FX_DIFFERENCE por partida con divisa distinta de la cuenta.

## Idiomas de los PDFs y plantillas

- Las cabeceras cambian por país (RECHNUNG, FACTURE, FATTURA, FATURA, INVOICE, FACTURA) y el texto libre de hechos solo
  aparece en ES/EN en TRAIN. Los patrones de `pdf_facts` son estructurales y admiten variantes DE/FR/IT/PT/PL (sin
  verificar: no hay ejemplos). Hay dos plantillas de anulación: "Abono por anulación total de la factura X" y
  "CANCELLING OF INVOICE Nº X" (esta última, 2.200 abonos que antes no se detectaban).
- Si LIVE/FINAL traen otra plantilla, el hecho no se detecta: revisar con `uv run python -m bankrec.pdf_facts live`.

## Contrato en el pipeline real (comprobado con los resolvers actuales)

- Los resolvers dan `Alloc(item, |efectivo|)` (efectivo en divisa de la cuenta): `finalize` lo trata como efectivo aplicado.
- `docs_dd` da RETURNED con importe negativo: `finalize` lo pasa a positivo (antes el pipeline descartaba la fila).
  `ar_dd_returned` 0 → 0,968 en val.

## ar_short_discount / ar_short_withholding (pedido de P1)

Causa: `amount_match` solo busca partidas con saldo == cobro; un cobro con descuento llega como total − %, así que no
hay candidata y sale UNIDENTIFIED por el fallback (val completo: acierto 0,000 en 6.317 movimientos; no era `finalize`).
Arreglo: `bankrec/exceptions_amounts.py` (resolver `short_cash`, de P3) indexa el efectivo esperado con descuento o
retención y aplica el mismo criterio de tercero que `amount_match`. **P1 debe añadir
`"bankrec.exceptions_amounts"` a `registry.CASCADE` justo después de `"bankrec.resolvers.amount_match"`.**
Medido en val completo con la cascada modificada: ar_short_discount 0,000 → 0,907; ar_short_withholding → 0,869;
ar_discount_late → 0,551; SCORE 54,55 → 58,27; excF1 0,139 → 0,244.
Lo que queda (590 movimientos de ar_short_discount) son cobros de VARIAS facturas con descuento en una de ellas
(suma de saldos de un mismo tercero − descuento de una): hace falta un subset-sum por tercero (P2, `subset_sum`/`multi_ref`).
