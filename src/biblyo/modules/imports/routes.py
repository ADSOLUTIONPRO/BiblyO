# SPDX-License-Identifier: EUPL-1.2
"""Écrans de l'import (lot L6, T3 à T7) : dépôt, correspondance, simulation, application, lots, exports.

Le dépôt et la simulation n'écrivent rien dans la base. Seules écrivent : l'application d'un plan confirmé (T5,
permission `imports.apply`, instance où l'application est activée), et l'enregistrement ou la suppression d'un
profil de correspondance. Les exports (T6, T7) lisent seulement ; ils sont journalisés.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from starlette.datastructures import FormData, UploadFile

from biblyo.core import audit, flash
from biblyo.core.clock import now_iso
from biblyo.core.i18n import _
from biblyo.core.permissions import require
from biblyo.core.security.sessions import AuthContext, get_auth
from biblyo.core.text import normalize
from biblyo.modules.imports import apply as importer
from biblyo.modules.imports import export, guard, original, reader, report, simulate, store
from biblyo.modules.imports import profile as profiles
from biblyo.modules.imports.models import ImportBatch, ImportProfile

router = APIRouter()

CanSimulate = Annotated[AuthContext, require("imports.simulate")]
CanApply = Annotated[AuthContext, require("imports.apply")]
ROWS_PER_PAGE = 100
VIEWS: dict[str, str] = {
    "lignes": _("Lignes"),
    "anomalies": _("Anomalies"),
    "livres": _("Livres"),
    "series": _("Séries"),
    "cotes": _("Cotes partagées"),
    "propositions": _("Propositions"),
    "differences": _("Différences"),
    "colonnes": _("Colonnes"),
    "noms": _("Contributeurs et éditeurs"),
}
BATCH_STATUSES: dict[str, str] = {
    "applied": _("Appliqué"),
    "failed": _("Échoué, rien écrit"),
    "applying": _("En cours"),
    "uploaded": _("Déposé"),
    "simulated": _("Simulé"),
    "cancelled": _("Abandonné"),
}
_SIMULATIONS: OrderedDict[tuple[str, str, str, str], simulate.Simulation] = OrderedDict()
_SIMULATIONS_KEPT = 4


async def _read_form(request: Request) -> FormData:
    return await request.form()


Form = Annotated[FormData, Depends(_read_form)]


def _render(request: Request, template: str, context: dict[str, Any], status: int = 200) -> HTMLResponse:
    response: HTMLResponse = request.app.state.templates.render(request, template, context, status_code=status)
    return response


def _go(request: Request, path: str, code: str = "") -> Response:
    response = RedirectResponse(path, status_code=303)
    if code:
        flash.set_flash(response, code, request.app.state.settings)
    return response


def _data_dir(request: Request) -> Any:
    return request.app.state.settings.data_dir


def _first(form: FormData, name: str) -> str:
    value = form.get(name)
    return value if isinstance(value, str) else ""


# --- Dépôt -----------------------------------------------------------------------------------------------------------
def _saved_profiles(request: Request) -> list[ImportProfile]:
    with request.app.state.database.session() as session:
        return list(session.scalars(select(ImportProfile).order_by(ImportProfile.name)))


def _batches(request: Request) -> tuple[list[dict[str, Any]], bool]:
    with request.app.state.database.session() as session:
        rows = list(session.scalars(select(ImportBatch).order_by(ImportBatch.id.desc()).limit(50)))
        enabled = importer.enabled(session)
    batches = []
    for batch in rows:
        summary = json.loads(batch.summary or "{}")
        batches.append(
            {
                "id": batch.id,
                "public_id": batch.public_id or "",
                "filename": batch.filename,
                "status": batch.status,
                "at": batch.applied_at or batch.created_at,
                "copies": (summary.get("created") or {}).get("copies"),
                "error": summary.get("erreur", ""),
            }
        )
    return batches, enabled


def _index(request: Request, auth: AuthContext, *, message: str = "", status: int = 200) -> HTMLResponse:
    batches, enabled = _batches(request)
    context = {
        "deposits": store.mine(_data_dir(request), auth.user_id),
        "batches": batches,
        "apply_enabled": enabled,
        "batch_statuses": BATCH_STATUSES,
        "saved": _saved_profiles(request),
        "message": message,
        "max_mib": reader.MAX_FILE_BYTES // (1024 * 1024),
        "rules": profiles.VALIDATED_RULES,
    }
    return _render(request, "imports/pages/index.html", context, status)


@router.get("/imports", include_in_schema=False)
def index(request: Request, auth: CanSimulate) -> HTMLResponse:
    return _index(request, auth)


def _upload(value: Any) -> tuple[bytes, str] | None:
    if not isinstance(value, UploadFile) or not value.filename:
        return None
    return value.file.read(reader.MAX_FILE_BYTES + 1), value.filename


@router.post("/imports", include_in_schema=False)
def deposit(request: Request, auth: CanSimulate, form: Form) -> Response:
    main_upload = _upload(form.get("principal"))
    other_upload = _upload(form.get("complementaire"))
    if main_upload is None:
        return _index(request, auth, message=_("Choisissez le fichier à simuler, en .xlsx ou en .csv."), status=400)
    try:
        main = reader.read(*main_upload)
        other = reader.read(*other_upload) if other_upload else None
    except reader.ReadError as exc:
        return _index(request, auth, message=exc.message, status=400)
    for workbook in (main, other):
        refused = guard.refusal(workbook) if workbook is not None else ""
        if refused:
            return _index(request, auth, message=refused, status=400)
    base = None
    profile_id = _first(form, "profil")
    if profile_id.isdigit():
        with request.app.state.database.session() as session:
            saved = session.get(ImportProfile, int(profile_id))
            if saved is not None:
                base = profiles.Profile.from_json(saved.mapping, name=saved.name)
    main_sheet = main.sheet(store.data_sheet(main))
    profile = profiles.detect(main_sheet, base) if main_sheet else profiles.Profile()
    other_sheet = other.sheet(store.data_sheet(other)) if other is not None else None
    if other_sheet is not None:
        profile = profiles.detect(other_sheet, profile)
    if base is not None:
        profile.name = base.name
    created = store.create(
        _data_dir(request),
        (main_upload[0], main),
        (other_upload[0], other) if other_upload and other else None,
        user_id=auth.user_id,
        profile=profile,
    )
    return _go(request, f"/imports/{created.token}", "import_deposited")


def _deposit(request: Request, token: str) -> store.Deposit:
    auth = get_auth(request)
    found = store.load(_data_dir(request), token, auth.user_id if auth else None)
    if found is None:
        raise HTTPException(status_code=404)
    return found


# --- Correspondance (T3) ---------------------------------------------------------------------------------------------
def _mapping_page(
    request: Request,
    deposit: store.Deposit,
    *,
    message: str = "",
    errors: dict[str, str] | None = None,
    status: int = 200,
) -> HTMLResponse:
    try:
        main, other = store.workbooks(_data_dir(request), deposit)
    except reader.ReadError as exc:
        store.discard(_data_dir(request), deposit.token)
        raise HTTPException(status_code=410, detail=exc.message) from exc
    sheet = main.sheet(deposit.sheet) or main.sheets[0]
    other_sheet = other.sheet(deposit.complement_sheet) if other else None
    profile = profiles.Profile.from_json(deposit.profile, name=deposit.profile_name)
    headers = [c.label for c in sheet.columns]
    columns = []
    sources = [(sheet, "main", main.filename)]
    if other is not None and other_sheet is not None:
        sources.append((other_sheet, "complement", other.filename))
    for current_sheet, current, label in sources:
        for column in current_sheet.columns:
            if current == "complement" and column.label in headers:
                continue
            code = profile.field_of(column)
            values = profiles.distinct_values(current_sheet, column)
            filled = sum(n for _v, n in values)
            columns.append(
                {
                    "source": current,
                    "file": label,
                    "column": column,
                    "field": code,
                    "level": profiles.LEVELS[profiles.FIELD_BY_CODE[code].level],
                    "suggestion": profiles.suggest(column.label, headers),
                    "filled": filled,
                    "rows": len(current_sheet.rows),
                    "examples": values[:4],
                    "distinct": len(values),
                    "risks": profiles.risky(current_sheet, column, code),
                }
            )
    preview = []
    for row in sheet.rows[:8]:
        split: dict[str, list[tuple[str, str]]] = {level: [] for level in profiles.LEVELS}
        for column in sheet.columns:
            value = profiles.display(row.get(column)).strip()
            if value:
                spec = profiles.FIELD_BY_CODE[profile.field_of(column)]
                split[spec.level].append((spec.label if spec.code != profiles.KEEP else column.label, value))
        preview.append((row.number, split))
    condition_values = [
        (value, count, profile.condition_map.get(value, ""), profiles.recommend_condition(value))
        for column in profile.columns_for(sheet, "condition")
        for value, count in profiles.distinct_values(sheet, column)
    ]
    collection_values = [
        (value, count, profile.collection_map.get(value, "collection"), profile.collection_links.get(value))
        for column in profile.columns_for(sheet, "collection")
        for value, count in profiles.distinct_values(sheet, column)
    ]
    # Variantes qu'une comparaison sans casse ni accents confondrait : la forme retenue se choisit ici (D-L6-5).
    variants: dict[str, list[str]] = {}
    for value, _count, _kind, _link in collection_values:
        variants.setdefault(normalize(value), []).append(value)
    collection_groups = {value: forms for forms in variants.values() if len(forms) > 1 for value in forms}
    context = {
        "deposit": deposit,
        "main": main,
        "other": other,
        "data_sheet": sheet,
        "columns": columns,
        "fields": profiles.FIELDS,
        "levels": profiles.LEVELS,
        "profile": profile,
        "preview": preview,
        "condition_values": condition_values,
        "collection_values": collection_values,
        "conditions": profiles.CONDITIONS,
        "collection_kinds": profiles.COLLECTION_KINDS,
        "existing_collections": _existing_collections(request),
        "collection_groups": collection_groups,
        "role_modes": profiles.ROLE_MODES,
        "barcode_modes": profiles.BARCODE_MODES,
        "rule_status": profiles.RULE_STATUS,
        "rules": profiles.VALIDATED_RULES,
        "notices": [(main.filename, n) for n in sheet.notices]
        + ([(other.filename, n) for n in other_sheet.notices] if other and other_sheet else []),
        "message": message,
        "errors": errors or {},
    }
    return _render(request, "imports/pages/mapping.html", context, status)


def _existing_collections(request: Request) -> list[dict[str, Any]]:
    """Collections déjà au catalogue, pour un rapprochement manuel (D-L6-5) : jamais un rapprochement d'office."""
    from biblyo.modules.catalogue.models import Collection, Publisher

    with request.app.state.database.session() as session:
        rows = session.execute(
            select(Collection.id, Collection.name, Publisher.name)
            .outerjoin(Publisher, Publisher.id == Collection.publisher_id)
            .order_by(Collection.name_norm, Collection.id)
        ).all()
    return [{"id": i, "label": f"{name} ({publisher})" if publisher else name} for i, name, publisher in rows]


@router.get("/imports/{token}", include_in_schema=False)
def mapping(request: Request, token: str, _auth: CanSimulate) -> HTMLResponse:
    return _mapping_page(request, _deposit(request, token))


def _profile_from_form(form: FormData, deposit: store.Deposit) -> tuple[profiles.Profile, dict[str, str]]:
    """Relit la correspondance de l'écran. Toute colonne garde une destination : à défaut, « conservée »."""
    current = profiles.Profile.from_json(deposit.profile, name=deposit.profile_name)
    errors: dict[str, str] = {}
    columns = dict(current.columns)
    for key in form:
        if key.startswith("col::"):
            label = key[5:]
            code = _first(form, key)
            if label in columns:
                columns[label] = code if code in profiles.FIELD_BY_CODE else profiles.KEEP
    single = [s.code for s in profiles.FIELDS if not s.multiple]
    for code in single:
        chosen = [label for label, value in columns.items() if value == code]
        if len(chosen) > 1:
            errors["colonnes"] = _("« {field} » ne peut recevoir qu'une colonne ; ici : {cols}.").format(
                field=profiles.FIELD_BY_CODE[code].label, cols=", ".join(chosen)
            )
    current.columns = columns
    for key in form:
        if key.startswith("etat::"):
            value = _first(form, key)
            current.condition_map[key[6:]] = value if value in profiles.CONDITIONS else ""
        elif key.startswith("collection::"):
            value = _first(form, key)
            current.collection_map[key[12:]] = value if value in profiles.COLLECTION_KINDS else "collection"
        elif key.startswith("canon::"):
            value = _first(form, key)
            if value:
                current.collection_canonical[key[7:]] = value
            else:
                current.collection_canonical.pop(key[7:], None)
        elif key.startswith("lien::"):
            value = _first(form, key)
            if value.isdigit():
                current.collection_links[key[6:]] = int(value)
            else:
                current.collection_links.pop(key[6:], None)
    roles = _first(form, "roles")
    current.roles = roles if roles in profiles.ROLE_MODES else current.roles
    barcode = _first(form, "codes_barres")
    current.barcode = barcode if barcode in profiles.BARCODE_MODES else current.barcode
    if current.barcode == "column" and "barcode" not in columns.values():
        errors["codes_barres"] = _("Associez d'abord une colonne à « Code-barres déjà attribué ».")
    prefix = _first(form, "prefixe").strip()
    if prefix:
        if not prefix.replace("-", "").isalnum() or len(prefix) > 12:
            errors["prefixe"] = _("Préfixe de 12 caractères au plus : lettres, chiffres et tirets.")
        else:
            current.barcode_prefix = prefix
    if "inventory" not in columns.values():
        errors.setdefault(
            "colonnes", _("Associez une colonne au numéro d'inventaire : c'est l'identité de chaque exemplaire.")
        )
    return current, errors


@router.post("/imports/{token}", include_in_schema=False)
def mapping_save(request: Request, token: str, auth: CanSimulate, form: Form) -> Response:
    deposit = _deposit(request, token)
    action = _first(form, "action")
    if action == "abandonner":
        store.discard(_data_dir(request), deposit.token)
        return _go(request, "/imports", "import_discarded")
    sheet = _first(form, "feuille")
    if sheet and sheet in deposit.sheets and sheet != deposit.sheet:
        deposit.sheet = sheet
        store.save(_data_dir(request), deposit)
        return _go(request, f"/imports/{token}")
    profile, errors = _profile_from_form(form, deposit)
    deposit.profile = profile.to_json()
    store.save(_data_dir(request), deposit)
    if errors:
        return _mapping_page(request, deposit, errors=errors, status=400)
    if action == "profil":
        name = " ".join(_first(form, "nom_profil").split())[:80]
        if not name:
            return _mapping_page(request, deposit, errors={"nom_profil": _("Donnez un nom au profil.")}, status=400)
        with request.app.state.database.session() as session:
            saved = session.scalar(select(ImportProfile).where(ImportProfile.name == name))
            stamp = now_iso()
            if saved is None:
                saved = ImportProfile(
                    name=name, target="catalogue", mapping=profile.to_json(), created_at=stamp, updated_at=stamp,
                    created_by=auth.user_id,
                )  # fmt: skip
                session.add(saved)
            else:
                saved.mapping, saved.updated_at = profile.to_json(), stamp
            session.flush()
            audit.record(session, "import.profile_saved", "import_profile", saved.id, user_id=auth.user_id)
            session.commit()
        deposit.profile_name = name
        store.save(_data_dir(request), deposit)
        return _go(request, f"/imports/{token}", "import_profile_saved")
    if action == "simuler":
        return _go(request, f"/imports/{token}/simulation")
    return _go(request, f"/imports/{token}", "import_mapping_saved")


@router.post("/imports/profils/{profile_id}/supprimer", include_in_schema=False)
def profile_delete(request: Request, profile_id: int, auth: CanSimulate) -> Response:
    with request.app.state.database.session() as session:
        saved = session.get(ImportProfile, profile_id)
        if saved is None:
            raise HTTPException(status_code=404)
        session.delete(saved)
        audit.record(session, "import.profile_deleted", "import_profile", profile_id, user_id=auth.user_id)
        session.commit()
    return _go(request, "/imports", "import_profile_deleted")


# --- Simulation (T4) -------------------------------------------------------------------------------------------------
def run(request: Request, deposit: store.Deposit) -> simulate.Simulation:
    """Simule ; le catalogue n'est que lu. Les quatre dernières simulations restent en mémoire, pas sur le disque."""
    main, other = store.workbooks(_data_dir(request), deposit)
    with request.app.state.database.session() as session:
        existing = simulate.snapshot(session)
        session.rollback()
    profile = profiles.Profile.from_json(deposit.profile, name=deposit.profile_name)
    key = (deposit.token, deposit.sheet, profile.sha256, existing.fingerprint)
    if key in _SIMULATIONS:
        _SIMULATIONS.move_to_end(key)
        return _SIMULATIONS[key]
    result = simulate.simulate(
        main,
        deposit.sheet,
        profile,
        complement=other,
        complement_sheet=deposit.complement_sheet,
        existing=existing,
    )
    _SIMULATIONS[key] = result
    while len(_SIMULATIONS) > _SIMULATIONS_KEPT:
        _SIMULATIONS.popitem(last=False)
    return result


def _filtered_rows(sim: simulate.Simulation, severity: str, code: str, query: str) -> list[simulate.RowPlan]:
    rows = sim.rows
    if severity in simulate.SEVERITY_ORDER:
        rows = [r for r in rows if r.severity == severity]
    if code in simulate.CODES:
        rows = [r for r in rows if any(m.code == code for m in r.messages)]
    if query:
        needle = query.strip().casefold()
        rows = [
            r
            for r in rows
            if needle in (str(r.number), r.inventory.casefold(), r.fields.get("call_number", "").casefold())
            or needle in " ".join(str(v) for v in r.raw.values() if v is not None).casefold()
        ]
    return rows


@router.get("/imports/{token}/simulation", include_in_schema=False)
def simulation(
    request: Request,
    token: str,
    auth: CanSimulate,
    vue: Annotated[str, Query()] = "lignes",
    gravite: Annotated[str, Query()] = "",
    code: Annotated[str, Query()] = "",
    q: Annotated[str, Query(max_length=120)] = "",
    page: Annotated[int, Query(ge=1)] = 1,
) -> HTMLResponse:
    return _simulation_page(request, auth, token, vue, gravite, code, q, page)


def _simulation_page(
    request: Request,
    auth: AuthContext,
    token: str,
    vue: str = "lignes",
    gravite: str = "",
    code: str = "",
    q: str = "",
    page: int = 1,
    *,
    apply_error: str = "",
    apply_details: list[str] | None = None,
    status: int = 200,
) -> HTMLResponse:
    deposit = _deposit(request, token)
    sim = run(request, deposit)
    with request.app.state.database.session() as session:
        enabled = importer.enabled(session)
    view = vue if vue in VIEWS else "lignes"
    rows = _filtered_rows(sim, gravite, code, q)
    pages = max(1, -(-len(rows) // ROWS_PER_PAGE))
    page = min(page, pages)
    anomalies = sim.anomalies()
    families: dict[str, list[tuple[str, str, str, int]]] = {}
    for found_code, found_rows in sorted(anomalies.items(), key=lambda item: (simulate.CODES[item[0]][2], item[0])):
        severity, label, family = simulate.CODES[found_code]
        families.setdefault(family, []).append((found_code, label, severity, len(found_rows)))
    query = {k: v for k, v in (("vue", view), ("gravite", gravite), ("code", code), ("q", q)) if v}
    context = {
        "deposit": deposit,
        "sim": sim,
        "view": view,
        "views": VIEWS,
        "rows": rows[(page - 1) * ROWS_PER_PAGE : page * ROWS_PER_PAGE],
        "row_total": len(rows),
        "page": page,
        "pages": pages,
        "page_href": f"/imports/{token}/simulation?{urlencode(query)}&page=",
        "severity": gravite,
        "code": code,
        "q": q,
        "families": families,
        "family_labels": simulate.FAMILIES,
        "codes": simulate.CODES,
        "proposal_kinds": simulate.PROPOSAL_KINDS,
        "severity_labels": report.SEVERITY_LABELS,
        "conditions": profiles.CONDITIONS,
        "collection_kinds": profiles.COLLECTION_KINDS,
        "base": f"/imports/{token}",
        "apply_enabled": enabled,
        "may_apply": auth.can("imports.apply"),
        "apply_error": apply_error,
        "apply_details": apply_details or [],
    }
    return _render(request, "imports/pages/simulation.html", context, status)


def _download(data: bytes, filename: str, media_type: str) -> Response:
    return Response(
        data,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )


@router.get("/imports/{token}/rapport.xlsx", include_in_schema=False)
def report_xlsx(request: Request, token: str, _auth: CanSimulate) -> Response:
    sim = run(request, _deposit(request, token))
    return _download(
        report.to_xlsx(sim),
        f"simulation-{sim.file_sha256[:12]}.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@router.get("/imports/{token}/lignes.csv", include_in_schema=False)
def report_csv(request: Request, token: str, _auth: CanSimulate) -> Response:
    sim = run(request, _deposit(request, token))
    return _download(report.to_csv(sim), f"simulation-{sim.file_sha256[:12]}-lignes.csv", "text/csv; charset=utf-8")


# --- Application (T5) ------------------------------------------------------------------------------------------------
@router.post("/imports/{token}/appliquer", include_in_schema=False)
def apply_import(request: Request, token: str, auth: CanApply, form: Form) -> Response:
    """Applique le plan confirmé : la case cochée et l'empreinte lue sont exigées, tout le reste est revérifié."""
    deposit = _deposit(request, token)
    if _first(form, "confirmer") != "1":
        return _simulation_page(
            request, auth, token, apply_error=_("Cochez la case de confirmation pour appliquer ce plan."), status=400
        )
    main, other = store.workbooks(_data_dir(request), deposit)
    plan_request = importer.ApplyRequest(
        workbook=main,
        sheet=deposit.sheet,
        profile=profiles.Profile.from_json(deposit.profile, name=deposit.profile_name),
        expected_plan=_first(form, "plan"),
        user_id=auth.user_id,
        complement=other,
        complement_sheet=deposit.complement_sheet,
    )
    try:
        applied = importer.apply(request.app.state.database, request.app.state.settings, plan_request)
    except importer.ApplyRefused as refused:
        return _simulation_page(
            request, auth, token, apply_error=refused.message, apply_details=refused.details, status=409
        )
    except importer.ApplyFailed as failed:
        return _simulation_page(request, auth, token, apply_error=failed.message, status=500)
    _SIMULATIONS.clear()
    store.discard(_data_dir(request), deposit.token)  # le lot garde tout : lignes, valeurs d'origine, empreintes
    return _go(request, f"/imports/lots/{applied.batch_id}", "import_applied")


def _batch(request: Request, batch_id: int) -> tuple[ImportBatch, dict[str, Any]]:
    with request.app.state.database.session() as session:
        batch = session.get(ImportBatch, batch_id)
        if batch is None:
            raise HTTPException(status_code=404)
        session.expunge(batch)
    return batch, json.loads(batch.summary or "{}")


@router.get("/imports/lots/{batch_id}", include_in_schema=False)
def batch_page(request: Request, batch_id: int, _auth: CanSimulate) -> HTMLResponse:
    batch, summary = _batch(request, batch_id)
    context = {"batch": batch, "summary": summary, "statuses": BATCH_STATUSES}
    return _render(request, "imports/pages/batch.html", context)


@router.get("/imports/lots/{batch_id}/format-origine.xlsx", include_in_schema=False)
def batch_original(request: Request, batch_id: int, auth: CanSimulate) -> Response:
    """Le classeur du lot au format d'origine (T7) : la règle de priorité est dans `original`."""
    batch, _summary = _batch(request, batch_id)
    if batch.status != "applied":
        raise HTTPException(status_code=404)
    with request.app.state.database.session(write=True) as session:
        result = original.original_workbook(session, batch_id)
        audit.record(
            session, "import.export_original", "import_batch", batch_id, user_id=auth.user_id,
            details={"lignes": result.rows, "modifications": len(result.modifications)},
        )  # fmt: skip
    stem = batch.filename.rsplit(".", 1)[0][:60] or "catalogue"
    return _download(result.data, f"{stem}-format-origine.xlsx", XLSX)


# --- Export complet (T6) ---------------------------------------------------------------------------------------------
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _export(request: Request, auth: AuthContext, kind: str) -> bytes:
    with request.app.state.database.session(write=True) as session:
        data = export.to_xlsx(session) if kind == "xlsx" else export.to_csv_zip(session)
        audit.record(
            session, "import.export_catalogue", "catalogue", None, user_id=auth.user_id, details={"format": kind}
        )
    return data


@router.get("/imports/export/catalogue.xlsx", include_in_schema=False)
def export_xlsx(request: Request, auth: CanSimulate) -> Response:
    return _download(_export(request, auth, "xlsx"), f"biblyo-catalogue-{now_iso()[:10]}.xlsx", XLSX)


@router.get("/imports/export/catalogue-csv.zip", include_in_schema=False)
def export_csv(request: Request, auth: CanSimulate) -> Response:
    return _download(_export(request, auth, "csv"), f"biblyo-catalogue-{now_iso()[:10]}-csv.zip", "application/zip")
