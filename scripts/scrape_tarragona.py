#!/usr/bin/env python3
"""Importa las guardias fechadas del Colegio Oficial de Farmacéuticos de Tarragona.

La consulta oficial es municipal: se recorren todas las poblaciones del listado
del Colegio y se combinan las fichas repetidas por código de farmacia. Para un
día se consultan también las guardias nocturnas de la víspera, que cubren la
madrugada hasta las 9:00.
"""
import argparse
import html
import json
import re
import time
import unicodedata
import urllib.parse
import urllib.request
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from typing import NamedTuple
from zoneinfo import ZoneInfo

from merge_espana import coordinates, identity_keys
from provincial_catalog import publish
from proximidad import PharmacyIndex, fold, phone_digits
from scrape_mes import OUT_DIR, write_json

SOURCE = "https://www.coft.cat/font/guardies.php"
QUERY = "https://www.coft.cat/font/gpobles.php"
DETAIL = "https://www.coft.cat/font/fitxafarma.php?cfci="
TZ = ZoneInfo("Europe/Madrid")
HEADERS = {"User-Agent": "FarmaciasDeGuardiaTracker/1.0"}

MONTHS = {
    "gener": 1, "febrer": 2, "marc": 3, "abril": 4, "maig": 5, "juny": 6,
    "juliol": 7, "agost": 8, "setembre": 9, "octubre": 10, "novembre": 11,
    "desembre": 12,
}
TITLE = re.compile(
    r"<h1[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
TITLE_DATE = re.compile(
    r"\((\d{1,2})\s+(?:de|d['’])\s*([^)]+)\)", re.IGNORECASE)
SECTION = re.compile(
    r"<h1[^>]*>\s*<strong>\s*(Servei d'Urgències\s+(Diürn|Nocturn))\s*</strong>\s*"
    r"(\([^<]*\))\s*</h1>", re.IGNORECASE | re.DOTALL)
CARD = re.compile(
    r'<table[^>]*class="[^"]*table-striped[^"]*"[^>]*>.*?</table>',
    re.IGNORECASE | re.DOTALL)
TITULAR = re.compile(
    r"<a[^>]*class=['\"]titularGuardia['\"][^>]*>(.*?)</a>",
    re.IGNORECASE | re.DOTALL)
CFCI = re.compile(r"fitxafarma\.php\?cfci=(\d+)", re.IGNORECASE)
ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL)
HOURS = re.compile(
    r"(\d{1,2}):(\d{2})\s*h?\s*a\s*(\d{1,2}):(\d{2})\s*h?", re.IGNORECASE)
MARKER = re.compile(
    r"\blat\s*:\s*(-?\d+(?:\.\d+)?)\s*,\s*\blng\s*:\s*(-?\d+(?:\.\d+)?)",
    re.IGNORECASE)
MUNICIPALITY_FORM = re.compile(
    r"<form[^>]*action=['\"]gpobles\.php['\"][^>]*>.*?</form>",
    re.IGNORECASE | re.DOTALL)
OPTION = re.compile(
    r"<option[^>]*value=['\"]?(\d+)['\"]?[^>]*>(.*?)</option>",
    re.IGNORECASE | re.DOTALL)


class Municipality(NamedTuple):
    code: str
    name: str


def clean_text(value):
    value = html.unescape(re.sub(r"<[^>]*>", " ", value or ""))
    return " ".join(value.replace("\xa0", " ").split())


def request(url, params=None):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=45) as response:
        content = response.read()
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return content.decode("iso-8859-1")


def fetch_municipalities(page=None):
    """Poblacions (totes) del formulario oficial del Colegio."""
    page = request(SOURCE) if page is None else page
    form = MUNICIPALITY_FORM.search(page)
    if not form or "GuardiesForm" not in form.group(0):
        raise ValueError("El Colegio no publica el formulario de poblaciones esperado.")
    items = []
    for code, name in OPTION.findall(form.group(0)):
        name = clean_text(name)
        if not code or not name:
            raise ValueError("Población oficial sin código o nombre.")
        items.append(Municipality(code, name))
    if len(items) < 100 or len({item.code for item in items}) != len(items):
        raise ValueError(f"Listado oficial de poblaciones incompleto: {len(items)}.")
    return items


def query_url(target, municipality):
    values = {"CODI": municipality.code, "MES": str(target.month),
              "DIA": str(target.day), "CONSUL": "OK"}
    return f"{QUERY}?{urllib.parse.urlencode(values)}"


def title_date(page, target):
    for match in TITLE.finditer(page):
        text = clean_text(match.group(1))
        if "Farmàcies de Guàrdia" not in text:
            continue
        found = TITLE_DATE.search(text)
        if not found or int(found.group(1)) != target.day:
            raise ValueError(f"La página no confirma el día solicitado: {text!r}")
        month = MONTHS.get(fold(found.group(2)))
        if month != target.month:
            raise ValueError(f"La página no confirma el mes solicitado: {text!r}")
        return text
    raise ValueError("La página no contiene el título provincial de guardias.")


def section_blocks(page, target):
    found = list(SECTION.finditer(page))
    if not found:
        return []
    result = []
    seen = set()
    for index, match in enumerate(found):
        kind = match.group(2).capitalize()
        if kind in seen:
            raise ValueError(f"Sección de guardia repetida: {kind}.")
        seen.add(kind)
        text = clean_text(f"{match.group(1)} {match.group(3)}")
        hours = HOURS.search(clean_text(match.group(3)))
        if not hours:
            raise ValueError(f"Franja oficial no reconocida: {text!r}")
        h1, m1, h2, m2 = map(int, hours.groups())
        begin = datetime.combine(target, dt_time(h1, m1))
        end = datetime.combine(target, dt_time(h2, m2))
        if kind == "Diürn":
            if end <= begin:
                raise ValueError(f"Franja diurna inválida: {text!r}")
        else:
            if end >= begin:
                raise ValueError(f"Franja nocturna inválida: {text!r}")
            end += timedelta(days=1)
        finish = found[index + 1].start() if index + 1 < len(found) else len(page)
        result.append(({"tipo": kind, "texto": text, "inicio": begin, "fin": end},
                       match.end(), finish))
    return result


def parse_card(card, section):
    titular = TITULAR.search(card)
    if not titular:
        raise ValueError("Ficha de guardia sin titular.")
    cfci = CFCI.search(card)
    if not cfci:
        raise ValueError("Ficha de guardia sin código de farmacia.")
    fields = {}
    for row in ROW.findall(card):
        cells = CELL.findall(row)
        if len(cells) < 2:
            continue
        label = fold(clean_text(cells[0]))
        value = re.sub(r"\(\s*veure mapa\s*\)", "", clean_text(cells[1]),
                       flags=re.IGNORECASE).strip()
        if label:
            fields[label] = value
    address = fields.get("adreca", "")
    town_text = fields.get("poblacio", "")
    if not address or not town_text:
        raise ValueError(f"Ficha {cfci.group(1)} sin dirección o población.")
    cp = re.search(r"\s*\(CP:\s*(\d{5})\)\s*$", town_text)
    town = town_text[:cp.start()].strip() if cp else town_text
    phone = re.sub(r"\D", "", fields.get("telefon", ""))
    if len(phone) == 11 and phone.startswith("34"):
        phone = phone[2:]
    name = clean_text(titular.group(1))
    if not name or not town:
        raise ValueError(f"Ficha {cfci.group(1)} sin nombre o municipio.")
    return {
        "cfci": cfci.group(1), "nombre": name, "direccion": address,
        "localidad": town, "cp": cp.group(1) if cp else "",
        "telefono": phone, "observaciones": fields.get("observacions", ""),
        "secciones": [dict(section)],
    }


def parse_page(page, target, municipality):
    title_date(page, target)
    page_sections = section_blocks(page, target)
    cards = CARD.findall(page)
    if not cards:
        return []
    if not page_sections:
        raise ValueError("Hay fichas de guardia fuera de las secciones oficiales.")
    parsed = []
    total = 0
    for section, start, finish in page_sections:
        for card in CARD.findall(page[start:finish]):
            parsed.append(parse_card(card, section))
            total += 1
    if total != len(cards):
        raise ValueError("El número de fichas no coincide con las secciones oficiales.")
    rows = []
    for row in parsed:
        row["localidades_atendidas"] = [municipality]
        rows.append(row)
    return merge_records(rows)


def same_identity(first, second):
    if fold(first["nombre"]) != fold(second["nombre"]):
        return False
    if fold(first["direccion"]) != fold(second["direccion"]):
        return False
    if fold(first["localidad"]) != fold(second["localidad"]):
        return False
    phones = [phone_digits(first), phone_digits(second)]
    return not all(phones) or phones[0] == phones[1]


def merge_records(records):
    merged = {}
    for record in records:
        current = merged.get(record["cfci"])
        if current is None:
            merged[record["cfci"]] = {
                **record,
                "secciones": list(record.get("secciones", [])),
                "localidades_atendidas": list(record.get("localidades_atendidas", [])),
            }
            continue
        if not same_identity(current, record):
            raise ValueError(f"La ficha {record['cfci']} cambia de identidad entre consultas.")
        known = {(item["inicio"], item["fin"], item["tipo"])
                 for item in current["secciones"]}
        current["secciones"].extend(
            item for item in record.get("secciones", [])
            if (item["inicio"], item["fin"], item["tipo"]) not in known)
        current["localidades_atendidas"] = list(dict.fromkeys(
            current["localidades_atendidas"] + record.get("localidades_atendidas", [])))
    return list(merged.values())


def to_rows(records, target):
    day_start = datetime.combine(target, dt_time.min)
    day_end = day_start + timedelta(days=1)
    rows = []
    for record in records:
        intervals = []
        franjas = []
        for section in sorted(record.get("secciones", []), key=lambda item: item["inicio"]):
            begin = max(section["inicio"], day_start)
            finish = min(section["fin"], day_end)
            if begin >= finish:
                continue
            intervals.append({
                "inicio": begin.isoformat(), "fin": finish.isoformat(),
                "inicio_original": section["inicio"].isoformat(),
                "fin_original": section["fin"].isoformat(),
                "tipo": section["tipo"],
            })
            text = section["texto"]
            if section["inicio"] < day_start:
                text += " (madrugada del día anterior)"
            if text not in franjas:
                franjas.append(text)
        if not intervals:
            continue
        rows.append({
            "id": f"cof-tarragona-{record['cfci']}",
            "nombre": record["nombre"], "direccion": record["direccion"],
            "localidad": record["localidad"], "provincia": "Tarragona",
            "cp": record.get("cp", ""), "telefono": record.get("telefono", ""),
            "observaciones": record.get("observaciones", ""),
            "lat": None, "lng": None, "ubicacion_pendiente_verificacion": True,
            "guardia": True, "fecha_guardia": target.isoformat(),
            "guardia_franja": " · ".join(franjas),
            "guardia_franjas": franjas, "guardia_intervalos": intervals,
            "localidades_atendidas": record.get("localidades_atendidas", []),
            "fuente": SOURCE, "fuentes": [SOURCE, DETAIL + record["cfci"]],
        })
    return rows


def name_keys(row):
    """Apellidos del titular que pueden identificar una ficha OSM cercana."""
    keys = set()
    for part in re.split(r"\s+-\s+|/", row.get("nombre", "")):
        surname = part.split(",")[0]
        key = fold(surname)
        if len(key.replace(" ", "")) < 4:
            continue
        keys.add(key)
        keys.update(word for word in key.split()
                    if len(word) >= 4 and word not in {"farmacia", "farmacies"})
    return keys


def name_matches(row, pharmacy):
    osm_name = f" {fold(pharmacy.get('nombre', ''))} "
    return any(f" {key} " in osm_name for key in name_keys(row))


def locate(row, page, index):
    """Sitúa la ficha solo con el marcador del Colegio y una farmacia OSM inequívoca a 15 m."""
    markers = {(round(float(lat), 7), round(float(lng), 7))
               for lat, lng in MARKER.findall(page)}
    if not markers:
        raise ValueError("La ficha oficial no publica coordenadas.")
    if len(markers) != 1:
        raise ValueError("La ficha oficial contiene coordenadas ambiguas.")
    lat, lng = markers.pop()
    if not (40.4 < lat < 41.9 and 0.1 < lng < 1.8):
        raise ValueError("Coordenada oficial fuera del ámbito de Tarragona.")
    keys = set(identity_keys(row))
    matches = [(distance, pharmacy) for distance, pharmacy in index.near((lat, lng))
               if keys.intersection(identity_keys(pharmacy)) or name_matches(row, pharmacy)]
    if len(matches) != 1:
        return False
    distance, pharmacy = matches[0]
    row.update(
        lat=lat, lng=lng, ubicacion_pendiente_verificacion=False,
        precision_ubicacion="farmacia", fuente_coordenadas=DETAIL + row["id"].removeprefix("cof-tarragona-"),
        ajuste_ubicacion_m=round(distance, 1), osm_id=pharmacy["id"],
        referencia_ubicacion=f"https://www.openstreetmap.org/node/{pharmacy['id'].removeprefix('osm-')}",
        nota_ubicacion=(f"Marcador del Colegio contrastado con una farmacia OSM "
                        f"a {distance:.1f} m."),
    )
    return True


def collect_day(target, municipalities):
    records = []
    reports = []
    for index, municipality in enumerate(municipalities, 1):
        page = request(QUERY, {"CODI": municipality.code, "MES": str(target.month),
                               "DIA": str(target.day), "CONSUL": "OK"})
        rows = parse_page(page, target, municipality.name)
        records.extend(rows)
        estado = "disponible" if rows else "parcial"
        report = {
            "nombre": municipality.name, "estado": estado, "farmacias": len(rows),
            "fecha_consultada": target.isoformat(), "url": query_url(target, municipality),
        }
        if not rows:
            report["error"] = ("La consulta oficial no publica farmacias para esta población; "
                               "no implica que no haya guardia. Confirmar por teléfono.")
        reports.append(report)
        if index == len(municipalities) or index % 25 == 0:
            print(f"Tarragona {target}: {index}/{len(municipalities)} municipios; "
                  f"{len(records)} fichas.", flush=True)
        time.sleep(0.15)
    return merge_records(records), reports


def run(target, output=OUT_DIR):
    municipalities = fetch_municipalities()
    current, reports = collect_day(target, municipalities)
    previous, previous_reports = collect_day(target - timedelta(days=1), municipalities)
    rows = to_rows(merge_records(current + previous), target)
    if not rows:
        raise ValueError("El Colegio no publica ninguna guardia para la fecha solicitada.")
    # El catálogo OSM original: en el nacional fusionado los nodos ya emparejados desaparecen.
    osm = json.loads((output / "farmacias-osm-espana.json").read_text())
    index = PharmacyIndex({**row, "provincia": "Tarragona"} for row in osm
                          if re.fullmatch(r"osm-\d+", str(row.get("id", ""))))
    details = {}
    for row in rows:
        cfci = row["id"].removeprefix("cof-tarragona-")
        try:
            if cfci not in details:
                details[cfci] = request(DETAIL + cfci)
                time.sleep(0.15)
            locate(row, details[cfci], index)
        except (OSError, ValueError, UnicodeError) as error:
            print(f"AVISO Tarragona {row['id']}: no se pudo contrastar la ubicación: {error}",
                  flush=True)
        if not coordinates(row):
            print(f"AVISO Tarragona {row['id']}: ubicación pendiente; no se dibuja marcador.",
                  flush=True)
    output.mkdir(parents=True, exist_ok=True)
    path = output / "farmacias-tarragona.json"
    saved = {row["id"]: row for row in json.loads(path.read_text())} if path.exists() else {}
    for row in rows:
        saved[row["id"]] = {**row, "guardia": False}
    write_json(path, list(saved.values()))
    reports.append({
        "nombre": "Madrugada del día anterior", "estado": "disponible",
        "farmacias": len(previous),
        "fecha_consultada": (target - timedelta(days=1)).isoformat(),
        "nota": "Turno nocturno de la víspera recortado a la madrugada solicitada.",
    })
    if any(report["estado"] != "disponible" for report in previous_reports):
        reports.append({
            "nombre": "Consulta de la víspera", "estado": "parcial",
            "error": "Alguna población de la víspera no publicó fichas; la madrugada puede estar incompleta.",
        })
    return publish(target, "Tarragona", SOURCE, rows, reports, output=output,
                   authoritative_sources={SOURCE})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fecha", type=date.fromisoformat,
                        default=datetime.now(TZ).date())
    parser.add_argument("--salida", type=Path, default=OUT_DIR)
    args = parser.parse_args()
    try:
        return 0 if run(args.fecha, args.salida) else 1
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"ERROR Tarragona: {error}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
