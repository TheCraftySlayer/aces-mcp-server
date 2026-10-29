"""
The public BernCo ArcGIS parcel layer: lookups, map links, candidate peers, and the parcel
context added to Assessment_Context_Expert prompts. Read-only public data; no county system
is touched.
"""

import json
import os
import re
from typing import Any
from urllib.parse import quote_plus

import httpx

from aces_common import _extract_label_block, _has_any_word_or_phrase, _log, _read_json_or_text, _safe_json_dumps

# Public Bernalillo County Assessor parcel layer.
# Default is the richer BernCo public MapServer layer. You can override this
# with ARCGIS_PUBLIC_PARCEL_LAYER_URL if GIS publishes a new URL.
ARCGIS_PUBLIC_PARCEL_LAYER_URL = os.getenv(
    "ARCGIS_PUBLIC_PARCEL_LAYER_URL",
    "https://assessormap.bernco.gov/server/rest/services/GIS/Assessor_Parcels_Public/MapServer/0",
).rstrip("/")
ARCGIS_PUBLIC_PARCEL_MAX_RESULTS = int(os.getenv("ARCGIS_PUBLIC_PARCEL_MAX_RESULTS", "10"))

# ArcGIS-only candidate peer search. These are NOT final comparable sales;
# they are parcel peers used to guide HomeHarvest/CAMA/MLS enrichment when the
# public GIS layer lacks living-area and verified sale fields.
ARCGIS_CANDIDATE_PEER_MAX_RESULTS = int(os.getenv("ACES_ARCGIS_CANDIDATE_PEER_MAX_RESULTS", "25"))
ARCGIS_CANDIDATE_PEER_RADIUS_MILES = os.getenv("ACES_ARCGIS_CANDIDATE_PEER_RADIUS_MILES", "1,3,5,10").strip()


# BernCo Assessor Experience Builder app. The data source ID is from the public
# Assessor map URL fragment and is used with OBJECTID to open/select a parcel.
BERNCO_EXPERIENCE_APP_URL = os.getenv(
    "BERNCO_EXPERIENCE_APP_URL",
    "https://assessormap.bernco.gov/portal/apps/experiencebuilder/experience/?id=9757f76e51d048d393c44d6487771bf7",
).rstrip("/")
BERNCO_EXPERIENCE_DATA_SOURCE_ID = os.getenv(
    "BERNCO_EXPERIENCE_DATA_SOURCE_ID",
    "f0093002972e4f1e823c0368ea06cf75-19e83adb259-layer-9-1",
).strip()

# When true, Assessment_Context_Expert HomeHarvest/address/comps tasks are
# pre-enriched with public ArcGIS parcel context before the CustomGPT task is
# submitted. This lets HomeHarvest use the GIS subject parcel as an anchor.
ENRICH_HOMEHARVEST_WITH_ARCGIS = os.getenv("ACES_ENRICH_HOMEHARVEST_WITH_ARCGIS", "true").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}


# ---------------------------------------------------------------------------
# ArcGIS public parcel lookup helpers
# ---------------------------------------------------------------------------

# Use * so the tool works against both the older hosted FeatureServer and the
# richer BernCo MapServer schema. The REST response is normalized before it is
# returned to Copilot, and geometry is disabled by default.
ARCGIS_PUBLIC_PARCEL_OUT_FIELDS = "*"


def _arcgis_request_headers() -> dict[str, str]:
    """Headers that keep BernCo ArcGIS/Cloudflare from treating server-side REST calls as suspicious."""
    return {
        "User-Agent": "ACES-MCP-Server/1.0 (+Bernalillo County Assessor internal staff tool)",
        "Accept": "application/json,text/plain,*/*",
        "Referer": f"{BERNCO_EXPERIENCE_APP_URL}/",
    }


_ARCGIS_STREET_SUFFIX_REPLACEMENTS = {
    "COURT": "CT",
    "DRIVE": "DR",
    "ROAD": "RD",
    "STREET": "ST",
    "AVENUE": "AVE",
    "LANE": "LN",
    "BOULEVARD": "BLVD",
    "PLACE": "PL",
    "CIRCLE": "CIR",
    "TRAIL": "TRL",
    "TERRACE": "TER",
    "HIGHWAY": "HWY",
    "PARKWAY": "PKWY",
}

_ARCGIS_CITY_TRAILING_WORDS = [
    "ALBUQUERQUE",
    "TIJERAS",
    "CEDAR CREST",
    "EDGEWOOD",
    "LOS RANCHOS DE ALBUQUERQUE",
    "LOS RANCHOS",
    "CORRALES",
    "ISLETA",
    "SANDIA PARK",
]


def _clean_arcgis_sql_text(value: str) -> str:
    """
    Normalize text for ArcGIS SQL where clauses: escape single quotes, and drop the LIKE
    wildcards (% and _, plus [ ] on a SQL Server backend) so a search can't be widened
    to every parcel. Addresses, owner names and UPCs don't use those characters.
    """
    cleaned = str(value or "").upper()
    cleaned = cleaned.replace("NEW MEXICO", "NM")
    cleaned = re.sub(r"[,;%_\[\]]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned.replace("'", "''")


def _compact_digits(value: str) -> str:
    return re.sub(r"[^0-9]", "", str(value or ""))


def _normalize_arcgis_situs_from_user_text(search_text: str) -> str:
    """
    Convert a user-entered address into the parcel layer's SITUSADD style.
    Example: '2 Lauren Taylor Court Tijeras NM 87059' -> '2 LAUREN TAYLOR CT'.

    This intentionally stops at the street suffix so typos in the city, such as
    'Tijersa', do not get included in the exact SITUSADD query.
    """
    value = _clean_arcgis_sql_text(search_text)
    if not re.match(r"^\d+\s+", value):
        return ""

    suffix_values = (
        set(_ARCGIS_STREET_SUFFIX_REPLACEMENTS.keys()) | set(_ARCGIS_STREET_SUFFIX_REPLACEMENTS.values()) | {"WAY"}
    )
    suffix_pattern = "|".join(sorted((re.escape(s) for s in suffix_values), key=len, reverse=True))

    # Prefer the first complete street-address span: house number + street + suffix.
    match = re.search(rf"\b(\d{{1,6}}\s+[A-Z0-9 .'-]{{1,90}}?\s+(?:{suffix_pattern}))\b", value)
    if match:
        value = match.group(1).strip()
    else:
        # Fallback: drop ZIP/state/city from the right side.
        value = re.sub(r"\s+\d{5}(?:-\d{4})?\s*$", "", value).strip()
        value = re.sub(r"\s+NM\s*$", "", value).strip()
        for city in sorted(_ARCGIS_CITY_TRAILING_WORDS, key=len, reverse=True):
            if value.endswith(" " + city):
                value = value[: -len(city)].strip()
                break

    parts = value.split()
    if len(parts) >= 2:
        last = parts[-1]
        if last in _ARCGIS_STREET_SUFFIX_REPLACEMENTS:
            parts[-1] = _ARCGIS_STREET_SUFFIX_REPLACEMENTS[last]
        value = " ".join(parts)

    return value.strip()


def _arcgis_where_candidates(search_text: str) -> list[tuple[str, str]]:
    """
    Return query candidates in safest order: UPC exact, SITUSADD exact,
    full-address prefix, then broad fallback.
    """
    raw = str(search_text or "").strip()
    safe = _clean_arcgis_sql_text(raw)
    compact = _compact_digits(raw)
    candidates: list[tuple[str, str]] = []

    if re.fullmatch(r"[0-9]{12,30}", compact):
        # UPC exists in both public layers. TXTUPC/PIN exist in the richer BernCo MapServer.
        candidates.append(("upc_exact", f"UPC = '{compact}' OR TXTUPC = '{compact}' OR PIN = '{compact}'"))

    situs = _normalize_arcgis_situs_from_user_text(raw)
    if situs:
        # SITUSADD exists in both public layers. Avoid CompleteSiteAddress here
        # because the richer MapServer does not expose that field.
        candidates.append(("situs_exact", f"SITUSADD = '{situs}'"))
        candidates.append(("situs_prefix", f"SITUSADD LIKE '{situs}%'"))

    if safe:
        # Use only fields that exist in the richer public MapServer.
        candidates.append(
            (
                "broad_text",
                " OR ".join(
                    [
                        f"SITUSADD LIKE '%{safe}%'",
                        f"SITUSADD2 LIKE '%{safe}%'",
                        f"OWNER LIKE '%{safe}%'",
                        f"UPC LIKE '%{safe}%'",
                        f"LEGALDESC LIKE '%{safe}%'",
                    ]
                ),
            )
        )

    # De-dupe while preserving order.
    seen = set()
    unique: list[tuple[str, str]] = []
    for mode, where in candidates:
        if where not in seen:
            seen.add(where)
            unique.append((mode, where))
    return unique


def _compact_none_dict(value: dict[str, Any]) -> dict[str, Any] | None:
    """Return a dict without empty/None values, or None if nothing remains."""
    cleaned = {key: val for key, val in (value or {}).items() if val is not None and val != ""}
    return cleaned or None


def _normalize_arcgis_parcel(attrs: dict[str, Any]) -> dict[str, Any]:
    owner_address = attrs.get("CompleteOwnerAddress") or " ".join(
        part for part in [attrs.get("OWNADD"), attrs.get("OWNADD2")] if part
    )
    situs_address = attrs.get("CompleteSiteAddress") or " ".join(
        part for part in [attrs.get("SITUSADD"), attrs.get("SITUSADD2")] if part
    )

    acreage = attrs.get("ACREAGE")
    if acreage is None:
        acreage = attrs.get("INTACRES")
    if acreage is None:
        acreage = attrs.get("PAR_CALCAC")

    year_built = attrs.get("DWEL_YRBLT") or attrs.get("COM_YRBLT")

    assessment_values = _compact_none_dict(
        {
            "land_value": attrs.get("LANDVALUE"),
            "ag_value": attrs.get("AGVALUE"),
            "improvement_value": attrs.get("IMPTVALUE"),
            "total_value": attrs.get("TOTVALUE"),
            "land_taxable": attrs.get("LANDTXBLE"),
            "improvement_taxable": attrs.get("IMPTTXBLE"),
            "total_taxable": attrs.get("TOTTXBLE"),
            "net_taxable": attrs.get("NETTAXABLE"),
        }
    )

    exemptions = _compact_none_dict(
        {
            "head_of_household": attrs.get("HOHEXEMP"),
            "veteran": attrs.get("VETEXEMP"),
            "other": attrs.get("OTHEREXEMP"),
            "total": attrs.get("TOTALEXEMP"),
        }
    )

    situs_components = _compact_none_dict(
        {
            "number": attrs.get("SITUSNUM"),
            "street": attrs.get("SITUSSTR"),
            "street_type": attrs.get("SITUSSTRTY"),
            "direction": attrs.get("SITUSDIREC"),
            "city": attrs.get("SITUSCITY"),
            "state": attrs.get("SITUSSTATE"),
            "zip": attrs.get("SITUSZIP"),
            "zip4": attrs.get("SITUSZIP2"),
        }
    )

    owner_components = _compact_none_dict(
        {
            "house_number": attrs.get("OWNHSENUM"),
            "sub_number": attrs.get("OWNSUBNUM"),
            "address_direction": attrs.get("OWNADDIR"),
            "street": attrs.get("OWNSTR"),
            "street_type": attrs.get("OWNSTRTYPE"),
            "direction": attrs.get("OWNDIRECT"),
            "box": attrs.get("OWNBOX"),
            "unit": attrs.get("OWNUNIT"),
            "unit_number": attrs.get("OWNUNITNO"),
            "city": attrs.get("OWNCITY"),
            "state": attrs.get("OWNSTATE"),
            "country": attrs.get("OWNCOUNTRY"),
            "zip": attrs.get("OWNZIPCODE"),
            "zip4": attrs.get("OWNZIP4"),
            "code": attrs.get("OWNCODE"),
        }
    )

    coordinates = _compact_none_dict(
        {
            "x": attrs.get("X_Coord"),
            "y": attrs.get("Y_Coord"),
        }
    )

    return {
        "object_id": attrs.get("OBJECTID"),
        "oid": attrs.get("OID"),
        "upc": attrs.get("UPC") or attrs.get("TXTUPC"),
        "txt_upc": attrs.get("TXTUPC"),
        "tax_year": attrs.get("TAXYR") or attrs.get("INTTAXYR"),
        "int_tax_year": attrs.get("INTTAXYR"),
        "pin": attrs.get("PIN"),
        "pid": attrs.get("PID"),
        "tid": attrs.get("TID"),
        "owner": attrs.get("OWNER"),
        "owner_address": owner_address or None,
        "owner_components": owner_components,
        "situs_address": situs_address or None,
        "situs_components": situs_components,
        "tax_district": attrs.get("TAXDIST"),
        "legal_description": attrs.get("LEGALDESC"),
        "document_number": attrs.get("DOCNUM"),
        "roll_type": attrs.get("ROLLTYPE"),
        "valuation_class": attrs.get("VALCLASS"),
        "property_class": attrs.get("PROPCLASS"),
        "land_use_code": attrs.get("LUC"),
        "land_use_description": attrs.get("LUC_MSG"),
        "class_description": attrs.get("C_DESCR"),
        "style": attrs.get("STYLE"),
        "year_built": year_built,
        "dwelling_year_built": attrs.get("DWEL_YRBLT"),
        "commercial_year_built": attrs.get("COM_YRBLT"),
        "acreage": acreage,
        "calculated_acres": attrs.get("PAR_CALCAC"),
        "int_acres": attrs.get("INTACRES"),
        "job_type": attrs.get("INTJOBTYPE"),
        "condominium": attrs.get("TXTCONDOMI"),
        "building": attrs.get("TXTBLDG"),
        "unit": attrs.get("TXTUNIT"),
        "floor": attrs.get("TXTFLR"),
        "duplicate_flag": attrs.get("DUPL"),
        "parcel_type": attrs.get("INTTYPE"),
        "coordinates": coordinates,
        "assessment_values": assessment_values,
        "exemptions": exemptions,
    }


def _fmt_arcgis_number(value: Any) -> str:
    if value is None or value == "":
        return ""
    try:
        number = float(value)
        if number.is_integer():
            return f"{int(number):,}"
        return f"{number:,.4f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError, OverflowError):
        return str(value)


def _fmt_arcgis_money(value: Any) -> str:
    if value is None or value == "":
        return ""
    try:
        return f"${float(value):,.0f}"
    except (TypeError, ValueError, OverflowError):
        return str(value)


def _build_bernco_assessor_map_link(parcel: dict[str, Any]) -> str:
    """Build a BernCo Assessor Experience Builder link that selects the parcel by OBJECTID."""
    object_id = parcel.get("object_id") or parcel.get("oid")
    if not object_id or not BERNCO_EXPERIENCE_DATA_SOURCE_ID or not BERNCO_EXPERIENCE_APP_URL:
        return ""

    return (
        f"{BERNCO_EXPERIENCE_APP_URL}"
        f"#data_s=id%3A{BERNCO_EXPERIENCE_DATA_SOURCE_ID}%3A{object_id}"
        f"&zoom_to_selection=true"
    )


def _build_google_maps_routing_links(parcel: dict[str, Any]) -> dict[str, str]:
    """Build Google Maps search/directions URLs from the public GIS situs address."""
    situs = parcel.get("situs_address") or ""
    if not situs:
        return {}

    destination = quote_plus(f"{situs}, Bernalillo County, NM")
    return {
        "google_maps_directions": (
            f"https://www.google.com/maps/dir/?api=1&destination={destination}&travelmode=driving"
        ),
        "google_maps_search": (f"https://www.google.com/maps/search/?api=1&query={destination}"),
    }


def _format_arcgis_map_routing_text(result: dict[str, Any]) -> str:
    """Format BernCo Assessor map and Google Maps routing links for staff."""
    status = str(result.get("status") or "")
    answer = str(result.get("answer") or "").strip()
    if status != "completed":
        return answer or "No matching public ArcGIS parcel record was found."

    lines = [
        answer,
        "Source: Bernalillo County Assessor Parcels public ArcGIS layer.",
        "Limit: Map/location support only. This is not a certified record, final appraisal, tax/legal decision, or routing guarantee.",
        "Note: BernCo Assessor Map opens the public Assessor map and selects the parcel by OBJECTID. Google Maps uses the public GIS situs address for driving directions.",
        "",
    ]

    for idx, item in enumerate(result.get("results") or [], start=1):
        bernco_map = _build_bernco_assessor_map_link(item)
        google_links = _build_google_maps_routing_links(item)

        lines.extend(
            [
                f"{idx}. {item.get('situs_address') or 'Unknown situs address'}",
                f"   UPC/PIN: {item.get('upc') or ''} / {item.get('pin') or ''}",
                f"   Owner: {item.get('owner') or ''}",
                f"   OBJECTID: {item.get('object_id') or ''}",
            ]
        )

        if bernco_map:
            lines.append(f"   BernCo Assessor Map: {bernco_map}")
        if google_links.get("google_maps_directions"):
            lines.append(f"   Google Maps Directions: {google_links['google_maps_directions']}")
        if google_links.get("google_maps_search"):
            lines.append(f"   Google Maps Search: {google_links['google_maps_search']}")

        lines.append("")

    return "\n".join(lines).strip()


def _format_arcgis_parcel_lookup_text(result: dict[str, Any]) -> str:
    status = str(result.get("status") or "")
    answer = str(result.get("answer") or "").strip()
    if status != "completed":
        return answer or "ArcGIS parcel lookup did not return usable results."

    lines = [
        answer,
        "Source: Bernalillo County Assessor Parcels public ArcGIS layer.",
        "Limit: Public GIS parcel context only. Verify final assessment details, value fields, exemption amount fields, ownership, and tax status in iasWorld.",
        "Note: Value, taxable, and exemption amount fields are public GIS attributes only; they are not verified sale prices, final tax amounts, exemption approvals, or legal determinations.",
        "",
    ]

    for idx, item in enumerate(result.get("results") or [], start=1):
        values = item.get("assessment_values") or {}
        exemptions = item.get("exemptions") or {}
        coords = item.get("coordinates") or {}

        lines.extend(
            [
                f"{idx}. {item.get('situs_address') or 'Unknown situs address'}",
                f"   UPC/PIN: {item.get('upc') or ''} / {item.get('pin') or ''}",
                f"   Tax Year: {item.get('tax_year') or ''}",
                f"   Owner: {item.get('owner') or ''}",
                f"   Owner Address: {item.get('owner_address') or ''}",
                f"   Legal Description: {item.get('legal_description') or ''}",
                f"   Roll/Class: {item.get('roll_type') or ''} / {item.get('valuation_class') or ''} / {item.get('property_class') or ''}",
                f"   Tax District / Doc: {item.get('tax_district') or ''} / {item.get('document_number') or ''}",
                f"   Land Use: {item.get('land_use_code') or ''} {item.get('land_use_description') or ''}",
                f"   Class/Style: {item.get('class_description') or ''} / {item.get('style') or ''}",
                f"   Built/Acres: {item.get('year_built') or ''} / {_fmt_arcgis_number(item.get('acreage'))}",
            ]
        )

        value_rows = [
            ("Land Value", values.get("land_value")),
            ("Agricultural Value", values.get("ag_value")),
            ("Improvement Value", values.get("improvement_value")),
            ("Total Value", values.get("total_value")),
            ("Land Taxable", values.get("land_taxable")),
            ("Improvement Taxable", values.get("improvement_taxable")),
            ("Total Taxable", values.get("total_taxable")),
            ("Net Taxable", values.get("net_taxable")),
        ]
        value_rows = [(label, value) for label, value in value_rows if value is not None and value != ""]
        if value_rows:
            lines.append("   Public GIS Value Fields:")
            for label, value in value_rows:
                lines.append(f"      {label}: {_fmt_arcgis_money(value)}")

        exemption_rows = [
            ("Head of Household Amount", exemptions.get("head_of_household")),
            ("Veteran Amount", exemptions.get("veteran")),
            ("Other Amount", exemptions.get("other")),
            ("Total Exemption Amount", exemptions.get("total")),
        ]
        exemption_rows = [(label, value) for label, value in exemption_rows if value is not None and value != ""]
        if exemption_rows:
            lines.append("   Public GIS Exemption Amount Fields:")
            for label, value in exemption_rows:
                lines.append(f"      {label}: {_fmt_arcgis_money(value)}")

        if value_rows or exemption_rows:
            lines.append(
                "   Verification Note: Confirm value fields, exemption status/amounts, calculations, and taxability in iasWorld before relying on them."
            )

        lines.extend(
            [
                f"   X/Y: {_fmt_arcgis_number(coords.get('x'))} / {_fmt_arcgis_number(coords.get('y'))}",
                f"   OBJECTID: {item.get('object_id') or ''}",
                "",
            ]
        )

    return "\n".join(lines).strip()


async def _arcgis_public_parcel_lookup_result(
    search_text: str,
    max_results: int = ARCGIS_PUBLIC_PARCEL_MAX_RESULTS,
    return_geometry: bool = False,
) -> dict[str, Any]:
    """Query the public BernCo parcel FeatureServer layer with exact-first fallback."""
    search_text = str(search_text or "").strip()
    if len(search_text) < 3:
        return {
            "status": "failed",
            "answer": "Provide an address, UPC, parcel ID, or owner name with at least 3 characters.",
            "task_id": "",
            "project_id": "",
            "source": "Bernalillo County Assessor Parcels public ArcGIS layer",
            "results": [],
        }

    try:
        max_results_int = max(1, min(int(max_results), 25))
    except (TypeError, ValueError, OverflowError):
        max_results_int = ARCGIS_PUBLIC_PARCEL_MAX_RESULTS

    candidates = _arcgis_where_candidates(search_text)
    if not candidates:
        return {
            "status": "failed",
            "answer": "Could not build an ArcGIS query from the supplied search text.",
            "task_id": "",
            "project_id": "",
            "source": "Bernalillo County Assessor Parcels public ArcGIS layer",
            "results": [],
        }

    timeout = httpx.Timeout(connect=10, read=25, write=20, pool=10)
    last_error: Any | None = None

    async with httpx.AsyncClient(timeout=timeout) as client:
        for query_mode, where_clause in candidates:
            params = {
                "f": "json",
                "where": where_clause,
                "outFields": ARCGIS_PUBLIC_PARCEL_OUT_FIELDS,
                "returnGeometry": "true" if return_geometry else "false",
                "resultRecordCount": str(max_results_int),
            }
            if return_geometry:
                # Layer coordinates are WKID 2903; use WGS84 when geometry is requested.
                params["outSR"] = "4326"

            try:
                response = await client.post(
                    f"{ARCGIS_PUBLIC_PARCEL_LAYER_URL}/query", data=params, headers=_arcgis_request_headers()
                )
                data = await _read_json_or_text(response)
            except httpx.HTTPError as exc:
                last_error = str(exc)
                continue

            if response.status_code >= 400:
                last_error = {"http_status": response.status_code, "response": data}
                continue

            if isinstance(data, dict) and data.get("error"):
                last_error = data.get("error")
                continue

            features = data.get("features", []) if isinstance(data, dict) else []
            if not features:
                continue

            results = [_normalize_arcgis_parcel(feature.get("attributes", {}) or {}) for feature in features]
            count = len(results)
            exact_note = "" if count == 1 else " Broad or fallback search returned multiple possible matches."
            transfer_note = (
                " ArcGIS indicated there may be more matches than returned."
                if isinstance(data, dict) and data.get("exceededTransferLimit")
                else ""
            )
            return {
                "status": "completed",
                "answer": (
                    f"Found {count} public ArcGIS parcel match(es). "
                    "Verify final assessment details in iasWorld before relying on them."
                    f"{exact_note}{transfer_note}"
                ).strip(),
                "task_id": "",
                "project_id": "",
                "source": "Bernalillo County Assessor Parcels public ArcGIS layer",
                "layer_url": ARCGIS_PUBLIC_PARCEL_LAYER_URL,
                "query_mode": query_mode,
                "where": where_clause,
                "count": count,
                "results": results,
            }

    if last_error:
        return {
            "status": "failed",
            "answer": f"ArcGIS REST lookup failed or returned an error: {_safe_json_dumps(last_error, 1200)}",
            "task_id": "",
            "project_id": "",
            "source": "Bernalillo County Assessor Parcels public ArcGIS layer",
            "results": [],
        }

    return {
        "status": "empty",
        "answer": "No matching public ArcGIS parcel records were found.",
        "task_id": "",
        "project_id": "",
        "source": "Bernalillo County Assessor Parcels public ArcGIS layer",
        "query_mode": candidates[-1][0] if candidates else "none",
        "where": candidates[-1][1] if candidates else "",
        "results": [],
    }


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _arcgis_candidate_radius_values() -> list[float]:
    values: list[float] = []
    for raw in re.split(r"[,;\s]+", ARCGIS_CANDIDATE_PEER_RADIUS_MILES or ""):
        try:
            miles = float(raw)
        except ValueError:
            continue
        if miles > 0:
            values.append(miles)
    return values or [1.0, 3.0, 5.0, 10.0]


def _arcgis_sql_equals(field: str, value: Any) -> str:
    return f"{field} = '{_clean_arcgis_sql_text(str(value))}'"


def _arcgis_candidate_where_stages(subject: dict[str, Any]) -> list[tuple[str, str]]:
    """
    Build progressively broader, Cloudflare-safe where clauses for ArcGIS-only
    parcel peers. Do NOT include SQL exclusion logic such as UPC <> subject,
    OBJECTID <>, IS NULL, or OR here. Those are filtered client-side after the
    ArcGIS response. Keeping these as simple equality-only clauses avoids WAF
    blocks while still limiting the candidate pool.
    """
    tax_year = subject.get("tax_year") or subject.get("int_tax_year")
    roll_type = subject.get("roll_type")
    prop_class = subject.get("property_class")
    val_class = subject.get("valuation_class")
    luc = subject.get("land_use_code")
    tax_district = subject.get("tax_district")
    style = subject.get("style")

    base: list[str] = []
    if tax_year:
        base.append(_arcgis_sql_equals("TAXYR", tax_year))

    def build(extra: list[str]) -> str:
        clauses = base + [item for item in extra if item]
        return " AND ".join(clauses) if clauses else "1=1"

    stages: list[tuple[str, str]] = []

    tight: list[str] = []
    if roll_type:
        tight.append(_arcgis_sql_equals("ROLLTYPE", roll_type))
    if prop_class:
        tight.append(_arcgis_sql_equals("PROPCLASS", prop_class))
    if val_class:
        tight.append(_arcgis_sql_equals("VALCLASS", val_class))
    if luc:
        tight.append(_arcgis_sql_equals("LUC", luc))
    if style:
        tight.append(_arcgis_sql_equals("STYLE", style))
    if tax_district:
        tight.append(_arcgis_sql_equals("TAXDIST", tax_district))
    if tight:
        stages.append(("same_class_use_style_taxdist", build(tight)))

    strong: list[str] = []
    if roll_type:
        strong.append(_arcgis_sql_equals("ROLLTYPE", roll_type))
    if prop_class:
        strong.append(_arcgis_sql_equals("PROPCLASS", prop_class))
    if luc:
        strong.append(_arcgis_sql_equals("LUC", luc))
    if strong:
        stages.append(("same_roll_propclass_luc", build(strong)))

    moderate: list[str] = []
    if roll_type:
        moderate.append(_arcgis_sql_equals("ROLLTYPE", roll_type))
    if prop_class:
        moderate.append(_arcgis_sql_equals("PROPCLASS", prop_class))
    if moderate:
        stages.append(("same_roll_propclass", build(moderate)))

    broad: list[str] = []
    if roll_type:
        broad.append(_arcgis_sql_equals("ROLLTYPE", roll_type))
    if broad:
        stages.append(("same_roll_type", build(broad)))

    stages.append(("nearby_public_parcels", build([])))

    seen = set()
    unique: list[tuple[str, str]] = []
    for name, where in stages:
        if where not in seen:
            seen.add(where)
            unique.append((name, where))
    return unique


def _arcgis_candidate_peer_score(subject: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """Score public GIS candidate peers using only fields available in ArcGIS."""
    score = 0.0
    reasons: list[str] = []

    def same(field: str, label: str, points: float) -> None:
        nonlocal score
        left = str(subject.get(field) or "").strip().upper()
        right = str(candidate.get(field) or "").strip().upper()
        if left and right and left == right:
            score += points
            reasons.append(label)

    same("roll_type", "same roll type", 8)
    same("property_class", "same property class", 18)
    same("valuation_class", "same valuation class", 10)
    same("land_use_code", "same land use code", 22)
    same("tax_district", "same tax district", 8)
    same("style", "same style", 10)

    subject_year = _float_or_none(subject.get("year_built"))
    candidate_year = _float_or_none(candidate.get("year_built"))
    if subject_year is not None and candidate_year is not None:
        diff = abs(subject_year - candidate_year)
        if diff <= 5:
            score += 12
            reasons.append("year built within 5 years")
        elif diff <= 10:
            score += 9
            reasons.append("year built within 10 years")
        elif diff <= 20:
            score += 5
            reasons.append("year built within 20 years")

    subject_acres = _float_or_none(subject.get("acreage"))
    candidate_acres = _float_or_none(candidate.get("acreage"))
    if subject_acres and candidate_acres:
        ratio_diff = abs(candidate_acres - subject_acres) / max(subject_acres, 0.01)
        if ratio_diff <= 0.20:
            score += 10
            reasons.append("acreage within 20%")
        elif ratio_diff <= 0.50:
            score += 6
            reasons.append("acreage within 50%")
        elif ratio_diff <= 1.00:
            score += 3
            reasons.append("acreage within 100%")

    subject_coords = subject.get("coordinates") or {}
    cand_coords = candidate.get("coordinates") or {}
    sx = _float_or_none(subject_coords.get("x"))
    sy = _float_or_none(subject_coords.get("y"))
    cx = _float_or_none(cand_coords.get("x"))
    cy = _float_or_none(cand_coords.get("y"))
    distance_miles: float | None = None
    if sx is not None and sy is not None and cx is not None and cy is not None:
        distance_miles = (((cx - sx) ** 2 + (cy - sy) ** 2) ** 0.5) / 5280.0
        if distance_miles <= 1:
            score += 12
            reasons.append("within 1 mile")
        elif distance_miles <= 3:
            score += 9
            reasons.append("within 3 miles")
        elif distance_miles <= 5:
            score += 6
            reasons.append("within 5 miles")
        elif distance_miles <= 10:
            score += 3
            reasons.append("within 10 miles")

    return {
        "candidate_score": round(min(score, 100.0), 1),
        "distance_miles": round(distance_miles, 3) if distance_miles is not None else None,
        "match_reasons": reasons[:12],
        "missing_for_final_comp": [
            "living/building square footage from an official or approved source",
            "verified sale date",
            "verified sale price",
        ],
    }


async def _arcgis_public_candidate_peers_result(
    search_text: str,
    max_results: int = 10,
) -> dict[str, Any]:
    """
    Find ArcGIS-only candidate parcel peers around a subject.

    This is intentionally a comp-triage helper, not a comparable-sales tool.
    The public ArcGIS layer lacks living-area and verified sale fields, so the
    output must be enriched with HomeHarvest/CAMA/MLS before final comp use.
    """
    try:
        max_results_int = max(1, min(int(max_results), ARCGIS_CANDIDATE_PEER_MAX_RESULTS))
    except (TypeError, ValueError, OverflowError):
        max_results_int = 10

    subject_lookup = await _arcgis_public_parcel_lookup_result(
        search_text=search_text,
        max_results=5,
        return_geometry=False,
    )
    if subject_lookup.get("status") != "completed" or int(subject_lookup.get("count") or 0) != 1:
        return {
            "status": "needs_subject_selection"
            if subject_lookup.get("status") == "completed"
            else subject_lookup.get("status", "empty"),
            "answer": (
                "ArcGIS candidate peer search needs exactly one subject parcel. "
                f"Subject lookup status: {subject_lookup.get('status')}; count: {subject_lookup.get('count', 0)}."
            ),
            "task_id": "",
            "project_id": "",
            "source": "Bernalillo County Assessor Parcels public ArcGIS layer",
            "subject_lookup": subject_lookup,
            "subject": None,
            "peers": [],
        }

    subject = (subject_lookup.get("results") or [None])[0]
    if not isinstance(subject, dict):
        return {
            "status": "failed",
            "answer": "ArcGIS subject lookup did not return a usable normalized subject parcel.",
            "task_id": "",
            "project_id": "",
            "source": "Bernalillo County Assessor Parcels public ArcGIS layer",
            "subject_lookup": subject_lookup,
            "subject": None,
            "peers": [],
        }

    coords = subject.get("coordinates") or {}
    sx = _float_or_none(coords.get("x"))
    sy = _float_or_none(coords.get("y"))
    if sx is None or sy is None:
        return {
            "status": "failed",
            "answer": "ArcGIS subject parcel did not include X_Coord/Y_Coord, so distance-based candidate peer search could not run.",
            "task_id": "",
            "project_id": "",
            "source": "Bernalillo County Assessor Parcels public ArcGIS layer",
            "subject_lookup": subject_lookup,
            "subject": subject,
            "peers": [],
        }

    timeout = httpx.Timeout(connect=10, read=30, write=20, pool=10)
    stages = _arcgis_candidate_where_stages(subject)
    radii = _arcgis_candidate_radius_values()
    last_error: Any | None = None

    async with httpx.AsyncClient(timeout=timeout) as client:
        for radius_miles in radii:
            for stage_name, where_clause in stages:
                params = {
                    "f": "json",
                    "where": where_clause,
                    "outFields": ARCGIS_PUBLIC_PARCEL_OUT_FIELDS,
                    "returnGeometry": "false",
                    "resultRecordCount": str(max(50, max_results_int * 5)),
                    "geometry": json.dumps({"x": sx, "y": sy, "spatialReference": {"wkid": 2903}}),
                    "geometryType": "esriGeometryPoint",
                    "inSR": "2903",
                    "spatialRel": "esriSpatialRelIntersects",
                    "distance": str(float(radius_miles) * 5280.0),
                    "units": "esriSRUnit_Foot",
                }
                try:
                    response = await client.post(
                        f"{ARCGIS_PUBLIC_PARCEL_LAYER_URL}/query", data=params, headers=_arcgis_request_headers()
                    )
                    data = await _read_json_or_text(response)
                except httpx.HTTPError as exc:
                    last_error = str(exc)
                    continue

                if response.status_code >= 400:
                    last_error = {"http_status": response.status_code, "response": data}
                    continue
                if isinstance(data, dict) and data.get("error"):
                    last_error = data.get("error")
                    continue

                features = data.get("features", []) if isinstance(data, dict) else []
                peers: list[dict[str, Any]] = []
                seen_keys = set()
                subject_upc = str(subject.get("upc") or subject.get("txt_upc") or "").strip()
                subject_oid = str(subject.get("object_id") or subject.get("oid") or "").strip()

                for feature in features:
                    peer = _normalize_arcgis_parcel(feature.get("attributes", {}) or {})
                    peer_upc = str(peer.get("upc") or peer.get("txt_upc") or "").strip()
                    peer_oid = str(peer.get("object_id") or peer.get("oid") or "").strip()
                    if subject_upc and peer_upc and peer_upc == subject_upc:
                        continue
                    if subject_oid and peer_oid and peer_oid == subject_oid:
                        continue
                    key = peer_upc or peer_oid or peer.get("situs_address")
                    if not key or key in seen_keys:
                        continue
                    seen_keys.add(key)
                    peer.update(_arcgis_candidate_peer_score(subject, peer))
                    peers.append(peer)

                if peers:
                    peers.sort(
                        key=lambda item: (item.get("candidate_score") or 0, -1 * (item.get("distance_miles") or 999)),
                        reverse=True,
                    )
                    selected = peers[:max_results_int]
                    return {
                        "status": "completed",
                        "answer": (
                            f"Found {len(selected)} ArcGIS candidate parcel peer(s) within {radius_miles:g} mile(s) "
                            f"using stage '{stage_name}'. These are not final comparable sales because ArcGIS does not provide living area or verified sale fields."
                        ),
                        "task_id": "",
                        "project_id": "",
                        "source": "Bernalillo County Assessor Parcels public ArcGIS layer",
                        "layer_url": ARCGIS_PUBLIC_PARCEL_LAYER_URL,
                        "subject_lookup": subject_lookup,
                        "subject": subject,
                        "radius_miles": radius_miles,
                        "query_stage": stage_name,
                        "where": where_clause,
                        "count": len(selected),
                        "peers": selected,
                    }

    if last_error:
        return {
            "status": "failed",
            "answer": f"ArcGIS candidate peer search failed or returned an error: {_safe_json_dumps(last_error, 1200)}",
            "task_id": "",
            "project_id": "",
            "source": "Bernalillo County Assessor Parcels public ArcGIS layer",
            "subject_lookup": subject_lookup,
            "subject": subject,
            "peers": [],
        }

    return {
        "status": "empty",
        "answer": "No ArcGIS candidate parcel peers were found in the configured search radii.",
        "task_id": "",
        "project_id": "",
        "source": "Bernalillo County Assessor Parcels public ArcGIS layer",
        "subject_lookup": subject_lookup,
        "subject": subject,
        "peers": [],
    }


def _format_arcgis_candidate_peers_text(result: dict[str, Any]) -> str:
    status = str(result.get("status") or "")
    answer = str(result.get("answer") or "").strip()
    lines = [
        answer or "ArcGIS candidate peer search did not return usable results.",
        "Source: Bernalillo County Assessor Parcels public ArcGIS layer.",
        "Limit: ArcGIS-only candidate parcel peers. These are not final comparable sales because the public GIS layer does not include living area, verified sale date, or verified sale price.",
        "Use: Enrich with HomeHarvest/public aggregator data and verify final use in iasWorld/CAMA, MLS, deed/sales records, or another office-approved source.",
        "",
    ]

    subject = result.get("subject") or {}
    if isinstance(subject, dict) and subject:
        lines.extend(
            [
                "Subject Anchor",
                f"   Situs: {subject.get('situs_address') or ''}",
                f"   UPC/PIN: {subject.get('upc') or ''} / {subject.get('pin') or ''}",
                f"   Class/LUC/Style: {subject.get('property_class') or ''} / {subject.get('land_use_code') or ''} {subject.get('land_use_description') or ''} / {subject.get('style') or ''}",
                f"   Built/Acres: {subject.get('year_built') or ''} / {_fmt_arcgis_number(subject.get('acreage'))}",
                "",
            ]
        )

    if status == "completed":
        for idx, item in enumerate(result.get("peers") or [], start=1):
            reasons = "; ".join(item.get("match_reasons") or [])
            lines.extend(
                [
                    f"{idx}. {item.get('situs_address') or 'Unknown situs address'}",
                    f"   Candidate Score: {item.get('candidate_score')}/100 | Distance: {_fmt_arcgis_number(item.get('distance_miles'))} mi",
                    f"   UPC/PIN: {item.get('upc') or ''} / {item.get('pin') or ''}",
                    f"   Class/LUC/Style: {item.get('property_class') or ''} / {item.get('land_use_code') or ''} {item.get('land_use_description') or ''} / {item.get('style') or ''}",
                    f"   Built/Acres: {item.get('year_built') or ''} / {_fmt_arcgis_number(item.get('acreage'))}",
                    f"   Reasons: {reasons}",
                    "   Missing for final comp use: living/building sqft, verified sale date, verified sale price.",
                    "",
                ]
            )

    return "\n".join(lines).strip()


def _request_needs_arcgis_candidate_peers(prompt_text: str) -> bool:
    """True for comp/sold-sale requests where ArcGIS peer candidates can help triage."""
    text = (prompt_text or "").lower()
    terms = [
        "comp",
        "comps",
        "comparable",
        "comparables",
        "candidate comps",
        "similar properties",
        "nearby sales",
        "nearby sale",
        "recent sales",
        "sold properties",
        "sold property",
        "sold homes",
        "sales nearby",
        "market support",
        "market value support",
    ]
    return _has_any_word_or_phrase(text, terms)


def _format_arcgis_candidate_peers_for_homeharvest(result: dict[str, Any]) -> str:
    """Compact ArcGIS peer block for the Context Expert/HomeHarvest prompt."""
    lines = [
        "PUBLIC ARCGIS CANDIDATE PARCEL PEERS:",
        "- Source: Bernalillo County Assessor Parcels public ArcGIS layer.",
        "- Use: Candidate peer/triage support only; these are not final comparable sales.",
        "- Limitation: Public GIS does not include living/building square footage, verified sale date, or verified sale price.",
        "- Required next step: use HomeHarvest/public aggregator data and/or iasWorld/CAMA/MLS/deed records to verify living area and sale facts before final comp use.",
        f"- Peer search status: {result.get('status') or 'unknown'}",
        f"- Peer search answer: {result.get('answer') or ''}",
    ]

    if result.get("status") == "completed":
        lines.extend(
            [
                f"- Radius used: {result.get('radius_miles')} mile(s)",
                f"- Query stage used: {result.get('query_stage') or ''}",
                f"- Candidate count: {result.get('count') or len(result.get('peers') or [])}",
            ]
        )
        for idx, item in enumerate((result.get("peers") or [])[:10], start=1):
            reasons = "; ".join(item.get("match_reasons") or [])
            lines.append(
                f"  {idx}. {item.get('situs_address') or 'Unknown situs'} | "
                f"UPC {item.get('upc') or ''} | PIN {item.get('pin') or ''} | "
                f"Score {item.get('candidate_score')} | Distance {item.get('distance_miles')} mi | "
                f"Class {item.get('property_class') or ''} | LUC {item.get('land_use_code') or ''} | "
                f"Style {item.get('style') or ''} | Built {item.get('year_built') or ''} | "
                f"Acres {item.get('acreage') if item.get('acreage') is not None else ''} | Reasons: {reasons}"
            )
    else:
        subject_lookup = result.get("subject_lookup") or {}
        if isinstance(subject_lookup, dict) and subject_lookup.get("answer"):
            lines.append(f"- Subject lookup note: {subject_lookup.get('answer')}")

    lines.extend(
        [
            "",
            "ARCGIS PEER USE RULES FOR HOMEHARVEST:",
            "- Try the candidate peer addresses as HomeHarvest sold-property searches when the staff request asks for comps/sales.",
            "- Keep any HomeHarvest subject-property facts separate from ArcGIS subject identity/assessment context.",
            "- Exclude candidate peer rows that remain missing living sqft, verified sold date, or verified sold price.",
            "- Never convert ArcGIS assessed/taxable/exemption/list/estimate values into sale prices.",
        ]
    )
    return "\n".join(lines).strip()


def _extract_arcgis_search_text_from_prompt(prompt_text: str) -> str:
    """
    Pull the best address/UPC search value out of an A.C.E.S/HomeHarvest prompt.
    Prefer labeled address blocks, then the first street-address pattern, then UPC.
    """
    text = str(prompt_text or "")

    for label in ["Address/search area:", "Address:", "Situs:", "Subject address:"]:
        block = _extract_label_block(
            text,
            label,
            [
                "Staff request:",
                "Instructions:",
                "Uploaded record transcription:",
                "HOMEHARVEST ACTION RULE:",
                "COMP SEARCH QUALITY RULES:",
            ],
        )
        if block:
            candidate = block.strip().splitlines()[0].strip(" -")
            if candidate:
                return candidate

    compact = _compact_digits(text)
    if re.fullmatch(r"[0-9]{12,30}", compact):
        return compact

    street_suffix = (
        r"ct|court|dr|drive|rd|road|st|street|ave|avenue|ln|lane|way|"
        r"blvd|boulevard|pl|place|cir|circle|trl|trail|ter|terrace|hwy|highway|pkwy|parkway"
    )
    match = re.search(
        rf"\b\d{{1,6}}\s+[A-Za-z0-9 .'-]{{2,80}}?\s+(?:{street_suffix})\b",
        text,
        re.IGNORECASE,
    )
    if match:
        return re.sub(r"\s+", " ", match.group(0)).strip(" ,")

    stripped = text.strip()
    if stripped and len(stripped) <= 160:
        return stripped
    return ""


def _format_arcgis_context_for_homeharvest(result: dict[str, Any], search_text: str) -> str:
    """Create a compact prompt block that CustomGPT/HomeHarvest can use as subject context."""
    status = str(result.get("status") or "")
    lines = [
        "PUBLIC ARCGIS PARCEL CONTEXT:",
        "- Source: Bernalillo County Assessor Parcels public ArcGIS layer.",
        "- Use: Subject parcel/GIS context only; verify final assessment details in iasWorld.",
        "- Do not treat GIS attributes as verified sale prices, exemption approvals/status, tax status, or a certified record.",
        f"- Search used: {search_text}",
        f"- GIS lookup status: {status or 'unknown'}",
    ]

    if status == "completed":
        results = result.get("results") or []
        lines.append(f"- Match count: {len(results)}")
        if len(results) == 1:
            item = results[0]
            values = item.get("assessment_values") or {}
            exemptions = item.get("exemptions") or {}
            coords = item.get("coordinates") or {}
            lines.extend(
                [
                    "- Subject anchor: exact/single public GIS match.",
                    f"- UPC: {item.get('upc') or ''}",
                    f"- PIN: {item.get('pin') or ''}",
                    f"- PID/TID: {item.get('pid') or ''} / {item.get('tid') or ''}",
                    f"- Tax year: {item.get('tax_year') or ''}",
                    f"- Situs address: {item.get('situs_address') or ''}",
                    f"- Owner: {item.get('owner') or ''}",
                    f"- Owner address: {item.get('owner_address') or ''}",
                    f"- Tax district: {item.get('tax_district') or ''}",
                    f"- Legal description: {item.get('legal_description') or ''}",
                    f"- Document number: {item.get('document_number') or ''}",
                    f"- Roll type: {item.get('roll_type') or ''}",
                    f"- Valuation class: {item.get('valuation_class') or ''}",
                    f"- Property class: {item.get('property_class') or ''}",
                    f"- Land use: {item.get('land_use_code') or ''} {item.get('land_use_description') or ''}",
                    f"- Class description: {item.get('class_description') or ''}",
                    f"- Style: {item.get('style') or ''}",
                    f"- Year built: {item.get('year_built') or ''}",
                    f"- Acreage: {item.get('acreage') if item.get('acreage') is not None else ''}",
                    f"- Land value: {values.get('land_value', '')}",
                    f"- Improvement value: {values.get('improvement_value', '')}",
                    f"- Total value: {values.get('total_value', '')}",
                    f"- Total taxable: {values.get('total_taxable', '')}",
                    f"- Net taxable: {values.get('net_taxable', '')}",
                    f"- Public GIS exemption amount fields shown: HOH {exemptions.get('head_of_household', '')}; Veteran {exemptions.get('veteran', '')}; Other {exemptions.get('other', '')}; Total {exemptions.get('total', '')}",
                    f"- Coordinates: X {coords.get('x', '')}; Y {coords.get('y', '')}",
                    f"- OBJECTID: {item.get('object_id') or ''}",
                ]
            )
        else:
            lines.append(
                "- Subject anchor: multiple public GIS candidates; ask/choose carefully and do not assume one is correct."
            )
            for idx, item in enumerate(results[:5], start=1):
                lines.append(
                    f"  {idx}. {item.get('situs_address') or 'Unknown situs'} | "
                    f"UPC {item.get('upc') or ''} | PIN {item.get('pin') or ''} | "
                    f"Class {item.get('valuation_class') or ''}/{item.get('property_class') or ''} | "
                    f"LUC {item.get('land_use_code') or ''} | "
                    f"Built {item.get('year_built') or ''} | "
                    f"Acreage {item.get('acreage') if item.get('acreage') is not None else ''}"
                )
        where = result.get("where")
        mode = result.get("query_mode")
        if where:
            lines.append(f"- ArcGIS query mode: {mode}; where: {where}")
    else:
        lines.extend(
            [
                f"- ArcGIS answer: {result.get('answer') or 'No usable GIS context.'}",
                "- Continue with HomeHarvest if the staff request requires market/listing/comps data, but state that GIS context was not found.",
            ]
        )

    lines.extend(
        [
            "",
            "GIS + HOMEHARVEST INSTRUCTIONS:",
            "- Use the GIS parcel as the subject identity/geography/assessment anchor when a single match is present.",
            "- Also search HomeHarvest/public aggregator data for the subject address when available; keep those unofficial subject market/characteristic fields separate from ArcGIS fields.",
            "- For HomeHarvest comps, search around the GIS situs address and prefer residential results consistent with property class, valuation class, land use, year built, style, acreage, tax district, and location when available.",
            "- If PUBLIC ARCGIS CANDIDATE PARCEL PEERS is present, use those addresses as peer-search leads only; do not treat them as final comps until HomeHarvest/CAMA/MLS/deed data confirms living sqft, sold date, and sold price.",
            "- Never use GIS assessment values, taxable values, exemption amount fields, AVMs, Zestimates, estimates, or list prices as sale prices, comp prices, exemption approvals, or tax/legal determinations.",
            "- Return the GIS subject context first, then HomeHarvest subject facts if found, then unofficial HomeHarvest/public-aggregator candidate results.",
            "- Clearly label HomeHarvest results as unofficial public-aggregator candidates, not verified sales or final appraisal comps.",
            "- Do not mention an interactive map, file manager, generated file, download, attachment, report, or exported view unless the current tool response includes an actual generated_files item or downloadable link.",
        ]
    )
    return "\n".join(lines).strip()


async def _enrich_homeharvest_prompt_with_arcgis(prompt_text: str) -> str:
    """Prepend public ArcGIS parcel context to HomeHarvest/address/comps prompts."""
    text = str(prompt_text or "")
    if not ENRICH_HOMEHARVEST_WITH_ARCGIS:
        return text
    if "PUBLIC ARCGIS PARCEL CONTEXT:" in text:
        return text

    search_text = _extract_arcgis_search_text_from_prompt(text)
    if not search_text:
        return (
            text
            + "\n\nPUBLIC ARCGIS PARCEL CONTEXT:\n- ArcGIS pre-check was not run because no address or UPC could be extracted."
        )

    try:
        gis_result = await _arcgis_public_parcel_lookup_result(
            search_text=search_text, max_results=5, return_geometry=False
        )
        context_block = _format_arcgis_context_for_homeharvest(gis_result, search_text)
        peer_block = ""
        if _request_needs_arcgis_candidate_peers(text):
            peer_result = await _arcgis_public_candidate_peers_result(search_text=search_text, max_results=10)
            peer_block = _format_arcgis_candidate_peers_for_homeharvest(peer_result)
        _log(
            "ArcGIS pre-check for HomeHarvest",
            status=gis_result.get("status"),
            count=gis_result.get("count"),
            query_mode=gis_result.get("query_mode"),
            candidate_peers=bool(peer_block),
        )
        return text + "\n\n" + context_block + (("\n\n" + peer_block) if peer_block else "")
    except Exception as exc:  # noqa: BLE001  the specialist still gets the question if the GIS pre-check fails
        _log("ArcGIS pre-check failed", error=str(exc))
        return (
            text
            + "\n\nPUBLIC ARCGIS PARCEL CONTEXT:\n"
            + f"- ArcGIS pre-check failed before HomeHarvest submission: {exc}\n"
            + "- Continue with HomeHarvest if the request requires public aggregator/comps data and disclose that GIS pre-check failed."
        )


def _format_arcgis_context_for_report_generation(
    result: dict[str, Any],
    search_text: str,
    include_homeharvest: bool = False,
) -> str:
    """Create ArcGIS subject context plus report-generation instructions for Context Expert."""
    status = str(result.get("status") or "")
    mode = (
        "MODE: ADDRESS REPORT + HOMEHARVEST MARKET SUPPORT"
        if include_homeharvest
        else "MODE: ADDRESS REPORT GENERATION"
    )
    lines = [
        mode,
        "",
        "PUBLIC ARCGIS PARCEL CONTEXT:",
        "- Source: Bernalillo County Assessor Parcels public ArcGIS layer.",
        "- Use: Subject parcel/GIS context only; verify final assessment details in iasWorld.",
        "- Do not treat GIS attributes as verified sale prices, tax status, exemption approvals/status, or a certified record.",
        f"- Search used: {search_text}",
        f"- GIS lookup status: {status or 'unknown'}",
    ]

    if status == "completed":
        results = result.get("results") or []
        lines.append(f"- Match count: {len(results)}")
        if len(results) == 1:
            item = results[0]
            values = item.get("assessment_values") or {}
            exemptions = item.get("exemptions") or {}
            coords = item.get("coordinates") or {}
            lines.extend(
                [
                    "- Subject anchor: exact/single public GIS match.",
                    f"- UPC: {item.get('upc') or ''}",
                    f"- PIN: {item.get('pin') or ''}",
                    f"- PID/TID: {item.get('pid') or ''} / {item.get('tid') or ''}",
                    f"- Tax year: {item.get('tax_year') or ''}",
                    f"- Situs address: {item.get('situs_address') or ''}",
                    f"- Owner: {item.get('owner') or ''}",
                    f"- Owner address: {item.get('owner_address') or ''}",
                    f"- Tax district: {item.get('tax_district') or ''}",
                    f"- Legal description: {item.get('legal_description') or ''}",
                    f"- Document number: {item.get('document_number') or ''}",
                    f"- Roll type: {item.get('roll_type') or ''}",
                    f"- Valuation class: {item.get('valuation_class') or ''}",
                    f"- Property class: {item.get('property_class') or ''}",
                    f"- Land use: {item.get('land_use_code') or ''} {item.get('land_use_description') or ''}",
                    f"- Class description: {item.get('class_description') or ''}",
                    f"- Style: {item.get('style') or ''}",
                    f"- Year built: {item.get('year_built') or ''}",
                    f"- Acreage: {item.get('acreage') if item.get('acreage') is not None else ''}",
                    f"- Land value: {values.get('land_value', '')}",
                    f"- Improvement value: {values.get('improvement_value', '')}",
                    f"- Total value: {values.get('total_value', '')}",
                    f"- Total taxable: {values.get('total_taxable', '')}",
                    f"- Net taxable: {values.get('net_taxable', '')}",
                    f"- Public GIS exemption amount fields shown: HOH {exemptions.get('head_of_household', '')}; Veteran {exemptions.get('veteran', '')}; Other {exemptions.get('other', '')}; Total {exemptions.get('total', '')}",
                    f"- Coordinates: X {coords.get('x', '')}; Y {coords.get('y', '')}",
                    f"- OBJECTID: {item.get('object_id') or ''}",
                ]
            )
        else:
            lines.append("- Subject anchor: multiple public GIS candidates; do not assume one is correct.")
            for idx, item in enumerate(results[:5], start=1):
                lines.append(
                    f"  {idx}. {item.get('situs_address') or 'Unknown situs'} | "
                    f"UPC {item.get('upc') or ''} | PIN {item.get('pin') or ''} | "
                    f"Class {item.get('valuation_class') or ''}/{item.get('property_class') or ''} | "
                    f"LUC {item.get('land_use_code') or ''} | "
                    f"Built {item.get('year_built') or ''} | "
                    f"Acreage {item.get('acreage') if item.get('acreage') is not None else ''}"
                )
    else:
        lines.extend(
            [
                f"- ArcGIS answer: {result.get('answer') or 'No usable GIS context.'}",
                "- Continue with Context Expert if the staff request requires report/file generation, but disclose that GIS context was not found.",
            ]
        )

    lines.extend(
        [
            "",
            "REPORT GENERATION INSTRUCTIONS:",
            "- Preserve the staff request exactly and create a staff-readable property/report response from the available context.",
            "- Include a Public GIS Parcel Context section using the ArcGIS context above.",
            "- If the Context Expert can create a downloadable artifact/file for this request, generate it.",
            "- Do not invent missing official fields, values, sales, comps, ownership conclusions, exemption approvals, tax status, or legal/appraisal conclusions.",
            "- Label ArcGIS as public GIS context only and require iasWorld verification before final use.",
            "- If no downloadable artifact is created, return the text report and do not claim that a file was generated.",
        ]
    )

    if include_homeharvest:
        lines.extend(
            [
                "",
                "HOMEHARVEST REPORT INSTRUCTIONS:",
                "- Use the enabled HomeHarvest custom action for unofficial public-aggregator market support.",
                "- Include a HomeHarvest/Public Aggregator Market Support section in both the text answer and any generated report file.",
                "- Search HomeHarvest/public aggregator data for the subject address itself when available, then search around the ArcGIS situs/subject anchor when a single ArcGIS match is present.",
                "- Keep ArcGIS subject identity/assessment context separate from unofficial HomeHarvest subject facts such as sqft, beds, baths, sale/listing fields, or market characteristics.",
                "- If ArcGIS candidate parcel peers are provided, use those addresses as sold-property search leads only and verify living sqft, sold date, and sold price before final comp use.",
                "- Prefer residential sales/listings consistent with property class, valuation class, land use, year built, acreage, tax district, and location when available.",
                "- Label HomeHarvest rows as unofficial public-aggregator data, not verified MLS, iasWorld/CAMA, legal, tax, valuation, exemption, ownership, or sale data.",
                "- Do not use AVMs, assessed values, taxable values, exemption amounts, estimates, or list/public-aggregator prices as verified sale prices.",
                "- If HomeHarvest returns no usable rows, include that limitation instead of inventing market data.",
            ]
        )

    return "\n".join(lines).strip()


async def _enrich_report_generation_prompt_with_arcgis(
    prompt_text: str,
    include_homeharvest: bool = False,
) -> str:
    """Prepend public ArcGIS parcel context for address/parcel report-generation tasks."""
    text = str(prompt_text or "").strip()
    if "REPORT GENERATION INSTRUCTIONS:" in text and "PUBLIC ARCGIS PARCEL CONTEXT:" in text:
        if include_homeharvest and "HOMEHARVEST REPORT INSTRUCTIONS:" not in text:
            return text + (
                "\n\nHOMEHARVEST REPORT INSTRUCTIONS:\n"
                "- Use the enabled HomeHarvest custom action for unofficial public-aggregator market support.\n"
                "- Include HomeHarvest/Public Aggregator Market Support in the text answer and any generated report file.\n"
                "- Label HomeHarvest data as unofficial and not verified MLS, iasWorld/CAMA, legal, tax, valuation, exemption, ownership, or sale data."
            )
        return text

    mode = "MODE: ADDRESS REPORT + HOMEHARVEST MARKET SUPPORT" if include_homeharvest else "MODE: REPORT GENERATION"

    search_text = _extract_arcgis_search_text_from_prompt(text)
    if not search_text:
        extra = ""
        if include_homeharvest:
            extra = (
                "\nHOMEHARVEST REPORT INSTRUCTIONS:\n"
                "- Use the enabled HomeHarvest custom action for unofficial public-aggregator market support if the request contains enough location context.\n"
                "- Label HomeHarvest data as unofficial and not verified sales or official assessment data.\n"
            )
        return (
            f"{mode}\n\n"
            f"STAFF REQUEST:\n{text}\n\n"
            "REPORT GENERATION INSTRUCTIONS:\n"
            "- Create the requested staff-readable report/file if supported by Context Expert.\n"
            "- No ArcGIS pre-check was run because no address or UPC could be extracted.\n"
            "- Do not invent missing official fields, values, sales, comps, or legal/appraisal conclusions.\n"
            f"{extra}"
        )

    try:
        gis_result = await _arcgis_public_parcel_lookup_result(
            search_text=search_text, max_results=5, return_geometry=False
        )
        context_block = _format_arcgis_context_for_report_generation(
            gis_result,
            search_text,
            include_homeharvest=include_homeharvest,
        )
        peer_block = ""
        if include_homeharvest and _request_needs_arcgis_candidate_peers(text):
            peer_result = await _arcgis_public_candidate_peers_result(search_text=search_text, max_results=10)
            peer_block = _format_arcgis_candidate_peers_for_homeharvest(peer_result)
        _log(
            "ArcGIS pre-check for report generation",
            status=gis_result.get("status"),
            count=gis_result.get("count"),
            query_mode=gis_result.get("query_mode"),
            include_homeharvest=include_homeharvest,
            candidate_peers=bool(peer_block),
        )
        return f"{context_block}{((chr(10) + chr(10) + peer_block) if peer_block else '')}\n\nSTAFF REQUEST:\n{text}"
    except Exception as exc:  # noqa: BLE001  the specialist still gets the question if the GIS pre-check fails
        _log("ArcGIS report-generation pre-check failed", error=str(exc))
        extra = ""
        if include_homeharvest:
            extra = (
                "\nHOMEHARVEST REPORT INSTRUCTIONS:\n"
                "- Use the enabled HomeHarvest custom action for unofficial public-aggregator market support if possible.\n"
                "- Label HomeHarvest data as unofficial and not verified sales or official assessment data.\n"
            )
        return (
            f"{mode}\n\n"
            f"STAFF REQUEST:\n{text}\n\n"
            "PUBLIC ARCGIS PARCEL CONTEXT:\n"
            f"- ArcGIS pre-check failed before Context Expert submission: {exc}\n\n"
            "REPORT GENERATION INSTRUCTIONS:\n"
            "- Continue with Context Expert report/file generation if supported.\n"
            "- Disclose that GIS pre-check failed.\n"
            "- Do not invent missing official fields, values, sales, comps, or legal/appraisal conclusions.\n"
            f"{extra}"
        )
