# CLAUDE.md — bankrec (hackathon Close The Books, Grupo Valtermia)

Lo leen los 4 Claude Code del equipo. Fichero compartido: **solo lo edita P1**; pídele los cambios.
Fuente de verdad del reto: `bankrec-docs-and-masters/{README,FORMAT,POLICIES,DATA_DICTIONARY}.md` y
`starter/bankrec_score.py`. Si contradicen esto, mandan ellos (y avisa a P1). Detalle de datos: `docs/DATA_NOTES.md`.

## El reto en 1 minuto

- 17 sociedades (15 países; ZA01 nueva en FINAL), 12 divisas, 13 bancos, 47 cuentas. Para cada movimiento
  bancario: qué partidas abiertas (ARI/ARC/API/APC) liquida y por qué importe; si no liquida ninguna, su category;
  excepción por fila (lista cerrada).
- Fases: TRAIN 2021-07..2025-06 (con golden + `segment`), LIVE 2025-07..12 (ranking; **ya sin límite
  de envíos**), FINAL 2026-01..06 (datos a las 18:00, **un único envío puntuado**, cierre 19:30;
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
| P1 core | `bankrec/{config,contract,load,phase,ledger,refs,parties,pipeline,registry}.py`, `bankrec/resolvers/{text_ref,amount_match,multi_ref_fill,ranker}.py`, `scripts/run_phase.py`, `scripts/smoke.py`, `CLAUDE.md` |
| P2 documentos | `bankrec/resolvers/{docs_dd,docs_card,docs_card_chq,docs_remit,docs_remit_parse,docs_ap,multi_ref,subset_sum}.py` |
| P3 PDFs/excepciones | `bankrec/{pdf_text,pdf_facts,exceptions,exceptions_amounts,exceptions_oracle}.py`, `docs/P3_NOTES.md` |
| P4 QA/envíos | `bankrec/resolvers/{nonitem,nonitem_unmatched,router}.py`, `bankrec/validate.py`, `scripts/{eval,submit,p4_run,p4_category_audit}.py`, `tests/`, `reports/`, `docs/SCOREBOARD.md` |

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

## FINAL: guía de pasos (18:00 → 19:30, un único envío puntuado)

Ensayado a las 13:55 y a las 16:44 (con el arreglo de ZA01) usando LIVE como FINAL: válido y sin errores; 3-6 min según la carga del portátil.
Lo ejecuta P1. P4 hace a la vez lo mismo en su portátil como respaldo: mismo commit, mismos pasos.

0. **Código de FINAL = etiqueta `final` (`3956fad`, val 94,07, LIVE 93,83).** En los dos portátiles:
   `git fetch --tags && git checkout final && uv sync`. Lo que se subió a main después (P3: b0ad505, 67e72d3,
   0130440, 69d902a) NO está medido y NO entra.
1. **17:45**: congelar main. Último commit medido en val y en LIVE, anotado en `docs/SCOREBOARD.md`. Nadie hace push
   después sin avisar a P1.
2. **18:00**: descargar y descomprimir FINAL en `<data_dir>/final/`. Deben existir
   `final/bank_transactions.csv.gz`, `final/open_items_at_start.csv.gz`, `final/ar_invoices.csv.gz`…
   y `final/masters_final/*.csv`. Si el zip trae otra carpeta intermedia (`final/final/…`), mover el contenido.
   Ignorar los `._*`.
3. Comprobar fechas y volumen:
   `uv run python -c "from bankrec import load; t=load.table('final','bank_transactions'); print(t.height, t['booking_date'].min(), t['booking_date'].max())"`
   Deben ser de 2026-01-01 a 2026-06-30.
   Maestros de FINAL: deben incluir ZA01 y tipos de cambio de 2026.
   `uv run python -c "from bankrec import load; c=load.master('customers','final'); f=load.master('fx_rates','final'); print((c['entity']=='ZA01').sum(), f['date'].max())"`
   Debe dar más de 0 clientes de ZA01 y una fecha de 2026-06. Si no, revisar la ruta `final/masters_final/`.
   Borrar cachés de ensayos: `_cache/ranker_final_*`, `_cache/pdf_text_final.parquet` y `_cache/remit_v*_final_*`, si existen.
4. **Precalentar los PDFs** (multiproceso): `uv run python -m bankrec.pdf_text final`
   Si el portátil va justo de memoria, cerrar otros procesos antes: lanza un proceso por núcleo.
5. **Ejecutar**: `uv run python scripts/run_phase.py --phase final` → `out/final/submission.csv.gz` + `VALIDO`.
   - Tiempo esperado < 6 min. El ranker se reentrena con todo TRAIN.
   - Si un resolver falla, el log lo dice y la cascada sigue. Si algo lo rompe todo, repetir con `--disable <nombre>`.
6. Revisar el log:
   - número de movimientos por resolver, parecido en proporción a LIVE;
   - avisos «partidas abiertas sin historial»: normales para ZA01, que toma los atributos del propio fichero;
     masivos en otras entidades = algo raro;
   - ningún aviso «FINAL sin final/masters_final/» ni «fx_rates llega solo hasta…»;
   - `fallback` < 1 %;
   - ninguna línea «falló» ni «se desactiva» (sobre todo `ranker`: si faltan datos de TRAIN se desactiva y el
     fichero sale peor sin error visible).
7. Preparar el fichero (P4, a mano): `scripts/submit.py out/final/submission.csv.gz --phase final`.
   Comparar con el portátil de respaldo el SHA-256 del contenido descomprimido (ver «Ciclo de envíos»).
   `subset_sum` aún no es 100 % reproducible: si difieren pocas filas, vale el de P1. Subir **una vez**. Una validación rechazada no gasta el envío.
8. Anotar commit, SHA-256 y hora en `docs/SCOREBOARD.md`.

Drift ya cubierto: `masters_final` se usa fichero a fichero (si falta uno, el del repo); la filial nueva ZA01 entra
por la misma ruta que cualquier entidad (sus números "KWI…" se indexan aunque sean cortos). La cuenta nueva de
cobros de IT01 (2026-02-01) es nuestra: sus movimientos llevan entity IT01 igual que los de la antigua.

## Anti-overfitting (obligatorio)

Solo cuenta FINAL (2026-01..06), con drift: ZA01 nueva (ZAR, facturas "KWI…", renombrada 2026-04-01),
`final/masters_final/`, IT01 cambia de cuenta de cobros el 2026-02-01, clientes renombrados y pagos más lentos.
Val (TRAIN 2025-01..06) es una aproximación optimista, no el objetivo.

**Prohibido (fuga de etiquetas):**
- Leer `ctx.golden` o `golden_allocations` desde un resolver, `exceptions.py`, `pdf_*` o un modelo. Solo lo leen
  `scripts/eval.py` y los análisis offline. En live/final no existe: si tu código lo necesita, en FINAL falla.
- Usar la columna `segment` o `difficulty` como feature o regla: no existen fuera de TRAIN.
- Entrenar o calcular estadísticas con movimientos de la propia fase. Histórico = todo lo de
  `booking_date < ctx.start`:
  ```python
  hist = load.table("train", "bank_transactions").filter(pl.col("booking_date") < ctx.start)   # val: < 2025-01
  ```
  En live/final esto da TRAIN completo (no hay golden de LIVE).
- `sample` (2025-06) está DENTRO de val: no sirve como validación independiente.

**Prohibido (memorizar):**
- Hardcodear bank_txn_id, item_id, nombres, IBAN, importes o fechas concretas sacados de los fallos de val.
- Listas fijas de entidades, cuentas, bancos o divisas: léelas de `ctx.master(...)` (en FINAL vienen de
  `masters_final`). Formatos de número por entidad: regex general por familia, no lista cerrada (ZA01 trae "KWI…").
- Formatos de banco atados a fecha o cuenta: detecta por contenido (`statement_format`, texto). Moncayo pasa de
  N43 a camt.053 en 2025-10.

**Cómo ajustar sin sobreajustar:**
- Cada regla debe apoyarse en POLICIES.md (cita la sección en el docstring) o en un patrón que se repite en
  varias entidades y años de TRAIN. Antes de subirla, compruébala también en un año anterior con un análisis
  offline (duckdb sobre `golden_allocations` + `bank_transactions` de 2023 o 2024).
- Umbrales (fuzzy, confianza, ventanas de días): elige valores redondos con margen, no el óptimo exacto de val.
  Si la nota cambia > 0,3 puntos entre umbrales vecinos, la regla es frágil.
- Prefiere precisión a recall: un par mal asignado resta en precisión y además rompe el movimiento entero en txn
  accuracy; ante la duda, que pase al siguiente resolver o a UNIDENTIFIED/categoría.
- Mira P y R por separado y el informe por segmento, no solo la nota: una subida global que hunde otro segmento
  suele ser sobreajuste.
- Iterar con `--months 1` o `--months 2` y confirmar con los 6 meses completos antes de push.
- Árbitro externo: LIVE, ya sin límite de envíos. Si val sube y LIVE no, desconfía del cambio. Apunta las dos notas
  en `docs/SCOREBOARD.md`.
- **Ciclo de envíos sin límite**:
  - **Un cambio por envío**, siempre desde un commit de main con su val anotada. Si un envío mezcla dos cambios, no
    se sabe cuál funcionó.
  - **LIVE confirma, no se optimiza contra él.** No ajustes umbrales a base de reenviar a LIVE: FINAL es otro semestre
    y con drift. Una mejora debe subir val y LIVE a la vez.
  - **Si LIVE baja de forma consistente con un cambio, se revierte** aunque val suba.
  - **Comparar contenido, no ficheros**: dos portátiles con el mismo commit deben dar el mismo SHA-256 del CSV
    descomprimido.
    `uv run python -c "import gzip,hashlib,sys;print(hashlib.sha256(gzip.open(sys.argv[1]).read()).hexdigest())" <fichero>`
- Sesgo conocido de val (optimista): el conjunto de partidas abiertas excluye las cerradas fuera de banco en
  2025-H1, y los maestros son una foto de 2025-12-31 (nombres e IBAN "del futuro" respecto a val).

## Estado y siguientes pasos por rol (noche del 7-nov, tras FINAL)

**ÚLTIMO ENVÍO FINAL (cierre 19:30; nosotros 19:15)**
- Candidato: `5fde967` (P1) → backtest 2025-H1 94,01 → 95,01 y 2024-H1 94,64 → 95,54. P1 genera FINAL con él.
- Solo entra otro cambio si está en main **antes de las 19:00** con su backtest 2025-H1 (y 2024-H1 si da tiempo)
  mejor que 95,01 en el mensaje del commit. Avisa a P1. Lo que llegue después no entra.
- P4: subir a LIVE `out/bt_p1b_live/live/submission.csv.gz` del portátil de P1 como confirmación (no gasta FINAL).

**Dónde estamos**
- FINAL = etiqueta `final` (`3956fad`): val 94,07 · LIVE 93,83 (puesto 8). El primero del ranking: 96,38.
- `7a8701e` (incluye `5e5a0d1`: subset_sum/multi_ref_fill/ranker se abstienen ante ambigüedad): val 94,01
  (P 0,998 · R 0,887): −0,06 frente a `final`.
- P1 (en local, se sube tras medir 2024-H1): `multi_ref_cited` + `parties` nombres cortos + `_overpay_guard` +
  `/CHGS/` + `docs_remit` números cortos → val **94,47** (+0,46 sobre `7a8701e`). Falta medir el post-pass.
- **Plan completo, cifras y evidencia de cada tarea: `docs/PLAN_MEJORAS.md`.** Léelo antes de empezar.

**Qué sabemos (vale para todos)**
- Falta recall, no precisión. 80 % de los pares que faltan (64,7 k de 80,5 k en val) están en ~6.500 cobros que
  `nonitem_unmatched` deja en `CUSTOMER_ADVANCE` sin excepción: cliente identificado, ninguna partida aplicada.
- En un movimiento que ya está perdido para txn accuracy, añadir pares con precisión > 0,47 sube el Allocation F1
  (F1 = 2·TP/(pred+gold)). «Precisión ante todo» sigue valiendo para los movimientos que hoy salen bien y para todo
  lo que consume saldo del ledger antes que el movimiento correcto.
- Medir SIEMPRE en dos semestres, con salidas aisladas (no pisan `out/val`):
  `uv run python scripts/backtest.py --start 2025-01-01 --end 2025-07-01 --out out/bt_<tuyo>_2025h1` (= val) y
  `--start 2024-01-01 --end 2024-07-01 --out out/bt_<tuyo>_2024h1` (anti-overfitting). Entra si sube en 2025-H1 y
  no baja en 2024-H1; después LIVE. Corre también la base de tu commit para comparar en tu portátil.

**P1** — integra, mide y mantiene `docs/SCOREBOARD.md`.
1. Subir lo medido: `multi_ref_cited` (citadas inequívocas de un cobro mayor), `pipeline_post` (suma exacta única
   sobre el ledger final para los cobros en CUSTOMER_ADVANCE; offline 8,7 k pares al 96,5 %, 1,6 k movimientos
   exactos), `_overpay_guard`, `parties`, `amount_match.pick_month`, y los de `docs_remit`/`exceptions` (abajo).
2. `5e5a0d1` restó en val (−0,06) y en LIVE (#13: −0,09): probar a revertir su `multi_ref_fill`/`ranker` encima de
   lo anterior y, si sube en los dos semestres, revertir.
3. Probar `multi_ref_cited` antes del `ranker` (el ranker pone 1 sola factura en cobros de varias: 218 txns).

**P2** — detalle en `docs/PLAN_MEJORAS.md` §3 P2.
1. FX en `subset_sum` y `multi_ref`: si `amount > 0` y `orig_currency != currency`, objetivo = `abs(orig_amount_cents)`
   y partidas en `orig_currency` (`multi_ref.py:210/214/250/268`, `subset_sum.py:213/226`). Val ≈ +0,07; 2024: 124/125.
2. `multi_ref_fill.py:95`: si las citadas son de varios terceros, elegir el de `parties.score ≥ 80` con más citadas e
   intentar completar (218 txns). Fichero de P1: avisa antes de tocarlo.
3. `subset_sum`: última etapa sobre todas las partidas del tercero hasta ~36 (hoy solo las 24 más antiguas), solución
   única y LAM bajo (≈ +600 pares).
4. Con P3: cobros de varias facturas con descuento (280 txns, 2-5 facturas, una descontada) o retención (118 txns).
5. Revisar el cambio de P1 en `docs_remit._map_rows` (números de 1-4 dígitos del layout nuevo; LIVE 479/479).

**P3** — detalle en `docs/PLAN_MEJORAS.md` §3 P3.
1. Revisar el cambio de P1 en `exceptions.cross_border` (`/CHGS/` = SHORT_BANK_CHARGES, 108/108 en TRAIN).
2. Regularización de disputa (`exceptions.py:425`): `REGULARIZACION|SETTLEMENT` (no "CARD SETTLEMENT") y una sola
   factura viva → excepción vacía, importe = efectivo (TRAIN 5.125 filas, todas así).
3. Comprobar `finalize` sobre las propuestas nuevas de P1 (`multi_ref_cited`: suma < cobro; `pipeline_post`: suma
   exacta): nada de PARTIAL/SHORT_*/OVERPAYMENT espurios; FX_DIFFERENCE solo en facturas.
4. Con P2 (tarea 4): importe = saldo completo con SHORT_DISCOUNT/SHORT_WITHHOLDING; en FX, FX_DIFFERENCE.
5. `ar_dd_returned` (47 FN/47 FP) y `ar_dd_represented_transfer` (32/29): sin mirar.

**P4** — detalle en `docs/PLAN_MEJORAS.md` §3 P4.
1. UNIDENTIFIED en `nonitem_unmatched`: veto si el texto trae un número impreso (TRAIN: 0 UNIDENTIFIED entre 395 k),
   y CUSTOMER_ADVANCE → UNIDENTIFIED solo con tercero por nombre/ref (nunca IBAN) y efectivo redondo
   (`cash % 10000 == 0`) o < 2 × la factura viva más pequeña (325 txns: 272 TP / 53 FP). Va después del post-pass de
   P1 en la práctica: no muevas cobros que el post-pass resuelve (compruébalo con el audit, resolver `postpass_subset`).
2. Cobros de una sola factura en el cubo CUSTOMER_ADVANCE: `short_cash._shapes` sobre `f["cli"] ∪ f["iban"]` (sc ≥ 90)
   con acierto único (OVERPAYMENT 34/6, saldo exacto 27/0, parcial 17/1).
3. LIVE como árbitro de cada cambio; SCOREBOARD con val, 2024-H1 y LIVE.

**Reglas**
- Un cambio por commit, `smoke.py` en verde, `git pull --rebase` antes de push. Ficheros de otro rol: avisa a su dueño.
- Cada commit con su nota en 2025-H1 y 2024-H1 en el mensaje o en el SCOREBOARD.
