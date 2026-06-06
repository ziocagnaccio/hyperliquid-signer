from flask import Flask, request, jsonify
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants
import eth_account
import json

app = Flask(__name__)

@app.route('/place-order', methods=['POST'])
def place_order():
    try:
        data = request.json
        private_key = data['privateKey']
        is_buy = data['isBuy']
        sz = float(data['sz'])
        price = float(data['price'])
        tp_price = float(data['tpPrice'])
        sl_price = float(data['slPrice'])

        account = eth_account.Account.from_key(private_key)
        exchange = Exchange(account, constants.MAINNET_API_URL)

        order = exchange.order(
            "BTC",
            is_buy,
            sz,
            price,
            {"limit": {"tif": "Gtc"}},
            reduce_only=False
        )

        return jsonify({"status": "ok", "result": order})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
