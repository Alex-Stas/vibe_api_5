import requests
from dotenv import load_dotenv
import os
load_dotenv()


def get_currency_rate(default: str='USD', currencies: list[str]=['RUB', 'EUR', 'GBP']):
    result = {}
    for currency in currencies:
        data = convert_currency(amount=1, from_currency=default, to_currency=currency)
        if data.get("result") is not None:
            result[currency] = data["result"]
    return {"base": default, "rates": result}

def convert_currency(amount: float, from_currency: str, to_currency: str):
    url = "https://api.exchangerate.host/convert"
    params = {
        'access_key': os.getenv('API_KEY'),
        'from': from_currency,
        'to': to_currency,
        'amount': amount
    }
    response = requests.get(url, params=params)
    data = response.json()
    return data

if __name__ == '__main__':
    # data = get_currency_rate(default="RUB", currencies=["USD", "EUR", "GBP", "JPY", "CNY"])
    # print(data["quotes"])
    data = convert_currency(amount=100, from_currency="USD", to_currency="RUB")
    print(data["result"])