"""Cross-source dedupe + multi-source storage (Phase 1, PLAN.md L#6 / D11)."""
from __future__ import annotations

from jobcopilot import dedupe, store
from jobcopilot.filters import FilterResult
from jobcopilot.models import Compensation, Job


def _job(source="ashby", ext="a1", title="Software Engineer", company="Acme", *,
         url=None, apply_url=None, location="Remote", workplace="remote", desc="d",
         comp=None, attribution=None) -> Job:
    return Job(
        source=source, company=company, company_sector="tech", external_id=ext, title=title,
        location_text=location, workplace_type=workplace, country_bucket="unknown",
        url=url or f"https://{source}.example/{ext}", description_text=desc,
        compensation=comp, apply_url=apply_url,
        attribution_name=attribution, attribution_url=("https://himalayas.app" if attribution else None),
    )


def _himalayas(ext="h1", title="Software Engineer", company="Acme Inc.", **kw) -> Job:
    return _job("himalayas", ext, title, company,
                url=f"https://himalayas.app/companies/acme/jobs/{ext}",
                apply_url=kw.pop("apply_url", f"https://himalayas.app/apply/{ext}"),
                attribution="Himalayas", **kw)


# --- keys -------------------------------------------------------------------

def test_url_key_normalises_scheme_host_slash_and_tracking():
    a = dedupe.url_key("HTTPS://www.Jobs.Example.com/a//b/?utm_source=x&gh_jid=1&ref=hn#top")
    assert a == "jobs.example.com/a/b?gh_jid=1"
    assert dedupe.url_key("http://jobs.example.com/a/b?gh_jid=1") == a
    assert dedupe.url_key("https://jobs.example.com/a/b?gh_jid=2") != a  # identifying params kept
    assert dedupe.url_key("") == "" and dedupe.url_key(None) == ""


def test_title_levels_stay_distinct_but_noise_folds():
    assert dedupe.normalize_title("Software Engineer II") != dedupe.normalize_title("Software Engineer III")
    assert dedupe.normalize_title("Sr. Software Engineer (Remote)") == dedupe.normalize_title("Senior Software Engineer")
    assert dedupe.normalize_title("Software Engineer 2") == dedupe.normalize_title("Software Engineer II")


def test_canonical_key_uses_company_normalisation_and_location_bucket():
    assert dedupe.canonical_key(_job(company="Acme")) == dedupe.canonical_key(_job(company="Acme, Inc."))
    onsite_blr = _job(location="Bengaluru, India", workplace="onsite")
    onsite_bang = _job(location="Bangalore", workplace="onsite")
    assert dedupe.canonical_key(onsite_blr) != dedupe.canonical_key(_job())  # onsite vs remote
    onsite_blr.country_bucket = onsite_bang.country_bucket = "IN"
    assert dedupe.canonical_key(onsite_blr) == dedupe.canonical_key(onsite_bang)


# --- merge ------------------------------------------------------------------

def test_same_role_on_himalayas_and_ashby_is_one_job_with_ats_apply_url():
    ats = _job("ashby", "ash-1", comp=Compensation(currency="USD", min_value=1, max_value=2),
               url="https://jobs.ashbyhq.com/acme/ash-1", desc="long description")
    board = _himalayas("h1")
    merged = dedupe.merge([board, ats])
    assert len(merged) == 1
    j = merged[0]
    assert j.source == "ashby"
    assert j.apply_url == "https://jobs.ashbyhq.com/acme/ash-1"
    assert {s["source"] for s in j.sources} == {"ashby", "himalayas"}
    assert j.attribution_name == "Himalayas"  # "via Himalayas" still shown on the card


def test_himalayas_only_keeps_application_link():
    merged = dedupe.merge([_himalayas("h9", apply_url="https://himalayas.app/apply/h9")])
    assert len(merged) == 1
    assert merged[0].apply_url == "https://himalayas.app/apply/h9"
    assert merged[0].sources[0]["attribution_name"] == "Himalayas"


def test_board_apply_link_pointing_at_ats_url_merges_by_url():
    ats = _job("greenhouse", "77", "Backend Engineer", url="https://boards.greenhouse.io/acme/jobs/77")
    board = _job("remoteok", "r1", "Backend Developer (Remote)", url="https://remoteok.com/l/r1",
                 apply_url="https://boards.greenhouse.io/acme/jobs/77?utm_source=remoteok")
    merged = dedupe.merge([ats, board])
    assert len(merged) == 1 and merged[0].source == "greenhouse"


def test_shared_generic_apply_page_does_not_merge_different_roles():
    careers = "https://acme.example/careers"
    merged = dedupe.merge([
        _job("remoteok", "r1", "Backend Engineer", apply_url=careers),
        _job("workingnomads", "w1", "Data Scientist", apply_url=careers),
        _job("remoteok", "r2", "Frontend Engineer", apply_url=careers),
    ])
    assert len(merged) == 3


def test_swe_ii_and_iii_not_merged():
    merged = dedupe.merge([_job("ashby", "1", "Software Engineer II"),
                           _himalayas("2", title="Software Engineer III")])
    assert len(merged) == 2


def test_two_same_source_postings_never_merged_by_canonical_key():
    # Ashby boards often list one role per region with the same title.
    merged = dedupe.merge([_job("ashby", "us", location="Remote - US"),
                           _job("ashby", "eu", location="Remote - EU")])
    assert len(merged) == 2


def test_primary_fills_missing_fields_from_others():
    ats = _job("lever", "l1", desc="")
    board = _himalayas("h1", desc="full text from the board")
    board.region_restrictions = ["India"]
    j = dedupe.merge([ats, board])[0]
    assert j.source == "lever"
    assert j.description_text == "full text from the board"
    assert j.region_restrictions == ["India"]


# --- store --------------------------------------------------------------------

def _fr(job: Job) -> FilterResult:
    return FilterResult(job=job, package_estimated=False, country_uncertain=False)


def test_upsert_writes_job_sources_and_reingest_updates_last_seen(tmp_path):
    db = tmp_path / "s.db"
    store.init_db(db)
    j = dedupe.merge([_himalayas("h1"), _job("ashby", "ash-1")])[0]
    assert store.upsert_jobs([_fr(j)], db_path=db) == (1, 0)
    got = store.get_job(j.key, db_path=db)
    assert {s["source"] for s in got["sources"]} == {"ashby", "himalayas"}
    assert got["apply_url"] == j.url and got["canonical_key"] == j.canonical_key
    first = {s["source"]: s["last_seen"] for s in got["sources"]}

    store.set_status(j.key, "applied", notes="sent", db_path=db)
    j2 = dedupe.merge([_himalayas("h1"), _job("ashby", "ash-1", desc="edited")])[0]
    assert store.upsert_jobs([_fr(j2)], db_path=db) == (0, 1)
    got = store.get_job(j.key, db_path=db)
    assert got["status"] == "applied" and got["notes"] == "sent"
    assert got["description_text"] == "edited"
    assert all(s["last_seen"] >= first[s["source"]] for s in got["sources"])
    assert len(got["sources"]) == 2


def test_canonical_match_attaches_new_source_to_existing_row(tmp_path):
    db = tmp_path / "c.db"
    store.init_db(db)
    ats = _job("ashby", "ash-1", url="https://jobs.ashbyhq.com/acme/ash-1")
    store.upsert_jobs([_fr(ats)], db_path=db)
    store.set_status(ats.key, "interested", db_path=db)

    # A later run sees only the board copy (e.g. Ashby was down).
    board = dedupe.merge([_himalayas("h1")])[0]
    assert store.upsert_jobs([_fr(board)], db_path=db) == (0, 1)
    assert store.get_job(board.key, db_path=db) is None  # no duplicate row
    got = store.get_job(ats.key, db_path=db)
    assert got["status"] == "interested"
    assert {s["source"] for s in got["sources"]} == {"ashby", "himalayas"}
    # A board never overwrites the employer's posting or its apply link.
    assert got["source"] == "ashby" and got["apply_url"] == ats.url


def test_json_fields_round_trip_and_source_state(tmp_path):
    db = tmp_path / "j.db"
    store.init_db(db)
    j = _himalayas("h1")
    j.region_restrictions, j.timezone_restrictions = ["APAC"], [5.5]
    store.upsert_jobs([_fr(j)], db_path=db)
    row = store.list_jobs(db_path=db)[0]
    assert row["region_restrictions"] == ["APAC"] and row["timezone_restrictions"] == [5.5]

    assert store.get_source_state("himalayas", db_path=db) is None
    store.set_source_state("himalayas", db_path=db, etag="abc", state={"n": 1})
    store.set_source_state("himalayas", db_path=db, items=3, last_error=None)
    st = store.get_source_state("himalayas", db_path=db)
    assert st["etag"] == "abc" and st["items"] == 3 and st["state"] == {"n": 1}
    assert [r["source"] for r in store.list_source_state(db_path=db)] == ["himalayas"]


def test_purge_expired_keeps_rows_the_user_touched(tmp_path):
    db = tmp_path / "p.db"
    store.init_db(db)
    old, kept = _himalayas("h1"), _himalayas("h2", title="Data Engineer")
    old.expires_at = kept.expires_at = "2020-01-01"
    store.upsert_jobs([_fr(old), _fr(kept)], db_path=db)
    store.set_status(kept.key, "applied", db_path=db)
    assert store.purge_expired("2026-01-01", db_path=db) == 1
    assert store.get_job(old.key, db_path=db) is None
    assert store.get_job(kept.key, db_path=db)["status"] == "applied"


def test_tenant_scoped_workday_ids_do_not_collide(tmp_path):
    db = tmp_path / "w.db"
    store.init_db(db)
    a = _job("workday", "R-1", "Software Engineer", company="PayPal", location="Chennai", workplace="onsite")
    b = _job("workday", "R-1", "Software Engineer", company="Adobe", location="Chennai", workplace="onsite")
    merged = dedupe.merge([a, b])
    assert len(merged) == 2
    assert store.upsert_jobs([_fr(j) for j in merged], db_path=db) == (2, 0)
    assert store.get_job(a.key, db_path=db)["sources"][0]["external_id"] == "paypal/R-1"
