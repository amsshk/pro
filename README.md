# PaySignal

Detects every payment a business receives, works out which post or ad caused it,
scores what it is really worth, and sends that real value to Meta so its
algorithm learns to find people who pay, not people who watch.

## Run it

```bash
pip install pandas numpy scikit-learn requests pytest   # lightgbm optional, preferred
python demo.py           # payments -> real value -> Meta events
python demo_organic.py   # organic engine on 60 simulated posts
pytest -q                # 43 tests
```

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
| Claude AI | small API cost | for subtle captions. Set `ANTHROPIC_API_KEY` (console.anthropic.com). Default model `claude-haiku-5-5`, change with `PAYSIGNAL_LABEL_MODEL`. Only captions are sent, never customer or payment data. Any API error falls back to the rules. |

The AI layer is tested with simulated replies only; check its labels on your first real batch.
The server labels posts automatically on `/upload/posts`.

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
