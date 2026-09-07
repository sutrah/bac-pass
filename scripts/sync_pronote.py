#!/usr/bin/env python3
"""Synchronise les moyennes Pronote de Loïse dans dashboard.html.

Usage :
    PRONOTE_URL=https://XXXX.index-education.net/pronote/parent.html \
    PRONOTE_USERNAME=... PRONOTE_PASSWORD=... \
    python3 scripts/sync_pronote.py

Ne modifie que les matières qui déclarent un `pronote:'...'` dans le tableau
SUBJECTS de dashboard.html, et seulement selon leur `mode` :
  - "cc"     (contrôle continu) : écrit dans `note2` (la moyenne de Terminale
    en cours). `note1`, la moyenne officielle de 1ère venant du relevé du
    Rectorat, n'est jamais touchée -- les deux se combinent à l'affichage.
  - "final"  (spécialité conservée, Philosophie) : écrit dans `note`, à titre
    indicatif seulement (cette moyenne de bulletin ne compte pas pour le bac,
    seul l'examen final de Terminale compte).
  - "locked" (épreuves anticipées, spécialité abandonnée) : jamais touché,
    quel que soit le libellé Pronote -- ces notes viennent du relevé officiel
    du Rectorat et sont figées pour toujours.

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
        mode_match = re.search(r"mode:\s*'([^']+)'", line)
        pronote_match = re.search(r"pronote:\s*'([^']+)'", line)
        if not mode_match or not pronote_match:
            return line
        mode = mode_match.group(1)
        if mode == "locked":
            return line  # figé pour toujours -- vient du relevé officiel, jamais de Pronote
        label = normalize(pronote_match.group(1))
        if label not in averages:
            return line
        key = re.search(r"key:\s*'([^']+)'", line).group(1)
        new_note = round(averages[label], 2)
        field = "note2" if mode == "cc" else "note"
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
    return updated


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
