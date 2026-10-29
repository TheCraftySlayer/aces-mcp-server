"""ArcGIS where clauses built from staff text. No request is made here."""

import server


def _all_where(text: str) -> str:
    return " | ".join(where for _, where in server._arcgis_where_candidates(text))


def test_quotes_are_escaped():
    assert "O''BRIEN" in _all_where("O'Brien")


def test_like_wildcards_from_the_user_are_dropped():
    for text in ["%", "_", "SMITH%", "1%0 MAIN ST", "[A-Z]%"]:
        for _, where in server._arcgis_where_candidates(text):
            for clause in where.split(" OR "):
                if " LIKE " not in clause:
                    continue
                # The only % signs left are the ones the server adds around the search text.
                inner = clause.split(" LIKE ", 1)[1].strip("'").strip("%")
                assert inner.strip(), (text, clause)
                assert not any(ch in inner for ch in "%_[]"), (text, clause)


def test_a_plain_address_still_builds_exact_and_prefix_searches():
    modes = [mode for mode, _ in server._arcgis_where_candidates("2 Lauren Taylor Court Tijeras NM 87059")]
    assert modes[:2] == ["situs_exact", "situs_prefix"]
    assert "SITUSADD = '2 LAUREN TAYLOR CT'" in _all_where("2 Lauren Taylor Court Tijeras NM 87059")


def test_a_upc_searches_by_upc_first():
    first_mode, first_where = server._arcgis_where_candidates("1-012-057-045-251-30305")[0]
    assert first_mode == "upc_exact"
    assert "UPC = '101205704525130305'" in first_where
