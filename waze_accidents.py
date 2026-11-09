"""Historical crash frequency utilities for the Waze IA notebook.

The input source is the Medellin en Movimiento extract described in that
repository's data dictionary: ``fecha_siniestro``, ``hora_siniestro``,
``latitud``, ``longitud``, and GeoJSON point geometry (WGS84).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
import math
from typing import Any

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString, Point
from shapely.strtree import STRtree


REQUIRED_COLUMNS = {"fecha_siniestro", "hora_siniestro"}
DEFAULT_MAX_SNAP_DISTANCE_M = 60.0


@dataclass
class IncidentLoadReport:
    read: int = 0
    accepted: int = 0
    dropped_total: int = 0
    dropped_missing_date: int = 0
    dropped_invalid_date: int = 0
    dropped_invalid_hour: int = 0
    dropped_invalid_coordinates: int = 0
    dropped_duplicates: int = 0


def _hour(value: Any) -> int | None:
    """Return an hour in 0..23 from the dataset's hour strings or numbers."""
    if value is None or pd.isna(value):
        return None
    if isinstance(value, (datetime, time, pd.Timestamp)):
        return int(value.hour)
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        parsed = int(value)
        return parsed if float(value) == parsed and 0 <= parsed <= 23 else None
    text = str(value).strip()
    if not text:
        return None
    try:
        number = float(text)
        parsed = int(number)
        return parsed if number == parsed and 0 <= parsed <= 23 else None
    except ValueError:
        pass
    try:
        return int(pd.to_datetime(text, errors="raise").hour)
    except (TypeError, ValueError, OverflowError):
        return None


def load_incidents(path: str | Path) -> tuple[gpd.GeoDataFrame, IncidentLoadReport]:
    """Load and validate the actual GeoJSON/CSV extract without silent drops."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(
            f"No se encontró el extracto de siniestralidad: {path}. "
            "Consulte docs/ACCIDENTES.md para adquirirlo desde la fuente oficial."
        )
    if path.suffix.lower() in {".geojson", ".json"}:
        data = gpd.read_file(path)
    elif path.suffix.lower() == ".csv":
        data = pd.read_csv(path)
        if {"longitud", "latitud"}.issubset(data.columns):
            data = gpd.GeoDataFrame(
                data,
                geometry=gpd.points_from_xy(data.longitud, data.latitud),
                crs="EPSG:4326",
            )
        else:
            raise ValueError("El CSV debe incluir longitud y latitud.")
    else:
        raise ValueError("Formato no admitido; use GeoJSON o CSV.")

    missing = REQUIRED_COLUMNS - set(data.columns)
    if missing:
        raise ValueError(f"Faltan columnas obligatorias: {sorted(missing)}")
    if data.crs is None:
        # The documented ArcGIS extract requests WGS84. A source with no CRS
        # must otherwise be made explicit by its provider; do not guess here.
        raise ValueError("El dataset no declara CRS; se requiere EPSG:4326.")
    data = data.to_crs("EPSG:4326")
    report = IncidentLoadReport(read=len(data))

    dates = pd.to_datetime(data["fecha_siniestro"], errors="coerce", dayfirst=True)
    report.dropped_missing_date = int(data["fecha_siniestro"].isna().sum())
    report.dropped_invalid_date = int((data["fecha_siniestro"].notna() & dates.isna()).sum())
    hours = data["hora_siniestro"].map(_hour)
    report.dropped_invalid_hour = int(hours.isna().sum())

    valid_geometry = data.geometry.notna() & ~data.geometry.is_empty
    geom_lon = data.geometry.x.where(valid_geometry, pd.NA)
    geom_lat = data.geometry.y.where(valid_geometry, pd.NA)
    lon = pd.to_numeric(data["longitud"], errors="coerce") if "longitud" in data else geom_lon
    lat = pd.to_numeric(data["latitud"], errors="coerce") if "latitud" in data else geom_lat
    lon = lon.fillna(geom_lon)
    lat = lat.fillna(geom_lat)
    coords_ok = lon.between(-180, 180) & lat.between(-90, 90)
    report.dropped_invalid_coordinates = int((~coords_ok).sum())

    keep = dates.notna() & hours.notna() & coords_ok
    cleaned = data.loc[keep].copy()
    cleaned["event_date"] = dates.loc[keep]
    cleaned["hour"] = hours.loc[keep].astype(int)
    cleaned["geometry"] = [Point(x, y) for x, y in zip(lon.loc[keep], lat.loc[keep])]
    if "objectid" in cleaned:
        duplicate = cleaned["objectid"].duplicated(keep="first")
    else:
        duplicate = cleaned.assign(
            _date=cleaned.event_date,
            _hour=cleaned.hour,
            _lon=lon.loc[keep].round(5),
            _lat=lat.loc[keep].round(5),
        ).duplicated(["_date", "_hour", "_lon", "_lat"], keep="first")
    report.dropped_duplicates = int(duplicate.sum())
    cleaned = cleaned.loc[~duplicate].copy()
    report.accepted = len(cleaned)
    report.dropped_total = report.read - report.accepted
    return cleaned, report


def _graph_edges(graph) -> gpd.GeoDataFrame:
    """Build a directed GeoDataFrame retaining each OSM u/v/key identity."""
    rows = []
    for u, v, key, data in graph.edges(keys=True, data=True):
        geometry = data.get("geometry")
        if geometry is None:
            geometry = LineString([
                (graph.nodes[u]["x"], graph.nodes[u]["y"]),
                (graph.nodes[v]["x"], graph.nodes[v]["y"]),
            ])
        physical_id = (tuple(sorted((str(u), str(v)))), geometry.normalize().wkb_hex)
        rows.append({"u": u, "v": v, "key": key,
                     "physical_id": physical_id, "geometry": geometry})
    return gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")


def associate_and_aggregate(
    incidents: gpd.GeoDataFrame,
    graph,
    max_distance_m: float = DEFAULT_MAX_SNAP_DISTANCE_M,
    ambiguity_margin_m: float = 10.0,
) -> tuple[dict[tuple[Any, Any, Any, int], int], dict[str, Any]]:
    """Snap points to nearest directed OSM edge, reject distant matches.

    Counts are returned per (u, v, key, hour). Spatial proximity cannot prove
    that the reported event occurred on the selected road. Events are left
    unmatched if a second physical road lies within ``ambiguity_margin_m`` of
    the nearest candidate. The threshold and unmatched totals are retained.
    """
    if max_distance_m <= 0:
        raise ValueError("max_distance_m debe ser positivo.")
    if ambiguity_margin_m < 0:
        raise ValueError("ambiguity_margin_m no puede ser negativo.")
    edges = _graph_edges(graph)
    if edges.empty:
        raise ValueError("El grafo no tiene segmentos para asociar.")
    target_crs = "EPSG:3116"  # MAGNA-SIRGAS / Colombia Bogotá zone, meters.
    edges_m = edges.to_crs(target_crs)
    points = incidents[["hour", "geometry"]].to_crs(target_crs)
    edge_geometries = edges_m.geometry.to_numpy()
    physical_ids = edges.physical_id.to_numpy()
    tree = STRtree(edge_geometries)
    counts: dict[tuple[Any, Any, Any, int], int] = {}
    physical_edges: dict[Any, list[tuple[Any, Any, Any]]] = {}
    for row in edges.itertuples():
        physical_edges.setdefault(row.physical_id, []).append((row.u, row.v, row.key))

    associated_distances: list[float] = []
    samples: list[dict[str, Any]] = []
    unmatched = ambiguous = 0
    for point_index, point_row in points.iterrows():
        point = point_row.geometry
        candidate_indexes = tree.query(
            point, predicate="dwithin", distance=max_distance_m
        )
        candidates: dict[Any, tuple[float, int]] = {}
        for edge_index in candidate_indexes:
            edge_index = int(edge_index)
            physical_id = physical_ids[edge_index]
            distance = float(point.distance(edge_geometries[edge_index]))
            previous = candidates.get(physical_id)
            if previous is None or distance < previous[0]:
                candidates[physical_id] = (distance, edge_index)

        nearest = sorted(
            ((distance, physical_id, edge_index)
             for physical_id, (distance, edge_index) in candidates.items()),
            key=lambda candidate: (candidate[0], str(candidate[1])),
        )
        if not nearest:
            unmatched += 1
            continue
        if len(nearest) > 1 and nearest[1][0] - nearest[0][0] <= ambiguity_margin_m:
            ambiguous += 1
            unmatched += 1
            continue

        distance, physical_id, edge_index = nearest[0]
        associated_distances.append(distance)
        matched_edge = edges.iloc[edge_index]
        incident = incidents.loc[point_index]
        for u, v, key in physical_edges[physical_id]:
            identity = (u, v, key, int(point_row.hour))
            counts[identity] = counts.get(identity, 0) + 1
        if len(samples) < 10:
            edge_data = graph.get_edge_data(matched_edge.u, matched_edge.v, matched_edge.key) or {}
            samples.append({
                "objectid": incident.get("objectid"),
                "event_date": str(incident.get("event_date", "")),
                "hour": int(point_row.hour),
                "distance_m": round(distance, 2),
                "u": matched_edge.u,
                "v": matched_edge.v,
                "key": matched_edge.key,
                "street": str(edge_data.get("name", "")),
                "highway": str(edge_data.get("highway", "")),
            })

    distance_series = pd.Series(associated_distances, dtype=float)
    stats = {
        "incidents": int(len(incidents)),
        "associated": int(len(associated_distances)),
        "unmatched": int(unmatched),
        "ambiguous_nearby_roads": int(ambiguous),
        "max_distance_m": float(max_distance_m),
        "ambiguity_margin_m": float(ambiguity_margin_m),
        "distance_m_median": float(distance_series.median()) if len(distance_series) else None,
        "distance_m_p95": float(distance_series.quantile(0.95)) if len(distance_series) else None,
        "sample_associations": samples,
    }
    return counts, stats


def normalized_hourly_risk(
    counts: dict[tuple[Any, Any, Any, int], int],
) -> dict[tuple[Any, Any, Any, int], float]:
    """Normalize event frequency within each hour to [0, 1]."""
    maxima: dict[int, int] = {}
    for (_, _, _, hour), count in counts.items():
        maxima[hour] = max(maxima.get(hour, 0), count)
    return {
        edge_hour: count / maxima[edge_hour[3]] if maxima.get(edge_hour[3], 0) else 0.0
        for edge_hour, count in counts.items()
    }


def edge_risk(risk: dict, u, v, key, hour: int) -> float:
    """Look up normalized risk; absent records have no measured evidence."""
    if not 0 <= int(hour) <= 23:
        raise ValueError("La hora debe estar entre 0 y 23.")
    return float(risk.get((u, v, key, int(hour)), 0.0))


def balanced_edge_cost(travel_time_s: float, risk: float, beta: float) -> float:
    """Return ``t * (1 + beta * R)`` after validating the model inputs."""
    travel_time_s, risk, beta = float(travel_time_s), float(risk), float(beta)
    if not all(math.isfinite(x) for x in (travel_time_s, risk, beta)):
        raise ValueError("Tiempo, riesgo y beta deben ser finitos.")
    if travel_time_s < 0 or beta < 0 or not 0 <= risk <= 1:
        raise ValueError("Se requiere tiempo >= 0, beta >= 0 y riesgo en [0, 1].")
    return travel_time_s * (1.0 + beta * risk)
