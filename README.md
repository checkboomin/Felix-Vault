# Felix Vault — Delta-Neutral Carry Trade Prototype

A working prototype of the Felix carry-trade vault on **Hyperliquid testnet**. It deploys
pooled USDC across reliable HIP-3 RWA perp markets, collects funding-rate income, and
returns yield to depositors minus an **8% performance fee** accrued hourly and claimable by
Felix.

> ⚠️ **Testnet only, fake funds.** Do not point this at mainnet or fund it with real keys.

---

## Architecture

| Layer | File(s) | What it does |
|------|---------|--------------|
| **Vault** (Solidity / HyperEVM) | `contracts/FelixVault.sol` | Deposits, ERC-4626-style shares, funding-yield accounting, 8% fee accrual + claim. Emits `DeployCapital` / `UnwindCapital` events that drive off-chain execution. |
| **Operator bot** (Python) | `bot/operator.py` + modules | Listens for vault events, opens the **real short perp** + **simulated long spot**, tracks PnL/hedge drift, collects funding hourly, reports yield back to the vault. |
| **Dashboard** (HTML) | `dashboard/index.html` | Deposit/withdraw UI, live position table, yield chart, operator log. ethers.js + HL API, no framework. |
| **Scripts** | `scripts/deploy.py`, `scripts/test_full_flow.py` | Deploy the vault; run the full deposit → trade → funding → withdraw demo. |

### The spot leg is SIMULATED (the key design decision)

A carry trade is **long spot + short perp** so it is delta-neutral: spot and perp price moves
cancel, leaving only funding income.

In this prototype:

- **Spot leg = SIMULATED** in the bot's memory (`bot/spot_simulator.py`). **No spot order is
  ever placed.** The simulated spot is priced off Hyperliquid's own **mark price**.
- **Perp leg = REAL** — an actual short position on HL testnet (fake funds).

Because **both legs reference the same HL mark price**, they cancel perfectly:

```
net PnL  =  simulated_spot_pnl  +  real_perp_pnl  +  funding
         ≈        +X            +      −X          +  funding
         ≈  funding   ← pure carry-trade yield (what we're measuring)
```

The dashboard's *Net PnL ≈ Funding Earned* column is the live proof the hedge holds.

**Why this works / why it's enough for a prototype:** it proves the full yield mechanic —
selection, allocation, funding collection, fee accrual, share-price growth, withdrawals —
against *real market data* before real spot execution is available. In production, Felix
buys actual spot (Ondo tokenized equities / HIP-3 spot tokens); only `spot_simulator.py`
changes. It exposes a clean `SpotTracker` interface (`open` / `close` / `get_pnl` /
`get_all_positions`) so you swap `SimulatedSpotTracker` → `RealSpotTracker` and touch
nothing else.

---

## Project structure

```
felix-vault/
├── contracts/
│   ├── FelixVault.sol            # the vault
│   ├── interfaces/
│   │   ├── IERC4626.sol          # ERC-20 + ERC-4626 surface
│   │   └── IHyperliquidPerp.sol  # documents the bot↔HL perp interaction
│   └── test/FelixVault.test.sol  # Foundry tests
├── bot/
│   ├── operator.py               # main loop, event listener, execution
│   ├── hyperliquid_client.py     # HL testnet API + order placement
│   ├── spot_simulator.py         # SIMULATED spot tracker (swappable interface)
│   ├── allocation.py             # market selection + greedy allocation
│   └── config.py                 # env-driven config
├── dashboard/index.html          # single-file UI
├── scripts/{deploy.py,test_full_flow.py}
├── requirements.txt
├── hardhat.config.js
└── README.md
```

---

## Setup

### 1. Get testnet funds
- HyperEVM/Hyperliquid testnet USDC from the [Hyperliquid testnet faucet](https://app.hyperliquid-testnet.xyz/).
- A testnet wallet with some gas on HyperEVM (chain **998**, RPC `https://rpc.hyperliquid-testnet.xyz/evm`).

### 2. Configure `.env`
```bash
cp .env.example .env
# edit .env: OPERATOR_PRIVATE_KEY, USDC_TESTNET_ADDRESS, FELIX_TREASURY_TESTNET
```

### 3. Install deps
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# (optional, for hardhat compile/deploy) npm install
```

### 4. Deploy the vault
```bash
python scripts/deploy.py
# writes deployment.json with the address + ABI; copy the address into .env VAULT_ADDRESS
```
`deploy.py` uses a Hardhat artifact if present (`npx hardhat compile`), otherwise compiles
`FelixVault.sol` on the fly with `py-solc-x`.

### 5. Start the operator bot
```bash
python -m bot.operator
```
> Run it as `python -m bot.operator` (a module), **not** `python bot/operator.py`. The file is
> named `operator`, which collides with Python's stdlib `operator` module; running it as part
> of the `bot` package keeps the stdlib module intact for dependencies. (The script also
> defends its own `sys.path` so the direct form works too, but the module form is preferred.)

Set `DRY_RUN=true` in `.env` to simulate perp fills (no real testnet orders) while you wire
everything up.

### 6. Open the dashboard
```bash
python -m http.server 8000      # from the repo root, so it can read bot/state.json + deployment.json
# then open http://localhost:8000/dashboard/
```
Serving from the repo root lets the dashboard read `deployment.json` (vault address + ABI)
and `bot/state.json` (the simulated-spot data, which lives only in bot memory). Opening the
file directly still works but those two enrichments degrade gracefully.

### 7. Run the full end-to-end demo
```bash
FAST_DEMO=1 python scripts/test_full_flow.py   # short funding wait for a quick demo
python scripts/test_full_flow.py               # real 1-hour funding wait
```

---

## How yield flows

1. **Deposit** → vault mints shares (1:1 on first deposit, else at current NAV), emits
   `DeployCapital` per market.
2. **Bot** opens a real short perp + simulated long spot in each market, sized so the legs are
   delta-neutral.
3. **Funding** accrues hourly to the short perp. The bot reads it via `/userFunding`, calls
   `reportFundingYield(marketId, amount)` → `accumulatedYield` grows → **share price rises**.
4. **Fees**: `accrueHourlyFees()` charges 8% of new funding yield into `pendingFees`;
   `claimFees()` (feeRecipient only) sweeps it to the Felix treasury. A high-water-mark
   performance fee is also charged on profit at withdrawal.
5. **Withdraw** → burns shares, returns USDC at current NAV minus the performance fee on
   profit above the depositor's high-water mark, and emits `UnwindCapital` so the bot closes
   the proportional perp + simulated spot.

## Testing the contract

Foundry:
```bash
forge install foundry-rs/forge-std   # if not vendored
forge test --match-contract FelixVaultTest -vv
```
The suite covers 1:1 first deposit, NAV-priced subsequent deposits, share-price growth from
reported yield, the 8% performance fee on profit (and no fee on losses), hourly accrual
(no double-charge), fee claiming permissions, and the `DeployCapital` event.

---

## Notes & caveats (it's a prototype)

- **Funding USDC custody.** On-chain `reportFundingYield` increases share price; in this
  prototype the corresponding USDC is assumed to be moved into the vault by the operator
  (testnet bookkeeping). A production version would settle funding into the vault atomically.
- **Per-market deploy amounts** emitted by the contract are advisory; the bot's
  `compute_allocation` (greedy highest-yield-first, capped by capacity / 30% TVL / 45% OI)
  is the source of truth for sizing.
- **Reliability gate** in `allocation.py` mirrors the capacity dashboard's "Reliable"
  definition (trimmed CV < 0.75, % time positive > 80%, % time > 9% APY > 60%).
- The operator key signs **both** HL orders and HyperEVM vault transactions for simplicity.
```
Spot leg: SIMULATED (mark price tracking)
Perp leg: REAL (HL testnet positions)
```
