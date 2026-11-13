#!/usr/bin/env python3
"""Générateur de la page d'état publique de Yanedo (dépôt yanedo/status).

Entrée : content/ (site.yaml, services.yaml, texts.yaml, events/*.json et *.yaml).
Sortie : HTML et CSS sans JavaScript, flux RSS 2.0 et calendrier iCalendar, en français, allemand et anglais.
Dépendances : Python 3.9 ou plus, bibliothèque standard, et PyYAML (safe_load) épinglé dans requirements.txt.
check-event n'a pas besoin de PyYAML : il tourne avec « python3 -I » depuis un clone (chaînes de releases).

Commandes :
  build        construit la page (refuse tout contenu invalide ou non public)
  check-event  vérifie un fichier d'événement (schéma v1 et garde de contenu public)
  publish-git  publie la source chez l'hébergeur par git (GitHub Pages), sans écraser une mise à jour faite à la main

Codes de sortie : 0 ok ; 2 contenu refusé ; 3 hébergeur injoignable ou jeton refusé ;
4 mise à jour faite à la main chez l'hébergeur, pas encore reportée dans yanedo/status ; 1 autre erreur.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
ZURICH = ZoneInfo("Europe/Zurich")
UTC = dt.timezone.utc
LANGS = ("fr", "de", "en")
HTML_LANG = {"fr": "fr-CH", "de": "de-CH", "en": "en"}
RSS_LANG = {"fr": "fr-ch", "de": "de-ch", "en": "en"}
SCHEMA = "yanedo-status-event/1"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
KEY_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
VERSION_RE = re.compile(r"^20\d\d-W(0[1-9]|[1-4]\d|5[0-3])(\.\d+)?$")
TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?(Z|[+-]\d{2}:\d{2})$")
MAX_TEXT = 500
FUTURE_TOLERANCE = dt.timedelta(minutes=10)
RSS_MAX_ITEMS = 50

KINDS = {
    "maintenance": {
        "statuses": ("scheduled", "in_progress", "completed", "cancelled"),
        "required": ("start", "end", "modules", "impact", "text"),
        "optional": ("title",),
    },
    "incident": {
        "statuses": ("reported", "investigating", "fixing", "monitoring", "resolved"),
        "required": ("modules", "severity", "effect", "text"),
        "optional": ("title", "next_update_by", "summary"),
    },
    "release": {
        "statuses": ("completed", "rolled_back"),
        "required": ("version",),
        "optional": ("start", "modules", "title", "interruption"),
    },
}
COMMON = ("schema", "id", "kind", "status", "at")
ENUMS = {
    "impact": ("none", "degraded", "unavailable"),
    "severity": ("partial", "major"),
    "interruption": ("none", "brief"),
}
I18N_FIELDS = ("title", "effect", "text", "summary")
TIME_FIELDS = ("at", "start", "end", "next_update_by")

# Garde de contenu public : ce qui ne doit jamais paraître sur la page (note T32 v2.0, § 7).
# Des motifs génériques seulement : la liste de nos composants ne doit pas vivre dans ce dépôt.
FORBIDDEN = (
    ("adresse interne (*.internal)", re.compile(r"\.internal\b", re.I)),
    ("adresse IPv4", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    ("adresse IPv6", re.compile(r"\b(?:[0-9a-f]{1,4}:){3,7}[0-9a-f]{1,4}\b|\b[0-9a-f]{1,4}::(?:[0-9a-f]{1,4}\b)?", re.I)),
    ("adresse e-mail", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
    ("numéro de téléphone", re.compile(r"(?:\+|\b00)\d{2}[\s./-]?\d{2,3}(?:[\s./-]?\d{2,4}){2,4}")),
    ("adresse web", re.compile(r"\b(?:https?|ftp)://|\bwww\.", re.I)),
    ("identifiant de faille (CVE)", re.compile(r"\bCVE-\d{4}-\d{3,}", re.I)),
    ("nom technique (xxx-api, xxx-web…)", re.compile(
        r"\b[a-z0-9]+(?:-[a-z0-9]+)*-(?:api|web|worker|svc|db|operator|controller|server|gateway|proxy)\b", re.I)),
    # Sensible à la casse : nos noms techniques sont en minuscules ; « Yanedo-Dienste » est de l'allemand.
    ("nom technique (yanedo-xxx)", re.compile(r"\byanedo-[a-z0-9]")),
)
# Dans les fichiers produits, on cherche en plus ce qui pourrait venir des gabarits ou de l'adresse du site.
OUTPUT_FORBIDDEN = tuple(f for f in FORBIDDEN
                         if f[0] in ("adresse interne (*.internal)", "adresse IPv4", "adresse IPv6"))
assert len(OUTPUT_FORBIDDEN) == 3

DAYS = {
    "fr": ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"),
    "de": ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"),
    "en": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
}
MONTHS = {
    "fr": ("janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre",
           "novembre", "décembre"),
    "de": ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
           "November", "Dezember"),
    "en": ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December"),
}


class ContentError(Exception):
    """Contenu refusé (code de sortie 2) : la liste des problèmes est dans args[0]."""


class ManualEditPending(ContentError):
    """Mise à jour faite à la main chez l'hébergeur, pas encore reportée dans la source (code de sortie 4)."""


class HostError(Exception):
    """Hébergeur injoignable ou jeton refusé (code de sortie 3) : la liste des problèmes est dans args[0]."""


# --------------------------------------------------------------------------------------------- lecture

_YAML_LOADER = None


def _yaml_loader():
    """SafeLoader sans conversion implicite des dates (les heures restent des chaînes). PyYAML n'est chargé qu'ici."""
    global _YAML_LOADER
    if _YAML_LOADER is None:
        import yaml  # noqa: PLC0415 — seulement pour les fichiers .yaml

        class Loader(yaml.SafeLoader):
            pass

        Loader.yaml_implicit_resolvers = {
            key: [r for r in resolvers if r[0] != "tag:yaml.org,2002:timestamp"]
            for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
        }
        _YAML_LOADER = (yaml, Loader)
    return _YAML_LOADER


def load_file(path: Path):
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        return json.loads(text)
    yaml, loader = _yaml_loader()
    return yaml.load(text, Loader=loader)  # noqa: S506 — chargeur dérivé de SafeLoader


SERVICE_KEY_LINE = re.compile(r"^\s*- key:\s*([a-z][a-z0-9-]*)\s*$", re.M)
INTERNAL_KEYS_LINE = re.compile(r"^internal_keys:\s*\[([^\]]*)\]\s*$", re.M)


def read_service_keys(root: Path) -> set[str]:
    """Clés de module acceptées dans un événement, sans PyYAML (lecture ligne à ligne de services.yaml, testée)."""
    text = (root / "content" / "services.yaml").read_text(encoding="utf-8")
    keys = set(SERVICE_KEY_LINE.findall(text))
    match = INTERNAL_KEYS_LINE.search(text)
    if match:
        keys |= {k.strip() for k in match.group(1).split(",") if k.strip()}
    return keys


def parse_time(value) -> dt.datetime:
    if not isinstance(value, str) or not TIME_RE.match(value):
        raise ValueError(f"heure attendue au format RFC 3339 avec décalage (ex. 2026-11-15T16:00:00+01:00), reçu {value!r}")
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def guard_text(text: str) -> list[str]:
    """Problèmes de contenu public dans un texte (liste vide si le texte peut paraître sur la page)."""
    return [label for label, pattern in FORBIDDEN if pattern.search(text)]


# --------------------------------------------------------------------------------------------- contenu fixe

class Content:
    def __init__(self, root: Path, base_url: str | None = None):
        problems: list[str] = []
        self.site = load_file(root / "content" / "site.yaml") or {}
        self.texts = load_file(root / "content" / "texts.yaml") or {}
        services_file = load_file(root / "content" / "services.yaml") or {}
        services = services_file.get("services") or []
        internal = services_file.get("internal_keys") or []
        self.base_url = base_url or self.site.get("base_url", "")
        if not re.match(r"^https://[a-z0-9.-]+(:\d+)?/([A-Za-z0-9._~-]+/)*$", self.base_url):
            problems.append(f"site.yaml : base_url doit être une adresse https se terminant par « / », reçu {self.base_url!r}")
        self.history_days = int(self.site.get("history_days", 30))
        try:
            self.updated_at = parse_time(self.site.get("updated_at"))
        except ValueError as exc:
            problems.append(f"site.yaml : updated_at : {exc}")
            self.updated_at = None
        self.services: list[dict] = []
        seen = set()
        for i, svc in enumerate(services):
            key = svc.get("key") if isinstance(svc, dict) else None
            if not isinstance(key, str) or not KEY_RE.match(key) or key == "all":
                problems.append(f"services.yaml : service n° {i + 1} : clé invalide {key!r}")
                continue
            if key in seen:
                problems.append(f"services.yaml : clé en double {key!r}")
            seen.add(key)
            problems += [f"services.yaml : {key} : {p}" for p in check_i18n(svc.get("name"), "name")]
            self.services.append(svc)
        if not self.services:
            problems.append("services.yaml : aucun service")
        for key in internal:
            if not isinstance(key, str) or not KEY_RE.match(key) or key in seen or key == "all":
                problems.append(f"services.yaml : clé interne invalide ou en double {key!r}")
        self.internal_keys = set(internal) - seen
        for lang in LANGS:
            block = self.texts.get(lang)
            if not isinstance(block, dict):
                problems.append(f"texts.yaml : langue {lang} absente")
                continue
            missing = sorted(set(self.texts.get("fr", {})) - set(block))
            if missing:
                problems.append(f"texts.yaml : {lang} : clés manquantes {missing}")
            for key, value in block.items():
                values = value.values() if isinstance(value, dict) else [value]
                for v in values:
                    for p in guard_text(str(v)):
                        problems.append(f"texts.yaml : {lang}.{key} : {p}")
        try:
            self.holidays = load_calendar(root)
        except ContentError as exc:
            problems += exc.args[0]
            self.holidays = {}
        if problems:
            raise ContentError(problems)
        # Clés publiques (affichées) et clés internes (acceptées dans un événement, jamais affichées).
        self.public_keys = {s["key"] for s in self.services}
        self.service_keys = self.public_keys | self.internal_keys

    @property
    def host(self) -> str:
        return urlparse(self.base_url).hostname or ""

    def t(self, lang: str, key: str, **kwargs) -> str:
        value = self.texts[lang][key]
        return value.format(**kwargs) if kwargs else value

    def label(self, lang: str, group: str, value: str) -> str:
        return self.texts[lang][group][value]

    def module_names(self, lang: str, modules) -> str:
        if not modules:
            return ""
        if list(modules) == ["all"]:
            return self.t(lang, "all_services")
        names = {s["key"]: s["name"][lang] for s in self.services}
        return ", ".join(names[m] for m in modules if m in names)

    def visible(self, modules) -> bool:
        """Un événement qui ne touche que des modules internes n'apparaît pas sur la page publique."""
        return not modules or list(modules) == ["all"] or any(m in self.public_keys for m in modules)

    def page_url(self, lang: str) -> str:
        return self.base_url if lang == "fr" else f"{self.base_url}{lang}/"


def check_i18n(value, field: str) -> list[str]:
    if not isinstance(value, dict) or set(value) != set(LANGS):
        return [f"{field} : les trois langues fr, de et en sont obligatoires, et seulement elles"]
    problems = []
    for lang in LANGS:
        text = value[lang]
        if not isinstance(text, str) or not text.strip():
            problems.append(f"{field}.{lang} : texte vide")
            continue
        if len(text) > MAX_TEXT:
            problems.append(f"{field}.{lang} : plus de {MAX_TEXT} caractères")
        problems += [f"{field}.{lang} : contient une {p}" if p.startswith("adresse") else f"{field}.{lang} : contient un {p}"
                     for p in guard_text(text)]
    return problems


# --------------------------------------------------------------------------------------------- événements

def validate_event(ev, service_keys, now: dt.datetime | None = None) -> list[str]:
    """Erreurs du schéma v1 et de la garde (liste vide si l'événement est acceptable)."""
    if not isinstance(ev, dict):
        return ["l'événement doit être un objet JSON"]
    errors: list[str] = []
    if ev.get("schema") != SCHEMA:
        errors.append(f"schema doit valoir {SCHEMA!r}")
    kind = ev.get("kind")
    if kind not in KINDS:
        return errors + [f"kind doit valoir l'une de {sorted(KINDS)}, reçu {kind!r}"]
    spec = KINDS[kind]
    allowed = set(COMMON) | set(spec["required"]) | set(spec["optional"])
    for key in ev:
        if key not in allowed:
            errors.append(f"champ inconnu ou interdit pour « {kind} » : {key}")
    for key in COMMON + spec["required"]:
        if ev.get(key) in (None, "", [], {}):
            errors.append(f"champ obligatoire manquant : {key}")
    ident = ev.get("id")
    if isinstance(ident, str):
        if not ID_RE.match(ident):
            errors.append(f"id invalide {ident!r} (attendu : minuscules, chiffres et tirets, 3 à 64 caractères)")
        errors += [f"id : contient un {p}" for p in guard_text(ident)]
    status = ev.get("status")
    if status is not None and status not in spec["statuses"]:
        errors.append(f"status doit valoir l'une de {list(spec['statuses'])} pour « {kind} », reçu {status!r}")
    times = {}
    for key in TIME_FIELDS:
        if key in ev and ev[key] is not None:
            try:
                times[key] = parse_time(ev[key])
            except ValueError as exc:
                errors.append(f"{key} : {exc}")
    for key, values in ENUMS.items():
        if key in ev and ev[key] not in values:
            errors.append(f"{key} doit valoir l'une de {list(values)}, reçu {ev[key]!r}")
    if "modules" in ev and ev["modules"] not in (None, []):
        mods = ev["modules"]
        if not isinstance(mods, list) or not all(isinstance(m, str) for m in mods):
            errors.append("modules doit être une liste de clés de services")
        elif mods != ["all"]:
            unknown = [m for m in mods if m not in service_keys]
            if unknown:
                errors.append(f"modules inconnus (voir content/services.yaml) : {unknown}")
            if len(set(mods)) != len(mods):
                errors.append("modules : clé en double")
    if "version" in ev and (not isinstance(ev["version"], str) or not VERSION_RE.match(ev["version"])):
        errors.append(f"version invalide {ev.get('version')!r} (attendu 2026-W48 ou 2026-W48.1)")
    for key in I18N_FIELDS:
        if key in ev:
            errors += check_i18n(ev[key], key)
    if kind == "maintenance" and "start" in times and "end" in times and times["end"] <= times["start"]:
        errors.append("end doit suivre start")
    if kind == "incident":
        if status == "resolved":
            if "next_update_by" in ev:
                errors.append("next_update_by n'a pas de sens pour un incident résolu")
        else:
            if "summary" in ev:
                errors.append("summary n'est permis que pour un incident résolu")
            if "next_update_by" not in ev:
                errors.append("next_update_by est obligatoire tant que l'incident n'est pas résolu (le silence inquiète)")
            elif "at" in times and "next_update_by" in times and times["next_update_by"] <= times["at"]:
                errors.append("next_update_by doit suivre at")
    if kind == "release" and "start" in times and "at" in times and times["at"] < times["start"]:
        errors.append("at doit suivre start")
    if now is not None and "at" in times and times["at"] > now + FUTURE_TOLERANCE:
        errors.append("at est dans le futur : la « dernière mise à jour » mentirait")
    return errors


class Entry:
    """Une maintenance, un incident ou une version, avec toutes ses mises à jour, dans l'ordre."""

    def __init__(self, ident: str, kind: str):
        self.id = ident
        self.kind = kind
        self.updates: list[dict] = []

    @property
    def latest(self) -> dict:
        return self.updates[-1]

    @property
    def first(self) -> dict:
        return self.updates[0]

    def time(self, key: str, update: dict | None = None) -> dt.datetime | None:
        value = (update or self.latest).get(key)
        return parse_time(value) if value else None

    @property
    def active(self) -> bool:
        status = self.latest["status"]
        if self.kind == "incident":
            return status != "resolved"
        if self.kind == "maintenance":
            return status in ("scheduled", "in_progress")
        return False

    @property
    def closed_at(self) -> dt.datetime:
        if self.kind == "maintenance" and self.latest["status"] == "completed":
            return max(self.time("end"), self.time("at"))
        return self.time("at")


def load_events(root: Path, content: Content, now: dt.datetime, extra: list[Path] = ()) -> list[Entry]:
    files = sorted((root / "content" / "events").glob("*.json")) + sorted((root / "content" / "events").glob("*.yaml"))
    files += list(extra)
    problems: list[str] = []
    entries: dict[str, Entry] = {}
    seen: set[tuple[str, str]] = set()
    for path in files:
        try:
            ev = load_file(path)
        except Exception as exc:  # noqa: BLE001 — JSON ou YAML illisible : refusé avec le message du lecteur
            problems.append(f"{path.name} : illisible : {exc}")
            continue
        errs = validate_event(ev, content.service_keys, now)
        if errs:
            problems += [f"{path.name} : {e}" for e in errs]
            continue
        key = (ev["id"], parse_time(ev["at"]).astimezone(UTC).isoformat())
        if key in seen:
            problems.append(f"{path.name} : deux mises à jour de {ev['id']} à la même heure")
            continue
        seen.add(key)
        entry = entries.setdefault(ev["id"], Entry(ev["id"], ev["kind"]))
        if entry.kind != ev["kind"]:
            problems.append(f"{path.name} : {ev['id']} est déjà un événement de type « {entry.kind} »")
            continue
        entry.updates.append(ev)
    if problems:
        raise ContentError(problems)
    for entry in entries.values():
        entry.updates.sort(key=lambda u: parse_time(u["at"]))
    return list(entries.values())


# --------------------------------------------------------------------------------------------- état dérivé

SEVERITY_RANK = {"ok": 0, "maintenance": 1, "partial": 2, "major": 3}
SERVICE_RANK = {"operational": 0, "maintenance": 1, "degraded": 2, "unavailable": 3}


class State:
    def __init__(self, content: Content, entries: list[Entry], now: dt.datetime):
        self.content = content
        self.now = now
        entries = [e for e in entries if content.visible(e.latest.get("modules"))]
        self.entries = entries
        self.incidents = sorted((e for e in entries if e.kind == "incident" and e.active),
                                key=lambda e: e.time("at", e.first), reverse=True)
        self.maintenances = sorted((e for e in entries if e.kind == "maintenance" and e.active),
                                   key=lambda e: e.time("start"))
        cutoff = now - dt.timedelta(days=content.history_days)
        self.history = sorted((e for e in entries if not e.active and e.closed_at >= cutoff),
                              key=lambda e: e.closed_at, reverse=True)
        times = [e.time("at", u) for e in entries for u in e.updates]
        self.last_update = max(times + ([content.updated_at] if content.updated_at else []))
        self.banner, self.banner_modules = self._banner()
        self.services = self._services()
        self.availability: Availability | None = None  # posée par build() quand la sonde a mesuré

    def _modules(self, entry: Entry) -> list[str]:
        mods = entry.latest.get("modules") or []
        if mods == ["all"]:
            return [s["key"] for s in self.content.services]
        return [m for m in mods if m in self.content.public_keys]

    def _banner(self):
        level, modules = "ok", []
        for e in self.incidents:
            sev = e.latest["severity"]
            if SEVERITY_RANK[sev] > SEVERITY_RANK[level]:
                level, modules = sev, []
            if sev == level:
                modules += [m for m in self._modules(e) if m not in modules]
        if level == "ok":
            for e in self.maintenances:
                if e.latest["status"] == "in_progress":
                    level = "maintenance"
                    modules += [m for m in self._modules(e) if m not in modules]
        return level, modules

    def _services(self) -> dict[str, str]:
        state = {s["key"]: "operational" for s in self.content.services}

        def worsen(key, value):
            if SERVICE_RANK[value] > SERVICE_RANK[state[key]]:
                state[key] = value

        for e in self.incidents:
            for m in self._modules(e):
                worsen(m, "unavailable" if e.latest["severity"] == "major" else "degraded")
        for e in self.maintenances:
            if e.latest["status"] == "in_progress" and e.latest["impact"] != "none":
                for m in self._modules(e):
                    worsen(m, "maintenance")
        return state


# --------------------------------------------------------------------------------------------- disponibilité

# Barres de disponibilité sur 90 jours (note T32 v2.0, § 7, « Plus tard ») : mesurées par la sonde extérieure
# (probe.py, chaîne « probe » de l'hébergeur), seulement pendant les heures du contrat. Constantes du contrat, pas des
# réglages : les changer, c'est changer le contrat.
PROBE_SCHEMA = "yanedo-status-probe/1"
AVAIL_DAYS = 90
AVAIL_SPANS = (90, 60, 30)  # bureau, tablette, téléphone : la feuille de style montre l'une des trois
AVAIL_MONTHS = 3  # tableau mensuel : les 3 derniers mois complets, plus le mois en cours (décision D7)
CONTRACT_START, CONTRACT_END = dt.time(9, 0), dt.time(17, 0)  # du lundi au vendredi, à Zurich
SLOT = dt.timedelta(minutes=5)
SLOTS_PER_DAY = 96
MIN_COVERAGE = 0.5  # moins d'un créneau mesuré sur deux : « mesures insuffisantes », sauf si un échec a été mesuré
LEVELS = ("full", "brief", "degraded", "down")  # du meilleur au pire
LEVEL_FLOOR = {"partial": "brief", "major": "degraded"}  # un jour avec un incident déclaré n'est jamais vert
# Contrat d'abonnement, définition de la « Maintenance Planifiée » : annoncée au moins 5 Jours Ouvrables à l'avance.
MAINTENANCE_NOTICE_DAYS = 5


def month_list(today: dt.date, back: int) -> list[tuple[int, int]]:
    """Les `back` mois complets qui précèdent celui de `today`, puis celui-ci : du plus ancien au plus récent."""
    y, m, out = today.year, today.month, []
    for _ in range(back + 1):
        out.append((y, m))
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return out[::-1]


def level_for(percent: float) -> str:
    if percent >= 100:
        return "full"
    if percent >= 95:
        return "brief"
    return "degraded" if percent >= 75 else "down"


def floor_slot(d: dt.datetime) -> dt.datetime:
    """Début du créneau de 5 minutes (UTC). Zurich est décalé d'heures entières : mêmes bornes qu'en heure locale."""
    d = d.astimezone(UTC)
    return d.replace(minute=d.minute - d.minute % 5, second=0, microsecond=0)


class ProbeData:
    """Les mesures de la sonde : par service, l'état de chaque créneau (le pire des mesures du créneau)."""

    def __init__(self):
        self.slots: dict[str, dict[dt.datetime, bool]] = {}
        self.first: dict[str, dt.datetime] = {}
        self.last: dt.datetime | None = None
        self.ignored = 0


def load_probe_data(directory: Path, keys: set[str], now: dt.datetime) -> ProbeData:
    """Lit les fichiers *.jsonl de la sonde. Une ligne illisible est ignorée et comptée, jamais bloquante : une mesure
    abîmée ne doit pas empêcher la publication d'un incident."""
    data = ProbeData()
    for path in sorted(directory.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                if rec.get("schema") != PROBE_SCHEMA or not isinstance(rec.get("witness"), bool):
                    raise ValueError("schéma")
                at = parse_time(rec["at"])
                results = rec["results"]
                if not isinstance(results, dict):
                    raise ValueError("results")
            except (ValueError, KeyError, TypeError, AttributeError):
                data.ignored += 1
                continue
            if at > now + FUTURE_TOLERANCE:
                data.ignored += 1
                continue
            if not rec["witness"]:
                continue  # le réseau de la sonde elle-même ne répondait pas : rien n'est mesuré
            data.last = at if data.last is None else max(data.last, at)
            slot = floor_slot(at)
            for key, value in results.items():
                if key not in keys or value not in ("up", "down"):
                    continue
                data.first[key] = min(data.first.get(key, at), at)
                slots = data.slots.setdefault(key, {})
                slots[slot] = (value == "up") and slots.get(slot, True)
    return data


class DayStat:
    """Un jour, un service : niveau (full, brief, degraded, down, unknown ou off), raison d'un « off », créneaux."""

    def __init__(self, date: dt.date, level: str, reason: str = "", up: int = 0, measured: int = 0, expected: int = 0,
                 incidents: list | None = None):
        self.date, self.level, self.reason = date, level, reason
        self.up, self.measured, self.expected = up, measured, expected
        self.incidents = incidents or []

    @property
    def percent(self) -> float | None:
        return 100.0 * self.up / self.measured if self.measured else None


def _overlaps(a1: dt.datetime, a2: dt.datetime, b1: dt.datetime, b2: dt.datetime) -> bool:
    return a1 < b2 and b1 < a2


class Availability:
    """Disponibilité par jour et par service, pendant les heures du contrat seulement.

    - Créneaux de 5 minutes de 09:00 à 17:00 (heure de Zurich), du lundi au vendredi, hors jours non ouvrables.
    - Hors du calcul : une Maintenance Planifiée au sens du contrat (annoncée au moins 5 Jours Ouvrables à l'avance),
      sur ce service, et seulement dans la fenêtre annoncée (planned_window). Le reste compte.
    - Créneau non mesuré (retard ou trou du planificateur) : ni disponible ni indisponible, simplement absent.
    - Pourcentage = créneaux disponibles / créneaux mesurés ; un incident déclaré ce jour-là pendant les heures du
      contrat empêche le vert (LEVEL_FLOOR), et un échec mesuré n'est jamais caché par « mesures insuffisantes ».
    """

    def __init__(self, content: Content, state: State, data: ProbeData, now: dt.datetime, holidays: dict[dt.date, dict]):
        self.content, self.data, self.now, self.holidays = content, data, now, holidays
        self.today = _local(now).date()
        self.days = [self.today - dt.timedelta(days=i) for i in range(AVAIL_DAYS - 1, -1, -1)]
        keys = [s["key"] for s in content.services]
        self.maintenance = {k: [] for k in keys}
        self.incidents = {k: [] for k in keys}
        for e in state.entries:
            mods = e.latest.get("modules") or []
            touched = keys if mods == ["all"] else [m for m in mods if m in self.maintenance]
            if e.kind == "maintenance":
                window = self.planned_window(e)
                if window:
                    for k in touched:
                        self.maintenance[k].append(window)
            elif e.kind == "incident":
                start = e.time("at", e.first)
                end = e.time("at") if e.latest["status"] == "resolved" else now
                end = max(end, start + dt.timedelta(minutes=1))
                severity = "major" if any(u["severity"] == "major" for u in e.updates) else "partial"
                for k in touched:
                    self.incidents[k].append((start, end, severity, e))
        # Mois du tableau (D7) : ils peuvent commencer avant la fenêtre de 90 jours ; même calcul jour par jour.
        self.months = month_list(self.today, AVAIL_MONTHS)
        start = min(self.days[0], dt.date(*self.months[0], 1))
        span = [start + dt.timedelta(days=i) for i in range((self.today - start).days + 1)]
        daily = {k: {d: self._day(k, d) for d in span} for k in keys}
        self.services = {k: [daily[k][d] for d in self.days] for k in keys}
        self.monthly: dict[str, dict[tuple[int, int], tuple[int, int, int]]] = {}
        for k in keys:
            self.monthly[k] = {}
            for ym in self.months:
                stats = [st for d, st in daily[k].items() if (d.year, d.month) == ym]
                self.monthly[k][ym] = (sum(st.up for st in stats), sum(st.measured for st in stats),
                                       sum(st.expected for st in stats))
        self.totals = {}
        for k in keys:
            self.totals[k] = {}
            for span in AVAIL_SPANS:
                stats = [d for d in self.services[k][-span:] if d.level != "off"]
                self.totals[k][span] = (sum(d.up for d in stats), sum(d.measured for d in stats))
        self.first = min(data.first.values()) if data.first else None
        self.last = data.last

    def working_day(self, day: dt.date) -> bool:
        """Jour Ouvrable du contrat : ni samedi, ni dimanche, ni jour férié officiel vaudois."""
        return day.weekday() < 5 and day not in self.holidays

    def notice_days(self, announced: dt.datetime, start: dt.datetime) -> int:
        """Jours Ouvrables entiers de préavis : le jour de l'annonce ne compte pas (CO 77, al. 1, ch. 1), et un jour
        ne compte que si ses Heures Ouvrables sont finies (17:00) avant le début de la maintenance."""
        day, count = _local(announced).date() + dt.timedelta(days=1), 0
        while dt.datetime.combine(day, CONTRACT_END, ZURICH) <= start:
            count += self.working_day(day)
            day += dt.timedelta(days=1)
        return count

    def planned_window(self, e: Entry) -> tuple[dt.datetime, dt.datetime] | None:
        """La part d'une maintenance qui sort du calcul : une Maintenance Planifiée au sens du contrat, « toute
        interruption de service annoncée au Client au moins cinq (5) Jours Ouvrables à l'avance et réalisée dans la
        fenêtre annoncée ».

        - La fenêtre annoncée est celle de la dernière annonce (status « scheduled ») publiée avec ce préavis, et qui
          annonce une interruption (impact autre que « none »).
        - Seule son intersection avec la fenêtre réelle (dernière mise à jour, en cours ou terminée) sort du calcul.
          Un dépassement, un report annoncé trop tard, une annulation ou une maintenance déclarée après coup comptent.
        """
        latest = e.latest
        if latest["status"] not in ("in_progress", "completed") or latest["impact"] == "none":
            return None
        announced = None
        for u in e.updates:
            if u["status"] == "scheduled" and u["impact"] != "none":
                start = parse_time(u["start"])
                if self.notice_days(parse_time(u["at"]), start) >= MAINTENANCE_NOTICE_DAYS:
                    announced = (start, parse_time(u["end"]))
        if announced is None:
            return None
        start, end = max(announced[0], e.time("start")), min(announced[1], e.time("end"))
        return (start, end) if start < end else None

    def _day(self, key: str, day: dt.date) -> DayStat:
        day_start = dt.datetime.combine(day, dt.time(0), ZURICH)
        day_end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0), ZURICH)
        incidents = [i for i in self.incidents[key] if _overlaps(i[0], i[1], day_start, day_end)]
        listed = [i[3] for i in incidents]
        if day.weekday() >= 5:
            return DayStat(day, "off", "weekend", incidents=listed)
        if day in self.holidays:
            return DayStat(day, "off", "holiday", incidents=listed)
        ws = dt.datetime.combine(day, CONTRACT_START, ZURICH)
        we = dt.datetime.combine(day, CONTRACT_END, ZURICH)
        first = self.data.first.get(key)
        slots = self.data.slots.get(key, {})
        up = measured = expected = excluded = 0
        for i in range(SLOTS_PER_DAY):
            s = ws + i * SLOT
            e = s + SLOT
            if any(_overlaps(s, e, a, b) for a, b in self.maintenance[key]):
                excluded += 1
                continue
            value = slots.get(s.astimezone(UTC))
            if value is None:
                if first is None or e <= first or e > self.now:
                    continue  # avant la première mesure de ce service, ou créneau pas encore écoulé
                expected += 1
            else:
                expected += 1
                measured += 1
                up += value
        if expected == 0:
            if excluded == SLOTS_PER_DAY:
                reason = "maintenance"
            elif day == self.today and first is not None and first < we:
                reason = "not_started"
            else:
                reason = "before"
            return DayStat(day, "off", reason, incidents=listed)
        if measured and (up < measured or measured >= MIN_COVERAGE * expected):
            level = level_for(100.0 * up / measured)
        else:
            level = "unknown"
        for start, end, severity, _ in incidents:
            if _overlaps(start, end, ws, we):
                floor = LEVEL_FLOOR[severity]
                if level == "unknown" or LEVELS.index(level) < LEVELS.index(floor):
                    level = floor
        return DayStat(day, level, "", up, measured, expected, listed)


def load_calendar(root: Path) -> dict[dt.date, dict]:
    """Jours non ouvrables : content/calendar.yaml, copie telle quelle du calendar.yaml du dépôt releases.

    Rend jour férié → nom en trois langues. Une année absente de covered_years n'est qu'un avertissement
    (calendar_warnings) : un calendrier en retard ne doit jamais empêcher la publication d'un incident."""
    path = root / "content" / "calendar.yaml"
    if not path.is_file():
        return {}
    data = load_file(path) or {}
    problems: list[str] = []
    hours = data.get("contract_hours") or {}
    if (data.get("timezone") != "Europe/Zurich" or hours.get("start") != CONTRACT_START.strftime("%H:%M")
            or hours.get("end") != CONTRACT_END.strftime("%H:%M")):
        problems.append("calendar.yaml : timezone et contract_hours doivent valoir Europe/Zurich, 09:00-17:00 "
                        "(heures du contrat du générateur et des textes de la page)")
    days: dict[dt.date, dict] = {}
    for item in data.get("holidays") or []:
        value = item.get("date") if isinstance(item, dict) else None
        try:
            day = dt.date.fromisoformat(str(value))
        except ValueError:
            problems.append(f"calendar.yaml : date invalide {value!r} (attendu AAAA-MM-JJ)")
            continue
        name = item.get("name")
        problems += [f"calendar.yaml : {day} : {p}" for p in check_i18n(name, "name")]
        days[day] = name
    if problems:
        raise ContentError(problems)
    return days


def calendar_warnings(root: Path, now: dt.datetime) -> list[str]:
    """Avertissements du calendrier pour l'année en cours (ne bloquent rien)."""
    path = root / "content" / "calendar.yaml"
    if not path.is_file():
        return ["calendar.yaml absent : seuls les week-ends sont hors des heures du contrat"]
    covered = (load_file(path) or {}).get("covered_years") or []
    year = _local(now).year
    return [] if year in covered else [f"calendar.yaml : l'année {year} n'est pas couverte (covered_years) : "
                                       "reporter le calendar.yaml de releases"]


def floor_percent(value: float, decimals: int) -> float:
    """Arrondi vers le bas : 99,99 % ne s'affiche jamais 100 %."""
    factor = 10 ** decimals
    return int(value * factor + 1e-9) / factor


def fmt_percent(value: float, lang: str, decimals: int) -> str:
    v = floor_percent(value, decimals)
    text = f"{v:.0f}" if v == 100 else f"{v:.{decimals}f}"
    if lang == "en":
        return f"{text}%"
    return text.replace(".", ",") + " %"


# --------------------------------------------------------------------------------------------- dates

def _local(d: dt.datetime) -> dt.datetime:
    return d.astimezone(ZURICH)


def fmt_date(d: dt.datetime, lang: str) -> str:
    d = _local(d)
    day, month = DAYS[lang][d.weekday()], MONTHS[lang][d.month - 1]
    if lang == "fr":
        return f"{day} {'1er' if d.day == 1 else d.day} {month} {d.year}"
    if lang == "de":
        return f"{day}, {d.day}. {month} {d.year}"
    return f"{day} {d.day} {month} {d.year}"


def fmt_time(d: dt.datetime) -> str:
    return _local(d).strftime("%H:%M")


def fmt_datetime(d: dt.datetime, lang: str) -> str:
    return f"{fmt_date(d, lang)}, {fmt_time(d)}"


def fmt_when(d: dt.datetime, ref: dt.datetime, lang: str) -> str:
    """L'heure seule le jour de référence, sinon la date et l'heure."""
    return fmt_time(d) if _local(d).date() == _local(ref).date() else fmt_datetime(d, lang)


def fmt_window(start: dt.datetime, end: dt.datetime, lang: str) -> str:
    if _local(start).date() == _local(end).date():
        date, t1, t2 = cap(fmt_date(start, lang)), fmt_time(start), fmt_time(end)
        return {"fr": f"{date}, de {t1} à {t2}", "de": f"{date}, {t1}–{t2} Uhr", "en": f"{date}, {t1}–{t2}"}[lang]
    a, b = fmt_datetime(start, lang), fmt_datetime(end, lang)
    return {"fr": f"Du {a} au {b}", "de": f"{cap(a)} Uhr bis {b} Uhr", "en": f"From {a} to {b}"}[lang]


def fmt_range(start: dt.datetime, end: dt.datetime, lang: str) -> str:
    if _local(start).date() == _local(end).date():
        return f"{fmt_time(start)} – {fmt_time(end)}"
    return f"{fmt_datetime(start, lang)} – {fmt_datetime(end, lang)}"


def cap(text: str) -> str:
    return text[:1].upper() + text[1:]


# --------------------------------------------------------------------------------------------- textes

def entry_title(c: Content, e: Entry, lang: str) -> str:
    latest = e.latest
    if latest.get("title"):
        return latest["title"][lang]
    names = c.module_names(lang, latest.get("modules"))
    if e.kind == "incident":
        return c.t(lang, "incident_title_" + latest["severity"], modules=names)
    if e.kind == "maintenance":
        return c.t(lang, "maintenance_title", modules=names)
    if latest["status"] == "completed":
        return c.t(lang, "release_completed", version=latest["version"])
    if latest.get("modules"):
        return c.t(lang, "release_rolled_back_modules", modules=names)
    return c.t(lang, "release_rolled_back", version=latest["version"])


def entry_span(e: Entry) -> tuple[dt.datetime, dt.datetime]:
    if e.kind == "maintenance":
        return e.time("start"), e.time("end")
    if e.kind == "release":
        return e.time("start") or e.time("at"), e.time("at")
    return e.time("at", e.first), e.time("at")


def entry_detail(c: Content, e: Entry, lang: str) -> str:
    latest = e.latest
    if e.kind == "release":
        return c.label(lang, "interruption", latest["interruption"]) if latest.get("interruption") else ""
    if e.kind == "maintenance":
        return c.label(lang, "impact", latest["impact"])
    return c.t(lang, "summary_published") if latest.get("summary") else c.label(lang, "incident_status", "resolved")


def status_label(c: Content, e: Entry, lang: str, update: dict | None = None) -> str:
    u = update or e.latest
    group = {"incident": "incident_status", "maintenance": "maintenance_status", "release": "release_status"}[e.kind]
    return c.label(lang, group, u["status"])


# --------------------------------------------------------------------------------------------- HTML

ICONS = {
    "ok": '<path d="M20 6 9 17l-5-5"/>',
    "partial": '<path d="M12 3 2 21h20L12 3z"/><path d="M12 10v5M12 18h.01"/>',
    "major": '<circle cx="12" cy="12" r="9"/><path d="m15 9-6 6M9 9l6 6"/>',
    "maintenance": '<path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18l3 3 6.3-6.3a4 4 0 0 0 5.4-5.4l-2.5 2.5-2.5-.5-.5-2.5 2.5-2.5z"/>',
}
PILL = {
    "operational": "ok", "degraded": "warn", "unavailable": "bad", "maintenance": "info",
    "reported": "warn", "investigating": "warn", "fixing": "warn", "monitoring": "warn", "resolved": "ok",
    "scheduled": "info", "in_progress": "info", "completed": "ok", "cancelled": "neutral", "rolled_back": "warn",
}


def esc(text: str) -> str:
    return html.escape(text, quote=True)


def icon(name: str) -> str:
    return ('<svg class="icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false" fill="none" stroke="currentColor" '
            f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">{ICONS[name]}</svg>')


def pill(text: str, status: str) -> str:
    return f'<span class="pill pill--{PILL.get(status, "neutral")}">{esc(text)}</span>'


def render_html(c: Content, s: State, lang: str) -> str:
    t = lambda key, **kw: esc(c.t(lang, key, **kw))  # noqa: E731
    prefix = "" if lang == "fr" else "../"
    ref = s.now

    if s.banner == "ok":
        banner_title = t("banner_ok")
        banner_line = ""
    else:
        banner_title = t("banner_" + s.banner, modules=c.module_names(lang, s.banner_modules))
        banner_line = t("banner_others") + " "
    banner_line += t("last_update", date=fmt_datetime(s.last_update, lang))

    langs = []
    for other in LANGS:
        label = other.upper()
        if other == lang:
            langs.append(f'<span aria-current="page">{label}</span>')
        else:
            href = prefix + ("" if other == "fr" else f"{other}/")
            langs.append(f'<a href="{href or "./"}" hreflang="{HTML_LANG[other]}" lang="{HTML_LANG[other]}">{label}</a>')

    cards = []
    for e in s.incidents:
        latest = e.latest
        names = esc(c.module_names(lang, latest["modules"]))
        steps = []
        for u in reversed(e.updates):
            when = esc(fmt_when(parse_time(u["at"]), ref, lang))
            steps.append(f'<li><p class="step-title">{when} · {esc(status_label(c, e, lang, u))}</p>'
                         f'<p class="muted">{esc(u["text"][lang])}</p></li>')
        nxt = ""
        if latest.get("next_update_by"):
            nxt = f'<p class="next">{t("next_update", time=fmt_when(parse_time(latest["next_update_by"]), ref, lang))}</p>'
        title = esc(latest["title"][lang]) if latest.get("title") else f'{t("current_incident")} · {names}'
        cards.append(
            f'<article class="card" id="{esc(e.id)}"><div class="card-head"><h2>{title}</h2>'
            f'{pill(status_label(c, e, lang), latest["status"])}</div>'
            f'<p class="muted">{t("since", time=fmt_when(parse_time(e.first["at"]), ref, lang))} · '
            f'{t("effect_label")} {esc(latest["effect"][lang])}</p>'
            f'<ol class="steps">{"".join(steps)}</ol>{nxt}</article>')
    for e in s.maintenances:
        latest = e.latest
        heading = t("maintenance_in_progress" if latest["status"] == "in_progress" else "maintenance_planned")
        window = esc(fmt_window(e.time("start"), e.time("end"), lang))
        cards.append(
            f'<article class="card" id="{esc(e.id)}"><div class="card-head"><h2>{heading}</h2>'
            f'{pill(status_label(c, e, lang), latest["status"])}</div>'
            f'<p>{window} · {esc(c.module_names(lang, latest["modules"]))} · '
            f'{esc(c.label(lang, "impact", latest["impact"]))}</p>'
            f'<p class="muted">{esc(latest["text"][lang])}</p>'
            f'<p><a class="btn" href="maintenance/{esc(e.id)}.ics">{t("add_to_calendar")}</a></p></article>')
    if not cards:
        cards.append(f'<article class="card"><h2>{t("nothing_planned_title")}</h2>'
                     f'<p class="muted">{t("nothing_planned")}</p></article>')

    services = render_services(c, s, lang)

    hist = []
    for e in s.history:
        start, end = entry_span(e)
        summary = ""
        if e.latest.get("summary"):
            summary = f'<p class="summary"><strong>{t("summary_label")}</strong> {esc(e.latest["summary"][lang])}</p>'
        hist.append(
            f'<li id="{esc(e.id)}"><p class="step-title">{esc(cap(fmt_date(start, lang)))} · {esc(entry_title(c, e, lang))}</p>'
            f'<p class="muted">{esc(" · ".join(x for x in (fmt_range(start, end, lang), entry_detail(c, e, lang)) if x))}</p>'
            f'{summary}{pill(status_label(c, e, lang), e.latest["status"])}</li>')
    history = (f'<ul class="history">{"".join(hist)}</ul>' if hist
               else f'<p class="muted">{t("history_empty", days=c.history_days)}</p>')

    alternates = "".join(
        f'<link rel="alternate" hreflang="{HTML_LANG[o]}" href="{esc(c.page_url(o))}">' for o in LANGS)
    return f"""<!doctype html>
<html lang="{HTML_LANG[lang]}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'">
<meta name="referrer" content="no-referrer">
<meta name="description" content="{t('meta_description')}">
<title>{t('page_title')}</title>
<link rel="stylesheet" href="{prefix}style.css">
<link rel="alternate" type="application/rss+xml" title="{t('rss_title')}" href="rss.xml">
<link rel="canonical" href="{esc(c.page_url(lang))}">
{alternates}
</head>
<body>
<div class="page">
<header class="top">
<p class="brand"><span class="logo" aria-hidden="true">Y</span><span class="brand-name">{t('page_title')}</span></p>
<nav class="langs" aria-label="{t('language_nav')}">{' · '.join(langs)}</nav>
</header>
<main>
<section class="banner banner--{s.banner}" role="status" aria-live="polite">
<h1>{icon(s.banner)}<span>{banner_title}</span></h1>
<p>{banner_line}</p>
</section>
<div class="stack">{''.join(cards)}</div>
{services}
<section class="card" aria-labelledby="history-title"><h2 id="history-title">{t('history_title', days=c.history_days)}</h2>
{history}</section>
</main>
<footer>
<p>{t('commitments')}</p>
<p>{t('support')} · <a href="rss.xml">{t('rss_link')}</a> · <a href="maintenances.ics">{t('ics_link')}</a></p>
<p>{t('subscribe_note')}</p>
</footer>
</div>
</body>
</html>
"""


def render_services(c: Content, s: State, lang: str) -> str:
    """La carte « Services » : l'état de chacun et, quand la sonde a mesuré, ses barres de disponibilité.

    Sans JavaScript (CSP default-src 'none') : l'info-bulle est en CSS (survol, ou appui qui donne le focus), la
    largeur d'écran choisit 90, 60 ou 30 jours. Les barres sont décoratives pour les lecteurs d'écran
    (aria-hidden) : le même contenu est en texte (pourcentage visible, légende, tableau des jours sous 100 %)."""
    t = lambda key, **kw: esc(c.t(lang, key, **kw))  # noqa: E731
    a: Availability | None = s.availability
    head = f'<h2 id="services-title">{t("services_title")}</h2>'
    if a is None:
        rows = "".join(f'<li><span>{esc(svc["name"][lang])}</span>'
                       f'{pill(c.label(lang, "service_status", s.services[svc["key"]]), s.services[svc["key"]])}</li>'
                       for svc in c.services)
        return f'<section class="card" aria-labelledby="services-title">{head}<ul class="services">{rows}</ul></section>'

    def spans(texts: dict[int, str]) -> str:
        return "".join(f'<span class="span-{n}">{esc(texts[n])}</span>' for n in AVAIL_SPANS)

    intro = [c.t(lang, "avail_intro")]
    if a.first is not None and _local(a.first).date() > a.days[0]:
        intro.append(c.t(lang, "avail_since", date=fmt_date(a.first, lang)))
    if a.last is not None:
        intro.append(c.t(lang, "avail_last", date=fmt_datetime(a.last, lang)))
    legend = "".join(f'<li><span class="sw sw--{lvl}" aria-hidden="true"></span>{esc(c.label(lang, "avail_legend", lvl))}</li>'
                     for lvl in (*LEVELS, "unknown", "off"))
    oldest = {n: a.days[-n].strftime("%d.%m.%Y") for n in AVAIL_SPANS}
    rows, below = [], []
    for svc in c.services:
        key, name = svc["key"], svc["name"][lang]
        st = s.services[key]
        totals = {}
        for n in AVAIL_SPANS:
            up, measured = a.totals[key][n]
            totals[n] = (c.t(lang, "avail_total", percent=fmt_percent(100.0 * up / measured, lang, 2), days=n)
                         if measured else c.t(lang, "avail_none"))
        bars = []
        for d in a.services[key]:
            bars.append(f'<li class="b b--{d.level}" tabindex="-1"><span class="tip">{bar_tip(c, d, lang)}</span></li>')
            if d.level not in ("off", "full"):
                below.append((d, name))
        rows.append(
            f'<li><div class="svc-head"><span class="svc-name">{esc(name)}</span><span class="svc-meta">'
            f'<span class="svc-total">{spans(totals)}</span>{pill(c.label(lang, "service_status", st), st)}</span></div>'
            f'<ol class="bars" aria-hidden="true">{"".join(bars)}</ol>'
            f'<p class="axis" aria-hidden="true"><span>{spans(oldest)}</span><span>{t("avail_today")}</span></p></li>')
    monthly = render_monthly(c, a, lang)
    below.sort(key=lambda x: x[0].date, reverse=True)
    if below:
        cells = []
        for d, name in below:
            pct = fmt_percent(d.percent, lang, 1) if d.percent is not None else "—"
            inc = "; ".join(entry_title(c, e, lang) for e in d.incidents)
            cells.append(f'<tr><td>{esc(cap(fmt_date(dt.datetime.combine(d.date, dt.time(12), ZURICH), lang)))}</td>'
                         f'<td>{esc(name)}</td><td>{esc(pct)}</td><td>{esc(c.label(lang, "avail_level", d.level))}</td>'
                         f'<td>{esc(inc)}</td></tr>')
        table = (f'<details class="avail-details"><summary>{t("avail_details", days=AVAIL_DAYS, count=len(below))}'
                 f'</summary><div class="details-body"><div class="table-wrap"><table><thead><tr><th scope="col">{t("avail_col_date")}</th>'
                 f'<th scope="col">{t("avail_col_service")}</th><th scope="col">{t("avail_col_value")}</th>'
                 f'<th scope="col">{t("avail_col_state")}</th><th scope="col">{t("avail_col_incident")}</th></tr></thead>'
                 f'<tbody>{"".join(cells)}</tbody></table></div><p class="muted">{t("avail_details_rest")}</p></div></details>')
    else:
        table = f'<p class="muted">{t("avail_details_none", days=AVAIL_DAYS)}</p>'
    return (f'<section class="card" aria-labelledby="services-title">{head}'
            f'<p class="muted">{esc(" ".join(intro))}</p>'
            f'<ul class="legend" aria-label="{t("avail_legend_title")}">{legend}</ul>'
            f'<ul class="services services--bars">{"".join(rows)}</ul>'
            f'<p class="muted">{t("avail_note")}</p>{monthly}{table}</section>')


def render_monthly(c: Content, a: Availability, lang: str) -> str:
    """Disponibilité par mois et par service (D7). Volontairement SANS objectif ni crédit : la page sert tous les
    clients et mesure des modules ; la comparaison au contrat appartient au rapport mensuel de chaque client."""
    t = lambda key, **kw: esc(c.t(lang, key, **kw))  # noqa: E731
    heads = []
    for ym in a.months:
        label = f"{cap(MONTHS[lang][ym[1] - 1])} {ym[0]}"
        heads.append(c.t(lang, "avail_monthly_current", month=label) if ym == a.months[-1] else label)
    rows = []
    for svc in c.services:
        cells = []
        for ym in a.months:
            up, measured, expected = a.monthly[svc["key"]][ym]
            if not measured:
                cells.append("—")
            else:
                text = fmt_percent(100.0 * up / measured, lang, 2)
                if measured < MIN_COVERAGE * expected:
                    text += f' ({c.t(lang, "avail_monthly_partial")})'
                cells.append(text)
        rows.append(f'<tr><th scope="row">{esc(svc["name"][lang])}</th>'
                    + "".join(f"<td>{esc(x)}</td>" for x in cells) + "</tr>")
    return (f'<details class="avail-details"><summary>{t("avail_monthly_title")}</summary><div class="details-body">'
            f'<div class="table-wrap"><table><thead><tr><th scope="col">{t("avail_col_service")}</th>'
            + "".join(f'<th scope="col">{esc(h)}</th>' for h in heads)
            + f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div>'
            f'<p class="muted">{t("avail_monthly_note")}</p></div></details>')


def bar_tip(c: Content, d: DayStat, lang: str) -> str:
    """Le texte d'une barre : la valeur d'abord, puis l'état, la date et les incidents du jour."""
    date = esc(cap(fmt_date(dt.datetime.combine(d.date, dt.time(12), ZURICH), lang)))
    if d.level == "off":
        if d.reason == "holiday" and d.date in c.holidays:
            what = c.t(lang, "avail_off_holiday_named", name=c.holidays[d.date][lang])
        else:
            what = c.label(lang, "avail_off", d.reason)
        lines = [f'<span class="tip-date">{date}</span>{esc(what)}']
    elif d.percent is None or d.level == "unknown":
        # Sans mesure suffisante ; un incident déclaré peut quand même colorer la barre (jamais caché par le gris).
        lines = [f'<span class="tip-date">{date}</span>{esc(c.label(lang, "avail_level", d.level))} · '
                 f'{esc(c.t(lang, "avail_coverage", measured=d.measured, expected=d.expected))}']
    else:
        lines = [f'<span class="tip-date">{date}</span><strong>{esc(fmt_percent(d.percent, lang, 1))}</strong> · '
                 f'{esc(c.label(lang, "avail_level", d.level))}']
    for e in d.incidents:
        lines.append(f'<span class="tip-inc">{esc(c.t(lang, "avail_incident", title=entry_title(c, e, lang)))}</span>')
    return "".join(lines)


# --------------------------------------------------------------------------------------------- RSS

def render_rss(c: Content, s: State, lang: str) -> str:
    atom = "http://www.w3.org/2005/Atom"
    ET.register_namespace("atom", atom)
    rss = ET.Element("rss", {"version": "2.0"})
    ch = ET.SubElement(rss, "channel")
    page = c.page_url(lang)
    for tag, text in (("title", c.t(lang, "rss_title")), ("link", page), ("description", c.t(lang, "rss_description")),
                      ("language", RSS_LANG[lang]),
                      ("lastBuildDate", format_datetime(s.last_update.astimezone(UTC), usegmt=True)),
                      ("ttl", "15")):
        ET.SubElement(ch, tag).text = text
    ET.SubElement(ch, f"{{{atom}}}link", {"href": f"{page}rss.xml", "rel": "self", "type": "application/rss+xml"})
    cutoff = s.now - dt.timedelta(days=c.history_days)
    items = []
    for e in s.entries:
        for u in e.updates:
            at = parse_time(u["at"])
            if e.active or at >= cutoff:
                items.append((at, e, u))
    items.sort(key=lambda x: x[0], reverse=True)
    for at, e, u in items[:RSS_MAX_ITEMS]:
        item = ET.SubElement(ch, "item")
        ET.SubElement(item, "title").text = f"{entry_title(c, e, lang)} — {status_label(c, e, lang, u)}"
        ET.SubElement(item, "link").text = f"{page}#{e.id}"
        stamp = at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        ET.SubElement(item, "guid", {"isPermaLink": "false"}).text = f"{c.host}:{e.id}:{stamp}"
        ET.SubElement(item, "pubDate").text = format_datetime(at.astimezone(UTC), usegmt=True)
        ET.SubElement(item, "description").text = rss_description(c, e, u, lang)
    ET.indent(rss)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(rss, encoding="unicode") + "\n"


def rss_description(c: Content, e: Entry, u: dict, lang: str) -> str:
    parts = []
    if e.kind == "maintenance":
        parts.append(fmt_window(parse_time(u["start"]), parse_time(u["end"]), lang) + ".")
        parts.append(u["text"][lang])
        parts.append(cap(c.label(lang, "impact", u["impact"])) + ".")
    elif e.kind == "incident":
        parts.append(c.t(lang, "effect_label") + " " + u["effect"][lang])
        parts.append(u["text"][lang])
        if u.get("summary"):
            parts.append(c.t(lang, "summary_label") + " " + u["summary"][lang])
    elif u.get("interruption"):
        parts.append(cap(c.label(lang, "interruption", u["interruption"])) + ".")
    else:
        parts.append(c.label(lang, "release_status", u["status"]) + ".")
    return " ".join(parts)


# --------------------------------------------------------------------------------------------- iCalendar

VTIMEZONE = (
    "BEGIN:VTIMEZONE", "TZID:Europe/Zurich",
    "BEGIN:DAYLIGHT", "TZOFFSETFROM:+0100", "TZOFFSETTO:+0200", "TZNAME:CEST", "DTSTART:19810329T020000",
    "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU", "END:DAYLIGHT",
    "BEGIN:STANDARD", "TZOFFSETFROM:+0200", "TZOFFSETTO:+0100", "TZNAME:CET", "DTSTART:19961027T030000",
    "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU", "END:STANDARD",
    "END:VTIMEZONE",
)


def ics_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r\n", "\\n").replace("\n", "\\n")


def ics_fold(line: str) -> str:
    """Plie une ligne à 75 octets (RFC 5545, 3.1) sans couper un caractère UTF-8."""
    out, current, size = [], "", 0
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > 75:
            out.append(current)
            current, size = " ", 1
        current += ch
        size += n
    out.append(current)
    return "\r\n".join(out)


def ics_utc(d: dt.datetime) -> str:
    return d.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def ics_local(d: dt.datetime) -> str:
    return _local(d).strftime("%Y%m%dT%H%M%S")


def ics_event(c: Content, e: Entry, lang: str) -> list[str]:
    latest = e.latest
    desc = "\n".join((latest["text"][lang], cap(c.label(lang, "impact", latest["impact"])) + ".",
                      c.t(lang, "ics_more"), f"{c.page_url(lang)}#{e.id}"))
    return [
        "BEGIN:VEVENT",
        f"UID:{e.id}@{c.host}",
        f"DTSTAMP:{ics_utc(e.time('at'))}",
        f"CREATED:{ics_utc(e.time('at', e.first))}",
        f"LAST-MODIFIED:{ics_utc(e.time('at'))}",
        f"SEQUENCE:{len(e.updates) - 1}",
        f"DTSTART;TZID=Europe/Zurich:{ics_local(e.time('start'))}",
        f"DTEND;TZID=Europe/Zurich:{ics_local(e.time('end'))}",
        f"SUMMARY:{ics_escape(c.t(lang, 'ics_summary', title=entry_title(c, e, lang)))}",
        f"DESCRIPTION:{ics_escape(desc)}",
        f"URL:{c.page_url(lang)}#{e.id}",
        "STATUS:" + ("CANCELLED" if latest["status"] == "cancelled" else "CONFIRMED"),
        "TRANSP:TRANSPARENT",
        "END:VEVENT",
    ]


def render_ics(c: Content, lang: str, entries: list[Entry], name: str) -> str:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:-//Yanedo//Status page//{lang.upper()}", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", f"X-WR-CALNAME:{ics_escape(name)}", "X-WR-TIMEZONE:Europe/Zurich",
             "REFRESH-INTERVAL;VALUE=DURATION:PT1H", "X-PUBLISHED-TTL:PT1H", *VTIMEZONE]
    for e in entries:
        lines += ics_event(c, e, lang)
    lines.append("END:VCALENDAR")
    return "".join(ics_fold(line) + "\r\n" for line in lines)


# --------------------------------------------------------------------------------------------- construction

def build(root: Path, out: Path, now: dt.datetime, base_url: str | None = None, extra: list[Path] = (),
          require_real_base_url: bool = False, probe_data: Path | None = None) -> State:
    content = Content(root, base_url)
    if require_real_base_url and content.host.endswith(".invalid"):
        raise ContentError([f"base_url {content.base_url!r} est un exemple : le nom de l'hébergeur doit être posé (G9)"])
    entries = load_events(root, content, now, list(extra))
    state = State(content, entries, now)
    state.probe_ignored = 0
    if probe_data is not None and probe_data.is_dir():
        data = load_probe_data(probe_data, content.public_keys, now)
        state.probe_ignored = data.ignored
        if data.slots:
            state.availability = Availability(content, state, data, now, content.holidays)
    cutoff = now - dt.timedelta(days=content.history_days)
    calendar = sorted((e for e in state.entries if e.kind == "maintenance" and (e.active or e.time("end") >= cutoff)),
                      key=lambda e: e.time("start"))
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    shutil.copyfile(root / "assets" / "style.css", out / "style.css")
    (out / ".nojekyll").write_text("", encoding="utf-8")
    files: dict[Path, str] = {}
    for lang in LANGS:
        base = out if lang == "fr" else out / lang
        files[base / "index.html"] = render_html(content, state, lang)
        files[base / "rss.xml"] = render_rss(content, state, lang)
        files[base / "maintenances.ics"] = render_ics(content, lang, calendar, content.t(lang, "ics_calendar_name"))
        for e in calendar:
            files[base / "maintenance" / f"{e.id}.ics"] = render_ics(content, lang, [e], content.t(lang, "ics_calendar_name"))
    problems = []
    for path, text in files.items():
        for label, pattern in OUTPUT_FORBIDDEN:
            if pattern.search(text):
                problems.append(f"{path.relative_to(out)} : la page produite contient une {label}")
    if problems:
        shutil.rmtree(out)
        raise ContentError(problems)
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    return state


# --------------------------------------------------------------------------------------------- publication par git

# Ce qui part chez l'hébergeur : tout est public par nature. Jamais .gitea/, ni les tests.
MANAGED = ("content", "assets", "docs", "statusgen.py", "probe.py", "requirements.txt")


def _git(cwd: Path, *args: str, env: dict | None = None) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        raise HostError([f"git {args[0]} a échoué : {result.stderr.strip()[-400:]}"])
    return result.stdout


def _auth_env(token: str | None) -> dict:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        # L'en-tête passe par l'environnement : le jeton n'apparaît ni dans l'URL ni dans la ligne de commande.
        env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.extraHeader",
                   GIT_CONFIG_VALUE_0=f"Authorization: Basic {basic}")
    return env


def remote_events_missing_here(root: Path, clone: Path) -> list[str]:
    """Événements présents chez l'hébergeur mais absents (ou différents) dans la source : mises à jour faites à la main."""
    missing = []
    remote_dir = clone / "content" / "events"
    if remote_dir.is_dir():
        for f in sorted(remote_dir.iterdir()):
            if f.name.startswith("."):
                continue
            mine = root / "content" / "events" / f.name
            if not mine.is_file() or mine.read_bytes() != f.read_bytes():
                missing.append(f.name)
    return missing


def publish_git(root: Path, remote: str, token: str | None, branch: str, author: str, email: str, source_ref: str,
                workflow: Path | list[Path] | None) -> str:
    """Seule la branche publiée est touchée : la branche probe-data (mesures de la sonde) n'est jamais lue ni écrite."""
    workflows = [] if workflow is None else [workflow] if isinstance(workflow, Path) else list(workflow)
    with tempfile.TemporaryDirectory(prefix="status-publish-") as tmp:
        clone = Path(tmp) / "site"
        env = _auth_env(token)
        clone.mkdir()
        _git(clone, "init", "-q", "-b", branch, env=env)
        _git(clone, "remote", "add", "origin", remote, env=env)
        exists = subprocess.run(["git", "ls-remote", "--exit-code", "--heads", "origin", branch], cwd=clone, env=env,
                                capture_output=True, text=True)
        if exists.returncode == 0:
            _git(clone, "fetch", "-q", "--depth", "1", "origin", branch, env=env)
            _git(clone, "checkout", "-q", "-B", branch, f"origin/{branch}", env=env)
        elif exists.returncode != 2:
            raise HostError([f"dépôt de l'hébergeur injoignable ou jeton refusé : {exists.stderr.strip()[-300:]}"])
        missing = remote_events_missing_here(root, clone)
        if missing:
            raise ManualEditPending([
                "mise à jour faite à la main chez l'hébergeur, pas encore reportée dans yanedo/status : "
                + ", ".join(missing),
                "reporter ces fichiers tels quels dans content/events/ de yanedo/status (procédure « Retour à la normale »)"])
        for child in clone.iterdir():
            if child.name != ".git":
                shutil.rmtree(child) if child.is_dir() else child.unlink()
        for name in MANAGED:
            src = root / name
            if src.is_dir():
                shutil.copytree(src, clone / name, ignore=shutil.ignore_patterns("__pycache__"))
            else:
                shutil.copyfile(src, clone / name)
        if workflows:
            (clone / ".github" / "workflows").mkdir(parents=True)
        for wf in workflows:
            shutil.copyfile(wf, clone / ".github" / "workflows" / wf.name)
        (clone / "README.md").write_text(
            "# Page d'état publique de Yanedo\n\nLa source est tenue ailleurs et publiée ici automatiquement. "
            "Ne modifier ici que `content/events/`, et seulement quand le cluster est arrêté : "
            "voir [docs/PROCEDURE-MANUELLE.md](docs/PROCEDURE-MANUELLE.md).\n", encoding="utf-8")
        _git(clone, "add", "-A", env=env)
        if not _git(clone, "status", "--porcelain", env=env).strip():
            return "rien à publier : l'hébergeur a déjà ce contenu"
        commit_env = dict(env, GIT_AUTHOR_NAME=author, GIT_AUTHOR_EMAIL=email, GIT_COMMITTER_NAME=author,
                          GIT_COMMITTER_EMAIL=email)
        _git(clone, "-c", "commit.gpgsign=false", "commit", "-q", "-m", f"publish {source_ref}", env=commit_env)
        _git(clone, "push", "-q", "origin", f"HEAD:refs/heads/{branch}", env=env)
        return f"publié : {source_ref}"


# --------------------------------------------------------------------------------------------- ligne de commande

def _now(value: str | None) -> dt.datetime:
    return parse_time(value) if value else dt.datetime.now(UTC).replace(microsecond=0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Page d'état publique de Yanedo")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_build = sub.add_parser("build", help="construire la page")
    p_build.add_argument("--out", default="public")
    p_build.add_argument("--now", help="heure de référence RFC 3339 (essais)")
    p_build.add_argument("--base-url")
    p_build.add_argument("--event", action="append", default=[], help="événement en plus (essai à blanc)")
    p_build.add_argument("--require-real-base-url", action="store_true")
    p_build.add_argument("--probe-data", help="dossier des mesures de la sonde (*.jsonl), branche probe-data")
    p_check = sub.add_parser("check-event", help="vérifier des fichiers d'événement")
    p_check.add_argument("files", nargs="+")
    p_check.add_argument("--now")
    p_pub = sub.add_parser("publish-git", help="publier la source chez l'hébergeur (GitHub Pages)")
    p_pub.add_argument("--remote", required=True)
    p_pub.add_argument("--token-env")
    p_pub.add_argument("--branch", default="main")
    p_pub.add_argument("--author-name", default="Yanedo status")
    p_pub.add_argument("--author-email", default="status@noreply.invalid")
    p_pub.add_argument("--source-ref", default="local")
    p_pub.add_argument("--workflow", action="append", default=[],
                       help="chaîne de l'hébergeur à poser dans .github/workflows/ (répétable : pages.yml, probe.yml)")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "build":
            now = _now(args.now)
            state = build(ROOT, Path(args.out), now, args.base_url, [Path(p) for p in args.event],
                          args.require_real_base_url, Path(args.probe_data) if args.probe_data else None)
            print(f"page construite dans {args.out} : bandeau « {state.banner} », {len(state.incidents)} incident(s), "
                  f"{len(state.maintenances)} maintenance(s), {len(state.history)} événement(s) dans l'historique")
            a = state.availability
            if args.probe_data:
                print("disponibilité : " + (f"mesurée, dernière mesure {a.last.isoformat()}" if a and a.last
                                            else "aucune mesure lisible, barres non affichées")
                      + f", {state.probe_ignored} ligne(s) de mesure ignorée(s)")
            for warning in calendar_warnings(ROOT, now):
                print(f"AVERTISSEMENT : {warning}", file=sys.stderr)
        elif args.cmd == "check-event":
            keys, now, bad = read_service_keys(ROOT), _now(args.now), 0
            for f in args.files:
                try:
                    ev = load_file(Path(f))
                except Exception as exc:  # noqa: BLE001 — fichier illisible : contenu refusé
                    ev = None
                    print(f"{f} : illisible : {exc}", file=sys.stderr)
                errs = validate_event(ev, keys, now) if ev is not None else ["illisible"]
                for e in errs:
                    print(f"{f} : {e}", file=sys.stderr)
                bad += bool(errs)
                if not errs:
                    print(f"{f} : conforme au schéma {SCHEMA}")
            return 2 if bad else 0
        elif args.cmd == "publish-git":
            token = os.environ.get(args.token_env, "") if args.token_env else None
            if args.token_env and not token:
                raise HostError([f"jeton absent : variable {args.token_env} vide"])
            print(publish_git(ROOT, args.remote, token, args.branch, args.author_name, args.author_email,
                              args.source_ref, [Path(w) for w in args.workflow]))
    except ImportError as exc:
        print(f"ERREUR : {exc} : python3 -m pip install --require-hashes -r requirements.txt", file=sys.stderr)
        return 1
    except (ContentError, HostError) as exc:
        for problem in exc.args[0]:
            print(f"REFUS : {problem}", file=sys.stderr)
        if isinstance(exc, ManualEditPending):
            return 4
        return 3 if isinstance(exc, HostError) else 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
