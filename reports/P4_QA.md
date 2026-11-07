# P4: categorías, QA y preparación manual

Cambios limitados a los módulos de P4 y sus pruebas. No se ha enviado nada a la plataforma.

## Generalización y procedencia

Las categorías siguen `bankrec-docs-and-masters/POLICIES.md` §4 y el maestro `categories.csv`.
Las expresiones textuales son heurísticas de desarrollo, no un vocabulario completo ni reglas garantizadas
por el contrato. No hay decisiones por identificador, importe concreto, año, sociedad o nombre del sample.
El resolver no consulta golden, segmentos ni dificultad. El código bancario se conserva como evidencia,
pero no determina por sí solo una categoría: `DATA_DICTIONARY.md` advierte de usos inconsistentes.

`TRASPASO PROPIO` / `TRASPASO ENTRE CUENTAS` solo es CASH_POOL en cuentas marcadas como cabecera o participante
de cash pooling en `bank_accounts.purpose` (POLICIES §4; en FINAL se lee de `masters_final`): sin esa evidencia
del maestro no se distingue de un traspaso propio. En 2023 y 2024 el 100 % de esos textos en cuentas del pool
son CASH_POOL y el 100 % del CASH_POOL del golden está en cuentas del pool. Intereses requieren
una descripción de liquidación/pago, y las menciones explícitas de factura o deducción se dejan a los
resolvers de partidas. Reembolsos de notas de crédito no se convierten en categorías genéricas.
Una anulación bancaria explícita puede identificar el original solo si existe una candidata única de
igual cuenta y divisa, importe opuesto y concepto coincidente, entre 0 y 7 días antes. Esta ventana es
una heurística conservadora, no un límite contable aprendido ni garantizado.

`confidence=0.9` es una prioridad heurística, no una probabilidad calibrada. Las abstenciones permiten
que la cascada continúe. El fallback compartido sigue siendo de P1 y P4 no ha modificado ese fichero.

## UNIDENTIFIED y categorías del último paso (`nonitem_unmatched`, registrado por P1)

Es el único sitio donde P4 decide UNIDENTIFIED (P2 pidió que esa lógica no salga de este módulo; el fallback de
`pipeline.py` solo ve lo que este paso deja pasar). Reglas, por orden (POLICIES §3, §4, §5.1, §5.5):
1. abono agregado (tarjetas/TPV, remesa, lockbox, cheques) → CUSTOMER_ADVANCE (100 % con partidas en 2023/24);
2. ordenante = proveedor sin abono APC abierto, con texto de reembolso o que cita una factura suya → SUPPLIER_REFUND;
3. existe partida probable → CUSTOMER_ADVANCE sin excepción (cobro identificado pendiente de aplicar): IBAN de un
   tercero con partidas abiertas, número impreso de una partida abierta citado, o nombre que identifica a un tercero
   con partidas abiertas. El nombre identifica si es de empresa (forma social en el maestro) con ≥ 2 palabras o de
   particular con ≥ 3; y si el banco trae la contraparte, debe coincidir entera (un nombre contenido en otro más
   largo es otra empresa: «POOL BENNETT» dentro de «POOL & SPA BENNETT HERMANOS CO.»);
4. si no, UNIDENTIFIED en category y exception; el audit guarda el candidato descartado y el motivo, y todas las
   señales en JSON («motivo | JSON») para ajustar sin reejecutar.
Cargos: CUSTOMER_REFUND si el beneficiario es un cliente sin abono ARC abierto y ningún proveedor encaja; el resto
va al fallback (BANK_FEES). En `nonitem`: abono con PAGO DUPLICADO / PAGO DOBLE / DUPLICATE PAYMENT →
SUPPLIER_REFUND (153 de 153 en 2023/24; antes solo se detectaban 81).

Cómo se eligió: versión instrumentada con todas las señales en el audit, una ejecución de val y simulación exacta de
cada variante (el paso es el último y no toca el ledger: Δnota = 30·Δaciertos/N + 2·ΔF1 de UNIDENTIFIED). La
simulación predijo F1 0,860 y la ejecución real dio 0,860. Descartadas por empeorar: exigir partidas emitidas antes
del cobro (−0,01: recupera 38 UNIDENTIFIED reales y añade 225 falsos), endurecer el número citado (−0,03 a −0,05)
y exigir la misma forma social en texto y maestro (el banco la escribe distinto: separa peor que sin ella).
Sensibilidad: parecido del nombre 85/90/95/100 → +0,19/+0,25/+0,25/+0,24; contraparte completa 80…100 →
+0,23…+0,25; particular ≥ 4 palabras → +0,24. Estable (< 0,3 entre vecinos).

A/B en val completo sobre la misma base (main 8141660): **70,41 → 70,68 (+0,27)**. UNIDENTIFIED F1 0,722 → 0,860
(P 0,621 → 0,910; R 0,863 → 0,816), macro-F1 de excepciones +0,012, precisión de asignación +0,003 (los cobros con
PAGO DUPLICADO dejan de casarse mal), txn accuracy −0,001 (616 UNIDENTIFIED reales con nombre de empresa que
coincide con un cliente pasan a CUSTOMER_ADVANCE: coste asumido de la regla). SUPPLIER_REFUND 55 de 56 aciertos
(recall 87 %, antes 38 %); CUSTOMER_REFUND 344 de 344 (recall 97 %). Ningún aviso de registro.

Año anterior (2024, mismas funciones): UNIDENTIFIED reales degradados 7,9 % → 12,9 % y pagos con partida cubiertos 76,5 % → 92,5 %, igual que en 2025-H1 (9,1 → 14,7 % y 76,8 → 92,2 %); SUPPLIER_REFUND y CUSTOMER_REFUND sin ningún falso en ambos años. Detalle en `reports/P4_HISTORY.md`.

## Avisos para el equipo (informe de fallos de `eval.py`, val sobre main 8141660)

- `subset_sum` (P2): 12.902 movimientos con acierto 61 % y 4.891 partidas equivocadas; la precisión de asignación
  cae de 0,995 a 0,90. La nota sube por recall; que LIVE lo confirme.
- `short_cash` (`exceptions_amounts`, P3): 5.837 movimientos, acierto 78 %, 740 partidas equivocadas.
- UNIDENTIFIED reales casados solo por importe (la trampa «lookalike» de POLICIES §3): short_cash 176,
  subset_sum 36, amount_match 16. Exigir tercero antes de casar por importe los devolvería.
- Ranker (P1): 1.700 pagos de varias facturas con una sola.
- Excepciones (P3): SHORT_DISPUTE no se predice nunca (3.036 en el golden); FX_DIFFERENCE recall 9 % (15.793);
  SHORT_BANK_CHARGES 617 predichas con 139 aciertos; DUPLICATE_PAYMENT 1.350 con 678.

El sample se inspeccionó durante desarrollo: sus resultados NO son una estimación independiente de
generalización. No ajustar vocabulario ni umbrales para recuperar sus errores individuales. Cuando
llegue TRAIN, separar cronológicamente desarrollo y evaluación antes de ajustar nada, entrenar solo
con historia anterior a la ventana evaluada y excluir el mes sample ya inspeccionado de cualquier
holdout que se declare intacto. Congelar reglas antes de abrir las etiquetas del holdout. Revisar
precisión de categorías y falsas categorías sobre movimientos con partidas, por banco, formato y mes;
no seleccionar cambios solo por la nota agregada. Mantener FINAL sin consultar para decisiones de ajuste.

## Verificación inicial (antes de integrar P1–P3)

- Baseline ejecutado en este checkout: sample **2.80**.
- Reglas finales conservadoras: sample **3.25**, txn accuracy **0.1037**, sin asignaciones de partidas.
  La versión exploratoria alcanzó 3.35; se retiraron reglas ambiguas por generalización.
- `scripts/smoke.py`: **SMOKE VERDE**, 8.499 movimientos, 8.499 filas, aproximadamente 2 segundos.
- Siete pruebas de regresión pasan: reglas positivas y abstenciones, independencia de banco/año/importe,
  señales contradictorias, anulación única/ambigua, rechazos oficiales y cobertura, avisos de estilo y
  partidas desconocidas, gzip/BOM/columnas reordenadas/CSV con punto y coma, integridad del archivo
  preparado y ausencia de archivo nuevo cuando se rechaza la entrada.
- `eval.py --phase sample --segment ar_1to1 --dump 2`: informe generado con candidatas diagnósticas.
- `git diff --check`: sin errores.

Comandos en este entorno (caché uv fuera del directorio personal de solo lectura):

```
UV_CACHE_DIR=/tmp/bankrec-uv-cache uv run --no-sync python -m unittest discover -s tests -p 'p4_test.py'
UV_CACHE_DIR=/tmp/bankrec-uv-cache uv run --no-sync python scripts/smoke.py
UV_CACHE_DIR=/tmp/bankrec-uv-cache uv run --no-sync python scripts/eval.py --phase sample --segment ar_1to1 --dump 2
```

## Herramientas y pendientes por datos

`eval.py` guarda `out/<fase>/metrics.json` y puede mostrar candidatas por referencia/importe en `--dump`.
Son una ayuda al diagnóstico, no decisiones de conciliación ni saldos vivos reconstruidos por movimiento.
El validador usa el scorer oficial, exige cobertura completa local y acepta sus variantes CSV para los avisos.

`submit.py` valida, archiva una copia con hora UTC y hash SHA-256 completo, crea manifiesto JSON y añade
un registro PREPARADO, NO ENVIADO al scoreboard. Solo imprime instrucciones de envío manual. Un fichero
inválido no crea copia ni registro. FINAL sigue teniendo un único envío puntuado; el script no puede
saber si el equipo ya lo ha consumido: verificar en la plataforma antes del envío manual.

TRAIN y LIVE ya están disponibles y `bankrec.local.toml` apunta a su raíz. Se ha ejecutado val completo y se han preparado copias LIVE, sin enviar a plataforma. La nota de val actual es 38,29. Los resultados históricos y las limitaciones se recogen en `reports/P4_HISTORY.md`.

La CLI del validador y `submit.py` ya buscan ids desconocidos automáticamente en partidas actuales y anteriores, incluidos abonos y abiertas al inicio; esta búsqueda no consulta golden. Hay diez pruebas de P4, incluida la selección de categorías de maestros FINAL mediante una fixture sintética. La comprobación con datos FINAL reales y las notas de plataforma siguen pendientes.

`eval.py` imprime además P/R/F1 por clase de excepción (mismo conteo por fila que el scorer; su media reproduce
`exception_macro_f1`) y la confusión predicho→golden de las filas sin partida; ambos van a `metrics.json`.

Ejemplos web: la página es una SPA; sus 36 ejemplos resueltos (uno por segmento, de TRAIN) están en
`https://hackathon.usetalky.com/examples.json`, copiados en `tests/p4_web_examples.json`. Las pruebas comprueban
que `nonitem` solo reclama los 4 ejemplos sin partida (y acierta la categoría) y que `nonitem_unmatched` deja
UNIDENTIFIED los dos ejemplos no identificados y no los que citan número, son lotes o traen nombre distintivo.
Discrepancia del ejemplo `ar_short_withholding`: el texto dice que la excepción va solo en la factura retenida,
pero su CSV la pone en las 4 filas. En el golden de TRAIN, de 1.535 cobros multi-factura de ese segmento, 1.135
llevan SHORT_WITHHOLDING en todas las filas y 400 solo en algunas: se decide por factura (aviso para P3).
