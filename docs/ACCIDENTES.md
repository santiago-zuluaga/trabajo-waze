# Siniestros reales y A* Balanced

## Fuente y adquisición

El extracto procede de la capa oficial [VM_Accidentes del Observatorio de Movilidad de Medellín](https://www.medellin.gov.co/servidormapas/rest/services/transporte/VM_Accidentes/MapServer/0), publicada también en el repositorio [Medellín en Movimiento](https://github.com/santiago-zuluaga/medellin-en-movimiento). La capa es de puntos, cubre 2019–2025 y permite GeoJSON, paginación y consultas de IDs. Los límites utilizados provienen de la capa oficial [Comunas y Corregimientos](https://www.medellin.gov.co/servidormapas/rest/services/mapas_nacionales/VC_Limite_Politico_Admtivo/MapServer/1).

Para reproducir la adquisición desde la raíz del repo:

```powershell
uv sync --extra notebook --extra test
uv run python scripts/acquire_incidents.py
uv run python scripts/verify_incidents.py
```

El descargador usa POST paginado y lotes de hasta 1.000, valida respuestas GeoJSON, verifica el total y genera un manifiesto con fecha, origen y hash. Se detiene si los archivos de salida ya existen. El verificador informa esquema, calidad y SHA-256 y compara el conjunto exacto de `objectid` con la capa oficial en vivo; `--no-network` omite esta última comparación. No sustituya el archivo ni el manifiesto existentes sin conservar antes una copia.

El extracto de esta validación (`data/raw/siniestralidad_medellin.geojson`) mide 122.149.362 bytes (116,5 MiB) y queda excluido de Git por tamaño. La capa pública es la fuente reproducible; revise sus términos antes de redistribuir el archivo. Los límites oficiales y el grafo OSM descargados se guardan también en rutas locales ignoradas. No se publican datos crudos, cachés ni credenciales.

## Inspección del archivo validado

Comprobado el 9 de noviembre de 2026:

- 235.820 features `Point`, EPSG:4326, con extensión lon/lat `[-75.713732, 6.130034, -75.481309, 6.438240]`.
- Campos: `objectid`, `radicado`, `fecha_siniestro`, `hora_siniestro`, `dia`, `mes`, `anio`, `clase_siniestro`, `gravedad_siniestro`, `direccion`, `barrio`, `num_comuna`, `nom_comuna`, `latitud`, `longitud` y geometría.
- Fechas válidas del 2019-01-01 al 2025-12-31; horas válidas para las 24 horas. No hubo fecha, hora, coordenada ni geometría inválida; coordenadas de atributos y geometría coincidieron dentro de 1e-6 grados.
- Los 235.820 `objectid` son únicos y contiguos de 1 a 235.820. Hay 274 filas repetidas por fecha/hora/coordenada redondeada a 6 decimales, pero tienen IDs distintos y se conservan: no se asume que sean errores.
- `barrio` tiene 236 nulos y `num_comuna` 257. No se usan estos campos para descartar el evento.
- El inventario oficial consultado en vivo devolvió 235.820 IDs: 0 faltantes y 0 IDs locales extra. Esta comprobación acredita completitud respecto al inventario de la capa en la fecha indicada, no que el servicio no vaya a actualizarse después.

`load_incidents` utilizó fecha, hora, geometría, `objectid`, longitud y latitud. Aceptó los 235.820 registros y descartó 0. Clase y gravedad se mantienen disponibles, pero no entran al costo actual.

## Asociación espacial y cobertura

La descarga de incidentes a la arista más cercana no se usa para Balanced. En una evaluación diagnóstica a 60 m, solo 56.456 puntos se asociaron a una arista y 176.008 asociaciones quedaron ambiguas entre calles cercanas. Usar ese emparejamiento habría introducido asignaciones dudosas en corredores paralelos.

En su lugar, los puntos se reproyectan al CRS WGS84 y se asignan por `within` a los polígonos oficiales: 235.583 quedaron dentro de exactamente un polígono, 0 tocaron múltiples polígonos y 237 no tuvieron coincidencia. En 2 de esos casos se usó `num_comuna` como respaldo; 235 quedaron sin asignar. Los puntos de borde no se fuerzan a una zona. La fuente y la geometría discrepan en 1.203 registros (0,51% de las asignaciones espaciales); en ese conflicto prevalece el polígono que contiene el punto y se informa el desacuerdo, sin desplazarlo a una calle vecina.

Los 23 polígonos incluyen 16 comunas, 5 corregimientos y las unidades oficiales sin nombre `SN01` y `SN02`. Para la red OSM de Medellín se asigna cada arista dirigida al polígono que contiene su punto medio. Esto cubre 218.655 de 219.157 aristas (99,77%); las 502 restantes quedan sin penalización histórica y no se adivina su zona por proximidad. Una arista que cruza límites se representa solo por su punto medio, una aproximación que puede ocultar un cruce de zona.

La tabla de frecuencias se calcula con los eventos asignados por unidad y hora. Los datos tienen actividad en cada una de las 24 horas. Los conteos varían desde 00:00 hasta 23:00; las combinaciones unidad/hora sin registros reciben cero. Esos ceros significan falta de eventos en el extracto, no seguridad.

## Metodología y límites

Se conserva la estructura de búsqueda existente `Node`/`Tree`, las aristas OSM dirigidas y sus claves, penalizaciones de giro y reconstrucción desde las geometrías OSM. Para cada arista se utiliza la unidad administrativa del punto medio. El conteo observado por unidad/hora se normaliza con el máximo de las unidades en esa hora:

```text
R(z,h) = N(z,h) / max_z N(z,h)
C(e,h) = t(e) * (1 + beta * R(z(e),h))
```

`t(e)` es el tiempo del tramo configurado por la notebook; `z(e)` es su unidad administrativa. La hora se fija durante toda la ruta. `beta=0` elimina el ajuste histórico; beta mayor escala la penalización. Los conteos se repiten para las aristas dentro de una misma unidad, por lo que este modelo no distingue calles distintas dentro de una comuna o corregimiento. No calcula riesgo por vehículo, probabilidad individual de accidente ni seguridad absoluta.

Los conteos absolutos por unidad favorecen unidades grandes o muy transitadas y no se ajustan por área, longitud vial, volumen de tránsito ni vehículo-kilómetro. Dividir por longitud vial o agregar por calle podría reducir parte del sesgo por tamaño, pero cambiaría el estimando y podría amplificar ruido en áreas pequeñas. Una tasa por vehículo-kilómetro requiere exposición de tráfico que este extracto no contiene. Se mantienen los conteos absolutos como señal exploratoria y se deben interpretar con esas limitaciones.

## Validación experimental

Se ejecutaron todas las celdas de `WAZE_IA_CORRECCION.ipynb` con el extracto real, los límites oficiales y el grafo OSM cacheado. Después se probaron tres pares de puntos estandarizados, a las 08:00 y 17:00 y con `beta` 0, 0,5, 1 y 2 (24 búsquedas Balanced). Las tres parejas encontraron ruta en todos los casos. La tabla completa, incluidos distancia, costo A*, frecuencia agregada, nodos explorados y tiempo de búsqueda, está en `docs/real_route_experiment.csv`; para repetirla usa `run_route_experiment` en el notebook. El archivo se versiona porque es pequeño y agregado; no contiene el GeoJSON ni una lista de siniestros.

| Origen → destino | Hora | Ruta beta 0 vs. más rápida | Cambios al subir beta | Desvío máximo observado |
|---|---:|---|---|---:|
| Plaza Botero → EAFIT | 08 y 17 | Idéntica | Misma geometría para beta 0–2; sube el costo objetivo y la búsqueda explora más nodos | 0% |
| Terminal Norte → Estadio | 08 y 17 | Idéntica | Misma geometría para beta 0–2; sube el costo objetivo y la búsqueda explora más nodos | 0% |
| Parque Poblado → Hospital General | 08 y 17 | Idéntica | Para beta ≥0,5 cambia a una ruta con 16 segmentos menos y algo más de longitud | 1,22% |

El mapa interactivo de la ruta más rápida y Balanced (beta 0,5; Parque Poblado → Hospital General, 08:00) está en [real_route_comparison.html](real_route_comparison.html). Requiere internet para cargar Plotly y el mapa base.

En el tercer par la distancia cambió de 4.238 m a 4.455 m y el tiempo base de 335,7 s a 339,8 s cuando beta fue 0,5–2. El costo acumulado creció al aumentar beta (por ejemplo, 335,7 s con beta 0 y 644,0 s con beta 2 a las 08:00); el tiempo base de viaje no es el costo ponderado. En las otras dos parejas la señal agregada no cambió la ruta, aunque sí elevó el costo objetivo. Por tanto, Balanced no garantiza una ruta distinta ni una reducción demostrada de siniestros.

Las pruebas automáticas verifican beta cero frente al tiempo, efecto de beta en grafos pequeños, suma del costo acumulado, direccionalidad, geometría de rutas, nodos destino inaccesibles y tramos sin datos. En esta corrida la ruta beta cero coincidió con la ruta rápida en los tres pares; todas las rutas reconstruidas pasaron la validación interna de endpoints, continuidad y costo acumulado. El CSV registra 24/24 búsquedas encontradas; no hubo caso sin ruta ni desvío superior a 1,22% en esta muestra intencionalmente pequeña. No es una evaluación de seguridad ni una muestra estadística representativa.

## Publicación

Versione código, documentación, pruebas y el CSV agregado. No publique `data/raw/siniestralidad_medellin.geojson` (116,5 MiB), `data/processed/medellin_admin_units.geojson`, `cache/medellin_drive.graphml`, archivos `.part` ni cachés de geocodificación. Las reglas de `.gitignore` excluyen esas rutas. El script de adquisición permite volver a obtener el extracto desde la fuente oficial sin modificar archivos existentes.
