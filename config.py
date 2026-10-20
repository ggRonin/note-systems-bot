# ---------------------------------------------------------------- modules (1 = on, 0 = off)
# Run order per account: FAUCET -> STREAK_GUARD -> DEPOSIT -> SHIELD -> BONDS -> DESK -> STAKE -> LOCK -> VOTE
#   -> BUYBACK -> SETTLE -> OMNICHAIN -> VAULT -> CREDIT -> BUILDER -> SWAP -> PROPOSALS -> LOGIN/NICK
#   (nick is allowed only after a deposit)
LOGIN = 1    # sign in on note.systems/community and show points (via the account's proxy)
NICK = 0     # set a random nick if the account has none (the site allows it only after the first deposit)
FAUCET = 1   # claim 1,000 USDG + 10 of each stock token (every 24h, direct RPC)
OPTIMIZER = 1  # optimizer.py decides COUPON / SHIELD, series and sizes for the most points (replaces DEPOSIT + SHIELD)
DEPOSIT = 0  # (only with OPTIMIZER = 0) plain USDG COUPON into random open series
SHIELD = 1   # (only with OPTIMIZER = 0) SHIELD into free COUPON demand + COUPON+SHIELD pairs over every stock held
STREAK_GUARD = 0  # right after the faucet: position health (Sound/Watch/At risk/Breached) + re-entry within 48h of a breach
                  # (Season 0 only: the streak and the Salvage Window belong to the frozen Season 0 profile)
BONDS = 1    # claim vested NOTE from bonds + buy one USDG bond (Bonds stream + "bond" quest; NOTE for the apps below)
# pre-season apps (apps.py): each app used in a week adds +0.25x breadth (cap 2x) and a quest
DESK = 1       # deposit part of the USDG into the Desk (never withdraws: a request within 7 days forfeits the week)
STAKE = 1      # stake part of the NOTE into sNOTE (never starts a cooldown: that forfeits the week)
LOCK = 1       # lock part of the NOTE in veNOTE (voting power for VOTE)
VOTE = 1       # put the veNOTE power on a gauge (re-vote every 10 days)
BUYBACK = 1    # sell a little NOTE into the buyback auction (skipped while the auction has no USDG reserve)
SETTLE = 1     # settle struck positions: mints the legs (needed by OMNICHAIN) and refunds the unmatched USDG
OMNICHAIN = 1  # move a part of one live COUPON leg to Arbitrum Sepolia (~0.0002 ETH LayerZero fee)
VAULT = 1      # deposit part of the USDG into a Build vault (one random vault per wallet, topped up; never withdraws)
CREDIT = 1     # Credit (Morpho): lend USDG every run + one small loan against a wrapped COUPON leg (never repays/withdraws)
BUILDER = 1    # deploy one vault per wallet through the vault factory ("Builder" quest, x3)
SWAP = 1       # Swap app (swap.py): swaps + stock inventory + Senior USDG + Backstop (swap stream 8% and 7 quests)
PROPOSALS = 0  # vote on open governance proposals with veNOTE (no stream or quest pays for it: only gauge votes count)

# ---------------------------------------------------------------- deposits
DEPOSIT_COUNT = 3         # new deposits per account on every run (different stocks first: "Spread" quest needs 3)
DEPOSIT_PCT = (4, 8)      # each deposit = random % of the USDG balance (leaves USDG for pairs, bonds and the Desk)

# ---------------------------------------------------------------- optimizer (see optimizer.py for the points model)
OPT_MIN_KEEP = 0.95            # Tight/Aggressive only if the barrier holds with >= 95% (a breach there zeroes the points)
OPT_HORIZON_DAYS = 60         # days of points counted per series at most (the season end is not announced)
# anti-sybil: every account and run draws its own values from these ranges, so wallets do not repeat one pattern
OPT_BUDGET_PCT = (60, 95)      # % of the USDG balance used per run
OPT_MAX_SERIES_SHARE = (0.25, 0.5)  # one series takes at most this share of a run's budget
OPT_CHUNKS = (25, 60)          # the budget is handed out in this many steps
OPT_JITTER = 0.35              # each series' weight is multiplied by a random 1 +- this (own taste per wallet)
OPT_SKIP_STOCK = 0.25          # chance a stock is left out of the first "one step per stock" pass
OPT_MIN_CHUNK = 20             # USDG, smallest step
OPT_VOLATILITY = {             # yearly volatility per stock: the chance a Tight/Aggressive barrier breaks
    "AAPL": 0.25, "MSFT": 0.22, "AMZN": 0.30, "META": 0.35,
    "NVDA": 0.50, "TSLA": 0.55, "COIN": 0.65, "HOOD": 0.60,
}

# ---------------------------------------------------------------- bonds
BOND_USDG = (10, 50)      # USDG per bond purchase (random in range)

# ---------------------------------------------------------------- apps (% random in range)
DESK_PCT = (10, 20)       # % of the USDG balance into the Desk per run
STAKE_PCT = (30, 50)      # % of the NOTE balance staked per run
LOCK_PCT = (40, 60)       # % of the NOTE left after staking locked per run
LOCK_DAYS = (365, 730)    # length of a new lock (longer = bigger lock multiplier, max 4 years)
OMNICHAIN_PCT = (5, 15)   # % of one COUPON leg moved away (the rest keeps earning Issuance at home)
VAULT_PCT = (3, 8)        # % of the USDG balance into the vault per run
CREDIT_SUPPLY_PCT = (2, 5)    # % of the USDG balance lent into a Credit market per run
CREDIT_WRAP_PCT = (5, 15)     # % of one COUPON leg wrapped and posted as collateral (once per wallet)
CREDIT_BORROW_PCT = (15, 35)  # % of the allowed loan borrowed: LTV ~6-13% against the 38.5% liquidation line
PROPOSAL_VOTE = (75, 10, 15)  # chance weights of for / against / abstain on a proposal

# ---------------------------------------------------------------- swap (swap.py)
SWAP_COUNT = (1, 3)          # swaps per run (the first ones go to stocks the wallet has not traded: "Three Stocks")
SWAP_USDG = (5, 30)          # USDG spent per buy
SWAP_SELL_CHANCE = 0.3       # chance a swap sells a faucet stock instead of buying
SWAP_SELL_PCT = (10, 30)     # % of that stock's balance sold
SWAP_SLIPPAGE_BPS = 100      # tolerance against the quote (1%)
SWAP_INVENTORY_PCT = (5, 12)  # % of one faucet stock supplied to its Regular / Continuous vault per run (the rest backs SHIELD)
SWAP_SENIOR_PCT = (2, 5)     # % of the USDG balance into the Senior USDG vault per run
SWAP_BACKSTOP_USDG = (20, 60)  # one Backstop deposit per wallet, USDG
# ---------------------------------------------------------------- captcha (LiteSpeed reCAPTCHA in front of note.systems)
CAPTCHA_SERVICE = "capsolver"   # "capsolver", "2captcha" or "anticaptcha"
# the service's API key sits in captcha_key.txt next to this file (one line; *.txt never goes into git)
_KEY_FILE = __import__("pathlib").Path(__file__).with_name("captcha_key.txt")
CAPTCHA_KEY = _KEY_FILE.read_text(encoding="utf-8").strip() if _KEY_FILE.exists() else ""

# ---------------------------------------------------------------- threads
THREADS = 27  # accounts processed in parallel

# ---------------------------------------------------------------- pauses, seconds (random in range)
DELAY_BETWEEN_ACCOUNTS = (5, 15)
DELAY_BETWEEN_TX = (3, 10)
