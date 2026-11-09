# Waze IA — rutas A* Balanced para Medellín

Proyecto académico de navegación sobre la red vial dirigida de OpenStreetMap. La notebook principal compara A* por tiempo con A* Balanced, que agrega una penalización horaria basada en siniestros históricos oficiales de Medellín. Si no encuentra el dataset, Balanced queda desactivado y el notebook no inventa eventos.

## Requisitos

- Windows 10/11, Linux, Google Colab o Jupyter.
- Python 3.12 recomendado. Python 3.11 también está soportado.
- `uv` para crear el entorno local reproducible. En Colab, la primera celda instala el proyecto y sus dependencias automáticamente.
- Internet para instalar paquetes y descargar la red vial de OpenStreetMap. El mapa base también requiere acceso a internet.

Las dependencias se definen en `pyproject.toml`; `uv.lock` conserva la resolución reproducible. Los dos módulos propios se instalan como parte del proyecto. No mantenemos un `requirements.txt` duplicado.

## Clonar en otro PC (Windows PowerShell)

Instala Git y [uv](https://docs.astral.sh/uv/getting-started/installation/), luego ejecuta:

```powershell
git clone https://github.com/santiago-zuluaga/trabajo-waze.git
cd trabajo-waze
uv sync --extra notebook --extra test
uv run python scripts/acquire_incidents.py
uv run python scripts/verify_incidents.py
uv run python -m pytest tests -v
uv run jupyter lab WAZE_IA_CORRECCION.ipynb
```

`uv` instala Python 3.12 si no está disponible, crea `.venv` y sincroniza las versiones fijadas en `uv.lock`. En VS Code también puedes abrir la carpeta y seleccionar el kernel `.venv\Scripts\python.exe`.

En Linux/macOS el mismo flujo funciona; el intérprete del kernel queda en `.venv/bin/python`.

## Google Colab

Abre [WAZE_IA_CORRECCION.ipynb en Google Colab](https://colab.research.google.com/github/santiago-zuluaga/trabajo-waze/blob/main/WAZE_IA_CORRECCION.ipynb) y selecciona **Entorno de ejecución → Ejecutar todas**. La primera celda clona este repositorio a `/content/trabajo-waze`, instala el proyecto desde `pyproject.toml` y agrega la raíz al import path. No necesitas cargar manualmente `waze_accidents.py` ni `waze_communes.py`.

La descarga de la red de OpenStreetMap requiere que el runtime tenga internet y puede tardar. Colab es temporal: para usar el extracto real, adquiere el archivo en `data/raw/siniestralidad_medellin.geojson` con el script documentado o cópialo a esa ruta durante la sesión. No se sube el GeoJSON crudo al repositorio.

### Estado de verificación portable

Verificado en Windows con Python 3.12: sincronización de dependencias, suite de pruebas y ejecución completa local del notebook con datos reales, polígonos oficiales y grafo OSM. La comparación del inventario en vivo encontró 235.820 de 235.820 IDs. Los resultados experimentales y límites del modelo están en [docs/ACCIDENTES.md](docs/ACCIDENTES.md), [docs/real_route_experiment.csv](docs/real_route_experiment.csv) y [el mapa comparativo](docs/real_route_comparison.html). No se inició un runtime de Colab en esta tarea.

## Ejecutar en Jupyter local

Después de `uv sync --extra notebook --extra test`, abre la carpeta del repositorio en JupyterLab o VS Code y ejecuta `WAZE_IA_CORRECCION.ipynb` de principio a fin. La celda inicial localiza la raíz del checkout, configura imports y cambia a esa carpeta; por eso las rutas de datos y resultados no dependen de la carpeta desde la que se inició Jupyter. No instala paquetes localmente: usa el entorno configurado con `uv sync`.

La red vial se consulta desde OpenStreetMap al ejecutar la celda de OSMnx. Nominatim y los servicios de mapas son recursos externos y pueden limitar o demorar solicitudes. El notebook no guarda sus resultados de ejecución ni credenciales en el archivo.

## Datos reales y demo

La fuente oficial es la capa pública ArcGIS `VM_Accidentes`, documentada en [docs/ACCIDENTES.md](docs/ACCIDENTES.md). Para adquirir el extracto desde la raíz del repositorio:

```powershell
uv run python scripts/acquire_incidents.py
```

El descargador guarda el GeoJSON y su manifiesto en `data/raw/`, se niega a sobrescribir archivos existentes y valida el formato/respuesta. `scripts/verify_incidents.py` valida estructura/calidad y compara IDs con la fuente oficial. El extracto de 116,5 MiB está excluido de Git. Si no hay archivo real, Balanced queda deshabilitado.

El modelo usa `C(e,h)=t(e)(1+beta R(e,h))`; la hora seleccionada se aplica a toda la ruta. Los conteos históricos no equivalen a probabilidad individual de accidente, no están ajustados por exposición o volumen vehicular y no garantizan seguridad. Consulta la documentación para el sesgo por longitud de segmentos, las limitaciones espaciales y la metodología.

## Pruebas

```powershell
uv run python -m pytest tests -v
```

La suite cubre validación y asociación espacial, costos por hora, agregación, selección de alternativas, grafos dirigidos, beta cero, destinos inalcanzables y configuración portable. No depende de la API externa. Para comprobar directamente que los módulos se importan:

```powershell
uv run python -c "import waze_accidents, waze_communes; print('imports OK')"
```

## Estructura

```text
WAZE_IA_CORRECCION.ipynb   Notebook principal (A* con Node/Tree)
Waze_IA.ipynb              Notebook alternativo conservado
waze_accidents.py          Carga, validación y asociación de siniestros
waze_communes.py           Asociación a polígonos y frecuencias por unidad oficial
scripts/acquire_incidents.py Descarga validada desde ArcGIS
scripts/verify_incidents.py Verifica extracto y completitud contra ArcGIS
tests/                      Pruebas offline
 docs/ACCIDENTES.md         Datos, método y limitaciones
pyproject.toml              Dependencias, metadatos y módulos instalables
uv.lock                     Resolución fijada para Python 3.11 y 3.12
```

## Limitaciones conocidas

- El GeoJSON crudo y el grafo descargado se excluyen de Git; cada persona debe adquirirlos con la fuente documentada.
- Balanced usa frecuencia por comuna/corregimiento, por lo que no distingue calles diferentes dentro de la misma unidad.
- El primer cálculo del grafo necesita red y los servicios externos pueden fallar o cambiar.
- No hay denominador de exposición vehicular; el modelo no estima riesgo por vehículo ni probabilidad de accidente.
- La hora se mantiene fija durante el recorrido y la correspondencia espacial es aproximada.
