# ---------------------------------------------------------------- modules (1 = on, 0 = off)
# Run order per account: FAUCET -> STREAK_GUARD -> DEPOSIT -> SHIELD -> BONDS -> LOGIN/NICK (nick is allowed only after a deposit)
LOGIN = 1    # sign in on note.systems/community and show points (via the account's proxy)
NICK = 0     # set a random nick if the account has none (the site allows it only after the first deposit)
FAUCET = 1   # claim 1,000 USDG + 10 of each stock token (every 24h, direct RPC)
DEPOSIT = 1  # COUPON deposits into random open series (direct RPC)
SHIELD = 1   # SHIELD every stock held, auto-sized to be fully matched (direct RPC)
STREAK_GUARD = 1  # right after the faucet: position health (Sound/Watch/At risk/Breached) + re-entry within 48h of a breach
BONDS = 1    # claim vested NOTE from bonds + buy one small USDG bond (no points, wallet history only)

# ---------------------------------------------------------------- deposits
DEPOSIT_COUNT = 5         # new deposits per account on every run (into series it hasn't used yet)
DEPOSIT_PCT = (5, 15)     # each deposit = random % of the USDG balance

# ---------------------------------------------------------------- bonds
BOND_USDG = (10, 50)      # USDG per bond purchase (random in range)

# ---------------------------------------------------------------- threads
THREADS = 27  # accounts processed in parallel

# ---------------------------------------------------------------- pauses, seconds (random in range)
DELAY_BETWEEN_ACCOUNTS = (5, 15)
DELAY_BETWEEN_TX = (3, 10)
