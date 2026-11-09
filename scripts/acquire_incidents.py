"""Download the minimal public ArcGIS crash extract used by the notebook."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

import requests


LAYER = "https://www.medellin.gov.co/servidormapas/rest/services/transporte/VM_Accidentes/MapServer/0"
FIELDS = [
    "objectid", "fecha_siniestro", "hora_siniestro", "clase_siniestro",
    "gravedad_siniestro", "latitud", "longitud", "num_comuna", "nom_comuna",
]


def _json_payload(response, source: str) -> dict:
    content_type = response.headers.get("Content-Type", "").lower()
    if content_type and "json" not in content_type:
        raise RuntimeError(
            f"{source} devolvió Content-Type {content_type!r}; se esperaba JSON/GeoJSON."
        )
    try:
        payload = response.json()
    except ValueError as error:
        raise RuntimeError(f"{source} no devolvió JSON válido.") from error
    if not isinstance(payload, dict):
        raise RuntimeError(f"{source} no devolvió un objeto JSON.")
    return payload


def acquire(output: Path, batch_size: int = 1000) -> dict:
    if not 1 <= batch_size <= 2000:
        raise ValueError("batch_size debe estar entre 1 y 2000.")
    output = Path(output)
    manifest_path = output.with_suffix(output.suffix + ".manifest.json")
    temporary = output.with_suffix(output.suffix + ".part")
    existing = [path for path in (output, manifest_path, temporary) if path.exists()]
    if existing:
        raise FileExistsError(
            "No se sobrescriben archivos de datos existentes: "
            + ", ".join(map(str, existing))
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    metadata = session.get(LAYER, params={"f": "json"}, timeout=(10, 60))
    metadata.raise_for_status()
    layer_info = _json_payload(metadata, "Metadatos ArcGIS")
    oid_field = (
        layer_info.get("objectIdField")
        or layer_info.get("objectIdFieldName")
        or next((field["name"] for field in layer_info.get("fields", [])
                 if field.get("type") == "esriFieldTypeOID"), None)
    )
    if not oid_field:
        raise RuntimeError("La capa ArcGIS no declara su campo object ID.")
    available_fields = {field["name"].lower() for field in layer_info.get("fields", [])}
    missing_fields = {field.lower() for field in FIELDS} - available_fields
    if missing_fields:
        raise RuntimeError(f"La capa no tiene los campos esperados: {sorted(missing_fields)}")
    supports_pagination = layer_info.get("advancedQueryCapabilities", {}).get("supportsPagination", False)
    if not supports_pagination:
        raise RuntimeError(
            "ArcGIS no anuncia supportsPagination; use consultas POST por objectIds "
            "en lotes pequeños como alternativa."
        )
    query_url = f"{LAYER}/query"
    where = "1=1"
    count_response = session.post(query_url, data={
        "where": where, "returnCountOnly": "true", "f": "json",
    }, timeout=(10, 120))
    count_response.raise_for_status()
    payload = _json_payload(count_response, "Conteo ArcGIS")
    if "error" in payload:
        raise RuntimeError(f"ArcGIS rechazó la consulta de conteo: {payload['error']}")
    expected_count = int(payload.get("count", 0))
    if expected_count <= 0:
        raise RuntimeError("La capa no devolvió registros.")

    downloaded = 0
    digest = hashlib.sha256()
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        prefix = '{"type":"FeatureCollection","features":[\n'
        stream.write(prefix)
        digest.update(prefix.encode("utf-8"))
        first = True
        for offset in range(0, expected_count, batch_size):
            # ArcGIS metadata advertises supportsPagination and a 2,000-row
            # maximum. POST keeps requests small and avoids long query URLs.
            response = session.post(query_url, data={
                "where": where,
                "outFields": ",".join(FIELDS),
                "returnGeometry": "true", "outSR": "4326", "f": "geojson",
                "orderByFields": f"{oid_field} ASC",
                "resultOffset": offset,
                "resultRecordCount": batch_size,
            }, timeout=(10, 180))
            response.raise_for_status()
            result = _json_payload(response, f"Lote ArcGIS offset={offset}")
            if "error" in result:
                raise RuntimeError(f"ArcGIS rechazó el lote {offset}: {result['error']}")
            if result.get("type") != "FeatureCollection" or not isinstance(result.get("features"), list):
                raise RuntimeError(
                    f"ArcGIS no devolvió una FeatureCollection en el lote {offset}; "
                    f"tipo recibido: {result.get('type')!r}."
                )
            for feature in result.get("features", []):
                if not isinstance(feature, dict) or not isinstance(feature.get("properties"), dict):
                    raise RuntimeError(
                        f"ArcGIS devolvió un feature inválido en el lote offset={offset}."
                    )
                if not first:
                    stream.write(",\n")
                    digest.update(b",\n")
                encoded = json.dumps(feature, ensure_ascii=False, separators=(",", ":"))
                stream.write(encoded)
                digest.update(encoded.encode("utf-8"))
                first = False
                downloaded += 1
            print(f"Descargados {downloaded:,}/{expected_count:,}", file=sys.stderr)
        if downloaded != expected_count:
            raise RuntimeError(
                f"Descarga incompleta: {downloaded} de {expected_count} registros; "
                "el archivo final no será creado."
            )
        suffix = "\n]}\n"
        stream.write(suffix)
        digest.update(suffix.encode("utf-8"))

    temporary.replace(output)
    with output.open("rb") as stream:
        if not stream.read(64).lstrip().startswith(b'{"type":"FeatureCollection"'):
            raise RuntimeError("El archivo escrito no es un GeoJSON FeatureCollection.")
    manifest = {
        "source": LAYER,
        "acquired_utc": datetime.now(timezone.utc).isoformat(),
        "record_count_at_start": expected_count,
        "features_downloaded": downloaded,
        "format": "GeoJSON FeatureCollection",
        "fields": FIELDS,
        "crs": "EPSG:4326",
        "sha256": digest.hexdigest(),
        "output": str(output),
        "note": "Extracto de uso local con campos minimizados; verificar licencia antes de redistribuir.",
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path,
        default=Path(__file__).resolve().parents[1] / "data" / "raw" / "siniestralidad_medellin.geojson",
    )
    parser.add_argument("--batch-size", type=int, default=1000)
    args = parser.parse_args()
    result = acquire(args.output, args.batch_size)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
