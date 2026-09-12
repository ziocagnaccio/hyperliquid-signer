# hyperliquid-signer
# Hyperliquid Signer Bot

A self-hostable signer/execution bot for [Hyperliquid](https://hyperliquid.xyz) that lets anyone deploy their own instance to open and manage leveraged positions on **BTC** and **ETH**.

Every user runs their own copy with their own keys — nothing is custodial, and no funds or signing keys ever leave your machine.

---

## ⚠️ Risk Disclaimer

Leveraged trading carries a high risk of liquidation and loss of principal. This software is provided **as-is**, with no warranty of profitability or uptime. You are solely responsible for:
- The wallet and private key you connect
- The leverage, size, and risk parameters you configure
- Any losses incurred while running this bot

Never share your private key. Never deploy this on a machine or service you don't fully control.

---

## Features

- 🔐 **Self-custodied signing** — your private key stays local (or in your own secrets manager); the bot never transmits it anywhere
- 📈 **BTC & ETH perp support** — long or short with configurable leverage per asset
- ⚙️ **Configurable risk limits** — max leverage, position size caps, and optional stop-loss / take-profit
- 🧾 **Order & fill logging** — every signed action is logged locally for auditing
- 🖥️ **Deploy anywhere** — run locally, in a Docker container, or on your own VPS

---

## How It Works

1. The bot connects to Hyperliquid's API using your **API wallet** (a restricted-permission wallet you generate and approve specifically for this bot — not your main wallet's private key).
2. You define your strategy/trigger parameters (manual commands, a config file, or your own signal source).
3. The bot constructs the order, signs it locally using your API wallet key, and submits it to Hyperliquid.
4. Position and account state are polled and logged so you can monitor leverage, margin, and PnL.

---

## Requirements

- Node.js ≥ 18 (or Python ≥ 3.10, depending on your implementation)
- A Hyperliquid account with funds deposited
- A dedicated **API wallet** generated from the Hyperliquid app (Settings → API) — do **not** use your main wallet's seed phrase
- (Optional) Docker, if deploying in a container

---

## Installation

```bash
git clone https://github.com/<your-org>/hyperliquid-signer-bot.git
cd hyperliquid-signer-bot
npm install   # or: pip install -r requirements.txt
```

---

## Configuration

Copy the example config and fill in your own values:

```bash
cp .env.example .env
```

```env
# .env
HYPERLIQUID_API_WALLET_KEY=your-api-wallet-private-key
HYPERLIQUID_ACCOUNT_ADDRESS=your-main-wallet-address
NETWORK=mainnet            # or testnet

# Risk settings
MAX_LEVERAGE_BTC=5
MAX_LEVERAGE_ETH=5
MAX_POSITION_SIZE_USD=1000
DEFAULT_STOP_LOSS_PCT=5
DEFAULT_TAKE_PROFIT_PCT=10
```

> **Note:** Always test on `testnet` before running with real funds.

---

## Usage

Start the bot:

```bash
npm run start   # or: python main.py
```

Open a leveraged position manually via CLI:

```bash
./bot open --asset BTC --side long --leverage 3 --size 500
./bot open --asset ETH --side short --leverage 2 --size 250
```

Close a position:

```bash
./bot close --asset BTC
```

Check status:

```bash
./bot status
```

---

## Deployment Options

| Method | Use case |
|---|---|
| Local machine | Testing, manual control |
| Docker | Consistent, portable deployment |
| VPS / cloud instance | 24/7 uptime for automated strategies |

Docker example:

```bash
docker build -t hl-signer-bot .
docker run -d --env-file .env hl-signer-bot
```

---

## Security Best Practices

- Use a dedicated **API wallet**, never your primary wallet key
- Set conservative `MAX_LEVERAGE` and `MAX_POSITION_SIZE_USD` limits
- Restrict server/API access with a firewall if deploying remotely
- Rotate API wallet keys periodically
- Monitor liquidation price on every open position

---

## Roadmap

- [ ] Support for additional assets beyond BTC/ETH
- [ ] Webhook/signal integration (TradingView, custom alerts)
- [ ] Telegram/Discord notifications for fills and liquidation warnings

---

## License

MIT
