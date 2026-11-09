import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.acquire_incidents import acquire


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.headers = {"Content-Type": "application/json"}

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def get(self, url, params, timeout):
        if params == {"f": "json"}:
            fields = [{"name": name, "type": "esriFieldTypeString"} for name in [
                "fecha_siniestro", "hora_siniestro", "clase_siniestro", "gravedad_siniestro",
                "latitud", "longitud", "num_comuna", "nom_comuna",
            ]]
            fields.insert(0, {"name": "objectid", "type": "esriFieldTypeOID"})
            return FakeResponse({
                "fields": fields,
                "maxRecordCount": 2,
                "advancedQueryCapabilities": {"supportsPagination": True},
            })
        return FakeResponse({"count": 2})

    def post(self, url, data, timeout):
        if data.get("returnCountOnly") == "true":
            return FakeResponse({"count": 2})
        start = int(data["resultOffset"])
        end = start + int(data["resultRecordCount"])
        ids = [value for value in [1, 2][start:end]]
        features = [{
            "type": "Feature", "id": value,
            "properties": {"objectid": value, "fecha_siniestro": "2020-01-01", "hora_siniestro": "08:00:00"},
            "geometry": {"type": "Point", "coordinates": [-75.6, 6.2]},
        } for value in ids]
        return FakeResponse({"type": "FeatureCollection", "features": features})


class AcquisitionTests(unittest.TestCase):
    def test_acquire_streams_local_geojson_and_records_manifest(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            output = Path(directory) / "incidents.geojson"
            with patch("scripts.acquire_incidents.requests.Session", return_value=FakeSession()):
                manifest = acquire(output, batch_size=1)
            data = json.loads(output.read_text(encoding="utf-8"))
            saved_manifest = json.loads(output.with_suffix(".geojson.manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(data["features"]), 2)
        self.assertEqual(manifest["features_downloaded"], 2)
        self.assertEqual(saved_manifest["sha256"], manifest["sha256"])

    def test_acquire_refuses_to_overwrite_existing_dataset(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            output = Path(directory) / "incidents.geojson"
            output.write_text("keep me", encoding="utf-8")
            with patch("scripts.acquire_incidents.requests.Session") as session:
                with self.assertRaises(FileExistsError):
                    acquire(output)
                session.assert_not_called()
            self.assertEqual(output.read_text(encoding="utf-8"), "keep me")

    def test_acquire_rejects_html_error_pages(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            output = Path(directory) / "incidents.geojson"
            response = FakeResponse({"html": "error"})
            response.headers["Content-Type"] = "text/html"
            with patch("scripts.acquire_incidents.requests.Session") as session_factory:
                session_factory.return_value.get.return_value = response
                with self.assertRaisesRegex(RuntimeError, "Content-Type"):
                    acquire(output)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
