"""Playwright assist on a local form: fills basics, uploads PDFs, never submits."""
from pathlib import Path

import pytest
import yaml

from careerops.config import ROOT

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "apply"


def test_fill_page_never_submits(tmp_path):
    pw = pytest.importorskip("playwright.sync_api")
    from careerops.apply.assist import fill_page
    applicant = yaml.safe_load((ROOT / "config/profile/applicant.yaml").read_text())
    applicant.update(last_name="Tester", email="t@example.com")
    cv = tmp_path / "CV.pdf"; cv.write_bytes(b"%PDF-1.4\n%%EOF")
    cl = tmp_path / "CL.pdf"; cl.write_bytes(b"%PDF-1.4\n%%EOF")
    try:
        with pw.sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto((FIX / "form.html").resolve().as_uri())
            r = fill_page(page, applicant, str(cv), str(cl))
            assert page.input_value("#first_name") == "Thiruvel"
            assert page.input_value("#email") == "t@example.com"
            assert page.input_value("#salary") == ""                       # never_fill
            assert any("Cover" in u for u in r["uploaded"]) and any("Resume" in u for u in r["uploaded"])
            assert page.input_value("#visa") == "Yes"                       # visa block: "now or in the future" -> sponsorship_future
            assert [v["value"] for v in r["visa"]] == ["Yes"]               # and it is reported, not left in `manual`
            assert page.evaluate("document.body.dataset.submitted") is None  # not submitted
            browser.close()
    except Exception as e:  # browser binaries missing in this environment
        if "Executable doesn't exist" in str(e):
            pytest.skip("playwright chromium not installed")
        raise
