#!/usr/bin/env python3
# SPDX-License-Identifier: EUPL-1.2
"""Mesure de performance du catalogue sur un jeu synthétique reproductible (100 000 notices par défaut).

La base est jetable, créée dans un dossier temporaire par les migrations. Les données sont inventées à partir d'une
graine fixe : deux exécutions produisent le même catalogue. Les mesures passent par le vrai code et par de vraies
requêtes HTTP. Les objectifs sont des cibles : un dépassement est publié, jamais masqué.

Usage : python scripts/perf_catalogue.py [--notices 100000] [--graine 20261108] [--mesures 100] [--sortie fichier.json]
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import random
import resource
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

SEARCH_TARGET_MS = 150.0
LIST_TARGET_MS = 300.0
MOMENT = "2026-01-01T00:00:00Z"
SYLLABLES = [consonant + vowel for consonant in "bcdfglmnprstv" for vowel in "aeiou"]


def _rss_mib() -> float:
    """Mémoire résidente maximale du processus, en Mio (Linux : Kio, macOS : octets)."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024, 1)


def _word(rng: random.Random) -> str:
    return "".join(rng.choice(SYLLABLES) for _ in range(rng.randint(2, 4)))


def _settings(folder: Path) -> Any:
    from biblyo.config import Settings

    return Settings(env="development", data_dir=folder / "data", backup_dir=folder / "backups")


HEAVY_LEVEL = 300  # exemplaires sur chaque niveau du meuble le plus chargé
LEVELS_PER_UNIT = 10
UNITS_PER_ROOM = 40


def _places(notices: int, copies: list[tuple[Any, ...]]) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    """Salles, meubles, niveaux, puis 95 % des exemplaires rangés en rangs continus ; les autres restent sans place."""
    levels_total = max(LEVELS_PER_UNIT * 2, notices // 50)
    units_total = max(2, math.ceil(levels_total / LEVELS_PER_UNIT))
    rooms_total = math.ceil(units_total / UNITS_PER_ROOM)
    locations: list[tuple[Any, ...]] = []
    next_id = 0

    def add(parent: Any, kind: str, name: str, path: str, depth: int, order: int, capacity: Any, grid: Any) -> int:
        nonlocal next_id
        next_id += 1
        row, col, width, height = grid
        place = (next_id, f"l{next_id}", parent, kind, name, path, depth, order, capacity, row, col, width, height)
        locations.append((*place, 1, 1, MOMENT, MOMENT))
        return next_id

    shelf_copies = len(copies) * 95 // 100
    usual = max(1, (shelf_copies - HEAVY_LEVEL * LEVELS_PER_UNIT) // max(1, levels_total - LEVELS_PER_UNIT))
    no_grid = (None, None, 1, 1)
    levels: list[tuple[int, int]] = []
    made_units = 0
    for room_number in range(1, rooms_total + 1):
        room = add(None, "room", f"Salle {room_number}", f"Salle {room_number}", 0, room_number, None, no_grid)
        for unit_number in range(1, UNITS_PER_ROOM + 1):
            if made_units == units_total:
                break
            made_units += 1
            name = f"Meuble {unit_number}"
            path = f"Salle {room_number} / {name}"
            box = ((unit_number - 1) // 4 + 1, (unit_number - 1) % 4 * 2 + 1, 2, 1)
            unit = add(room, "furniture", name, path, 1, unit_number, None, box)
            for level_number in range(1, LEVELS_PER_UNIT + 1):
                size = HEAVY_LEVEL if made_units == 1 else usual
                level_name = f"Étagère {level_number}"
                level = add(unit, "shelf", level_name, f"{path} / {level_name}", 2, level_number, size + 20, no_grid)
                levels.append((level, size))
    placed: list[tuple[Any, ...]] = []
    cursor = 0
    for level, size in levels:
        for rank, row in enumerate(copies[cursor : cursor + size], start=1):
            placed.append((*row[:5], "available", row[6], row[7], level, rank, f"BD X {row[0] % 500}", MOMENT))
        cursor += size
    placed += [(*row, None, None, None, None) for row in copies[cursor:]]
    return locations, placed


READERS = 10_000
HOUSEHOLDS = 4_000
MINOR_SHARE = 0.3
DENSE_HOUSEHOLDS = 50  # foyers très liés : chaque enfant y a un lien avec chacun des adultes
DENSE_ADULTS = 6
DENSE_MINORS = 30


def _patrons(connection: sqlite3.Connection, rng: random.Random, insert: Any, normalize: Any) -> dict[str, int]:
    """10 000 lecteurs inventés, 4 000 foyers, leurs cartes, liens et coordonnées fictives (example.org)."""
    lasts = [_word(rng).capitalize() for _ in range(900)]
    firsts = [_word(rng).capitalize() for _ in range(300)]
    patrons, cards, contacts, members, links = [], [], [], [], []
    adults_of: dict[int, list[int]] = {}
    first_minor = int(READERS * (1 - MINOR_SHARE)) + 1  # les adultes d'abord : un mineur trouve ses responsables
    dense_adults, dense_minors = DENSE_HOUSEHOLDS * DENSE_ADULTS, DENSE_HOUSEHOLDS * DENSE_MINORS
    for patron_id in range(1, READERS + 1):
        first, last = rng.choice(firsts), rng.choice(lasts)
        minor = patron_id >= first_minor
        norm = normalize(f"{first} {last}")
        status = "archived" if patron_id % 40 == 0 else "active"
        number = f"P-{patron_id:06d}"
        birth = str(rng.randint(2010, 2019)) if minor else None
        patrons.append(
            (
                patron_id, f"p{patron_id}", number, first, last, norm, normalize(f"{last} {first}"), birth,
                "minor" if minor else "adult", status, MOMENT[:10], 1, MOMENT, MOMENT,
            )
        )  # fmt: skip
        cards.append((patron_id, f"L-{patron_id:06d}", patron_id, MOMENT[:10], MOMENT))
        household: int | None
        if minor and patron_id < first_minor + dense_minors:
            household = 1 + (patron_id - first_minor) % DENSE_HOUSEHOLDS
        elif not minor and patron_id <= dense_adults:
            household = 1 + (patron_id - 1) % DENSE_HOUSEHOLDS
        else:
            household = rng.randint(DENSE_HOUSEHOLDS + 1, HOUSEHOLDS) if rng.random() < 0.8 else None
        if household is not None:
            members.append((household, patron_id, "child" if minor else "responsible", MOMENT[:10]))
        if minor:
            for rank, parent in enumerate(adults_of.get(household or 0, [])[:DENSE_ADULTS], start=1):
                links.append((patron_id, parent, "parent", rank % 2, 1, rank, 1, MOMENT, MOMENT))
        else:
            if household is not None:
                adults_of.setdefault(household, []).append(patron_id)
            phone, email = f"+33639980{patron_id % 1000:03d}", f"l{patron_id}@example.org"
            contacts.append((patron_id, "phone", phone, phone.lstrip("+"), 1, MOMENT, MOMENT))
            contacts.append((patron_id, "email", email, email, 1, MOMENT, MOMENT))
    households = [
        (household, f"h{household}", "structure" if household % 50 == 0 else "family", f"Foyer {rng.choice(lasts)}",
         normalize(f"foyer {rng.choice(lasts)}"), 1, MOMENT, MOMENT)
        for household in range(1, HOUSEHOLDS + 1)
    ]  # fmt: skip
    insert(
        "patrons",
        "id,public_id,number,first_name,last_name,name_norm,sort_key,birth,category,status,registered_on,version,"
        "created_at,updated_at",
        patrons,
    )
    insert("patron_cards", "id,number,patron_id,issued_on,created_at", cards)
    insert("households", "id,public_id,kind,name,name_norm,version,created_at,updated_at", households)
    insert("household_members", "household_id,patron_id,role,joined_on", members)
    insert(
        "guardianships",
        "minor_id,guardian_id,relation,can_borrow_for,is_contact,contact_rank,version,created_at,updated_at",
        links,
    )
    insert("contacts", "patron_id,kind,value,search_value,is_primary,created_at,updated_at", contacts)
    connection.execute("UPDATE sequences SET last_value = ? WHERE name = 'patron_number'", (READERS,))
    connection.execute("UPDATE sequences SET last_value = ? WHERE name = 'patron_card'", (READERS,))
    return {"lecteurs": READERS, "foyers": HOUSEHOLDS, "liens_responsables": len(links), "coordonnees": len(contacts)}


def generate(folder: Path, notices: int, seed: int) -> dict[str, Any]:
    """Écrit le catalogue directement en SQL, sans index de recherche : sa reconstruction est mesurée à part."""
    from biblyo.core.isbn import to_isbn10
    from biblyo.core.migrate import upgrade
    from biblyo.core.text import normalize, sort_name, title_sort
    from biblyo.modules.catalogue.rules import completeness

    (folder / "data").mkdir(parents=True)
    (folder / "backups").mkdir()
    settings = _settings(folder)
    upgrade(settings)
    rng = random.Random(seed)  # noqa: S311 : jeu d'essai reproductible, sans usage cryptographique
    vocabulary = sorted({_word(rng) for _ in range(6000)})
    connection = sqlite3.connect(settings.database_path)
    connection.execute("PRAGMA synchronous = OFF")
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("BEGIN")

    def name(count: int) -> str:
        return " ".join(rng.choice(vocabulary).capitalize() for _ in range(count))

    def insert(table: str, columns: str, rows: list[tuple[Any, ...]]) -> None:
        marks = ",".join("?" * len(columns.split(",")))
        connection.executemany(f"INSERT INTO {table} ({columns}) VALUES ({marks})", rows)  # noqa: S608

    types = [row[0] for row in connection.execute("SELECT id FROM document_types")]
    section_ids = list(range(1, 11))
    insert(
        "sections",
        "id,code,label,sort_order,is_active",
        [(i, f"section-{i}", f"Section {i}", i, 1) for i in section_ids],
    )
    tag_rows, tag_ids = [], {"category": [], "genre": [], "keyword": []}
    tag_id = 0
    for kind, size in (("category", 40), ("genre", 60), ("keyword", 3000)):
        for label in sorted({name(2) for _ in range(size)}):
            tag_id += 1
            tag_rows.append((tag_id, kind, label, normalize(label), MOMENT))
            tag_ids[kind].append(tag_id)
    insert("tags", "id,kind,label,label_norm,created_at", tag_rows)
    publishers = sorted({name(2) for _ in range(600)})
    insert(
        "publishers",
        "id,name,name_norm,created_at",
        [(i + 1, label, normalize(label), MOMENT) for i, label in enumerate(publishers)],
    )
    collections = sorted({name(2) for _ in range(2000)})
    collection_owner = [rng.randint(1, len(publishers)) for _ in collections]
    insert(
        "collections",
        "id,publisher_id,name,name_norm,created_at",
        [(i + 1, collection_owner[i], label, normalize(label), MOMENT) for i, label in enumerate(collections)],
    )
    agents = [name(2) for _ in range(25_000)]
    insert(
        "agents",
        "id,kind,display_name,sort_name,name_norm,created_at",
        [(i + 1, "person", label, sort_name(label), normalize(label), MOMENT) for i, label in enumerate(agents)],
    )
    series_titles = sorted({name(3) for _ in range(6000)})
    series_counter = [0] * len(series_titles)
    series_call = [0] * len(series_titles)

    works, work_contributors, work_series, work_tags, editions, identifiers, copies, expected = ([] for _ in range(8))
    edition_id = copy_id = 0
    for work_id in range(1, notices + 1):
        title = name(rng.randint(2, 5))
        has_isbn = rng.random() < 0.7
        has_summary = rng.random() < 0.45
        author_ids = rng.sample(range(1, len(agents) + 1), rng.randint(0, 3))
        categories = rng.sample(tag_ids["category"], rng.randint(0, 2))
        created = f"2024-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}T{work_id % 24:02d}:00:00Z"
        year = rng.randint(1950, 2025) if rng.random() < 0.9 else None
        pages = rng.randint(24, 900) if rng.random() < 0.7 else None
        publisher = rng.randint(1, len(publishers)) if rng.random() < 0.9 else None
        collection = rng.randint(1, len(collections)) if rng.random() < 0.3 else None
        score, _missing = completeness(
            {
                "isbn": has_isbn,
                "publisher": publisher is not None,
                "year": year is not None,
                "summary": has_summary,
                "cover": False,
                "categories": bool(categories),
                "contributors": bool(author_ids),
                "pages": pages is not None,
                "collection": collection is not None,
            }
        )
        works.append(
            (
                work_id,
                f"w{work_id}",
                rng.choice(types),
                rng.choice(section_ids) if rng.random() < 0.6 else None,
                title,
                title_sort(title),
                normalize(title),
                rng.choice(("children", "teen", "adult", "all", None)),
                name(20) if has_summary else None,
                "archived" if rng.random() < 0.02 else "active",
                score,
                1,
                created,
                created,
            )
        )
        for position, agent in enumerate(author_ids):
            work_contributors.append((work_id, agent, "author", position))
        if rng.random() < 0.6:
            index = rng.randrange(len(series_titles))
            series_counter[index] += 1 + (rng.random() < 0.08)  # des trous, comme dans une vraie collection
            series_call[index] += 1
            work_series.append((work_id, index + 1, float(series_counter[index]), None, series_call[index], 1))
        genres = rng.sample(tag_ids["genre"], rng.randint(0, 2))
        chosen = categories + genres + rng.sample(tag_ids["keyword"], rng.randint(0, 3))
        work_tags += [(work_id, tag) for tag in chosen]
        for rank in range(1 + (rng.random() < 0.15)):
            edition_id += 1
            editions.append(
                (
                    edition_id,
                    f"e{edition_id}",
                    work_id,
                    1 if rank == 0 else 0,
                    publisher,
                    collection,
                    year,
                    rng.choice(("fre", "fre", "fre", "eng", "ita")),
                    pages,
                    "active",
                    "manual",
                    1,
                    created,
                    created,
                )
            )
            if has_isbn:
                stem = "978" + f"{edition_id:09d}"
                total = sum((3 if i % 2 else 1) * int(c) for i, c in enumerate(stem))
                isbn13 = stem + str((10 - total) % 10)
                identifiers.append((edition_id, "isbn13", isbn13))
                isbn10 = to_isbn10(isbn13)
                if isbn10:
                    identifiers.append((edition_id, "isbn10", isbn10))
            for _copy in range(rng.choice((0, 1, 1, 1, 2))):
                copy_id += 1
                barcode = f"EX-{copy_id:06d}"
                copies.append((copy_id, f"c{copy_id}", edition_id, barcode, barcode, "in_processing", MOMENT, MOMENT))
    for index in range(0, len(series_titles), 3):  # un tiers des séries a un tome confirmé au-delà du dernier connu
        expected.append((index + 1, float(series_counter[index] + 1), MOMENT))
    insert(
        "series",
        "id,public_id,title,title_sort,title_norm,next_call_sequence,status,created_at,updated_at",
        [
            (
                i + 1,
                f"s{i + 1}",
                label,
                title_sort(label),
                normalize(label),
                series_call[i] + 1,
                "unknown",
                MOMENT,
                MOMENT,
            )
            for i, label in enumerate(series_titles)
        ],
    )
    insert("series_expected_volumes", "series_id,volume_number,confirmed_at", expected)
    insert(
        "works",
        "id,public_id,document_type_id,section_id,title,title_sort,title_norm,audience,summary,status,completeness,"
        "version,created_at,updated_at",
        works,
    )
    insert("work_contributors", "work_id,agent_id,role,position", work_contributors)
    insert("work_series", "work_id,series_id,volume_number,volume_label,call_sequence,is_primary", work_series)
    insert("work_tags", "work_id,tag_id", work_tags)
    insert(
        "editions",
        "id,public_id,work_id,is_primary,publisher_id,collection_id,publication_year,language,page_count,status,origin,"
        "version,created_at,updated_at",
        editions,
    )
    insert("edition_identifiers", "edition_id,scheme,value", identifiers)
    # Lot L2 : 120 exemplaires pour 100 notices, rangés sur des étagères (1 pour 50 notices), un meuble très chargé.
    wanted = notices * 12 // 10
    next_edition = 0
    while copy_id < wanted and edition_id:
        copy_id += 1
        next_edition = next_edition % edition_id + 1
        barcode = f"EX-{copy_id:06d}"
        copies.append((copy_id, f"c{copy_id}", next_edition, barcode, barcode, "in_processing", MOMENT, MOMENT))
    locations, placed = _places(notices, copies)
    insert(
        "locations",
        "id,public_id,parent_id,kind,name,path,depth,sort_order,capacity,grid_row,grid_col,grid_width,grid_height,"
        "is_active,version,created_at,updated_at",
        locations,
    )
    insert(
        "copies",
        "id,public_id,edition_id,barcode,inventory_number,status,created_at,updated_at,location_id,shelf_position,"
        "call_number,first_shelved_at",
        placed,
    )
    connection.execute("UPDATE sequences SET last_value = ? WHERE name = 'copy_barcode'", (copy_id,))
    patrons = _patrons(connection, rng, insert, normalize)
    connection.commit()
    connection.execute("ANALYZE")
    connection.close()
    return {
        "notices": notices,
        "editions": edition_id,
        "exemplaires": copy_id,
        "contributeurs": len(agents),
        "series": len(series_titles),
        "etiquettes": tag_id,
        "vocabulaire": vocabulary[:40],
        "editeurs": publishers[:8],
        **patrons,
    }


def percentile(values: list[float], share: float) -> float:
    """Rang le plus proche : une valeur réellement observée."""
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(share * len(ordered)) - 1)]


def _summary(name: str, family: str, target: float | None, values: list[float]) -> dict[str, Any]:
    p95 = round(percentile(values, 0.95), 1)
    return {
        "scenario": name,
        "famille": family,
        "mesures": len(values),
        "p50_ms": round(percentile(values, 0.5), 1),
        "p95_ms": p95,
        "max_ms": round(max(values), 1),
        "moyenne_ms": round(statistics.fmean(values), 1),
        "cible_ms": target,
        "cible_atteinte": None if target is None else p95 <= target,
    }


def _measure(count: int, operation: Callable[[int], Any]) -> list[float]:
    values: list[float] = []
    for index in range(count):
        started = time.perf_counter()
        operation(index)
        values.append((time.perf_counter() - started) * 1000)
    return values


def step_reindex(folder: Path) -> dict[str, Any]:
    from biblyo.core.db import Database
    from biblyo.modules.catalogue import search

    database = Database(_settings(folder).database_path)
    started = time.perf_counter()
    with database.session(write=True) as session:
        count = search.reindex_all(session)
    rebuild = time.perf_counter() - started
    started = time.perf_counter()
    with database.session() as session:
        problems = search.inconsistencies(session)
    check = time.perf_counter() - started
    database.dispose()
    size = _settings(folder).database_path.stat().st_size
    return {
        "oeuvres": count,
        "reconstruction_s": round(rebuild, 2),
        "controle_coherence_s": round(check, 2),
        "ecarts": len(problems),
        "taille_base_mio": round(size / 1024 / 1024, 1),
        "rss_max_mio": _rss_mib(),
    }


def _queries(folder: Path, seed: int) -> dict[str, list[str]]:
    connection = sqlite3.connect(_settings(folder).database_path)
    rng = random.Random(seed + 1)  # noqa: S311
    titles = [row[0] for row in connection.execute("SELECT title_norm FROM works ORDER BY id LIMIT 20000")]
    authors = [row[0] for row in connection.execute("SELECT name_norm FROM agents ORDER BY id LIMIT 5000")]
    isbn_sql = "SELECT value FROM edition_identifiers WHERE scheme = 'isbn13' LIMIT 5000"
    isbn = [row[0] for row in connection.execute(isbn_sql)]
    codes = [row[0] for row in connection.execute("SELECT barcode FROM copies LIMIT 5000")]
    connection.close()
    words = [word for title in titles for word in title.split()]
    size = 40
    return {
        "un_mot": [rng.choice(words) for _ in range(size)],
        "prefixe": [rng.choice(words)[:3] for _ in range(size)],
        "deux_mots": [" ".join(rng.choice(titles).split()[:2]) for _ in range(size)],
        "auteur": [f"auteur:{rng.choice(authors).split()[0]}" for _ in range(size)],
        "isbn": [rng.choice(isbn) for _ in range(size)],
        "code": [rng.choice(codes) for _ in range(size)],
    }


def step_measure(folder: Path, seed: int, count: int) -> dict[str, Any]:
    import uvicorn

    from biblyo.app import create_app
    from biblyo.core.clock import now_iso
    from biblyo.core.db import Database
    from biblyo.core.models import User
    from biblyo.core.security import passwords, sessions
    from biblyo.core.security.sessions import COOKIE_NAME
    from biblyo.modules.admin import repository as admin_repository
    from biblyo.modules.catalogue import listing, search, series_view, stats
    from biblyo.modules.catalogue.listing import Criteria

    settings = _settings(folder)
    database = Database(settings.database_path)
    queries = _queries(folder, seed)
    connection = sqlite3.connect(settings.database_path)
    category_sql = "SELECT label_norm FROM tags WHERE kind = 'category' ORDER BY id"
    categories = [row[0] for row in connection.execute(category_sql)]
    publishers = [row[0] for row in connection.execute("SELECT name_norm FROM publishers ORDER BY id LIMIT 200")]
    types = [row[0] for row in connection.execute("SELECT code FROM document_types ORDER BY id")]
    connection.close()
    results: list[dict[str, Any]] = []

    def run(name: str, family: str, target: float | None, make: Callable[[int], Criteria]) -> None:
        def operation(index: int) -> None:
            with database.session() as session:
                listing.run(session, make(index))

        results.append(_summary(name, family, target, _measure(count, operation)))

    def fts(name: str, pool: list[str]) -> None:
        def operation(index: int) -> None:
            with database.session() as session:
                search.match_ids(session, pool[index % len(pool)], limit=50)

        results.append(_summary(f"FTS seul : {name}", "recherche", SEARCH_TARGET_MS, _measure(count, operation)))

    for label, key in (
        ("un mot", "un_mot"),
        ("préfixe de 3 lettres", "prefixe"),
        ("deux mots", "deux_mots"),
        ("critère auteur:", "auteur"),
        ("ISBN exact", "isbn"),
        ("code-barres", "code"),
    ):
        fts(label, queries[key])

    pool = queries["un_mot"]
    run("Page de résultats : un mot", "recherche", SEARCH_TARGET_MS, lambda i: Criteria(q=pool[i % len(pool)]))
    run("Page de résultats : préfixe", "recherche", SEARCH_TARGET_MS, lambda i: Criteria(q=queries["prefixe"][i % 40]))
    run("Palette ⌘K (8 notices)", "recherche", SEARCH_TARGET_MS, lambda i: Criteria(q=queries["deux_mots"][i % 40]))

    run("Sans filtre, titre", "liste", LIST_TARGET_MS, lambda _i: Criteria())
    run("Filtre : type", "liste", LIST_TARGET_MS, lambda i: Criteria(type=types[i % len(types)]))
    run("Filtre : catégorie", "liste", LIST_TARGET_MS, lambda i: Criteria(cat=categories[i % len(categories)]))
    run(
        "Filtre : éditeur (texte)",
        "liste",
        LIST_TARGET_MS,
        lambda i: Criteria(editeur=publishers[i % len(publishers)]),
    )
    run(
        "Filtre : années",
        "liste",
        LIST_TARGET_MS,
        lambda i: Criteria(annee_min=str(1950 + i % 60), annee_max=str(1960 + i % 60)),
    )
    run("Filtre : sans ISBN", "liste", LIST_TARGET_MS, lambda _i: Criteria(meta="sans-isbn"))
    run("Filtre : avec exemplaire", "liste", LIST_TARGET_MS, lambda _i: Criteria(exemplaires="avec"))
    run("Filtre : langue", "liste", LIST_TARGET_MS, lambda _i: Criteria(langue="ita"))

    run(
        "Plusieurs critères : recherche, type, catégorie",
        "combinaison",
        LIST_TARGET_MS,
        lambda i: Criteria(q=pool[i % len(pool)], type=types[i % len(types)], cat=categories[i % len(categories)]),
    )
    run(
        "Plusieurs critères : type, années, sans ISBN",
        "combinaison",
        LIST_TARGET_MS,
        lambda i: Criteria(type=types[i % len(types)], annee_min="1980", annee_max="2010", meta="sans-isbn"),
    )
    run(
        "Plusieurs critères : éditeur, catégorie, tri par année",
        "combinaison",
        LIST_TARGET_MS,
        lambda i: Criteria(editeur=publishers[i % len(publishers)], cat=categories[i % len(categories)], tri="annee"),
    )

    run("Pagination : page 2", "pagination", LIST_TARGET_MS, lambda _i: Criteria(page=2))
    run("Pagination : page 500", "pagination", LIST_TARGET_MS, lambda _i: Criteria(page=500))
    run("Pagination : dernière page", "pagination", LIST_TARGET_MS, lambda _i: Criteria(page=10_000))
    run(
        "Pagination : page 1000, tri récents",
        "pagination",
        LIST_TARGET_MS,
        lambda _i: Criteria(page=1000, tri="recents"),
    )
    run("Tri : série et tome", "tri", LIST_TARGET_MS, lambda _i: Criteria(tri="serie"))
    run("Tri : année", "tri", LIST_TARGET_MS, lambda _i: Criteria(tri="annee"))
    run("Tri : complétude", "tri", LIST_TARGET_MS, lambda _i: Criteria(tri="completude"))

    heavy = max(10, count // 5)

    def series_page(_index: int) -> None:
        with database.session() as session:
            series_view.list_series(session)

    def dashboard(_index: int) -> None:
        with database.session() as session:
            stats.compute(session)

    results.append(_summary("Liste des séries", "autre", LIST_TARGET_MS, _measure(heavy, series_page)))

    for code, label in (("incompletes", "filtre Incomplètes"), ("doublons", "filtre Doublons")):

        def filtered_series(_index: int, code: str = code) -> None:
            with database.session() as session:
                series_view.list_series(session, "", code)

        values = _measure(count, filtered_series)
        results.append(_summary(f"Liste des séries, {label}", "autre", LIST_TARGET_MS, values))

    results += _series_breakdown(database, count)
    results.append(_summary("Chiffres du tableau de bord", "autre", LIST_TARGET_MS, _measure(heavy, dashboard)))

    # Réponses HTTP complètes : routage, droits, requêtes et rendu du gabarit, par un vrai serveur local.
    moment = now_iso()
    with database.session(write=True) as session:
        user = User(
            username="mesure",
            display_name="Mesure",
            password_hash=passwords.hash_password("Mesure-De-Performance-42"),
            is_active=True,
            must_change_password=False,
            created_at=moment,
            updated_at=moment,
        )
        user.roles = list(admin_repository.roles_by_codes(session, ["admin"]))
        session.add(user)
        session.flush()
        from sqlalchemy import select

        from biblyo.core.models import AuthSession

        raw = sessions.open_session(session, user.id, settings, "127.0.0.1", "Mesure")
        csrf_token = session.scalars(select(AuthSession.csrf_token).where(AuthSession.user_id == user.id)).one()
    port = 18950
    application = create_app(settings)
    application.state.search_limiter.limit = 10**9  # la mesure enchaîne les recherches : le plafond n'est pas son objet
    renders: list[float] = []
    templates = application.state.templates
    render_text = templates.render_text

    def timed_render(request: Any, template: str, context: Any = None) -> str:
        started = time.perf_counter()
        try:
            return render_text(request, template, context)
        finally:
            if template.endswith("series_list.html") and "filtre=incompletes" in str(request.url):
                renders.append((time.perf_counter() - started) * 1000)

    templates.render_text = timed_render  # mesure du seul rendu HTML de la page des séries filtrée
    server = uvicorn.Server(uvicorn.Config(application, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 60
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started, "Le serveur de mesure n'a pas démarré"

    def http(name: str, target: float | None, path: Callable[[int], str]) -> None:
        def operation(index: int) -> None:
            request = urllib.request.Request(f"http://127.0.0.1:{port}{path(index)}")
            request.add_header("Cookie", f"{COOKIE_NAME}={raw}")
            with urllib.request.urlopen(request) as response:
                assert response.status == 200
                response.read()

        results.append(_summary(name, "http", target, _measure(count, operation)))

    def post(
        name: str,
        target: float | None,
        url: str,
        text: Callable[[int], str],
        field: str = "q",
        extra: dict[str, str] | None = None,
    ) -> None:
        def operation(index: int) -> None:
            body = urllib.parse.urlencode({field: text(index), **(extra or {})}).encode()
            request = urllib.request.Request(f"http://127.0.0.1:{port}{url}", data=body)
            request.add_header("Cookie", f"{COOKIE_NAME}={raw}")
            request.add_header("X-CSRF-Token", csrf_token)
            with urllib.request.urlopen(request) as response:
                assert response.status == 200
                response.read()

        results.append(_summary(name, "http", target, _measure(count, operation)))

    quote = urllib.parse.quote
    http("GET /catalogue", LIST_TARGET_MS, lambda _i: "/catalogue")
    http("GET /catalogue?q=", SEARCH_TARGET_MS, lambda i: f"/catalogue?q={quote(pool[i % len(pool)])}")
    post("Palette, notices", SEARCH_TARGET_MS, "/ui/recherche", lambda i: queries["deux_mots"][i % 40])
    http(
        "GET /catalogue, plusieurs filtres",
        LIST_TARGET_MS,
        lambda i: f"/catalogue?type={types[i % len(types)]}&cat={quote(categories[i % len(categories)])}&view=list",
    )
    http("GET /catalogue/series", LIST_TARGET_MS, lambda _i: "/catalogue/series")
    http("GET /catalogue/series?filtre=incompletes", LIST_TARGET_MS, lambda _i: "/catalogue/series?filtre=incompletes")
    http("GET /catalogue/series?filtre=doublons", LIST_TARGET_MS, lambda _i: "/catalogue/series?filtre=doublons")
    render_label = "Séries filtrées, décomposition : rendu HTML (Incomplètes)"
    results.append(_summary(render_label, "decomposition", None, renders))
    http("GET / (tableau de bord)", LIST_TARGET_MS, lambda _i: "/")
    # Fiche d'un livre (L5 : pas plus de 10 % au-dessus d'avant) : des notices prises à pas fixe dans tout le jeu
    from sqlalchemy import text as sql_text

    with database.session() as session:
        works = [row[0] for row in session.execute(sql_text("SELECT id FROM works ORDER BY id"))]
    stride = max(1, len(works) // count)
    http(
        "GET /catalogue/notices/{id} (fiche d'un livre)",
        SEARCH_TARGET_MS,
        lambda i: f"/catalogue/notices/{works[(i * stride) % len(works)]}",
    )
    _measure_places(database, results, http, _measure, count)
    _measure_patrons(database, results, http, post, _measure, count, seed)
    server.should_exit = True
    thread.join(timeout=15)
    database.dispose()
    return {"scenarios": results, "rss_max_mio": _rss_mib()}


def _measure_patrons(
    database: Any, results: list[dict[str, Any]], http: Any, post: Any, measure: Any, count: int, seed: int
) -> None:
    """Annuaire, recherche, fiche, sélecteur, inscription d'une famille, sur 10 000 lecteurs et 4 000 foyers."""
    from biblyo.modules.patrons import service
    from biblyo.modules.patrons.service import FamilyAdult, FamilyRegistration, PersonInput

    rng = random.Random(seed + 7)  # noqa: S311 : jeu d'essai reproductible
    with database.session() as session:
        names = [row[0] for row in session.execute(text_query("SELECT last_name FROM patrons LIMIT 400"))]
        first_names = [row[0] for row in session.execute(text_query("SELECT first_name FROM patrons LIMIT 400"))]
        minors = [row[0] for row in session.execute(text_query("SELECT minor_id FROM guardianships LIMIT 200"))]
    pool = [f"{rng.choice(first_names)} {rng.choice(names)}" for _ in range(100)]
    http("GET /lecteurs", LIST_TARGET_MS, lambda _i: "/lecteurs")
    http("GET /lecteurs, page 100", LIST_TARGET_MS, lambda _i: "/lecteurs?page=100")
    search = "/lecteurs/recherche"
    http("GET /lecteurs, 100 par page", LIST_TARGET_MS, lambda _i: "/lecteurs?par_page=100")
    http("GET /lecteurs, 100 par page, page 50", LIST_TARGET_MS, lambda _i: "/lecteurs?par_page=100&page=50")
    post("Recherche, nom complet", SEARCH_TARGET_MS, search, lambda i: pool[i % 100])
    post("Recherche, début de nom", SEARCH_TARGET_MS, search, lambda i: names[i % 400][:3])
    post("Recherche, carte", SEARCH_TARGET_MS, search, lambda i: f"L-{1 + i % 9000:06d}")
    post("Recherche, téléphone", SEARCH_TARGET_MS, search, lambda i: f"0639980{i % 900:03d}")
    http("GET /lecteurs?vue=mineurs", LIST_TARGET_MS, lambda _i: "/lecteurs?vue=mineurs")
    http("GET /lecteurs?vue=responsable", LIST_TARGET_MS, lambda _i: "/lecteurs?vue=responsable")
    http("GET /lecteurs?vue=foyers", LIST_TARGET_MS, lambda _i: "/lecteurs?vue=foyers")
    post("Recherche dans les foyers", SEARCH_TARGET_MS, search, lambda i: names[i % 400][:4], extra={"vue": "foyers"})
    post("Sélecteur de lecteur", SEARCH_TARGET_MS, "/lecteurs/choix", lambda i: pool[i % 100], "choix_q")
    post(
        "Sélecteur, un adulte par carte",
        SEARCH_TARGET_MS,
        "/lecteurs/choix?adultes=1",
        lambda i: f"L-{1 + i % 9000:06d}",
        "choix_q",
    )
    http("Fiche d'un mineur", LIST_TARGET_MS, lambda i: f"/lecteurs/{minors[i % len(minors)]}")
    http("Fiche d'un foyer", LIST_TARGET_MS, lambda i: f"/foyers/{1 + i % 4000}")
    first_dense_minor = int(READERS * (1 - MINOR_SHARE)) + 1
    http("Fiche d'un foyer dense (36 membres)", LIST_TARGET_MS, lambda i: f"/foyers/{1 + i % DENSE_HOUSEHOLDS}")
    http("Fiche d'un adulte, 30 enfants à charge", LIST_TARGET_MS, lambda i: f"/lecteurs/{1 + i % DENSE_HOUSEHOLDS}")
    http("Fiche d'un mineur à 6 responsables", LIST_TARGET_MS, lambda i: f"/lecteurs/{first_dense_minor + i % 1500}")
    post("Palette, lecteurs par nom", SEARCH_TARGET_MS, "/ui/recherche/lecteurs", lambda i: pool[i % 100])
    post("Palette, début de nom", SEARCH_TARGET_MS, "/ui/recherche/lecteurs", lambda i: names[i % 400][:3])
    post("Palette, carte", SEARCH_TARGET_MS, "/ui/recherche/lecteurs", lambda i: f"L-{1 + i % 9000:06d}")
    http("GET /lecteurs/a-traiter", LIST_TARGET_MS, lambda _i: "/lecteurs/a-traiter")

    def family(index: int) -> None:
        def person(first: str, category: str, birth: str = "") -> PersonInput:
            return PersonInput(first_name=first, last_name=f"Mesure{index}", category=category, birth=birth)

        data = FamilyRegistration(
            name=f"Famille Mesure {index}",
            adults=(FamilyAdult(person("Alix", "adult"), can_borrow_for=True),),
            children=(person("Noa", "minor", "2016"), person("Sacha", "minor", "2018")),
            homonyms_confirmed=True,
        )
        service.register_family(database, data, None)

    results.append(
        _summary("Inscription d'une famille (3 personnes)", "lecteurs", LIST_TARGET_MS, measure(count, family))
    )


def text_query(statement: str) -> Any:
    from sqlalchemy import text

    return text(statement)


def _measure_places(database: Any, results: list[dict[str, Any]], http: Any, measure: Any, count: int) -> None:
    """Lot L2 : arbre, meuble le plus chargé, localisation, tri, catalogue par emplacement, rangement."""
    from sqlalchemy import func, select

    from biblyo.modules.catalogue.models import Copy
    from biblyo.modules.holdings import repository, service
    from biblyo.modules.holdings.models import Location

    with database.session() as session:
        count_label = func.count().label("n")
        level = session.execute(
            select(Copy.location_id, count_label)
            .group_by(Copy.location_id)
            .where(Copy.location_id.is_not(None))
            .order_by(count_label.desc(), Copy.location_id)
            .limit(1)
        ).one()[0]
        furniture = session.get(Location, level).parent_id
        room = session.get(Location, furniture).parent_id
        rows = session.execute(
            select(Copy.id, Copy.barcode).where(Copy.location_id == level).order_by(Copy.shelf_position)
        ).all()
    copies = [row[0] for row in rows]
    codes = [row[1] for row in rows]
    http("GET /emplacements (arbre et salles)", LIST_TARGET_MS, lambda _i: "/emplacements")
    http("GET /emplacements/{meuble le plus chargé}", LIST_TARGET_MS, lambda _i: f"/emplacements/{furniture}")
    http("GET /emplacements/{meuble}?vue=liste", LIST_TARGET_MS, lambda _i: f"/emplacements/{furniture}?vue=liste")
    http(
        "Localisation : page avec le dos cerné",
        SEARCH_TARGET_MS,
        lambda i: f"/emplacements/{level}?localiser={copies[i % len(copies)]}",
    )
    http(
        "Localisation : par code, jusqu'à la page",
        LIST_TARGET_MS,
        lambda i: f"/emplacements/localiser?q={codes[i % len(codes)]}",
    )

    def locate(index: int) -> None:
        with database.session() as session:
            repository.localise(session, copies[index % len(copies)])

    results.append(_summary("Localisation : lecture seule", "emplacements", SEARCH_TARGET_MS, measure(count, locate)))
    results.append(
        _summary(
            "Aperçu du tri d'une étagère de 300",
            "emplacements",
            LIST_TARGET_MS,
            measure(count, lambda _i: service.sort_preview(database, level, "titre")),
        )
    )
    http("GET /catalogue filtré par salle", LIST_TARGET_MS, lambda _i: f"/catalogue?view=list&emplacement={room}")
    http("GET /catalogue filtré par étagère", LIST_TARGET_MS, lambda _i: f"/catalogue?view=list&emplacement={level}")
    http(
        "GET /catalogue, ordre sur l'étagère",
        LIST_TARGET_MS,
        lambda _i: f"/catalogue?view=list&emplacement={furniture}&tri=rayon",
    )

    def move(index: int) -> None:
        service.shelve(database, [copies[-1 - index % 40]], level, position=1, user_id=None)

    results.append(
        _summary("Rangement en tête d'une étagère de 300", "emplacements", LIST_TARGET_MS, measure(count, move))
    )


def _series_breakdown(database: Any, count: int) -> list[dict[str, Any]]:
    """Page des séries filtrée, par étape : requêtes SQL, calcul métier. Le rendu HTML est mesuré par le serveur."""
    from sqlalchemy import select

    from biblyo.modules.catalogue import series_view
    from biblyo.modules.catalogue.models import Series

    timings: dict[str, list[float]] = {"sql": [], "calcul": [], "sql_page": [], "calcul_page": []}

    def lap(key: str, started: float) -> float:
        now = time.perf_counter()
        timings[key].append((now - started) * 1000)
        return now

    for _index in range(count):
        with database.session() as session:
            started = time.perf_counter()
            ids = series_view.ordered_series_ids(session)
            facts = series_view.load_facts(session, with_duplicates=False)
            started = lap("sql", started)
            counts = series_view.classify(ids, facts)
            matching = [series_id for series_id in ids if counts[series_id].matches("incompletes")]
            shown = matching[: series_view.PAGE_SIZE]
            started = lap("calcul", started)
            found = {item.id: item for item in session.scalars(select(Series).where(Series.id.in_(shown)))}
            links = series_view._links_by_series(session, shown)
            confirmed = series_view._confirmed_by_series(session, shown)
            editions = series_view._edition_counts(session, shown)
            copies = series_view._copy_counts(session, shown)
            started = lap("sql_page", started)
            for series_id in shown:
                series_view._build_card(
                    found[series_id], links.get(series_id, []), confirmed.get(series_id, {}), editions, copies
                )
            lap("calcul_page", started)
    labels = {
        "sql": "SQL : ordre et faits de toutes les séries",
        "calcul": "calcul : classement de toutes les séries",
        "sql_page": "SQL : les 40 séries affichées",
        "calcul_page": "calcul : frises des 40 séries affichées",
    }
    return [
        _summary(f"Séries filtrées, décomposition : {labels[key]}", "decomposition", None, values)
        for key, values in timings.items()
    ]


def _environment() -> dict[str, Any]:
    info: dict[str, Any] = {
        "plateforme": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "processeurs_visibles": os.cpu_count(),
        "memoire_visible_mio": round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024 / 1024),
    }
    limit = Path("/sys/fs/cgroup/memory.max")
    if limit.is_file():
        info["limite_cgroup_memoire"] = limit.read_text().strip()
    return info


def _child(step: str, folder: Path, seed: int, count: int) -> dict[str, Any]:
    """Chaque étape dans son propre processus : la mémoire mesurée est la sienne seule."""
    command = [sys.executable, __file__, "--etape", step, "--dossier", str(folder), "--graine", str(seed)]
    done = subprocess.run([*command, "--mesures", str(count)], capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise SystemExit(f"Étape {step} en échec :\n{done.stderr[-3000:]}")
    return json.loads(done.stdout.strip().splitlines()[-1])


def _table(report: dict[str, Any]) -> str:
    lines = [
        "| Scénario | N | p50 (ms) | p95 (ms) | max (ms) | Cible p95 (ms) | Atteinte |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in report["mesures"]["scenarios"]:
        verdict = {True: "oui", False: "**non**", None: "sans objet"}[row["cible_atteinte"]]
        target = row["cible_ms"] if row["cible_ms"] is not None else "—"
        lines.append(
            f"| {row['scenario']} | {row['mesures']} | {row['p50_ms']} | {row['p95_ms']} | {row['max_ms']} "
            f"| {target} | {verdict} |"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--notices", type=int, default=100_000)
    parser.add_argument("--graine", type=int, default=20261108)
    parser.add_argument("--mesures", type=int, default=100)
    parser.add_argument("--sortie", type=Path)
    parser.add_argument("--dossier", type=Path)
    parser.add_argument("--etape", choices=("reindex", "mesure"))
    arguments = parser.parse_args()

    if arguments.etape == "reindex":
        print(json.dumps(step_reindex(arguments.dossier)))
        return 0
    if arguments.etape == "mesure":
        print(json.dumps(step_measure(arguments.dossier, arguments.graine, arguments.mesures)))
        return 0

    folder = Path(tempfile.mkdtemp(prefix="biblyo-perf-"))
    try:
        started = time.perf_counter()
        dataset = generate(folder, arguments.notices, arguments.graine)
        generation = round(time.perf_counter() - started, 1)
        rebuild = _child("reindex", folder, arguments.graine, arguments.mesures)
        measures = _child("mesure", folder, arguments.graine, arguments.mesures)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    report = {
        "environnement": _environment(),
        "graine": arguments.graine,
        "jeu": {key: value for key, value in dataset.items() if key not in ("vocabulaire", "editeurs")},
        "generation_s": generation,
        "reconstruction": rebuild,
        "mesures": measures,
    }
    if arguments.sortie:
        arguments.sortie.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    head = {key: report[key] for key in ("environnement", "jeu", "generation_s", "reconstruction")}
    print(json.dumps(head, ensure_ascii=False, indent=2))
    print(f"Mémoire maximale de l'étape de mesure : {measures['rss_max_mio']} Mio")
    print(_table(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
