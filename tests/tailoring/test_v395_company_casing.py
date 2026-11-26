"""v3.9.5: the company is written as the ad writes it; capitals of 2-5 letters stay; no .title()."""
import pytest

from careerops.tailoring.letters import display_company, greeting


@pytest.mark.parametrize("company,ad,hello", [
    ("Lseg", "Join LSEG, a global markets group. LSEG is hiring.", "Hi LSEG team"),
    ("Kong Inc", "Kong is an API company.", "Hi Kong team"),
    ("deliveroo", "we are deliveroo, riders and food", "Hi Deliveroo team"),
])
def test_greeting_uses_the_ads_spelling(settings, company, ad, hello):
    assert greeting({"company": display_company(company, ad)}, "", settings) == hello


def test_name_not_in_the_ad_keeps_its_casing():
    assert display_company("McKinsey", "") == "McKinsey"
