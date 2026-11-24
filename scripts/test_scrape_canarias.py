"""Los calendarios de Canarias deben publicarse con el slug que pide el mapa."""
import re
import unicodedata
import unittest
from datetime import date
from pathlib import Path

import scrape_laspalmas
import scrape_tenerife
from scrape_mes import OUT_DIR, merge_schedule, slugify

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def map_slugify(value):
    """Réplica de slugify() en src/components/Mapa.jsx (sin acentos, todo a minúsculas)."""
    normalized = unicodedata.normalize("NFD", str(value or ""))
    normalized = "".join(char for char in normalized if unicodedata.category(char) != "Mn")
    return re.sub(r"^-|-$", "", re.sub(r"[^a-z0-9]+", "-", normalized.lower().strip()))


class CanaryOutputTests(unittest.TestCase):
    def test_output_files_use_the_province_slug_the_map_requests(self):
        for module in (scrape_laspalmas, scrape_tenerife):
            with self.subTest(provincia=module.PROVINCIA):
                self.assertEqual(module.SLUG, map_slugify(module.PROVINCIA))
                self.assertEqual(module.SLUG, slugify(module.PROVINCIA))

    def test_guard_and_coverage_paths_match_the_map_urls(self):
        target = date(2026, 11, 5)
        for module in (scrape_laspalmas, scrape_tenerife):
            with self.subTest(provincia=module.PROVINCIA):
                guard = OUT_DIR / f"guardias-{module.SLUG}-{target.isoformat()}.json"
                coverage = OUT_DIR / f"cobertura-{module.SLUG}-{target.isoformat()}.json"
                self.assertEqual(guard, OUT_DIR / f"guardias-{map_slugify(module.PROVINCIA)}-{target.isoformat()}.json")
                self.assertEqual(coverage, OUT_DIR / f"cobertura-{map_slugify(module.PROVINCIA)}-{target.isoformat()}.json")

    def test_canary_sources_are_wired_into_the_updater(self):
        updater = Path(__file__).resolve().parent / "daily_update.sh"
        sources = {
            words[1]
            for line in updater.read_text().splitlines()
            for words in [line.split()]
            if words and words[0] in ("dated", "dated_worker", "today_only", "dated_today_only")
        }
        self.assertIn("las-palmas", sources)
        self.assertIn("santa-cruz-de-tenerife", sources)

    def test_canary_provinces_are_in_the_official_catalogue(self):
        import json

        provinces = json.loads(DATA_DIR.joinpath("provincias.json").read_text())
        names = {map_slugify(province["nombre"]) for province in provinces}
        self.assertIn("las-palmas", names)
        self.assertIn("santa-cruz-de-tenerife", names)


class CanaryDutyMergeTests(unittest.TestCase):
    def test_two_shifts_of_the_same_pharmacy_become_one_row_with_both_franjas(self):
        rows = [
            {"id": "cof-tenerife-adeje-1", "nombre": "Farmacia Ejemplo",
             "guardia_franja": "00:00 - 09:00", "fecha_guardia": "2026-11-05"},
            {"id": "cof-tenerife-adeje-1", "nombre": "Farmacia Ejemplo",
             "guardia_franja": "09:00 - 23:59", "fecha_guardia": "2026-11-05"},
        ]
        merged = merge_schedule(rows)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["guardia_franjas"], ["00:00 - 09:00", "09:00 - 23:59"])
        self.assertEqual(merged[0]["guardia_franja"], "00:00 - 09:00 · 09:00 - 23:59")

    def test_repeated_rows_from_the_source_are_deduplicated(self):
        rows = [
            {"id": "cof-laspalmas-55379053", "nombre": "Limiñana Abreut, Nicolás María",
             "guardia_franja": "09:00 - 09:00 (+1)"},
            {"id": "cof-laspalmas-55379053", "nombre": "Limiñana Abreut, Nicolás María",
             "guardia_franja": "09:00 - 09:00 (+1)"},
        ]
        merged = merge_schedule(rows)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["guardia_franjas"], ["09:00 - 09:00 (+1)"])


if __name__ == "__main__":
    unittest.main()
