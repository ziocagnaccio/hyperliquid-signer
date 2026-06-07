from flask import Flask, request, jsonify
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants
import eth_account

app = Flask(__name__)

MAIN_WALLET = "0x39766bC02d31134a16F0F66d000B47FAD9398e75"
COIN        = "ETH"
LEVERAGE    = 10

@app.route('/place-order', methods=['POST'])
def place_order():
    try:
        data      = request.json
        private_key = data['privateKey']
        is_buy    = data['isBuy']
        sz        = float(data['sz'])
        price     = float(data['price'])
        tp_price  = float(data['tpPrice'])
        sl_price  = float(data['slPrice'])

        account  = eth_account.Account.from_key(private_key)
        exchange = Exchange(
            account,
            constants.MAINNET_API_URL,
            account_address=MAIN_WALLET
        )

        # Set leverage (10x isolated)
        exchange.update_leverage(LEVERAGE, COIN, is_cross=False)

        # Main order
        order = exchange.order(
            COIN,
            is_buy,
            sz,
            price,
            {"limit": {"tif": "Gtc"}},
            reduce_only=False
        )

        # TP order (opposite direction, reduce only)
        tp_order = exchange.order(
            COIN,
            not is_buy,
            sz,
            tp_price,
            {"limit": {"tif": "Gtc"}},
            reduce_only=True
        )

        # SL order (trigger stop, opposite direction, reduce only)
        sl_order = exchange.order(
            COIN,
            not is_buy,
            sz,
            sl_price,
            {"trigger": {"triggerPx": sl_price, "isMarket": True, "tpsl": "sl"}},
            reduce_only=True
        )

        return jsonify({
            "status":   "ok",
            "order":    order,
            "tp_order": tp_order,
            "sl_order": sl_order
        })

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/close-position', methods=['POST'])
def close_position():
    try:
        data        = request.json
        private_key = data['privateKey']

        account  = eth_account.Account.from_key(private_key)
        exchange = Exchange(
            account,
            constants.MAINNET_API_URL,
            account_address=MAIN_WALLET
        )

        # Close all open ETH positions (needed for flip logic)
        result = exchange.market_close(COIN)

        return jsonify({"status": "ok", "result": result})

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
