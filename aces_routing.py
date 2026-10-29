"""
Reads a staff prompt to decide what extra work it needs before it goes to a specialist:
HomeHarvest, a report, a map, or an ArcGIS-only lookup.
"""

import re

from aces_arcgis import _compact_digits, _extract_arcgis_search_text_from_prompt, _normalize_arcgis_situs_from_user_text
from aces_common import _has_any_word_or_phrase, _past_years_date_range


def _request_needs_homeharvest(prompt_text: str) -> bool:
    """
    True when the user asks for HomeHarvest/public aggregator market support:
    comps, sales, listings, sold properties, market data, or similar external
    public-aggregator context. Plain address and map/directions lookups stay ArcGIS-only.
    """
    text = (prompt_text or "").lower()

    explicit_mode_or_source = [
        "mode: address / homeharvest lookup",
        "mode: record + homeharvest comp support",
        "mode: address report + homeharvest market support",
        "homeharvest",
        "home harvest",
        "public aggregator",
        "public-aggregator",
        "aggregator data",
        "external market data",
    ]
    if _has_any_word_or_phrase(text, explicit_mode_or_source):
        return True

    market_words = [
        "comp",
        "comps",
        "comparable",
        "comparables",
        "similar properties",
        "nearby sales",
        "nearby sale",
        "recent sales",
        "sales nearby",
        "sold properties",
        "sold property",
        "sold homes",
        "sale price",
        "sales price",
        "sale date",
        "listing data",
        "listings",
        "active listing",
        "pending listing",
        "for sale",
        "market support",
        "market data",
        "market activity",
        "market value support",
        "candidate comps",
    ]

    return _has_any_word_or_phrase(text, market_words)


def _request_needs_report_generation(prompt_text: str) -> bool:
    """
    True when staff asks for a generated report/file/PDF/export/download. These
    requests must go to Context Expert, not the ArcGIS-only fast path.
    """
    text = (prompt_text or "").lower()

    explicit_modes = [
        "mode: address report generation",
        "mode: address report + homeharvest market support",
        "mode: report generation",
        "report generation",
        "file generation",
    ]
    if any(mode in text for mode in explicit_modes):
        return True

    report_or_file_phrases = [
        "generate a report",
        "generate report",
        "create a report",
        "create report",
        "make a report",
        "make report",
        "prepare a report",
        "write a report",
        "report on",
        "property report",
        "parcel report",
        "owner report",
        "staff report",
        "generate a file",
        "generate file",
        "create a file",
        "create file",
        "downloadable file",
        "generated file",
        "download link",
        "download links",
        "download the report",
        "download report",
        "export report",
        "export a report",
        "export to pdf",
        "create pdf",
        "generate pdf",
        "pdf report",
        "attachment",
        "artifact",
    ]

    return any(phrase in text for phrase in report_or_file_phrases)


def _request_needs_map_routing(prompt_text: str) -> bool:
    """True when staff asks to map, locate, route to, or get directions to a parcel/address."""
    text = (prompt_text or "").lower()
    map_terms = [
        "map",
        "maps",
        "google maps",
        "google map",
        "directions",
        "direction",
        "route",
        "routing",
        "navigate",
        "navigation",
        "locate",
        "location",
        "open on map",
        "show on map",
        "show me on map",
        "assessor map",
        "bernco map",
    ]
    return _has_any_word_or_phrase(text, map_terms)


def _should_use_arcgis_map_lookup(prompt_text: str) -> bool:
    """Use map fast path only for map/location/direction requests, not comps/reports."""
    return (
        _looks_like_arcgis_lookup(prompt_text)
        and _request_needs_map_routing(prompt_text)
        and not _request_needs_homeharvest(prompt_text)
        and not _request_needs_report_generation(prompt_text)
    )


def _looks_like_arcgis_lookup(prompt_text: str) -> bool:
    """Detect a plain address/UPC/property lookup that can be answered by ArcGIS.

    Important: do not require a perfect command phrase like "look up".
    Copilot/user text can contain typos such as "ook up", or a user may paste
    only an address/UPC. If a street address, UPC, PIN, or situs-like value can
    be extracted and the request does not explicitly ask for comps/sales/listings
    HomeHarvest, the lookup should stay on the fast ArcGIS-only path.
    """
    text = (prompt_text or "").lower()
    search_text = _extract_arcgis_search_text_from_prompt(prompt_text)
    if not search_text:
        return False

    compact = _compact_digits(search_text)
    if re.fullmatch(r"[0-9]{12,30}", compact):
        return True

    # Any extractable street-address/situs pattern should be treated as a
    # parcel lookup unless the separate HomeHarvest detector sees comp/sale
    # language. This handles ZIP+4 and minor typos in the command phrase.
    if _normalize_arcgis_situs_from_user_text(search_text):
        return True

    lookup_words = [
        "look up",
        "lookup",
        "ook up",
        "search",
        "find",
        "parcel",
        "property",
        "situs",
        "address",
        "owner",
        "upc",
        "pin",
    ]
    return any(word in text for word in lookup_words)


def _should_use_arcgis_only_lookup(prompt_text: str) -> bool:
    """Use ArcGIS-only fast path for plain parcel/address lookup."""
    return (
        _looks_like_arcgis_lookup(prompt_text)
        and not _request_needs_homeharvest(prompt_text)
        and not _request_needs_report_generation(prompt_text)
    )


def _should_enable_homeharvest(prompt_text: str) -> bool:
    """
    Enable HomeHarvest only for explicit HomeHarvest/comps/sales/listing/market
    work. Plain address lookup is handled by the ArcGIS-only fast path.
    """
    return _request_needs_homeharvest(prompt_text)


def _enrich_homeharvest_prompt(prompt_text: str) -> str:
    """Add action-use guidance without overriding an existing MODE block."""
    text = prompt_text or ""
    if "homeharvest action rule" in text.lower():
        return text

    date_from, date_to = _past_years_date_range(10)

    action_rule = (
        "\n\nHOMEHARVEST ACTION RULE:\n"
        "- Use the enabled HomeHarvest custom action when the request is an address, nearby sale, or comp lookup.\n"
        "- Do not answer from memory, previous runs, cached examples, or stale conversation context. Run the action for this request.\n"
        "- Prefer operation homeharvestSearchProperties.\n"
        "- Use POST /properties/search.\n"
        "- For address/comps work, search the subject address itself first, then search sold/listing candidates around the subject.\n"
        "- Return staff-readable numbered cards, not raw JSON.\n"
        "- For report-generation requests, include the HomeHarvest/Public Aggregator Market Support section in the report text and any generated file.\n"
        "- A no-result response is not a tool failure.\n"
        "\n"
        "GIS + HOMEHARVEST RULES:\n"
        "- If a PUBLIC ARCGIS PARCEL CONTEXT block is present, use it as the subject identity/geography/assessment anchor.\n"
        "- Also run/use HomeHarvest for the subject address when available; keep HomeHarvest subject fields separate and label them unofficial.\n"
        "- If a PUBLIC ARCGIS CANDIDATE PARCEL PEERS block is present, use those addresses as peer leads for HomeHarvest sold-property enrichment.\n"
        "- Return the GIS subject context first, HomeHarvest subject facts second if found, then HomeHarvest/public-aggregator candidate results.\n"
        "- Do not treat GIS data as a verified sale, tax status, exemption approval/status, or certified record.\n"
        "- Verify final parcel/account details in iasWorld before relying on them.\n"
        "- Describe only what the returned data shows. Don't characterize market behavior, trends or neighborhoods beyond the returned rows.\n"
        "- Don't offer to prepare or assist with a protest. You may explain the protest process if asked.\n"
        "\n"
        "COMP SEARCH QUALITY RULES:\n"
        "- For comp requests, use listing_type=sold or sold status when supported.\n"
        f"- For 'past 10 years', use date_from={date_from} and date_to={date_to} unless the user gives a different date range.\n"
        "- Exclude land, lots, mobile homes, manufactured homes, rentals, active listings, pending listings, and rows with missing price, missing date, missing sqft, or missing residential characteristics.\n"
        "- Do not return exactly 10 unless 10 usable residential candidates are found.\n"
        "- If fewer than 10 usable residential candidates are found, return only the usable candidates and clearly say how many were found.\n"
        "- Do not pad the list with land, missing-data rows, atypical low-price rows, or poor matches.\n"
        "- Prefer similar residential properties by property type, living area, beds, baths, lot size, year built, and proximity; when ArcGIS lacks living area, use ArcGIS only for peer triage and require HomeHarvest/CAMA/MLS enrichment.\n"
        "- Sort by comp similarity first, not newest first.\n"
        "- Label results as unofficial public-aggregator candidate comps, not verified sales.\n"
        "- If the source only returns list/public aggregator prices, say they are not verified sold prices.\n"
        "- Do not mention an interactive map, file manager, generated file, download, attachment, report, or exported view unless the current tool response includes an actual generated_files item or downloadable link.\n"
    )

    if "mode:" in text.lower():
        return text + action_rule

    return "MODE: ADDRESS / HOMEHARVEST LOOKUP\n\nSTAFF REQUEST:\n" + text + action_rule


def _detect_assessment_status_message(prompt_text: str) -> str:
    text = (prompt_text or "").lower()
    if "mode: address / homeharvest lookup" in text:
        return (
            "Address/HomeHarvest task is still running. Use Check_CustomGPT_Task with the Task ID and Project ID below."
        )
    if "mode: record + homeharvest comp support" in text:
        return "Record/HomeHarvest comp-support task is still running. Use Check_CustomGPT_Task with the Task ID and Project ID below."
    if (
        "mode: address report generation" in text
        or "mode: report generation" in text
        or _request_needs_report_generation(prompt_text)
    ):
        return "Report/file generation task is still running. Use Check_CustomGPT_Task with the Task ID and Project ID below."
    return "Task is still running. Use Check_CustomGPT_Task with the Task ID and Project ID below."
