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
    ({"name": "FK Internet", "domain": "flipkart.com"}, "brand", "brand domain"),   # exact domain
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
    rows = {r["name"]: r for r in co.enabled_ats_companies(rules={"require_tier": ["brand", "funded"]},
                                                       include_config=True)}
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
    assert co.enabled_ats_companies(include_config=True)[0]["slug"] == "litellm"
    assert co.enabled_ats_companies() == []          # config rows come from the config files


def test_real_config_files_sync():
    n = co.sync_config_companies(co.default_config_paths())
    assert n > 0
    assert co.get_company("Google")["tier"] == "brand"


# ------------------------------------------------- Phase 1 review regressions

@pytest.mark.parametrize("url", [
    "https://play.google.com/store/apps/details?id=com.acme", "https://apps.apple.com/in/app/x/id1",
    "https://docs.google.com/forms/d/x", "https://forms.gle/abc", "https://calendly.com/acme",
    "https://acme.typeform.com/to/x", "https://linktr.ee/acme", "https://acme.notion.site/jobs",
    "https://acme.wixsite.com/home", "https://acme.webflow.io", "https://acme.carrd.co",
    "https://acme.github.io", "https://acme.vercel.app", "https://acme.netlify.app",
    "https://medium.com/@acme", "https://acme.substack.com", "https://sites.google.com/view/acme",
])
def test_shared_hosts_are_never_identity_domains(url):   # logic-1 / safety-3
    assert co.usable_domain(url) == ""


def test_two_companies_on_play_store_stay_separate_and_not_brand():   # logic-1 / safety-3
    a = co.upsert_company("Alpha Pay", website="https://play.google.com/store/apps/details?id=a")
    b = co.upsert_company("Beta Ride", website="https://play.google.com/store/apps/details?id=b")
    c = co.upsert_company("Gamma", website="https://apps.apple.com/in/app/gamma/id9")
    assert len({a, b, c}) == 3
    rules = co.load_rules()
    for name in ("Alpha Pay", "Beta Ride", "Gamma"):
        assert co.compute_tier(co.get_company(name), rules)[0] != "brand"
    # brand_domains match exactly, never by suffix
    rules = {**RULES, "brand_domains": ["google.com", "apple.com"]}
    assert co.compute_tier({"name": "Acme", "domain": "play.google.com"}, rules)[0] != "brand"
    assert co.compute_tier({"name": "Acme", "domain": "foo.google.com"}, rules)[0] != "brand"
    assert co.compute_tier({"name": "Acme", "domain": "google.com"}, rules)[0] == "brand"
    # explicitly listed subdomains still count; a shared host listed by mistake never does
    rules = {**RULES, "brand_domains": ["careers.flipkart.com", "play.google.com"]}
    assert co.compute_tier({"name": "FK", "domain": "careers.flipkart.com"}, rules)[0] == "brand"
    assert co.compute_tier({"name": "Acme", "domain": "play.google.com"}, rules)[0] != "brand"


def test_common_word_brands_need_exact_name_and_domain():   # logic-6
    rules = co.load_rules()
    assert co.compute_tier({"name": "Zoom Technologies", "funding_stage": "Seed",
                            "signal_sources": ["bsm"]}, rules)[0] == "unknown"
    assert co.compute_tier({"name": "Target Software"}, rules)[0] != "brand"
    assert co.compute_tier({"name": "Linear", "domain": "linear-labs.in"}, rules)[0] != "brand"
    assert co.compute_tier({"name": "Linear", "domain": "linear.app"}, rules)[0] == "brand"
    assert co.compute_tier({"name": "Zoom", "domain": "zoom.us"}, rules)[0] == "brand"
    assert co.compute_tier({"name": "Mercury"}, rules)[0] == "brand"      # no domain known
    # distinctive brands keep the loose match
    assert co.compute_tier({"name": "Google India"}, rules)[0] == "brand"
    # inline rules: dict entries with domains
    r = {**RULES, "brands": [{"name": "Slice", "domains": ["sliceit.com"]}]}
    assert co.compute_tier({"name": "Slice", "domain": "slice.dev"}, r)[0] != "brand"
    assert co.compute_tier({"name": "Slice", "domain": "sliceit.com"}, r)[0] == "brand"
    assert co.compute_tier({"name": "Slice Labs"}, r)[0] != "brand"


def test_public_stage_does_not_bypass_body_shop_checks():   # safety-5
    c = {"name": "Nimbus Consultancy Services", "funding_stage": "Public", "signal_sources": ["bsm"]}
    assert co.compute_tier(c, RULES)[0] == "excluded"
    rules = co.load_rules()
    for name in ("ALTEN Calsoft Labs", "Tata Elxsi", "Quest Global", "Sify Technologies",
                 "LatentView Analytics", "Mastek", "Zensar", "Birlasoft", "Cyient", "KPIT"):
        assert co.compute_tier({"name": name, "funding_stage": "Public",
                                "signal_sources": ["bsm"]}, rules)[0] == "excluded", name


def test_same_name_different_domains_are_different_companies():   # logic-3
    a = co.upsert_company("Atlas", website="https://atlas.so", signal_source="yc-oss", yc_batch="W24")
    b = co.upsert_company("Atlas", website="https://atlashq.in", signal_source="bsm",
                          funding_stage="Seed")
    assert a != b
    assert co.get_company("atlas.so")["yc_batch"] == "W24"
    hq = co.get_company("atlashq.in")
    assert hq["yc_batch"] is None and hq["name"] == "Atlas"
    assert hq["normalized_name"] == "atlas#atlashq.in"
    # same domain again -> same row; tier is computed from the bare name
    assert co.upsert_company("Atlas", website="atlashq.in") == b
    assert co.compute_tier(hq, RULES)[0] == "unknown"


def test_config_row_replaces_ats_fields_even_with_verify(tmp_path):   # logic-9
    co.upsert_company("Groww", ats="greenhouse", ats_slug="groww", ats_region="eu",
                      ats_confidence=1.0, ats_evidence="ats_detect")
    cfg = _write(tmp_path / "companies.yaml",
                 "companies:\n  - { name: Groww, connector: lever, slug: groww-in, verify: true }\n")
    co.sync_config_companies([cfg])
    g = co.get_company("Groww")
    assert (g["ats"], g["ats_slug"], g["ats_region"]) == ("lever", "groww-in", None)
    assert g["ats_confidence"] == co.UNVERIFIED_CONFIDENCE
    co.upsert_company("PayPal", ats="workday", ats_tenant="paypal", ats_site="jobs",
                      ats_wd_server="wd1", ats_confidence=1.0)
    _write(cfg, "companies:\n  - { name: PayPal, connector: greenhouse, slug: paypal }\n")
    co.sync_config_companies([cfg])
    p = co.get_company("PayPal")
    assert (p["ats"], p["ats_slug"], p["ats_tenant"], p["ats_site"], p["ats_wd_server"]) == \
        ("greenhouse", "paypal", None, None, None)
    assert p["ats_confidence"] == 1.0


def test_company_removed_from_config_stops_being_ingested(tmp_path):   # logic-2
    cfg = _write(tmp_path / "companies.yaml", """
companies:
  - { name: Stripe, connector: greenhouse, slug: stripe, tier: brand }
  - { name: Tiny Co, connector: lever, slug: tiny, tier: funded }
""")
    co.sync_config_companies([cfg])
    rows = co.enabled_ats_companies(rules={"require_tier": ["brand", "funded"]})
    assert rows == []                                   # config rows are already in the roster
    assert {r["name"] for r in co.enabled_ats_companies(include_config=True)} == {"Stripe", "Tiny Co"}
    _write(cfg, "companies:\n  - { name: Stripe, connector: greenhouse, slug: stripe, tier: brand }\n")
    co.sync_config_companies([cfg])
    tiny = co.get_company("Tiny Co")
    assert "config" not in tiny["signal_sources"]
    assert not any(r.startswith("config:") for r in tiny["tier_reasons"])
    assert tiny["tier"] != "funded"
    assert tiny["ats_confidence"] < co.VERIFIED_CONFIDENCE
    assert co.enabled_ats_companies() == []
    assert co.get_company("Stripe")["tier"] == "brand"


def test_tier_uses_fresh_name_key_not_stale_stored_key():
    c = {"name": "D.E. Shaw", "normalized_name": "d e shaw"}
    assert co.compute_tier(c, co.load_rules())[0] == "brand"


def test_renormalize_rekeys_and_merges_only_same_company():
    from jobcopilot import store
    ids = {}
    with store.connect() as conn:          # rows keyed by an OLDER normaliser
        for name, key, dom, src in [
            ("Proxify AB", "proxify ab", "proxify.se", '["bsm"]'),
            ("Proxify", "proxify", None, '["yc-oss"]'),            # no domain -> same company
            ("Wolt Oy", "wolt oy", "wolt.com", '["bsm"]'),
            ("Wolt", "wolt", "wolt.fi", '["yc-oss"]'),              # different domain -> apart
            ("Mercari K.K.", "mercari k k", "mercari.com", '["bsm"]'),
            ("Atlas", "atlas", "atlas.so", '["yc-oss"]'),
            ("Atlas", "atlas#atlashq.in", "atlashq.in", '["bsm"]'),
            ("Atlas", "atlas#funding-rss", None, '["funding-rss"]'),
        ]:
            cur = conn.execute("INSERT INTO companies (name, normalized_name, domain, signal_sources) "
                               "VALUES (?, ?, ?, ?)", (name, key, dom, src))
            ids[key] = cur.lastrowid
        dup = ids["proxify ab"]                # this row is folded into "proxify"
        conn.execute("INSERT INTO jobs (key, source, company, title, company_id) "
                     "VALUES ('j1', 'test', 'Proxify', 'SWE', ?)", (dup,))
    out = co.renormalize_names()
    assert out["merged"] == 1
    prox = co.get_company("Proxify")
    assert prox["normalized_name"] == "proxify" and prox["domain"] == "proxify.se"
    assert set(prox["signal_sources"]) == {"bsm", "yc-oss"}
    with store.connect() as conn:            # the job follows the surviving row
        assert conn.execute("SELECT company_id FROM jobs WHERE key = 'j1'").fetchone()[0] == prox["id"]
    keys = sorted(c["normalized_name"] for c in co.list_companies())
    assert keys == ["atlas", "atlas#atlashq.in", "atlas#funding-rss", "mercari",
                    "proxify", "wolt", "wolt#wolt.com"]
    assert co.get_company("wolt.com")["name"] == "Wolt Oy"
    assert co.renormalize_names() == {"rekeyed": 0, "merged": 0}      # idempotent


def test_watchlist_tier_is_curated_separate_and_never_a_body_shop():
    """Unvetted companies named by a curated early-career list get their own lane
    ('watchlist'); random seed rows stay 'unknown'; body shops stay excluded."""
    rules = co.prepare_rules({"require_tier": ["brand", "funded"]})
    one = co.prepare_rules({"watchlist": {"min_signals": 1}})
    assert co.compute_tier({"name": "Tiny Tools", "signal_sources": ["seed:simplify-newgrad"]}, rules)[0] == "unknown"  # default needs 2 lists
    tier, why = co.compute_tier({"name": "Tiny Tools", "signal_sources": ["seed:simplify-newgrad"]}, one)
    assert tier == "watchlist" and "simplify-newgrad" in why[0]
    tier, why = co.compute_tier({"name": "Tiny Tools", "signal_sources": ["seed:simplify-newgrad", "seed:zshah-internships"]}, rules)
    assert tier == "watchlist"
    assert co.compute_tier({"name": "Tiny Tools", "signal_sources": ["seed:ats-scrapers"]}, rules)[0] == "unknown"
    real = co.prepare_rules(co.load_rules())   # includes config/brands.yaml body-shop names + keywords
    assert co.compute_tier({"name": "Acme Staffing Solutions", "signal_sources": ["seed:simplify-newgrad", "seed:zshah-internships"]}, real)[0] == "excluded"
    assert co.compute_tier({"name": "Infosys", "signal_sources": ["seed:simplify-newgrad", "seed:zshah-internships"]}, real)[0] == "excluded"
    # a funded signal still wins over the watchlist lane
    assert co.compute_tier({"name": "Seedling", "yc_batch": "W24", "signal_sources": ["seed:simplify-newgrad"]}, rules)[0] == "funded"
    # switches
    assert co.ingest_tiers({"require_tier": ["brand", "funded"]}) == ["brand", "funded", "watchlist"]
    assert co.ingest_tiers({"require_tier": ["brand", "funded"], "include_extended": False}) == ["brand", "funded"]
    off = co.prepare_rules({"watchlist": {"enabled": False}})
    assert co.compute_tier({"name": "Tiny Tools", "signal_sources": ["seed:simplify-newgrad", "seed:zshah-internships"]}, off)[0] == "unknown"
