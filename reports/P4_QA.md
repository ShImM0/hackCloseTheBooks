# P4: categorías, QA y preparación manual

Cambios limitados a los módulos de P4 y sus pruebas. No se ha enviado nada a la plataforma.

## Generalización y procedencia

Las categorías siguen `bankrec-docs-and-masters/POLICIES.md` §4 y el maestro `categories.csv`.
Las expresiones textuales son heurísticas de desarrollo, no un vocabulario completo ni reglas garantizadas
por el contrato. No hay decisiones por identificador, importe concreto, año, sociedad o nombre del sample.
El resolver no consulta golden, segmentos ni dificultad. El código bancario se conserva como evidencia,
pero no determina por sí solo una categoría: `DATA_DICTIONARY.md` advierte de usos inconsistentes.

Se retiraron las asociaciones de `TRASPASO PROPIO` / `TRASPASO ENTRE CUENTAS ES00` con cash pool:
sin evidencia adicional no distinguen pooling, traspaso propio y financiación intragrupo. Intereses requieren
una descripción de liquidación/pago, y las menciones explícitas de factura o deducción se dejan a los
resolvers de partidas. Reembolsos de notas de crédito no se convierten en categorías genéricas.
Una anulación bancaria explícita puede identificar el original solo si existe una candidata única de
igual cuenta y divisa, importe opuesto y concepto coincidente, entre 0 y 7 días antes. Esta ventana es
una heurística conservadora, no un límite contable aprendido ni garantizado.

`confidence=0.9` es una prioridad heurística, no una probabilidad calibrada. Las abstenciones permiten
que la cascada continúe. El fallback compartido sigue siendo de P1 y todavía sobrepredice UNIDENTIFIED
cuando faltan resolvers de partidas; P4 no ha modificado ese fichero.

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

La CLI del validador y `submit.py` ya buscan ids desconocidos automáticamente en partidas actuales y anteriores, incluidos abonos y abiertas al inicio; esta búsqueda no consulta golden. Hay diez pruebas de P4, incluida la selección de categorías de maestros FINAL mediante una fixture sintética. La comprobación con datos FINAL reales y las notas de plataforma siguen pendientes. También falta contrastar los ejemplos web, todavía inaccesibles.
