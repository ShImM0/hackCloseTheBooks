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

## UNIDENTIFIED: último paso `nonitem_unmatched` (pendiente de registrar por P1)

El fallback marca UNIDENTIFIED todo abono sin resolver. En val completo eran 42.890 filas UNIDENTIFIED con
4.578 aciertas (F1 de la clase 0,19), porque casi todas tienen partida y solo falta el resolver que la encuentre.
POLICIES §3: UNIDENTIFIED exige que NO exista partida abierta. `bankrec/resolvers/nonitem_unmatched.py` solo ve
los abonos que nadie resolvió y mantiene UNIDENTIFIED si nada los ata a una partida abierta (ARI o APC) de su
sociedad: IBAN de cliente/proveedor con partidas, número impreso citado (≥ 6 dígitos), nombre distintivo
(≥ 3 palabras sin forma social, partial_ratio ≥ 90) o abono agregado (tarjetas/TPV, remesa, lockbox, cheques:
100 % con partidas en 2023 y 2024). Si hay evidencia, emite CUSTOMER_ADVANCE sin excepción (cobro identificado
pendiente de aplicar). El movimiento es un fallo de txn accuracy con cualquier etiqueta; lo que cambia es que no
se declara un UNIDENTIFIED falso.

Los nombres de 2 palabras ("NOMBRE APELLIDO") no cuentan: los cobros no identificados suelen venir de
particulares cuyo nombre coincide con un cliente (val mes 1: 8 % de las coincidencias de 2 palabras eran
UNIDENTIFIED reales frente a 1,6 % con 3+). Sensibilidad (val mes 1): NAME_MIN 85/90/95 → 59,41/59,39/59,39;
NAME_WORDS 4 → 59,22. Estable (< 0,3).

Medir sin tocar `registry.py`: `uv run python scripts/p4_run.py --phase val` (añade el módulo al final de la
cascada en memoria) y después `scripts/eval.py --phase val`.

**Petición a P1**: añadir `"bankrec.resolvers.nonitem_unmatched",` como ÚLTIMA línea de `registry.CASCADE`.

## Avisos para P3 (`exceptions.py`), del informe por clase de `eval.py`

Val completo: OVERPAYMENT 2.676 filas predichas con 182 aciertos (P 0,07) y DUPLICATE_PAYMENT 506 con 0 aciertos.
Cada fila de más es un FP de su clase en el macro-F1 (pesa igual que las clases grandes). SHORT_DISPUTE no se
predice nunca (3.036 en el golden).

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
