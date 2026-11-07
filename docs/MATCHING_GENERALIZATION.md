# Matching y generalización

## Cambios

- `subset_sum`: sin cambios aquí; se mantiene la versión de P2 (`15eb609`: densidad de coincidencias casuales,
  tercero claro, abono con su factura, FIFO con huecos). La variante "suma única sobre todas las candidatas"
  chocaba con ella al integrar y no se ha medido en val; queda fuera.
- `multi_ref_fill`: conserva la evidencia de referencias, pero solo completa cuando puede comprobar el
  conjunto entero de candidatas. No toma las 22 más cercanas en fecha entre cientos de alternativas.
  Las partidas adicionales deben pertenecer a la entidad del movimiento; la salida se ordena por FIFO.
- `ranker`: restringe las candidatas a divisas respaldadas por el movimiento; normaliza las dos features
  monetarias antes absolutas respecto al saldo o total. Incluye pagos 1:N como ejemplos negativos para
  evitar que el modelo 1:1 cierre solo una factura de un lote. Reduce la complejidad de los árboles,
  añade regularización L2 y reserva los últimos 60 días del histórico para parada temprana. Ninguna
  candidata de una misma transacción se reparte entre ajuste y holdout. La caché cambia de versión y
  recoge los parámetros del modelo y de la separación temporal.

Los límites son presupuestos de búsqueda y valores redondos, no reglas por cliente, entidad o importe.
No se añaden fechas, IDs ni etiquetas del período evaluado a las reglas. Las búsquedas que se abstienen
dejan continuar la cascada; puede bajar la cobertura de esos resolvers a cambio de evitar falsos positivos.

## Reproducción

```bash
uv run python -m unittest discover -s tests -p '*test*.py' -v
uv run python tests/test_docs_remit.py
uv run python scripts/smoke.py
uv run python scripts/backtest.py --start 2025-01-01 --end 2025-07-01 --out out/matching_after
uv run python scripts/backtest.py --start 2024-07-01 --end 2024-08-01 --out out/matching_2024_after
```

`backtest.py` utiliza el pipeline y scorer oficiales, con la fecha inicial como corte de entrenamiento.
Guarda las salidas en una carpeta separada y falla si hay errores de ejecución en los resolvers.
Los tests sintéticos cubren `multi_ref_fill` (conjunto completo de candidatas, entidades que no se cruzan) y
`ranker` (divisas no respaldadas, features invariantes a la escala monetaria, holdout temporal).

## Límites de la evaluación

La reconstrucción de saldos de `phase.build("val")` sigue utilizando la disponibilidad futura de partidas
para excluir cierres fuera de banco. Los maestros históricos son una foto posterior y el entrenamiento
aproxima el saldo abierto por la fecha del primer pago. Por ello, estos backtests son optimistas y no
demuestran por sí solos rendimiento en FINAL o en una filial nunca vista. `sample` está dentro de val:
sirve de smoke, no de validación independiente. No se ha enviado ningún resultado a la plataforma.
