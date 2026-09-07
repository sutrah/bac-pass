#!/usr/bin/env python3
"""Synchronise les moyennes Pronote de Loïse dans dashboard.html.

Usage :
    PRONOTE_URL=https://XXXX.index-education.net/pronote/parent.html \
    PRONOTE_USERNAME=... PRONOTE_PASSWORD=... \
    python3 scripts/sync_pronote.py

Ne modifie que le champ `note:` des matières qui déclarent un `pronote:'...'`
dans le tableau SUBJECTS de dashboard.html -- c'est-à-dire les matières de
contrôle continu ("mode:'cc'") et les spécialités conservées ("mode:'final'",
mise à jour à titre indicatif uniquement, ces notes de bulletin ne comptant
pas pour le bac). Les matières "mode:'locked'" (épreuves anticipées,
spécialité abandonnée) ne sont jamais touchées : elles viennent du relevé
officiel du Rectorat, pas de Pronote.

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


def fetch_averages(client: "pronotepy.Client") -> dict:
    """{libellé de matière normalisé: moyenne (float)} pour la période Pronote en cours."""
    averages = {}
    for avg in client.current_period.averages:
        try:
            value = float(str(avg.student).replace(",", "."))
        except (TypeError, ValueError):
            continue
        averages[normalize(avg.subject.name)] = value
    return averages


def sync_dashboard(averages: dict) -> list[str]:
    html = DASHBOARD.read_text(encoding="utf-8")
    updated = []

    def replace_line(match: "re.Match") -> str:
        line = match.group(0)
        pronote_match = re.search(r"pronote:\s*'([^']+)'", line)
        if not pronote_match:
            return line
        label = normalize(pronote_match.group(1))
        if label not in averages:
            return line
        key = re.search(r"key:\s*'([^']+)'", line).group(1)
        new_note = round(averages[label], 2)
        new_line, count = re.subn(
            r"note:\s*(null|-?\d+(?:\.\d+)?)", f"note:{new_note}", line, count=1
        )
        if count and new_line != line:
            updated.append(f"{key} -> {new_note}/20")
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
    return updated


def main() -> int:
    try:
        url = os.environ["PRONOTE_URL"]
        username = os.environ["PRONOTE_USERNAME"]
        password = os.environ["PRONOTE_PASSWORD"]
    except KeyError as exc:
        print(f"Variable d'environnement manquante : {exc}", file=sys.stderr)
        return 1

    client = pronotepy.Client(url, username=username, password=password)
    if not client.logged_in:
        print("Échec de connexion à Pronote.", file=sys.stderr)
        return 1

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
