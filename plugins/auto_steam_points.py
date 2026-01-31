import json
import os
import re
import logging
import requests
import time
from datetime import datetime
from telebot import types
from FunPayAPI.types import Order
import datetime

API_BASE_URL = "https://api.buysteampoints.com/api"

class SteamPointsAPIClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        
    def get_balance(self) -> float:
        url = f"{API_BASE_URL}/balance"
        payload = {"api_key": self.api_key}
        
        try:
            resp = requests.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            
            if data.get("success"):
                return data["balance"]
            else:
                error = data.get("error", "Unknown error")
                raise Exception(f"API error: {error}")
                
        except Exception as e:
            logging.error(f"[autopoints] ❌ Balance check error: {e}")
            raise

    def purchase_points(self, steam_link: str, points: int) -> dict:
        url = f"{API_BASE_URL}/buy"
        payload = {
            "api_key": self.api_key,
            "puan": points,
            "steam_link": steam_link
        }
        
        try:
            resp = requests.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            
            if data.get("success"):
                return data
            else:
                error = data.get("error", "Unknown error")
                raise Exception(f"API error: {error}")
                
        except Exception as e:
            logging.error(f"[autopoints] ❌ Purchase error: {e}")
            raise
            
    def get_points_price(self) -> float:
        url = f"{API_BASE_URL}/price"
        
        try:
            resp = requests.get(url)
            resp.raise_for_status()
            data = resp.json()
            
            if data.get("success"):
                return data["price"]
            else:
                error = data.get("error", "Unknown error")
                raise Exception(f"API error: {error}")
                
        except Exception as e:
            logging.error(f"[autopoints] ❌ Price check error: {e}")
            raise

def is_valid_link(link: str) -> (bool, str):
    pattern = r'^https?://steamcommunity\.com/(?:id|profiles)/[A-Za-z0-9_-]+/?$'
    if re.match(pattern, link):
        return True, ""
    return False, "❌ Неверная ссылка на профиль Steam. Формат должен быть:\n" \
                 "https://steamcommunity.com/id/ВашID или\n" \
                 "https://steamcommunity.com/profiles/7656119XXXXXXXXXX"

NAME = "Steam Points"
VERSION = "3.0"
DESCRIPTION = "Автовыдача очков Steam на FunPay."
CREDITS = "@veemp"
UUID = "d3b07384-9e7b-4f6a-b834-123456abcdef"
SETTINGS_PAGE = False

bot = None
cardinal = None
api_client = None
config = {}
waiting_for_link = {}
order_history = []
points_price = 0.01

CONFIG_PATH = "storage/points/cfg.json"
DEFAULT_CONFIG = {
    "api_key": "",
    "auto_refunds": False,
    "auto_restock": False,
    "managers": [],
    "order_history": [],
    "lot_manager": {
        "balance_threshold": 0,
        "auto_deactivate": False,
        "saved_lots": [],
        "lots_active": True
    },
    "templates": {
        "start_message": "✅ Заказ получен!\n💎 Количество очков: {total_points} ({points_per_unit} за единицу × {units} шт.)\n\nУкажите ссылку на Ваш профиль Steam:",
        "invalid_link": "❌ Неверная ссылка на профиль Steam. Формат должен быть:\nhttps://steamcommunity.com/id/ВашID или\nhttps://steamcommunity.com/profiles/7656119XXXXXXXXXX",
        "link_confirmation": "✅ Ссылка принята: {link}\nПодтвердите: + / -",
        "purchase_success": "🎉 Успешно! {qty} очков добавлено!\n👉 Отслеживать: https://steamcommunity.com/profiles/{steam64}/awards\n📥 Награды могут приходить до 20 минут.",
        "purchase_error": "❌ Ошибка: {error}",
        "insufficient_balance": "❌ Недостаточно средств! Средства возвращены."
    }
}

logger = logging.getLogger("FPC.autopoints")

def ensure_config():
    global config, order_history
    
    if not os.path.exists("storage/points"):
        os.makedirs("storage/points")

    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, indent=4, ensure_ascii=False)

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)
    
    for key in DEFAULT_CONFIG:
        if key not in config:
            config[key] = DEFAULT_CONFIG[key].copy() if isinstance(DEFAULT_CONFIG[key], dict) else DEFAULT_CONFIG[key]
    
    for key in DEFAULT_CONFIG["templates"]:
        if key not in config["templates"]:
            config["templates"][key] = DEFAULT_CONFIG["templates"][key]
    
    order_history = config.get("order_history", [])
    
    return config

def save_config():
    global config, order_history
    config["order_history"] = order_history
    
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4, ensure_ascii=False)

def parse_points_from_description(description: str) -> int:
    if not description:
        return 0
        
    match = re.search(r'points:\s*(\d+)', description, re.IGNORECASE)
    if match:
        try:
            return int(match.group(1))
        except (TypeError, ValueError):
            pass
    return 0

def format_template(template_name: str, **kwargs) -> str:
    template = config["templates"].get(template_name, "")
    if not template:
        return DEFAULT_CONFIG["templates"].get(template_name, "")
    
    try:
        return template.format(**kwargs)
    except KeyError:
        return template

def get_balance_status():
    global config, api_client
    
    if not config.get("api_key"):
        return "🔴 Не задан"
    
    try:
        balance = api_client.get_balance()
        return f"🟢 Активен | {balance:.2f}₽"
    except:
        return "🔴 Невалидный"

def main_menu():
    global api_client, config, points_price
    
    kb = types.InlineKeyboardMarkup(row_width=2)
    
    api_status = get_balance_status()
    balance = "недоступен"
    if "Активен" in api_status:
        balance = api_status.split("|")[1].strip()
    
    points_per_1000 = points_price * 1000

    lots_status = ""
    saved_lots = config["lot_manager"].get("saved_lots", [])
    if saved_lots:
        lots_active = config["lot_manager"].get("lots_active", True)
        lots_status = f"📦 Лоты: {'🟢 Активны' if lots_active else '🔴 Не активны'}\n"
    
    buttons = [
        types.InlineKeyboardButton("🔑 Управление API", callback_data="ap_api_settings"),
        types.InlineKeyboardButton("📊 Статистика", callback_data="ap_stats_reports"),
        types.InlineKeyboardButton("📜 История заказов", callback_data="ap_order_history"),
        types.InlineKeyboardButton("✏️ Настройки сообщений", callback_data="ap_message_settings"),
        types.InlineKeyboardButton("⚙️ Управление", callback_data="ap_plugin_management"),
        types.InlineKeyboardButton("❓ Помощь", callback_data="ap_help")
    ]
    
    kb.add(buttons[0])
    kb.add(buttons[1], buttons[2])
    kb.add(buttons[3], buttons[4])
    kb.add(buttons[5])
    
    text = (
        f"🚀 <b>Панель управления Steam Points</b>\n"
        f"────────────────────────\n"
        f"💰 <b>Баланс:</b> {balance}\n"
        f"{lots_status}"
        f"💱 <b>Курс:</b> 1000 очков = {points_per_1000:.2f}₽\n\n"
        "Выберите действие:"
    )
    
    return kb, text

def api_settings_menu():
    kb = types.InlineKeyboardMarkup()
    
    if config.get("api_key"):
        buttons = [
            types.InlineKeyboardButton("✏️ Редактировать токен", callback_data="ap_edit_token"),
            types.InlineKeyboardButton("🗑️ Удалить токен", callback_data="ap_delete_token"),
            types.InlineKeyboardButton("🔍 Проверить токен", callback_data="ap_check_token"),
        ]
    else:
        buttons = [
            types.InlineKeyboardButton("➕ Добавить токен", callback_data="ap_add_token"),
        ]
    
    buttons.append(types.InlineKeyboardButton("↩️ Назад", callback_data="ap_main_menu"))
    
    for btn in buttons:
        kb.add(btn)
    
    return kb

def message_settings_menu():
    kb = types.InlineKeyboardMarkup(row_width=1)
    
    templates = [
        ("📝 Начальное сообщение", "start_message"),
        ("🔗 Сообщение о неверной ссылке", "invalid_link"),
        ("✅ Подтверждение ссылки", "link_confirmation"),
        ("🎉 Успешная покупка", "purchase_success"),
        ("❌ Ошибка покупки", "purchase_error"),
        ("💸 Недостаток средств", "insufficient_balance")
    ]
    
    buttons = []
    for text, callback in templates:
        buttons.append(types.InlineKeyboardButton(text, callback_data=f"ap_edit_template:{callback}"))
    
    buttons.append(types.InlineKeyboardButton("↩️ Назад", callback_data="ap_main_menu"))
    
    for btn in buttons:
        kb.add(btn)
            
    return kb

def plugin_management_menu():
    kb = types.InlineKeyboardMarkup()
    
    auto_refunds_status = "✅" if config.get("auto_refunds") else "❌"
    auto_restock_status = "✅" if config.get("auto_restock") else "❌"
    
    buttons = [
        types.InlineKeyboardButton(f"🔄 Авто-возвраты: {auto_refunds_status}", callback_data="ap_toggle_refunds"),
        types.InlineKeyboardButton(f"📦 Авто-количество: {auto_restock_status}", callback_data="ap_toggle_restock"),
        types.InlineKeyboardButton("📦 Лот менеджер", callback_data="ap_lot_manager"),
        types.InlineKeyboardButton("🔄 Стандартные настройки", callback_data="ap_reset_settings"),
        types.InlineKeyboardButton("🧹 Очистить историю заказов", callback_data="ap_clear_history"),
        types.InlineKeyboardButton("↩️ Назад", callback_data="ap_main_menu")
    ]
    
    for btn in buttons:
        kb.add(btn)
    
    return kb

def lot_manager_menu():
    kb = types.InlineKeyboardMarkup()
    
    auto_deactivate = config["lot_manager"].get("auto_deactivate", False)
    balance_threshold = config["lot_manager"].get("balance_threshold", 0)
    lots_active = config["lot_manager"].get("lots_active", True)
    
    toggle_button = "🔴 Деактивировать лоты" if lots_active else "🟢 Активировать лоты"
    
    buttons = [
        types.InlineKeyboardButton(f"🔢 Порог баланса: {balance_threshold}₽", callback_data="ap_set_balance_threshold"),
        types.InlineKeyboardButton(f"🔄 Авто-деактивация: {'✅' if auto_deactivate else '❌'}", callback_data="ap_toggle_auto_deactivate"),
        types.InlineKeyboardButton("💾 Сохранить лоты", callback_data="ap_save_lots"),
        types.InlineKeyboardButton(toggle_button, callback_data="ap_toggle_lots"),
        types.InlineKeyboardButton("↩️ Назад", callback_data="ap_plugin_management")
    ]
    
    for btn in buttons:
        kb.add(btn)
    
    return kb

def order_history_menu(page=1, items_per_page=5):
    kb = types.InlineKeyboardMarkup()
    global order_history
    
    sorted_orders = sorted(
        order_history, 
        key=lambda x: datetime.datetime.strptime(x['timestamp'], '%Y-%m-%d %H:%M:%S'), 
        reverse=True
    )
    
    total_orders = len(sorted_orders)
    if total_orders == 0:
        return None, 0, 0
    
    total_pages = (total_orders + items_per_page - 1) // items_per_page
    start_index = (page - 1) * items_per_page
    end_index = min(start_index + items_per_page, total_orders)
    
    for order in sorted_orders[start_index:end_index]:
        btn_text = f"Закaз #{order['order_id']}  {order.get('revenue', 0):.2f}₽"
        kb.add(types.InlineKeyboardButton(
            btn_text, 
            callback_data=f"ap_order_details:{order['order_id']}"
        ))
    
    nav_buttons = []
    if page > 1:
        nav_buttons.append(types.InlineKeyboardButton("◀️ Назад", callback_data=f"ap_history_page:{page-1}"))
    
    if page < total_pages:
        nav_buttons.append(types.InlineKeyboardButton("▶️ Вперед", callback_data=f"ap_history_page:{page+1}"))
    
    if nav_buttons:
        kb.add(*nav_buttons)
    
    kb.add(types.InlineKeyboardButton("↩️ Назад", callback_data="ap_main_menu"))
    
    return kb, page, total_pages

def order_details_menu(order_id):
    global order_history

    order = next((o for o in order_history if o["order_id"] == order_id), None)
    if not order:
        return None
    
    kb = types.InlineKeyboardMarkup()
    
    order_url = f"https://funpay.com/orders/{order_id}/"
    kb.add(types.InlineKeyboardButton("🔗 Открыть заказ на FunPay", url=order_url))
    
    if 'link' in order and order['link']:
        kb.add(types.InlineKeyboardButton("👤Открыть профиль Steam", url=order['link']))
    
    kb.add(types.InlineKeyboardButton("↩️ Назад", callback_data="ap_order_history"))
    
    return kb

def handle_command(message: types.Message):
    if message.text == "/steam_points":
        menu, text = main_menu()
        bot.send_message(
            message.chat.id,
            text,
            parse_mode="HTML",
            reply_markup=menu
        )

def mask_api_key(api_key: str) -> str:
    if not api_key or len(api_key) < 8:
        return "****"
    return f"{api_key[:4]}...{api_key[-4:]}"

def calculate_statistics():
    global order_history, points_price
    
    total_orders = len(order_history)
    total_points = sum(order.get("qty", 0) for order in order_history)
    total_revenue = sum(order.get("revenue", 0) for order in order_history)
    total_cost = total_points * points_price
    profit = total_revenue - total_cost
    points_per_1000 = points_price * 1000
    
    return {
        "total_orders": total_orders,
        "total_points": total_points,
        "total_revenue": total_revenue,
        "total_cost": total_cost,
        "profit": profit,
        "points_per_1000": points_per_1000
    }

def handle_callback(call: types.CallbackQuery):
    global api_client, config, order_history, points_price, current_page
    
    data = call.data
    user_id = call.from_user.id
    
    if data == "ap_api_settings":
        bot.edit_message_text(
            "🔑 <b>Управление API ключом</b>\n"
            "────────────────────────",
            call.message.chat.id,
            call.message.message_id,
            parse_mode="HTML",
            reply_markup=api_settings_menu()
        )
        
    elif data == "ap_add_token":
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("↩️ Назад", callback_data="ap_api_settings"))
        
        msg = bot.send_message(
            user_id, 
            "🔑 <b>Добавление API ключа</b>\n\n"
            "Отправьте ваш API ключ или /cancel для отмены:",
            parse_mode="HTML",
            reply_markup=kb
        )
        bot.register_next_step_handler(msg, receive_api_key)
        
    elif data == "ap_edit_token":
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("↩️ Назад", callback_data="ap_api_settings"))
        
        msg = bot.send_message(
            user_id, 
            "🔑 <b>Редактирование API ключа</b>\n\n"
            "Отправьте новый API ключ или /cancel для отмены:",
            parse_mode="HTML",
            reply_markup=kb
        )
        bot.register_next_step_handler(msg, receive_api_key)
        
    elif data == "ap_delete_token":
        config["api_key"] = ""
        save_config()
        bot.answer_callback_query(call.id, "🗑️ API ключ удалён!")
        bot.edit_message_reply_markup(
            call.message.chat.id, 
            call.message.message_id, 
            reply_markup=api_settings_menu()
        )
        
    elif data == "ap_check_token":
        if not config.get("api_key"):
            bot.answer_callback_query(call.id, "❌ API ключ не задан!")
            return
            
        try:
            balance = api_client.get_balance()
            bot.answer_callback_query(call.id, f"💰 Баланс: {balance:.2f}₽")
        except Exception as e:
            logger.error(f"[autopoints] ❌ Ошибка проверки токена: {e}")
            bot.answer_callback_query(call.id, f"❌ Ошибка: {str(e)}")
            
    elif data == "ap_toggle_refunds":
        config["auto_refunds"] = not config.get("auto_refunds", False)
        save_config()
        bot.edit_message_reply_markup(
            call.message.chat.id, 
            call.message.message_id, 
            reply_markup=plugin_management_menu()
        )
        status = "включены" if config["auto_refunds"] else "выключены"
        bot.answer_callback_query(call.id, f"🔄 Авто-возвраты {status}!")
        
    elif data == "ap_toggle_restock":
        config["auto_restock"] = not config.get("auto_restock", False)
        save_config()
        bot.edit_message_reply_markup(
            call.message.chat.id, 
            call.message.message_id, 
            reply_markup=plugin_management_menu()
        )
        status = "включено" if config["auto_restock"] else "выключено"
        bot.answer_callback_query(call.id, f"🔄 Авто-пополнение {status}!")
        
    elif data == "ap_stats_reports":
        stats = calculate_statistics()
        response = (
            f"📊 <b>Статистика продаж</b>\n"
            f"────────────────────────\n"
            f"• Всего заказов: <b>{stats['total_orders']}</b>\n"
            f"• Проданные очки: <b>{stats['total_points']}</b>\n"
            f"• Выручка: <b>{stats['total_revenue']:.2f}₽</b>\n"
            f"• Расходы: <b>{stats['total_cost']:.2f}₽</b>\n"
            f"• Прибыль: <b>{stats['profit']:.2f}₽</b>\n"
            f"• Текущий курс: <b>1000 очков = {stats['points_per_1000']:.2f}₽</b>"
        )
        bot.edit_message_text(
            response,
            call.message.chat.id,
            call.message.message_id,
            parse_mode="HTML",
            reply_markup=main_menu()[0]
        )
        
    elif data == "ap_help":
        help_text = (
            "🆘 <b>Помощь по плагину</b>\n"
            "────────────────────────\n"
            "<b>Главное меню:</b>\n"
            "• 🔑 <b>Управление API</b> - подключение и управление API ключом\n"
            "• 📊 <b>Статистика</b> - отчет по продажам и прибыли\n"
            "• 📜 <b>История заказов</b> - список выполненных операций\n"
            "• ✏️ <b>Настройки сообщений</b> - редактирование шаблонов сообщений\n"
            "• ⚙️ <b>Управление плагином</b> - настройки автоматизации\n"
            "• ❓ <b>Помощь</b> - текущее меню\n\n"
            
            "<b>Управление плагином:</b>\n"
            "• 🔄 Авто-возвраты - автоматически отменять невалидные заказы\n"
            "• 📦 Авто-количество - автоматически обновлять количество стим очков через баланс\n"
            "• 📦 Лот менеджер - управление лотами Steam Points\n"
            "• 🔄 Стандартные настройки - сброс шаблонов к стандартным\n"
            "• 🧹 Очистить историю заказов - удалить всю историю операций\n\n"
            
            "<b>Лот менеджер:</b>\n"
            "• 🔢 Порог баланса - минимальный баланс для деактивации лотов\n"
            "• 🔄 Авто-деактивация - автоматически деактивировать лоты при низком балансе\n"
            "• 💾 Сохранить лоты - сохранить текущие лоты категории Steam Points\n"
            "• 🔴 Деактивировать/🟢 Активировать - ручное управление активностью лотов\n\n"
            
            "<b>Настройки сообщений:</b>\n"
            "• 📝 Начальное сообщение - запрос ссылки на профиль Steam\n"
            "• 🔗 Сообщение о неверной ссылке - сообщение о неправильном формате ссылки\n"
            "• ✅ Подтверждение ссылки - запрос подтверждения введенной ссылки\n"
            "• 🎉 Успешная покупка - сообщение об успешной покупке\n"
            "• ❌ Ошибка покупки - сообщение об ошибке при покупке\n"
            "• 💸 Недостаток средств - сообщение о недостатке средств на балансе\n\n"
            
            "💡 <b>Как работает привязка очков:</b>\n"
            "1. Добавьте в конец описания лота: <code>points:XXXX</code>\n"
            "2. XXXX = количество очков за 1 единицу товара\n"
            "3. Пример: <code>points:1000</code> + заказ 2 шт. = 2000 очков\n"
            "4. Если нужна одиночная выдача 1к1, не указывайте ничего в описании\n\n"
            "⚠️ <b>Важно:</b> Лоты должны быть в категории Steam Points (ID: 714)"
        )
        bot.edit_message_text(
            help_text,
            call.message.chat.id,
            call.message.message_id,
            parse_mode="HTML",
            reply_markup=main_menu()[0]
        )
    
    elif data == "ap_order_history":
        kb, page, total_pages = order_history_menu()
        if kb is None:
            bot.edit_message_text(
                "📜 История заказов пуста.",
                call.message.chat.id,
                call.message.message_id,
                parse_mode="HTML",
                reply_markup=main_menu()[0]
            )
            return
            
        header = f"📜 История заказов (стр. {page}/{total_pages})\n────────────────────────"
        bot.edit_message_text(
            header,
            call.message.chat.id,
            call.message.message_id,
            parse_mode="HTML",
            reply_markup=kb
        )
        
    elif data.startswith("ap_history_page:"):
        page = int(data.split(":")[1])
        kb, page, total_pages = order_history_menu(page)
        
        header = f"📜 История заказов (стр. {page}/{total_pages})\n────────────────────────"
        bot.edit_message_text(
            header,
            call.message.chat.id,
            call.message.message_id,
            parse_mode="HTML",
            reply_markup=kb
        )
        
    elif data.startswith("ap_order_details:"):
        order_id = data.split(":")[1]
        order = next((o for o in order_history if o["order_id"] == order_id), None)
        
        if not order:
            bot.answer_callback_query(call.id, "❌ Заказ не найден!")
            return
            
        details = (
        f"📋 Детали заказа #{order_id}\n"
        f"────────────────────────\n"
        f"👤 Покупатель: https://funpay.com/users/{order.get('buyer_id', 'N/A')}/\n"
        f"💎 Очков: {order.get('qty', 0)}\n"
        f"💰 Сумма: {order.get('revenue', 0):.2f} P\n"
        f"📅 Дата: {order.get('timestamp', 'N/A')}\n\n"
        f"🔄 Кол-во единиц: {order.get('units', 1)}\n"
        f"💎 Очков за единицу: {order.get('points_per_unit', 1)}"
        )
        
        kb = order_details_menu(order_id)
        if not kb:
            bot.answer_callback_query(call.id, "❌ Ошибка создания меню!")
            return
            
        bot.edit_message_text(
        details,
        call.message.chat.id,
        call.message.message_id,
        reply_markup=kb
        )
        
    elif data == "ap_message_settings":
        bot.edit_message_text(
            "✏️ <b>Настройки сообщений</b>\n"
            "────────────────────────\n"
            "Выберите шаблон для редактирования:",
            call.message.chat.id,
            call.message.message_id,
            parse_mode="HTML",
            reply_markup=message_settings_menu()
        )
        
    elif data == "ap_plugin_management":
        bot.edit_message_text(
            "⚙️ <b>Управление плагином</b>\n"
            "────────────────────────",
            call.message.chat.id,
            call.message.message_id,
            parse_mode="HTML",
            reply_markup=plugin_management_menu()
        )
        
    elif data == "ap_lot_manager":
        bot.edit_message_text(
            "📦 <b>Лот менеджер</b>\n"
            "────────────────────────",
            call.message.chat.id,
            call.message.message_id,
            parse_mode="HTML",
            reply_markup=lot_manager_menu()
        )
        
    elif data == "ap_set_balance_threshold":
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("↩️ Назад", callback_data="ap_lot_manager"))
        
        msg = bot.send_message(
            user_id,
            "🔢 <b>Установка порога баланса</b>\n\n"
            "Отправьте минимальный баланс в рублях, при котором лоты будут автоматически деактивироваться:",
            parse_mode="HTML",
            reply_markup=kb
        )
        bot.register_next_step_handler(msg, receive_balance_threshold)
        
    elif data == "ap_toggle_auto_deactivate":
        config["lot_manager"]["auto_deactivate"] = not config["lot_manager"].get("auto_deactivate", False)
        save_config()
        bot.edit_message_reply_markup(
            call.message.chat.id, 
            call.message.message_id, 
            reply_markup=lot_manager_menu()
        )
        status = "включена" if config["lot_manager"]["auto_deactivate"] else "выключена"
        bot.answer_callback_query(call.id, f"🔄 Авто-деактивация {status}!")
        
    elif data == "ap_save_lots":
        try:
            account = cardinal.account
            user_profile = account.get_user(account.id)
            all_lots = user_profile.get_lots()
            
            steam_lots = [lot.id for lot in all_lots if lot.subcategory.id == 714]
            
            config["lot_manager"]["saved_lots"] = steam_lots
            save_config()
            bot.answer_callback_query(call.id, f"💾 Сохранено {len(steam_lots)} лотов категории 714!")
            
        except Exception as e:
            logger.error(f"[autopoints] ❌ Ошибка сохранения лотов: {e}")
            bot.answer_callback_query(call.id, f"❌ Ошибка: {str(e)}")
            
    elif data == "ap_toggle_lots":
        try:
            saved_lots = config["lot_manager"].get("saved_lots", [])
            if not saved_lots:
                bot.answer_callback_query(call.id, "❌ Сначала сохраните лоты!")
                return
                
            current_status = config["lot_manager"].get("lots_active", True)
            new_status = not current_status
            
            config["lot_manager"]["lots_active"] = new_status
            save_config()
            
            account = cardinal.account
            
            if new_status:
                activate_steam_lots(account, saved_lots)
            else:
                deactivate_steam_lots(account, saved_lots)
            
            status = "активированы" if new_status else "деактивированы"
            bot.answer_callback_query(call.id, f"📦 Все лоты {status}!")
            
            bot.edit_message_reply_markup(
                call.message.chat.id, 
                call.message.message_id, 
                reply_markup=lot_manager_menu()
            )
            
        except Exception as e:
            logger.error(f"[autopoints] ❌ Ошибка управления лотами: {e}")
            bot.answer_callback_query(call.id, f"❌ Ошибка: {str(e)}")
        
    elif data == "ap_main_menu":
        menu, text = main_menu()
        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            parse_mode="HTML",
            reply_markup=menu
        )
        
    elif data.startswith("ap_edit_template:"):
        template_name = data.split(":")[1]
        current_template = config["templates"].get(template_name, "")
        
        hints = {
            "start_message": "Доступные переменные:\n- {total_points} - общее кол-во очков\n- {points_per_unit} - очков за единицу\n- {units} - кол-во единиц",
            "invalid_link": "Доступные переменные: нет",
            "link_confirmation": "Доступные переменные:\n- {link} - ссылка Steam",
            "purchase_success": "Доступные переменные:\n- {qty} - кол-во очков\n- {steam64} - SteamID64",
            "purchase_error": "Доступные переменные:\n- {error} - текст ошибки",
            "insufficient_balance": "Доступные переменные: нет"
        }
        
        hint = hints.get(template_name, "Доступные переменные не определены")
        
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("↩️ Назад", callback_data="ap_message_settings"))
        
        msg = bot.send_message(
            user_id,
            f"✏️ <b>Редактирование шаблона:</b> <code>{template_name}</code>\n\n"
            f"Текущий шаблон:\n<code>{current_template}</code>\n\n"
            f"{hint}\n\n"
            "Отправьте новый шаблон или /cancel для отмены:",
            parse_mode="HTML",
            reply_markup=kb
        )
        bot.register_next_step_handler(msg, receive_template, template_name)
        
    elif data == "ap_reset_settings":
        config["templates"] = DEFAULT_CONFIG["templates"].copy()
        save_config()
        bot.answer_callback_query(call.id, "🔄 Настройки сброшены к стандартным!")
        bot.edit_message_reply_markup(
            call.message.chat.id, 
            call.message.message_id, 
            reply_markup=plugin_management_menu()
        )
            
    elif data == "ap_clear_history":
        kb = types.InlineKeyboardMarkup()
        kb.add(
            types.InlineKeyboardButton("✅ Да, очистить", callback_data="ap_confirm_clear_history"),
            types.InlineKeyboardButton("❌ Отмена", callback_data="ap_plugin_management")
        )
        bot.edit_message_text(
            "⚠️ <b>Вы уверены, что хотите очистить историю заказов?</b>\n"
            "Это действие нельзя отменить!",
            call.message.chat.id,
            call.message.message_id,
            parse_mode="HTML",
            reply_markup=kb
        )
        
    elif data == "ap_confirm_clear_history":
        order_history.clear()
        config["order_history"] = []
        save_config()
        bot.answer_callback_query(call.id, "🧹 История заказов очищена!")
        bot.edit_message_text(
            "⚙️ <b>Управление плагином</b>\n"
            "────────────────────────",
            call.message.chat.id,
            call.message.message_id,
            parse_mode="HTML",
            reply_markup=plugin_management_menu()
        )
        
    else:
        bot.answer_callback_query(call.id, "⚠️ Неизвестная команда")

def receive_api_key(message: types.Message):
    global api_client
    
    if message.text == "/cancel":
        bot.send_message(message.chat.id, "❌ Изменение отменено.")
        return
        
    new_api_key = message.text.strip()
    
    if len(new_api_key) < 10:
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("↩️ Назад", callback_data="ap_api_settings"))
        
        msg = bot.send_message(
            message.chat.id, 
            "❌ Ключ слишком короткий (мин. 10 символов)\n"
            "Повторите ввод или /cancel:",
            reply_markup=kb
        )
        bot.register_next_step_handler(msg, receive_api_key)
        return
        
    temp_client = SteamPointsAPIClient(api_key=new_api_key)
    
    try:
        balance = temp_client.get_balance()
        config["api_key"] = new_api_key
        save_config()
        
        api_client = SteamPointsAPIClient(api_key=config["api_key"])
        
        bot.send_message(
            message.chat.id, 
            f"✅ API ключ сохранен!\n💰 Баланс: {balance:.2f}₽"
        )
        
    except Exception as e:
        logger.error(f"[autopoints] ❌ Ошибка проверки API: {e}")
        
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("↩️ Назад", callback_data="ap_api_settings"))
        
        msg = bot.send_message(
            message.chat.id, 
            f"❌ Ошибка: {str(e)}\nПовторите ввод или /cancel:",
            reply_markup=kb
        )
        bot.register_next_step_handler(msg, receive_api_key)

def receive_balance_threshold(message: types.Message):
    try:
        threshold = float(message.text.strip())
        config["lot_manager"]["balance_threshold"] = threshold
        save_config()
        bot.send_message(
            message.chat.id,
            f"✅ Порог баланса установлен: {threshold}₽"
        )
    except ValueError:
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("↩️ Назад", callback_data="ap_lot_manager"))
        
        msg = bot.send_message(
            message.chat.id,
            "❌ Неверный формат числа. Отправьте число (например: 100.5):",
            reply_markup=kb
        )
        bot.register_next_step_handler(msg, receive_balance_threshold)

def receive_template(message: types.Message, template_name: str):
    if message.text == "/cancel":
        bot.send_message(message.chat.id, "❌ Изменение отменено.")
        return
        
    new_template = message.text.strip()
    
    if not new_template:
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("↩️ Назад", callback_data="ap_message_settings"))
        
        msg = bot.send_message(
            message.chat.id,
            "❌ Шаблон не может быть пустым!\n"
            "Повторите ввод или /cancel:",
            reply_markup=kb
        )
        bot.register_next_step_handler(msg, receive_template, template_name)
        return
        
    config["templates"][template_name] = new_template
    save_config()
    
    bot.send_message(
        message.chat.id,
        f"✅ Шаблон <code>{template_name}</code> обновлен!\n"
        f"Новый шаблон:\n<code>{new_template}</code>",
        parse_mode="HTML"
    )
    bot.send_message(
        message.chat.id,
        "✏️ Выберите следующий шаблон для редактирования:",
        parse_mode="HTML",
        reply_markup=message_settings_menu()
    )

def generate_stats_text(stats: dict) -> str:
    return (
        f"📊 <b>Статистика продаж</b>\n"
        f"────────────────────────\n"
        f"• Всего заказов: <b>{stats['total_orders']}</b>\n"
        f"• Проданные очки: <b>{stats['total_points']}</b>\n"
        f"• Выручка: <b>{stats['total_revenue']:.2f}₽</b>\n"
        f"• Расходы: <b>{stats['total_cost']:.2f}₽</b>\n"
        f"• Прибыль: <b>{stats['profit']:.2f}₽</b>\n"
        f"• Текущий курс: <b>1000 очков = {stats['points_per_1000']:.2f}₽</b>"
    )

def generate_order_history_header(page: int, total_pages: int) -> str:
    return f"📜 История заказов (стр. {page}/{total_pages})\n────────────────────────"

def generate_order_history_item(order: dict) -> str:
    return f"Закaз #{order['order_id']}  (+{order['revenue']:.2f} P)"

def generate_order_details(order: dict) -> str:
    return (
        f"📋 Детали заказа #{order['order_id']}\n"
        f"────────────────────────\n"
        f"👤 Покупатель: https://funpay.com/users/{order['buyer_id']}/\n"
        f"💎 Очков: {order['qty']}\n"
        f"💰 Сумма: {order['revenue']:.2f} P\n"
        f"📅 Дата: {order['timestamp']}\n\n"
        f"🔄 Кол-во единиц: {order['units']}\n"
        f"💎 Очков за единицу: {order['points_per_unit']}"
    )

def activate_steam_lots(account, lot_ids):
    for lot_id in lot_ids:
        try:
            lot_fields = account.get_lot_fields(lot_id)
            if not lot_fields.active:
                lot_fields.active = True
                account.save_lot(lot_fields)
                logger.info(f"[autopoints] ✅ Лот {lot_id} активирован")
            else:
                logger.info(f"[autopoints] ℹ️ Лот {lot_id} уже активен")
            time.sleep(1.5)
        except Exception as e:
            logger.error(f"[autopoints] ❌ Ошибка активации лота {lot_id}: {e}")

def deactivate_steam_lots(account, lot_ids):
    for lot_id in lot_ids:
        try:
            lot_fields = account.get_lot_fields(lot_id)
            if lot_fields.active:
                lot_fields.active = False
                account.save_lot(lot_fields)
                logger.info(f"[autopoints] ✅ Лот {lot_id} деактивирован")
            else:
                logger.info(f"[autopoints] ℹ️ Лот {lot_id} уже не активен")
            time.sleep(1.5)
        except Exception as e:
            logger.error(f"[autopoints] ❌ Ошибка деактивации лота {lot_id}: {e}")

def restock_lots(account):
    global config, api_client, points_price
    
    if not config.get("auto_restock", False):
        return
        
    if not config["api_key"]:
        return
        
    try:
        balance = api_client.get_balance()
        if balance <= 0:
            return
            
        saved_lots = config["lot_manager"].get("saved_lots", [])
        if not saved_lots:
            return
            
        points_available = balance / points_price
        
        for lot_id in saved_lots:
            try:
                lot_fields = account.get_lot_fields(lot_id)
                
                points_per_unit = parse_points_from_description(lot_fields.description_ru)
                if points_per_unit <= 0:
                    points_per_unit = 1
                    
                new_amount = int(points_available // points_per_unit)
                if new_amount < 1:
                    new_amount = 0
                    
                if lot_fields.amount != new_amount:
                    lot_fields.amount = new_amount
                    account.save_lot(lot_fields)
                    logger.info(f"[autopoints] 🔄 Лот {lot_id} обновлен: {new_amount} шт.")
                    
                time.sleep(1.5)
                
            except Exception as e:
                logger.error(f"[autopoints] ❌ Ошибка обновления лота {lot_id}: {e}")
                time.sleep(5)
                
    except Exception as e:
        logger.error(f"[autopoints] ❌ Ошибка авто-пополнения: {e}")

def handle_new_order(c, event):
    global waiting_for_link, api_client, config
    
    order_id = event.order.id
    order = event.order
    logger.info(f"[autopoints] 📦 Новый заказ: #{order_id}")

    if order.subcategory.id != 714:
        return

    try:
        full_order = c.account.get_order(order_id)
        logger.info(f"[autopoints] 🔍 Детали заказа #{order_id} загружены")
    except Exception as e:
        logger.error(f"[autopoints] ❌ Ошибка: {e}")
        return

    if hasattr(full_order, "chat_id"):
        chat_id = full_order.chat_id
    elif hasattr(full_order, "chat") and hasattr(full_order.chat, "id"):
        chat_id = full_order.chat.id
    else:
        logger.error(f"[autopoints] ❌ Не найден chat_id для #{order_id}")
        return

    buyer_id = getattr(full_order, "buyer_id", None)
    if buyer_id is None:
        logger.error(f"[autopoints] ❌ Не найден buyer_id для #{order_id}")
        return

    units = (
        getattr(full_order, "quantity", None)
        or getattr(full_order, "count", None)
        or getattr(full_order, "amount", None)
    )
    
    if units is None:
        logger.error(f"[autopoints] ❌ Не определено количество для #{order_id}")
        c.account.send_message(chat_id, "❌ Ошибка: не удалось определить количество.")
        try_refund(c, order_id, "не определено количество")
        return

    description = getattr(full_order, "full_description", "")
    points_per_unit = parse_points_from_description(description)
    
    if points_per_unit <= 0:
        points_per_unit = 1
        
    total_points = points_per_unit * units
    logger.info(f"[autopoints] 🔢 Рассчитано очков: {points_per_unit} * {units} = {total_points}")

    try:
        revenue = full_order.sum
    except AttributeError:
        logger.error(f"[autopoints] ❌ Не найдена сумма заказа #{order_id}")
        c.account.send_message(chat_id, "❌ Ошибка: не удалось определить сумму заказа.")
        try_refund(c, order_id, "ошибка определения суммы")
        return

    if total_points < 100:
        logger.error(f"[autopoints] ❌ Недостаточное количество очков ({total_points})")
        c.account.send_message(chat_id, f"❌ Минимальное количество - 100 очков.\nВы заказали: {total_points} очков.")
        try_refund(c, order_id, "недостаточное количество очков")
        return

    if total_points % 100 != 0:
        logger.error(f"[autopoints] ❌ Некратное количество очков ({total_points})")
        c.account.send_message(chat_id, f"❌ Количество должно быть кратно 100.\nВы заказали: {total_points} очков.")
        try_refund(c, order_id, "некратное количество очков")
        return

    waiting_for_link[order_id] = {
        "buyer_id": buyer_id,
        "step": "await_link",
        "chat_id": chat_id,
        "qty": total_points,
        "order_id": order_id,
        "revenue": revenue,
        "units": units,
        "points_per_unit": points_per_unit
    }
    
    message = format_template(
        "start_message",
        total_points=total_points,
        points_per_unit=points_per_unit,
        units=units
    )
    
    c.account.send_message(chat_id, message)
    logger.info(f"[autopoints] ✅ Ожидаем ссылку от {buyer_id}")
    
    if config["lot_manager"]["auto_deactivate"] and config["api_key"]:
        try:
            balance = api_client.get_balance()
            threshold = config["lot_manager"]["balance_threshold"]
            
            if balance < threshold:
                saved_lots = config["lot_manager"].get("saved_lots", [])
                if saved_lots:
                    deactivate_steam_lots(c.account, saved_lots)
                    config["lot_manager"]["lots_active"] = False
                    save_config()
                    logger.info(f"[autopoints] 📦 Лоты деактивированы (баланс {balance} < {threshold})")
        except Exception as e:
            logger.error(f"[autopoints] ❌ Ошибка проверки баланса: {e}")

def try_refund(c, order_id, reason):
    if not config.get("auto_refunds", False):
        return
        
    try:
        c.account.refund(order_id)
        logger.info(f"[autopoints] 🔄 Заказ #{order_id} отменён ({reason})")
        return True
    except Exception as e:
        logger.error(f"[autopoints] ❌ Ошибка отмены #{order_id}: {e}")
        return False

def handle_new_message(c, event):
    global waiting_for_link
    
    msg = event.message
    chat_id = getattr(msg, "chat_id", None)
    text = getattr(msg, "content", None) or getattr(msg, "text", None)
    author_id = getattr(msg, "author_id", None)

    if text is None or chat_id is None or author_id is None:
        return

    text = text.replace("\u2061", "").strip()
    logger.info(f"[autopoints] 📥 Сообщение: {text[:50]}...")

    for order_id, data in list(waiting_for_link.items()):
        if data["buyer_id"] == author_id:
            if data["step"] == "await_link":
                link_match = re.search(r'(https?://\S+)', text)
                if not link_match:
                    c.account.send_message(chat_id, format_template("invalid_link"))
                    return
                    
                link = link_match.group(0)
                ok, reason = is_valid_link(link)
                if not ok:
                    c.account.send_message(chat_id, reason)
                    return

                data["link"] = link
                data["step"] = "await_confirm"
                c.account.send_message(
                    chat_id, 
                    format_template("link_confirmation", link=link)
                )
                return

            elif data["step"] == "await_confirm":
                if text.lower() == "+":
                    process_purchase(c, data)
                    return
                elif text.lower() == "-":
                    data["step"] = "await_link"
                    c.account.send_message(chat_id, "❌ Подтверждение отклонено. Введите другую ссылку.")
                    return
                else:
                    c.account.send_message(chat_id, "❌ Используйте + или - для подтверждения.")
                    return

def process_purchase(c, data):
    global api_client, order_history, points_price
    
    chat_id = data["chat_id"]
    link = data["link"]
    qty = data.get("qty", 0)
    order_id = data["order_id"]
    buyer_id = data["buyer_id"]
    revenue = data.get("revenue", 0)
    units = data.get("units", 1)
    points_per_unit = data.get("points_per_unit", 1)

    c.account.send_message(
        chat_id,
        f"⏳ Начинаю покупку {qty} очков..."
    )

    try:
        result = api_client.purchase_points(link, qty)
        
        success_message = format_template(
            "purchase_success",
            qty=qty,
            steam64=result.get('steam64', '')
        )
        
        c.account.send_message(chat_id, success_message)
        
        cost = qty * points_price
        
        order_history.append({
            "order_id": order_id,
            "buyer_id": buyer_id,
            "qty": qty,
            "link": link,
            "revenue": revenue,
            "cost": cost,
            "units": units,
            "points_per_unit": points_per_unit,
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })
        save_config()
        
        restock_lots(c.account)
        
    except Exception as e:
        error_msg = str(e)
        logger.error(f"[autopoints] ❌ Ошибка: {error_msg}")
        
        if "Insufficient" in error_msg:
            c.account.send_message(chat_id, format_template("insufficient_balance"))
            try_refund(c, order_id, "недостаток средств")
        else:
            c.account.send_message(
                chat_id, 
                format_template("purchase_error", error=error_msg)
            )
            
    finally:
        if order_id in waiting_for_link:
            del waiting_for_link[order_id]

def init_commands(c):
    global bot, cardinal, config, api_client, points_price
    
    cardinal = c
    bot = c.telegram.bot
    config = ensure_config()
    
    c.add_telegram_commands(
        UUID,
        [
            ("steam_points", "управление автопродажей Steam Points", True),
        ]
    )
    
    c.telegram.msg_handler(handle_command, commands=["steam_points"])
    
    if config.get("api_key"):
        api_client = SteamPointsAPIClient(api_key=config["api_key"])
        try:
            points_price = api_client.get_points_price()
            logger.info(f"[autopoints] ✅ Курс очков: {points_price}")
        except:
            logger.error(f"[autopoints] ❌ Ошибка получения курса очков")
        logger.info(f"[autopoints] 🔑 API клиент инициализирован")

    bot.register_message_handler(handle_command, commands=["steam_points"])
    bot.register_callback_query_handler(handle_callback, func=lambda call: call.data.startswith("ap_"))

BIND_TO_PRE_INIT    = [init_commands]
BIND_TO_NEW_ORDER   = [handle_new_order]
BIND_TO_NEW_MESSAGE = [handle_new_message]
BIND_TO_DELETE = []