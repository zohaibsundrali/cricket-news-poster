# 🏏 Cricket News Auto-Poster

Finds fresh cricket news, rewrites it in natural Pakistani Urdu, renders a branded
news card, asks you to approve it on Telegram, and posts it to your Facebook Page.

Runs entirely on free tiers. **Total cost: $0/month.**

```
every 3h ──▶ discover ──▶ dedupe ──▶ rank ──▶ extract ──▶ Urdu (AI) ──▶ render card
                                                                            │
                                          ┌─────────────────────────────────┘
                                          ▼
                            Telegram: [✅ پوسٹ کریں] [❌ منسوخ]
                                          │
                       every 15m ─────────┴──▶ Facebook Page ──▶ ✅ confirmation
```

---

## What runs where

| Piece | Service | Free tier |
|---|---|---|
| Scheduling + compute | GitHub Actions | Unlimited minutes on a **public** repo |
| Database | `data/state.json`, committed to this repo | Free; also keeps the cron alive |
| Urdu writing | Google Gemini | Free tier, ~6 calls/day |
| Card rendering | Playwright + Chromium | Free on the runner |
| Fonts | Noto Nastaliq Urdu + Noto Naskh Arabic + Inter | OFL, commercial use OK |
| Approval + alerts | Telegram Bot API | Free |
| Publishing | Meta Graph API | Free |

---

## Setup

### 1. Create the repo

Create a **new, empty, public** GitHub repo, then:

```bash
git init
git add .
git commit -m "feat: cricket news auto-poster"
git branch -M main
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

> The repo must be **public** for unlimited Actions minutes. Your secrets live in
> GitHub Secrets and are never in the code — but do not commit a `.env` file.

### 2. Add the secrets

**Settings → Secrets and variables → Actions → New repository secret:**

| Secret | Where to get it |
|---|---|
| `FB_PAGE_ID` | Your Page → About → Page transparency |
| `FB_PAGE_ACCESS_TOKEN` | See §3 below |
| `FB_APP_ID` | developers.facebook.com → your App → Settings → Basic |
| `FB_APP_SECRET` | Same screen, click "Show" |
| `GEMINI_API_KEY` | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) → Create API key |
| `TELEGRAM_BOT_TOKEN` | Telegram → [@BotFather](https://t.me/BotFather) → `/newbot` |
| `TELEGRAM_CHAT_ID` | Message your bot, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and read `message.chat.id` |

### 3. Get a never-expiring Facebook token

This is the step that decides whether the bot runs for years or breaks every
two months. Use a **System User** token — it never expires.

1. [business.facebook.com](https://business.facebook.com) → **Business Settings**
2. **Users → System Users** → **Add** → name it `cricket-bot`, role **Admin**
3. Select it → **Assign Assets** → **Pages** → pick your Page → enable **Manage Page** (full control)
4. **Generate New Token** → choose your App → tick these scopes:
   - `pages_manage_posts` — create posts
   - `pages_read_engagement` — read Page info
   - `pages_show_list` — resolve the Page
   - `business_management` — token introspection
5. Set expiry to **Never** → copy the token into `FB_PAGE_ACCESS_TOKEN`

<details>
<summary>Don't have Business Manager?</summary>

Use Graph API Explorer to get a User token with the same scopes, then exchange it
for a 60-day Page token. The bot will Telegram you 7 days before it expires, and
you regenerate it the same way. Everything else works identically.
</details>

### 4. Optional: tune it without touching code

**Settings → Secrets and variables → Actions → Variables** (not secrets):

| Variable | Default | What it does |
|---|---|---|
| `BRAND_NAME` | `کرکٹ اپڈیٹس` | Name shown on the card header |
| `BRAND_HANDLE` | — | e.g. `fb.com/yourpage`, shown under the name |
| `BRAND_ACCENT` | `#F5C542` | Accent colour (gold) |
| `BRAND_DEEP` | `#04140E` | Background base colour |
| `AUTO_APPROVE` | `false` | `true` skips the Telegram gate and posts immediately |
| `APPROVAL_TIMEOUT_HOURS` | `12` | Unapproved cards expire after this |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Change if Google renames the free model |
| `TIMEZONE` | `Asia/Karachi` | Used for the date on the card |

Drop a `logo.png` into `assets/` and it replaces the default cricket-ball mark.

### 5. Verify everything

Locally, with a `.env` file (copy `.env.example`):

```bash
pip install -r requirements.txt
playwright install chromium
python scripts/fetch_fonts.py
python scripts/check_setup.py     # ✅/❌ for every credential, feed and font
```

`check_setup.py` prints your Page name, bot username, token expiry, and sends a
test Telegram message — so you know it all works before the first real run.

### 6. First run

**Actions → Generate cricket post → Run workflow.** Tick `dry_run` first if you
want to see the output without posting anything. Within a minute you should get
an approval card in Telegram.

---

## How the approval gate works

1. `generate.py` builds the card and sends it to you with two buttons.
2. You tap **✅ پوسٹ کریں** or **❌ منسوخ**.
3. `publish.py` runs every 15 minutes, sees the tap, and posts to Facebook.

So a post goes live **up to 15 minutes** after you approve it. Rejected stories
are remembered and never resurface. Anything you ignore for 12 hours expires.

Once you trust the Urdu quality, set the `AUTO_APPROVE` variable to `true` and
posts go straight to Facebook.

> The rendered card is stored **on Telegram**, not in this repo — `publish.py`
> downloads it back by `file_id`. That is why the repo does not grow by 200 KB
> every three hours.

---

## Tuning

Everything lives in `config.py`:

- **`FEEDS`** — add or remove sources. Each has a `weight` (source trust) and a
  `region` (`pk` gets a bonus).
- **`PK_KEYWORDS`** — Pakistan relevance. A match in the **headline** earns the
  full boost; a passing mention in the summary earns 35% of it (`PK_MENTION_FACTOR`).
- **`HEAT_KEYWORDS`** — globally trending signals (World Cup, IPL, records...).
- **`SCORE_WEIGHTS`** — the whole editorial policy in eight numbers.
- **`MAX_ARTICLE_AGE_HOURS`** — anything older is dropped.

Change the schedule in `.github/workflows/generate.yml`. Cron is UTC; Pakistan is
UTC+5. The default `0 2,5,8,11,14,17 * * *` is 07:00–22:00 PKT, deliberately
skipping the dead hours when nobody is awake to see the post.

Preview design changes without any credentials:

```bash
python scripts/preview_card.py          # short headline
python scripts/preview_card.py --long   # long headline
# → build/preview.png
```

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Telegram says `facebook-auth` failed | Token expired or missing a scope. Regenerate (§3) and update the secret. |
| "No candidate survived the pipeline" | All top articles were paywalled or blocked. Usually self-corrects next run. |
| Urdu looks like disconnected letters | Fonts missing. Run `python scripts/fetch_fonts.py`. |
| Gemini 404 on the model | Google renamed it. Run `check_setup.py` — it lists available models. Set the `GEMINI_MODEL` variable. |
| Nothing posts after approving | Check the **Publish approved post** workflow run log. |
| Scheduled runs stopped | GitHub disables cron after 60 days of repo inactivity. The state commits normally prevent this; if it happens, re-enable in the Actions tab. |
| Duplicate story posted | Two outlets, very different wording. Lower `DUPLICATE_TITLE_RATIO` in `config.py`. |

---

## Known limits

- **Free tiers can change.** The AI call sits behind one interface in
  `services/ai.py` — switching to Groq or OpenRouter is a config change
  (`AI_PROVIDER=openai_compatible` + `AI_BASE_URL`/`AI_API_KEY`/`AI_MODEL`),
  not a rewrite.
- **Actions cron drifts** 5–30 minutes under load. Harmless for news.
- **AI can mistranslate names or numbers.** `compose.py` verifies every number
  against the source article and rejects the post if one was invented — but the
  approval gate is your real safety net. Keep it on for the first couple of weeks.
- **Extraction fails ~15–25% of the time** (paywalls, bot blocks). The pipeline
  tries up to 6 articles per run and falls back to the feed summary.
- **Don't post article photos.** The card is deliberately photo-free; reusing
  AP/Getty/Reuters images risks copyright strikes on your Page.

---

## Layout

```
config.py              all settings + editorial weights
models.py              Candidate / Article / Post
generate.py            entrypoint A — find, write, render, ask
publish.py             entrypoint B — read approvals, post to Facebook
pipeline/
  discover.py          RSS → candidates (incl. Google News URL decoding)
  dedupe.py            URL canonicalisation + fuzzy title matching
  score.py             editorial ranking
  extract.py           article body extraction w/ feed-summary fallback
  compose.py           the Urdu prompt, validation, caption assembly
  render.py            HTML → Chromium → PNG
  facebook.py          Graph API publishing + token health
services/
  ai.py                provider-agnostic JSON LLM client
  http.py              retries, timeouts, shared UA
  store.py             state.json as a database
  telegram.py          approval cards, alerts, image storage
  timeutil.py          UTC/PKT handling, Urdu dates
templates/card.html    the card design
scripts/               fetch_fonts · check_setup · preview_card
```
