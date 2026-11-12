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

    rows = []
    for svc in c.services:
        st = s.services[svc["key"]]
        rows.append(f'<li><span>{esc(svc["name"][lang])}</span>{pill(c.label(lang, "service_status", st), st)}</li>')

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
<div class="grid">
<div class="stack">{''.join(cards)}</div>
<section class="card" aria-labelledby="services-title"><h2 id="services-title">{t('services_title')}</h2>
<ul class="services">{''.join(rows)}</ul></section>
</div>
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
          require_real_base_url: bool = False) -> State:
    content = Content(root, base_url)
    if require_real_base_url and content.host.endswith(".invalid"):
        raise ContentError([f"base_url {content.base_url!r} est un exemple : le nom de l'hébergeur doit être posé (G9)"])
    entries = load_events(root, content, now, list(extra))
    state = State(content, entries, now)
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
MANAGED = ("content", "assets", "docs", "statusgen.py", "requirements.txt")


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
                workflow: Path | None) -> str:
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
        if workflow:
            (clone / ".github" / "workflows").mkdir(parents=True)
            shutil.copyfile(workflow, clone / ".github" / "workflows" / workflow.name)
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
    p_pub.add_argument("--workflow", help="chaîne de construction de l'hébergeur à poser dans .github/workflows/")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "build":
            state = build(ROOT, Path(args.out), _now(args.now), args.base_url, [Path(p) for p in args.event],
                          args.require_real_base_url)
            print(f"page construite dans {args.out} : bandeau « {state.banner} », {len(state.incidents)} incident(s), "
                  f"{len(state.maintenances)} maintenance(s), {len(state.history)} événement(s) dans l'historique")
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
                              args.source_ref, Path(args.workflow) if args.workflow else None))
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
