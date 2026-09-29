#!/usr/bin/env python3
"""
Shopify Shop Campaigns → Triple Whale spend import (Cloud Function, gen2).

Shop Campaigns (ads in the Shop app) have no ads API and no place for UTMs, so
Triple Whale sees their sales but not their spend. Since 2026-08-11 Shopify
exposes their performance through ShopifyQL (dataset `shop_campaign_insights`,
scope `read_reports`). This function:

  1. mints an Admin API token with the client-credentials grant,
  2. runs one ShopifyQL query for a trailing window, grouped by
     day × campaign × customer segment,
  3. upserts each row to Triple Whale Data-In (POST /api/v2/data-in/ads).

Mapping:
  campaign          ← shop_campaign_name          (no id dimension → stable hash of the name)
  adset and ad      ← shop_campaign_target_name   (customer segment; Shop has no ad level)
  spend             ← shop_campaign_ad_spend
  conversions       ← shop_campaign_customers     (no orders metric is exposed)
  conversion_value  ← shop_campaign_sales
  clicks, impressions, visits = 0                 (not exposed)

HTTP (deploy with --no-allow-unauthenticated; Cloud Scheduler calls it with OIDC):
  GET|POST /?days=14                               trailing window (default from config)
  GET|POST /?since=YYYY-MM-DD&until=YYYY-MM-DD     explicit window
  &dry_run=1                                       build records, do not send them

CLI:
  python spend-import/main.py --fixture tests/fixtures/shopifyql_rows.json \\
      --since 2026-09-08 --until 2026-09-14       # offline, no credentials
  python spend-import/main.py --days 7 --dry-run  # live Shopify, no Triple Whale push
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import requests

try:
    import runtime
except ImportError:  # running from a checkout: shared modules live in ../lib
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
    import runtime
import tw_data_in as tw

try:
    import functions_framework
except ImportError:  # CLI use without the framework installed
    functions_framework = None

log = runtime.setup_logging("shop-campaigns-tw-import")

METRICS = ("shop_campaign_ad_spend", "shop_campaign_sales", "shop_campaign_customers")
DEFAULT_SEGMENT = "All customers"


# ---------------------------------------------------------------------------
# Shopify
# ---------------------------------------------------------------------------

def shopify_token(shop: str, client_id: str, client_secret: str) -> str:
    """Client-credentials grant → Admin API access token (valid ~24h)."""
    resp = requests.post(
        f"https://{shop}/admin/oauth/access_token",
        json={"client_id": client_id, "client_secret": client_secret,
              "grant_type": "client_credentials"},
        timeout=20,
    )
    if not resp.ok:
        raise RuntimeError(f"Shopify token {resp.status_code}: {resp.text[:300]}")
    return resp.json()["access_token"]


def build_query(since: str, until: str) -> str:
    return (f"FROM shop_campaign_insights SHOW {', '.join(METRICS)} "
            f"GROUP BY day, shop_campaign_name, shop_campaign_target_name "
            f"SINCE {since} UNTIL {until} ORDER BY day")


def run_shopifyql(shop: str, token: str, api_version: str, query: str) -> list[dict]:
    gql = """
    query run($q: String!) {
      shopifyqlQuery(query: $q) {
        tableData { columns { name dataType } rows }
        parseErrors
      }
    }"""
    resp = requests.post(
        f"https://{shop}/admin/api/{api_version}/graphql.json",
        headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
        json={"query": gql, "variables": {"q": query}},
        timeout=60,
    )
    if not resp.ok:
        raise RuntimeError(f"Shopify GraphQL {resp.status_code}: {resp.text[:300]}")
    body = resp.json()
    if body.get("errors"):
        raise RuntimeError(f"Shopify GraphQL errors: {body['errors']}")
    result = body["data"]["shopifyqlQuery"]
    if result.get("parseErrors"):
        raise RuntimeError(f"ShopifyQL parse errors: {result['parseErrors']}")
    table = result.get("tableData") or {}
    cols = [c["name"] for c in table.get("columns", [])]
    # Rows come back as objects keyed by column name; accept positional arrays too.
    return [r if isinstance(r, dict) else dict(zip(cols, r)) for r in table.get("rows", [])]


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------

def to_records(rows: list[dict], since: str, until: str, cfg: dict) -> list[dict]:
    t = cfg["triplewhale"]
    records = []
    for r in rows:
        campaign = r["shop_campaign_name"]
        segment = r.get("shop_campaign_target_name") or DEFAULT_SEGMENT
        records.append(tw.ad_record(
            shop=t["shop_id"], channel=t["channel"], channel_account_id=t["channel_account_id"],
            event_date=r["day"][:10], currency=t["currency"],
            campaign={"id": tw.stable_id("shopcamp", campaign), "name": campaign},
            adset={"id": tw.stable_id("shopseg", campaign, segment), "name": segment},
            # Shop has no ad level. The segment doubles as the ad so the hierarchy is
            # campaign → adset → ad like every other channel, and ad.id is stable.
            ad={"id": tw.stable_id("shopad", campaign, segment), "name": segment},
            metrics={
                "spend": float(r.get("shop_campaign_ad_spend") or 0),
                "conversion_value": float(r.get("shop_campaign_sales") or 0),
                "conversions": float(r.get("shop_campaign_customers") or 0),
            },
        ))
    return tw.zero_fill(records, since, until)


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def import_window(since: str, until: str, dry_run: bool) -> dict:
    cfg = runtime.config()
    secrets = cfg["gcp"]["secrets"]
    app = json.loads(runtime.secret(secrets["shopify_app"]))
    shop = cfg["shopify"]["shop"]
    token = shopify_token(shop, app["client_id"], app["client_secret"])
    rows = run_shopifyql(shop, token, cfg["shopify"]["api_version"], build_query(since, until))
    records = to_records(rows, since, until, cfg)
    out = {"window": [since, until], "source_rows": len(rows), **tw.summarize(records),
           "dry_run": dry_run}
    if dry_run or not records:
        out["sample"] = records[:3]
        return out
    out["result"] = tw.send_records(records, runtime.secret(secrets["triplewhale_api_key"]))
    return out


def spend_import(request):
    """HTTP entry point."""
    args = request.args
    try:
        days = int(args["days"]) if args.get("days") else None
        since, until = runtime.date_window(days, args.get("since"), args.get("until"),
                                           runtime.config()["import"]["lookback_days"])
    except ValueError as exc:
        return runtime.json_response({"error": str(exc)}, 400)
    try:
        out = import_window(since, until, runtime.truthy(args.get("dry_run")))
    except Exception as exc:  # never leak a stack trace to the caller
        log.exception("import failed")
        return runtime.json_response({"error": str(exc)[:400]}, 500)
    log.info("done: %s", {k: v for k, v in out.items() if k != "sample"})
    failed = out.get("result", {}).get("failed", 0)
    return runtime.json_response(out, 207 if failed else 200)


if functions_framework:
    spend_import = functions_framework.http(spend_import)


def _cli() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--fixture", help="JSON file of ShopifyQL rows; skips Shopify and Triple Whale")
    p.add_argument("--days", type=int)
    p.add_argument("--since")
    p.add_argument("--until")
    p.add_argument("--dry-run", action="store_true", help="query Shopify, do not push")
    a = p.parse_args()
    cfg = runtime.config()
    since, until = runtime.date_window(a.days, a.since, a.until, cfg["import"]["lookback_days"])
    if a.fixture:
        rows = json.loads(Path(a.fixture).read_text())
        records = to_records(rows, since, until, cfg)
        out = {"window": [since, until], "source_rows": len(rows), **tw.summarize(records),
               "tw_records": records}
    else:
        out = import_window(since, until, a.dry_run)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    _cli()
