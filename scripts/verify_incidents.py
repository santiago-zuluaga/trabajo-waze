"""Inspect an official Medellin crash extract and compare it with live OID inventory."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import time
import hashlib
import json
from pathlib import Path
import sys

import geopandas as gpd
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
LAYER = "https://www.medellin.gov.co/servidormapas/rest/services/transporte/VM_Accidentes/MapServer/0"
DEFAULT_INPUT = ROOT / "data" / "raw" / "siniestralidad_medellin.geojson"


def _hour_string(value):
    return value.strftime("%H:%M:%S") if isinstance(value, time) else str(value)


def verify(path: Path, online: bool = True) -> dict:
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(path)
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            sha.update(block)

    frame = gpd.read_file(path)
    required = {"objectid", "fecha_siniestro", "hora_siniestro", "latitud", "longitud"}
    missing_fields = sorted(required - set(frame.columns))
    if missing_fields:
        raise ValueError(f"Faltan campos obligatorios: {missing_fields}")
    ids = pd.to_numeric(frame["objectid"], errors="coerce")
    dates = pd.to_datetime(frame["fecha_siniestro"], errors="coerce")
    hours = frame["hora_siniestro"].map(_hour_string)
    parsed_hours = pd.to_datetime(hours, format="%H:%M:%S", errors="coerce").dt.hour
    lon = pd.to_numeric(frame["longitud"], errors="coerce")
    lat = pd.to_numeric(frame["latitud"], errors="coerce")
    geom_lon, geom_lat = frame.geometry.x, frame.geometry.y
    geometry_types = frame.geometry.geom_type.value_counts(dropna=False).to_dict()
    id_set = set(ids.dropna().astype("int64").tolist())
    local_signature_duplicates = int(frame.assign(
        _date=dates, _hour=hours, _lon=geom_lon.round(6), _lat=geom_lat.round(6),
    ).duplicated(["_date", "_hour", "_lon", "_lat"]).sum())

    report = {
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "sha256": sha.hexdigest(),
        "driver": "GeoJSON",
        "crs": str(frame.crs),
        "records": int(len(frame)),
        "fields": list(frame.columns),
        "geometry_types": {str(key): int(value) for key, value in geometry_types.items()},
        "objectid_unique": int(ids.nunique()),
        "objectid_duplicates": int(ids.duplicated().sum()),
        "objectid_null_or_invalid": int(ids.isna().sum()),
        "objectid_range": [int(ids.min()), int(ids.max())],
        "objectids_contiguous_from_one": bool(ids.nunique() == ids.max() and ids.min() == 1),
        "date_min": str(dates.min().date()),
        "date_max": str(dates.max().date()),
        "invalid_dates": int(dates.isna().sum()),
        "invalid_hours": int(parsed_hours.isna().sum()),
        "hours_present": sorted(int(value) for value in parsed_hours.dropna().unique()),
        "years": {str(int(year)): int(count) for year, count in dates.dt.year.value_counts().sort_index().items()},
        "invalid_coordinate_attributes": int((
            lon.isna() | lat.isna() | ~lon.between(-180, 180) | ~lat.between(-90, 90)
        ).sum()),
        "nonpoint_or_empty_geometries": int((
            frame.geometry.isna() | frame.geometry.is_empty | (frame.geometry.geom_type != "Point")
        ).sum()),
        "attribute_geometry_mismatches_gt_1e_6": int((
            (lon - geom_lon).abs().gt(1e-6) | (lat - geom_lat).abs().gt(1e-6)
        ).sum()),
        "bounds_lonlat": [float(value) for value in frame.total_bounds],
        "duplicate_date_hour_coordinate_rows_6dp": local_signature_duplicates,
        "nulls_by_field": {str(key): int(value) for key, value in frame.isna().sum().items()},
        "official_source": LAYER,
    }
    if online:
        response = requests.post(
            f"{LAYER}/query",
            data={"where": "1=1", "returnIdsOnly": "true", "f": "json"},
            timeout=(10, 90),
        )
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            raise RuntimeError(f"ArcGIS rechazó la comparación de IDs: {payload['error']}")
        official_ids = set(map(int, payload.get("objectIds", [])))
        missing_ids = sorted(official_ids - id_set)
        extra_ids = sorted(id_set - official_ids)
        report["official_current_count"] = len(official_ids)
        report["official_ids_missing_locally"] = len(missing_ids)
        report["local_ids_not_in_official_layer"] = len(extra_ids)
        report["sample_missing_ids"] = missing_ids[:10]
        report["sample_extra_ids"] = extra_ids[:10]
        report["complete_against_live_official_id_set"] = not missing_ids and not extra_ids
    else:
        report["complete_against_live_official_id_set"] = None
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--no-network", action="store_true", help="omitir la comparación de IDs oficial en vivo")
    args = parser.parse_args()
    print(json.dumps(verify(args.input, online=not args.no_network), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
