# ---------------------------------------------------------------- modules (1 = on, 0 = off)
# Run order per account: FAUCET -> STREAK_GUARD -> DEPOSIT -> SHIELD -> BONDS -> DESK -> STAKE -> LOCK -> VOTE
#   -> BUYBACK -> OMNICHAIN -> LOGIN/NICK (nick is allowed only after a deposit)
LOGIN = 1    # sign in on note.systems/community and show points (via the account's proxy)
NICK = 0     # set a random nick if the account has none (the site allows it only after the first deposit)
FAUCET = 1   # claim 1,000 USDG + 10 of each stock token (every 24h, direct RPC)
DEPOSIT = 1  # COUPON deposits into random open series (direct RPC)
SHIELD = 1   # SHIELD every stock held, auto-sized to be fully matched (direct RPC)
STREAK_GUARD = 1  # right after the faucet: position health (Sound/Watch/At risk/Breached) + re-entry within 48h of a breach
BONDS = 1    # claim vested NOTE from bonds + buy one USDG bond (Bonds stream + "bond" quest; NOTE for the apps below)
# pre-season apps (apps.py): each app used in a week adds +0.25x breadth (cap 2x) and a quest
DESK = 1       # deposit part of the USDG into the Desk (never withdraws: a request within 7 days forfeits the week)
STAKE = 1      # stake part of the NOTE into sNOTE (never starts a cooldown: that forfeits the week)
LOCK = 1       # lock part of the NOTE in veNOTE (voting power for VOTE)
VOTE = 1       # put the veNOTE power on a gauge (re-vote every 10 days)
BUYBACK = 1    # sell a little NOTE into the buyback auction (skipped while the auction has no USDG reserve)
OMNICHAIN = 1  # move a part of one live COUPON leg to Arbitrum Sepolia (~0.0002 ETH LayerZero fee)

# ---------------------------------------------------------------- deposits
DEPOSIT_COUNT = 3         # new deposits per account on every run (different stocks first: "Spread" quest needs 3)
DEPOSIT_PCT = (4, 8)      # each deposit = random % of the USDG balance (leaves USDG for pairs, bonds and the Desk)

# ---------------------------------------------------------------- bonds
BOND_USDG = (10, 50)      # USDG per bond purchase (random in range)

# ---------------------------------------------------------------- apps (% random in range)
DESK_PCT = (10, 20)       # % of the USDG balance into the Desk per run
STAKE_PCT = (30, 50)      # % of the NOTE balance staked per run
LOCK_PCT = (40, 60)       # % of the NOTE left after staking locked per run
LOCK_DAYS = (365, 730)    # length of a new lock (longer = bigger lock multiplier, max 4 years)
OMNICHAIN_PCT = (5, 15)   # % of one COUPON leg moved away (the rest keeps earning Issuance at home)

# ---------------------------------------------------------------- threads
THREADS = 27  # accounts processed in parallel

# ---------------------------------------------------------------- pauses, seconds (random in range)
DELAY_BETWEEN_ACCOUNTS = (5, 15)
DELAY_BETWEEN_TX = (3, 10)
