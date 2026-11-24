import json
import unittest
from datetime import date

from proximidad import PharmacyIndex
from scrape_tarragona import (
    DETAIL, QUERY, Municipality, locate, merge_records, parse_page, run, to_rows,
)

TARGET = date(2026, 11, 3)


def card(cfci=188, name="Esplugas Solé, Laura", address="Jaume I, 46",
         town="Tarragona (CP: 43005)", phone="977139854"):
    return f"""
    <table class="table table-striped effect-fade in" id="tablaverde">
    <thead><tr><th width="23%"> </th><th width="70%">
    <A class='titularGuardia' HREF='mailto:of{cfci}@coft.org' target='forguar'>{name}</A>
    </th></tr></thead><tbody>
    <tr><td><b>Adreça</b></td><td>{address}
    (<a href="fitxafarma.php?cfci={cfci}" class='normal'>veure mapa</a>)</td></tr>
    <tr><td><b>Població</b></td><td>{town}</td></tr>
    <tr><td><b>Telèfon&nbsp;Urgències</b></td><td></td></tr>
    <tr><td><b>Telèfon</b></td><td>{phone}</td></tr>
    <tr><td><b>Observacions</b></td><td>Dilluns a diumenge.</td></tr>
    </tbody></table>
    """


def page(day=3, month="Novembre", diurnal="", nocturnal=""):
    return f"""
    <html><body>
    <h1><strong>Farmàcies de Guàrdia</strong>: El Tarragonès ({day} de {month})</h1>
    <h1><strong>Servei d'Urgències Diürn</strong> (9:00h a 22:00h)</h1>
    {diurnal}
    <h1><strong>Servei d'Urgències Nocturn</strong> (22:00h a 9:00h)</h1>
    {nocturnal}
    </body></html>
    """


class TarragonaTest(unittest.TestCase):
    def test_parse_merges_repeated_pharmacy_and_clips_night_to_requested_day(self):
        html = page(diurnal=card(188), nocturnal=card(188))
        records = parse_page(html, TARGET, "Tarragona")
        self.assertEqual(len(records), 1)
        row, = to_rows(records, TARGET)
        self.assertEqual(row["id"], "cof-tarragona-188")
        self.assertEqual(row["localidad"], "Tarragona")
        self.assertEqual(row["cp"], "43005")
        self.assertEqual(row["localidades_atendidas"], ["Tarragona"])
        self.assertEqual(
            [(item["inicio"], item["fin"], item["tipo"]) for item in row["guardia_intervalos"]],
            [
                ("2026-11-03T09:00:00", "2026-11-03T22:00:00", "Diürn"),
                ("2026-11-03T22:00:00", "2026-11-04T00:00:00", "Nocturn"),
            ],
        )
        self.assertEqual(row["guardia_intervalos"][1]["fin_original"],
                         "2026-11-04T09:00:00")
        self.assertIn("Servei d'Urgències Diürn", row["guardia_franja"])
        self.assertIn("Servei d'Urgències Nocturn", row["guardia_franja"])

    def test_previous_day_night_supplies_the_early_morning(self):
        previous = parse_page(
            page(day=2, diurnal=card(1, "Farmàcia Diürna"), nocturnal=card(188)),
            date(2026, 11, 2), "Renau")
        current = parse_page(
            page(diurnal=card(2, "Farmàcia Diürna")), TARGET, "Tarragona")
        rows = {row["id"]: row for row in to_rows(merge_records(previous + current), TARGET)}
        self.assertIn("cof-tarragona-188", rows)
        early = rows["cof-tarragona-188"]
        self.assertEqual(early["guardia_intervalos"][0]["inicio"], "2026-11-03T00:00:00")
        self.assertEqual(early["guardia_intervalos"][0]["fin"], "2026-11-03T09:00:00")
        self.assertEqual(early["guardia_intervalos"][0]["inicio_original"],
                         "2026-11-02T22:00:00")
        self.assertIn("madrugada del día anterior", early["guardia_franja"])
        self.assertEqual(early["localidades_atendidas"], ["Renau"])
        self.assertNotIn("cof-tarragona-1", rows)

    def test_rejects_changed_date_sections_or_identity(self):
        with self.assertRaises(ValueError):
            parse_page(page(day=4, diurnal=card()), TARGET, "Tarragona")
        with self.assertRaises(ValueError):
            parse_page(
                page(diurnal=card()).replace(
                    "Servei d'Urgències Diürn</strong> (9:00h a 22:00h)",
                    "Servei d'Urgències Mixt</strong> (9:00h a 22:00h)"),
                TARGET, "Tarragona")
        with self.assertRaises(ValueError):
            parse_page(
                page(diurnal=card()).replace(
                    "(9:00h a 22:00h)", "(horario desconocido)"), TARGET, "Tarragona")
        with self.assertRaises(ValueError):
            parse_page(
                page(diurnal=card()).replace(
                    "fitxafarma.php?cfci=188", "fitxafarma.php?cfci="),
                TARGET, "Tarragona")
        with self.assertRaises(ValueError):
            merge_records([
                {**parse_page(page(diurnal=card()), TARGET, "Tarragona")[0],
                 "localidades_atendidas": ["Tarragona"]},
                {**parse_page(page(diurnal=card(town="Reus (CP: 43201)")),
                              TARGET, "Reus")[0],
                 "localidades_atendidas": ["Reus"]},
            ])

    def test_marker_needs_one_identified_osm_pharmacy_within_fifteen_metres(self):
        row, = to_rows(parse_page(page(diurnal=card()), TARGET, "Tarragona"), TARGET)
        ref = {
            "id": "osm-77", "provincia": "Tarragona", "telefono": "977139854",
            "direccion": "Jaume I, 46", "localidad": "Tarragona",
            "lat": 41.12005, "lng": 1.25005,
        }
        marker = "lat: 41.1200, lng: 1.2500,\nlat: 41.1200, lng: 1.2500"
        self.assertTrue(locate(row, marker, PharmacyIndex([ref])))
        self.assertFalse(row["ubicacion_pendiente_verificacion"])
        self.assertLessEqual(row["ajuste_ubicacion_m"], 15)
        self.assertEqual(row["fuente_coordenadas"], DETAIL + "188")

        far, = to_rows(parse_page(page(diurnal=card()), TARGET, "Tarragona"), TARGET)
        self.assertFalse(locate(far, marker, PharmacyIndex([{**ref, "lat": 41.121}])))
        self.assertIsNone(far["lat"])

        ambiguous, = to_rows(parse_page(page(diurnal=card()), TARGET, "Tarragona"), TARGET)
        self.assertFalse(locate(ambiguous, marker, PharmacyIndex([
            ref, {**ref, "id": "osm-78", "lat": 41.12004, "lng": 1.25004}])))
        self.assertIsNone(ambiguous["lat"])

        with self.assertRaises(ValueError):
            locate(row, "<html>sin mapa</html>", PharmacyIndex([ref]))
        with self.assertRaises(ValueError):
            locate(row, "lat: 40.0, lng: 1.0", PharmacyIndex([ref]))

    def test_run_publishes_calendar_catalog_and_coverage(self):
        import tempfile
        from pathlib import Path

        def fake_request(url, params=None):
            if url == QUERY:
                day = int(params["DIA"])
                return page(day=day, diurnal=card(), nocturnal=card())
            if url.startswith(DETAIL):
                return "lat: 41.1200, lng: 1.2500"
            raise AssertionError(url)

        ref = {
            "id": "osm-77", "telefono": "977139854",
            "direccion": "Jaume I, 46", "localidad": "Tarragona",
            "lat": 41.12005, "lng": 1.25005,
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "farmacias-osm-espana.json").write_text(json.dumps([ref]))
            from unittest import mock
            with mock.patch("scrape_tarragona.fetch_municipalities",
                            return_value=[Municipality("130", "Tarragona")]), \
                 mock.patch("scrape_tarragona.request", side_effect=fake_request), \
                 mock.patch("scrape_tarragona.time.sleep", return_value=None):
                self.assertTrue(run(TARGET, output))
            coverage = json.loads(
                (output / "cobertura-tarragona-2026-11-03.json").read_text())
            duty = json.loads(
                (output / "guardias-tarragona-2026-11-03.json").read_text())
            catalog = json.loads((output / "farmacias-tarragona.json").read_text())
            self.assertTrue(coverage["completa"])
            self.assertEqual(coverage["farmacias_confirmadas"], 1)
            self.assertEqual(coverage["con_coordenadas"], 1)
            self.assertEqual(duty[0]["fuentes"], [QUERY.replace("gpobles.php", "guardies.php"),
                                                  DETAIL + "188"])
            self.assertFalse(duty[0]["ubicacion_pendiente_verificacion"])
            self.assertEqual(duty[0]["osm_id"], "osm-77")
            self.assertFalse(catalog[0]["guardia"])

    def test_invalid_response_does_not_replace_existing_calendar(self):
        import tempfile
        from pathlib import Path
        from unittest import mock

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "farmacias-osm-espana.json").write_text("[]")
            path = output / "guardias-tarragona-2026-11-03.json"
            path.write_text("previous")
            with mock.patch("scrape_tarragona.fetch_municipalities",
                            return_value=[Municipality("130", "Tarragona")]), \
                 mock.patch("scrape_tarragona.request",
                            return_value=page(day=4, diurnal=card())), \
                 mock.patch("scrape_tarragona.time.sleep", return_value=None):
                with self.assertRaises(ValueError):
                    run(TARGET, output)
            self.assertEqual(path.read_text(), "previous")


if __name__ == "__main__":
    unittest.main()
