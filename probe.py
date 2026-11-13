#!/usr/bin/env python3
"""Sonde extérieure de la page d'état de Yanedo : les mesures des barres de disponibilité sur 90 jours.

Elle tourne chez l'hébergeur de la page (chaîne « probe » de GitHub Actions), hors du cluster et hors du réseau de
Yanedo : une sonde dans le cluster serait muette justement quand le cluster est en panne (note T32 v2.0, § 7).
Bibliothèque standard seulement : « python3 -I probe.py ».

Commandes :
  measure  sonde chaque service une fois (un échec est refait 20 s plus tard) et écrit UNE ligne de mesure
  record   range cette ligne dans le fichier du jour, sur la branche probe-data du dépôt public de la page

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
from urllib.parse import urlsplit

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

Branche écrite par la chaîne `probe` : une ligne JSON par passage de la sonde (toutes les 5 minutes, du lundi au
vendredi, pendant les heures du contrat et autour), dans `probe/AAAA-MM-JJ.jsonl` (date UTC). Format
`yanedo-status-probe/1` : `at` (UTC), `witness` (le réseau de la sonde fonctionnait), `results` (clé du service →
`up` ou `down`), `reasons` (raison générique d'un `down`). La chaîne `pages` les lit pour les barres de
disponibilité. Ne pas modifier à la main.
"""


def record(line: dict, remote: str, branch: str, token: str | None, author: str = "Yanedo status probe",
           email: str = "status@noreply.invalid", attempts: int = 4) -> str:
    """Ajoute la ligne au fichier du jour, sur sa branche ; refait tout si une autre passe a poussé entre-temps."""
    if line.get("schema") != PROBE_SCHEMA or not re.match(r"^\d{4}-\d{2}-\d{2}T", str(line.get("at", ""))):
        raise TargetsError(["ligne de mesure invalide"])
    text = json.dumps(line, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    env = _auth_env(token)
    commit_env = dict(env, GIT_AUTHOR_NAME=author, GIT_AUTHOR_EMAIL=email, GIT_COMMITTER_NAME=author,
                      GIT_COMMITTER_EMAIL=email)
    name = f"probe/{line['at'][:10]}.jsonl"
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
            if text in current.splitlines():
                return f"déjà rangée : {name}"
            path.write_text(current + text + "\n", encoding="utf-8")
            _git(clone, "add", "-A", env=env)
            _git(clone, "-c", "commit.gpgsign=false", "commit", "-q", "-m", f"probe {line['at']}", env=commit_env)
            pushed = _git(clone, "push", "-q", "origin", f"HEAD:refs/heads/{branch}", env=env, check=False)
            if pushed.returncode == 0:
                return f"rangée : {name}"
        time.sleep(2 + 3 * attempt + random.random())
    raise HostError(f"poussée refusée {attempts} fois de suite sur {branch}")


# --------------------------------------------------------------------------------------------- ligne de commande

def _github_output(values: dict[str, str]) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            for key, value in values.items():
                fh.write(f"{key}={value}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sonde extérieure de la page d'état de Yanedo")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_measure = sub.add_parser("measure", help="sonder les services et écrire une ligne de mesure")
    src = p_measure.add_mutually_exclusive_group(required=True)
    src.add_argument("--targets-env", help="variable d'environnement qui contient les cibles (secret de l'hébergeur)")
    src.add_argument("--targets-file", help="fichier des cibles (essai à blanc sur un poste)")
    p_measure.add_argument("--out", help="fichier où écrire la ligne")
    p_record = sub.add_parser("record", help="ranger une ligne de mesure sur la branche des mesures")
    p_record.add_argument("--line-env", required=True, help="variable d'environnement qui contient la ligne")
    p_record.add_argument("--remote", required=True)
    p_record.add_argument("--branch", default="probe-data")
    p_record.add_argument("--token-env")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "measure":
            raw = os.environ.get(args.targets_env, "") if args.targets_env else Path(args.targets_file).read_text("utf-8")
            if not raw.strip():
                raise TargetsError([f"cibles absentes : {args.targets_env or args.targets_file} est vide "
                                    "(secret PROBE_TARGETS non posé ?)"])
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
            line = measure(targets, witnesses=WITNESSES, retry_delay=RETRY_DELAY)
            text = json.dumps(line, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if args.out:
                Path(args.out).write_text(text + "\n", encoding="utf-8")
            if not line["witness"]:
                print("témoin muet : le réseau de la sonde ne répond pas ; mesure sans valeur")
            for key, value in line["results"].items():
                print(f"{key} : {value}" + (f" ({line['reasons'][key]})" if key in line["reasons"] else ""))
            all_down = bool(line["results"]) and all(v == "down" for v in line["results"].values())
            _github_output({"line": text, "all_down": "true" if all_down else "false"})
        else:
            raw = os.environ.get(args.line_env, "")
            try:
                line = json.loads(raw)
            except ValueError:
                raise TargetsError(["ligne de mesure absente ou illisible"]) from None
            token = os.environ.get(args.token_env, "") if args.token_env else None
            if args.token_env and not token:
                raise HostError(f"jeton absent : variable {args.token_env} vide")
            print(record(line, args.remote, args.branch, token))
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
