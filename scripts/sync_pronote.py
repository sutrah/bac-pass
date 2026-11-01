#!/usr/bin/env python3
"""Synchronise les moyennes Pronote de Loïse dans dashboard.html.

Usage :
    PRONOTE_URL=https://XXXX.index-education.net/pronote/parent.html \
    PRONOTE_USERNAME=... PRONOTE_PASSWORD=... \
    python3 scripts/sync_pronote.py

Ne modifie que les matières qui déclarent un `pronote:'...'` dans le tableau
SUBJECTS de dashboard.html, et seulement selon leur `mode` :
  - "cc"     (contrôle continu) : écrit dans `note2` (la moyenne annuelle de
    Terminale en cours). `note1`, la moyenne officielle de 1ère venant du relevé du
    Rectorat, n'est jamais touchée -- les deux se combinent à l'affichage.
  - "term"   (contrôle continu de Terminale seulement : EPS, option suivie
    uniquement en Terminale) : écrit dans `note`, qui compte avec le coefficient plein.
  - "final"  (spécialité conservée, Philosophie) : écrit dans `note`, à titre
    indicatif seulement (cette moyenne de bulletin ne compte pas pour le bac,
    seul l'examen final de Terminale compte).
  - "locked" (épreuves anticipées, spécialité abandonnée) : jamais touché,
    quel que soit le libellé Pronote -- ces notes viennent du relevé officiel
    du Rectorat et sont figées pour toujours.

La moyenne écrite est la moyenne ANNUELLE de Terminale : moyenne des périodes
(trimestres ou semestres) déjà notées, pas seulement la période en cours. Sinon,
au passage au trimestre 2, la note sauterait sur la seule moyenne du T2.

Avertissement : ce script s'appuie sur pronotepy (bibliothèque communautaire,
non officielle -- Pronote n'a pas d'API publique). L'attribut exact des
moyennes par matière (`period.averages`, `average.subject.name`,
`average.student`) correspond à l'usage courant de la bibliothèque au moment
de l'écriture de ce script, mais n'a pas pu être vérifié contre un compte
Pronote réel. Faites un premier lancement supervisé (`workflow_dispatch`
manuel sur GitHub Actions, puis vérifier le dashboard) avant de compter
dessus en automatique.
"""
import os
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import pronotepy

DASHBOARD = Path(__file__).resolve().parent.parent / "dashboard.html"


def normalize(label: str) -> str:
    """Enlève accents/ponctuation pour comparer les libellés de matières sans surprise."""
    label = unicodedata.normalize("NFKD", label).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Z0-9]", "", label.upper())


def _period_averages(period) -> dict:
    """{libellé normalisé: (libellé brut, moyenne)} pour une période Pronote."""
    out = {}
    for avg in period.averages:
        try:
            value = float(str(avg.student).replace(",", "."))
        except (TypeError, ValueError):
            continue  # "Abs", "Disp", "NonNote"...
        out[normalize(avg.subject.name)] = (avg.subject.name, value)
    return out


def fetch_averages(client: "pronotepy.Client") -> dict:
    """{libellé de matière normalisé: moyenne annuelle (float)}.

    Moyenne simple des moyennes de chaque période déjà notée, dans le même découpage
    que la période en cours (trimestres OU semestres -- jamais les deux mélangés, ni
    une éventuelle période "Année"). Repli sur la seule période en cours si le
    découpage n'est pas reconnu.
    """
    current = client.current_period
    kind = next((k for k in ("TRIMESTRE", "SEMESTRE") if k in normalize(current.name)), None)
    periods = [p for p in client.periods if kind and kind in normalize(p.name)] or [current]

    values: dict[str, list[float]] = {}
    raw_names: dict[str, str] = {}
    for period in periods:
        try:
            per = _period_averages(period)
        except Exception as exc:  # noqa: BLE001 -- une période future peut ne rien renvoyer
            print(f"Période ignorée ({period.name}) : {type(exc).__name__}: {exc}")
            continue
        for key, (raw, value) in per.items():
            values.setdefault(key, []).append(value)
            raw_names[key] = raw
    print("Périodes prises en compte : " + ", ".join(p.name for p in periods))
    fetch_averages.raw_names = raw_names  # pour le diagnostic des libellés non reconnus
    return {k: sum(v) / len(v) for k, v in values.items()}


def sync_dashboard(averages: dict) -> list[str]:
    html = DASHBOARD.read_text(encoding="utf-8")
    updated = []
    matched = set()

    def replace_line(match: "re.Match") -> str:
        line = match.group(0)
        mode_match = re.search(r"mode:\s*'([^']+)'", line)
        pronote_match = re.search(r"pronote:\s*'([^']+)'", line)
        if not mode_match or not pronote_match:
            return line
        mode = mode_match.group(1)
        label = normalize(pronote_match.group(1))
        matched.add(label)
        if mode == "locked":
            return line  # figé pour toujours -- vient du relevé officiel, jamais de Pronote
        if label not in averages:
            return line
        key = re.search(r"key:\s*'([^']+)'", line).group(1)
        new_note = round(averages[label], 2)
        field = "note2" if mode == "cc" else "note"  # "term" et "final" -> note
        new_line, count = re.subn(
            rf"{field}:\s*(null|-?\d+(?:\.\d+)?)", f"{field}:{new_note}", line, count=1
        )
        if count and new_line != line:
            updated.append(f"{key} -> {field}={new_note}/20")
            return new_line
        return line

    html = re.sub(r"^\s*\{ key:.*\},?\s*$", replace_line, html, flags=re.MULTILINE)
    html = re.sub(
        r"const LAST_SYNC = .*;",
        f'const LAST_SYNC = "{datetime.now(timezone.utc).astimezone().isoformat()}";',
        html,
        count=1,
    )
    DASHBOARD.write_text(html, encoding="utf-8")

    # Diagnostic : les matières Pronote qui ne correspondent à aucune ligne du dashboard.
    # Si une matière attendue (LV1, LV2...) reste vide, recopier ici son libellé exact
    # dans le champ `pronote:'...'` de dashboard.html.
    raw = getattr(fetch_averages, "raw_names", {})
    unmatched = sorted(raw[k] for k in averages if k not in matched and k in raw)
    if unmatched:
        print("Libellés Pronote sans correspondance dans le dashboard :")
        for name in unmatched:
            print(f"  - {name!r}")
    return updated


def print_diagnostic(client: "pronotepy.Client") -> None:
    """Liste les périodes et les matières de l'année en cours (noms seulement, pas de notes,
    car les logs Actions d'un dépôt public sont publics). Pronote n'expose que l'année
    scolaire en cours : les moyennes de 1ère n'y sont plus accessibles."""
    from datetime import date, timedelta

    print("=== Diagnostic ===")
    print("Périodes : " + ", ".join(p.name for p in client.periods))
    subjects = set()
    for period in client.periods:
        try:
            subjects.update(a.subject.name for a in period.averages)
        except Exception:  # noqa: BLE001
            pass
    try:
        today = date.today()
        lessons = client.lessons(today, today + timedelta(days=21))
        subjects.update(l.subject.name for l in lessons if l.subject)
    except Exception as exc:  # noqa: BLE001
        print(f"Emploi du temps illisible ({type(exc).__name__}: {exc})")
    print("Matières de l'année (bulletins + emploi du temps sur 3 semaines) :")
    for name in sorted(subjects):
        print(f"  - {name}")
    # Ce que chaque période expose (compteurs et champs remplis -- jamais de valeurs).
    for period in client.periods:
        row = []
        for attr in ("grades", "averages", "evaluations", "absences", "delays", "punishments"):
            try:
                items = getattr(period, attr)
                extra = ""
                if attr == "grades" and items:
                    g = items[0]
                    filled = [f for f in ("coefficient", "average", "min", "max", "out_of", "comment", "date")
                              if getattr(g, f, None) not in (None, "")]
                    extra = "{" + ",".join(filled) + "}"
                if attr == "averages" and items:
                    a = items[0]
                    filled = [f for f in ("student", "class_average", "min", "max", "out_of")
                              if getattr(a, f, None) not in (None, "")]
                    extra = "{" + ",".join(filled) + "}"
                row.append(f"{attr}={len(items)}{extra}")
            except Exception as exc:  # noqa: BLE001
                row.append(f"{attr}=ERR({type(exc).__name__})")
        for attr in ("overall_average", "class_overall_average"):
            try:
                row.append(f"{attr}={'oui' if getattr(period, attr) not in (None, '') else 'vide'}")
            except Exception as exc:  # noqa: BLE001
                row.append(f"{attr}=ERR({type(exc).__name__})")
        try:
            rep = period.report
            row.append(f"report={'oui' if rep and (rep.subjects or rep.comments) else 'vide'}")
        except Exception as exc:  # noqa: BLE001
            row.append(f"report=ERR({type(exc).__name__})")
        print(f"[{period.name}] " + " ".join(row))
    try:
        today = date.today()
        hw = client.homework(today, today + timedelta(days=14))
        print(f"Devoirs à faire (14 j) : {len(hw)}")
        lessons = client.lessons(today, today + timedelta(days=21))
        print(f"Cours (21 j) : {len(lessons)}, dont contrôles (test=True) : {sum(1 for l in lessons if l.test)}, "
              f"annulés : {sum(1 for l in lessons if l.canceled)}")
    except Exception as exc:  # noqa: BLE001
        print(f"Devoirs/cours illisibles ({type(exc).__name__}: {exc})")
    print("=== Fin du diagnostic ===")


def main() -> int:
    try:
        url = os.environ["PRONOTE_URL"]
        username = os.environ["PRONOTE_USERNAME"]
        password = os.environ["PRONOTE_PASSWORD"]
    except KeyError as exc:
        print(f"Variable d'environnement manquante : {exc}", file=sys.stderr)
        return 1

    # Compte parent (parent.html) : il faut ParentClient, pas Client, et sélectionner
    # explicitement l'enfant avant de pouvoir lire ses notes -- sinon pronotepy lève un
    # KeyError('listeOngletsPourPeriodes') en essayant de lire une période inexistante
    # à ce niveau.
    is_parent = url.rstrip("/").endswith("parent.html")
    client_cls = pronotepy.ParentClient if is_parent else pronotepy.Client

    try:
        client = client_cls(url, username=username, password=password)
    except Exception as exc:  # noqa: BLE001 -- on veut le diagnostic complet dans les logs CI
        print(f"Erreur pendant la connexion à Pronote ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 1

    if not client.logged_in:
        print(
            "Échec de connexion à Pronote : identifiants refusés par le serveur, ou "
            "PRONOTE_URL ne pointe pas sur la bonne page de connexion "
            "(vérifier qu'elle se termine bien par eleve.html ou parent.html, "
            "copiée depuis la barre d'adresse du navigateur sur la page de login Pronote).",
            file=sys.stderr,
        )
        return 1

    if is_parent:
        children = client.children
        if not children:
            print("Compte parent connecté, mais aucun enfant trouvé.", file=sys.stderr)
            return 1
        child_name = os.environ.get("PRONOTE_CHILD_NAME", "").strip().lower()
        chosen = children[0]
        if child_name:
            for c in children:
                if child_name in c.name.lower():
                    chosen = c
                    break
        client.set_child(chosen)
        print(f"Enfant sélectionné : {chosen.name}")

    if os.environ.get("PRONOTE_DIAG") == "true":
        print_diagnostic(client)

    averages = fetch_averages(client)
    if not averages:
        print("Aucune moyenne récupérée depuis Pronote -- rien à mettre à jour.")
        return 0

    updated = sync_dashboard(averages)
    if updated:
        print("Matières mises à jour :")
        for line in updated:
            print(f"  - {line}")
    else:
        print("Aucun changement (moyennes identiques ou aucune correspondance de libellé).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
