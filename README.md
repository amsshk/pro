# ProAds

Detects every payment a business receives, works out which post or ad caused it,
scores what it is really worth, and sends that real value to Meta so its
algorithm learns to find people who pay, not people who watch.

## Website and accounts

- `/` public landing page with free sign-up (email + password). New accounts land in the app.
- `/app` the app. Sign in with email + password, or business ID + access key for businesses
  set in `PAYSIGNAL_CLIENTS`.
- `/privacy` privacy policy (set `PROADS_CONTACT_EMAIL` to show a contact email).
- 5 wrong passwords or access keys lock that login for 15 minutes.

**Tracking ProAds' own ads** (optional): set `PROADS_META_DATASET_ID` (your Pixel ID) and
`PROADS_META_TOKEN` (Conversions API token, from Events Manager). Then, only for visitors who
click "Allow" in the cookie notice:
- `CompleteRegistration` when they sign up (Pixel + server, counted once)
- `StartTrial` the first time they upload or connect data: optimise campaigns for this one,
  so Meta finds people who actually use ProAds.
Use `PROADS_META_TEST_CODE` to check events in Events Manager > Test events first.

## Web app

Three screens:

- **Audit**: sales, refunds, real profit, money-losing discount codes, refund hot spots, posts,
  and a "do this week" list. AI summary in English or Arabic, report download.
- **Connect & upload**: drop Shopify/Stripe/posts exports, webhook addresses to copy, and a
  tracking link + promo code for every new post (saved, so later sales match the post).
- **Ask & campaigns**: chat with Meta Muse about the results, and draft campaigns.

Everything uploaded is shown as plain text, never run as code. The access key stays in the
browser tab only and is cleared when the tab closes.

## Connect Shopify (button in the app)

Store owners click **Connect Shopify**, approve on Shopify's page, and their orders import
automatically; new orders and refunds then arrive by themselves. Read-only.

One-time setup by the ProAds owner:
1. Create an app at **dev.shopify.com** (Shopify Dev Dashboard).
   - App URL: `https://pro.ezdevsoft.net`
   - Redirect URL: `https://pro.ezdevsoft.net/shopify/callback`
   - Scopes: `read_orders`
   - Compliance webhooks (customer data request, customer redact, shop redact):
     `https://pro.ezdevsoft.net/webhooks/shopify-compliance`
2. Server settings (Render > Environment):
   `SHOPIFY_CLIENT_ID`, `SHOPIFY_CLIENT_SECRET` (from the app),
   `PROADS_SECRET_KEY` (a long random value; encrypts store tokens; never change it once set),
   `PROADS_PUBLIC_URL=https://pro.ezdevsoft.net`.

Shopify's limits: apps see the **last 60 days** of orders unless Shopify approves
`read_all_orders` (then set `SHOPIFY_SCOPES=read_orders,read_all_orders`). Customer emails
and phones need Shopify's protected-data approval for public apps; until then orders import
without them and the app says so.

## Start

```bash
pip install -r requirements.txt
cp clients.example.json clients.json   # one entry per business; never commit this file
python -m paysignal.server             # then follow "Going live" below
```

Onboard a business in its first week:
1. `POST /import/<id>` its Shopify or Stripe orders export and its Meta posts export
2. `GET /audit/<id>?summary=1` for the first-week audit
3. Connect its Stripe/Shopify webhooks and put a tracking link or code on every new post

`pytest -q` runs 69 checks, using generated test data that ships only in `tests/`.

## Organic engine (`organic.py`)

Judges every post by **real money per 1,000 views**, not likes.

- **Finds hidden sales:** direct (link/code), returning customers a post first brought in,
  and halo (extra unlinked sales in the days after a post, shown as an estimate).
- **Tags posts:** PAYER POST, WATCHERS ONLY (lots of likes, no payers), QUIET SELLER
  (few likes, real buyers), HIGH REFUNDS.
- **Learns patterns:** which formats, hooks, offers and posting times make money, with
  a `?` when the evidence isn't strong enough yet. The bar rises when many options are compared.
- **Post next:** Thompson sampling picks the next post type, mixing proven winners with experiments.
- **Promote first:** the posts to turn into ads once you start paying.

Input: payments (as above) plus post stats as CSV, see `posts_template.csv`
(export from Meta Business Suite or TikTok analytics). Label each post's `format`, `hook` and `offer`.

```python
from paysignal.organic import OrganicEngine, load_posts_csv
posts, errors = load_posts_csv(open("posts.csv").read())
print(OrganicEngine(engine, posts).report())
```

Tested on simulated data with known hidden patterns: across 6 datasets it found the
paying hooks and formats and never recommended a refund-heavy discount post.
Simulation is not proof; the real test is your own posts.

## Post labeller (`labeler.py`)

Fills in blank `format`, `hook` and `offer` from each post's caption and media type.
Labels typed in by hand are never overwritten.

```bash
python label_posts.py posts.csv labelled.csv
```

| Layer | Cost | Accuracy (measured) |
|---|---|---|
| Rules (default) | free, offline | 32/32 on clear captions; on subtle captions it usually can't tell, so it labels them `other` and lists them for checking. It is never confidently wrong in the test set. |
| Meta AI (default when `MODEL_API_KEY` is set) | small API cost | for subtle captions, via the model router below. |
| Claude AI (if `ANTHROPIC_API_KEY` is set instead) | small API cost | same job; default model `claude-haiku-5-5`. |

Only captions are sent, never customer or payment data. Any AI error falls back to the rules.
Force a choice with `PAYSIGNAL_LABEL_PROVIDER=meta|claude|rules`.
The server labels posts automatically on `/upload/posts`.

## Day one: first-week audit (`importers.py`, `audit.py`)

Results in week one, not month two: import the business's history and audit it immediately.

| Upload to `POST /import/<id>` | Where to get it |
|---|---|
| Shopify orders export | Shopify admin > Orders > Export (CSV) |
| Stripe payments export | Stripe dashboard > Payments > Export |
| Posts export | Meta Business Suite > Insights > Content > Export (also Instagram/TikTok exports) |
| Any sales list | CSV with `id,date,amount,currency` |

The file type is detected automatically and columns are matched by name, so slightly
different export versions still work. Re-uploading the same file never double-counts.

`GET /audit/<id>` (add `?summary=1&language=Arabic` for a Muse-written summary) shows:
- **Measured from real orders:** sales, refunds and real profit by month; discount codes that
  lose money; products with high refunds; how much profit comes from repeat customers; when people buy.
- **Estimated:** which old posts came before sales jumps. Old posts had no tracking links, so this
  is a timing estimate and is labelled as one. It often isn't strong enough to call patterns confident.
- **Do this week:** specific actions from the numbers above.

Day one gives measured money findings; tracking links from day one turn the post findings from
estimates into measurements within a few weeks.

## Meta AI (`ai.py`, `assistant.py`)

All AI features use the Meta Model API (dev.meta.ai). Put your key in `MODEL_API_KEY`.
Each feature asks for a task; the router picks the model:

| Task | Model | Price per 1M tokens (in / out) | Why |
|---|---|---|---|
| Campaign builder | `muse-wonder-1.0`, fallback `muse-spark-1.3` | $4.00 / $20.00 | best model, high reasoning |
| Chat about results | `muse-wonder-1.0`, fallback `muse-spark-1.3` | $4.00 / $20.00 | best model |
| Post labelling | `muse-spark-1.3`, fallback `muse-spark-1.2` | $1.25 / $4.25 | simple, high-volume job |

If a model fails, the next one in the chain is tried. Change routes with
`PAYSIGNAL_MODELS='{"chat": ["muse-spark-1.3"]}'`. Every reply reports its model and cost.

**Contributor tier** (`-contributor` models, ~95% cheaper): Meta trains on everything sent to
it, so ProAds never uses it for client data. It can be enabled for labelling public captions
only, with `PAYSIGNAL_ALLOW_CONTRIBUTOR=1`.

**Endpoints**
- `POST /ask/<id>` with `{"question": "..."}`: answers in plain language, in the question's language.
- `POST /campaign/<id>` with `{"daily_budget": 300, "goal": "...", "notes": "..."}`: a full draft
  campaign (ad sets, ads built from your best organic posts, tests, kill rules), checked for
  budget mistakes. Drafts only: nothing is launched.

The AI only sees aggregated results (totals, per-post numbers, patterns), never customer
names, emails, phones or single payments. AI features are tested with simulated replies;
review the first real answers and campaign drafts carefully.

## How it works

| Step | File | What it does |
|---|---|---|
| 1. Detect | `connectors/` | Stripe, Shopify and CSV (cash, WhatsApp, bank, POS) into one format. Webhook signatures verified. |
| 2. Store | `store.py` | Deduplicates, links refunds to purchases, keeps each client separate. |
| 3. Attribute | `attribution.py` | Tagged links and promo codes per post, then revenue per post. |
| 4. Score | `scoring.py` | real value = margin x (1 - return risk) + expected repeat margin. Trained per client. |
| 5. Send | `meta_capi.py` | Conversions API events with hashed email/phone, click ids and the real value. Dry run by default. |

## Organic test plan

1. Create a tagged link (`tag_link`) or promo code (`make_promo_code`) for every post, story and reel.
2. Put the link in the bio/story sticker/caption; give the code for WhatsApp and in-person buyers.
3. Connect the store (Shopify/Stripe webhooks) and upload offline sales as CSV with the code.
4. After 4-6 weeks, `Engine.content_report()` shows which posts make real money.
5. Keep sending purchases to Meta (`push_to_meta`, start with a `test_event_code`).
   When ads start, Meta already knows what a paying customer looks like.

## Live server (`paysignal/server.py`)

Receives payments 24/7, rejects fakes (signature check), ignores retried
duplicates, and sends new payments' real value to Meta every 10 minutes.

**Run locally**
```bash
pip install -r requirements.txt
cp clients.example.json clients.json      # fill in each client's settings
python -m paysignal.server                # http://localhost:8000/health
```

**Endpoints** (`<id>` = the client's key in clients.json)

| Endpoint | Who calls it |
|---|---|
| `POST /webhooks/stripe/<id>` | Stripe (events: `charge.succeeded`, `charge.refunded`) |
| `POST /webhooks/shopify/<id>` | Shopify (topics: `orders/paid`, `refunds/create`) |
| `POST /upload/csv/<id>` | You: offline sales CSV, header `X-API-Key` |
| `POST /upload/posts/<id>` | You: post stats CSV, header `X-API-Key` |
| `GET /report/<id>` | You: organic report as JSON, header `X-API-Key` |
| `POST /sync/meta/<id>` | You: push to Meta now (also runs automatically) |

**Going live, step by step**
1. Deploy (e.g. Render using `render.yaml`; check Render's current plans and settings
   when you do). Use a paid always-on plan: a sleeping server misses payments.
2. Put the clients JSON in the `PAYSIGNAL_CLIENTS` environment variable, never in git.
3. In Stripe: Developers > Webhooks > add `https://YOUR-SERVER/webhooks/stripe/<id>`,
   copy its signing secret into `stripe_webhook_secret`.
4. In Shopify: add webhooks for `orders/paid` and `refunds/create` to
   `https://YOUR-SERVER/webhooks/shopify/<id>`, put the signing secret in `shopify_webhook_secret`.
5. Meta: keep `"live": false` and check the dry run, then set `test_event_code` and
   `"live": true` and watch Events Manager > Test events. Remove the test code when correct.

## Not built yet

- Pulling post stats automatically (Instagram/TikTok APIs) instead of CSV
- Labelling from the video itself (the labeller reads captions only)
- TikTok Events API sender
- Client onboarding (Meta login, connect store)
- Dashboard
