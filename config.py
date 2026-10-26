# config.py

# -----------------------------
# Global experiment parameters
# -----------------------------
BIN_SECONDS = 600 # 10 minute bin
SECONDS_PER_YEAR = 365 * 24 * 60 * 60
ETHEREUM_BIN = 12 # number of seconds in each Ethereum Block

LIQUIDITY_USD = 100_000_000 # TOTAL LOCKED CAPITAL OF THE POOL

CAPITAL_TURN_OVER_YEAR = 100 # for computing TOTAL_LIQUIDITY
# 100 means total capital is traded 100 times per year on average)
CAPITAL_TURN_OVER_BIN = CAPITAL_TURN_OVER_YEAR / (SECONDS_PER_YEAR / BIN_SECONDS) # scaled to bin size
# CAPITAL TURN_OVER liquidity per day, scaled to bin size

TOTAL_USER_DEMAND_USD = int(CAPITAL_TURN_OVER_BIN*LIQUIDITY_USD)
# USER DEMAND IS CONSTANT REGARDING THE RISKY ASSET (X) = TOTAL_USER_DEMAND_USD / INITIAL_PRICE
# TOTAL_USER_DEMAND_USD is not constant across different bins, only TOTAL_USER_DEMAND_X

# Risk-free + premium (annualized)
RISK_FREE_APR = 0.035          # 3.5% Treasury
MARKET_PREMIUM_TOP = 0.08          # 8.0% upper bound market premium
MARKET_PREMIUM_FLOOR = 0.04          # 4.0% equity premium

# Fee grid (VeltoraCore)
FEE_GRID = [
        0.0001,  # 0.01%
        0.00025, # 0.025%
        0.0005,  # 0.05%
        0.001,  # 0.10%
        0.002,  # 0.20%
        0.003,  # 0.30%
        0.005,  # 0.50%
        0.01     # 1%
    ]

INITIAL_PRICE = 2000 # USD per unit of ETH
## PRICE VARIATION FOR BUILDING THE PRICE GRID
PRICE_BAND = 0.003  # ±0.3%

## PARAMETERS FOR STOCHASTIC PRICE GENERATION
ANNUAL_VOL = 0.80 # Annual volatility for geometric Brownian motion price generator (if PRICE_STOCHASTIC is True)
ANNUAL_DRIFT = 0.05 # Annual drift for geometric Brownian motion price generator (if PRICE_STOCHASTIC is True)
'''
Typical ETH-like long-run drift might be:
0% (neutra)
5% (mild bull)
15% (strong bull)
'''
N_USERS = int(((BIN_SECONDS/60)*5)*(CAPITAL_TURN_OVER_YEAR)) # APPROXIMATELY 5 USERS PER MINUTE (ETHEREUM)
N_LPS = N_USERS

# --------------------------------------------------
# specific Experiment configuration
# --------------------------------------------------
#N_BINS = int(30*24*60*60/BIN_SECONDS)             # 30 DAY SIMULATION
N_BINS = int(7*24*60*60/BIN_SECONDS)              # 7 DAY SIMULATION
#N_BINS = 10             # DEBUG: 10 BINS
RANDOM_SEED = 42

### Historical Config  and MAIN EXPERIMENT CONSTANTS ###
START_BLOCK = 19230000
END_BLOCK   = START_BLOCK + int((N_BINS*BIN_SECONDS)/ETHEREUM_BIN)

OUTPUT_DIR = "historical_data"

PRICE_STOCHASTIC = False
# SET PRICE GENERATION AS STOCHASTIC
# IF NOT STOCHASTIC PRICE EVOLVES BASED ON LAST BIN PRICE OR UNISWAP

RUN_HISTORICAL = True# Run Simulation based on Historical Data

FEE_MATCH_UNISWAP = False    # If True, VeltoraCore fee grid is ignored and fee is set to FEE_UNISWAP for direct comparison
FEE_UNISWAP = 0.003  # 0.30% Uniswap v3 fee tier
# Number of Users and LPs per bin

if RUN_HISTORICAL:
    PRICE_STOCHASTIC = False
else: PRICE_STOCHASTIC = True

if PRICE_STOCHASTIC:
    FEE_MATCH_UNISWAP = True    # If True, VeltoraCore fee grid is ignored and fee is set to FEE_UNISWAP for direct comparison

OUTPUT_CSV = "summary.csv"

# CARRY-1: carry each LP's inventory (x, y) from bin to bin (operational mode).
# False reproduces the legacy per-bin 50/50 restrike at the previous price.
CARRY_INVENTORY = True

# JCB-SIMPLE-1: how the sweep picks the winning (price, fee).
#   "jcb"    -> Joint Clearing Bargain score z = m^p * (1 - r^2)^q (Videira 2026,
#               SSRN 7107418, eqs. 1 and 3), attainments normalized per bin.
#   "legacy" -> the original protocol_objective (reproduces older results).
OBJECTIVE = "jcb"
JCB_P_PRICE_ON = 0.76    # price and fee both swept (preprint, section 4)
JCB_P_PRICE_OFF = 0.60   # fee is the only instrument (single-price grid)

### Liquidity Drift Filter (Historical Mode)

LIQ_DRIFT_THRESHOLD = 0.01
LIQ_PRICE_MULTIPLIER = 2.0

#Bins are excluded if liquidity movement exceeds threshold relative to price movement.