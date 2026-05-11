# WhaleWatch

A premium Streamlit dashboard for ranking public Polymarket wallets that may be worth researching for copy-trading ideas.

This is research-only software. It does not place trades, sign orders, request private keys, or connect to a wallet.
Users must log in with Firebase Authentication before using the dashboard. Watchlists, alert settings, and Whale Mode preference are stored per user in Firestore.

## What it does

- Pulls public wallet data from Polymarket APIs.
- Discovers candidate wallets from recent public Polymarket trades, or lets you paste wallet addresses manually.
- Calculates whale-focused metrics: net profit, ROI, win rate, adjusted win rate, total traded volume, average trade size, largest trade, resolved markets, copyability, and bot-likeness warnings.
- Whale Mode is enabled by default and prioritizes wallets with meaningful capital.
- Ranks wallets from best to worst using a reliability-adjusted score:

```text
Whale Score =
Profit x 0.30
+ Win Rate x 0.25
+ Volume Traded x 0.20
+ Position Sizing x 0.10
+ Consistency x 0.10
+ Activity Frequency x 0.05
```

Each whale metric is normalized from 0 to 100 before weighting. Bot-like repetition, tiny high-frequency flow, concentrated one-off wins, and weak sizing reduce the score. Wallets are grouped into tiers: Kraken, Leviathan, Blue Whale, Shark, and Dolphin.

Standard Score =
Adjusted Win Rate x 0.40
+ ROI x 0.20
+ Copyability x 0.20
+ Liquidity Quality x 0.10
+ Trade Count Reliability x 0.10
```

Adjusted Win Rate is `(wallet wins + 5) / (wallet resolved markets + 10)` so small lucky samples do not rank first.

- Uses weighted scoring and moderate filters instead of excluding wallets for a single imperfect metric.
- Shows why each wallet ranked highly and includes a Trending Whales section for unusually strong recent activity.
- Exports the ranked table to CSV.
- Lets you click/select a ranked wallet to inspect recent trades, market links, open/resolved status, and trade details.
- Includes a private read-only copy watchlist that can refresh every 30 or 60 seconds, highlight trades from whale wallets, and show optional popup alerts for new watched-wallet trades.
- Caches API results in SQLite at `data/polymarket_tracker.sqlite`.

## Data sources

The app uses public, unauthenticated Polymarket endpoints:

- `https://data-api.polymarket.com/closed-positions`
- `https://data-api.polymarket.com/trades`
- `https://clob.polymarket.com/prices-history`

Order-book liquidity/open-interest data is not required for the MVP. WhaleWatch uses safer trade-data proxies instead: total traded volume, average trade size, unique markets, recent activity, and resolved market count.

The Polymarket Data API currently limits `/trades` historical pagination. The app uses `MAX_OFFSET = 3000`, never requests offsets at or above that limit, caches recent trade pages, and scans discovery candidates by one-day windows inside the selected lookback period.

The CLOB `prices-history` endpoint is treated as best-effort. The app never asks for large ranges in one request; it fetches one-day chunks first, retries unavailable windows in six-hour chunks, caches each token/time window, and keeps ranking wallets if entry timing cannot be estimated.

Fast Mode scans at most 1,000 recent trades, analyzes at most 50 candidate wallets, skips price history and deep wallet history, and stops long-running work after 60 seconds. In Whale Mode, Fast Mode is best treated as a quick whale-candidate scan; turn it off for net profit and resolved-market whale filters.

## Install

1. Install Python 3.10 or newer.
2. Open a terminal in this folder.
3. Create and activate a virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

4. Install dependencies:

```powershell
pip install -r requirements.txt
```

## Firebase Setup

1. Create a project in the [Firebase Console](https://console.firebase.google.com/).
2. Go to **Authentication > Sign-in method** and enable **Email/Password**.
3. Go to **Firestore Database** and create a database.
4. Set Firestore security rules so users can only read and write their own data:

```text
rules_version = '2';

service cloud.firestore {
  match /databases/{database}/documents {
    match /users/{userId}/{document=**} {
      allow read, write: if request.auth != null && request.auth.uid == userId;
    }
  }
}
```

5. Create `.streamlit/secrets.toml` in this project folder:

```toml
FIREBASE_WEB_API_KEY = "your-firebase-web-api-key"
FIREBASE_PROJECT_ID = "your-firebase-project-id"
```

Find the Web API key in **Project settings > General > Web API Key**. Use the Firebase project ID from the same settings page. Do not add service account files, private keys, seed phrases, or trading credentials.

WhaleWatch stores user data in these Firestore paths:

```text
users/{user_id}/watchlist/{wallet_address}
users/{user_id}/settings/main
```

## Run

```powershell
streamlit run app.py
```

Streamlit will print a local URL, usually `http://localhost:8501`.

## How to use

1. Sign up or log in with email and password.
2. Choose a category and time period in the sidebar.
3. Click **Discover Wallets** to scan recent public trades for active candidate wallets.
4. Optionally paste known wallets and click **Analyze manual wallets**.
5. Keep Whale Mode on for a whale-focused first pass:
   - minimum whale score
   - minimum realized profit
   - minimum total volume
   - minimum average position size
   - minimum win rate
   - optional aggressive trader inclusion
   - optional bot inclusion
   - score penalties for one-off wins and repetitive bot-like flow
6. Review the ranked dashboard table.
7. Add wallets to your private watchlist.
8. Click **Export results to CSV** if you want the results in a spreadsheet.

## Notes for beginners

- A high score is not a trading recommendation.
- Wallets can change behavior.
- Public API data may be delayed, incomplete, or temporarily unavailable.
- Copy-trading can still lose money, especially in thin markets.
- The entry timing metric is an estimate: it checks whether prices moved favorably after the wallet's historical entries, when price history is available.

## Project structure

```text
app.py                    Streamlit dashboard
polymarket_tracker/api.py Public API client
polymarket_tracker/db.py  SQLite cache
polymarket_tracker/metrics.py Scoring and filters
polymarket_tracker/firebase_store.py Firebase auth and per-user storage
requirements.txt          Python dependencies
```
