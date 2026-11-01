#!/usr/bin/env python3
"""Synchronise les données Pronote de Loïse pour le dashboard du bac.

Usage :
    PRONOTE_URL=https://XXXX.index-education.net/pronote/parent.html \
    PRONOTE_USERNAME=... PRONOTE_PASSWORD=... [PRONOTE_CHILD_NAME=...] \
    python3 scripts/sync_pronote.py

Deux sorties :

1. `pronote.json` -- l'état des lieux complet de l'année en cours, lu par le
   dashboard au chargement (et relu toutes les 10 minutes) :
   moyennes par matière et par période (élève, classe, min, max), moyenne
   générale, dernières notes, bac blanc, devoirs et contrôles à venir, vie scolaire.
   Le fichier n'est réécrit que si les données ont changé (pas de commit à vide).

2. `dashboard.html` -- les valeurs de repli inscrites dans le tableau SUBJECTS
   (utilisées si pronote.json est absent, par ex. en ouvrant le fichier en local).
   Seules les matières qui déclarent un `pronote:'...'` sont touchées, selon leur mode :
     - "cc"     : `note2` (moyenne annuelle de Terminale, provisoire).
     - "term"   : `note`  (contrôle continu de Terminale seulement : EPS, Théâtre).
     - "final"  : `note`  (indicatif : seul l'examen final compte).
     - "locked" : jamais touché.

COFFRE : les moyennes de 1ère (`note1`) sont les moyennes officielles, confirmées.
Le script ne les écrit jamais, et s'arrête en erreur si une modification de
dashboard.html les changeait par accident.

Moyenne annuelle = moyenne des périodes (trimestres OU semestres, selon le
découpage de la période en cours) déjà notées.

Les logs GitHub Actions d'un dépôt public sont publics : le script n'y écrit
jamais de note, seulement des noms de matières et des compteurs.
"""
import json
import os
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pronotepy

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = ROOT / "dashboard.html"
DATA = ROOT / "pronote.json"
RECENT_GRADES = 12
HORIZON_DAYS = 14


# ----------------------------------------------------------------------------- utilitaires

def normalize(label: str) -> str:
    """Enlève accents/ponctuation pour comparer les libellés sans surprise (même règle côté JS)."""
    label = unicodedata.normalize("NFKD", label).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Z0-9]", "", label.upper())


def num(value):
    """'15,5' -> 15.5 ; 'Abs', 'Disp', '', None -> None."""
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def r2(value):
    return None if value is None else round(value, 2)


def safe(fn, default):
    """Une rubrique Pronote illisible ne doit pas faire tomber tout le sync."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        print(f"  (rubrique ignorée : {type(exc).__name__}: {exc})")
        return default


# ----------------------------------------------------------------------------- collecte

def school_periods(client):
    """Les périodes du découpage en cours (trimestres ou semestres), dans l'ordre."""
    current = client.current_period
    kind = next((k for k in ("TRIMESTRE", "SEMESTRE") if k in normalize(current.name)), None)
    periods = [p for p in client.periods if kind and kind in normalize(p.name)] or [current]
    return current, kind, periods


def collect(client) -> dict:
    current, kind, periods = school_periods(client)
    today = date.today()

    matieres: dict[str, dict] = {}
    grades_all = []
    for period in periods:
        for avg in safe(lambda: period.averages, []):
            entry = matieres.setdefault(avg.subject.name, {"periodes": {}, "nb_notes": 0})
            entry["periodes"][period.name] = {
                "eleve": r2(num(avg.student)),
                "classe": r2(num(avg.class_average)),
                "min": r2(num(avg.min)),
                "max": r2(num(avg.max)),
            }
        for g in safe(lambda: period.grades, []):
            grades_all.append(g)
            matieres.setdefault(g.subject.name, {"periodes": {}, "nb_notes": 0})["nb_notes"] += 1

    for entry in matieres.values():
        vals = [p["eleve"] for p in entry["periodes"].values() if p["eleve"] is not None]
        entry["annuelle"] = r2(sum(vals) / len(vals)) if vals else None

    def grade_row(g):
        out_of = num(g.out_of) or 20
        return {
            "date": g.date.isoformat() if g.date else None,
            "matiere": g.subject.name,
            "note": num(g.grade),          # None si Abs / Disp / Non noté
            "brut": None if num(g.grade) is not None else str(g.grade),
            "sur": out_of,
            "coeff": num(g.coefficient),
            "classe": num(g.average),
            "min": num(g.min),
            "max": num(g.max),
        }

    grades_all.sort(key=lambda g: (g.date or date.min), reverse=True)
    notes_recentes = [grade_row(g) for g in grades_all[:RECENT_GRADES]]

    # Moyenne générale de chaque période du découpage
    generale = {}
    for period in periods:
        eleve = num(safe(lambda: period.overall_average, None))
        if eleve is not None:
            generale[period.name] = {"eleve": r2(eleve),
                                     "classe": r2(num(safe(lambda: period.class_overall_average, None)))}

    # Bac blanc : la meilleure indication pour les épreuves finales (spécialités, philo)
    bac_blanc = {}
    for period in client.periods:
        if "BACBLANC" not in normalize(period.name):
            continue
        for avg in safe(lambda: period.averages, []):
            if num(avg.student) is not None:
                bac_blanc[avg.subject.name] = {"eleve": r2(num(avg.student)), "classe": r2(num(avg.class_average))}
        for g in safe(lambda: period.grades, []):
            if num(g.grade) is not None and g.subject.name not in bac_blanc:
                out_of = num(g.out_of) or 20
                bac_blanc[g.subject.name] = {"eleve": r2(num(g.grade) * 20 / out_of),
                                             "classe": r2((num(g.average) or 0) * 20 / out_of) if num(g.average) else None}

    devoirs = [
        {"date": h.date.isoformat(), "matiere": h.subject.name,
         "texte": re.sub(r"\s+", " ", h.description or "").strip()[:200], "fait": bool(h.done)}
        for h in safe(lambda: client.homework(today, today + timedelta(days=HORIZON_DAYS)), [])
    ]
    devoirs.sort(key=lambda d: (d["date"], d["matiere"]))

    lessons = safe(lambda: client.lessons(today, today + timedelta(days=HORIZON_DAYS)), [])
    # Matières réellement suivies cette année : emploi du temps sur 5 semaines + toutes les notes.
    # C'est ce qui confirme les spécialités, les langues et les options qui comptent au bac.
    wide = safe(lambda: client.lessons(today - timedelta(days=14), today + timedelta(days=21)), [])
    suivies = {l.subject.name for l in wide if l.subject} | set(matieres)
    controles = sorted(
        ({"date": l.start.isoformat(timespec="minutes"), "matiere": l.subject.name if l.subject else "?"}
         for l in lessons if getattr(l, "test", False) and not l.canceled),
        key=lambda c: c["date"])
    annules = sorted(
        ({"date": l.start.isoformat(timespec="minutes"), "matiere": l.subject.name if l.subject else "?"}
         for l in lessons if l.canceled),
        key=lambda c: c["date"])

    absences = safe(lambda: current.absences, [])
    delays = safe(lambda: current.delays, [])
    vie_scolaire = {
        "absences": len(absences),
        "absences_non_justifiees": sum(1 for a in absences if not a.justified),
        "heures_absence": safe(lambda: sum(float(str(a.hours or "0").split("h")[0] or 0) for a in absences), None),
        "retards": len(delays),
    }

    return {
        "decoupage": (kind or "PERIODE").capitalize(),
        "periode_en_cours": current.name,
        "periodes": [p.name for p in periods],
        "matieres": dict(sorted(matieres.items())),
        "matieres_suivies": sorted(suivies),
        "moyenne_generale": generale,
        "notes_recentes": notes_recentes,
        "bac_blanc": bac_blanc,
        "devoirs": devoirs,
        "controles": controles,
        "cours_annules": annules,
        "vie_scolaire": vie_scolaire,
    }


# ----------------------------------------------------------------------------- écriture

def write_json(data: dict) -> bool:
    """Écrit pronote.json seulement si les données (hors horodatage) ont changé."""
    previous = {}
    if DATA.exists():
        try:
            previous = json.loads(DATA.read_text(encoding="utf-8"))
        except ValueError:
            pass
    previous.pop("mis_a_jour", None)
    if previous == data:
        return False
    out = {"mis_a_jour": datetime.now(timezone.utc).isoformat(timespec="seconds"), **data}
    DATA.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return True


def premiere_values(html: str) -> list[str]:
    return re.findall(r"key:\s*'([^']+)'[^\n]*?note1:\s*([^,\s]+)", html)


def sync_dashboard(annual: dict) -> list[str]:
    """Met à jour les valeurs de repli de SUBJECTS. `annual` : {libellé normalisé: moyenne}."""
    html = DASHBOARD.read_text(encoding="utf-8")
    coffre_avant = premiere_values(html)
    updated = []

    def replace_line(match):
        line = match.group(0)
        mode_match = re.search(r"mode:\s*'([^']+)'", line)
        pronote_match = re.search(r"pronote:\s*'([^']+)'", line)
        if not mode_match or not pronote_match or mode_match.group(1) == "locked":
            return line
        label = normalize(pronote_match.group(1))
        if annual.get(label) is None:
            return line
        key = re.search(r"key:\s*'([^']+)'", line).group(1)
        field = "note2" if mode_match.group(1) == "cc" else "note"
        new_line, count = re.subn(rf"\b{field}:\s*(null|-?\d+(?:\.\d+)?)", f"{field}:{annual[label]}", line, count=1)
        if count and new_line != line:
            updated.append(f"{key} -> {field}")
            return new_line
        return line

    new_html = re.sub(r"^\s*\{ key:.*\},?\s*$", replace_line, html, flags=re.MULTILINE)
    if premiere_values(new_html) != coffre_avant:
        raise SystemExit("ARRÊT : une moyenne de 1ère (note1) aurait été modifiée. Rien n'est écrit.")
    if new_html != html:
        DASHBOARD.write_text(new_html, encoding="utf-8")
    return updated


def report_labels(data: dict) -> None:
    html = DASHBOARD.read_text(encoding="utf-8")
    known = {normalize(l) for l in re.findall(r"pronote:\s*'([^']+)'", html)}
    unmatched = [name for name in data["matieres"] if normalize(name) not in known]
    if unmatched:
        print("Matières Pronote sans ligne dans le dashboard : " + ", ".join(unmatched))


# ----------------------------------------------------------------------------- connexion

def connect():
    try:
        url = os.environ["PRONOTE_URL"]
        username = os.environ["PRONOTE_USERNAME"]
        password = os.environ["PRONOTE_PASSWORD"]
    except KeyError as exc:
        raise SystemExit(f"Variable d'environnement manquante : {exc}")

    # Compte parent (parent.html) : ParentClient + sélection explicite de l'enfant.
    is_parent = url.rstrip("/").endswith("parent.html")
    client_cls = pronotepy.ParentClient if is_parent else pronotepy.Client
    try:
        client = client_cls(url, username=username, password=password)
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"Erreur pendant la connexion à Pronote ({type(exc).__name__}): {exc}")
    if not client.logged_in:
        raise SystemExit("Échec de connexion à Pronote : identifiants refusés, ou PRONOTE_URL ne se "
                         "termine pas par eleve.html / parent.html.")

    if is_parent:
        if not client.children:
            raise SystemExit("Compte parent connecté, mais aucun enfant trouvé.")
        wanted = os.environ.get("PRONOTE_CHILD_NAME", "").strip().lower()
        chosen = next((c for c in client.children if wanted and wanted in c.name.lower()), client.children[0])
        client.set_child(chosen)
        print(f"Enfant sélectionné : {chosen.name}")
    return client


def main() -> int:
    client = connect()
    data = collect(client)
    print(f"Découpage : {data['decoupage']} ({', '.join(data['periodes'])}) -- en cours : {data['periode_en_cours']}")
    print(f"Matières notées : {len(data['matieres'])}, notes récentes : {len(data['notes_recentes'])}, "
          f"bac blanc : {len(data['bac_blanc'])}, devoirs : {len(data['devoirs'])}, contrôles : {len(data['controles'])}")
    report_labels(data)

    annual = {normalize(name): m["annuelle"] for name, m in data["matieres"].items()}
    updated = sync_dashboard(annual)
    changed = write_json(data)
    print("pronote.json mis à jour." if changed else "pronote.json : aucun changement.")
    if updated:
        print("dashboard.html (valeurs de repli) : " + ", ".join(updated))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
