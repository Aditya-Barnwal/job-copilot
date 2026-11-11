"""companies table: tier rules, upsert merging, config sync, ingest selection."""
from __future__ import annotations

from pathlib import Path

import pytest

from jobcopilot import companies as co

RULES = {
    "require_tier": ["brand", "funded"], "funded_min_stage": "series_a", "yc_counts_as_funded": True,
    "brands": ["Google", "Razorpay"], "brand_domains": ["flipkart.com"],
    "body_shop_names": ["Infosys", "Tata Consultancy Services"],
    "body_shop_patterns": ["staffing", "consultancy services"], "body_shop_allow": [],
}


@pytest.mark.parametrize("company, tier, reason_part", [
    ({"name": "Google India"}, "brand", "brands.yaml: brand"),
    ({"name": "Flipkart Internet", "domain": "careers.flipkart.com"}, "brand", "brand domain"),
    ({"name": "LiteLLM", "yc_batch": "W23"}, "funded", "yc-oss: W23"),
    ({"name": "Bimakavach", "funding_stage": "Series B", "signal_sources": ["bsm"]}, "funded", "bsm: Series B"),
    ({"name": "Groww", "funding_stage": "Public", "signal_sources": ["bsm"]}, "brand", "bsm: Public"),
    ({"name": "10Club", "funding_stage": "Seed", "signal_sources": ["bsm"]}, "unknown", "below series_a"),
    ({"name": "Acme", "signal_sources": ["funding-rss"]}, "funded", "funding-rss"),
    ({"name": "Infosys Limited"}, "excluded", "IT-services/staffing"),
    ({"name": "TATA Consultancy Services Ltd."}, "excluded", "IT-services/staffing"),
    ({"name": "Quickhire Staffing Solutions"}, "excluded", "keyword 'staffing'"),
    ({"name": "Nimbus Consultancy Services"}, "excluded", "keyword 'consultancy services'"),
    ({"name": "Some Startup"}, "unknown", "no brand"),
])
def test_compute_tier_matrix(company, tier, reason_part):
    got, reasons = co.compute_tier(company, RULES)
    assert got == tier
    assert any(reason_part in r for r in reasons), reasons


def test_staffing_keyword_false_positive_guards():
    # substring inside one word is not the keyword
    assert co.compute_tier({"name": "Gostaffingly"}, RULES)[0] == "unknown"
    # a YC-funded product company with the word in its name stays funded
    assert co.compute_tier({"name": "Staffing AI", "yc_batch": "S24"}, RULES)[0] == "funded"
    # explicit allow-list wins over a keyword
    rules = {**RULES, "body_shop_allow": ["Quickhire Staffing Solutions"]}
    assert co.compute_tier({"name": "Quickhire Staffing Solutions"}, rules)[0] == "unknown"


def test_config_tier_is_sticky():
    c = {"name": "Infosys", "tier_reasons": ["config: brand"], "yc_batch": "W20"}
    assert co.compute_tier(c, RULES) == ("brand", ["config: brand"])


def test_yc_batch_shortening_and_stage_levels():
    assert co.short_yc_batch("Winter 2023") == "W23"
    assert co.short_yc_batch("Spring 2025") == "X25"
    assert co.short_yc_batch("Unspecified") is None
    assert co.stage_level("Series C+") == 4 and co.stage_level("series_a") == 2
    assert co.stage_level("Bootstrapped") is None


def test_real_brands_yaml_loads():
    rules = co.load_rules()
    assert co.compute_tier({"name": "Wipro Limited"}, rules)[0] == "excluded"
    assert co.compute_tier({"name": "Razorpay Software Private Limited"}, rules)[0] == "brand"
    assert co.compute_tier({"name": "Stripe"}, rules)[0] == "brand"


def test_upsert_merges_by_domain_then_name():
    a = co.upsert_company("Razorpay Software Pvt Ltd", website="https://razorpay.com/",
                          signal_source="yc-oss", yc_batch="W15")
    b = co.upsert_company("Razorpay", website="https://www.razorpay.com", signal_source="bsm",
                          funding_stage="Series C+")
    assert a == b                                    # matched on domain despite the name
    c = co.upsert_company("razorpay software pvt. ltd.", signal_source="funding-rss",
                          investors=["Peak XV"])
    assert c == a                                    # matched on normalised name
    row = co.get_company("razorpay.com")
    assert row["signal_sources"] == ["yc-oss", "bsm", "funding-rss"]
    assert row["yc_batch"] == "W15" and row["funding_stage"] == "Series C+"
    assert row["investors"] == ["Peak XV"]
    assert co.get_company("Razorpay Software Pvt Ltd")["id"] == a


def test_generic_domains_never_merge_companies():
    a = co.upsert_company("Alpha", website="https://www.linkedin.com/company/alpha")
    b = co.upsert_company("Beta", website="https://www.linkedin.com/company/beta")
    assert a != b and co.get_company("Alpha")["domain"] is None


def test_upsert_rejects_unknown_fields():
    with pytest.raises(TypeError):
        co.upsert_company("X Corp", wesbite="typo.com")


def test_low_confidence_ats_hint_does_not_replace_verified():
    co.upsert_company("Emergent", ats="greenhouse", ats_slug="emergentlabsinc", ats_confidence=1.0)
    co.upsert_company("Emergent", ats="lever", ats_slug="emergent", verified=False)
    row = co.get_company("Emergent")
    assert (row["ats"], row["ats_slug"]) == ("greenhouse", "emergentlabsinc")


def _write(p: Path, text: str) -> Path:
    p.write_text(text, encoding="utf-8")
    return p


def test_sync_config_and_tier_not_downgraded(tmp_path):
    cfg = _write(tmp_path / "companies.yaml", """
companies:
  - { name: Infosys, sector: tech, connector: manual, tier: brand }
  - { name: Stripe, sector: fintech, connector: greenhouse, slug: stripe, verify: false }
  - { name: Okta, sector: tech, connector: greenhouse, slug: okta, verify: true }
  - { name: Adzuna IN, connector: adzuna, what: engineer }
""")
    missing = tmp_path / "target_companies.yaml"
    assert co.sync_config_companies([cfg, missing]) == 3
    assert co.get_company("Adzuna IN") is None                     # search query, not a company
    inf = co.get_company("Infosys")
    assert inf["tier"] == "brand" and inf["tier_reasons"] == ["config: brand"]
    # a later discovery source passing tier must not downgrade the config tier
    co.upsert_company("Infosys", tier="excluded", signal_source="bsm")
    co.recompute_tiers()
    assert co.get_company("Infosys")["tier"] == "brand"
    # removing the tier from config makes it un-sticky again
    _write(cfg, "companies:\n  - { name: Infosys, sector: tech, connector: manual }\n")
    co.sync_config_companies([cfg])
    assert co.get_company("Infosys")["tier"] == "excluded"


def test_enabled_ats_companies_filters(tmp_path):
    cfg = _write(tmp_path / "companies.yaml", """
companies:
  - { name: Stripe, sector: fintech, connector: greenhouse, slug: stripe }
  - { name: Groww, sector: fintech, connector: greenhouse, slug: groww, region: eu, tier: brand }
  - { name: Okta, sector: tech, connector: greenhouse, slug: okta, verify: true }
  - { name: Google, sector: tech, connector: manual }
  - { name: PayPal, sector: fintech, connector: workday, tenant: paypal, wd_server: wd1, site: jobs }
  - { name: Nobody Labs, sector: tech, connector: ashby, slug: nobody }
  - { name: Off Inc, sector: tech, connector: lever, slug: off, enabled: false, tier: funded }
""")
    co.sync_config_companies([cfg])
    rows = {r["name"]: r for r in co.enabled_ats_companies(rules={"require_tier": ["brand", "funded"]})}
    assert set(rows) == {"Stripe", "Groww", "PayPal"}   # Okta unverified, Google manual,
    assert rows["Stripe"]["connector"] == "greenhouse" and rows["Stripe"]["slug"] == "stripe"
    assert rows["Groww"]["region"] == "eu"               # Nobody Labs unknown tier, Off disabled
    assert rows["PayPal"] == {"name": "PayPal", "connector": "workday", "sector": "fintech",
                              "tier": "brand", "company_id": rows["PayPal"]["company_id"],
                              "tenant": "paypal", "wd_server": "wd1", "site": "jobs"}


def test_list_companies_filters():
    co.upsert_company("A1", tier="brand")
    co.upsert_company("B1", tier="unknown", enabled=False)
    assert [c["name"] for c in co.list_companies(tier="brand")] == ["A1"]
    assert [c["name"] for c in co.list_companies(enabled=False)] == ["B1"]


def test_sync_target_companies_style_row(tmp_path):
    cfg = _write(tmp_path / "target_companies.yaml", """
companies:
  - { name: LiteLLM, aliases: [BerriAI], sector: tech, connector: ashby, slug: litellm,
      tier: funded, tier_reason: "YC-backed LLM gateway", yc_batch: W23, apply_channel: ats, verify: false }
""")
    co.sync_config_companies([cfg])
    c = co.get_company("LiteLLM")
    assert c["tier"] == "funded" and c["tier_reasons"] == ["config: funded (YC-backed LLM gateway)"]
    assert c["yc_batch"] == "W23" and c["apply_channel"] == "ats"
    assert co.enabled_ats_companies()[0]["slug"] == "litellm"


def test_real_config_files_sync():
    n = co.sync_config_companies(co.default_config_paths())
    assert n > 0
    assert co.get_company("Google")["tier"] == "brand"
