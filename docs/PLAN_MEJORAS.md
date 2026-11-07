# Plan de mejoras tras FINAL (análisis de P1, 2026-11-07 noche)

Base: val de `3956fad` (= etiqueta `final`) 94,07 · LIVE 93,83 (top 1 del ranking: 96,38).
Análisis offline sobre `out/val` (submission + audit + golden). Cifras de val (TRAIN 2025-01..06); cada propuesta lleva
su comprobación en 2024 cuando se pudo hacer. Son estimaciones de primer orden: **se confirman con `backtest.py`**.

## 1. Dónde están los puntos

- Pares: TP 654.929 · FP 2.108 · FN 80.545. La precisión (99,7 %) sobra; **falta recall (0,89)**.
- **64.670 de los 80.545 FN (80 %) están en 6.524 cobros que `nonitem_unmatched` deja en `CUSTOMER_ADVANCE` sin
  excepción** (cobro identificado sin partida). 5.982 tienen partidas en el golden y 542 son UNIDENTIFIED de verdad.
  Ninguno puntúa en txn accuracy: ya están perdidos.
- **Regla de oro para esos movimientos perdidos:** Allocation F1 = 2·TP / (pred + gold). Añadir pares con precisión q
  sube el F1 si q > F1/2 ≈ 0,47, y no cuesta txn accuracy. La regla «precisión ante todo» sigue valiendo para los
  movimientos que hoy salen bien y para lo que consume saldo del ledger antes que el movimiento correcto.
- Cobros sin aviso ni referencia que pagan 5-80 facturas al azar entre 30-800 abiertas del cliente: el conjunto NO es
  FIFO (161 de 227 con ≥ 8 facturas antiguas saltadas) y hay facturas "zombi" abiertas para siempre. Solo se recuperan
  por suma exacta cuando la solución es única.
- Excepciones: solo 571 filas tienen el par bien y la excepción mal. El resto del recall perdido por clase son pares
  que faltan (FX_DIFFERENCE 2.436 filas, PARTIAL 1.012, SHORT_WITHHOLDING 645, SHORT_DISCOUNT 622, SHORT_DISPUTE 535).

## 2. Cómo medir (obligatorio para cada cambio)

```
uv run python scripts/backtest.py --start 2025-01-01 --end 2025-07-01 --out out/bt_<tuyo>_2025h1   # = val
uv run python scripts/backtest.py --start 2024-01-01 --end 2024-07-01 --out out/bt_<tuyo>_2024h1   # anti-overfit
```
- Salidas aisladas (no pisan `out/val`); al final imprime la nota de `eval.py`.
- Un cambio solo entra si sube en 2025-H1 **y** no baja en 2024-H1. Después, LIVE como árbitro.
- Compara contra la base del mismo commit en tu portátil (corre la base una vez con el mismo comando).

## 3. Tareas

### P1 (en curso en este portátil)
- `multi_ref_cited` (nuevo, antes de `nonitem_unmatched`): cobros que citan algunas facturas y cuya suma no llega.
  Asigna solo las citadas inequívocas. Offline: ~5.000 pares, 98,5 % (2024: 98,7 %).
- `pipeline._overpay_guard`: no borrar OVERPAYMENT con exceso de forma reconocible (`overpay_shape`). +26 TP / +5 FP.
- `parties`: nombres que sin forma jurídica quedan en < 5 letras ("AÁS Spa S.L.") se comparan enteros (175 clientes
  con score 0).
- Post-pass de suma exacta única sobre los cobros que siguen en `CUSTOMER_ADVANCE`.
- Hecho en ficheros de otros (aviso a sus dueños): `docs_remit` números cortos del layout nuevo (P2) y `/CHGS/` en
  `exceptions.cross_border` (P3). Ver abajo.

### P2 — `subset_sum`, `multi_ref`, `multi_ref_fill` (de P1, pero la tarea 2 es tuya con P1)
1. **FX en `subset_sum` y `multi_ref` (≈ +0,07).** Hoy filtran partidas por la divisa de la cuenta y usan el importe de
   la cuenta como objetivo (`multi_ref.py:210/214/250/268`, `subset_sum.py:213/226`), así que nunca ven un cobro FX.
   Si `amount > 0` y `orig_currency != currency`: objetivo = `abs(orig_amount_cents)`, divisa de partida =
   `orig_currency`. `finalize` ya lee orig (`exceptions.py:307, 407-426`): facturas FX_DIFFERENCE, abonos vacíos.
   - Datos: todos los cobros en otra divisa traen `orig_amount`/`orig_currency` (= divisa de factura, 100 %).
     `orig_amount` = suma exacta del golden en 88 % (el resto: descuento CA 2 % y retenciones MX/MA).
   - Val: subset_sum +81 txns / +344 pares / 0 falsos; multi_ref +40 txns / +168 pares, falsos 3 → 0.
     2024-H1: 124 de 125 bien.
   - Suma exacta única fiable con n ≤ 20 partidas abiertas (≤ 365 días); n 21-25 a medias; > 30 casi siempre ambigua.
2. **Cuando el texto cita partidas de varios terceros, `multi_ref_fill.py:95` devuelve None** y el ranker pone 1 sola
   factura (218 txns, 6,8 k pares del golden). Elegir el tercero con `parties.score ≥ 80` y más citadas e intentar
   completar. `multi_ref_cited` ya recoge las citadas sueltas; esto solo añade los movimientos que se completan.
   Coordinar con P1 (dueño del fichero).
3. **`subset_sum` mira solo las 24 facturas más antiguas** (`subset_sum.py:50` `STAGES`, `:248` `k+len(notes)>30`).
   Una última etapa sobre todas las partidas del tercero hasta ~36 con solución única y LAM bajo: ≈ +600 pares.
   Riesgo medio: las "lookalike" (UNIDENTIFIED con importe de otra factura) dan falsos.
4. **Variantes con deducción en `subset_sum`** (POLICIES §3): 280 txns SHORT_DISCOUNT de 2-5 facturas con exactamente
   una descontada; 118 txns SHORT_WITHHOLDING (MX/BR/MA, 1,25/1,5/10 %) hasta 10 facturas; 32 cobros FX con
   retención. Objetivo = Σ saldo − descuento/retención de una o de todas. Necesita a P3 para la excepción.
5. **Hecho por P1 en `docs_remit.py` (revisar):** el layout nuevo de avisos (sep-2025, POLICIES §5.4/§6) trae nuestro
   número sin prefijo ni ceros, 1-5 dígitos; `_map_rows` solo casaba sufijos ≥ 5. Ahora casa 1-4 dígitos si el
   importe es exacto, es sufijo y lleva un 0 justo antes. LIVE: 479 de 479 filas casan, todas del tercero del aviso
   (antes 0). FINAL: 2.141 filas, 1.250 retenciones BR01 al 10 % (SHORT_WITHHOLDING). 2024 simulado: 64.363/64.363.
   No hace falta subir la versión de caché.

### P3 — `exceptions*.py`
1. **Hecho por P1 (revisar):** `cross_border` acepta `/CHGS/` (etiqueta de gastos MT940). TRAIN: 108 de 108
   movimientos con ella son SHORT_BANK_CHARGES. Val: 12 PARTIAL → SBC, F1 0,843 → 0,885 (≈ +0,085).
2. **Regularización de disputa** (`exceptions.py:425`, antes del paso 1 del defecto): texto `\bREGULARIZACION\b|
   \bSETTLEMENT\b` (no "CARD SETTLEMENT") y una sola factura viva → excepción vacía con importe = efectivo.
   TRAIN: 5.125 filas, todas `ar_dispute_settlement` con "" o FX. Val: 61 ""→PARTIAL corregidas (≈ +0,015).
3. **Revisar `finalize` sobre propuestas incompletas** de `multi_ref_cited` (suma < cobro): no debe poner PARTIAL,
   SHORT_* ni OVERPAYMENT; en FX, FX_DIFFERENCE en facturas y vacío en abonos.
4. **Con P2 (tarea 4):** descuento/retención en cobros de varias facturas → importe = saldo completo con
   SHORT_DISCOUNT/SHORT_WITHHOLDING; en FX, FX_DIFFERENCE (sustituye a SHORT_*) pasado `exceptions.py:424`.
5. Sin mirar aún: `ar_dd_returned` (47 FN / 47 FP) y `ar_dd_represented_transfer` (32/29).

### P4 — `nonitem_unmatched`, `nonitem`, QA
1. **UNIDENTIFIED (≈ +0,1 con txn accuracy).**
   - Veto en `decide()`: si el texto trae un número impreso (`[A-Z]{1,3}\d{2}[-/]?\d{4,}`, `INV|FV|FA|FACT…\s*\w*\d`,
     `N/REF`, `EXPTE`, `CONFIRMING`) nunca UNIDENTIFIED. TRAIN 2021-25: 0 UNIDENTIFIED entre 395 k movimientos que
     casan. Val: −71 de los 251 FP.
   - Pasar de CUSTOMER_ADVANCE a UNIDENTIFIED (solo tercero por nombre/ref, nunca por IBAN: 709 txns, 0 UNIDENTIFIED),
     tras el veto: efectivo redondo (`cash % 10000 == 0`: 106 txns, 105 TP) o efectivo < 2 × la factura viva más
     pequeña de los candidatos en esa divisa. Juntas: 325 txns, 272 TP / 53 FP. Umbral redondo 2 (pico en ≈ 3,7).
   - `NAME_LEN` 8 → 7 (línea 59): 100 de los 180 FP restantes tienen pagador "XXX SPA S.L.". Sin probar.
2. **Una sola factura en el cubo CUSTOMER_ADVANCE:** antes de devolver PENDING, `short_cash._shapes` sobre los
   candidatos `f["cli"] ∪ f["iban"]` (sc ≥ 90), solo si hay un único acierto. Simulado (TP/FP): OVERPAYMENT 34/6,
   saldo exacto 27/0, parcial 17/1, SBC 5/1 (≈ +0,1).
3. Medir cada cambio con los dos backtests y LIVE; mantener `docs/SCOREBOARD.md`.

## 4. Descartado (medido)
- Adivinar facturas por FIFO/vencimiento en el cubo CUSTOMER_ADVANCE: precisión 0,30-0,44 (< 0,47). No.
- Voto por marginales entre soluciones exactas múltiples: 0,29-0,55. No.
- Quitar el relleno con ventana de `multi_ref_fill` (`len(free) > MAX_FREE`): sus pares aciertan 58-75 % (> 0,47);
  quitarlo baja el F1.
- Regla de edad en `ar_same_amount_fifo`: pierde más de lo que arregla (T=90 días: −437 / +60).
- Erratas por Levenshtein en referencias: 0,91 en val, 0,98 en 2024 (inestable).
- `docs_remit` en val ya casa todos los avisos (181.368/181.368 pares): el cubo CUSTOMER_ADVANCE no es de avisos.
