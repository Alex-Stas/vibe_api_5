import os
import sqlite3
from datetime import datetime, timezone

import telebot
import pycountry
from babel import Locale
from babel.numbers import get_territory_currencies
from dotenv import load_dotenv
from telebot import types

from currency_api import convert_currency

load_dotenv()

API_KEY = os.getenv("API_KEY")
BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = "travel_wallet.db"

if not API_KEY:
    raise RuntimeError("API_KEY не найден в конфигурации.")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не найден в конфигурации.")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")


MENU_NEW_TRIP = "Создать новое путешествие"
MENU_TRIPS = "Мои путешествия"
MENU_BALANCE = "Баланс"
MENU_HISTORY = "История расходов"
MENU_SET_RATE = "Изменить курс"

COUNTRY_ALIASES = {
    "сша": "US",
    "usa": "US",
    "u.s.a": "US",
    "uk": "GB",
    "great britain": "GB",
    "оаэ": "AE",
    "uae": "AE",
    "south korea": "KR",
    "korea": "KR",
    "корея": "KR",
    "north korea": "KP",
    "еврозона": "EU",
}
COUNTRY_NAME_TO_ALPHA2 = {}

user_states = {}
pending_expenses = {}


def get_conn():
    return sqlite3.connect(DB_PATH)


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            created_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS trips (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            home_country TEXT NOT NULL,
            destination_country TEXT NOT NULL,
            home_currency TEXT NOT NULL,
            destination_currency TEXT NOT NULL,
            rate REAL NOT NULL,
            home_balance REAL NOT NULL,
            destination_balance REAL NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            trip_id INTEGER NOT NULL,
            amount_destination REAL NOT NULL,
            amount_home REAL NOT NULL,
            description TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (trip_id) REFERENCES trips(id)
        )
        """
    )
    conn.commit()
    conn.close()


def ensure_user(user_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,))
    if cur.fetchone() is None:
        cur.execute(
            "INSERT INTO users(user_id, created_at) VALUES(?, ?)",
            (user_id, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    conn.close()


def normalize_country(country: str) -> str:
    return " ".join(country.strip().lower().split())


def build_country_index():
    if COUNTRY_NAME_TO_ALPHA2:
        return

    def save_name(name: str, alpha2: str):
        normalized = normalize_country(name)
        if normalized:
            COUNTRY_NAME_TO_ALPHA2[normalized] = alpha2

    for country in pycountry.countries:
        for field in ("name", "official_name", "common_name", "alpha_2", "alpha_3"):
            value = getattr(country, field, None)
            if value:
                save_name(str(value), country.alpha_2)

    for locale_code in ("en", "ru"):
        locale = Locale.parse(locale_code)
        for alpha2, country_name in locale.territories.items():
            if isinstance(alpha2, str) and len(alpha2) == 2 and alpha2.isalpha():
                save_name(str(country_name), alpha2.upper())

    for alias, alpha2 in COUNTRY_ALIASES.items():
        save_name(alias, alpha2)


def country_to_currency(country: str):
    build_country_index()
    normalized = normalize_country(country)
    alpha2 = COUNTRY_NAME_TO_ALPHA2.get(normalized)

    if not alpha2:
        try:
            alpha2 = pycountry.countries.search_fuzzy(country)[0].alpha_2
        except LookupError:
            return None

    currencies = get_territory_currencies(alpha2, tender=True)
    if not currencies:
        return None
    return currencies[0].upper()


def parse_amount(text: str):
    try:
        normalized = text.strip().replace(",", ".").replace(" ", "")
        amount = float(normalized)
        if amount <= 0:
            return None
        return amount
    except (TypeError, ValueError):
        return None


def format_amount(amount: float) -> str:
    text = f"{amount:,.2f}".replace(",", " ")
    text = text.rstrip("0").rstrip(".")
    return text


def home_equivalent_by_rate(destination_balance: float, rate: float) -> float:
    if rate <= 0:
        return 0.0
    return destination_balance / rate


def main_menu_markup():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton(MENU_NEW_TRIP, callback_data="menu_newtrip"))
    kb.add(types.InlineKeyboardButton(MENU_TRIPS, callback_data="menu_trips"))
    kb.add(types.InlineKeyboardButton(MENU_BALANCE, callback_data="menu_balance"))
    kb.add(types.InlineKeyboardButton(MENU_HISTORY, callback_data="menu_history"))
    kb.add(types.InlineKeyboardButton(MENU_SET_RATE, callback_data="menu_setrate"))
    return kb


def persistent_menu_markup():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(
        types.KeyboardButton(MENU_NEW_TRIP),
        types.KeyboardButton(MENU_TRIPS),
        types.KeyboardButton(MENU_BALANCE),
        types.KeyboardButton(MENU_HISTORY),
        types.KeyboardButton(MENU_SET_RATE),
    )
    return kb


def send_text(chat_id: int, text: str, reply_markup=None):
    if reply_markup is None:
        reply_markup = persistent_menu_markup()
    bot.send_message(chat_id, text, reply_markup=reply_markup)


def get_active_trip(user_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, title, home_country, destination_country, home_currency, destination_currency,
               rate, home_balance, destination_balance
        FROM trips
        WHERE user_id = ? AND is_active = 1
        LIMIT 1
        """,
        (user_id,),
    )
    row = cur.fetchone()
    conn.close()
    return row


def set_active_trip(user_id: int, trip_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE trips SET is_active = 0 WHERE user_id = ?", (user_id,))
    cur.execute("UPDATE trips SET is_active = 1 WHERE user_id = ? AND id = ?", (user_id, trip_id))
    conn.commit()
    conn.close()


def convert_with_api(amount: float, from_currency: str, to_currency: str):
    try:
        data = convert_currency(amount=amount, from_currency=from_currency, to_currency=to_currency)
    except Exception:
        return None, "Не удалось связаться с сервисом курсов. Попробуйте ещё раз чуть позже."

    if not isinstance(data, dict):
        return None, "Сервис вернул неожиданный ответ. Попробуйте позже."
    if data.get("success") is False:
        info = ""
        if isinstance(data.get("error"), dict):
            info = data["error"].get("info") or data["error"].get("type") or ""
        return None, f"Ошибка API курсов: {info or 'неизвестная ошибка'}."
    if data.get("result") is None:
        return None, "В ответе API нет результата конвертации."
    return float(data["result"]), None


def create_trip(user_id: int, state: dict, rate: float, initial_home: float, initial_destination: float):
    title = f"{state['home_country']} -> {state['destination_country']}"
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE trips SET is_active = 0 WHERE user_id = ?", (user_id,))
    cur.execute(
        """
        INSERT INTO trips(
            user_id, title, home_country, destination_country,
            home_currency, destination_currency, rate,
            home_balance, destination_balance, is_active, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
        """,
        (
            user_id,
            title,
            state["home_country"],
            state["destination_country"],
            state["home_currency"],
            state["destination_currency"],
            rate,
            initial_home,
            initial_destination,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    conn.close()


def show_main_menu(chat_id: int, user_id: int):
    active = get_active_trip(user_id)
    text = "Мини-кошелёк путешественника.\nВыберите действие ниже."
    if active:
        text += (
            f"\n\nАктивное путешествие: <b>{active[1]}</b>"
            f"\nКурс: 1 {active[4]} = {format_amount(active[6])} {active[5]}"
        )
    send_text(chat_id, text, reply_markup=main_menu_markup())


def start_new_trip(chat_id: int, user_id: int):
    user_states[user_id] = {"step": "await_home_country"}
    send_text(
        chat_id,
        "Введите страну отправления (домашнюю страну). Например: Россия",
    )


def show_trips(chat_id: int, user_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, title, home_currency, destination_currency, rate, is_active
        FROM trips
        WHERE user_id = ?
        ORDER BY id DESC
        """,
        (user_id,),
    )
    rows = cur.fetchall()
    conn.close()
    if not rows:
        send_text(chat_id, "У вас пока нет путешествий. Создайте первое через меню.")
        return
    kb = types.InlineKeyboardMarkup(row_width=1)
    lines = []
    for trip_id, title, home_cur, dest_cur, rate, is_active in rows:
        marker = " (активно)" if is_active else ""
        lines.append(f"• {title}{marker} — 1 {home_cur} = {format_amount(rate)} {dest_cur}")
        kb.add(types.InlineKeyboardButton(f"Сделать активным: {title}", callback_data=f"switch_{trip_id}"))
    send_text(chat_id, "Ваши путешествия:\n" + "\n".join(lines), reply_markup=kb)


def show_balance(chat_id: int, user_id: int):
    trip = get_active_trip(user_id)
    if not trip:
        send_text(chat_id, "Нет активного путешествия. Сначала создайте его или переключитесь.")
        return
    _, title, _, _, home_cur, dest_cur, rate, _, dest_balance = trip
    home_balance = home_equivalent_by_rate(dest_balance, rate)
    text = (
        f"<b>{title}</b>\n"
        f"Остаток: {format_amount(dest_balance)} {dest_cur} = "
        f"{format_amount(home_balance)} {home_cur}"
    )
    send_text(chat_id, text)


def show_history(chat_id: int, user_id: int):
    trip = get_active_trip(user_id)
    if not trip:
        send_text(chat_id, "Сначала выберите активное путешествие.")
        return
    trip_id = trip[0]
    home_cur = trip[4]
    dest_cur = trip[5]
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT amount_destination, amount_home, description, created_at
        FROM expenses
        WHERE user_id = ? AND trip_id = ?
        ORDER BY id DESC
        LIMIT 15
        """,
        (user_id, trip_id),
    )
    rows = cur.fetchall()
    conn.close()
    if not rows:
        send_text(chat_id, "История расходов пока пустая.")
        return
    lines = []
    for amount_dest, amount_home, desc, created_at in rows:
        dt = created_at.split("T")[0]
        lines.append(
            f"{dt}: {desc} — {format_amount(amount_dest)} {dest_cur} "
            f"({format_amount(amount_home)} {home_cur})"
        )
    send_text(chat_id, "Последние расходы:\n" + "\n".join(lines))


def ask_set_rate(chat_id: int, user_id: int):
    trip = get_active_trip(user_id)
    if not trip:
        send_text(chat_id, "Нет активного путешествия для изменения курса.")
        return
    user_states[user_id] = {"step": "await_new_rate"}
    send_text(
        chat_id,
        (
            f"Текущий курс: 1 {trip[4]} = {format_amount(trip[6])} {trip[5]}\n"
            "Введите новый курс вручную (число)."
        ),
    )


@bot.message_handler(commands=["start"])
def cmd_start(message):
    user_id = message.from_user.id
    ensure_user(user_id)
    show_main_menu(message.chat.id, user_id)


@bot.message_handler(commands=["newtrip"])
def cmd_newtrip(message):
    user_id = message.from_user.id
    ensure_user(user_id)
    start_new_trip(message.chat.id, user_id)


@bot.message_handler(commands=["switch"])
def cmd_switch(message):
    user_id = message.from_user.id
    ensure_user(user_id)
    show_trips(message.chat.id, user_id)


@bot.message_handler(commands=["balance"])
def cmd_balance(message):
    user_id = message.from_user.id
    ensure_user(user_id)
    show_balance(message.chat.id, user_id)


@bot.message_handler(commands=["history"])
def cmd_history(message):
    user_id = message.from_user.id
    ensure_user(user_id)
    show_history(message.chat.id, user_id)


@bot.message_handler(commands=["setrate"])
def cmd_setrate(message):
    user_id = message.from_user.id
    ensure_user(user_id)
    ask_set_rate(message.chat.id, user_id)


@bot.callback_query_handler(func=lambda call: True)
def callback_router(call):
    user_id = call.from_user.id
    chat_id = call.message.chat.id
    ensure_user(user_id)
    data = call.data

    if data == "menu_newtrip":
        start_new_trip(chat_id, user_id)
    elif data == "menu_trips":
        show_trips(chat_id, user_id)
    elif data == "menu_balance":
        show_balance(chat_id, user_id)
    elif data == "menu_history":
        show_history(chat_id, user_id)
    elif data == "menu_setrate":
        ask_set_rate(chat_id, user_id)
    elif data.startswith("switch_"):
        trip_id = int(data.split("_", 1)[1])
        set_active_trip(user_id, trip_id)
        send_text(chat_id, "Активное путешествие переключено.")
        show_balance(chat_id, user_id)
    elif data == "rate_ok":
        state = user_states.get(user_id, {})
        if not state:
            send_text(chat_id, "Сессия создания путешествия устарела. Начните заново.")
            return
        state["selected_rate"] = state["api_rate"]
        state["step"] = "await_initial_amount"
        send_text(
            chat_id,
            f"Введите стартовую сумму в {state['home_currency']} (домашняя валюта).",
        )
    elif data == "rate_manual":
        state = user_states.get(user_id, {})
        if not state:
            send_text(chat_id, "Сессия создания путешествия устарела. Начните заново.")
            return
        state["step"] = "await_manual_rate"
        send_text(
            chat_id,
            (
                f"Введите ваш курс вручную (например 12.8):\n"
                f"1 {state['home_currency']} = X {state['destination_currency']}"
            ),
        )
    elif data == "expense_yes":
        pending = pending_expenses.get(user_id)
        if not pending:
            send_text(chat_id, "Нет расхода для подтверждения.")
            return
        user_states[user_id] = {"step": "await_expense_description"}
        send_text(chat_id, "Введите короткое описание расхода (до 20 символов).")
    elif data == "expense_no":
        if user_id in pending_expenses:
            del pending_expenses[user_id]
        send_text(chat_id, "Ок, расход не учтён.")
    bot.answer_callback_query(call.id)


@bot.message_handler(func=lambda message: True)
def text_router(message):
    user_id = message.from_user.id
    ensure_user(user_id)
    chat_id = message.chat.id
    text = message.text.strip()
    state = user_states.get(user_id, {})
    step = state.get("step")
    menu_handlers = {
        MENU_NEW_TRIP: lambda: start_new_trip(chat_id, user_id),
        MENU_TRIPS: lambda: show_trips(chat_id, user_id),
        MENU_BALANCE: lambda: show_balance(chat_id, user_id),
        MENU_HISTORY: lambda: show_history(chat_id, user_id),
        MENU_SET_RATE: lambda: ask_set_rate(chat_id, user_id),
    }

    if text in menu_handlers:
        menu_handlers[text]()
        return

    if step == "await_home_country":
        state["home_country"] = text
        home_currency = country_to_currency(text)
        if not home_currency:
            send_text(
                chat_id,
                "Не смог определить валюту этой страны. Попробуйте другую страну или более стандартное название.",
            )
            return
        state["home_currency"] = home_currency
        state["step"] = "await_destination_country"
        user_states[user_id] = state
        send_text(chat_id, "Теперь введите страну назначения.")
        return

    if step == "await_destination_country":
        state["destination_country"] = text
        destination_currency = country_to_currency(text)
        if not destination_currency:
            send_text(
                chat_id,
                "Для страны назначения не нашёл валюту. Попробуйте другое название.",
            )
            return
        state["destination_currency"] = destination_currency
        result, err = convert_with_api(1, state["home_currency"], destination_currency)
        if err:
            send_text(chat_id, err)
            return
        state["api_rate"] = result
        state["step"] = "await_rate_confirmation"
        user_states[user_id] = state
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            types.InlineKeyboardButton("✅ Да", callback_data="rate_ok"),
            types.InlineKeyboardButton("❌ Нет", callback_data="rate_manual"),
        )
        send_text(
            chat_id,
            (
                f"Нашёл пару: {state['home_currency']} -> {state['destination_currency']}\n"
                f"Текущий курс API: 1 {state['home_currency']} = {format_amount(result)} {state['destination_currency']}\n"
                "Использовать этот курс?"
            ),
            reply_markup=kb,
        )
        return

    if step == "await_manual_rate":
        manual_rate = parse_amount(text)
        if not manual_rate:
            send_text(chat_id, "Нужен положительный числовой курс. Пример: 12.8")
            return
        state["selected_rate"] = manual_rate
        state["step"] = "await_initial_amount"
        user_states[user_id] = state
        send_text(
            chat_id,
            f"Введите стартовую сумму в {state['home_currency']} (домашняя валюта).",
        )
        return

    if step == "await_initial_amount":
        initial_home = parse_amount(text)
        if not initial_home:
            send_text(chat_id, "Введите положительную сумму числом.")
            return
        state = user_states.get(user_id, {})
        result, err = convert_with_api(
            initial_home, state["home_currency"], state["destination_currency"]
        )
        if err:
            send_text(
                chat_id,
                "Не удалось конвертировать стартовую сумму через API. Проверьте позже и попробуйте снова.",
            )
            return
        create_trip(
            user_id=user_id,
            state=state,
            rate=state["selected_rate"],
            initial_home=initial_home,
            initial_destination=result,
        )
        user_states.pop(user_id, None)
        send_text(
            chat_id,
            (
                "Путешествие создано.\n"
                f"Старт: {format_amount(initial_home)} {state['home_currency']} = "
                f"{format_amount(result)} {state['destination_currency']} (по API)\n"
                f"Рабочий курс кошелька: 1 {state['home_currency']} = "
                f"{format_amount(state['selected_rate'])} {state['destination_currency']}"
            ),
        )
        show_main_menu(chat_id, user_id)
        return

    if step == "await_new_rate":
        new_rate = parse_amount(text)
        if not new_rate:
            send_text(chat_id, "Курс должен быть положительным числом.")
            return
        trip = get_active_trip(user_id)
        if not trip:
            send_text(chat_id, "Активное путешествие не найдено.")
            user_states.pop(user_id, None)
            return
        conn = get_conn()
        cur = conn.cursor()
        recalculated_home_balance = home_equivalent_by_rate(trip[8], new_rate)
        cur.execute(
            "UPDATE trips SET rate = ?, home_balance = ? WHERE id = ?",
            (new_rate, recalculated_home_balance, trip[0]),
        )
        conn.commit()
        conn.close()
        user_states.pop(user_id, None)
        send_text(
            chat_id,
            f"Курс обновлён: 1 {trip[4]} = {format_amount(new_rate)} {trip[5]}",
        )
        return

    if step == "await_expense_description":
        pending = pending_expenses.get(user_id)
        if not pending:
            user_states.pop(user_id, None)
            send_text(chat_id, "Сумма для сохранения не найдена. Отправьте сумму заново.")
            return
        description = text[:20]
        if not description:
            send_text(chat_id, "Описание не должно быть пустым.")
            return
        trip = get_active_trip(user_id)
        if not trip:
            user_states.pop(user_id, None)
            pending_expenses.pop(user_id, None)
            send_text(chat_id, "Активное путешествие не найдено.")
            return
        new_dest_balance = trip[8] - pending["amount_destination"]
        new_home_balance = home_equivalent_by_rate(new_dest_balance, trip[6])
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO expenses(
                user_id, trip_id, amount_destination, amount_home, description, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                trip[0],
                pending["amount_destination"],
                pending["amount_home"],
                description,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        cur.execute(
            """
            UPDATE trips
            SET home_balance = ?, destination_balance = ?
            WHERE id = ? AND user_id = ?
            """,
            (new_home_balance, new_dest_balance, trip[0], user_id),
        )
        conn.commit()
        conn.close()
        user_states.pop(user_id, None)
        pending_expenses.pop(user_id, None)
        send_text(
            chat_id,
            (
                "Расход учтён.\n"
                f"Остаток: {format_amount(new_dest_balance)} {trip[5]} = "
                f"{format_amount(new_home_balance)} {trip[4]}"
            ),
        )
        return

    amount_destination = parse_amount(text)
    if amount_destination:
        trip = get_active_trip(user_id)
        if not trip:
            send_text(
                chat_id,
                "Сначала создайте путешествие, чтобы учитывать расходы.",
                reply_markup=main_menu_markup(),
            )
            return
        amount_home = amount_destination / trip[6]
        pending_expenses[user_id] = {
            "trip_id": trip[0],
            "amount_destination": amount_destination,
            "amount_home": amount_home,
        }
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            types.InlineKeyboardButton("✅ Да", callback_data="expense_yes"),
            types.InlineKeyboardButton("❌ Нет", callback_data="expense_no"),
        )
        send_text(
            chat_id,
            (
                f"{format_amount(amount_destination)} {trip[5]} = "
                f"{format_amount(amount_home)} {trip[4]}\n"
                "Учесть как расход?"
            ),
            reply_markup=kb,
        )
        return

    send_text(
        chat_id,
        (
            "Не понял сообщение. Для навигации используйте кнопки меню,\n"
            "или отправьте сумму расхода числом (например: 100)."
        ),
        reply_markup=main_menu_markup(),
    )


if __name__ == "__main__":
    init_db()
    bot.infinity_polling(skip_pending=True)
