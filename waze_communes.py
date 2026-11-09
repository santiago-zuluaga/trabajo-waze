"""Commune-level crash frequency and explicit synthetic demo helpers."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
import requests
from shapely.geometry import LineString


OFFICIAL_ADMIN_UNITS_LAYER = (
    "https://www.medellin.gov.co/servidormapas/rest/services/"
    "mapas_nacionales/VC_Limite_Politico_Admtivo/MapServer/1"
)


def normalize_commune_id(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip().upper()
    if not text:
        return None
    if text.startswith("SN"):
        return text
    digits = "".join(character for character in text if character.isdigit())
    return str(int(digits)) if digits else text


def load_official_admin_units(path: str | Path) -> gpd.GeoDataFrame:
    """Load official comuna/corregimiento polygons with normalized columns."""
    units = gpd.read_file(path)
    required_source = {"codigo", "nombre", "subtipo_comunacorregimiento", "geometry"}
    missing = required_source - set(units.columns)
    if missing:
        raise ValueError(f"La capa oficial no tiene atributos requeridos: {sorted(missing)}")
    if units.crs is None:
        raise ValueError("La capa oficial no declara CRS; se esperaba EPSG:4326.")
    units = units.to_crs("EPSG:4326").copy()
    units["comuna_id"] = units["codigo"].map(normalize_commune_id)
    units["comuna_name"] = units["nombre"].fillna("").astype(str).str.strip()
    unnamed = units["comuna_name"].eq("") | units["comuna_name"].str.lower().eq("nan")
    units.loc[unnamed, "comuna_name"] = units.loc[unnamed, "comuna_id"].map(
        lambda code: f"Unidad administrativa {code} (sin nombre en la capa)"
    )
    if units["comuna_id"].isna().any() or units["comuna_id"].duplicated().any():
        raise ValueError("Los códigos administrativos oficiales están vacíos o duplicados.")
    if units.geometry.isna().any() or units.geometry.is_empty.any():
        raise ValueError("La capa oficial contiene geometrías vacías.")
    return units[["comuna_id", "comuna_name", "subtipo_comunacorregimiento", "geometry"]]


def download_official_admin_units(output_path: str | Any) -> gpd.GeoDataFrame:
    """Download and validate the small official boundary layer once.

    Existing output or partial files are never overwritten. The query requests
    GeoJSON in WGS84 and includes both communes and rural corregimientos.
    """
    output = Path(output_path)
    partial = output.with_suffix(output.suffix + ".part")
    if output.exists() or partial.exists():
        raise FileExistsError(f"No se sobrescriben límites administrativos existentes: {output}")
    response = requests.get(
        f"{OFFICIAL_ADMIN_UNITS_LAYER}/query",
        params={
            "where": "1=1",
            "outFields": "codigo,nombre,subtipo_comunacorregimiento",
            "returnGeometry": "true",
            "outSR": "4326",
            "f": "geojson",
        },
        timeout=(10, 60),
    )
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as error:
        raise RuntimeError("El servicio de límites no devolvió JSON válido.") from error
    if not isinstance(payload, dict):
        raise RuntimeError("El servicio de límites no devolvió un objeto JSON.")
    features = payload.get("features")
    if payload.get("type") != "FeatureCollection" or not isinstance(features, list):
        raise RuntimeError("El servicio de límites no devolvió una GeoJSON FeatureCollection.")
    required_ids = {str(code) for code in range(1, 17)} | {"50", "60", "70", "80", "90"}
    returned_ids = {
        normalize_commune_id(feature.get("properties", {}).get("codigo"))
        for feature in features
    }
    if not required_ids.issubset(returned_ids):
        raise RuntimeError(
            "La capa oficial no contiene las 16 comunas y los 5 corregimientos esperados. "
            f"Faltan: {sorted(required_ids - returned_ids)}"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    partial.write_bytes(response.content)
    partial.replace(output)
    try:
        return load_official_admin_units(output)
    except Exception:
        output.unlink(missing_ok=True)
        raise


def assign_incidents_to_communes(
    incidents: gpd.GeoDataFrame,
    boundaries: gpd.GeoDataFrame,
) -> tuple[gpd.GeoDataFrame, dict[str, Any]]:
    """Assign points to official admin polygons and audit source-code conflicts.

    A point inside exactly one polygon is assigned spatially. Only points with
    no polygon hit may fall back to a recognized source ``num_comuna`` code;
    points overlapping multiple polygons remain unassigned.
    """
    required = {"geometry", "comuna_id", "comuna_name"}
    missing = required - set(boundaries.columns)
    if missing:
        raise ValueError(f"Faltan columnas en límites administrativos: {sorted(missing)}")
    if incidents.crs is None or boundaries.crs is None:
        raise ValueError("Incidentes y límites deben declarar su CRS.")
    zones = boundaries[["comuna_id", "geometry"]].to_crs("EPSG:4326").copy()
    zones["comuna_id"] = zones["comuna_id"].map(normalize_commune_id)
    valid_zone_ids = set(zones["comuna_id"].dropna())
    points = incidents[["geometry"]].to_crs("EPSG:4326").reset_index(drop=True)
    points["_row_id"] = range(len(points))
    joined = gpd.sjoin(points, zones, how="left", predicate="within")
    spatial_matches: dict[int, set[str]] = {}
    for row_id, group in joined.groupby("_row_id", sort=False):
        spatial_matches[int(row_id)] = {
            normalize_commune_id(code)
            for code in group["comuna_id"].dropna()
        }

    result = incidents.copy().reset_index(drop=True)
    if "num_comuna" in result:
        source_codes = result["num_comuna"].map(normalize_commune_id)
    else:
        source_codes = pd.Series([None] * len(result))
    assigned: list[str | None] = []
    by_geometry = fallbacks = multiple = mismatches = unknown_source_codes = 0
    mismatch_samples = []
    for row_id, source_code in enumerate(source_codes):
        if source_code is not None and pd.isna(source_code):
            source_code = None
        matches = spatial_matches.get(row_id, set())
        code = None
        if len(matches) == 1:
            code = next(iter(matches))
            by_geometry += 1
            if source_code in valid_zone_ids and source_code != code:
                mismatches += 1
                if len(mismatch_samples) < 10:
                    mismatch_samples.append({
                        "objectid": result.iloc[row_id].get("objectid"),
                        "source_num_comuna": source_code,
                        "point_in_polygon": code,
                    })
        elif len(matches) > 1:
            multiple += 1
        elif source_code in valid_zone_ids:
            code = source_code
            fallbacks += 1
        elif source_code is not None:
            unknown_source_codes += 1
        assigned.append(code)

    result["comuna_id"] = pd.Series(assigned, dtype="object")
    stats = {
        "incidents": int(len(result)),
        "assigned": int(result["comuna_id"].notna().sum()),
        "assigned_by_point_in_polygon": int(by_geometry),
        "assigned_by_source_attribute_fallback": int(fallbacks),
        "unassigned": int(result["comuna_id"].isna().sum()),
        "multiple_polygon_matches": int(multiple),
        "source_code_vs_polygon_mismatch": int(mismatches),
        "unknown_nonempty_source_codes": int(unknown_source_codes),
        "mismatch_samples": mismatch_samples,
    }
    return result, stats


def build_commune_frequency(records: pd.DataFrame) -> tuple[dict, dict]:
    """Return raw counts and per-hour max-normalized R(comuna, hora)."""
    required = {"num_comuna", "hour", "event_count"}
    missing = required - set(records.columns)
    if missing:
        raise ValueError(f"Faltan columnas para el agregado por comuna: {sorted(missing)}")

    counts = defaultdict(int)
    for row in records.itertuples(index=False):
        commune = normalize_commune_id(row.num_comuna)
        hour = int(row.hour)
        count = int(row.event_count)
        if commune is None or not 0 <= hour <= 23 or count < 0:
            continue
        counts[(commune, hour)] += count

    hourly_max: dict[int, int] = {}
    for (_, hour), count in counts.items():
        hourly_max[hour] = max(hourly_max.get(hour, 0), count)
    normalized = {
        key: count / hourly_max[key[1]] if hourly_max.get(key[1], 0) else 0.0
        for key, count in counts.items()
    }
    return dict(counts), normalized


def map_edges_to_communes(graph, boundaries: gpd.GeoDataFrame) -> tuple[dict, dict]:
    """Assign each directed OSM edge by midpoint inside an official polygon.

    Boundary-crossing edges are represented by their midpoint. Edges that do
    not fall inside a polygon stay unassigned; this function never guesses a
    commune from nearby crash points.
    """
    required = {"comuna_id", "comuna_name", "geometry"}
    missing = required - set(boundaries.columns)
    if missing:
        raise ValueError(f"Faltan atributos en los polígonos de comuna: {sorted(missing)}")
    rows = []
    for u, v, key, data in graph.edges(keys=True, data=True):
        geometry = data.get("geometry")
        if geometry is None:
            geometry = LineString([
                (graph.nodes[u]["x"], graph.nodes[u]["y"]),
                (graph.nodes[v]["x"], graph.nodes[v]["y"]),
            ])
        rows.append({"u": u, "v": v, "key": key, "geometry": geometry.interpolate(0.5, normalized=True)})
    midpoints = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")
    communes = boundaries[["comuna_id", "comuna_name", "geometry"]].to_crs("EPSG:4326")
    joined = gpd.sjoin(midpoints, communes, how="left", predicate="within")
    edge_communes = {}
    commune_names = {}
    for row in joined.itertuples():
        code = normalize_commune_id(row.comuna_id)
        if code is None or pd.isna(row.comuna_name):
            continue
        edge_communes[(row.u, row.v, row.key)] = code
        commune_names[code] = str(row.comuna_name)
    return edge_communes, commune_names


def make_synthetic_demo_frequencies(commune_ids) -> tuple[dict, dict]:
    """Deterministic, clearly synthetic hourly counts for UI demonstrations."""
    ids = sorted({normalize_commune_id(value) for value in commune_ids if normalize_commune_id(value)})
    records = []
    for index, commune in enumerate(ids):
        peak = (7 + index * 3) % 24
        for hour in range(24):
            circular_distance = min((hour - peak) % 24, (peak - hour) % 24)
            count = 2 + ((index * 7 + hour * 5) % 11) + (8 if circular_distance <= 1 else 0)
            records.append({"num_comuna": commune, "hour": hour, "event_count": count})
    return build_commune_frequency(pd.DataFrame(records))


def make_synthetic_demo_zones(graph, cell_degrees: float = 0.012) -> tuple[dict, dict]:
    """Create grid labels for demo-only routing when commune polygons are absent."""
    if cell_degrees <= 0:
        raise ValueError("cell_degrees debe ser positivo.")
    edge_zones = {}
    zone_names = {}
    for u, v, key, data in graph.edges(keys=True, data=True):
        geometry = data.get("geometry")
        if geometry is None:
            geometry = LineString([
                (graph.nodes[u]["x"], graph.nodes[u]["y"]),
                (graph.nodes[v]["x"], graph.nodes[v]["y"]),
            ])
        midpoint = geometry.interpolate(0.5, normalized=True)
        col = int((midpoint.x + 76.0) / cell_degrees)
        row = int((midpoint.y + 6.0) / cell_degrees)
        zone = f"DEMO-{row:02d}-{col:02d}"
        edge_zones[(u, v, key)] = zone
        zone_names[zone] = f"Zona sintética {row:02d}-{col:02d}"
    return edge_zones, zone_names


def expand_commune_risk_to_edges(graph, edge_communes: dict, counts: dict, risk: dict) -> tuple[dict, dict]:
    """Adapt comuna-hour values to the existing OSMRouteNode edge lookup."""
    edge_counts = {}
    edge_risk = {}
    for u, v, key in graph.edges(keys=True):
        commune = edge_communes.get((u, v, key))
        if commune is None:
            continue
        for hour in range(24):
            edge_counts[(u, v, key, hour)] = int(counts.get((commune, hour), 0))
            edge_risk[(u, v, key, hour)] = float(risk.get((commune, hour), 0.0))
    return edge_counts, edge_risk


def result_route_communes(result: dict, commune_names: dict | None = None) -> list[str]:
    """Unique communes/zones along a route in traversal order."""
    names = commune_names or {}
    output = []
    seen = set()
    for detail in result.get("edge_details", []):
        commune = detail.get("comuna_id")
        if commune is None or pd.isna(commune):
            continue
        code = normalize_commune_id(commune)
        if code in seen:
            continue
        seen.add(code)
        output.append(names.get(code, code))
    return output


def route_frequency_total(result: dict) -> int:
    """Sum hourly event counts once per distinct commune/zone on the route."""
    totals = {}
    for detail in result.get("edge_details", []):
        commune = detail.get("comuna_id")
        if commune is None or pd.isna(commune):
            # Legacy per-edge data has no commune field; count each segment.
            totals[(detail.get("u"), detail.get("v"), detail.get("key"))] = int(
                detail.get("historical_count", 0)
            )
            continue
        hour = int(detail.get("hour", 0))
        totals[(normalize_commune_id(commune), hour)] = int(detail.get("historical_count", 0))
    return sum(totals.values())


def route_risk_burden(result: dict) -> float:
    """Travel-time-weighted normalized frequency used to compare alternatives."""
    return sum(
        float(detail.get("base_time_s", 0.0)) * float(detail.get("historical_risk_normalized", 0.0))
        for detail in result.get("edge_details", [])
    )


def choose_lower_frequency_alternative(fastest: dict, candidates: list[dict]) -> dict | None:
    """Choose a distinct slower path with lower aggregate frequency, if present."""
    fastest_signature = tuple(fastest.get("route_edges", []))
    fastest_time = sum(float(row.get("base_time_s", 0.0)) for row in fastest.get("edge_details", []))
    fastest_frequency = route_frequency_total(fastest)
    eligible = []
    for candidate in candidates:
        if not candidate.get("found") or tuple(candidate.get("route_edges", [])) == fastest_signature:
            continue
        candidate_time = sum(float(row.get("base_time_s", 0.0)) for row in candidate.get("edge_details", []))
        if candidate_time <= fastest_time or route_frequency_total(candidate) >= fastest_frequency:
            continue
        eligible.append(candidate)
    return min(eligible, key=lambda route: (route_frequency_total(route), route_risk_burden(route))) if eligible else None
