"""UKVI sponsor register: parsing, normalisation, matching."""
from careerops.discovery import register as sponsors


def test_parse_filters_route_and_splits_trading_names(settings):
    rows = sponsors.parse(settings.path("sponsors_csv"), "Skilled Worker")
    norms = {r[0] for r in rows}
    assert "monzo bank" in norms
    assert {"acme analytics", "dataco"} <= norms          # T/A split
    assert not any("globex" in n for n in norms)          # GBM route excluded


def test_match_methods(db, settings):
    db.replace_sponsors(sponsors.parse(settings.path("sponsors_csv")))
    idx = sponsors.SponsorIndex(db)
    assert idx.match("Northwind Health AI")[2] == "exact"
    assert idx.match("Northwind Health AI Ltd.")[2] == "exact"
    assert idx.match("DataCo")[0] == "Acme Analytics Limited T/A DataCo"
    name, score, method = idx.match("Monzo")
    assert (name, method) == ("Monzo Bank Ltd", "prefix")
    assert idx.match("Totally Unknown Startup")[2] == "none"
    # ambiguous prefix ('data lab' → 'the data lab' & 'data lab partners') must not guess
    assert idx.match("Data")[2] == "none"


def test_normalise():
    assert sponsors.normalise("The Data Lab Ltd") == "data lab"
    assert sponsors.normalise("Smith & Co (UK) Limited") == "smith and"
