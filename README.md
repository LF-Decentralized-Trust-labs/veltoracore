# VeltoraCore

**A mechanism-level clearing and liquidity-optimisation engine.** VeltoraCore is an [approved lab](https://lf-decentralized-trust-labs.github.io/labs/approved/VeltoraCore.html) of LF Decentralized Trust (Linux Foundation).

Each period, the engine solves one optimisation that jointly decides the clearing price, the fee, the executed quantities and how provider capital is allocated. The price and the fee are outputs of the clearing, not inputs taken from an oracle or a fixed curve. The economic foundation is the **Joint Clearing Bargain**: a scoring rule for two jointly determined objectives, provider capital and executed flow, whose single governing parameter is measured from the market instead of being chosen by the operator.

> **Status:** research prototype under active development. It has not been audited, so do not use it with real funds.

## Resources

**Papers**
- **Theory:** Videira, H. de C. (2026). *The Joint Clearing Bargain: How a Two-Sided Market Prices Itself.* [SSRN working paper](https://ssrn.com/abstract=7107418)
- **Architecture:** Videira, H. de C., Aragão Pereira, J. P., and Grossi, B. (2026). *OptiMesh: A System-Level Framework for Joint Execution and Liquidity Optimization in Fragmented Markets.* [SSRN working paper](https://ssrn.com/abstract=6826599)
- **Settlement and Proof-of-Optimization commitments:** Videira, H. de C. (2025). *Listrack: A Custom Interoperability Design for Regulated Blockchains.* [TechRxiv preprint](https://www.techrxiv.org/doi/full/10.36227/techrxiv.174650571.12702688/v1)

**Videos**
- [VeltoraCore: The Optimization Layer for Fragmented Markets](https://www.youtube.com/watch?v=GF_PfLbDNr4): recording of the LF Decentralized Trust session, streamed live on 17 September 2026 (YouTube)
- [SentinelGrid in 3 minutes](https://www.loom.com/share/e20b0da592944369bb20a95ea76d0573): the hackathon prototype built on VeltoraCore (Loom)

**Events**
- [LF Decentralized Trust SF Meetup, 17 September 2026](https://www.meetup.com/lfdt-sf/events/315617411/): event page (recording above)
- [Lab page at LF Decentralized Trust Labs](https://lf-decentralized-trust-labs.github.io/labs/approved/VeltoraCore.html)

## Community

- **Chat:** join the [LF Decentralized Trust Discord](https://discord.lfdecentralizedtrust.org) and find the **#veltoracore** channel.
- **Issues and pull requests:** use this repository's [issue tracker](https://github.com/LF-Decentralized-Trust-labs/veltoracore/issues).
- **Maintainers:** see [MAINTAINERS.md](MAINTAINERS.md).

## News

- **September 2026:** Team VeltoraCore took 2nd place in its problem space (Market Manipulation, Agent Herding & Stability) at the C:\>DIR Global "Agentic Regulator" Hackathon (Cambridge Digital Innovation and Regulation Initiative). The field drew more than 900 applicants from over 65 countries, with 36 finalists judged by international regulators. The entry, SentinelGrid, places VeltoraCore's clearing at the centre of a set of supervisory agents, with a human supervisor approving every action.

## Roadmap

- Maintenance update to the historical and simulation engine.
- Integration with smart contracts.

------------------------------------------------------------------------

## Historical & Simulation Engine

The engine backtests the mechanism on Ethereum mainnet swap data and compares the results against Uniswap liquidity-provider returns.

### 1. System Requirements

-   Python 3.10+ (recommended 3.11 or 3.12)
-   Linux / macOS / Windows (Linux recommended)
-   Alchemy API Key (Ethereum Mainnet access)
-   Internet connection (for historical extraction phase)

------------------------------------------------------------------------

### 2. Python Installation

Check version:

    python3 --version

If not installed (Ubuntu):

    sudo apt update
    sudo apt install python3 python3-venv python3-pip

------------------------------------------------------------------------

### 3. Create Virtual Environment

From project root:

    python3 -m venv .venv

Activate:

Linux / macOS: source .venv/bin/activate

Windows (PowerShell): .venv\Scripts\Activate.ps1

------------------------------------------------------------------------

### 4. Install Dependencies

    pip install -r requirements.txt

The solver is OR-Tools (CBC); no commercial solver is needed.

Check the install:

    python -m pytest tests/ -q

------------------------------------------------------------------------

### 5. Configure Alchemy API Key

Linux / macOS:

    export ALCHEMY_KEY="your_alchemy_key_here"

Windows (PowerShell):

    setx ALCHEMY_KEY "your_alchemy_key_here"

Restart terminal after Windows setx.

Verify:

    echo $ALCHEMY_KEY

------------------------------------------------------------------------

### 6. Project Execution Flow

There are two main phases:

1.  Historical Data Extraction\
2.  Simulation / VeltoraCore Engine

------------------------------------------------------------------------

### 7. config.py -- Parameter Explanation

#### Historical Block Configuration

    START_BLOCK = 19230000
    END_BLOCK   = START_BLOCK + int((N_BINS*BIN_SECONDS)/ETHEREUM_BIN)
    N_BINS      = int(7*24*60*60/BIN_SECONDS)   # 7 days

-   START_BLOCK → first Ethereum block to analyze
-   END_BLOCK → last block (derived from N_BINS and BIN_SECONDS)
-   BIN_SECONDS / ETHEREUM_BIN → bin length (600 s = 50 blocks)

Ethereum produces \~7200 blocks/day.

------------------------------------------------------------------------

#### LP Inventory

    CARRY_INVENTORY = True

-   True → one LP set per run, seeded 50/50 once; each LP's inventory
    (x, y) carries from bin to bin
-   False → legacy mode: inventory restruck 50/50 at the previous price
    every bin (reproduces results produced before this option existed)

------------------------------------------------------------------------

#### Simulation Parameters

    INITIAL_PRICE = 2000
    BIN_SECONDS = 600
    SECONDS_PER_YEAR = 31536000

-   INITIAL_PRICE → used only in stochastic mode
-   Historical mode uses first-bin VWAP
-   BIN_SECONDS → duration of each bin
-   SECONDS_PER_YEAR → annualization diagnostics

------------------------------------------------------------------------

#### Fee Parameters

    FEE_UNISWAP_V2 = 0.003
    FEE_UNISWAP_V3_005 = 0.0005
    FEE_UNISWAP_V3_03 = 0.003

Used for fee reconstruction and comparisons.

------------------------------------------------------------------------

#### Liquidity Drift Filter (Historical Mode)

    LIQ_DRIFT_THRESHOLD = 0.01
    LIQ_PRICE_MULTIPLIER = 2.0

Bins are excluded if liquidity movement exceeds threshold relative to
price movement.

------------------------------------------------------------------------

### 8. Step 1 -- Run Historical Pipeline

This downloads and processes:

-   swaps
-   liquidity snapshots
-   bin construction
-   Uniswap LP returns

Run:

    python historical_pipeline.py

Generated files:

    historical_data/
        swaps.parquet
        liquidity.parquet
        bins.parquet
        uniswap_bins.parquet

⚠ Slow and API-heavy.\
If data already exists, do NOT rerun.

------------------------------------------------------------------------

### 9. Step 2 -- Run Simulation (Historical Mode)

Ensure in config.py:

    RUN_HISTORICAL = True

Then run:

    python run_multibin.py

This will:

-   Build solver instances
-   Run VeltoraCore
-   Compare against Uniswap
-   Output summary CSV

------------------------------------------------------------------------

### 10. When to Rerun Historical Pipeline

Re-run ONLY if:

-   START_BLOCK changes
-   END_BLOCK changes
-   BIN_SIZE_BLOCKS changes

If adjusting solver parameters only:

    python run_multibin.py

------------------------------------------------------------------------

### 11. Output Files

After simulation:

    summary_HISTORICAL_*.csv

Containing:

-   Per-bin returns
-   Cumulative returns
-   Mark-to-market LP return (`opt_lp_return_bin_mtm`,
    `opt_log_cum_return_mtm`) on the same basis as Uniswap's raw LP
    return; `opt_x`, `opt_y` = pool inventory after the bin
-   VeltoraCore vs Uniswap comparison
-   Solver metrics

------------------------------------------------------------------------

### 12. Typical 7-Day Workflow

    source .venv/bin/activate
    export ALCHEMY_KEY="..."
    python historical_pipeline.py
    python run_multibin.py

------------------------------------------------------------------------

### 13. Notes

-   First bin is calibration only
-   Abnormal liquidity bins are filtered
-   Cumulative returns are recomputed locally
-   Net LP inflows/outflows assumed second-order within bins
-   Every LP takes part in every candidate solve, pro rata to its capital; an LP's `floorAPR`
    is reported (`lps_below_floor`), not enforced

------------------------------------------------------------------------

## Licence

Apache-2.0. See [LICENSE](LICENSE).
