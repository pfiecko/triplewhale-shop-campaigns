import json
from pathlib import Path
from unittest import mock

import main
import runtime

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "shopifyql_rows.json").read_text())


def test_query_groups_by_day_campaign_segment():
    q = main.build_query("2026-09-08", "2026-09-14")
    assert "FROM shop_campaign_insights" in q
    assert "GROUP BY day, shop_campaign_name, shop_campaign_target_name" in q
    assert "SINCE 2026-09-08 UNTIL 2026-09-14" in q


def test_fixture_maps_to_unique_zero_filled_records():
    records = main.to_records(FIXTURE, "2026-09-08", "2026-09-14", runtime.config())
    segments = {r["adset"]["name"] for r in records}
    assert segments == {"New customers", "All other customers", "Lapsed - 3 months"}
    assert len(records) == 7 * 3                      # every segment on every day
    keys = {(r["event_date"], r["adset"]["id"], r["ad"]["id"]) for r in records}
    assert len(keys) == len(records)
    spend = round(sum(round(float(r["shop_campaign_ad_spend"]), 2) for r in FIXTURE), 2)
    assert round(sum(r["metrics"]["spend"] for r in records), 2) == spend
    assert all(r["metrics"]["clicks"] == 0 for r in records)


def test_ids_are_stable_across_runs():
    a = main.to_records(FIXTURE, "2026-09-08", "2026-09-14", runtime.config())
    b = main.to_records(FIXTURE[::-1], "2026-09-08", "2026-09-14", runtime.config())
    assert [r["ad"]["id"] for r in a] == [r["ad"]["id"] for r in b]


def test_missing_segment_falls_back():
    row = {"day": "2026-09-08T00:00:00", "shop_campaign_name": "C", "shop_campaign_ad_spend": "5"}
    [rec] = main.to_records([row], "2026-09-08", "2026-09-08", runtime.config())
    assert rec["adset"]["name"] == main.DEFAULT_SEGMENT and rec["event_date"] == "2026-09-08"


def test_shopifyql_accepts_positional_rows():
    body = {"data": {"shopifyqlQuery": {"parseErrors": [], "tableData": {
        "columns": [{"name": "day"}, {"name": "shop_campaign_ad_spend"}],
        "rows": [["2026-09-08", "1.5"]]}}}}
    with mock.patch.object(main.requests, "post", return_value=mock.Mock(ok=True, json=lambda: body)):
        rows = main.run_shopifyql("x.myshopify.com", "t", "2026-10", "FROM …")
    assert rows == [{"day": "2026-09-08", "shop_campaign_ad_spend": "1.5"}]


def test_http_handler_rejects_bad_days():
    req = mock.Mock(args={"days": "abc"})
    body, status, _ = main.spend_import(req)
    assert status == 400
