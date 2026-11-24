import unittest
from datetime import date

from proximidad import PharmacyIndex
from scrape_lleida import build_rows, locate, parse_zone_page, zone_segments

TARGET = date(2026, 11, 3)


def city_entry(ident, name, address, phone):
    return f"""<tr><td><a HREF="/cat/farmacies/farmacies.asp?Detall=Farmacia&amp;IdFarmacia={ident}&amp;PaginaAnterior=">{name}</a> </td>
    <td style="width:33%">{address}<br/>Lleida, El Segri&#224;</td>
    <td style="width:33%">Tel. Farm&agrave;cia: {phone}</td></tr>"""


def city_block(ident, name, address, phone):
    return f"""<div class="dades"><a HREF="/cat/farmacies/farmacies.asp?Detall=Farmacia&amp;IdFarmacia={ident}"><b>{name}</b></a><br>
    {address}<br> Lleida<br> Tel. Farm&agrave;cia: {phone}<br> </div>"""


def city_page(day, entries_day, entries_night):
    return f"""<html><b>TOT EL MES</b>
    <td class="TitolSubSeccioVermell">{day} de novembre - FARM&Agrave;CIES DE GU&Agrave;RDIA OBERTES
    <span><b>de 9 a 22 hores</b></span></td>{entries_day}
    <td class="TitolSubSeccioVermell">{day} de novembre - GU&Agrave;RDIA DE NIT
    <span><b>de 22 a 9 hores</b></span></td>{entries_night}
    <a HREF="">Tornar</a></html>"""


def rural_entry(ident, name, label, lines):
    return (f'<div class="dades"><a HREF="farmacies.asp?Detall=Farmacia&amp;IdFarmacia={ident}">'
            f'<b>{name}</b></a> <br/> <b>{label}</b> <br/>{"<br/>".join(lines)}</div>')


def rural_page(day, entries, rule="Els torns canvien els dijous a les 20 hores"):
    return f"""<html><b>TOT EL MES</b> {rule} AGRAMUNT
    A totes les poblacions a excepci&oacute; de Lleida ciutat, les gu&agrave;rdies s&oacute;n LOCALITZADES.
    <td ALIGN="CENTER" class="titolSubseccioBlanc">{day} de novembre</td>{entries}
    <a HREF="">Tornar</a></html>"""


def agramunt(ident, name):
    return rural_entry(ident, name, "DIA de 9 a 24 hores",
                       ["Pl. del Pou, 8, 25310", "Agramunt", "Tel. Farm&agrave;cia: 973390123"])


class ParseTest(unittest.TestCase):
    def test_city_page_reads_day_and_night_sections(self):
        page = city_page(3, city_entry(336, "Barios Baquero, Rosal&#237;a", "Riu Ebre, 10", "973211855"),
                         city_block(102, "Garr&#243;s Iglesias, Esther", "Prat de la Riba, 53", "973232112"))
        parsed = parse_zone_page(page, TARGET)
        self.assertEqual(parsed["tipo"], "diaria")
        self.assertEqual([(e["id"], e["turno"], e["localidad"]) for e in parsed["fichas"]],
                         [("336", "DIA de 9 a 22 hores", "Lleida"), ("102", "NIT de 22 a 9 hores", "Lleida")])
        self.assertEqual(parsed["fichas"][1]["telefono"], "973232112")

    def test_rural_page_reads_change_rule_postcode_and_localized_duty(self):
        parsed = parse_zone_page(rural_page(3, agramunt(7, "Esteve Gatnau, Gilbert")), TARGET)
        self.assertEqual((parsed["tipo"], parsed["cambio"]), ("semanal", (3, 20)))
        entry = parsed["fichas"][0]
        self.assertEqual((entry["direccion"], entry["localidad"], entry["cp"], entry["localizada"]),
                         ("Pl. del Pou, 8", "Agramunt", "25310", True))

    def test_rejects_wrong_day_unknown_shift_and_missing_rule(self):
        with self.assertRaisesRegex(ValueError, "no confirma el día"):
            parse_zone_page(rural_page(4, agramunt(7, "A")), TARGET)
        bad = rural_entry(7, "A", "TARDA de 15 a 20 hores", ["C. Major, 1", "Agramunt"])
        with self.assertRaisesRegex(ValueError, "número de fichas|Turno desconocido"):
            parse_zone_page(rural_page(3, bad), TARGET)
        with self.assertRaisesRegex(ValueError, "cambio de turno"):
            parse_zone_page(rural_page(3, agramunt(7, "A"), rule=""), TARGET)

    def test_closed_pharmacy_is_never_published_and_keeps_official_referral(self):
        closed = rural_entry(9, "Ponton, F. (Farm&agrave;cia TANCADA DEFINITIVAMENT)", "DIA I NIT",
                             ["Farm&agrave;cia de GU&Agrave;RDIA m&eacute;s propera: VALL D'ARAN (veure Vielha)",
                              "Vilaller"])
        parsed = parse_zone_page(rural_page(3, closed), TARGET)
        self.assertEqual(parsed["fichas"], [])
        self.assertIn("VALL D'ARAN", parsed["cerradas"][0])
        reports = []
        self.assertEqual(build_rows(TARGET, {"1": "Vilaller"}, lambda z, d: rural_page(d.day, closed),
                                    reports), [])
        self.assertEqual(reports[0]["estado"], "disponible")
        self.assertIn("VALL D'ARAN", reports[0]["nota"])

    def test_zone_without_defined_duty(self):
        page = ("<b>TOT EL MES</b> Els torns canvien els dijous a les 20 hores CALAF "
                "No hi ha definides farm&agrave;cies de gu&agrave;rdia per aquesta poblaci&oacute;. "
                "<a>Tornar</a>")
        self.assertEqual(zone_segments(TARGET, "1", lambda z, d: page), ([], [
            "No hi ha definides farmàcies de guàrdia per aquesta població."]))
        reports = []
        self.assertEqual(build_rows(TARGET, {"1": "Calaf"}, lambda z, d: page, reports), [])
        self.assertEqual(reports[0]["estado"], "parcial")
        self.assertIn("confirmar por teléfono", reports[0]["error"])
        self.assertIn("No hi ha definides", reports[0]["nota"])


class SegmentsTest(unittest.TestCase):
    def test_city_includes_early_morning_of_previous_night(self):
        def fetch(zone, day):
            night = city_block(102 if day == TARGET else 55, "Garr&#243;s" if day == TARGET else "Vila",
                               "Prat de la Riba, 53" if day == TARGET else "Major, 2", "973232112")
            return city_page(day.day, city_entry(336, "Barios", "Riu Ebre, 10", "973211855"), night)

        rows = build_rows(TARGET, {"14": "Lleida"}, fetch, [])
        hours = {r["id"]: [(i["inicio"][11:16], i["fin"][11:16]) for i in r["guardia_intervalos"]]
                 for r in rows}
        self.assertEqual(hours["cof-lleida-55"], [("00:00", "09:00")])
        self.assertEqual(hours["cof-lleida-102"], [("22:00", "00:00")])
        self.assertEqual(hours["cof-lleida-336"], [("09:00", "22:00")])
        self.assertIn("Madrugada del turno anterior: NIT de 22 a 9 hores", rows[0]["guardia_franja"])

    def test_weekly_change_day_splits_outgoing_and_incoming(self):
        thursday = date(2026, 11, 5)

        def fetch(zone, day):
            return rural_page(day.day, agramunt(7, "Saliente") if day <= thursday else agramunt(8, "Entrante"))

        rows = build_rows(thursday, {"21": "Agramunt"}, fetch, [])
        spans = {r["nombre"]: [(i["inicio"][8:16], i["fin"][8:16]) for i in r["guardia_intervalos"]]
                 for r in rows}
        self.assertEqual(spans, {"Saliente": [("05T09:00", "05T20:00")],
                                 "Entrante": [("05T20:00", "06T00:00")]})
        self.assertIn("guardia localizada", rows[0]["guardia_franja"])

    def test_zone_error_is_reported_and_other_zones_continue(self):
        def fetch(zone, day):
            if zone == "1":
                return "<html>mantenimiento</html>"
            return rural_page(day.day, agramunt(7, "A"))

        reports = []
        rows = build_rows(TARGET, {"1": "Rota", "2": "Agramunt"}, fetch, reports)
        self.assertEqual([r["id"] for r in rows], ["cof-lleida-7"])
        self.assertEqual([r["estado"] for r in reports], ["error", "disponible"])


class LocateTest(unittest.TestCase):
    def row(self):
        return {"id": "cof-lleida-102", "nombre": "Garrós Iglesias, Esther",
                "direccion": "Prat de la Riba, 53", "localidad": "Lleida", "telefono": "973232112",
                "lat": None, "lng": None, "ubicacion_pendiente_verificacion": True}

    def test_accepts_single_official_point_matching_one_osm_pharmacy(self):
        index = PharmacyIndex([{"id": "osm-1", "nombre": "Farmàcia Garrós", "lat": 41.6201,
                                "lng": 0.6301, "provincia": "Lleida"}])
        row = self.row()
        self.assertTrue(locate(row, "new google.maps.LatLng(41.6200, 0.6300)", index))
        self.assertEqual((row["lat"], row["lng"], row["ubicacion_pendiente_verificacion"], row["osm_id"]),
                         (41.62, 0.63, False, "osm-1"))

    def test_rejects_ambiguous_or_unmatched_points(self):
        index = PharmacyIndex([{"id": "osm-1", "nombre": "Farmàcia Pérez", "lat": 41.6201,
                                "lng": 0.6301, "provincia": "Lleida"}])
        row = self.row()
        self.assertFalse(locate(row, "LatLng(41.6200, 0.6300)", index))
        self.assertIsNone(row["lat"])
        with self.assertRaises(ValueError):
            locate(row, "LatLng(41.62, 0.63) LatLng(41.63, 0.64)", index)


if __name__ == "__main__":
    unittest.main()
