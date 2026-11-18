#!/usr/bin/env python3
"""Sonde extérieure de la page d'état de Yanedo : les mesures des barres de disponibilité sur 90 jours.

Elle tourne chez l'hébergeur de la page (chaîne « probe » de GitHub Actions), hors du cluster et hors du réseau de
Yanedo : une sonde dans le cluster serait muette justement quand le cluster est en panne (note T32 v2.0, § 7).
Bibliothèque standard seulement : « python3 -I probe.py ».

Commandes :
  measure  sonde chaque service une fois (un échec est refait 20 s plus tard) et écrit UNE ligne de mesure
  plan     nombre de lots de ce passage : les créneaux utiles d'ici la fin de l'horizon, BATCH_SLOTS par lot
  loop     un lot : mesure les créneaux de 5 minutes l'un après l'autre (au plus BATCH_SLOTS), sans jamais
           remesurer un créneau déjà mesuré ; s'arrête sur une panne totale (un autre runner la confirme)
  record   range les lignes dans le fichier du jour, sur la branche probe-data du dépôt public de la page ; un
           créneau qui a déjà sa ligne n'en reçoit pas une deuxième

Pourquoi des lots (16/11/2026) : le planificateur de GitHub n'a lancé que 5 des ~132 passages prévus ce jour-là ;
un passage par créneau donnait des jours gris. Chaque passage lancé mesure donc plusieurs créneaux, par lots courts
rangés l'un après l'autre (docs/SONDE.md, « Usage de GitHub Actions »).

Les cibles (adresses, ports) ne sont JAMAIS dans le dépôt public, dans un journal ni dans une mesure : elles viennent
d'un secret de l'hébergeur (PROBE_TARGETS, docs/SONDE.md). Une mesure ne contient que la clé du service, « up » ou
« down », et une raison générique (http-503, timeout…).

Codes de sortie : 0 ok ; 2 cibles absentes ou invalides ; 3 dépôt de l'hébergeur injoignable ou jeton refusé ;
1 autre erreur.
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import datetime as dt
import http.client
import json
import os
import random
import re
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
PROBE_SCHEMA = "yanedo-status-probe/1"
TARGETS_SCHEMA = "yanedo-status-targets/1"
# Secondes sans réponse, par opération réseau : au-delà, le service « ne répond pas » (down). Une réponse lente
# reste « disponible » : contrat d'abonnement, « un ralentissement de performance n'est pas assimilé à une absence »
# de Disponibilité (décision D5 de Yannick, 13/11/2026).
TIMEOUT = 30.0
WITNESS_TIMEOUT = 10.0  # les témoins ne mesurent que le réseau de la sonde : un délai court suffit
RETRY_DELAY = 20.0
MAX_BODY = 65536
USER_AGENT = "yanedo-status-probe/1 (+https://yanedo-status.github.io/)"
# Témoin positif : si aucun de ces sites ne répond, c'est le réseau de la sonde qui est en panne, pas Yanedo ; la
# mesure est alors « sans témoin » et ne compte pas. Un site suisse (le chemin vers la Suisse) et un site mondial.
WITNESSES = ("https://www.switch.ch/", "https://www.cloudflare.com/cdn-cgi/trace")
CHECK_TYPES = {
    "https": {"required": ("url",), "optional": ("expect", "json", "contains")},
    "tls": {"required": ("host", "port"), "optional": ("banner",)},
    "tcp": {"required": ("host", "port"), "optional": ("banner",)},
    "dns": {"required": ("server", "zone"), "optional": ("port",)},
    "any": {"required": ("checks",), "optional": ()},
}
HOST_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
SERVICE_KEY_LINE = re.compile(r"^\s*- key:\s*([a-z][a-z0-9-]*)\s*$", re.M)

# Créneaux : les mêmes que le calcul (statusgen.py, Availability) : 5 minutes, alignés sur :00, :05… Zurich est décalé
# d'heures entières, donc un créneau UTC est aussi un créneau de Zurich.
SLOT = dt.timedelta(minutes=5)
ZURICH = ZoneInfo("Europe/Zurich")
CONTRACT_START, CONTRACT_END = dt.time(9, 0), dt.time(17, 0)  # du lundi au vendredi, à Zurich (contrat)
UTC_WINDOW = (dt.time(6, 0), dt.time(17, 0))  # fenêtre de la chaîne (06:00-16:59 UTC) : la sonde n'en sort jamais
BATCH_SLOTS = 4  # créneaux par lot : au plus 20 minutes de mesures perdues si un lot est interrompu
# Horizon d'un passage, en créneaux : 96 = jusqu'à la fin des heures du contrat (17:00 à Zurich). Le premier passage
# lancé du jour mesure donc jusqu'au soir ; les suivants ne servent que de relève (docs/SONDE.md).
RUN_SLOTS = 96
MEASURE_OFFSET = dt.timedelta(minutes=2)  # un créneau suivant se mesure à sa 2e minute, comme l'ancien cron (2, 7, 12…)
MAX_WAIT = dt.timedelta(minutes=5)  # avant le premier créneau utile : attendre au plus le créneau suivant


class TargetsError(Exception):
    """Cibles absentes ou invalides (code 2). Les messages ne citent jamais une valeur, seulement son chemin."""


class HostError(Exception):
    """Dépôt de l'hébergeur injoignable ou jeton refusé (code 3)."""


# --------------------------------------------------------------------------------------------- cibles

def public_keys(root: Path = ROOT) -> set[str]:
    """Les services affichés (content/services.yaml), lus sans PyYAML."""
    return set(SERVICE_KEY_LINE.findall((root / "content" / "services.yaml").read_text(encoding="utf-8")))


def validate_check(check, path: str, allow_http: bool) -> list[str]:
    if not isinstance(check, dict) or check.get("type") not in CHECK_TYPES:
        return [f"{path} : type attendu parmi {sorted(CHECK_TYPES)}"]
    spec = CHECK_TYPES[check["type"]]
    errors = [f"{path} : champ inconnu {k}" for k in check if k not in ("type", *spec["required"], *spec["optional"])]
    errors += [f"{path} : champ obligatoire manquant {k}" for k in spec["required"] if k not in check]
    if errors:
        return errors
    kind = check["type"]
    if kind == "https":
        url = check["url"]
        parts = urlsplit(url) if isinstance(url, str) else None
        schemes = ("https", "http") if allow_http else ("https",)
        if not parts or parts.scheme not in schemes or not parts.hostname or parts.username or parts.password:
            errors.append(f"{path}.url : adresse https:// sans identifiant attendue")
        expect = check.get("expect", [200])
        if not isinstance(expect, list) or not expect or not all(isinstance(c, int) and 100 <= c < 500 for c in expect):
            errors.append(f"{path}.expect : liste de codes HTTP de 100 à 499 attendue (un 5xx n'est jamais « disponible »)")
        if not isinstance(check.get("json", False), bool):
            errors.append(f"{path}.json : vrai ou faux attendu")
        if "contains" in check and (not isinstance(check["contains"], str) or not check["contains"]):
            errors.append(f"{path}.contains : texte non vide attendu")
    elif kind in ("tls", "tcp"):
        if not isinstance(check["host"], str) or not HOST_RE.match(check["host"]):
            errors.append(f"{path}.host : nom d'hôte attendu")
        if not isinstance(check["port"], int) or not 0 < check["port"] < 65536:
            errors.append(f"{path}.port : port attendu")
        if "banner" in check and (not isinstance(check["banner"], str) or not check["banner"]):
            errors.append(f"{path}.banner : texte non vide attendu")
    elif kind == "dns":
        for k in ("server", "zone"):
            if not isinstance(check[k], str) or not HOST_RE.match(check[k]):
                errors.append(f"{path}.{k} : nom attendu")
        if "port" in check and (not isinstance(check["port"], int) or not 0 < check["port"] < 65536):
            errors.append(f"{path}.port : port attendu")
    else:
        subs = check["checks"]
        if not isinstance(subs, list) or not subs:
            errors.append(f"{path}.checks : liste non vide attendue")
        else:
            for i, sub in enumerate(subs):
                if isinstance(sub, dict) and sub.get("type") == "any":
                    errors.append(f"{path}.checks[{i}] : « any » ne s'imbrique pas")
                else:
                    errors += validate_check(sub, f"{path}.checks[{i}]", allow_http)
    return errors


def validate_targets(targets, keys: set[str], allow_http: bool = False) -> list[str]:
    if not isinstance(targets, dict) or targets.get("schema") != TARGETS_SCHEMA:
        return [f"schema doit valoir {TARGETS_SCHEMA!r}"]
    errors = [f"champ inconnu {k}" for k in targets if k not in ("schema", "common", "services")]
    common = targets.get("common", [])
    if not isinstance(common, list):
        errors.append("common : liste attendue")
    else:
        for i, check in enumerate(common):
            errors += validate_check(check, f"common[{i}]", allow_http)
    services = targets.get("services")
    if not isinstance(services, dict) or not services:
        return errors + ["services : objet non vide attendu (clé du service → liste de vérifications)"]
    for key, checks in services.items():
        if key not in keys:
            errors.append(f"services : clé inconnue de content/services.yaml : {key}")
            continue
        if not isinstance(checks, list) or not checks:
            errors.append(f"services.{key} : liste non vide attendue")
            continue
        for i, check in enumerate(checks):
            errors += validate_check(check, f"services.{key}[{i}]", allow_http)
    return errors


def secrets_to_mask(targets) -> list[str]:
    """Adresses et noms d'hôte à masquer dans les journaux publics de l'hébergeur (en plus du secret lui-même)."""
    found: list[str] = []

    def walk(check):
        if check.get("type") == "any":
            for sub in check["checks"]:
                walk(sub)
        elif check.get("type") == "https":
            found.extend([check["url"], urlsplit(check["url"]).hostname or ""])
        elif check.get("type") == "dns":
            found.extend([check["server"], check["zone"]])
        else:
            found.append(check["host"])

    for check in targets.get("common", []):
        walk(check)
    for checks in targets["services"].values():
        for check in checks:
            walk(check)
    return sorted({v for v in found if v}, key=len, reverse=True)


# --------------------------------------------------------------------------------------------- vérifications

def _network_reason(exc: BaseException) -> str:
    """Raison générique d'un échec réseau : jamais le message de l'exception (il peut citer l'adresse)."""
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "timeout"
    if isinstance(exc, socket.gaierror):
        return "dns-resolve"
    if isinstance(exc, ssl.SSLCertVerificationError):
        return "tls-certificate"
    if isinstance(exc, ssl.SSLError):
        return "tls"
    if isinstance(exc, ConnectionRefusedError):
        return "refused"
    if isinstance(exc, http.client.HTTPException):
        return "protocol"
    return "connect"


def check_https(check: dict, context: ssl.SSLContext | None = None, timeout: float | None = None) -> str | None:
    """None si disponible, sinon la raison. Pas de redirection suivie : une redirection inattendue est un échec.
    Aucun seuil de lenteur : une réponse arrivée, même lente, est « disponible » (D5)."""
    parts = urlsplit(check["url"])
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    timeout = TIMEOUT if timeout is None else timeout
    if parts.scheme == "https":
        conn = http.client.HTTPSConnection(parts.hostname, parts.port, timeout=timeout,
                                           context=context or ssl.create_default_context())
    else:
        conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=timeout)
    try:
        conn.request("GET", path, headers={"User-Agent": USER_AGENT, "Accept": "application/json, */*;q=0.5",
                                           "Cache-Control": "no-cache"})
        resp = conn.getresponse()
        body = resp.read(MAX_BODY)
        ctype = (resp.getheader("Content-Type") or "").lower()
    except Exception as exc:  # noqa: BLE001 — toute panne réseau devient une raison générique
        return _network_reason(exc)
    finally:
        conn.close()
    if resp.status not in check.get("expect", [200]):
        return f"http-{resp.status}"
    # Une route inconnue peut tomber sur la page HTML de l'application, avec un 200 : sans ce contrôle, un module
    # arrêté paraîtrait disponible.
    if check.get("json") and ("json" not in ctype or body.lstrip()[:1] not in (b"{", b"[")):
        return "not-json"
    if "contains" in check and check["contains"] not in body.decode("utf-8", "replace"):
        return "content"
    return None


def check_socket(check: dict, use_tls: bool, context: ssl.SSLContext | None = None) -> str | None:
    host, port = check["host"], check["port"]
    try:
        with socket.create_connection((host, port), timeout=TIMEOUT) as raw:
            sock = (context or ssl.create_default_context()).wrap_socket(raw, server_hostname=host) if use_tls else raw
            try:
                if "banner" in check:
                    data = b""
                    while b"\n" not in data and len(data) < 512:
                        chunk = sock.recv(512)
                        if not chunk:
                            break
                        data += chunk
                    if not data.decode("utf-8", "replace").startswith(check["banner"]):
                        return "banner"
            finally:
                if use_tls:
                    sock.close()
    except Exception as exc:  # noqa: BLE001
        return _network_reason(exc)
    return None


def dns_query(zone: str, qid: int) -> bytes:
    labels = zone.rstrip(".").split(".")
    qname = b"".join(bytes([len(label)]) + label.encode("ascii") for label in labels) + b"\x00"
    return struct.pack("!HHHHHH", qid, 0, 1, 0, 0, 0) + qname + struct.pack("!HH", 6, 1)  # SOA, IN ; sans récursion


def check_dns(check: dict) -> str | None:
    """Le serveur faisant autorité répond-il, avec autorité, au SOA de la zone ?"""
    try:
        addr = socket.getaddrinfo(check["server"], check.get("port", 53), socket.AF_INET, socket.SOCK_DGRAM)[0][4]
    except Exception:  # noqa: BLE001
        return "dns-resolve"
    reason = "timeout"
    for _ in range(2):
        qid = random.randrange(1, 65536)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(TIMEOUT / 2)
            try:
                sock.sendto(dns_query(check["zone"], qid), addr)
                data = sock.recv(4096)
            except Exception as exc:  # noqa: BLE001
                reason = _network_reason(exc)
                continue
        if len(data) < 12:
            return "dns-protocol"
        rid, flags, _qd, ancount, _ns, _ar = struct.unpack("!HHHHHH", data[:12])
        if rid != qid or not flags & 0x8000:
            return "dns-protocol"
        if flags & 0x000F:
            return f"dns-rcode-{flags & 0x000F}"
        if not flags & 0x0400:
            return "dns-not-authoritative"
        return None if ancount else "dns-empty"
    return reason


def run_check(check: dict, context: ssl.SSLContext | None = None) -> str | None:
    kind = check["type"]
    if kind == "https":
        return check_https(check, context)
    if kind in ("tls", "tcp"):
        return check_socket(check, kind == "tls", context)
    if kind == "dns":
        return check_dns(check)
    reason = None
    for sub in check["checks"]:  # « any » : disponible si l'une répond
        reason = run_check(sub, context)
        if reason is None:
            return None
    return reason


def witness_ok(witnesses=WITNESSES, context: ssl.SSLContext | None = None) -> bool:
    """Le réseau de la sonde fonctionne si au moins un témoin répond (n'importe quel code HTTP sous 500)."""
    probe = {"expect": list(range(100, 500))}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(witnesses)) as pool:
        return any(r is None for r in pool.map(lambda u: check_https({**probe, "url": u}, context, WITNESS_TIMEOUT),
                                               witnesses))


def measure_round(targets: dict, keys: list[str], context: ssl.SSLContext | None = None) -> dict[str, str | None]:
    """Une passe : chaque service vaut None (disponible) ou la raison de son premier échec. Les vérifications
    communes (le shell, la connexion…) valent pour tous : sans elles, aucun module n'est utilisable."""
    jobs = [("common", i, c) for i, c in enumerate(targets.get("common", []))]
    jobs += [(k, i, c) for k in keys for i, c in enumerate(targets["services"][k])]
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        reasons = list(pool.map(lambda job: run_check(job[2], context), jobs))
    common = next((f"common-{r}" for (owner, _, _), r in zip(jobs, reasons) if owner == "common" and r), None)
    out: dict[str, str | None] = {}
    for key in keys:
        own = next((r for (owner, _, _), r in zip(jobs, reasons) if owner == key and r), None)
        out[key] = common or own
    return out


def measure(targets: dict, now: dt.datetime | None = None, witnesses=WITNESSES, retry_delay: float = RETRY_DELAY,
            context: ssl.SSLContext | None = None) -> dict:
    """Une ligne de mesure. Un service n'est « down » que si deux passes, à retry_delay d'intervalle, échouent."""
    at = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc).replace(microsecond=0)
    line = {"schema": PROBE_SCHEMA, "at": at.strftime("%Y-%m-%dT%H:%M:%SZ"), "witness": False, "results": {},
            "reasons": {}}
    if not witness_ok(witnesses, context):
        return line
    keys = sorted(targets["services"])
    first = measure_round(targets, keys, context)
    failed = [k for k in keys if first[k]]
    second = {}
    if failed:
        time.sleep(retry_delay)
        second = measure_round(targets, failed, context)
    line["witness"] = True
    for key in keys:
        reason = second.get(key) if key in second else first[key]
        line["results"][key] = "down" if reason else "up"
        if reason:
            line["reasons"][key] = reason
    return line


def is_all_down(line: dict) -> bool:
    """Tout en panne d'un coup, témoin compris : le cas qu'un autre runner doit confirmer (liste de blocage)."""
    return bool(line.get("results")) and all(v == "down" for v in line["results"].values())


# --------------------------------------------------------------------------------------------- créneaux et lots

def floor_slot(d: dt.datetime) -> dt.datetime:
    """Début du créneau de 5 minutes (UTC) : la même règle que statusgen.floor_slot."""
    d = d.astimezone(dt.timezone.utc)
    return d.replace(minute=d.minute - d.minute % 5, second=0, microsecond=0)


def in_utc_window(slot: dt.datetime) -> bool:
    """Fenêtre de la chaîne : du lundi au vendredi, de 06:00 à 16:59 UTC. Rien n'est mesuré en dehors."""
    u = slot.astimezone(dt.timezone.utc)
    return u.weekday() < 5 and UTC_WINDOW[0] <= u.time() < UTC_WINDOW[1]


def useful(slot: dt.datetime) -> bool:
    """Créneau qui compte pour les barres : heures du contrat à Zurich (09:00-17:00, du lundi au vendredi), dans la
    fenêtre de la chaîne. Les jours fériés ne sont pas connus ici (content/calendar.yaml demande PyYAML) : ils sont
    mesurés, et le calcul les écarte."""
    z = slot.astimezone(ZURICH)
    return in_utc_window(slot) and z.weekday() < 5 and CONTRACT_START <= z.time() < CONTRACT_END


def slot_of_line(line) -> dt.datetime | None:
    """Créneau d'une ligne de mesure (son « at »), ou None si la ligne est abîmée."""
    try:
        at = dt.datetime.fromisoformat(str(line["at"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return None
    return floor_slot(at) if at.tzinfo else None


def taken_slots(lines: Iterable) -> set[dt.datetime]:
    """Créneaux qui ont déjà une ligne, avec ou sans témoin : jamais deux lignes pour un même créneau. Une ligne
    abîmée n'occupe aucun créneau."""
    out = set()
    for line in lines:
        if isinstance(line, str):
            try:
                line = json.loads(line)
            except ValueError:
                continue
        slot = slot_of_line(line) if isinstance(line, dict) else None
        if slot is not None:
            out.add(slot)
    return out


def next_slot(now: dt.datetime, taken: set[dt.datetime], started: bool,
              max_wait: dt.timedelta = MAX_WAIT) -> dt.datetime | None:
    """Prochain créneau à mesurer : celui en cours s'il est libre, sinon le suivant libre. Avant la première mesure
    du lot, on attend au plus max_wait un créneau utile ; ensuite, le premier créneau inutile (17:00 à Zurich, fin de
    la fenêtre, week-end) termine le lot. None : rien à mesurer."""
    slot, limit = floor_slot(now), now + max_wait
    while True:
        if slot in taken:
            slot += SLOT
        elif useful(slot):
            return slot
        elif started or slot > limit:
            return None
        else:
            slot += SLOT


def plan_batches(now: dt.datetime, horizon_slots: int = RUN_SLOTS, quota: int = BATCH_SLOTS,
                 max_wait: dt.timedelta = MAX_WAIT) -> int:
    """Lots de ce passage : les créneaux utiles consécutifs d'ici l'horizon, `quota` par lot. Zéro hors des heures
    du contrat : le passage ne lance alors aucun runner de mesure."""
    slot = next_slot(now, set(), started=False, max_wait=max_wait)
    end, count = floor_slot(now) + horizon_slots * SLOT, 0
    while slot is not None and slot < end and useful(slot):
        count += 1
        slot += SLOT
    return -(-count // quota)


class Clock:
    """Horloge réelle. Les tests passent une horloge factice : aucun vrai sommeil, le temps avance à la demande."""

    def now(self) -> dt.datetime:
        return dt.datetime.now(dt.timezone.utc)

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


def run_batch(measure_one: Callable[[dt.datetime], dict], clock, taken: set[dt.datetime], quota: int = BATCH_SLOTS,
              confirm: bool = False, emit: Callable[[dict, str], None] | None = None,
              max_wait: dt.timedelta = MAX_WAIT) -> tuple[list[dict], dict | None]:
    """Un lot : au plus `quota` créneaux, l'un après l'autre, chacun mesuré une seule fois.

    - Le créneau en cours se mesure tout de suite s'il est libre ; un créneau suivant, à sa 2e minute.
    - Un créneau déjà mesuré (branche probe-data, lot précédent) n'est jamais remesuré.
    - Aucune mesure ne commence après la fin prévue du lot : le job finit avant son délai, et le lot est rangé.
    - Tout en panne d'un coup : le lot s'arrête, et la ligne attend la confirmation d'un autre runner (job confirm).
      Le job confirm (confirm=True) mesure tout de suite : cette ligne remplace celle en attente et est toujours
      rangée. Si elle confirme la panne, il finit le lot, et la panne qui dure reste rangée ; sinon, une nouvelle
      panne totale vue de lui seul n'est pas rangée : le lot suivant, sur un autre runner, reprend.
    Renvoie (lignes à ranger, ligne en attente de confirmation ou None). emit(ligne, note) est appelé après chaque
    mesure, avant toute attente."""
    deadline = clock.now() + quota * SLOT + SLOT
    taken = set(taken)
    lines: list[dict] = []
    pending, outage = None, False
    while len(lines) < quota:
        now = clock.now()
        if confirm and not lines:  # la confirmation : tout de suite, dans la fenêtre de la chaîne
            slot = floor_slot(now)
            while slot in taken:
                slot += SLOT
            if not in_utc_window(slot):
                break
            wake = max(now, slot)
        else:
            slot = next_slot(now, taken, started=bool(lines), max_wait=max_wait)
            if slot is None:
                break
            wake = now if slot == floor_slot(now) else slot + MEASURE_OFFSET
        if wake >= deadline:
            break
        clock.sleep((wake - clock.now()).total_seconds())
        at = clock.now()
        if floor_slot(at) != slot:  # réveil tardif : le créneau prévu est passé, on recalcule
            continue
        line = measure_one(at)
        taken.add(slot_of_line(line) or slot)
        down = is_all_down(line)
        if confirm and not lines:
            lines.append(line)
            outage = down
            note = "confirmation : panne totale confirmée" if down else "confirmation : pas de panne totale"
        elif down and not outage:
            pending = line
            note = ("nouvelle panne totale, vue de ce seul runner : non rangée, le lot suivant (autre runner) reprend"
                    if confirm else "panne totale : confirmation demandée à un autre runner")
        else:
            lines.append(line)
            outage = outage and down
            note = "rangée avec le lot"
        if emit:
            emit(line, note)
        if pending is not None:
            break
    return lines, pending


# --------------------------------------------------------------------------------------------- rangement (git)

def _git(cwd: Path, *args: str, env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise HostError(f"git {args[0]} a échoué : {result.stderr.strip()[-300:]}")
    return result


def _auth_env(token: str | None) -> dict:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        # L'en-tête passe par l'environnement : le jeton n'apparaît ni dans l'URL ni dans la ligne de commande.
        env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.extraHeader",
                   GIT_CONFIG_VALUE_0=f"Authorization: Basic {basic}")
    return env


BRANCH_README = """# Mesures de la sonde extérieure

Branche écrite par la chaîne `probe` : au plus une ligne JSON par créneau de 5 minutes, du lundi au vendredi, pendant
les heures du contrat, dans `probe/AAAA-MM-JJ.jsonl` (date UTC). Format
`yanedo-status-probe/1` : `at` (UTC), `witness` (le réseau de la sonde fonctionnait), `results` (clé du service →
`up` ou `down`), `reasons` (raison générique d'un `down`). La chaîne `pages` les lit pour les barres de
disponibilité. Ne pas modifier à la main.
"""


def dump_line(line: dict) -> str:
    return json.dumps(line, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def record(lines: dict | list[dict], remote: str, branch: str, token: str | None,
           author: str = "Yanedo status probe", email: str = "status@noreply.invalid", attempts: int = 4) -> str:
    """Ajoute les lignes (une, ou un lot) au fichier de leur jour, sur leur branche, en un seul commit par jour.

    - Jamais deux lignes pour un même créneau : une ligne dont le créneau a déjà sa ligne sur la branche (ou plus tôt
      dans le lot) est écartée. C'est la garde finale, quoi qu'aient fait la mesure et les autres passages.
    - Si une autre passe a poussé entre-temps, tout est refait depuis la branche à jour, garde comprise."""
    lines = [lines] if isinstance(lines, dict) else list(lines)
    for line in lines:
        if (not isinstance(line, dict) or line.get("schema") != PROBE_SCHEMA or slot_of_line(line) is None
                or not re.match(r"^\d{4}-\d{2}-\d{2}T", str(line.get("at", "")))):
            raise TargetsError(["ligne de mesure invalide"])
    days: dict[str, list[dict]] = {}
    for line in lines:
        days.setdefault(line["at"][:10], []).append(line)
    return " ; ".join(_record_day(day, batch, remote, branch, token, author, email, attempts)
                      for day, batch in sorted(days.items()))


def _record_day(day: str, lines: list[dict], remote: str, branch: str, token: str | None, author: str, email: str,
                attempts: int) -> str:
    env = _auth_env(token)
    commit_env = dict(env, GIT_AUTHOR_NAME=author, GIT_AUTHOR_EMAIL=email, GIT_COMMITTER_NAME=author,
                      GIT_COMMITTER_EMAIL=email)
    name = f"probe/{day}.jsonl"
    for attempt in range(attempts):
        with tempfile.TemporaryDirectory(prefix="status-probe-") as tmp:
            clone = Path(tmp)
            _git(clone, "init", "-q", "-b", branch, env=env)
            _git(clone, "remote", "add", "origin", remote, env=env)
            exists = _git(clone, "ls-remote", "--exit-code", "--heads", "origin", branch, env=env, check=False)
            if exists.returncode == 0:
                _git(clone, "fetch", "-q", "--depth", "1", "origin", branch, env=env)
                _git(clone, "checkout", "-q", "-B", branch, "FETCH_HEAD", env=env)
            elif exists.returncode != 2:
                raise HostError(f"dépôt de l'hébergeur injoignable ou jeton refusé : {exists.stderr.strip()[-300:]}")
            else:
                (clone / "README.md").write_text(BRANCH_README, encoding="utf-8")
            path = clone / name
            path.parent.mkdir(exist_ok=True)
            current = path.read_text(encoding="utf-8") if path.exists() else ""
            if current and not current.endswith("\n"):
                current += "\n"
            taken = taken_slots(current.splitlines())
            added: list[dict] = []
            for line in lines:
                slot = slot_of_line(line)
                if slot not in taken:
                    taken.add(slot)
                    added.append(line)
            skipped = len(lines) - len(added)
            if not added:
                return f"déjà rangée : {name} ({skipped} créneau(x) déjà mesuré(s))"
            path.write_text(current + "".join(dump_line(l) + "\n" for l in added), encoding="utf-8")
            _git(clone, "add", "-A", env=env)
            message = f"probe {added[0]['at']}" + (f" (+{len(added) - 1})" if len(added) > 1 else "")
            _git(clone, "-c", "commit.gpgsign=false", "commit", "-q", "-m", message, env=commit_env)
            pushed = _git(clone, "push", "-q", "origin", f"HEAD:refs/heads/{branch}", env=env, check=False)
            if pushed.returncode == 0:
                done = f"rangée : {name}" if len(added) == 1 else f"rangées : {len(added)} lignes dans {name}"
                return done + (f" ({skipped} créneau(x) déjà mesuré(s), écarté(s))" if skipped else "")
        time.sleep(2 + 3 * attempt + random.random())
    raise HostError(f"poussée refusée {attempts} fois de suite sur {branch}")


# --------------------------------------------------------------------------------------------- ligne de commande

def _github_output(values: dict[str, str]) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            for key, value in values.items():
                fh.write(f"{key}={value}\n")


def load_targets(env_name: str | None, file_name: str | None) -> dict:
    """Les cibles, validées ; dans GitHub Actions, chaque adresse est masquée dans le journal public avant tout."""
    raw = os.environ.get(env_name, "") if env_name else Path(file_name).read_text("utf-8")
    if not raw.strip():
        raise TargetsError([f"cibles absentes : {env_name or file_name} est vide (secret PROBE_TARGETS non posé ?)"])
    try:
        targets = json.loads(raw)
    except ValueError:
        raise TargetsError(["cibles illisibles : JSON invalide"]) from None
    errors = validate_targets(targets, public_keys())
    if errors:
        raise TargetsError(errors)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        for value in secrets_to_mask(targets):
            print(f"::add-mask::{value}")
    return targets


def print_line(line: dict, prefix: str = "") -> None:
    """Journal public : l'heure, puis la clé de chaque service et son état ; jamais une adresse."""
    if prefix:
        print(prefix)
    if not line["witness"]:
        print("témoin muet : le réseau de la sonde ne répond pas ; mesure sans valeur")
    for key, value in line["results"].items():
        print(f"{key} : {value}" + (f" ({line['reasons'][key]})" if key in line["reasons"] else ""))


def lines_from_env(name: str) -> list[dict]:
    """Lignes de mesure d'une variable : un tableau JSON, une ligne JSON, ou des lignes JSON l'une sous l'autre."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return []
    try:
        values = json.loads(raw) if raw.startswith("[") else [json.loads(l) for l in raw.splitlines() if l.strip()]
    except ValueError:
        raise TargetsError([f"lignes de mesure illisibles dans {name}"]) from None
    if not all(isinstance(v, dict) for v in values):
        raise TargetsError([f"lignes de mesure illisibles dans {name}"])
    return values


def _dump_lines(lines: list[dict]) -> str:
    return "[" + ",".join(dump_line(l) for l in lines) + "]"


def main(argv: list[str] | None = None, clock=None) -> int:
    parser = argparse.ArgumentParser(description="Sonde extérieure de la page d'état de Yanedo")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_measure = sub.add_parser("measure", help="sonder les services et écrire une ligne de mesure")
    p_loop = sub.add_parser("loop", help="un lot : mesurer les créneaux l'un après l'autre (au plus BATCH_SLOTS)")
    for p in (p_measure, p_loop):
        src = p.add_mutually_exclusive_group(required=True)
        src.add_argument("--targets-env", help="variable d'environnement qui contient les cibles (secret de l'hébergeur)")
        src.add_argument("--targets-file", help="fichier des cibles (essai à blanc sur un poste)")
        p.add_argument("--out", help="fichier où écrire la ligne (loop : ajoutée après chaque mesure)")
    p_loop.add_argument("--known-dir", help="dossier probe/ de la branche probe-data : créneaux déjà mesurés")
    p_loop.add_argument("--known-env", help="variable qui contient les lignes du job measure (job confirm)")
    p_loop.add_argument("--confirm", action="store_true", help="job confirm : mesurer tout de suite, puis finir le lot")
    p_plan = sub.add_parser("plan", help="nombre de lots de ce passage (sortie batches=[1,…])")
    p_plan.add_argument("--now", help="heure de référence RFC 3339 (essais)")
    p_record = sub.add_parser("record", help="ranger des lignes de mesure sur la branche des mesures")
    p_record.add_argument("--lines-env", action="append", required=True,
                          help="variable qui contient des lignes de mesure (répétable ; vide : rien)")
    p_record.add_argument("--remote", required=True)
    p_record.add_argument("--branch", default="probe-data")
    p_record.add_argument("--token-env")
    args = parser.parse_args(argv)
    clock = clock or Clock()
    try:
        if args.cmd == "measure":
            targets = load_targets(args.targets_env, args.targets_file)
            line = measure(targets, witnesses=WITNESSES, retry_delay=RETRY_DELAY)
            text = dump_line(line)
            if args.out:
                Path(args.out).write_text(text + "\n", encoding="utf-8")
            print_line(line)
            _github_output({"line": text, "all_down": "true" if is_all_down(line) else "false"})
        elif args.cmd == "loop":
            targets = load_targets(args.targets_env, args.targets_file)
            taken: set[dt.datetime] = set()
            if args.known_dir:
                day_file = Path(args.known_dir) / f"{clock.now():%Y-%m-%d}.jsonl"
                if day_file.is_file():
                    taken |= taken_slots(day_file.read_text(encoding="utf-8", errors="replace").splitlines())
            before = lines_from_env(args.known_env) if args.known_env else []
            taken |= taken_slots(before)
            quota = BATCH_SLOTS - (len(before) if args.confirm else 0)
            out = Path(args.out) if args.out else None

            def measure_one(at: dt.datetime) -> dict:
                return measure(targets, now=at, witnesses=WITNESSES, retry_delay=RETRY_DELAY)

            def emit(line: dict, note: str) -> None:
                print_line(line, f"créneau {floor_slot(slot_of_line(line)):%H:%M} UTC, mesure {line['at']} : {note}")
                if out:  # au fil de l'eau : chaque mesure est écrite dès qu'elle est faite
                    with out.open("a", encoding="utf-8") as fh:
                        fh.write(dump_line(line) + "\n")

            lines, pending = run_batch(measure_one, clock, taken, quota=quota, confirm=args.confirm, emit=emit)
            waiting = [pending] if pending is not None and not args.confirm else []
            print(f"lot : {len(lines)} créneau(x) à ranger" + (", un en attente de confirmation" if waiting else ""))
            _github_output({"lines": _dump_lines(lines), "pending": _dump_lines(waiting),
                            "all_down": "true" if waiting else "false"})
        elif args.cmd == "plan":
            now = (dt.datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else clock.now())
            batches = list(range(1, plan_batches(now) + 1))
            print(f"{len(batches)} lot(s) de {BATCH_SLOTS} créneaux au plus" if batches
                  else "hors des heures du contrat : rien à mesurer")
            _github_output({"batches": json.dumps(batches, separators=(",", ":"))})
        else:
            lines = [l for name in args.lines_env for l in lines_from_env(name)]
            token = os.environ.get(args.token_env, "") if args.token_env else None
            if args.token_env and not token:
                raise HostError(f"jeton absent : variable {args.token_env} vide")
            print(record(lines, args.remote, args.branch, token) if lines else "rien à ranger")
    except TargetsError as exc:
        for problem in exc.args[0]:
            print(f"REFUS : {problem}", file=sys.stderr)
        return 2
    except HostError as exc:
        print(f"REFUS : {exc.args[0]}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
