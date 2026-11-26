"""v3.9.5: a company the register lists under another name matches through config/sponsor_aliases.yaml, and says which name matched."""
from careerops.discovery import register as R


def _idx(db, names):
    db.replace_sponsors([(R.normalise(n), n, "London", "", "A", "Skilled Worker") for n in names])
    return R.SponsorIndex(db)


def test_alias_hit_matches_and_names_the_register_entry(db, tmp_path, monkeypatch):
    f = tmp_path / "a.yaml"
    f.write_text("LSEG:\n  - Refinitiv\n  - London Stock Exchange Group\n", encoding="utf-8")
    monkeypatch.setattr(R, "ALIASES_PATH", f)
    m = _idx(db, ["London Stock Exchange Group plc"]).lookup("Lseg")
    assert (m.method, m.name, m.via) == ("alias", "London Stock Exchange Group plc", "London Stock Exchange Group")


def test_no_alias_on_register_stays_not_matched(db, tmp_path, monkeypatch):
    f = tmp_path / "a.yaml"
    f.write_text("LSEG:\n  - Refinitiv\n", encoding="utf-8")
    monkeypatch.setattr(R, "ALIASES_PATH", f)
    assert _idx(db, ["Somebody Else Ltd"]).lookup("LSEG").method == "none"


def test_no_alias_file_changes_nothing(db, tmp_path, monkeypatch):
    monkeypatch.setattr(R, "ALIASES_PATH", tmp_path / "missing.yaml")
    assert _idx(db, ["Monzo Bank Ltd"]).lookup("Monzo").method == "fuzzy"
