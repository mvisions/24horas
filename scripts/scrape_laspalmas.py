#!/usr/bin/env python3
"""Scraper de farmacias de guardia de Las Palmas (Canarias) via API del COF Las Palmas."""
import argparse
import json
import re
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

from scrape_mes import OUT_DIR, merge_schedule, slugify, write_json

BASE = "https://www.coflaspalmas.es"
AJAX = "https://www.coflaspalmas.es/wp-admin/admin-ajax.php"
FINDER = f"{BASE}/buscador-de-farmacias"
PROVINCIA = "Las Palmas"
# La web pide los calendarios por el slug del nombre de provincia («las-palmas»),
# igual que hace slugify() en el mapa; un nombre corto rompería la carga.
SLUG = slugify(PROVINCIA)
NONCE_PATTERN = re.compile(r'var vbf_vars = \{"nonce":"([a-f0-9]+)"')


class NonceError(RuntimeError):
    """WordPress rechaza el nonce: hay que leerlo otra vez de la página del buscador."""


def finder_nonce():
    """El nonce de WordPress caduca, así que se lee en cada ejecución en lugar de fijarlo."""
    req = urllib.request.Request(FINDER, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=40) as response:
        page = response.read().decode("utf-8", errors="ignore")
    match = NONCE_PATTERN.search(page)
    if not match:
        raise NonceError("No se encontró el nonce de vbf en la página del buscador del COF Las Palmas.")
    return match.group(1)


def request_api(nonce, endpoint, **params):
    body = urllib.parse.urlencode({
        "action": "bearer_authentication_with_params",
        "nonce": nonce,
        "vbf_req_endpoint": endpoint,
        "vbf_req_aplica_filtro_fecha": 0,
        "vbf_req_lang": "es",
        **params,
    }).encode()
    req = urllib.request.Request(AJAX, data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=40) as r:
        data = json.loads(r.read())
    if not data.get("success"):
        message = ((data.get("data") or {}).get("message")) or "respuesta sin éxito"
        raise NonceError(f"{endpoint}: {message}")
    return data["data"]["data"]["informacion"]


def request_municipios(nonce):
    return [m["municipio"] for m in request_api(nonce, "/municipios")]


def request_guardias(nonce, municipio, fecha):
    return request_api(nonce, "/farmacias-abiertas",
                       vbf_req_fecha=fecha.isoformat(),
                       vbf_req_municipio=municipio,
                       vbf_req_es_listado_geo="false",
                       vbf_req_solo_guardias="true",
                       vbf_req_aplica_filtro_fecha=1)


def parse_guardias(municipio, fecha, groups):
    rows = []
    for group in groups or []:
        for item in group.get("contenido", []):
            nombre = item.get("nombre", "")
            direccion = item.get("direccion", "")
            telefono = item.get("telefono")
            municipio_real = item.get("municipio", municipio)
            lat = item.get("coordenadas", [None, None])[0]
            lng = item.get("coordenadas", [None, None])[1]
            for g in item.get("guardias", []):
                inicio = g.get("guardia_horario_fecha_inicio")
                fin = g.get("guardia_horario_fecha_fin")
                franja = f"{g.get('guardia_hora_inicio','')} - {g.get('guardia_hora_fin','')}"
                rows.append({
                    "id": f"cof-laspalmas-{item.get('uuid','')}",
                    "nombre": nombre,
                    "direccion": direccion,
                    "localidad": municipio_real,
                    "provincia": "Las Palmas",
                    "telefono": telefono,
                    "lat": lat,
                    "lng": lng,
                    "guardia": True,
                    "guardia_franja": franja,
                    "guardia_franjas": [franja],
                    "fecha_guardia": fecha.isoformat(),
                    "fuente": BASE,
                    "fuentes": [BASE],
                    "hora_inicio": inicio,
                    "hora_fin": fin,
                })
    return rows


def run(fecha, offline=False):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    nonce = finder_nonce()

    def with_nonce(action):
        """Vuelve a leer el nonce si WordPress lo rechaza a mitad de la consulta."""
        nonlocal nonce
        try:
            return action(nonce)
        except NonceError:
            nonce = finder_nonce()
            return action(nonce)

    municipios = with_nonce(request_municipios)
    print(f"Municipios: {len(municipios)}", flush=True)
    rows = []
    reports = []
    for i, m in enumerate(municipios, 1):
        try:
            groups = with_nonce(lambda token: request_guardias(token, m, fecha))
            found = parse_guardias(m, fecha, groups)
            rows.extend(found)
            reports.append({"nombre": m, "estado": "disponible", "farmacias": len({r["id"] for r in found})})
            print(f"{i}/{len(municipios)} {m}: {len(found)} guardias", flush=True)
        except Exception as e:
            reports.append({"nombre": m, "estado": "error", "error": str(e)})
            print(f"{i}/{len(municipios)} {m}: ERROR {e}", flush=True)
        time.sleep(0.3)
    # publish
    # La fuente repite la misma farmacia en varios bloques: una sola fila con todas sus franjas.
    rows = merge_schedule(rows)
    path = OUT_DIR / f"guardias-{SLUG}-{fecha.isoformat()}.json"
    write_json(path, rows)
    report = {"fecha": fecha.isoformat(), "provincia": PROVINCIA, "fuente": BASE,
              "consultas": reports, "farmacias": len(rows),
              "farmacias_confirmadas": len(rows)}
    write_json(OUT_DIR / f"cobertura-{SLUG}-{fecha.isoformat()}.json", report)
    print(f"Las Palmas: {len(rows)} guardias publicadas.", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fecha", type=date.fromisoformat, default=date.today())
    args = parser.parse_args()
    run(args.fecha)