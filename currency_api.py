import requests
from dotenv import load_dotenv
import os
load_dotenv()


def get_currency_rate(default: str='USD', currencies: list[str]=['RUB', 'EUR', 'GBP']):
    url = "https://api.exchangerate.host/live"
    params = {
        'access_key': os.getenv('API_KEY'),
        'source': default,
        'currencies': ','.join(currencies)
    }
    response = requests.get(url, params=params)
    data = response.json()
    return data

if __name__ == '__main__':
    print(get_currency_rate())