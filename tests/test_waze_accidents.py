import heapq
import json
import math
import tempfile
import unittest
from pathlib import Path
import time

import geopandas as gpd
import networkx as nx
import pandas as pd
from shapely.geometry import LineString, Point

from waze_communes import (
    assign_incidents_to_communes,
    build_commune_frequency,
    choose_lower_frequency_alternative,
    map_edges_to_communes,
    normalize_commune_id,
)

from waze_accidents import (
    associate_and_aggregate,
    balanced_edge_cost,
    edge_risk,
    load_incidents,
    normalized_hourly_risk,
)


class CostAndRiskTests(unittest.TestCase):
    def test_hourly_normalization_and_missing_segments(self):
        counts = {(1, 2, 0, 8): 2, (2, 3, 0, 8): 4, (3, 4, 0, 9): 1}
        risk = normalized_hourly_risk(counts)
        self.assertEqual(risk[(1, 2, 0, 8)], 0.5)
        self.assertEqual(risk[(2, 3, 0, 8)], 1.0)
        self.assertEqual(risk[(3, 4, 0, 9)], 1.0)
        self.assertEqual(edge_risk(risk, 9, 10, 0, 8), 0.0)

    def test_balanced_cost_beta_zero_and_increasing(self):
        self.assertEqual(balanced_edge_cost(100, 1.0, 0), 100)
        self.assertEqual(balanced_edge_cost(100, 0.0, 1), 100)
        self.assertEqual(balanced_edge_cost(100, 0.25, 2), 150)
        self.assertGreater(balanced_edge_cost(100, 1.0, 2), balanced_edge_cost(100, 1.0, 0.5))

    def test_balanced_cost_rejects_invalid_values(self):
        for inputs in [(-1, 0, 0), (1, 1.1, 0), (1, 0, -1)]:
            with self.subTest(inputs=inputs), self.assertRaises(ValueError):
                balanced_edge_cost(*inputs)


class CommuneFrequencyTests(unittest.TestCase):
    def test_special_official_codes_are_not_mistaken_for_commune_numbers(self):
        self.assertEqual(normalize_commune_id("SN01"), "SN01")
        self.assertEqual(normalize_commune_id("Comuna 01"), "1")

    def test_points_use_official_polygons_with_audited_attribute_fallback(self):
        from shapely.geometry import box

        boundaries = gpd.GeoDataFrame(
            {
                "comuna_id": ["1", "2"],
                "comuna_name": ["Uno", "Dos"],
            },
            geometry=[box(0, 0, 1, 1), box(1, 0, 2, 1)],
            crs="EPSG:4326",
        )
        incidents = gpd.GeoDataFrame(
            {
                "objectid": [1, 2, 3, 4],
                "num_comuna": ["Comuna 2", None, "Comuna 1", None],
                "hour": [8, 8, 9, 9],
            },
            geometry=[Point(0.5, 0.5), Point(1.5, 0.5), Point(3, 3), Point(4, 4)],
            crs="EPSG:4326",
        )
        assigned, stats = assign_incidents_to_communes(incidents, boundaries)
        self.assertEqual(assigned.comuna_id.iloc[:3].tolist(), ["1", "2", "1"])
        self.assertTrue(pd.isna(assigned.comuna_id.iloc[3]))
        self.assertEqual(stats["assigned_by_point_in_polygon"], 2)
        self.assertEqual(stats["assigned_by_source_attribute_fallback"], 1)
        self.assertEqual(stats["source_code_vs_polygon_mismatch"], 1)
        self.assertEqual(stats["unassigned"], 1)

    def test_osm_directed_edges_are_mapped_by_midpoint_to_official_polygon(self):
        from shapely.geometry import box

        graph = nx.MultiDiGraph()
        graph.add_node(1, x=0.25, y=0.5)
        graph.add_node(2, x=0.75, y=0.5)
        graph.add_node(3, x=1.75, y=0.5)
        graph.add_edge(1, 2, key=0)
        graph.add_edge(2, 1, key=0)
        graph.add_edge(2, 3, key=0)
        boundaries = gpd.GeoDataFrame(
            {"comuna_id": ["1", "2"], "comuna_name": ["Uno", "Dos"]},
            geometry=[box(0, 0, 1, 1), box(1, 0, 2, 1)],
            crs="EPSG:4326",
        )
        edge_units, names = map_edges_to_communes(graph, boundaries)
        self.assertEqual(edge_units[(1, 2, 0)], "1")
        self.assertEqual(edge_units[(2, 1, 0)], "1")
        self.assertEqual(edge_units[(2, 3, 0)], "2")
        self.assertEqual(names["2"], "Dos")

    def test_hourly_counts_normalize_and_missing_communes_are_zero(self):
        frame = pd.DataFrame([
            {"num_comuna": "01", "hour": 8, "event_count": 4},
            {"num_comuna": "2", "hour": 8, "event_count": 2},
            {"num_comuna": "1", "hour": 9, "event_count": 3},
        ])
        counts, risk = build_commune_frequency(frame)
        self.assertEqual(counts[("1", 8)], 4)
        self.assertEqual(risk[("1", 8)], 1.0)
        self.assertEqual(risk[("2", 8)], 0.5)
        self.assertNotIn(("3", 8), counts)

    def test_alternative_requires_distinct_slower_lower_frequency_route(self):
        fast = {"route_edges": [(1, 2, 0)], "edge_details": [
            {"base_time_s": 10, "historical_count": 8, "historical_risk_normalized": 1.0}
        ]}
        worse = {"found": True, "route_edges": [(1, 3, 0)], "edge_details": [
            {"base_time_s": 12, "historical_count": 7, "historical_risk_normalized": 0.5}
        ]}
        self.assertIs(choose_lower_frequency_alternative(fast, [worse]), worse)
        self.assertIsNone(choose_lower_frequency_alternative(fast, [fast]))


class DataTests(unittest.TestCase):
    def test_geojson_validation_reports_invalid_rows_and_duplicates(self):
        rows = [
            {"objectid": 1, "fecha_siniestro": "2020-01-02", "hora_siniestro": "08:30:00", "latitud": 6.1, "longitud": -75.6, "geometry": Point(-75.6, 6.1)},
            {"objectid": 1, "fecha_siniestro": "2020-01-02", "hora_siniestro": "08:30:00", "latitud": 6.1, "longitud": -75.6, "geometry": Point(-75.6, 6.1)},
            {"objectid": 3, "fecha_siniestro": "no-date", "hora_siniestro": "08:00", "latitud": 6.1, "longitud": -75.6, "geometry": Point(-75.6, 6.1)},
            {"objectid": 4, "fecha_siniestro": "2020-01-03", "hora_siniestro": "25:00", "latitud": 6.1, "longitud": -75.6, "geometry": Point(-75.6, 6.1)},
            {"objectid": 5, "fecha_siniestro": "2020-01-04", "hora_siniestro": "09:00", "latitud": 100.0, "longitud": 200.0, "geometry": None},
            {"objectid": 6, "fecha_siniestro": None, "hora_siniestro": "10:00", "latitud": 6.1, "longitud": -75.6, "geometry": Point(-75.6, 6.1)},
        ]
        frame = gpd.GeoDataFrame(rows, geometry="geometry", crs="EPSG:4326")
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "incidents.geojson"
            frame.to_file(path, driver="GeoJSON")
            accepted, report = load_incidents(path)
        self.assertEqual(len(accepted), 1)
        self.assertEqual(int(accepted.iloc[0].hour), 8)
        self.assertEqual(report.dropped_duplicates, 1)
        self.assertEqual(report.dropped_total, 5)
        self.assertEqual(report.dropped_invalid_date, 1)
        self.assertEqual(report.dropped_invalid_hour, 1)
        self.assertEqual(report.dropped_invalid_coordinates, 1)
        self.assertEqual(report.dropped_missing_date, 1)

    def test_nearest_association_preserves_direction_and_threshold(self):
        graph = nx.MultiDiGraph()
        graph.add_node(1, x=-75.6, y=6.1)
        graph.add_node(2, x=-75.599, y=6.1)
        graph.add_node(3, x=-75.5, y=6.2)
        line = LineString([(-75.6, 6.1), (-75.599, 6.1)])
        graph.add_edge(1, 2, key=0, geometry=line)
        graph.add_edge(2, 1, key=0, geometry=LineString(list(line.coords)[::-1]))
        graph.add_edge(2, 3, key=0, geometry=LineString([(-75.599, 6.1), (-75.5, 6.2)]))
        incidents = gpd.GeoDataFrame(
            {"hour": [8, 8]}, geometry=[Point(-75.5995, 6.10001), Point(-75.4, 6.3)], crs="EPSG:4326"
        )
        counts, stats = associate_and_aggregate(incidents, graph, max_distance_m=60)
        self.assertEqual(stats["associated"], 1)
        self.assertEqual(stats["unmatched"], 1)
        self.assertEqual(counts[(1, 2, 0, 8)], 1)
        self.assertEqual(counts[(2, 1, 0, 8)], 1)

    def test_association_rejects_points_ambiguous_between_parallel_roads(self):
        graph = nx.MultiDiGraph()
        lower = [(-75.6005, 6.2), (-75.5995, 6.2)]
        upper = [(-75.6005, 6.2001), (-75.5995, 6.2001)]
        for node, coordinate in enumerate(lower + upper, start=1):
            graph.add_node(node, x=coordinate[0], y=coordinate[1])
        graph.add_edge(1, 2, key=0, geometry=LineString(lower))
        graph.add_edge(3, 4, key=0, geometry=LineString(upper))
        incidents = gpd.GeoDataFrame(
            {"hour": [8]}, geometry=[Point(-75.6, 6.20005)], crs="EPSG:4326"
        )
        counts, stats = associate_and_aggregate(
            incidents, graph, max_distance_m=30, ambiguity_margin_m=10
        )
        self.assertEqual(counts, {})
        self.assertEqual(stats["associated"], 0)
        self.assertEqual(stats["ambiguous_nearby_roads"], 1)


class NotebookAStarTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        notebook_path = Path(__file__).parents[1] / "WAZE_IA_CORRECCION.ipynb"
        notebook = json.loads(notebook_path.read_text(encoding="utf-8"))
        namespace = {
            "heapq": heapq,
            "math": math,
            "time": time,
            "pd": pd,
            "edge_risk": edge_risk,
            "ACCIDENT_COUNTS": {},
            "DATA_MODE": "real",
            "DEMO_ZONE_BY_EDGE": {},
            "DEMO_COUNTS": {},
            "DEMO_RISK": {},
        }
        sources = ["".join(cell["source"]) for cell in notebook["cells"] if cell["cell_type"] == "code"]
        for source in sources:
            compile(source, str(notebook_path), "exec")
        node_source = next(source for source in sources if "class OSMRouteNode(Node):" in source)
        tree_source = next(source for source in sources if "class OSMRouteTree(Tree):" in source)
        result_source = next(source for source in sources if "def result_from_goal" in source)
        summary_source = next(source for source in sources if "def summarize_composite_result" in source)
        exec(node_source, namespace)
        exec(tree_source, namespace)
        exec(result_source, namespace)
        exec(summary_source, namespace)
        cls.namespace = namespace

    def make_graph(self):
        graph = nx.MultiDiGraph()
        coordinates = {1: -75.6000, 2: -75.5998, 3: -75.5999, 4: -75.5997}
        for node, lon in coordinates.items():
            graph.add_node(node, x=lon, y=6.2, street_count=2)
        def edge(a, b, seconds):
            graph.add_edge(a, b, key=0, travel_time=seconds, speed_kph=50,
                           highway="residential", length=seconds * 10, bearing=0)
        edge(1, 2, 3)
        edge(2, 4, 3)
        edge(1, 3, 3.5)
        edge(3, 4, 3.5)
        edge(1, 4, 8)
        return graph

    def search(self, graph, beta, risk, hour=8):
        ns = self.namespace
        config = {
            "highway_time_factors": {}, "default_highway_time_factor": 1.0,
            "intersection_delay_s_by_street_count": {},
            "turn_delay_s": {"recto": 0.0, "giro": 0.0, "retorno": 0.0},
        }
        ns["OSMRouteNode"].configure(graph, config, risk, hour=hour, beta=beta)
        root = ns["OSMRouteNode"](
            state=(None, 1, None), value="root", objective=4, accumulated_cost=0.0
        )
        tree = ns["OSMRouteTree"](root)
        result = ns["result_from_goal"](tree.aStar(4), tree)
        return result

    def test_astar_fastest_and_balanced_choose_expected_directed_paths(self):
        graph = self.make_graph()
        fast = self.search(graph, 0.0, {})
        risk = {(1, 2, 0, 8): 1.0, (2, 4, 0, 8): 1.0}
        balanced = self.search(graph, 2.0, risk)
        other_hour = self.search(graph, 2.0, risk, hour=9)
        self.assertEqual(fast["route_nodes"], [1, 2, 4])
        self.assertEqual(balanced["route_nodes"], [1, 3, 4])
        self.assertEqual(other_hour["route_nodes"], [1, 2, 4])
        self.assertEqual(fast["total_cost_s"], 6)
        self.assertEqual(balanced["total_cost_s"], 7)
        self.assertTrue(math.isclose(sum(x["total_edge_cost_s"] for x in balanced["edge_details"]), balanced["total_cost_s"]))
        ns = self.namespace
        ns["OSMRouteNode"].configure(graph, {
            "highway_time_factors": {}, "default_highway_time_factor": 1.0,
            "intersection_delay_s_by_street_count": {},
            "turn_delay_s": {"recto": 0.0, "giro": 0.0, "retorno": 0.0},
        }, {}, hour=8, beta=0)
        root = ns["OSMRouteNode"](
            state=(None, 1, None), value="root", objective=4, accumulated_cost=0.0
        )
        self.assertLessEqual(root.heuristic(), fast["total_cost_s"])
        summary = ns["summarize_composite_result"](balanced)
        self.assertNotIn("Frecuencia histórica agregada (conteos)", summary.index)
        ns["ACCIDENT_COUNTS"] = {(1, 2, 0, 8): 1}
        summary_with_data = ns["summarize_composite_result"](balanced)
        self.assertIn("Frecuencia histórica agregada (conteos)", summary_with_data.index)

    def test_synthetic_zone_penalty_changes_route_and_is_explicit(self):
        graph = self.make_graph()
        ns = self.namespace
        old = {key: ns.get(key) for key in ("DATA_MODE", "DEMO_ZONE_BY_EDGE", "DEMO_COUNTS", "DEMO_RISK")}
        try:
            ns["DATA_MODE"] = "synthetic_demo"
            ns["DEMO_ZONE_BY_EDGE"] = {
                (1, 2, 0): "DEMO-01", (2, 4, 0): "DEMO-01",
                (1, 3, 0): "DEMO-02", (3, 4, 0): "DEMO-02",
                (1, 4, 0): "DEMO-02",
            }
            ns["DEMO_COUNTS"] = {("DEMO-01", 8): 9, ("DEMO-02", 8): 2}
            ns["DEMO_RISK"] = {("DEMO-01", 8): 1.0, ("DEMO-02", 8): 0.1}
            result = self.search(graph, 2.0, {})
            self.assertEqual(result["route_nodes"], [1, 3, 4])
            self.assertEqual(result["edge_details"][0]["comuna_id"], "DEMO-02")
            self.assertTrue(math.isclose(
                result["total_cost_s"],
                sum(row["total_edge_cost_s"] for row in result["edge_details"]),
            ))
        finally:
            for key, value in old.items():
                if value is None:
                    ns.pop(key, None)
                else:
                    ns[key] = value

    def test_astar_reports_unreachable_directed_destination(self):
        graph = nx.MultiDiGraph()
        graph.add_node(1, x=-75.6, y=6.2)
        graph.add_node(2, x=-75.6, y=6.2)
        graph.add_edge(1, 2, key=0, travel_time=1, speed_kph=50, length=10, bearing=0)
        ns = self.namespace
        config = {
            "highway_time_factors": {}, "default_highway_time_factor": 1.0,
            "intersection_delay_s_by_street_count": {},
            "turn_delay_s": {"recto": 0.0, "giro": 0.0, "retorno": 0.0},
        }
        ns["OSMRouteNode"].configure(graph, config, {}, hour=8, beta=0)
        tree = ns["OSMRouteTree"](ns["OSMRouteNode"](
            state=(None, 2, None), value="root", objective=1, accumulated_cost=0
        ))
        result = ns["result_from_goal"](tree.aStar(1), tree)
        self.assertFalse(result["found"])


if __name__ == "__main__":
    unittest.main()
