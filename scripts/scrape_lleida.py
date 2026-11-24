#!/usr/bin/env python3
"""Importa las guardias del Col·legi Oficial de Farmacèutics de Lleida.

El Colegio publica un calendario por zona. Lleida ciudad y las zonas que la
muestran cambian de turno cada día (día 9-22 h y noche 22-9 h). El resto de la
provincia tiene guardias localizadas con turnos semanales que cambian a una
hora indicada en cada zona ("Els torns canvien els dijous a les 20 hores"):
el día del cambio se toma la farmacia saliente del día anterior y la entrante
del día siguiente, partiendo el día a esa hora.
"""
import argparse
import html
import json
import re
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from merge_espana import coordinates, identity_keys
from provincial_catalog import publish
from proximidad import PharmacyIndex, fold
from scrape_mes import OUT_DIR, write_json
from scrape_tarragona import name_matches

SOURCE = "https://www.coflleida.cat/cat/farmacies/farmacies.asp"
TZ = ZoneInfo("Europe/Madrid")
HEADERS = {"User-Agent": "FarmaciasDeGuardiaTracker/1.0"}
MONTHS = ("gener", "febrer", "març", "abril", "maig", "juny", "juliol", "agost",
          "setembre", "octubre", "novembre", "desembre")
WEEKDAYS = {"dilluns": 0, "dimarts": 1, "dimecres": 2, "dijous": 3,
            "divendres": 4, "dissabte": 5, "diumenge": 6}
LOCALIZED = "guardia localizada: llamar antes de acudir"
CLOSED = "tancada definitivament"

ZONE_LINK = re.compile(r'IdZona=(\d+)"[^>]*>\s*<b>([^<]+)</b>', re.IGNORECASE)
LETTER = re.compile(r'Inicial=([A-Z])"')
HEADING = re.compile(
    r'class="(?:TitolSubSeccioVermell|titolSubseccioBlanc)"[^>]*>(.*?)</td>',
    re.IGNORECASE | re.DOTALL)
CHANGE = re.compile(r"Els torns canvien els (\w+) a les (\d{1,2}) hores", re.IGNORECASE)
LINK = re.compile(r"IdFarmacia=(\d+)", re.IGNORECASE)
CITY_ENTRY = re.compile(
    r'IdFarmacia=(\d+)[^>]*>([^<]+)</a>\s*</td>\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>',
    re.IGNORECASE | re.DOTALL)
CITY_BLOCK = re.compile(
    r'IdFarmacia=(\d+)[^>]*>\s*<b>([^<]+)</b>\s*</a>\s*<br\s*/?>(?!\s*<b>)(.*?)</div>',
    re.IGNORECASE | re.DOTALL)
NO_DUTY = "No hi ha definides farmàcies de guàrdia per aquesta població."
RURAL_ENTRY = re.compile(
    r'IdFarmacia=(\d+)[^>]*>\s*<b>([^<]+)</b>\s*</a>\s*<br\s*/?>\s*<b>([^<]+)</b>'
    r'\s*<br\s*/?>(.*?)</div>', re.IGNORECASE | re.DOTALL)
LABEL = re.compile(r"DIA I NIT|(DIA|NIT) de (\d{1,2}) a (\d{1,2}) hores")
PHONE = re.compile(r"Tel\. Farm(?:à|&agrave;)cia:\s*(\d{9})", re.IGNORECASE)
LATLNG = re.compile(r"LatLng\(\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\)")


def clean(value):
    value = html.unescape(re.sub(r"<[^>]+>", " ", value or ""))
    return " ".join(value.replace("\xa0", " ").split())


def request(params=None):
    url = f"{SOURCE}?{urllib.parse.urlencode(params)}" if params else SOURCE
    with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS),
                                timeout=45) as response:
        content = response.read()
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return content.decode("iso-8859-1")


def zone_url(zone, day):
    return f"{SOURCE}?" + urllib.parse.urlencode({
        "Detall": "Calendari", "IdZona": zone, "Any": day.year, "Mes": day.month, "Dia": day.day})


def detail_url(ident):
    return f"{SOURCE}?Detall=Farmacia&IdFarmacia={ident}"


def parse_zones(pages):
    zones = {}
    for page in pages:
        for code, name in ZONE_LINK.findall(page):
            name = clean(name)
            if zones.get(code, name) != name:
                raise ValueError(f"Zona {code} con dos nombres.")
            zones[code] = name
    if len(zones) < 100:
        raise ValueError(f"Listado oficial de zonas incompleto: {len(zones)}.")
    return zones


def fetch_zones():
    first = request()
    pages = [first]
    for letter in sorted(set(LETTER.findall(first))):
        pages.append(request({"Inicial": letter}))
        time.sleep(0.1)
    return parse_zones(pages)


def body_of(page):
    page = re.sub(r"(?is)<script.*?</script>", "", page)
    start = page.find("TOT EL MES")
    end = page.find("Tornar", start + 1) if start >= 0 else -1
    if start < 0 or end < 0 or page.count("TOT EL MES") != 1:
        raise ValueError("La página de la zona no tiene el calendario esperado.")
    return page[start:end]


def split_address(lines):
    if len(lines) < 2:
        raise ValueError("Ficha sin dirección o población.")
    address, town = lines[0], lines[1].split(",")[0].strip()
    cp = re.search(r",?\s*(\d{5})$", address)
    if cp:
        address = address[:cp.start()].strip()
    if not address or not town:
        raise ValueError("Ficha sin dirección o población.")
    return address, town, cp.group(1) if cp else ""


def parse_zone_page(page, day):
    """Fichas de una zona y día; falla si la página no confirma la fecha o el formato."""
    body = body_of(page)
    day_text = f"{day.day} de {MONTHS[day.month - 1]}"
    headings = [(match, clean(match.group(1))) for match in HEADING.finditer(body)]
    entries, closed = [], []
    if any(text.startswith(f"{day_text} - ") for _, text in headings):
        expected = {f"{day_text} - FARMÀCIES DE GUÀRDIA OBERTES de 9 a 22 hores": "DIA de 9 a 22 hores",
                    f"{day_text} - GUÀRDIA DE NIT de 22 a 9 hores": "NIT de 22 a 9 hores"}
        if sorted(text for _, text in headings) != sorted(expected):
            raise ValueError(f"Secciones diarias inesperadas: {[text for _, text in headings]!r}")
        for index, (match, text) in enumerate(headings):
            end = headings[index + 1][0].start() if index + 1 < len(headings) else len(body)
            section = body[match.end():end]
            found = [(item.start(), item.group(1), item.group(2), item.group(3), item.group(4))
                     for item in CITY_ENTRY.finditer(section)]
            found += [(item.start(), item.group(1), item.group(2), item.group(3), item.group(3))
                      for item in CITY_BLOCK.finditer(section)]
            for _, ident, name, place, phone in sorted(found):
                lines = [clean(line) for line in re.split(r"<br\s*/?>", place)
                         if clean(line) and not PHONE.search(line)]
                address, town, cp = split_address(lines)
                found_phone = PHONE.search(phone)
                entries.append({"id": ident, "nombre": clean(name), "turno": expected[text],
                                "direccion": address, "localidad": town, "cp": cp,
                                "telefono": found_phone.group(1) if found_phone else "",
                                "localizada": False})
        kind, change = "diaria", None
    elif not headings and NO_DUTY in clean(body) and not LINK.search(body):
        return {"tipo": "sin_guardias", "cambio": None, "fichas": [], "cerradas": []}
    else:
        if [text for _, text in headings] != [day_text]:
            raise ValueError(f"La página no confirma el día solicitado: {[text for _, text in headings]!r}")
        rule = CHANGE.search(clean(body))
        if not rule or fold(rule.group(1)) not in WEEKDAYS or not 0 <= int(rule.group(2)) <= 23:
            raise ValueError("Zona semanal sin día y hora oficiales de cambio de turno.")
        if "LOCALITZADES" not in clean(body):
            raise ValueError("La zona semanal no indica el tipo de guardia.")
        change = (WEEKDAYS[fold(rule.group(1))], int(rule.group(2)))
        for ident, name, label, rest in RURAL_ENTRY.findall(body):
            lines = [clean(line) for line in re.split(r"<br\s*/?>", rest) if clean(line)]
            if CLOSED in fold(name):
                referral = [line for line in lines if "mes propera" in fold(line)]
                if len(referral) != 1:
                    raise ValueError(f"Farmacia {ident} cerrada sin remisión oficial a otra guardia.")
                closed.append(f"{clean(name)}: {referral[0]}")
                continue
            address, town, cp = split_address(lines)
            found = PHONE.search(rest)
            entries.append({"id": ident, "nombre": clean(name), "turno": clean(label),
                            "direccion": address, "localidad": town, "cp": cp,
                            "telefono": found.group(1) if found else "", "localizada": True})
        kind = "semanal"
    if len(entries) + len(closed) != len(LINK.findall(body)):
        raise ValueError("El número de fichas no coincide con los enlaces de farmacia.")
    for entry in entries:
        if not LABEL.fullmatch(entry["turno"]):
            raise ValueError(f"Turno desconocido en {entry['id']}: {entry['turno']!r}")
    return {"tipo": kind, "cambio": change, "fichas": entries, "cerradas": closed}


def shifts(entry, day):
    """Turnos que la ficha representa cuando se publica para el día dado."""
    start = datetime.combine(day, dt_time.min)
    match = LABEL.fullmatch(entry["turno"])
    if not match.group(1):
        return [(start, start + timedelta(days=1))]
    begin, end = int(match.group(2)), int(match.group(3))
    if match.group(1) == "DIA":
        if not begin < end <= 24:
            raise ValueError(f"Turno de día inválido: {entry['turno']!r}")
        return [(start + timedelta(hours=begin), start + timedelta(hours=end))]
    if not end < begin <= 23:
        raise ValueError(f"Turno de noche inválido: {entry['turno']!r}")
    return [(start + timedelta(hours=begin), start + timedelta(days=1, hours=end))]


def zone_segments(target, zone, fetch):
    """(fichas recortadas al día pedido, avisos oficiales de la zona)."""
    day_start = datetime.combine(target, dt_time.min)
    day_end = day_start + timedelta(days=1)
    today = parse_zone_page(fetch(zone, target), target)
    pieces = []
    if today["tipo"] == "sin_guardias":
        return [], [NO_DUTY]
    if today["tipo"] == "diaria":
        previous = target - timedelta(days=1)
        listings = [(today, target), (parse_zone_page(fetch(zone, previous), previous), previous)]
        notes = []
        for listing, day in listings:
            if listing["tipo"] != "diaria":
                raise ValueError("La zona cambia de formato entre días.")
            for entry in listing["fichas"]:
                for begin, end in shifts(entry, day):
                    pieces.append((entry, begin, end, day, day_start, day_end))
    else:
        weekday, hour = today["cambio"]
        windows = [(today, target, day_start, day_end)]
        if target.weekday() == weekday:
            change = day_start + timedelta(hours=hour)
            before, after = target - timedelta(days=1), target + timedelta(days=1)
            windows = [(parse_zone_page(fetch(zone, before), before), before, day_start, change),
                       (parse_zone_page(fetch(zone, after), after), after, change, day_end)]
        notes = []
        for listing, published, low, high in windows:
            if listing["tipo"] != "semanal" or listing["cambio"] != today["cambio"]:
                raise ValueError("La zona cambia de formato entre días.")
            notes += [note for note in listing["cerradas"] if note not in notes]
            for entry in listing["fichas"]:
                for day in (target - timedelta(days=1), target):
                    for begin, end in shifts(entry, day):
                        pieces.append((entry, begin, end, published, low, high))
    result = []
    for entry, begin, end, published, low, high in pieces:
        clipped = max(begin, low), min(end, high)
        if clipped[0] < clipped[1]:
            result.append((entry, clipped[0], clipped[1], begin, end, published))
    return result, notes


def same_identity(first, second):
    return all(fold(first[key]) == fold(second[key]) for key in ("nombre", "direccion", "localidad"))


def build_rows(target, zones, fetch, reports):
    records = {}
    for zone, name in zones.items():
        try:
            pieces, notes = zone_segments(target, zone, fetch)
        except (OSError, ValueError, UnicodeError) as error:
            reports.append({"nombre": name, "estado": "error", "farmacias": 0,
                            "url": zone_url(zone, target), "error": str(error)})
            print(f"AVISO Lleida zona {name}: {error}", flush=True)
            continue
        found = set()
        for entry, begin, end, original_begin, original_end, published in pieces:
            record = records.setdefault(entry["id"], {**entry, "intervalos": {}, "turnos": [],
                                                      "zonas": []})
            if not same_identity(record, entry):
                raise ValueError(f"La farmacia {entry['id']} cambia de identidad entre zonas.")
            text = entry["turno"] + (f" ({LOCALIZED})" if entry["localizada"] else "")
            if original_begin.date() < target:
                text = f"Madrugada del turno anterior: {text}"
            if text not in record["turnos"]:
                record["turnos"].append(text)
            record["intervalos"][(begin, end)] = {
                "inicio": begin.isoformat(), "fin": end.isoformat(),
                "inicio_original": original_begin.isoformat(), "fin_original": original_end.isoformat(),
                "tipo": entry["turno"], "publicado": published.isoformat()}
            if name not in record["zonas"]:
                record["zonas"].append(name)
            found.add(entry["id"])
        unconfirmed = NO_DUTY in notes
        report = {"nombre": name, "estado": "disponible" if (found or notes) and not unconfirmed else "parcial",
                  "farmacias": len(found), "url": zone_url(zone, target)}
        if notes:
            report["nota"] = " · ".join(notes)
        if unconfirmed or (not found and not notes):
            report["error"] = "La zona no publica farmacias para esta fecha; confirmar por teléfono."
        reports.append(report)
    rows = []
    for ident, record in sorted(records.items(), key=lambda item: int(item[0])):
        intervals = [record["intervalos"][key] for key in sorted(record["intervalos"])]
        rows.append({
            "id": f"cof-lleida-{ident}", "nombre": record["nombre"],
            "direccion": record["direccion"], "localidad": record["localidad"],
            "provincia": "Lleida", "cp": record["cp"], "telefono": record["telefono"],
            "lat": None, "lng": None, "ubicacion_pendiente_verificacion": True,
            "guardia": True, "fecha_guardia": target.isoformat(),
            "guardia_franja": " · ".join(record["turnos"]), "guardia_franjas": record["turnos"],
            "guardia_intervalos": intervals, "localidades_atendidas": record["zonas"],
            "fuente": SOURCE, "fuentes": [SOURCE, detail_url(ident)],
        })
    return rows


def locate(row, page, index):
    """Coordenada del Colegio solo si coincide a 15 m con una única farmacia OSM identificada."""
    points = {(round(float(lat), 7), round(float(lng), 7)) for lat, lng in LATLNG.findall(page)}
    if len(points) != 1:
        raise ValueError("La ficha oficial no tiene una coordenada única.")
    lat, lng = points.pop()
    if not (40.4 < lat < 42.95 and 0.1 < lng < 2.0):
        raise ValueError("Coordenada oficial fuera del ámbito de Lleida.")
    keys = set(identity_keys(row))
    matches = [(distance, pharmacy) for distance, pharmacy in index.near((lat, lng))
               if keys.intersection(identity_keys(pharmacy)) or name_matches(row, pharmacy)]
    if len(matches) != 1:
        return False
    distance, pharmacy = matches[0]
    row.update(lat=lat, lng=lng, ubicacion_pendiente_verificacion=False,
               precision_ubicacion="farmacia", fuente_coordenadas=detail_url(row["id"].removeprefix("cof-lleida-")),
               ajuste_ubicacion_m=round(distance, 1), osm_id=pharmacy["id"],
               referencia_ubicacion=f"https://www.openstreetmap.org/node/{pharmacy['id'].removeprefix('osm-')}",
               nota_ubicacion=f"Marcador del Colegio contrastado con una farmacia OSM a {distance:.1f} m.")
    return True


def run(target, output=OUT_DIR):
    zones = fetch_zones()
    cache = {}

    def fetch(zone, day):
        if (zone, day) not in cache:
            cache[zone, day] = request({"Detall": "Calendari", "IdZona": zone,
                                        "Any": day.year, "Mes": day.month, "Dia": day.day})
            time.sleep(0.1)
        return cache[zone, day]

    reports = []
    rows = build_rows(target, zones, fetch, reports)
    if not rows:
        raise ValueError("El Colegio no publica ninguna guardia para la fecha solicitada.")
    print(f"Lleida {target}: {len(zones)} zonas; {len(rows)} farmacias.", flush=True)
    # El catálogo OSM original: en el nacional fusionado los nodos ya emparejados desaparecen.
    osm = json.loads((output / "farmacias-osm-espana.json").read_text())
    index = PharmacyIndex({**row, "provincia": "Lleida"} for row in osm
                          if re.fullmatch(r"osm-\d+", str(row.get("id", ""))))
    for row in rows:
        try:
            locate(row, request({"Detall": "Farmacia", "IdFarmacia": row["id"].removeprefix("cof-lleida-")}), index)
            time.sleep(0.1)
        except (OSError, ValueError, UnicodeError) as error:
            print(f"AVISO Lleida {row['id']}: no se pudo contrastar la ubicación: {error}", flush=True)
    output.mkdir(parents=True, exist_ok=True)
    path = output / "farmacias-lleida.json"
    saved = {row["id"]: row for row in json.loads(path.read_text())} if path.exists() else {}
    for row in rows:
        saved[row["id"]] = {**row, "guardia": False}
    write_json(path, list(saved.values()))
    return publish(target, "Lleida", SOURCE, rows, reports, output=output,
                   authoritative_sources={SOURCE})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fecha", type=date.fromisoformat, default=datetime.now(TZ).date())
    parser.add_argument("--salida", type=Path, default=OUT_DIR)
    args = parser.parse_args()
    try:
        return 0 if run(args.fecha, args.salida) else 1
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"ERROR Lleida: {error}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
