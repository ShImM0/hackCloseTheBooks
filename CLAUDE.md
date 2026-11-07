# CLAUDE.md — bankrec (hackathon Close The Books, Grupo Valtermia)

Lo leen los 4 Claude Code del equipo. Fichero compartido: **solo lo edita P1**; pídele los cambios.
Fuente de verdad del reto: `bankrec-docs-and-masters/{README,FORMAT,POLICIES,DATA_DICTIONARY}.md` y
`starter/bankrec_score.py`. Si contradicen esto, mandan ellos (y avisa a P1). Detalle de datos: `docs/DATA_NOTES.md`.

## El reto en 1 minuto

- 17 sociedades (15 países; ZA01 nueva en FINAL), 12 divisas, 13 bancos, 47 cuentas. Para cada movimiento
  bancario: qué partidas abiertas (ARI/ARC/API/APC) liquida y por qué importe; si no liquida ninguna, su category;
  excepción por fila (lista cerrada).
- Fases: TRAIN 2021-07..2025-06 (con golden + `segment`), LIVE 2025-07..12 (ranking; 20 envíos/día, uno cada 3 min;
  pedir upload-url gasta hueco), FINAL 2026-01..06 (datos a las 18:00, **un único envío puntuado**, cierre 19:30;
  un fichero rechazado por validación no cuenta y se puede resubir; empate → gana el envío más temprano; revelación 20:30).
- Nota = 100 × (0,5·Allocation F1 + 0,3·Txn accuracy + 0,2·Exception macro-F1). Tolerancia |Δ| ≤ max(0,01; 0,5 %).
  - Allocation F1: pares (txn, item) con importe en tolerancia. No mira la excepción.
  - Txn accuracy: TODAS las filas del movimiento bien (item, importe, excepción) o category+exception si no hay partida.
  - Exception macro-F1: **por fila**, media de las 10 clases presentes. Las clases raras pesan igual que las comunes.
- Envío: CSV UTF-8 (.csv.gz), cabecera `bank_txn_id,item_id,amount,category,exception`, una fila por par;
  sin partida → una fila con item_id y amount vacíos y category; no identificado → `UNIDENTIFIED,UNIDENTIFIED`.
  amount positivo, divisa de la partida, 2 decimales (estilo). Rechazo: txn ajeno, item_id mal formado, par
  duplicado, mezcla partida+categoría, >1 fila sin partida, category/exception fuera de lista, >60 MB o >3 M filas.
  item_id inexistente con patrón válido NO rechaza: es un FP.
- Importe por excepción (POLICIES §2): saldo abierto completo en SHORT_DISCOUNT, SHORT_BANK_CHARGES,
  SHORT_WITHHOLDING, FX_DIFFERENCE, OVERPAYMENT; lo aplicado en PARTIAL y SHORT_DISPUTE; lo pagado otra vez en
  DUPLICATE_PAYMENT; lo reabierto en RETURNED. Abonos (ARC/APC) en su fila, positivos, sin excepción.
  Tarjetas: una fila por pedido al bruto, sin excepción. FX_DIFFERENCE sustituye a vacío/SHORT_* en facturas
  totalmente liquidadas en otra divisa; PARTIAL/SHORT_DISPUTE se mantienen.
- Reglas clave: FIFO por fecha de factura si hay varias iguales sin referencia, salvo que el texto cite el mes de
  servicio; "anula y sustituye a X" → se liquida la sustituta; pagadores alternativos (matriz `parent_customer_id`,
  confirming, factoring, agencias "EXPTE"); ES00 paga proveedores de ES01/ES02/PT01; UNIDENTIFIED solo si no existe
  partida (no forzar: hay "lookalikes" con el importe de otra factura).
- El banco no trae item_id: solo el número impreso (FV25-0400572, AB24-…, FA/AV…, TW22-…, numéricos US…), a menudo
  sin prefijo, con erratas o cortado a 16/35/38 caracteres; N43 parte el concepto en trozos de 38 unidos por ` | `.

## Propiedad de ficheros (cada uno toca solo lo suyo)

| Rol | Ficheros |
|---|---|
| P1 core | `bankrec/{config,contract,load,phase,ledger,refs,parties,pipeline,registry}.py`, `bankrec/resolvers/{text_ref,amount_match,ranker}.py`, `scripts/run_phase.py`, `scripts/smoke.py`, `CLAUDE.md` |
| P2 documentos | `bankrec/resolvers/{docs_dd,docs_card,docs_remit,docs_ap,multi_ref,subset_sum}.py` |
| P3 PDFs/excepciones | `bankrec/{pdf_text,pdf_facts,exceptions}.py` |
| P4 QA/envíos | `bankrec/resolvers/{nonitem,router}.py`, `bankrec/validate.py`, `scripts/{eval,submit}.py`, `reports/`, `docs/SCOREBOARD.md` |

Compartidos (`contract.py`, `pipeline.py`, `registry.py`, `CLAUDE.md`): solo P1. Para un cambio, pídelo.
Ficheros nuevos propios (helpers, tests) sí: ponles tu prefijo de módulo (p. ej. `bankrec/resolvers/docs_dd_parse.py`).

## Contrato (`bankrec/contract.py`)

```python
Alloc(item_id: str, amount_cents: int, exception: str = "")
Proposal(bank_txn_id, allocs: list[Alloc], category="", exception="", confidence=0.0, resolver="", reason="")
```
Cada módulo de la cascada (`registry.CASCADE`) define:
- `NAME`;
- `prepare(ctx)`: indexa una vez por fase y guarda en `ctx.shared[NAME]`;
- opcional `resolve_batch(ctx) -> dict[bank_txn_id, Proposal]` (joins vectorizados, sin ledger);
- opcional `resolve(txn: dict, ctx, ledger) -> Proposal | None`.

Pipeline (`pipeline.run`): movimientos en orden (booking_date, bank_txn_id); para cada uno, el primer resolver que
devuelve Proposal gana → `exceptions.finalize(prop, txn, ctx, ledger)` (P3) → limpieza (items conocidos, sin
duplicados) → `ledger.apply`. Sin propuesta: abono → UNIDENTIFIED, cargo → BANK_FEES. Cada resolver va en
try/except: si falla se registra y la cascada sigue. Nunca falta un bank_txn_id.

`ctx` (`bankrec.phase.Phase`): `name, folder, dir, start, end, txns, items, golden, shared`, `ctx.master("customers")`,
`ctx.docs("direct_debit_batches")`. `ctx.items` (una fila por item_id): `item_id, item_type, entity, party_id,
party_name, document_number, issue_date, due_date, currency, total_cents, open_cents, payment_terms, po,
payment_method, related_document, reason`. Incluye las abiertas al inicio, las emitidas en la fase y las
cerradas recientes (`open_cents=0`, 240 días) para RETURNED y DUPLICATE_PAYMENT. `txn` es un dict con las columnas de bank_transactions + `amount_cents`
(con signo, divisa de la cuenta) y fechas como `date`. `ledger.open[item_id]` = saldo vivo en céntimos.

Importes SIEMPRE en céntimos `int`. Nunca floats para comparar ni sumar.

Fases: `sample` (2025-06, golden), `val` (TRAIN 2025-01..06 con partidas abiertas reconstruidas; modelos solo con
datos < 2025-01), `live`, `final`. Salidas en `out/<fase>/submission.csv.gz` y `audit.parquet`.

## Comandos (idénticos en Windows y Linux)

```
uv sync                                                        # entorno
uv run python scripts/smoke.py                                 # sample + validador + scorer (< 2 min). Verde antes de push
uv run python scripts/run_phase.py --phase val [--months 1] [--disable a,b]
uv run python scripts/eval.py --phase val [--segment ar_1to1] [--dump 30]   # métricas + por segmento + fallos .md
uv run python scripts/run_phase.py --phase live                # out/live/submission.csv.gz (+ validación)
uv run python -m bankrec.validate out/live/submission.csv.gz --phase live
```

## Multiplataforma

- Siempre `uv run python scripts/...` o `uv run python -m bankrec...`. Nada de .sh ni .ps1.
- `pathlib` en todo; rutas de los datos (`pdfs/ES01/x.pdf`) se unen con `Path`; respeta mayúsculas (Linux distingue).
- Datos: `BANKREC_DATA` o `bankrec.local.toml` (`data_dir = "C:/Users/yo/CTB"`, no se sube). Por defecto `./data`.
  Maestros, sample y starter están en el repo (`bankrec-docs-and-masters/`).
- `encoding="utf-8"` siempre explícito; con `csv`, `newline=""`; CLI: `sys.stdout.reconfigure(encoding="utf-8")`
  (con `python -I` se ignora `PYTHONIOENCODING`). N43 en bruto es latin-1. CSV de salida con `\n`.
- Multiproceso: guarda `if __name__ == "__main__":`, workers a nivel de módulo, pasa rutas y no DataFrames (spawn).
- Lee CSV con `bankrec.load` (todo texto + `*_cents` + fechas, caché parquet). Ignora ficheros `._*`.
- Python 3.12 + uv + `uv.lock` en el repo. Dependencia nueva: pídesela a P1 (toca pyproject/uv.lock).

## Reglas de trabajo

- **Nunca** envíes nada a la plataforma ni llames a su API; no toques tokens. Solo P4 envía, a mano.
- Antes de cada push: `git pull --rebase` y `smoke.py` en verde. main siempre ejecutable. Commits pequeños.
- Mide cada cambio con `eval.py --phase val` y apunta la nota en `docs/SCOREBOARD.md`.
- Lo mínimo que funcione: nada de UI ni abstracciones que no se usen hoy.
- Comentarios y docstrings en español.
- Nunca vuelques ficheros grandes al contexto: cabeceras, esquemas y ≤ 20 filas. Usa duckdb/polars para agregados.
- Antes de pedir permiso para un comando, explica en una línea qué hace.
- Presupuesto: LIVE completa (203 k movimientos) en < 10 min en un portátil.

## Si eres P2 / P3 / P4, empieza por…

**P2 (documentos y agregados 1:N)**
1. `docs_dd.py`: abono de remesa ↔ `direct_debit_batches` (cuenta, fecha, batch_total) → todos los recibos;
   recibo → partida (`end_to_end_id`/`remittance_info`/mandato). Segmento `ar_dd_batch` (713 k pares).
   Después devoluciones R → RETURNED (`ar_dd_returned`).
2. `docs_ap.py`: cargos ↔ `payment_runs` (transfers por end_to_end_id, aggregated/confirming por total y fecha).
   Segmentos `ap_payment_multi`, `ap_payment_single`, `ap_credit_note_netted`, `ap_paid_by_holding`, `ap_confirming`.
3. `docs_card.py` (`ar_card_settlement`) y cheques; luego `docs_remit.py` (`ar_1toN_remittance`, `ar_short_dispute`).

**P3 (PDFs y excepciones)**
1. `exceptions.py`: SHORT_DISCOUNT (payment_terms + 3 días de gracia, cash = total − % × total), FX_DIFFERENCE
   (divisa del movimiento ≠ divisa de la partida), PARTIAL (aplicado < saldo). Son ~300 k filas de TRAIN.
2. `pdf_text.py`: texto de `pdfs/index.csv` a parquet en caché (multiproceso, mide tiempo en LIVE).
3. `pdf_facts.py`: plazos, retención, anticipo, "anula y sustituye", mes de servicio, IBAN nuevo, cesión →
   segmentos `ar_instalment`, `ar_retention*`, `ar_replaced_invoice`, `ar_advance_deduction`, `ar_new_iban`.

**P4 (categorías, QA, envíos)**
1. Enviar a mano `out/live/starter.csv.gz` (baseline) y apuntar la nota en `docs/SCOREBOARD.md`.
2. `nonitem.py`: categorías por bank_code/texto (BANK_FEES 70 k, CASH_POOL 12,5 k, PAYROLL…); UNIDENTIFIED solo
   para cobros sin partida (es una clase del macro-F1: el fallback actual la sobre-predice).
3. `eval.py`/`validate.py`: informe por segmento, `--dump` con candidatas; checklist FINAL en `submit.py`.
