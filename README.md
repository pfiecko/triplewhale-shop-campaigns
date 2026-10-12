# triplewhale-shop-campaigns

Import **Shopify Shop Campaigns** spend into **Triple Whale**, so Shop app ads get ROAS,
CAC and blended metrics like every other channel.

Part of a family of Triple Whale connectors that share one layout and one Data-In client:
[triplewhale-shop-campaigns](https://github.com/pfiecko/triplewhale-shop-campaigns) ·
[triplewhale-roku-ads](https://github.com/pfiecko/triplewhale-roku-ads) ·
[triplewhale-openai-ads](https://github.com/pfiecko/triplewhale-openai-ads) ·
[triplewhale-gravity-ads](https://github.com/pfiecko/triplewhale-gravity-ads)

## The problem

Shop Campaigns are ads shown inside Shopify's Shop app. Triple Whale sees the resulting
**sales** (as Shop-channel orders) but not the **spend**, so the channel has no ROAS and
blended numbers are understated. There is no ads API, and Shop Campaigns have no place to add
UTMs.

Since 11 Aug 2026 Shopify exposes Shop Campaigns performance through **ShopifyQL**
([changelog](https://shopify.dev/changelog/shop-campaigns-shopifyql), dataset
[`shop_campaign_insights`](https://shopify.dev/docs/api/shopifyql/2026-07/schemas/marketing/shop_campaign_insights)).
This Cloud Function reads it on a schedule and pushes it to Triple Whale as a custom ad channel.

```
Shopify Admin API  (ShopifyQL: shop_campaign_insights, scope read_reports)
      │  client-credentials token
      ▼
Cloud Function  shop-campaigns-tw-import   (gen2, every 6h, 14-day trailing window)
      │  day × campaign × customer segment → spend, sales, customers
      ▼
Triple Whale Data-In   POST /api/v2/data-in/ads   channel = "Shop Campaigns"
```

## What you get in Triple Whale

| In Triple Whale | Result |
|---|---|
| Total ad spend, blended ROAS, MER | ✅ Shop Campaigns spend included |
| Channel → campaign → ad set (customer segment) | ✅ spend, conversions and conversion value by day |
| Channel ROAS / CAC as reported by Shop | ✅ platform-reported view, from the customers and sales Shop reports |
| Pixel / Triple Attribution credit | ❌ orders are placed inside the Shop app with no web click, so the pixel never sees the journey |
| Clicks, impressions, CTR, CPC | ❌ not exposed by Shopify (sent as 0) |

Nothing is double counted: the orders are already in Triple Whale as sales. This only adds
spend and Shop's own reported conversions.

## Mapping

ShopifyQL has no campaign id dimension and no orders, clicks or impressions metrics.

| Data-In field | Source |
|---|---|
| `event_date` | `day` (store timezone) |
| `campaign.id` / `name` | stable hash of `shop_campaign_name` / the name |
| `adset.id` / `name` | stable hash of campaign + `shop_campaign_target_name` / the segment |
| `ad.id` / `name` | same segment: Shop has no ad level, but keeping one keeps the key stable |
| `metrics.spend` | `shop_campaign_ad_spend` |
| `metrics.conversions` | `shop_campaign_customers` (orders are not exposed; customers ≈ orders for acquisition-billed campaigns) |
| `metrics.conversion_value` | `shop_campaign_sales` |
| `clicks`, `impressions`, `visits` | 0 |

Shop bills per acquired customer (you set a maximum CPA and a daily budget), attributing to the
last paid click within 7 days. That is why the import re-sends 14 days on every run, and why
daily spend is usually a whole number of customers times roughly the CPA.

## Setup

Requirements: a GCP project with Cloud Functions, Cloud Run, Cloud Scheduler and Secret Manager
enabled; `gcloud`; Python 3 with PyYAML (used by `deploy.sh` to read the config).

1. **Shopify app.** In the Shopify Dev Dashboard, create an app with only the `read_reports`
   scope and install it on your store. It must be owned by the same organisation as the store
   for the client-credentials grant to work. Copy the client id and secret.
2. **Config.** `cp config.example.yaml config.yaml` and fill it in.
3. **Secrets and IAM** (names as in your config):
   ```bash
   PID=my-gcp-project
   SA=tw-importer@$PID.iam.gserviceaccount.com
   gcloud secrets create shopify-shop-campaigns-app --project=$PID
   printf '{"client_id":"…","client_secret":"…"}' | \
     gcloud secrets versions add shopify-shop-campaigns-app --data-file=- --project=$PID
   gcloud secrets create triplewhale-api-key --project=$PID      # key with scope "Ads: Write"
   printf '…' | gcloud secrets versions add triplewhale-api-key --data-file=- --project=$PID
   for s in shopify-shop-campaigns-app triplewhale-api-key; do
     gcloud secrets add-iam-policy-binding $s --project=$PID \
       --member="serviceAccount:$SA" --role=roles/secretmanager.secretAccessor
   done
   ```
4. **Deploy:** `./deploy.sh spend-import`
5. **Dry run** and compare one day with the Shop Campaigns dashboard:
   ```bash
   curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" "$URL?days=7&dry_run=1"
   ```
6. **Backfill** from your first campaign day: `"$URL?since=2026-08-04&until=2026-09-30"`
7. **Schedule:** `./deploy.sh spend-import --schedule` (every 6 hours by default).
8. **Verify** in Triple Whale:
   `SELECT event_date, sum(spend) FROM ads_table WHERE channel = 'Shop Campaigns' GROUP BY 1`.
   Triple Whale converts the currency you send into the shop's reporting currency, so compare
   after conversion.

## Local development

```bash
pip install -r requirements-dev.txt
pytest

# offline: map the synthetic fixture, print the Triple Whale records
CONFIG_PATH=tests/config.test.yaml python spend-import/main.py \
  --fixture tests/fixtures/shopifyql_rows.json --since 2026-09-08 --until 2026-09-14

# live Shopify query, nothing sent to Triple Whale. Secrets come from env vars named after
# the secret id, so no GCP access is needed.
export SHOPIFY_SHOP_CAMPAIGNS_APP='{"client_id":"…","client_secret":"…"}'
python spend-import/main.py --days 7 --dry-run
```

## Gotchas

- **Use Admin API `2026-10` or newer.** `2026-07` accepts the dataset but serves stale data:
  the latest day lags and the current day is missing.
- **Never change how ids are derived, and never drop the ad level, once data is live.** Triple
  Whale upserts on shop + channel + account + date + campaign + ad set + ad. A new key means a
  second copy of every row. If it happens, re-send the old keys with zero metrics to neutralise
  them.
- **Data-In is asynchronous.** A 200 response means accepted. Rows show up in `ads_table`
  minutes to hours later.
- **Restatements to zero.** A segment/day that disappears from the report inside the window is
  re-sent as 0, so downward corrections also land.
- **Native support may arrive.** Shopify's changelog names Triple Whale as a tool that *can*
  integrate this dataset. If Triple Whale ships it natively, turn this function off to avoid
  counting spend twice.

## Layout

| Path | What |
|---|---|
| `spend-import/main.py` | Cloud Function and CLI: token → ShopifyQL → Triple Whale records → upsert |
| `lib/` | shared modules, identical across the connector family (`runtime.py`, `tw_data_in.py`) |
| `config.example.yaml` | every setting, with comments |
| `deploy.sh` | stages the function with `lib/` and `config.yaml`, deploys, optionally schedules |
| `tests/` | unit tests and a synthetic ShopifyQL fixture |

## License

[MIT](LICENSE)
