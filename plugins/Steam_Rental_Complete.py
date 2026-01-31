import os
import logging
import json
import time
import threading
import random
import string
import requests
from uuid import uuid4
from datetime import datetime, timedelta
import re
import hashlib
import base64
import hmac

from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

# Информация о плагине (обязательные поля)
NAME = "Auto_fxck"
VERSION = "6.6.6"
DESCRIPTION = "Продвинутая система аренды аккаунтов Steam"
CREDITS = "@stophittingyoursxlf"
UUID = "a09b8c7d-6e5f-4a3b-8c1d-098765432109"  # Valid UUID4 format
SETTINGS_PAGE = False 

# Настройка логгера
logger = logging.getLogger("FPC.Steam_Rental")
LOGGER_PREFIX = "[Steam_Rental]"

# Глобальные константы
DATA_DIR = os.path.join("data", "steam_rental")
ACCOUNTS_FILE = os.path.join(DATA_DIR, "accounts.json")
RENTALS_FILE = os.path.join(DATA_DIR, "rentals.json")
LOT_BINDINGS_FILE = os.path.join(DATA_DIR, "lot_bindings.json")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
TEMPLATES_FILE = os.path.join(DATA_DIR, "message_templates.json")

# Состояния для интерактивного добавления аккаунта
ADD_ACCOUNT_STATES = {}  # chat_id -> {state: "login|password|type|api_key", data: {}}
EDIT_TEMPLATE_STATES = {}  # chat_id -> {template_name: "...", editing: True/False}
ADMIN_ID_STATES = {}  # chat_id -> {setting: True/False}
ADD_BINDING_STATES = {}  # chat_id -> {state: "name|type|duration", data: {}}

# Создаем директории при необходимости
os.makedirs(DATA_DIR, exist_ok=True)

# Глобальные переменные
RUNNING = False
AUTO_START = True  # Автоматический запуск при инициализации
CARDINAL = None
lot_bindings = {}  # lot_id -> {"account_type": "...", "duration_hours": N}
message_templates = {}  # template_name -> template_text
admin_id = None  # ID администратора
binding_hash_map = {}  # Сопоставление хешей с именами лотов

# Стандартные шаблоны сообщений
DEFAULT_TEMPLATES = {
    "rental_start": "🎮 <b>Аренда аккаунта Steam</b>\n\n"
                   "👤 Логин: <code>{login}</code>\n"
                   "🔑 Пароль: <code>{password}</code>\n"
                   "🔰 Тип: {account_type}\n\n"
                   "⏱ Срок аренды: {duration_hours} ч.\n"
                   "⌛ Дата окончания: {end_time}\n\n"
                   "❗ <b>Важно:</b>\n"
                   "• По истечении срока доступ будет заблокирован\n"
                   "• Пароль будет изменен\n"
                   "• Не меняйте пароль от аккаунта\n"
                   "• Не включайте двухфакторную аутентификацию",
    
    "rental_end": "⏰ <b>Аренда аккаунта завершена</b>\n\n"
                 "Срок аренды аккаунта Steam истек. Доступ прекращен, пароль изменен.\n"
                 "Благодарим за использование нашего сервиса!",
    
    "rental_force_end": "⚠️ <b>Аренда досрочно завершена</b>\n\n"
                       "Аренда аккаунта Steam была принудительно завершена администратором.\n"
                       "Доступ прекращен, пароль изменен.",
    
    "admin_rental_start": "✅ <b>Аккаунт выдан</b>\n\n"
                         "🔹 Заказ: <code>#{order_id}</code>\n"
                         "🔹 Покупатель: <b>{username}</b>\n"
                         "🔹 Аккаунт: <code>{login}</code>\n"
                         "🔹 Пароль: <code>{password}</code>\n"
                         "🔹 Тип: <code>{account_type}</code>\n"
                         "🔹 Срок: <code>{duration_hours} ч.</code>\n"
                         "🔹 Окончание: {end_time}",
    
    "admin_rental_end": "⏰ <b>Аренда завершена</b>\n\n"
                       "👤 Пользователь: {username}\n"
                       "🎮 Аккаунт: {login}\n"
                       "🔰 Тип: {account_type}\n"
                       "✅ Аккаунт возвращен в пул доступных\n"
                       "🔐 Новый пароль: <code>{new_password}</code>"
}

# Классы данных
class Account:
    def __init__(self, login, password, status="available", account_type="standard", api_key=None):
        self.login = login
        self.password = password
        self.status = status  # available, rented, disabled
        self.type = account_type
        self.rental_id = None
        self.api_key = api_key  # API ключ для управления Steam сессиями
        self.original_password = password  # Сохраняем изначальный пароль
        
    def to_dict(self):
        return {
            "login": self.login,
            "password": self.password,
            "status": self.status,
            "type": self.type,
            "rental_id": self.rental_id,
            "api_key": self.api_key,
            "original_password": self.original_password
        }
        
    @staticmethod
    def from_dict(data):
        account = Account(
            data["login"],
            data["password"],
            data.get("status", "available"),
            data.get("type", "standard"),
            data.get("api_key")
        )
        account.rental_id = data.get("rental_id")
        account.original_password = data.get("original_password", data["password"])
        return account

    def change_password(self, new_password=None):
        """Изменяет пароль аккаунта, возвращает новый пароль"""
        if new_password is None:
            # Генерируем случайный надежный пароль
            new_password = generate_strong_password()
        
        # Здесь можно добавить вызов API Steam для смены пароля
        old_password = self.password
        self.password = new_password
        
        # Пытаемся сменить пароль через API Steam, если есть API ключ
        if self.api_key:
            success = self.change_password_via_api(old_password, new_password)
            if success:
                logger.info(f"{LOGGER_PREFIX} Пароль для аккаунта {self.login} успешно изменен через API")
            else:
                logger.warning(f"{LOGGER_PREFIX} Не удалось изменить пароль через API для {self.login}")
        
        return new_password

    def change_password_via_api(self, old_password, new_password):
        """Изменяет пароль аккаунта через Steam API"""
        try:
            # Используем правильный API URL для обновления пароля
            api_url = "https://api.steampowered.com/IAuthenticationService/UpdatePassword/v1/"
            
            # Получаем rsatimestamp и publickey_mod для шифрования пароля
            get_key_url = "https://steamcommunity.com/login/getrsakey/"
            get_key_data = {
                "username": self.login,
                "donotcache": int(time.time() * 1000)
            }
            
            # Настройки для запросов
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://steamcommunity.com",
                "Referer": "https://steamcommunity.com/login/home/"
            }
            
            # Получаем RSA ключ для шифрования пароля
            key_response = requests.post(get_key_url, data=get_key_data, headers=headers)
            if not key_response.ok:
                logger.error(f"{LOGGER_PREFIX} Ошибка получения RSA ключа: {key_response.status_code}")
                return False, "Ошибка получения RSA ключа", None
            
            key_data = key_response.json()
            if not key_data.get("success"):
                logger.error(f"{LOGGER_PREFIX} Сервер не вернул RSA ключ: {key_data}")
                return False, "Сервер не вернул RSA ключ", None
            
            # Подготавливаем данные для шифрования
            timestamp = key_data.get("timestamp")
            modulus = int(key_data.get("publickey_mod"), 16)
            exponent = int(key_data.get("publickey_exp"), 16)
            
            # Шифруем пароль с помощью RSA
            from Crypto.PublicKey import RSA
            from Crypto.Cipher import PKCS1_v1_5
            
            key = RSA.construct((modulus, exponent))
            cipher = PKCS1_v1_5.new(key)
            encrypted_password = base64.b64encode(cipher.encrypt(old_password.encode('utf-8')))
            
            # Данные для авторизации
            login_data = {
                "username": self.login,
                "password": encrypted_password.decode('utf-8'),
                "rsatimestamp": timestamp,
                "remember_login": True,
                "captchagid": -1,
                "captcha_text": ""
            }
            
            # URL для входа
            login_url = "https://steamcommunity.com/login/dologin/"
            
            # Выполняем вход
            session = requests.Session()
            login_response = session.post(login_url, data=login_data, headers=headers)
            login_result = login_response.json()
            
            if not login_result.get("success"):
                error_message = login_result.get("message", "Неизвестная ошибка авторизации")
                logger.error(f"{LOGGER_PREFIX} Ошибка авторизации в Steam: {error_message}")
                return False, f"Ошибка авторизации: {error_message}", None
            
            # Если авторизация успешна, меняем пароль
            steamid = login_result.get("transfer_parameters", {}).get("steamid")
            if not steamid:
                logger.error(f"{LOGGER_PREFIX} Не удалось получить steamid после авторизации")
                return False, "Не удалось получить steamid", None
            
            # Получаем токен доступа из cookie
            sessionid = None
            for cookie in session.cookies:
                if cookie.name == "sessionid":
                    sessionid = cookie.value
                    break
            
            if not sessionid:
                logger.error(f"{LOGGER_PREFIX} Не удалось получить sessionid из cookies")
                return False, "Не удалось получить sessionid", None
            
            # Данные для изменения пароля
            change_password_data = {
                "sessionid": sessionid,
                "steamid": steamid,
                "password": old_password,
                "new_password": new_password,
                "confirm_new_password": new_password
            }
            
            # URL для изменения пароля
            change_password_url = "https://steamcommunity.com/profiles/" + steamid + "/edit/changepassword"
            
            # Выполняем запрос на изменение пароля
            change_response = session.post(change_password_url, data=change_password_data, headers=headers)
            
            # Проверяем успешность изменения пароля
            if change_response.ok and "successfully updated" in change_response.text.lower():
                logger.info(f"{LOGGER_PREFIX} Пароль для {self.login} успешно изменен")
                return True, "Пароль успешно изменен", new_password
            else:
                logger.error(f"{LOGGER_PREFIX} Ошибка изменения пароля: {change_response.status_code}")
                return False, f"Ошибка изменения пароля: {change_response.status_code}", None
        
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Исключение при смене пароля через API: {str(e)}")
            return False, f"Ошибка при смене пароля: {str(e)}", None

    def end_session(self):
        """Завершает сессии на аккаунте"""
        self.status = "available"  # Меняем статус на доступный
        
        if self.api_key:
            try:
                # Пытаемся завершить сессии через API
                success = self.end_session_via_api()
                if success:
                    logger.info(f"{LOGGER_PREFIX} Сессии для аккаунта {self.login} успешно завершены через API")
                else:
                    logger.warning(f"{LOGGER_PREFIX} Не удалось завершить сессии через API для {self.login}")
                return success
            except Exception as e:
                logger.error(f"{LOGGER_PREFIX} Ошибка завершения сессий: {e}")
        
        return True  # Считаем, что сессии завершены

    def end_session_via_api(self):
        """Завершает сессии на аккаунте через Steam API"""
        try:
            # Обращение к Steam API для завершения сессий
            # https://partner.steamgames.com/doc/webapi/ISteamUser
            api_url = "https://api.steampowered.com/ISteamUser/RevokeAuthSessions/v1/"
            headers = {
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Bearer {self.api_key}"
            }
            data = {
                "steamid": self.login
            }
            
            # Отправляем запрос к API
            response = requests.post(api_url, headers=headers, data=data, timeout=10)
            if response.status_code == 200:
                try:
                    result = response.json()
                    return result.get('success', False)
                except:
                    return response.status_code == 200
            return False
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка API запроса для завершения сессий: {e}")
            return False

    def reset_to_original_password(self):
        """Сбрасывает пароль к исходному значению"""
        if self.original_password:
            old_password = self.password
            self.password = self.original_password
            
            # Пытаемся сменить пароль через API Steam, если есть API ключ
            if self.api_key:
                success = self.change_password_via_api(old_password, self.original_password)
                if success:
                    logger.info(f"{LOGGER_PREFIX} Пароль для аккаунта {self.login} сброшен к исходному через API")
                else:
                    logger.warning(f"{LOGGER_PREFIX} Не удалось сбросить пароль через API для {self.login}")
            
            return True
        return False

class Rental:
    def __init__(self, account_login, user_id, username, duration_hours, order_id=None):
        self.id = str(uuid4())
        self.account_login = account_login
        self.user_id = user_id
        self.username = username
        self.start_time = time.time()
        self.duration_hours = duration_hours
        self.end_time = self.start_time + (duration_hours * 3600)
        self.order_id = order_id
        self.is_active = True
        
    def to_dict(self):
        return {
            "id": self.id,
            "account_login": self.account_login,
            "user_id": self.user_id,
            "username": self.username,
            "start_time": self.start_time,
            "duration_hours": self.duration_hours,
            "end_time": self.end_time,
            "order_id": self.order_id,
            "is_active": self.is_active
        }
        
    @staticmethod
    def from_dict(data):
        rental = Rental(
            data["account_login"],
            data["user_id"],
            data["username"],
            data["duration_hours"],
            data.get("order_id")
        )
        rental.id = data["id"]
        rental.start_time = data["start_time"]
        rental.end_time = data["end_time"]
        rental.is_active = data["is_active"]
        return rental
        
    def is_expired(self):
        return time.time() >= self.end_time
        
    def get_remaining_time(self):
        if not self.is_active:
            return timedelta(0)
        
        remaining_seconds = max(0, self.end_time - time.time())
        return timedelta(seconds=remaining_seconds)
    
    def extend_rental(self, additional_hours):
        """Продлевает аренду на указанное количество часов"""
        self.duration_hours += additional_hours
        self.end_time += additional_hours * 3600
        return True
    
    def get_formatted_end_time(self):
        """Возвращает отформатированное время окончания аренды"""
        return datetime.fromtimestamp(self.end_time).strftime("%d.%m.%Y %H:%M")

# Вспомогательные функции
def generate_strong_password(length=12):
    """Генерирует надежный случайный пароль"""
    lowercase = string.ascii_lowercase
    uppercase = string.ascii_uppercase
    digits = string.digits
    special = "!@#$%^&*"
    
    # Убедимся, что в пароле будет хотя бы по одному символу каждого типа
    password = [
        random.choice(lowercase),
        random.choice(uppercase),
        random.choice(digits),
        random.choice(special)
    ]
    
    # Добавляем остальные символы
    remaining_length = length - len(password)
    all_chars = lowercase + uppercase + digits + special
    password.extend(random.choice(all_chars) for _ in range(remaining_length))
    
    # Перемешиваем пароль
    random.shuffle(password)
    
    return ''.join(password)

def format_message(template_name, **kwargs):
    """Форматирует сообщение по шаблону с заменой переменных"""
    if template_name not in message_templates:
        logger.warning(f"{LOGGER_PREFIX} Шаблон '{template_name}' не найден, используем стандартный")
        template = DEFAULT_TEMPLATES.get(template_name, "Текст сообщения")
    else:
        template = message_templates[template_name]
    
    # Заменяем переменные в шаблоне
    try:
        formatted_message = template.format(**kwargs)
        return formatted_message
    except KeyError as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка форматирования шаблона: отсутствует ключ {e}")
        return template
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка форматирования шаблона: {e}")
        return template

# Управление данными
class RentalManager:
    def __init__(self):
        self.accounts = {}  # login -> Account
        self.rentals = {}   # id -> Rental
        self.load_data()
        
    def load_data(self):
        """Загружает данные из файлов"""
        # Загрузка аккаунтов
        if os.path.exists(ACCOUNTS_FILE):
            try:
                with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
                    accounts_data = json.load(f)
                    self.accounts = {
                        login: Account.from_dict(data) 
                        for login, data in accounts_data.items()
                    }
            except Exception as e:
                logger.error(f"{LOGGER_PREFIX} Ошибка загрузки аккаунтов: {e}")
                self.accounts = {}
        
        # Загрузка аренд
        if os.path.exists(RENTALS_FILE):
            try:
                with open(RENTALS_FILE, "r", encoding="utf-8") as f:
                    rentals_data = json.load(f)
                    self.rentals = {
                        data["id"]: Rental.from_dict(data)
                        for data in rentals_data
                    }
            except Exception as e:
                logger.error(f"{LOGGER_PREFIX} Ошибка загрузки аренд: {e}")
                self.rentals = {}
    
    def save_data(self):
        """Сохраняет данные в файлы"""
        # Сохранение аккаунтов
        try:
            accounts_data = {
                login: account.to_dict()
                for login, account in self.accounts.items()
            }
            with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
                json.dump(accounts_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка сохранения аккаунтов: {e}")
        
        # Сохранение аренд
        try:
            rentals_data = [rental.to_dict() for rental in self.rentals.values()]
            with open(RENTALS_FILE, "w", encoding="utf-8") as f:
                json.dump(rentals_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка сохранения аренд: {e}")
    
    def add_account(self, login, password, account_type="standard", api_key=None):
        """Добавляет новый аккаунт"""
        if login in self.accounts:
            return False, "Аккаунт с таким логином уже существует"
        
        # Стандартизируем типы аккаунтов
        normalized_type = account_type.lower().replace('.', '').replace(' ', '')
        if normalized_type == "repo":
            # Используем единый формат для REPO аккаунтов
            account_type = "repo"
        
        self.accounts[login] = Account(login, password, "available", account_type, api_key)
        self.save_data()
        return True, "Аккаунт успешно добавлен"
    
    def update_account(self, login, **kwargs):
        """Обновляет данные аккаунта"""
        if login not in self.accounts:
            return False, "Аккаунт не найден"
        
        account = self.accounts[login]
        
        # Обновляем поля, если они указаны
        if "password" in kwargs:
            account.password = kwargs["password"]
            account.original_password = kwargs.get("original_password", account.password)
        
        if "type" in kwargs:
            account.type = kwargs["type"]
        
        if "api_key" in kwargs:
            account.api_key = kwargs["api_key"]
        
        self.save_data()
        return True, "Аккаунт успешно обновлен"
    
    def remove_account(self, login):
        """Удаляет аккаунт"""
        if login not in self.accounts:
            return False, "Аккаунт не найден"
        
        account = self.accounts[login]
        if account.status == "rented":
            return False, "Нельзя удалить аккаунт, который сейчас в аренде"
        
        del self.accounts[login]
        self.save_data()
        return True, "Аккаунт успешно удален"
    
    def get_available_account(self, account_type=None):
        """Возвращает доступный аккаунт указанного типа"""
        if not account_type:
            # Если тип не указан, вернем любой доступный
            for login, account in self.accounts.items():
                if account.status == "available":
                    logger.info(f"{LOGGER_PREFIX} Выбран любой доступный аккаунт: {login} ({account.type})")
                    return account
            logger.warning(f"{LOGGER_PREFIX} Нет доступных аккаунтов в системе")
            return None
    
        logger.info(f"{LOGGER_PREFIX} Поиск доступного аккаунта по типу: {account_type}")
            
        # Используем нормализацию для сравнения типов
        normalized_type = account_type.lower().replace('.', '').replace(' ', '')
        logger.info(f"{LOGGER_PREFIX} Нормализованный тип: {normalized_type}")
        
        # Вывод всех доступных типов для отладки
        available_types = [(account.login, account.type, account.status) for account in self.accounts.values()]
        logger.info(f"{LOGGER_PREFIX} Доступные аккаунты: {available_types}")
        
        # Сначала пытаемся найти точное совпадение
        for login, account in self.accounts.items():
            if account.status == "available" and account.type.lower() == account_type.lower():
                logger.info(f"{LOGGER_PREFIX} Найдено точное совпадение: {login} ({account.type})")
                return account
                
        # Если не нашли точное совпадение, ищем с нормализацией
        for login, account in self.accounts.items():
            if account.status == "available":
                normalized_account_type = account.type.lower().replace('.', '').replace(' ', '')
                if normalized_account_type == normalized_type:
                    logger.info(f"{LOGGER_PREFIX} Найдено совпадение по нормализации: {login} ({account.type})")
                    return account
        
        # Специальная обработка для R.E.P.O / REPO
        if normalized_type == "repo":
            for login, account in self.accounts.items():
                if account.status == "available":
                    acc_type = account.type.lower().replace('.', '').replace(' ', '')
                    if acc_type == "repo":
                        logger.info(f"{LOGGER_PREFIX} Найдено совпадение REPO: {login} ({account.type})")
                        return account
        
        logger.warning(f"{LOGGER_PREFIX} Не найдено доступных аккаунтов типа {account_type}")
        return None
    
    def rent_account(self, user_id, username, duration_hours, account_type=None, order_id=None, specific_account=None):
        """Аренда аккаунта"""
        # Если указан конкретный аккаунт для аренды
        if specific_account:
            if specific_account.login in self.accounts and self.accounts[specific_account.login].status == "available":
                account = self.accounts[specific_account.login]
            else:
                return False, "Указанный аккаунт недоступен", None, None
        else:
            # Иначе ищем доступный аккаунт указанного типа
            account = self.get_available_account(account_type)
            
        if not account:
            return False, "Нет доступных аккаунтов", None, None
        
        # Создаем запись об аренде
        rental = Rental(account.login, user_id, username, duration_hours, order_id)
        
        # Обновляем статус аккаунта
        account.status = "rented"
        account.rental_id = rental.id
        
        # Сохраняем данные
        self.rentals[rental.id] = rental
        self.save_data()
        
        return True, "Аккаунт успешно арендован", account, rental
    
    def return_account(self, rental_id):
        """Возвращает аккаунт от аренды"""
        if rental_id not in self.rentals:
            return False, "Аренда не найдена"
        
        rental = self.rentals[rental_id]
        if not rental.is_active:
            return False, "Аренда уже завершена"
        
        # Находим аккаунт
        if rental.account_login not in self.accounts:
            logger.error(f"{LOGGER_PREFIX} Аккаунт для аренды {rental_id} не найден")
            rental.is_active = False
            self.save_data()
            return False, "Аккаунт не найден"
        
        account = self.accounts[rental.account_login]
        
        # Обновляем статус
        account.status = "available"
        account.rental_id = None
        rental.is_active = False
        
        # Генерируем новый пароль и завершаем сессии
        new_password = account.change_password()
        
        # Завершаем сессии
        try:
            account.end_session()
            logger.info(f"{LOGGER_PREFIX} Сессии для аккаунта {account.login} завершены")
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка завершения сессий для аккаунта {account.login}: {e}")
        
        # Сохраняем данные
        self.save_data()
        
        return True, "Аккаунт успешно возвращен", new_password
    
    def check_expired_rentals(self):
        """Проверяет истекшие аренды и возвращает их список"""
        expired_rentals = []
        
        for rental_id, rental in list(self.rentals.items()):
            if rental.is_active and rental.is_expired():
                account_login = rental.account_login
                if account_login in self.accounts:
                    account = self.accounts[account_login]
                    success, message, new_password = self.return_account(rental_id)
                    if success:
                        expired_rentals.append((rental, account, new_password))
                    else:
                        try:
                            logger.error(f"{LOGGER_PREFIX} Ошибка возврата истекшей аренды: {message}")
                        except Exception:
                            pass
        
        return expired_rentals
    
    def get_account_by_type(self, account_type):
        """Возвращает доступный аккаунт указанного типа"""
        if not account_type:
            return None
            
        logger.info(f"{LOGGER_PREFIX} Поиск аккаунта по типу: {account_type}")
            
        # Нормализация типа для лучшего сравнения
        normalized_type = account_type.lower().replace('.', '').replace(' ', '')
        logger.info(f"{LOGGER_PREFIX} Нормализованный тип: {normalized_type}")
        
        # Вывод всех доступных типов для отладки
        available_types = [(account.login, account.type, account.status) for account in self.accounts.values()]
        logger.info(f"{LOGGER_PREFIX} Доступные аккаунты: {available_types}")
        
        # Сначала пытаемся найти точное совпадение
        for login, account in self.accounts.items():
            if account.status == "available" and account.type.lower() == account_type.lower():
                logger.info(f"{LOGGER_PREFIX} Найдено точное совпадение: {login} ({account.type})")
                return account
                
        # Если не нашли точное совпадение, ищем, убрав специальные символы
        for login, account in self.accounts.items():
            if account.status == "available":
                normalized_account_type = account.type.lower().replace('.', '').replace(' ', '')
                if normalized_account_type == normalized_type:
                    logger.info(f"{LOGGER_PREFIX} Найдено совпадение по нормализации: {login} ({account.type})")
                    return account
                    
        # Специальная обработка для R.E.P.O / REPO
        if normalized_type == "repo":
            for login, account in self.accounts.items():
                if account.status == "available":
                    acc_type = account.type.lower().replace('.', '').replace(' ', '')
                    if acc_type == "repo":
                        logger.info(f"{LOGGER_PREFIX} Найдено совпадение REPO: {login} ({account.type})")
                        return account
        
        logger.warning(f"{LOGGER_PREFIX} Не найдено доступных аккаунтов типа {account_type}")                
        return None
    
    def extend_rental(self, rental_id, additional_hours):
        """Продлевает аренду на указанное количество часов"""
        if rental_id not in self.rentals:
            return False, "Аренда не найдена"
        
        rental = self.rentals[rental_id]
        if not rental.is_active:
            return False, "Аренда уже завершена"
        
        # Продлеваем аренду
        rental.extend_rental(additional_hours)
        self.save_data()
        
        return True, f"Аренда продлена на {additional_hours} ч. Новое время окончания: {rental.get_formatted_end_time()}"
    
    def reset_account_password(self, login):
        """Сбрасывает пароль аккаунта к исходному значению"""
        if login not in self.accounts:
            return False, "Аккаунт не найден"
        
        account = self.accounts[login]
        if account.status == "rented":
            return False, "Нельзя сбросить пароль арендованного аккаунта"
        
        # Сбрасываем пароль
        if account.reset_to_original_password():
            self.save_data()
            return True, f"Пароль аккаунта сброшен к исходному: {account.password}"
        else:
            return False, "Не удалось сбросить пароль (исходный пароль не сохранен)"
    
    def get_account_info(self, login):
        """Возвращает подробную информацию об аккаунте"""
        if login not in self.accounts:
            return None
        
        account = self.accounts[login]
        info = {
            "login": account.login,
            "password": account.password,
            "type": account.type,
            "status": account.status,
            "rental_id": account.rental_id,
            "has_api_key": bool(account.api_key),
            "original_password": account.original_password
        }
        
        # Если аккаунт в аренде, добавляем информацию об аренде
        if account.status == "rented" and account.rental_id in self.rentals:
            rental = self.rentals[account.rental_id]
            remaining_time = rental.get_remaining_time()
            hours, remainder = divmod(remaining_time.seconds, 3600)
            minutes, _ = divmod(remainder, 60)
            
            info["rental"] = {
                "username": rental.username,
                "user_id": rental.user_id,
                "order_id": rental.order_id,
                "duration_hours": rental.duration_hours,
                "start_time": datetime.fromtimestamp(rental.start_time).strftime("%d.%m.%Y %H:%M"),
                "end_time": datetime.fromtimestamp(rental.end_time).strftime("%d.%m.%Y %H:%M"),
                "remaining_hours": hours,
                "remaining_minutes": minutes
            }
        
        return info

# Создаем глобальный менеджер аренды
rental_manager = RentalManager()

# Добавим функцию для загрузки конфигурации
def load_config():
    """Загружает настройки из файла конфигурации"""
    global AUTO_START, admin_id, message_templates
    
    # Загружаем основные настройки
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                config = json.load(f)
                AUTO_START = config.get("auto_start", AUTO_START)
                if "admin_id" in config and config["admin_id"] is not None:
                    admin_id = config["admin_id"]
                logger.info(f"{LOGGER_PREFIX} Загружена настройка автозапуска: {AUTO_START}")
                logger.info(f"{LOGGER_PREFIX} Загружен admin_id: {admin_id}")
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка загрузки настроек: {e}")
    else:
        # Создаем файл с настройками по умолчанию
        save_config()
    
    # Загружаем шаблоны сообщений
    if os.path.exists(TEMPLATES_FILE):
        try:
            with open(TEMPLATES_FILE, "r", encoding="utf-8") as f:
                message_templates = json.load(f)
                logger.info(f"{LOGGER_PREFIX} Загружено {len(message_templates)} шаблонов сообщений")
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка загрузки шаблонов сообщений: {e}")
            # Используем стандартные шаблоны
            message_templates = DEFAULT_TEMPLATES.copy()
            save_templates()
    else:
        # Используем стандартные шаблоны
        message_templates = DEFAULT_TEMPLATES.copy()
        save_templates()

def save_config():
    """Сохраняет настройки в файл конфигурации"""
    try:
        config = {
            "auto_start": AUTO_START,
            "admin_id": admin_id
        }
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        logger.info(f"{LOGGER_PREFIX} Настройки сохранены")
        return True
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка сохранения настроек: {e}")
        return False

def save_templates():
    """Сохраняет шаблоны сообщений в файл"""
    try:
        with open(TEMPLATES_FILE, "w", encoding="utf-8") as f:
            json.dump(message_templates, f, ensure_ascii=False, indent=2)
        logger.info(f"{LOGGER_PREFIX} Шаблоны сообщений сохранены")
        return True
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка сохранения шаблонов сообщений: {e}")
        return False

def load_lot_bindings():
    """Загружает привязки лотов из файла"""
    global lot_bindings
    
    if os.path.exists(LOT_BINDINGS_FILE):
        try:
            with open(LOT_BINDINGS_FILE, "r", encoding="utf-8") as f:
                lot_bindings = json.load(f)
                logger.info(f"{LOGGER_PREFIX} Загружено {len(lot_bindings)} привязок лотов")
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка загрузки привязок лотов: {e}")
            lot_bindings = {}
    else:
        lot_bindings = {}
        save_lot_bindings()  # Создаем пустой файл

def save_lot_bindings():
    """Сохраняет привязки лотов в файл"""
    try:
        # Убедимся, что директория существует
        os.makedirs(os.path.dirname(LOT_BINDINGS_FILE), exist_ok=True)
        
        with open(LOT_BINDINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(lot_bindings, f, ensure_ascii=False, indent=2)
        logger.info(f"{LOGGER_PREFIX} Сохранено {len(lot_bindings)} привязок лотов")
        return True
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка сохранения привязок лотов: {e}")
        return False

# Команды для управления admin_id
def set_admin_id_cmd(message):
    """Устанавливает ID администратора"""
    global admin_id
    
    # Проверяем, был ли уже установлен admin_id
    if admin_id:
        # Если админ уже есть, проверяем, что команду вызывает текущий админ
        if message.chat.id != admin_id:
            CARDINAL.telegram.bot.send_message(
                message.chat.id,
                "❌ <b>Доступ запрещен</b>\n\n"
                "Только текущий администратор может изменить ID администратора.",
                parse_mode="HTML"
            )
            return
    
    # Получаем ID из сообщения
    text = message.text.strip()
    if text.startswith('/admin_id'):
        text = text[len('/admin_id'):].strip()
    
    if text:
        # Если указан ID в команде, устанавливаем его
        try:
            new_admin_id = int(text)
            admin_id = new_admin_id
            save_config()
            
            CARDINAL.telegram.bot.send_message(
                message.chat.id,
                "✅ <b>ID администратора успешно установлен</b>\n\n"
                f"Новый ID администратора: <code>{admin_id}</code>",
                parse_mode="HTML"
            )
        except ValueError:
            CARDINAL.telegram.bot.send_message(
                message.chat.id,
                "❌ <b>Ошибка</b>\n\n"
                "ID администратора должен быть числом.\n"
                "Пример: <code>/admin_id 123456789</code>",
                parse_mode="HTML"
            )
    else:
        # Если ID не указан, предлагаем установить текущий ID
        markup = InlineKeyboardMarkup()
        markup.row(InlineKeyboardButton("✅ Установить мой ID", callback_data=f"srent_set_admin_id_{message.chat.id}"))
        
        CARDINAL.telegram.bot.send_message(
            message.chat.id,
            "🔐 <b>Установка ID администратора</b>\n\n"
            "Вы можете установить свой ID в качестве ID администратора, нажав на кнопку ниже.\n\n"
            f"Ваш ID: <code>{message.chat.id}</code>\n\n"
            "Или укажите ID вручную с помощью команды:\n"
            "<code>/admin_id ЧИСЛО</code>",
            reply_markup=markup,
            parse_mode="HTML"
        )

def set_admin_id_callback(call):
    """Устанавливает ID администратора через callback"""
    global admin_id
    
    # Получаем ID из callback data
    data = call.data.split('_')
    if len(data) < 4:
        CARDINAL.telegram.bot.answer_callback_query(call.id, "Ошибка в данных callback")
        return
    
    try:
        new_admin_id = int(data[3])
        admin_id = new_admin_id
        save_config()
        
        CARDINAL.telegram.bot.edit_message_text(
            "✅ <b>ID администратора успешно установлен</b>\n\n"
            f"Новый ID администратора: <code>{admin_id}</code>",
            call.message.chat.id,
            call.message.message_id,
            parse_mode="HTML"
        )
        CARDINAL.telegram.bot.answer_callback_query(call.id, "ID администратора установлен!")
    except ValueError:
        CARDINAL.telegram.bot.answer_callback_query(call.id, "Некорректный формат ID")

# Команды для управления шаблонами сообщений
def list_templates_cmd(message):
    """Показывает список доступных шаблонов сообщений"""
    # Проверяем права администратора
    if admin_id and message.chat.id != admin_id:
        CARDINAL.telegram.bot.send_message(
            message.chat.id,
            "❌ <b>Доступ запрещен</b>\n\n"
            "Только администратор может управлять шаблонами сообщений.",
            parse_mode="HTML"
        )
        return
    
    if not message_templates:
        CARDINAL.telegram.bot.send_message(
            message.chat.id,
            "ℹ️ <b>Шаблоны сообщений</b>\n\n"
            "В данный момент не настроено ни одного шаблона.",
            parse_mode="HTML"
        )
        return
    
    # Формируем список шаблонов
    templates_text = "📝 <b>Доступные шаблоны сообщений:</b>\n\n"
    for name, template in message_templates.items():
        # Обрезаем длинные шаблоны для компактности
        preview = template[:100] + "..." if len(template) > 100 else template
        templates_text += f"<b>{name}</b>\n{preview}\n\n"
        templates_text += f"Редактировать: <code>/edit_template {name}</code>\n\n"
    
    templates_text += "\n<b>Управление шаблонами:</b>\n"
    templates_text += "• Просмотреть: <code>/view_template ИМЯ</code>\n"
    templates_text += "• Редактировать: <code>/edit_template ИМЯ</code>\n"
    templates_text += "• Сбросить все к стандартным: <code>/reset_templates</code>"
    
    CARDINAL.telegram.bot.send_message(
        message.chat.id,
        templates_text,
        parse_mode="HTML"
    )

def view_template_cmd(message):
    """Показывает содержимое конкретного шаблона"""
    # Проверяем права администратора
    if admin_id and message.chat.id != admin_id:
        CARDINAL.telegram.bot.send_message(
            message.chat.id,
            "❌ <b>Доступ запрещен</b>\n\n"
            "Только администратор может просматривать шаблоны сообщений.",
            parse_mode="HTML"
        )
        return
    
    # Получаем имя шаблона из сообщения
    text = message.text.strip()
    if text.startswith('/view_template'):
        text = text[len('/view_template'):].strip()
    
    if not text:
        CARDINAL.telegram.bot.send_message(
            message.chat.id,
            "❌ <b>Не указано имя шаблона</b>\n\n"
            "Используйте: <code>/view_template ИМЯ</code>\n\n"
            "Доступные шаблоны можно посмотреть командой <code>/templates</code>",
            parse_mode="HTML"
        )
        return
    
    # Ищем шаблон
    if text in message_templates:
        template = message_templates[text]
        
        markup = InlineKeyboardMarkup()
        markup.row(InlineKeyboardButton("✏️ Редактировать", callback_data=f"srent_edit_template_{text}"))
        markup.row(InlineKeyboardButton("↩️ К списку шаблонов", callback_data="srent_list_templates"))
        
        CARDINAL.telegram.bot.send_message(
            message.chat.id,
            f"📝 <b>Шаблон: {text}</b>\n\n"
            f"{template}",
            reply_markup=markup,
            parse_mode="HTML"
        )
    else:
        CARDINAL.telegram.bot.send_message(
            message.chat.id,
            "❌ <b>Шаблон не найден</b>\n\n"
            f"Шаблон с именем '{text}' не существует.\n\n"
            "Доступные шаблоны можно посмотреть командой <code>/templates</code>",
            parse_mode="HTML"
        )

def edit_template_cmd(message):
    """Начинает процесс редактирования шаблона"""
    # Проверяем права администратора
    if admin_id and message.chat.id != admin_id:
        CARDINAL.telegram.bot.send_message(
            message.chat.id,
            "❌ <b>Доступ запрещен</b>\n\n"
            "Только администратор может редактировать шаблоны сообщений.",
            parse_mode="HTML"
        )
        return
    
    # Получаем имя шаблона из сообщения
    text = message.text.strip()
    if text.startswith('/edit_template'):
        text = text[len('/edit_template'):].strip()
    
    if not text:
        CARDINAL.telegram.bot.send_message(
            message.chat.id,
            "❌ <b>Не указано имя шаблона</b>\n\n"
            "Используйте: <code>/edit_template ИМЯ</code>\n\n"
            "Доступные шаблоны можно посмотреть командой <code>/templates</code>",
            parse_mode="HTML"
        )
        return
    
    # Ищем шаблон
    if text in message_templates:
        template = message_templates[text]
        
        # Сохраняем состояние редактирования
        EDIT_TEMPLATE_STATES[message.chat.id] = {
            "template_name": text,
            "editing": True
        }
        
        CARDINAL.telegram.bot.send_message(
            message.chat.id,
            f"✏️ <b>Редактирование шаблона: {text}</b>\n\n"
            f"Текущий шаблон:\n\n"
            f"{template}\n\n"
            f"Отправьте новый текст шаблона в ответ на это сообщение.\n\n"
            f"Доступные переменные:\n"
            f"• {{login}} - логин аккаунта\n"
            f"• {{password}} - пароль аккаунта\n"
            f"• {{account_type}} - тип аккаунта\n"
            f"• {{duration_hours}} - срок аренды в часах\n"
            f"• {{end_time}} - дата и время окончания аренды\n"
            f"• {{username}} - имя пользователя\n"
            f"• {{order_id}} - ID заказа\n"
            f"• {{new_password}} - новый пароль (для уведомлений об окончании)",
            parse_mode="HTML"
        )
    else:
        CARDINAL.telegram.bot.send_message(
            message.chat.id,
            "❌ <b>Шаблон не найден</b>\n\n"
            f"Шаблон с именем '{text}' не существует.\n\n"
            "Доступные шаблоны можно посмотреть командой <code>/templates</code>",
            parse_mode="HTML"
        )

def handle_template_edit(message):
    """Обрабатывает ввод нового текста для шаблона"""
    # Проверяем, находится ли пользователь в процессе редактирования шаблона
    if message.chat.id in EDIT_TEMPLATE_STATES:
        if handle_template_edit(message):
            return True
    
    # Проверяем, находится ли пользователь в процессе добавления привязки
    if message.chat.id in ADD_BINDING_STATES:
        if handle_binding_add_steps(message):
            return True
    
    # Если не редактируем шаблон и не добавляем привязку, проверяем добавление аккаунта
    return handle_account_add_steps(message)

def reset_templates_cmd(message):
    """Сбрасывает все шаблоны к стандартным значениям"""
    # Проверяем права администратора
    if admin_id and message.chat.id != admin_id:
        CARDINAL.telegram.bot.send_message(
            message.chat.id,
            "❌ <b>Доступ запрещен</b>\n\n"
            "Только администратор может сбрасывать шаблоны сообщений.",
            parse_mode="HTML"
        )
        return
    
    # Создаем клавиатуру для подтверждения
    markup = InlineKeyboardMarkup()
    markup.row(
        InlineKeyboardButton("✅ Да, сбросить", callback_data="srent_reset_templates_confirm"),
        InlineKeyboardButton("❌ Отмена", callback_data="srent_reset_templates_cancel")
    )
    
    CARDINAL.telegram.bot.send_message(
        message.chat.id,
        "⚠️ <b>Сброс шаблонов сообщений</b>\n\n"
        "Вы уверены, что хотите сбросить все шаблоны сообщений к стандартным значениям?\n\n"
        "Это действие нельзя отменить.",
        reply_markup=markup,
        parse_mode="HTML"
    )

def reset_templates_confirm_callback(call):
    """Подтверждает сброс шаблонов к стандартным значениям"""
    global message_templates
    
    # Сбрасываем шаблоны
    message_templates = DEFAULT_TEMPLATES.copy()
    save_templates()
    
    CARDINAL.telegram.bot.edit_message_text(
        "✅ <b>Шаблоны сообщений сброшены</b>\n\n"
        "Все шаблоны сообщений сброшены к стандартным значениям.",
        call.message.chat.id,
        call.message.message_id,
        parse_mode="HTML"
    )
    
    CARDINAL.telegram.bot.answer_callback_query(call.id, "Шаблоны сброшены!")

def reset_templates_cancel_callback(call):
    """Отменяет сброс шаблонов"""
    CARDINAL.telegram.bot.edit_message_text(
        "❌ <b>Сброс шаблонов отменен</b>\n\n"
        "Шаблоны сообщений остались без изменений.",
        call.message.chat.id,
        call.message.message_id,
        parse_mode="HTML"
    )
    
    CARDINAL.telegram.bot.answer_callback_query(call.id, "Операция отменена")

# Отдельный поток для проверки истекших аренд
def check_rentals_thread():
    """Запускает проверку истекших аренд в отдельном потоке"""
    logger.info(f"{LOGGER_PREFIX} Запущен поток проверки истекших аренд")
    
    while True:
        try:
            if RUNNING:
                # Проверяем истекшие аренды
                expired_rentals = rental_manager.check_expired_rentals()
                
                # Обрабатываем истекшие аренды
                for rental, account, new_password in expired_rentals:
                    logger.info(f"{LOGGER_PREFIX} Аренда истекла: {rental.account_login} ({rental.username})")
                    
                    # Отправляем уведомление пользователю
                    try:
                        # Отправляем простое текстовое сообщение
                        message = "Срок аренды аккаунта Steam истек. Доступ прекращен, пароль изменен."
                        
                        if hasattr(CARDINAL, 'account') and hasattr(CARDINAL.account, 'send_message'):
                            # Получаем данные чата
                            chat_id = f"users-{rental.user_id}-{CARDINAL.account.id}"
                            interlocutor_id = rental.user_id
                            chat_name = f"Переписка с {rental.username}"
                            
                            # Используем низкоуровневый метод отправки сообщений
                            CARDINAL.account.send_message(chat_id, message, chat_name, interlocutor_id, None, True, False, False)
                            logger.info(f"{LOGGER_PREFIX} Сообщение об окончании аренды отправлено пользователю {rental.username}")
                        else:
                            logger.warning(f"{LOGGER_PREFIX} Невозможно отправить сообщение: методы отправки недоступны")
                    except Exception as e:
                        logger.error(f"{LOGGER_PREFIX} Ошибка отправки сообщения об окончании аренды: {e}")
                    
                    # Отправляем уведомление администратору
                    if admin_id:
                        try:
                            admin_message = format_message("admin_rental_end", 
                                username=rental.username,
                                login=rental.account_login,
                                account_type=account.type,
                                new_password=new_password
                            )
                            
                            CARDINAL.telegram.bot.send_message(
                                admin_id,
                                admin_message,
                                parse_mode="HTML"
                            )
                        except Exception as e:
                            logger.error(f"{LOGGER_PREFIX} Ошибка отправки уведомления администратору: {e}")
            
            # Пауза между проверками
            time.sleep(60)  # Проверяем каждую минуту
            
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка в потоке проверки аренд: {e}")
            time.sleep(60)  # В случае ошибки тоже ждем минуту

# Основная функция инициализации
def init_plugin(c):
    """Функция инициализации плагина"""
    global CARDINAL, RUNNING, AUTO_START
    CARDINAL = c
    
    logger.info(f"{LOGGER_PREFIX} Плагин инициализируется...")
    
    # Загружаем настройки
    load_config()
    load_lot_bindings()
    
    try:
        
        # Регистрация команд в Telegram
        c.add_telegram_commands(UUID, [
            ("srent_menu", "Меню аренды Steam", True),
        ])
        
        # Регистрация обработчиков команд
        c.telegram.msg_handler(show_menu, commands=["srent_menu"])
        c.telegram.msg_handler(add_account_cmd, commands=["srent_add"])
        c.telegram.msg_handler(interactive_add_account_start, commands=["steam_add"])
        c.telegram.msg_handler(list_accounts_cmd, commands=["steam_list", "srent_list"])
        c.telegram.msg_handler(list_rentals_cmd, commands=["steam_active"])
        c.telegram.msg_handler(start_rental_system, commands=["srent_start"])
        c.telegram.msg_handler(stop_rental_system, commands=["srent_stop"])
        c.telegram.msg_handler(force_return_account_cmd, commands=["srent_force"])
        c.telegram.msg_handler(manual_rent_account_cmd, commands=["srent_manual"])
        c.telegram.msg_handler(return_account_cmd, commands=["srent_return"])
        c.telegram.msg_handler(del_account_cmd, commands=["srent_del"])
        
        # Регистрация команд для привязки лотов
        c.telegram.msg_handler(unbind_lot_cmd, commands=["srent_unbind"])
        c.telegram.msg_handler(list_bindings_cmd, commands=["srent_bindings"])
        c.telegram.msg_handler(bind_lot_cmd, commands=["srent_bind"])
        c.telegram.msg_handler(help_lot_binding_cmd, commands=["srent_help"])
        
        # Регистрация команд для управления шаблонами
        c.telegram.msg_handler(list_templates_cmd, commands=["templates", "srent_templates"])
        c.telegram.msg_handler(view_template_cmd, commands=["view_template"])
        c.telegram.msg_handler(edit_template_cmd, commands=["edit_template"])
        c.telegram.msg_handler(reset_templates_cmd, commands=["reset_templates"])
        
        # Регистрация команды для установки admin_id
        c.telegram.msg_handler(set_admin_id_cmd, commands=["admin_id", "srent_admin"])
        
        # Регистрация обработчика текстовых сообщений
        c.telegram.msg_handler(handle_account_add_steps_and_template_edit, content_types=["text"])
        
        # Регистрация обработчика кнопки меню в клавиатуре
        c.telegram.msg_handler(show_menu, func=lambda message: message.text == "Меню💻" or message.text == "меню")
        
        # Создаем клавиатуру с кнопкой меню
        try:
            menu_kb = ReplyKeyboardMarkup(resize_keyboard=True)
            menu_kb.add(KeyboardButton("Меню💻"))
            
            # Получаем админ чаты из настроек кардинала или из нашей настройки
            admin_ids = []
            if admin_id:
                admin_ids = [admin_id]
            elif hasattr(c, "MAIN_CFG") and "telegram" in c.MAIN_CFG and "admin_id" in c.MAIN_CFG["telegram"]:
                admin_ids = [c.MAIN_CFG["telegram"]["admin_id"]]
                
            # Отправляем клавиатуру с меню админам
            for chat_id in admin_ids:
                try:
                    # Отправляем админу сообщение с клавиатурой
                    c.telegram.bot.send_message(
                        chat_id,
                        "🎮 <b>Система аренды Steam готова к использованию</b>\n\n"
                        "Используйте кнопку <b>Меню💻</b> для доступа к функциям.",
                        reply_markup=menu_kb,
                        parse_mode="HTML"
                    )
                except Exception as e:
                    logger.error(f"{LOGGER_PREFIX} Ошибка отправки клавиатуры меню админу {chat_id}: {e}")
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка создания клавиатуры меню: {e}")
        
        # Обработчики кнопок
        @c.telegram.bot.callback_query_handler(func=lambda call: call.data.startswith("srent_"))
        def handle_button_press(call):
            try:
                # Обработка особых callbacks
                if call.data.startswith("srent_set_admin_id_"):
                    set_admin_id_callback(call)
                    return
                elif call.data.startswith("srent_edit_template_"):
                    template_name = call.data.replace("srent_edit_template_", "")
                    edit_template_callback(call, template_name)
                    return
                elif call.data == "srent_reset_templates_confirm":
                    reset_templates_confirm_callback(call)
                    return
                elif call.data == "srent_reset_templates_cancel":
                    reset_templates_cancel_callback(call)
                    return
                elif call.data == "srent_list_templates":
                    list_templates_callback(call)
                    return
                elif call.data == "srent_cancel_add":
                    cancel_add_account_callback(call)
                    return
                
                # Особая обработка для привязок лотов
                if call.data == "srent_lot_bindings":
                    show_lot_bindings_callback(call)
                    return
                elif call.data.startswith("srent_binding_"):
                    # Новый формат: srent_binding_HASH
                    binding_hash = call.data.replace("srent_binding_", "")
                    manage_binding_callback(call, binding_hash)
                    return
                elif call.data == "srent_add_binding":
                    # Здесь будет вызов функции для добавления привязки
                    start_add_binding_callback(call)
                    return
                elif call.data == "srent_binding_help":
                    # Вызываем функцию help_lot_binding_cmd с сообщением из call
                    help_lot_binding_callback(call)
                    return
                elif call.data == "srent_all_bindings":
                    # Здесь будет вызов функции для просмотра всех привязок
                    show_all_bindings_callback(call)
                    return
                elif call.data == "srent_cancel_binding":
                    # Обработка отмены добавления привязки
                    cancel_binding_callback(call)
                    return
                elif call.data.startswith("srent_binding_duration_"):
                    # Обработка выбора длительности привязки
                    binding_duration_callback(call)
                    return
                elif call.data.startswith("srent_edit_binding_type_"):
                    # Обработка изменения типа привязки
                    binding_hash = call.data.replace("srent_edit_binding_type_", "")
                    edit_binding_type_callback(call, binding_hash)
                    return
                elif call.data.startswith("srent_edit_binding_time_"):
                    # Обработка изменения времени привязки
                    binding_hash = call.data.replace("srent_edit_binding_time_", "")
                    edit_binding_time_callback(call, binding_hash)
                    return
                elif call.data.startswith("srent_delete_binding_"):
                    # Обработка удаления привязки
                    binding_hash = call.data.replace("srent_delete_binding_", "")
                    delete_binding_callback(call, binding_hash)
                    return
                    
                # Стандартная обработка по первой части callback data
                action = call.data.split("_")[1] if len(call.data.split("_")) > 1 else ""
                
                if action == "menu":
                    show_menu_callback(call)
                elif action == "start":
                    start_rental_callback(call)
                elif action == "stop":
                    stop_rental_callback(call)
                elif action == "status":
                    show_status_callback(call)
                elif action == "accounts":
                    show_accounts_callback(call)
                elif action == "rentals":
                    show_rentals_callback(call)
                elif action == "add":
                    interactive_add_account_start_callback(call)
                elif action == "delete" and len(call.data.split("_")) > 2:
                    # Извлекаем логин аккаунта для удаления
                    login = call.data.split("_")[2]
                    delete_account_callback(call, login)
                elif action == "return":
                    show_return_account_callback(call)
                elif action == "show" and call.data == "srent_show_bindings":
                    list_bindings_cmd(call.message)
                elif action == "force" and call.data.startswith("srent_force_return_"):
                    login = call.data.replace("srent_force_return_", "")
                    force_return_account_from_callback(call, login)
                else:
                    CARDINAL.telegram.bot.answer_callback_query(call.id, "Неизвестное действие")
            except Exception as e:
                logger.error(f"{LOGGER_PREFIX} Ошибка в обработчике кнопок: {e}")
                try:
                    CARDINAL.telegram.bot.answer_callback_query(call.id, f"Ошибка: {str(e)[:50]}")
                except:
                    pass
        
        # Запускаем проверку истекших аренд в отдельном потоке
        check_thread = threading.Thread(target=check_rentals_thread, daemon=True)
        check_thread.start()
        
        # Автозапуск системы аренды если включено
        if AUTO_START:
            RUNNING = True
            logger.info(f"{LOGGER_PREFIX} Система аренды запущена автоматически")
        
        logger.info(f"{LOGGER_PREFIX} Плагин успешно инициализирован!")
        return True
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка при инициализации плагина: {e}")
        return False


# Обработчик текстовых сообщений для всех интерактивных процессов
def handle_account_add_steps_and_template_edit(message):
    """Обрабатывает шаги интерактивного добавления аккаунта и редактирования шаблонов"""
    # Проверяем, находится ли пользователь в процессе редактирования шаблона
    if message.chat.id in EDIT_TEMPLATE_STATES:
        if handle_template_edit(message):
            return True
    
    # Проверяем, находится ли пользователь в процессе добавления привязки
    if message.chat.id in ADD_BINDING_STATES:
        if handle_binding_add_steps(message):
            return True
    
    # Если не редактируем шаблон и не добавляем привязку, проверяем добавление аккаунта
    return handle_account_add_steps(message)

# Callback для редактирования шаблона
def edit_template_callback(call, template_name):
    """Обрабатывает callback для редактирования шаблона"""
    # Проверяем права администратора
    if admin_id and call.message.chat.id != admin_id:
        CARDINAL.telegram.bot.answer_callback_query(call.id, "Доступ запрещен")
        return
    
    # Проверяем существование шаблона
    if template_name not in message_templates:
        CARDINAL.telegram.bot.answer_callback_query(call.id, "Шаблон не найден")
        return
    
    template = message_templates[template_name]
    
    # Сохраняем состояние редактирования
    EDIT_TEMPLATE_STATES[call.message.chat.id] = {
        "template_name": template_name,
        "editing": True
    }
    
    # Определяем заголовок в зависимости от типа шаблона
    template_titles = {
        "rental_start": "Сообщение после оплаты",
        "rental_end": "Сообщение о завершении аренды",
        "rental_force_end": "Сообщение о досрочном завершении",
        "admin_rental_start": "Сообщение администратору о покупке",
        "admin_rental_end": "Сообщение администратору о завершении аренды"
    }
    
    title = template_titles.get(template_name, template_name)
    
    # Создаем кнопку отмены
    markup = InlineKeyboardMarkup()
    markup.row(InlineKeyboardButton("❌ ОТМЕНА", callback_data="srent_list_templates"))
    
    CARDINAL.telegram.bot.edit_message_text(
        f"✏️ <b>Редактирование шаблона: {title}</b>\n\n"
        f"Текущий шаблон:\n\n"
        f"{template}\n\n"
        f"Отправьте новый текст шаблона в ответ на это сообщение.\n\n"
        f"<b>Доступные переменные:</b>\n"
        f"• <code>{{{{'login'}}}}</code> - логин аккаунта\n"
        f"• <code>{{{{'password'}}}}</code> - пароль аккаунта\n"
        f"• <code>{{{{'account_type'}}}}</code> - тип аккаунта\n"
        f"• <code>{{{{'duration_hours'}}}}</code> - срок аренды в часах\n"
        f"• <code>{{{{'end_time'}}}}</code> - дата и время окончания аренды\n"
        f"• <code>{{{{'username'}}}}</code> - имя пользователя\n"
        f"• <code>{{{{'order_id'}}}}</code> - ID заказа\n"
        f"• <code>{{{{'new_password'}}}}</code> - новый пароль (для уведомлений об окончании)",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=markup,
        parse_mode="HTML"
    )
    
    CARDINAL.telegram.bot.answer_callback_query(call.id, "Отправьте новый текст шаблона")

# Callback для списка шаблонов
def list_templates_callback(call):
    """Обрабатывает callback для отображения списка шаблонов"""
    # Проверяем права администратора
    if admin_id and call.message.chat.id != admin_id:
        CARDINAL.telegram.bot.answer_callback_query(call.id, "Доступ запрещен")
        return
    
    if not message_templates:
        CARDINAL.telegram.bot.edit_message_text(
            "ℹ️ <b>Шаблоны сообщений</b>\n\n"
            "В данный момент не настроено ни одного шаблона.",
            call.message.chat.id,
            call.message.message_id,
            parse_mode="HTML"
        )
        CARDINAL.telegram.bot.answer_callback_query(call.id, "Нет шаблонов")
        return
    
    # Формируем список шаблонов с красивыми кнопками
    templates_text = "✏️ <b>ТЕКСТ ШАБЛОНОВ</b>\n\n"
    templates_text += "━━━━━━━━━━━━━━━━━━━━━━\n"
    templates_text += "Доступные шаблоны сообщений:\n"
    
    # Создаем удобные кнопки для редактирования шаблонов
    markup = InlineKeyboardMarkup(row_width=1)
    
    # Добавляем кнопки для стандартных шаблонов
    markup.row(InlineKeyboardButton("Сообщение после оплаты ✅", callback_data="srent_edit_template_rental_start"))
    markup.row(InlineKeyboardButton("Сообщение о завершении аренды 🍉", callback_data="srent_edit_template_rental_end"))
    markup.row(InlineKeyboardButton("Сообщение о досрочном завершении ⚠️", callback_data="srent_edit_template_rental_force_end"))
    markup.row(InlineKeyboardButton("Сообщение администратору о покупке 🛒", callback_data="srent_edit_template_admin_rental_start"))
    markup.row(InlineKeyboardButton("Сообщение администратору о завершении аренды 🕸️", callback_data="srent_edit_template_admin_rental_end"))
    
    # Добавляем кнопки для нестандартных шаблонов (если есть)
    for name in message_templates:
        if name not in DEFAULT_TEMPLATES:
            markup.row(InlineKeyboardButton(f"✏️ {name}", callback_data=f"srent_edit_template_{name}"))
    
    # Добавляем кнопки для сброса шаблонов и возврата в меню
    markup.row(InlineKeyboardButton("🔄 Сбросить шаблоны", callback_data="srent_reset_templates_confirm"))
    markup.row(InlineKeyboardButton("❌ Отмена", callback_data="srent_menu"))
    
    CARDINAL.telegram.bot.edit_message_text(
        templates_text,
        call.message.chat.id,
        call.message.message_id,
        reply_markup=markup,
        parse_mode="HTML"
    )
    
    CARDINAL.telegram.bot.answer_callback_query(call.id, "Список шаблонов")

# Функции для API
def start_rent_plugin():
    """Функция запуска плагина через API"""
    global RUNNING
    if not RUNNING:
        RUNNING = True
        logger.info(f"{LOGGER_PREFIX} Система аренды запущена через API")
        return True, "Система аренды успешно запущена"
    return False, "Система аренды уже запущена"

def stop_rent_plugin():
    """Функция остановки плагина через API"""
    global RUNNING
    if RUNNING:
        RUNNING = False
        logger.info(f"{LOGGER_PREFIX} Система аренды остановлена через API")
        return True, "Система аренды успешно остановлена"
    return False, "Система аренды уже остановлена"

def add_steam_account(login, password, account_type="standard", api_key=None):
    """Добавляет новый аккаунт через API"""
    success, message = rental_manager.add_account(login, password, account_type, api_key)
    return {"success": success, "message": message}

def check_rentals():
    """Проверяет состояние всех аренд"""
    if not RUNNING:
        return False, "Система аренды не запущена"
    
    try:
        expired_rentals = rental_manager.check_expired_rentals()
        return True, {
            "expired": len(expired_rentals),
            "active": sum(1 for rent in rental_manager.rentals.values() if rent.is_active),
            "total_accounts": len(rental_manager.accounts),
            "available_accounts": sum(1 for acc in rental_manager.accounts.values() if acc.status == "available")
        }
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка при проверке аренд: {e}")
        return False, f"Ошибка: {e}"

def delete_steam_account(login):
    """Удаляет аккаунт через API"""
    success, message = rental_manager.remove_account(login)
    return {"success": success, "message": message}

def set_auto_start(enabled):
    """Устанавливает настройку автозапуска"""
    global AUTO_START
    try:
        AUTO_START = bool(enabled)
        # Сохраняем настройку в файл конфигурации
        config_file = os.path.join(DATA_DIR, "config.json")
        with open(config_file, "w", encoding="utf-8") as f:
            json.dump({"auto_start": AUTO_START}, f)
        return {"success": True, "message": "Настройка сохранена"}
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка сохранения настройки автозапуска: {e}")
        return {"success": False, "message": f"Ошибка: {e}"}

def message_handler(c, event, *args):
    """Обработчик входящих сообщений"""
    if not RUNNING:
        return
    
    if not hasattr(event, "message") or not event.message:
        return
        
    # Сообщение от себя не обрабатываем
    if event.message.author_id == c.account.id:
        return
        
    message = event.message
    username = message.author
    user_id = message.author_id
    text = message.text
    
    logger.info(f"{LOGGER_PREFIX} Получено сообщение от {username}: {text}")
    
    # Здесь можно добавить обработку команд из сообщений
    # Например, команда для получения информации о текущей аренде
    
    # В данной реализации не требуется особой обработки сообщений,
    # основная логика выполняется через Telegram-команды

def order_handler(c, event, *args):
    """Обработчик новых заказов"""
    if not RUNNING:
        return
    
    # Проверяем, что у нас есть данные заказа
    if not hasattr(event, "order") or not event.order:
        return
    
    order = event.order
    logger.info(f"{LOGGER_PREFIX} Получен новый заказ: {order.id}")
    
    # Отладочный вывод для диагностики
    logger.info(f"{LOGGER_PREFIX} Отладка заказа: {vars(order)}")
    
    # Получаем описание лота
    lot_description = None
    if hasattr(order, 'description'):
        full_description = order.description
        logger.info(f"{LOGGER_PREFIX} Полное описание лота: {full_description}")
        
        # Извлекаем только название лота, отбрасывая категорию
        # Формат: "Название лота, Категория, Подкатегория"
        parts = full_description.split(',', 1)
        lot_name = parts[0].strip()
        logger.info(f"{LOGGER_PREFIX} Извлеченное название лота: {lot_name}")
    else:
        logger.info(f"{LOGGER_PREFIX} Не удалось получить описание лота")
        return

    # Ищем точное совпадение по названию лота
    matching_binding = None
    matching_name = None
    
    # Проверяем точное совпадение
    if lot_name in lot_bindings:
        matching_binding = lot_bindings[lot_name]
        matching_name = lot_name
        logger.info(f"{LOGGER_PREFIX} Найдено точное совпадение: '{lot_name}'")
    
    # Если не нашли подходящую привязку - выходим
    if not matching_binding:
        logger.info(f"{LOGGER_PREFIX} Не найдено привязки для заказа {order.id} с названием: {lot_name}")
        return
    
    # Получаем тип аккаунта и длительность аренды
    account_type = matching_binding["account_type"]
    duration_hours = matching_binding["duration_hours"]
    
    logger.info(f"{LOGGER_PREFIX} Используем привязку '{matching_name}': тип={account_type}, часы={duration_hours}")
    
    # Получаем данные пользователя
    user_id = None
    username = None
    
    if hasattr(order, 'buyer_id'):
        user_id = order.buyer_id
    if hasattr(order, 'buyer') and hasattr(order.buyer, 'username'):
        username = order.buyer.username
    elif hasattr(order, 'buyer_username'):
        username = order.buyer_username
    elif hasattr(order, 'buyer_name'):
        username = order.buyer_name
    
    # Проверяем, все ли необходимые данные получены
    if not user_id or not username:
        try:
            logger.error(f"{LOGGER_PREFIX} Не удалось получить данные покупателя для заказа {order.id}")
        except Exception:
            pass
        return
    
    # Выбираем аккаунт нужного типа
    account = rental_manager.get_account_by_type(account_type)
    
    if not account:
        try:
            logger.error(f"{LOGGER_PREFIX} Нет доступных аккаунтов типа {account_type}")
        except Exception:
            pass
        
        # Отправляем уведомление администратору об отсутствии доступных аккаунтов
        if CARDINAL and hasattr(CARDINAL, "MAIN_CFG") and "telegram" in CARDINAL.MAIN_CFG and "admin_id" in CARDINAL.MAIN_CFG["telegram"]:
            admin_id = CARDINAL.MAIN_CFG["telegram"]["admin_id"]
            try:
                error_message = f"⚠️ <b>Ошибка аренды аккаунта</b>\n\n" \
                               f"Заказ: <code>#{order.id}</code>\n" \
                               f"Покупатель: <b>{username}</b>\n" \
                               f"Требуемый тип: <code>{account_type}</code>\n\n" \
                               f"<b>Нет доступных аккаунтов указанного типа!</b>"
                CARDINAL.telegram.bot.send_message(admin_id, error_message, parse_mode="HTML")
            except:
                pass
        
        return
    
    # Арендуем аккаунт
    success, message_text, account, rental = rental_manager.rent_account(
        user_id, username, duration_hours, account_type, order.id
    )
    
    if not success:
        try:
            logger.error(f"{LOGGER_PREFIX} Не удалось арендовать аккаунт: {message_text}")
        except Exception:
            pass
            
        # Отправляем уведомление администратору о проблеме
        if CARDINAL and hasattr(CARDINAL, "MAIN_CFG") and "telegram" in CARDINAL.MAIN_CFG and "admin_id" in CARDINAL.MAIN_CFG["telegram"]:
            admin_id = CARDINAL.MAIN_CFG["telegram"]["admin_id"]
            try:
                error_message = f"⚠️ <b>Ошибка аренды аккаунта</b>\n\n" \
                               f"Заказ: <code>#{order.id}</code>\n" \
                               f"Покупатель: <b>{username}</b>\n" \
                               f"Требуемый тип: <code>{account_type}</code>\n\n" \
                               f"<b>Ошибка:</b> {message_text}"
                CARDINAL.telegram.bot.send_message(admin_id, error_message, parse_mode="HTML")
            except:
                pass
        
        return
    
    # Формируем информацию об аренде
    end_time_str = datetime.fromtimestamp(rental.end_time).strftime("%d.%m.%Y %H:%M")
    
    # Используем шаблон сообщения вместо жестко закодированного текста
    message = format_message("rental_start", 
        login=account.login,
        password=account.password,
        account_type=account.type,
        duration_hours=duration_hours,
        end_time=end_time_str,
        username=username,
        order_id=order.id
    )
    
    # Отправляем сообщение максимально простым способом
    try:
        # Прямая отправка сообщения через FunPay
        if hasattr(c, 'account') and hasattr(c.account, 'send_message'):
            # Получаем данные чата
            chat_id = f"users-{user_id}-{c.account.id}"
            interlocutor_id = user_id
            chat_name = f"Переписка с {username}"
            
            # Используем низкоуровневый метод отправки сообщений
            c.account.send_message(chat_id, message, chat_name, interlocutor_id, None, True, False, False)
            logger.info(f"{LOGGER_PREFIX} Сообщение отправлено пользователю {username}")
        else:
            logger.warning(f"{LOGGER_PREFIX} Невозможно отправить сообщение: методы отправки недоступны")
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка при отправке сообщения: {e}")
        
        # Отправляем информацию администратору
        if CARDINAL and hasattr(CARDINAL, "MAIN_CFG") and "telegram" in CARDINAL.MAIN_CFG and "admin_id" in CARDINAL.MAIN_CFG["telegram"]:
            admin_id = CARDINAL.MAIN_CFG["telegram"]["admin_id"]
            try:
                admin_message = f"⚠️ <b>Аккаунт выдан (ошибка отправки сообщения)</b>\n\n" \
                              f"Заказ: <code>#{order.id}</code>\n" \
                              f"Покупатель: <b>{username}</b>\n" \
                              f"Аккаунт: <code>{account.login}</code>\n" \
                              f"Пароль: <code>{account.password}</code>\n" \
                              f"Тип: <code>{account.type}</code>\n" \
                              f"Срок: <code>{duration_hours} ч.</code>\n\n" \
                              f"<b>Ошибка:</b> {str(e)}"
                CARDINAL.telegram.bot.send_message(admin_id, admin_message, parse_mode="HTML")
            except:
                pass
    
    # Отправляем подтверждение администратору
    if CARDINAL and hasattr(CARDINAL, "MAIN_CFG") and "telegram" in CARDINAL.MAIN_CFG and "admin_id" in CARDINAL.MAIN_CFG["telegram"]:
        admin_id = CARDINAL.MAIN_CFG["telegram"]["admin_id"]
        try:
            admin_message = f"✅ <b>Аккаунт выдан</b>\n\n" \
                          f"Заказ: <code>#{order.id}</code>\n" \
                          f"Покупатель: <b>{username}</b>\n" \
                          f"Аккаунт: <code>{account.login}</code>\n" \
                          f"Пароль: <code>{account.password}</code>\n" \
                          f"Тип: <code>{account.type}</code>\n" \
                          f"Срок: <code>{duration_hours} ч.</code>"
            CARDINAL.telegram.bot.send_message(admin_id, admin_message, parse_mode="HTML")
        except:
            pass

# Глобальные привязки (обязательные)
BIND_TO_PRE_INIT = [init_plugin]
BIND_TO_NEW_MESSAGE = [message_handler]
BIND_TO_NEW_ORDER = [order_handler]  # Исправлено
BIND_TO_DELETE = []
BIND_TO_API = {
    "start_rent_plugin": start_rent_plugin, 
    "stop_rent_plugin": stop_rent_plugin,
    "add_steam_account": add_steam_account,
    "check_rentals": check_rentals,
    "delete_steam_account": delete_steam_account,
    "set_auto_start": set_auto_start
}

# Интерактивное добавление аккаунта
def interactive_add_account_start(message):
    """Начинает процесс интерактивного добавления аккаунта"""
    try:
        chat_id = message.chat.id
        ADD_ACCOUNT_STATES[chat_id] = {"state": "login", "data": {}}
        
        # Создаем клавиатуру для отмены
        markup = InlineKeyboardMarkup()
        markup.row(InlineKeyboardButton("❌ Отмена", callback_data="srent_cancel_add"))
        
        CARDINAL.telegram.bot.send_message(
            chat_id,
            "🎮 <b>Добавление аккаунта Steam</b>\n\n"
            "Пожалуйста, отправьте <b>логин</b> аккаунта.\n\n"
            "Вы можете отменить процесс добавления, написав <code>отмена</code> или нажав кнопку отмены.",
            reply_markup=markup,
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка в обработчике interactive_add_account_start: {e}")
        try:
            CARDINAL.telegram.bot.send_message(message.chat.id, f"❌ Произошла ошибка: {e}")
        except:
            pass

def interactive_add_account_start_callback(call):
    """Начинает процесс интерактивного добавления аккаунта (callback)"""
    try:
        chat_id = call.message.chat.id
        ADD_ACCOUNT_STATES[chat_id] = {"state": "login", "data": {}}
        
        # Создаем клавиатуру для отмены
        markup = InlineKeyboardMarkup()
        markup.row(InlineKeyboardButton("❌ Отмена", callback_data="srent_cancel_add"))
        
        CARDINAL.telegram.bot.edit_message_text(
            "🎮 <b>Добавление аккаунта Steam</b>\n\n"
            "Пожалуйста, отправьте <b>логин</b> аккаунта.\n\n"
            "Вы можете отменить процесс добавления, написав <code>отмена</code> или нажав кнопку отмены.",
            chat_id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode="HTML"
        )
        CARDINAL.telegram.bot.answer_callback_query(call.id, "Введите логин аккаунта")
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка в обработчике interactive_add_account_start_callback: {e}")
        try:
            CARDINAL.telegram.bot.answer_callback_query(call.id, f"Ошибка: {str(e)[:50]}")
        except:
            pass

# Добавим новый обработчик для кнопки отмены
def cancel_add_account_callback(call):
    """Отменяет процесс добавления аккаунта"""
    try:
        chat_id = call.message.chat.id
        
        # Проверяем, находится ли пользователь в процессе добавления аккаунта
        if chat_id in ADD_ACCOUNT_STATES:
            del ADD_ACCOUNT_STATES[chat_id]
        
        # Создаем клавиатуру для возврата в меню
        markup = InlineKeyboardMarkup()
        markup.row(InlineKeyboardButton("В меню 💻", callback_data="srent_menu"))
        
        CARDINAL.telegram.bot.edit_message_text(
            "❌ <b>Процесс добавления аккаунта отменен.</b>\n\n"
            "Вы можете вернуться в меню или запустить процесс заново.",
            chat_id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode="HTML"
        )
        CARDINAL.telegram.bot.answer_callback_query(call.id, "Добавление отменено")
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка в обработчике cancel_add_account_callback: {e}")
        try:
            CARDINAL.telegram.bot.answer_callback_query(call.id, f"Ошибка: {str(e)[:50]}")
        except:
            pass

def handle_account_add_steps(message):
    """Обрабатывает шаги интерактивного добавления аккаунта"""
    try:
        chat_id = message.chat.id
        
        # Проверяем, находится ли пользователь в процессе добавления аккаунта
        if chat_id not in ADD_ACCOUNT_STATES:
            return
        
        # Получаем текущее состояние и данные
        state = ADD_ACCOUNT_STATES[chat_id]["state"]
        data = ADD_ACCOUNT_STATES[chat_id]["data"]
        
        # Проверяем запрос на отмену
        if message.text.lower() in ["отмена", "cancel", "/cancel", "/отмена"]:
            del ADD_ACCOUNT_STATES[chat_id]
            CARDINAL.telegram.bot.send_message(
                chat_id,
                "❌ Процесс добавления аккаунта отменен.",
                parse_mode="HTML"
            )
            return
        
        if state == "login":
            # Получаем логин
            login = message.text.strip()
            
            # Проверяем, не существует ли уже такого аккаунта
            if login in rental_manager.accounts:
                CARDINAL.telegram.bot.send_message(
                    chat_id,
                    "❌ Аккаунт с таким логином уже существует. Пожалуйста, введите другой логин.\n\nВы можете отменить добавление, написав <code>отмена</code>",
                    parse_mode="HTML"
                )
                return
            
            data["login"] = login
            ADD_ACCOUNT_STATES[chat_id]["state"] = "password"
            
            CARDINAL.telegram.bot.send_message(
                chat_id,
                "✅ Логин сохранен.\n\n"
                "Теперь отправьте <b>пароль</b> аккаунта.\n\n"
                "Вы можете отменить добавление, написав <code>отмена</code>",
                parse_mode="HTML"
            )
        
        elif state == "password":
            # Получаем пароль
            password = message.text.strip()
            data["password"] = password
            ADD_ACCOUNT_STATES[chat_id]["state"] = "type"
            
            CARDINAL.telegram.bot.send_message(
                chat_id,
                "✅ Пароль сохранен.\n\n"
                "Теперь укажите <b>тип</b> аккаунта (например: standard, games, premium, repo).\n"
                "Или отправьте слово <code>standard</code>, если нет особого типа.\n\n"
                "Вы можете отменить добавление, написав <code>отмена</code>",
                parse_mode="HTML"
            )
        
        elif state == "type":
            # Получаем тип
            account_type = message.text.strip().lower()
            data["type"] = account_type
            ADD_ACCOUNT_STATES[chat_id]["state"] = "api_key"
            
            CARDINAL.telegram.bot.send_message(
                chat_id,
                "✅ Тип аккаунта сохранен.\n\n"
                "Теперь укажите <b>API ключ</b> для управления аккаунтом (смены пароля и завершения сессий).\n"
                "Если у вас нет API ключа, отправьте <code>нет</code> или <code>-</code>.\n\n"
                "Вы можете отменить добавление, написав <code>отмена</code>",
                parse_mode="HTML"
            )
            
        elif state == "api_key":
            # Получаем API ключ
            api_key = message.text.strip()
            
            # Если пользователь не указал API ключ
            if api_key.lower() in ["нет", "-", "no", "none"]:
                api_key = None
            
            data["api_key"] = api_key
            
            # Добавляем аккаунт
            success, message_text = rental_manager.add_account(
                data["login"], 
                data["password"], 
                data["type"],
                api_key
            )
            
            # Удаляем состояние
            del ADD_ACCOUNT_STATES[chat_id]
            
            # Формируем клавиатуру для перехода в меню
            markup = InlineKeyboardMarkup()
            markup.row(InlineKeyboardButton("В меню 💻", callback_data="srent_menu"))
            markup.row(InlineKeyboardButton("Добавить еще аккаунт ✅", callback_data="srent_add"))
            
            if success:
                CARDINAL.telegram.bot.send_message(
                    chat_id,
                    f"✅ {message_text}\n\n"
                    f"Логин: <code>{data['login']}</code>\n"
                    f"Тип: <code>{data['type']}</code>\n"
                    f"API ключ: {'✅ Установлен' if api_key else '❌ Отсутствует'}",
                    reply_markup=markup,
                    parse_mode="HTML"
                )
            else:
                CARDINAL.telegram.bot.send_message(
                    chat_id,
                    f"❌ {message_text}",
                    reply_markup=markup,
                    parse_mode="HTML"
                )
    
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка в обработчике handle_account_add_steps: {e}")
        try:
            CARDINAL.telegram.bot.send_message(message.chat.id, f"❌ Произошла ошибка: {e}")
        except:
            pass
        
        # В случае ошибки удаляем состояние
        if chat_id in ADD_ACCOUNT_STATES:
            del ADD_ACCOUNT_STATES[chat_id]

# Функции меню и интерфейса
def show_menu(message):
    """Показывает главное меню плагина"""
    try:
        # Создаем клавиатуру с кнопками
        markup = InlineKeyboardMarkup(row_width=2)
        
        # Статус-секция
        status_emoji = "✅" if RUNNING else "❌"
        available_count = sum(1 for acc in rental_manager.accounts.values() if acc.status == "available")
        rented_count = sum(1 for acc in rental_manager.accounts.values() if acc.status == "rented")
        
        # Верхние кнопки управления - самые важные
        if RUNNING:
            markup.row(InlineKeyboardButton("🔴 Остановить аренду", callback_data="srent_stop"))
        else:
            markup.row(InlineKeyboardButton("🟢 Запустить аренду", callback_data="srent_start"))
        
        # Раздел аккаунтов и добавление
        markup.row(
            InlineKeyboardButton("🕹️ Аккаунты", callback_data="srent_accounts"),
            InlineKeyboardButton("➕ Добавить", callback_data="srent_add")
        )
        
        # Раздел привязок и статистики
        markup.row(
            InlineKeyboardButton("🛜 Привязки", callback_data="srent_lot_bindings"),
            InlineKeyboardButton("📊 Статистика", callback_data="srent_status")
        )
        
        # Раздел возврата и шаблонов
        markup.row(
            InlineKeyboardButton("🛞 Возврат аккаунта", callback_data="srent_return"),
            InlineKeyboardButton("✏️ Текст шаблонов", callback_data="srent_list_templates")
        )
        
        # Создаем красивый вывод статуса
        header = "🎮 Система аренды аккаунтов Steam"
        status_line = f"\nСтатус: {status_emoji} {'АКТИВНА' if RUNNING else 'ОСТАНОВЛЕНА'}"
        
        # Добавляем список аккаунтов по типам
        accounts_text = "\n\n📝 Список аккаунтов по типам:\n\n"
        
        # Группируем аккаунты по типам
        accounts_by_type = {}
        for acc in rental_manager.accounts.values():
            if acc.type not in accounts_by_type:
                accounts_by_type[acc.type] = []
            accounts_by_type[acc.type].append(acc)
        
        # Формируем список аккаунтов по типам
        for acc_type, accs in accounts_by_type.items():
            accounts_text += f"🔹 {acc_type.upper()} ({len(accs)} шт.):\n"
            for acc in accs:
                status = "✅ Доступен" if acc.status == "available" else "❌ В аренде"
                accounts_text += f"  • {acc.login} - {status} \n"
            accounts_text += "\n"
        
        # Форматируем сообщение
        message_text = f"{header}{status_line}\n\nВыберите действие из меню ниже:"
        
        # Добавляем список аккаунтов, если есть аккаунты
        if rental_manager.accounts:
            message_text += accounts_text
        
        # Добавляем кнопки для действий внизу сообщения
        # Только одна кнопка для обновления и добавления аккаунта
        markup.row(
            InlineKeyboardButton("Обновить 🆙", callback_data="srent_menu"),
        )
        
        CARDINAL.telegram.bot.send_message(
            message.chat.id,
            message_text,
            reply_markup=markup,
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка отображения меню: {e}")
        try:
            CARDINAL.telegram.bot.send_message(message.chat.id, f"❌ Ошибка отображения меню: {e}")
        except:
            pass

def show_menu_callback(call):
    """Обработчик кнопки меню"""
    try:
        # Создаем клавиатуру с кнопками
        markup = InlineKeyboardMarkup(row_width=2)
        
        # Статус-секция
        status_emoji = "✅" if RUNNING else "❌"
        available_count = sum(1 for acc in rental_manager.accounts.values() if acc.status == "available")
        rented_count = sum(1 for acc in rental_manager.accounts.values() if acc.status == "rented")
        
        # Верхние кнопки управления - самые важные
        if RUNNING:
            markup.row(InlineKeyboardButton("🔴 Остановить аренду", callback_data="srent_stop"))
        else:
            markup.row(InlineKeyboardButton("🟢 Запустить аренду", callback_data="srent_start"))
        
        # Раздел аккаунтов и добавление
        markup.row(
            InlineKeyboardButton("🕹️ Аккаунты", callback_data="srent_accounts"),
            InlineKeyboardButton("➕ Добавить", callback_data="srent_add")
        )
        
        # Раздел привязок и статистики
        markup.row(
            InlineKeyboardButton("🛜 Привязки", callback_data="srent_lot_bindings"),
            InlineKeyboardButton("📊 Статистика", callback_data="srent_status")
        )
        
        # Раздел возврата и шаблонов
        markup.row(
            InlineKeyboardButton("🛞 Возврат аккаунта", callback_data="srent_return"),
            InlineKeyboardButton("✏️ Текст шаблонов", callback_data="srent_list_templates")
        )
        
        # Создаем красивый вывод статуса
        header = "🎮 Система аренды аккаунтов Steam"
        status_line = f"\nСтатус: {status_emoji} {'АКТИВНА' if RUNNING else 'ОСТАНОВЛЕНА'}"
        
        # Добавляем список аккаунтов по типам
        accounts_text = "\n\n📝 Список аккаунтов по типам:\n\n"
        
        # Группируем аккаунты по типам
        accounts_by_type = {}
        for acc in rental_manager.accounts.values():
            if acc.type not in accounts_by_type:
                accounts_by_type[acc.type] = []
            accounts_by_type[acc.type].append(acc)
        
        # Формируем список аккаунтов по типам
        for acc_type, accs in accounts_by_type.items():
            accounts_text += f"🔹 {acc_type.upper()} ({len(accs)} шт.):\n"
            for acc in accs:
                status = "✅ Доступен" if acc.status == "available" else "❌ В аренде"
                accounts_text += f"  • {acc.login} - {status} \n"
            accounts_text += "\n"
        
        # Форматируем сообщение
        message_text = f"{header}{status_line}\n\nВыберите действие из меню ниже:"
        
        # Добавляем список аккаунтов, если есть аккаунты
        if rental_manager.accounts:
            message_text += accounts_text
        
        # Добавляем кнопки для действий внизу сообщения
        markup.row(
            InlineKeyboardButton("Обновить 🆙", callback_data="srent_menu"),
            InlineKeyboardButton("Добавить аккаунт ✅", callback_data="srent_add")
        )
        
        try:
            CARDINAL.telegram.bot.edit_message_text(
                message_text,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=markup,
                parse_mode="HTML"
            )
            CARDINAL.telegram.bot.answer_callback_query(call.id, "Меню обновлено")
        except Exception as edit_error:
            # Если сообщение не изменилось, просто уведомляем пользователя
            if "message is not modified" in str(edit_error):
                CARDINAL.telegram.bot.answer_callback_query(call.id, "Меню актуально")
            else:
                # Если другая ошибка - перебрасываем исключение
                raise edit_error
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка обновления меню: {e}")
        try:
            CARDINAL.telegram.bot.answer_callback_query(call.id, f"Ошибка: {str(e)[:50]}")
        except:
            pass

def start_rental_callback(call):
    """Обработчик кнопки запуска системы"""
    global RUNNING
    try:
        if RUNNING:
            CARDINAL.telegram.bot.answer_callback_query(call.id, "Система уже запущена!")
            return
            
        RUNNING = True
        logger.info(f"{LOGGER_PREFIX} Система аренды запущена через кнопку меню")
        
        # Обновляем сообщение меню
        markup = InlineKeyboardMarkup()
        markup.row(InlineKeyboardButton("« Назад в меню", callback_data="srent_menu"))
        
        CARDINAL.telegram.bot.edit_message_text(
            "✅ <b>Система аренды успешно запущена!</b>\n\n"
            "Теперь бот будет автоматически выдавать аккаунты при покупке соответствующих лотов.",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode="HTML"
        )
        CARDINAL.telegram.bot.answer_callback_query(call.id, "Система запущена!")
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка запуска системы аренды: {e}")
        try:
            CARDINAL.telegram.bot.answer_callback_query(call.id, f"Ошибка: {str(e)[:50]}")
        except:
            pass

def stop_rental_callback(call):
    """Обработчик кнопки остановки системы"""
    global RUNNING
    try:
        if not RUNNING:
            CARDINAL.telegram.bot.answer_callback_query(call.id, "Система уже остановлена!")
            return
            
        RUNNING = False
        logger.info(f"{LOGGER_PREFIX} Система аренды остановлена через кнопку меню")
        
        # Обновляем сообщение меню
        markup = InlineKeyboardMarkup()
        markup.row(InlineKeyboardButton("« Назад в меню", callback_data="srent_menu"))
        
        CARDINAL.telegram.bot.edit_message_text(
            "🛑 <b>Система аренды остановлена!</b>\n\n"
            "Автоматическая выдача аккаунтов при покупке лотов отключена.\n"
            "Активные аренды продолжат работать до завершения.",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode="HTML"
        )
        CARDINAL.telegram.bot.answer_callback_query(call.id, "Система остановлена!")
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка остановки системы аренды: {e}")
        try:
            CARDINAL.telegram.bot.answer_callback_query(call.id, f"Ошибка: {str(e)[:50]}")
        except:
            pass

def show_status_callback(call):
    """Показывает текущий статус системы аренды"""
    try:
        # Считаем статистику
        total_accounts = len(rental_manager.accounts)
        available_accounts = sum(1 for acc in rental_manager.accounts.values() if acc.status == 'available')
        rented_accounts = sum(1 for acc in rental_manager.accounts.values() if acc.status == 'rented')
        disabled_accounts = sum(1 for acc in rental_manager.accounts.values() if acc.status == 'disabled')
        
        active_rentals = sum(1 for rent in rental_manager.rentals.values() if rent.is_active)
        total_rentals = len(rental_manager.rentals)
        
        # Общая информация о состоянии
        status_emoji = "🟢" if RUNNING else "🔴"
        
        # Создаем сообщение статистики
        status_text = "📊 <b>СТАТИСТИКА СИСТЕМЫ</b> 📊\n\n"
        status_text += f"{'='*30}\n\n"
        
        # Общий статус
        status_text += f"<b>СТАТУС СИСТЕМЫ:</b> {status_emoji} <b>{'АКТИВНА' if RUNNING else 'ОСТАНОВЛЕНА'}</b>\n\n"
        status_text += f"{'='*30}\n\n"
        
        # Секция аккаунтов
        status_text += "🖥️ <b>АККАУНТЫ</b>\n\n"
        
        # Добавляем график в виде прогресс-бара для наглядности
        if total_accounts > 0:
            available_percent = int((available_accounts / total_accounts) * 10)
            rented_percent = int((rented_accounts / total_accounts) * 10)
            disabled_percent = max(0, 10 - available_percent - rented_percent)
            
            progress_bar = "🟢" * available_percent + "🔴" * rented_percent + "⚫" * disabled_percent
            status_text += f"{progress_bar}\n\n"
        
        status_text += f"• <b>Всего аккаунтов:</b> {total_accounts}\n"
        status_text += f"• <b>Доступно:</b> {available_accounts} ({int(available_accounts/total_accounts*100) if total_accounts else 0}%)\n"
        status_text += f"• <b>В аренде:</b> {rented_accounts} ({int(rented_accounts/total_accounts*100) if total_accounts else 0}%)\n"
        status_text += f"• <b>Отключено:</b> {disabled_accounts} ({int(disabled_accounts/total_accounts*100) if total_accounts else 0}%)\n\n"
        
        # Секция типов аккаунтов
        if total_accounts > 0:
            status_text += "<b>📋 ПО ТИПАМ</b>\n\n"
            
            # Группируем аккаунты по типам
            accounts_by_type = {}
            for acc in rental_manager.accounts.values():
                if acc.type not in accounts_by_type:
                    accounts_by_type[acc.type] = {"total": 0, "available": 0, "rented": 0}
                
                accounts_by_type[acc.type]["total"] += 1
                if acc.status == "available":
                    accounts_by_type[acc.type]["available"] += 1
                elif acc.status == "rented":
                    accounts_by_type[acc.type]["rented"] += 1
            
            # Выводим статистику по каждому типу
            for acc_type, stats in accounts_by_type.items():
                status_text += f"• <b>{acc_type.upper()}</b>: {stats['total']} шт. "
                status_text += f"(🟢 {stats['available']} | 🔴 {stats['rented']})\n"
            
            status_text += "\n"
        
        status_text += f"{'='*30}\n\n"
        
        # Секция аренд
        status_text += "⏰ <b>АРЕНДЫ</b>\n\n"
        status_text += f"• <b>Активных аренд:</b> {active_rentals}\n"
        status_text += f"• <b>Всего выдано:</b> {total_rentals}\n"
        status_text += f"• <b>Завершено:</b> {total_rentals - active_rentals}\n\n"
        
        # Добавляем информацию о ближайших истечениях срока аренды, если есть активные аренды
        if active_rentals > 0:
            status_text += "<b>🔄 БЛИЖАЙШИЕ ИСТЕЧЕНИЯ</b>\n\n"
            
            # Сортируем аренды по оставшемуся времени
            active_rental_objects = [r for r in rental_manager.rentals.values() if r.is_active]
            active_rental_objects.sort(key=lambda r: r.end_time)
            
            # Показываем до 3 ближайших истечений
            for i, rental in enumerate(active_rental_objects[:3]):
                account_login = rental.account_login if hasattr(rental, 'account_login') else "Неизвестно"
                remaining_time = rental.get_remaining_time()
                hours, remainder = divmod(remaining_time.seconds, 3600)
                minutes, _ = divmod(remainder, 60)
                
                time_warning = "⚠️ " if hours == 0 and minutes < 30 else ""
                
                status_text += f"{time_warning}<b>{rental.username}</b>: {account_login}\n"
                status_text += f"⏱ Осталось: <b>{hours} ч. {minutes} мин.</b>\n\n"
        
        status_text += f"{'='*30}\n\n"
        
        # Секция привязок
        status_text += "🔗 <b>ПРИВЯЗКИ</b>\n\n"
        status_text += f"• <b>Всего привязок:</b> {len(lot_bindings)}\n\n"
        
        if len(lot_bindings) > 0:
            # Группируем привязки по типам
            bindings_by_type = {}
            for binding in lot_bindings.values():
                bind_type = binding.get("account_type", "unknown")
                if bind_type not in bindings_by_type:
                    bindings_by_type[bind_type] = 0
                bindings_by_type[bind_type] += 1
            
            # Выводим количество привязок по типам
            for bind_type, count in bindings_by_type.items():
                # Определяем наличие свободных аккаунтов для этого типа
                avail_accounts = sum(1 for acc in rental_manager.accounts.values() 
                                 if acc.type == bind_type and acc.status == "available")
                status_emoji = "🟢" if avail_accounts > 0 else "🔴"
                
                status_text += f"• {status_emoji} <b>{bind_type.upper()}</b>: {count} привязок\n"
        
        status_text += f"{'='*30}\n"
        
        markup = InlineKeyboardMarkup()
        markup.row(InlineKeyboardButton("⬅️ НАЗАД", callback_data="srent_menu"))
        markup.row(InlineKeyboardButton("🔄 ОБНОВИТЬ", callback_data="srent_status"))
        
        # Опциональные кнопки управления
        if RUNNING:
            markup.row(InlineKeyboardButton("⛔ ОСТАНОВИТЬ", callback_data="srent_stop"))
        else:
            markup.row(InlineKeyboardButton("▶️ ЗАПУСТИТЬ", callback_data="srent_start"))
        
        try:
            CARDINAL.telegram.bot.edit_message_text(
                status_text,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=markup,
                parse_mode="HTML"
            )
            CARDINAL.telegram.bot.answer_callback_query(call.id, "Статистика обновлена")
        except Exception as edit_error:
            # Если сообщение не изменилось, просто отвечаем callback_query
            if "message is not modified" in str(edit_error):
                CARDINAL.telegram.bot.answer_callback_query(call.id, "Статистика актуальна")
            else:
                # Если другая ошибка - перебрасываем исключение
                raise edit_error
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка отображения статистики: {e}")
        try:
            CARDINAL.telegram.bot.answer_callback_query(call.id, f"Ошибка: {str(e)[:50]}")
        except:
            pass

def show_accounts_callback(call):
    """Показывает список аккаунтов"""
    try:
        if not rental_manager.accounts:
            markup = InlineKeyboardMarkup()
            markup.row(InlineKeyboardButton("⬅️ НАЗАД", callback_data="srent_menu"))
            markup.row(InlineKeyboardButton("➕ ДОБАВИТЬ АККАУНТ", callback_data="srent_add"))
            
            CARDINAL.telegram.bot.edit_message_text(
                "🖥️ <b>АККАУНТЫ STEAM</b>\n\n"
                f"{'='*30}\n\n"
                "⚠️ В системе еще нет добавленных аккаунтов.\n\n"
                "Используйте кнопку ниже, чтобы добавить аккаунт.\n\n"
                f"{'='*30}",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=markup,
                parse_mode="HTML"
            )
            CARDINAL.telegram.bot.answer_callback_query(call.id, "Список аккаунтов пуст")
            return
        
        # Группируем аккаунты по типам
        accounts_by_type = {}
        for login, account in rental_manager.accounts.items():
            acc_type = account.type
            if acc_type not in accounts_by_type:
                accounts_by_type[acc_type] = []
            accounts_by_type[acc_type].append(account)
        
        # Формируем список аккаунтов по группам
        accounts_text = "🖥️ <b>АККАУНТЫ STEAM</b> 🖥️\n\n"
        accounts_text += f"{'='*30}\n\n"
        
        total = len(rental_manager.accounts)
        available = sum(1 for acc in rental_manager.accounts.values() if acc.status == "available")
        rented = sum(1 for acc in rental_manager.accounts.values() if acc.status == "rented")
        
        # Добавляем статистику
        accounts_text += "<b>📊 СТАТИСТИКА</b>\n\n"
        accounts_text += f"🔸 Всего: <b>{total}</b>\n"
        accounts_text += f"🔸 Доступно: <b>{available}</b> 🟢\n"
        accounts_text += f"🔸 В аренде: <b>{rented}</b> 🔴\n\n"
        accounts_text += f"{'='*30}\n\n"
        
        # Добавляем разделы по типам аккаунтов
        for acc_type, accounts in accounts_by_type.items():
            accounts_text += f"<b>📁 ТИП: {acc_type.upper()}</b>\n\n"
            
            available_in_type = sum(1 for acc in accounts if acc.status == "available")
            rented_in_type = sum(1 for acc in accounts if acc.status == "rented")
            
            accounts_text += f"<b>Всего:</b> {len(accounts)} | <b>Доступно:</b> {available_in_type} | <b>В аренде:</b> {rented_in_type}\n\n"
            
            for account in accounts:
                status_emoji = "🟢" if account.status == "available" else "🔴" if account.status == "rented" else "⚫"
                accounts_text += f"{status_emoji} <b>{account.login}</b>\n"
                
                # Если аккаунт в аренде, показываем информацию об аренде
                if account.status == "rented" and account.rental_id in rental_manager.rentals:
                    rental = rental_manager.rentals[account.rental_id]
                    remaining_time = rental.get_remaining_time()
                    hours, remainder = divmod(remaining_time.seconds, 3600)
                    minutes, _ = divmod(remainder, 60)
                    accounts_text += f"  👤 <b>{rental.username}</b>\n"
                    accounts_text += f"  ⏱ Осталось: <b>{hours} ч. {minutes} мин.</b>\n"
                    accounts_text += f"  🔄 <code>/srent_force {account.login}</code>\n"
            
            accounts_text += f"\n{'-'*20}\n\n"
        
        # Если текст слишком длинный, обрезаем его
        if len(accounts_text) > 3500:
            accounts_text = accounts_text[:3500] + "...\n\n⚠️ Список слишком длинный, показаны не все аккаунты"
        
        accounts_text += f"{'='*30}\n\n"
        accounts_text += "<b>КОМАНДЫ:</b>\n"
        accounts_text += "• <code>/srent_del ЛОГИН</code> - удалить аккаунт\n"
        accounts_text += "• <code>/srent_force ЛОГИН</code> - принудительный возврат"
        
        markup = InlineKeyboardMarkup(row_width=2)
        markup.row(InlineKeyboardButton("⬅️ НАЗАД", callback_data="srent_menu"))
        markup.row(
            InlineKeyboardButton("➕ ДОБАВИТЬ", callback_data="srent_add"),
            InlineKeyboardButton("🔄 ВОЗВРАТ", callback_data="srent_return")
        )
        
        CARDINAL.telegram.bot.edit_message_text(
            accounts_text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode="HTML"
        )
        CARDINAL.telegram.bot.answer_callback_query(call.id, "Список аккаунтов")
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка отображения списка аккаунтов: {e}")
        try:
            CARDINAL.telegram.bot.answer_callback_query(call.id, f"Ошибка: {str(e)[:50]}")
        except:
            pass

def show_rentals_callback(call):
    """Показывает список активных аренд"""
    try:
        # Фильтруем только активные аренды
        active_rentals = [rental for rental in rental_manager.rentals.values() if rental.is_active]
        
        if not active_rentals:
            markup = InlineKeyboardMarkup()
            markup.row(InlineKeyboardButton("⬅️ НАЗАД", callback_data="srent_menu"))
            
            CARDINAL.telegram.bot.edit_message_text(
                "⏰ <b>АКТИВНЫЕ АРЕНДЫ</b> ⏰\n\n"
                f"{'='*30}\n\n"
                "📌 В данный момент нет активных аренд.\n\n"
                f"{'='*30}",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=markup,
                parse_mode="HTML"
            )
            CARDINAL.telegram.bot.answer_callback_query(call.id, "Нет активных аренд")
            return
        
        # Сортируем аренды по времени окончания (ближайшие к завершению в начале)
        active_rentals.sort(key=lambda rental: rental.expires_at)
        
        # Формируем сообщение с информацией об арендах
        rentals_text = "⏰ <b>АКТИВНЫЕ АРЕНДЫ</b> ⏰\n\n"
        rentals_text += f"{'='*30}\n\n"
        
        # Добавляем общую статистику
        rentals_text += f"<b>📊 СТАТИСТИКА</b>\n\n"
        rentals_text += f"🔸 Всего активных: <b>{len(active_rentals)}</b>\n"
        rentals_text += f"🔸 Завершено ранее: <b>{len(rental_manager.rentals) - len(active_rentals)}</b>\n\n"
        rentals_text += f"{'='*30}\n\n"
        
        now = datetime.now()
        
        # Группируем аренды по покупателям
        rentals_by_user = {}
        for rental in active_rentals:
            if rental.username not in rentals_by_user:
                rentals_by_user[rental.username] = []
            rentals_by_user[rental.username].append(rental)
        
        # Выводим аренды, сгруппированные по пользователям
        rentals_text += "<b>📋 СПИСОК АРЕНД</b>\n\n"
        
        for username, user_rentals in rentals_by_user.items():
            rentals_text += f"<b>👤 {username}</b>\n"
            for rental in user_rentals:
                # Находим аккаунт по rental_id
                account = None
                for acc in rental_manager.accounts.values():
                    if acc.rental_id == rental.id:
                        account = acc
                        break
                
                remaining_time = rental.get_remaining_time()
                hours, remainder = divmod(remaining_time.seconds, 3600)
                minutes, _ = divmod(remainder, 60)
                
                # Если осталось мало времени (менее 30 минут), добавляем предупреждение
                time_warning = "⚠️ " if hours == 0 and minutes < 30 else ""
                
                rentals_text += f"  {time_warning}🔑 <b>{account.login if account else 'Неизвестно'}</b>\n"
                rentals_text += f"  ⏱ <b>Осталось:</b> {hours} ч. {minutes} мин.\n"
                rentals_text += f"  💰 <b>Тип:</b> {account.type if account else 'Неизвестно'}\n"
                rentals_text += f"  🔄 <code>/srent_force {account.login if account else '?'}</code>\n\n"
            
            rentals_text += f"{'-'*20}\n\n"
        
        # Если текст слишком длинный, обрезаем его
        if len(rentals_text) > 3900:
            rentals_text = rentals_text[:3900] + "...\n\n⚠️ Список слишком длинный, показаны не все аренды"
        
        rentals_text += f"{'='*30}\n\n"
        rentals_text += "<b>УПРАВЛЕНИЕ:</b> <code>/srent_force ЛОГИН</code> - принудительный возврат"
        
        markup = InlineKeyboardMarkup()
        markup.row(InlineKeyboardButton("⬅️ НАЗАД", callback_data="srent_menu"))
        markup.row(InlineKeyboardButton("🔄 ОБНОВИТЬ", callback_data="srent_rentals"))
        
        CARDINAL.telegram.bot.edit_message_text(
            rentals_text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode="HTML"
        )
        CARDINAL.telegram.bot.answer_callback_query(call.id, "Список активных аренд")
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка отображения списка аренд: {e}")
        try:
            CARDINAL.telegram.bot.answer_callback_query(call.id, f"Ошибка: {str(e)[:50]}")
        except:
            pass

def show_return_account_callback(call):
    """Показывает список аккаунтов для принудительного возврата"""
    try:
        # Фильтруем только арендованные аккаунты
        rented_accounts = {login: account for login, account in rental_manager.accounts.items() if account.status == "rented"}
        
        if not rented_accounts:
            markup = InlineKeyboardMarkup()
            markup.row(InlineKeyboardButton("« Назад в меню", callback_data="srent_menu"))
            
            CARDINAL.telegram.bot.edit_message_text(
                "🔄 <b>Возврат аккаунта</b>\n\n"
                "В данный момент нет арендованных аккаунтов для возврата.",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=markup,
                parse_mode="HTML"
            )
            CARDINAL.telegram.bot.answer_callback_query(call.id, "Нет арендованных аккаунтов")
            return
        
        # Создаем клавиатуру с кнопками для каждого аккаунта
        markup = InlineKeyboardMarkup()
        for login in rented_accounts:
            markup.row(InlineKeyboardButton(f"🔄 {login}", callback_data=f"srent_force_return_{login}"))
        markup.row(InlineKeyboardButton("« Назад в меню", callback_data="srent_menu"))
        
        CARDINAL.telegram.bot.edit_message_text(
            "🔄 <b>Возврат аккаунта</b>\n\n"
            "Выберите аккаунт для принудительного возврата из аренды.\n\n"
            "⚠️ <b>Внимание!</b> При возврате будет изменен пароль аккаунта и текущие сессии будут завершены.",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode="HTML"
        )
        CARDINAL.telegram.bot.answer_callback_query(call.id, "Выберите аккаунт для возврата")
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка отображения меню возврата аккаунта: {e}")
        try:
            CARDINAL.telegram.bot.answer_callback_query(call.id, f"Ошибка: {str(e)[:50]}")
        except:
            pass

def force_return_account_from_callback(call, login):
    """Принудительно возвращает аккаунт из аренды по callback"""
    try:
        # Проверяем, существует ли аккаунт
        if login not in rental_manager.accounts:
            CARDINAL.telegram.bot.answer_callback_query(call.id, "Аккаунт не найден!")
            return
            
        account = rental_manager.accounts[login]
        
        # Проверяем, арендован ли аккаунт
        if account.status != "rented" or not account.rental_id:
            CARDINAL.telegram.bot.answer_callback_query(call.id, "Аккаунт не арендован!")
            return
            
        # Находим аренду
        rental = rental_manager.rentals.get(account.rental_id)
        if not rental:
            CARDINAL.telegram.bot.answer_callback_query(call.id, "Данные аренды не найдены!")
            return
            
        # Запоминаем данные пользователя
        username = rental.username
        user_id = rental.user_id
        
        # Возвращаем аккаунт
        success, message, new_password = rental_manager.return_account(account.rental_id)
        
        if not success:
            CARDINAL.telegram.bot.answer_callback_query(call.id, f"Ошибка: {message}")
            return
            
        # Создаем клавиатуру для возврата в меню
        markup = InlineKeyboardMarkup()
        markup.row(InlineKeyboardButton("« Назад в меню", callback_data="srent_menu"))
        
        CARDINAL.telegram.bot.edit_message_text(
            "✅ <b>Аккаунт успешно возвращен</b>\n\n"
            f"🎮 Логин: {login}\n"
            f"👤 Пользователь: {username}\n"
            f"✅ Статус аккаунта изменен на 'available'\n"
            f"✅ Пароль изменен на: <code>{new_password}</code>\n"
            "✅ Текущие сессии завершены",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode="HTML"
        )
        CARDINAL.telegram.bot.answer_callback_query(call.id, "Аккаунт успешно возвращен")
        
        # Отправляем сообщение пользователю FunPay о завершении аренды
        try:
            message = format_message("rental_force_end", 
                login=login,
                username=username
            )
            
            if hasattr(CARDINAL, 'account') and hasattr(CARDINAL.account, 'send_message'):
                # Получаем данные чата
                chat_id = f"users-{user_id}-{CARDINAL.account.id}"
                interlocutor_id = user_id
                chat_name = f"Переписка с {username}"
                
                # Используем низкоуровневый метод отправки сообщений
                CARDINAL.account.send_message(chat_id, message, chat_name, interlocutor_id, None, True, False, False)
                logger.info(f"{LOGGER_PREFIX} Сообщение о принудительном завершении аренды отправлено")
            else:
                logger.warning(f"{LOGGER_PREFIX} Невозможно отправить сообщение: методы отправки недоступны")
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка отправки сообщения о завершении аренды: {e}")
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка принудительного возврата аккаунта: {e}")
        try:
            CARDINAL.telegram.bot.answer_callback_query(call.id, f"Ошибка: {str(e)[:50]}")
        except:
            pass

def show_lot_bindings_callback(call):
    """Показывает список привязок лотов"""
    try:
        if not lot_bindings:
            markup = InlineKeyboardMarkup()
            markup.row(InlineKeyboardButton("⬅️ Назад в меню", callback_data="srent_menu"))
            markup.row(InlineKeyboardButton("Добавить привязку лота", callback_data="srent_add_binding"))
            
            CARDINAL.telegram.bot.edit_message_text(
                "🔗 ПРИВЯЗКИ ЛОТОВ\n\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "В данный момент нет привязок лотов.\n\n"
                "Нажмите кнопку ниже для добавления привязки.\n"
                "━━━━━━━━━━━━━━━━━━━━━━",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=markup,
                parse_mode="HTML"
            )
            CARDINAL.telegram.bot.answer_callback_query(call.id, "Нет привязок лотов")
            return
        
        # Сортируем привязки по типу аккаунта
        sorted_bindings = sorted(lot_bindings.items(), key=lambda x: (x[1]["account_type"], x[1]["duration_hours"]))
        
        # Группируем привязки по типу аккаунта
        bindings_by_type = {}
        for lot_name, binding in sorted_bindings:
            acc_type = binding["account_type"]
            if acc_type not in bindings_by_type:
                bindings_by_type[acc_type] = []
            bindings_by_type[acc_type].append((lot_name, binding))
        
        # Формируем список привязок
        bindings_text = "🔗 ПРИВЯЗКИ ЛОТОВ\n\n"
        bindings_text += "━━━━━━━━━━━━━━━━━━━━━━\n"
        bindings_text += f"Всего привязок: {len(lot_bindings)}\n"
        bindings_text += "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        
        # Создаем клавиатуру с кнопками для каждой привязки
        markup = InlineKeyboardMarkup(row_width=1)
        
        # Показываем не более 10 привязок, чтобы не превысить лимит Telegram
        binding_count = 0
        shown_types = []
        
        # Создаем словарь для хранения соответствия хешей с оригинальными названиями лотов
        binding_hashes = {}
        
        for acc_type, bindings in bindings_by_type.items():
            if binding_count >= 10:
                break
                
            bindings_text += f"📋 ТИП: {acc_type.upper()}\n\n"
            shown_types.append(acc_type)
            
            for lot_name, binding in bindings[:3]:  # Показываем до 3 привязок каждого типа
                if binding_count >= 10:
                    break
                    
                # Сокращаем длинные названия лотов
                display_name = lot_name
                if len(display_name) > 40:
                    display_name = display_name[:37] + "..."
                
                bindings_text += f"⏱ {binding['duration_hours']} ч. | 💜 {display_name}\n"
                
                # Создаем уникальный короткий хеш для лота
                lot_hash = str(abs(hash(lot_name)) % 1000000)  # Используем хеш для создания короткого идентификатора
                binding_hashes[lot_hash] = lot_name  # Сохраняем соответствие хеша оригинальному имени
                
                # Добавляем кнопку для этой привязки с коротким идентификатором
                markup.row(InlineKeyboardButton(f"Управление: {display_name[:20]}...", callback_data=f"srent_binding_{lot_hash}"))
                
                binding_count += 1
        
        # Сохраняем словарь хешей в глобальную переменную для использования в других функциях
        global binding_hash_map
        binding_hash_map = binding_hashes
        
        # Добавляем кнопки управления привязками
        bindings_text += "\n━━━━━━━━━━━━━━━━━━━━━━\n"
        bindings_text += "Доступные команды:"
        
        # Если показаны не все типы, добавляем кнопку "Показать все"
        if len(shown_types) < len(bindings_by_type):
            markup.row(InlineKeyboardButton("Показать все привязки 📋", callback_data="srent_all_bindings"))
        
        # Добавляем кнопки для добавления/удаления привязок
        markup.row(InlineKeyboardButton("Добавить привязку лота", callback_data="srent_add_binding"))
        markup.row(InlineKeyboardButton("Справка по привязкам", callback_data="srent_binding_help"))
        markup.row(InlineKeyboardButton("⬅️ Назад в меню", callback_data="srent_menu"))
        markup.row(InlineKeyboardButton("🔄 Обновить", callback_data="srent_lot_bindings"))
        
        try:
            CARDINAL.telegram.bot.edit_message_text(
                bindings_text,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=markup,
                parse_mode="HTML"
            )
        except Exception as edit_error:
            # Если сообщение не изменилось, просто отвечаем callback_query
            if "message is not modified" in str(edit_error):
                CARDINAL.telegram.bot.answer_callback_query(call.id, "Список привязок актуален")
            else:
                # Если другая ошибка - перебрасываем исключение
                raise edit_error
                
        CARDINAL.telegram.bot.answer_callback_query(call.id, "Список привязок лотов")
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка отображения привязок лотов: {e}")
        try:
            CARDINAL.telegram.bot.answer_callback_query(call.id, f"Ошибка: {str(e)[:50]}")
        except:
            pass

def delete_account_callback(call, login):
    """Удаляет аккаунт по callback"""
    try:
        # Проверяем, существует ли аккаунт
        if login not in rental_manager.accounts:
            CARDINAL.telegram.bot.answer_callback_query(call.id, "Аккаунт не найден!")
            return
            
        account = rental_manager.accounts[login]
        
        # Проверяем, не арендован ли аккаунт
        if account.status == "rented":
            CARDINAL.telegram.bot.answer_callback_query(call.id, "Нельзя удалить арендованный аккаунт!")
            return
            
        # Удаляем аккаунт
        success, message = rental_manager.remove_account(login)
        
        if not success:
            CARDINAL.telegram.bot.answer_callback_query(call.id, f"Ошибка: {message}")
            return
            
        # Создаем клавиатуру для возврата в меню
        markup = InlineKeyboardMarkup()
        markup.row(InlineKeyboardButton("« Назад к аккаунтам", callback_data="srent_accounts"))
        markup.row(InlineKeyboardButton("« Назад в меню", callback_data="srent_menu"))
        
        CARDINAL.telegram.bot.edit_message_text(
            "✅ <b>Аккаунт успешно удален</b>\n\n"
            f"🎮 Логин: {login}\n"
            f"🔰 Тип: {account.type}",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode="HTML"
        )
        CARDINAL.telegram.bot.answer_callback_query(call.id, "Аккаунт успешно удален")
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка удаления аккаунта: {e}")
        try:
            CARDINAL.telegram.bot.answer_callback_query(call.id, f"Ошибка: {str(e)[:50]}")
        except:
            pass

# Команды для работы с системой аренды
def start_rental_system(message):
    """Запускает систему аренды"""
    global RUNNING
    if RUNNING:
        CARDINAL.telegram.bot.send_message(
            message.chat.id,
            "⚠️ Система аренды уже запущена."
        )
        return
        
    RUNNING = True
    logger.info(f"{LOGGER_PREFIX} Система аренды запущена через команду")
    
    CARDINAL.telegram.bot.send_message(
        message.chat.id,
        "✅ <b>Система аренды успешно запущена!</b>\n\n"
        "Теперь бот будет автоматически выдавать аккаунты при покупке соответствующих лотов.",
        parse_mode="HTML"
    )

def stop_rental_system(message):
    """Останавливает систему аренды"""
    global RUNNING
    if not RUNNING:
        CARDINAL.telegram.bot.send_message(
            message.chat.id,
            "⚠️ Система аренды уже остановлена."
        )
        return
        
    RUNNING = False
    logger.info(f"{LOGGER_PREFIX} Система аренды остановлена через команду")
    
    CARDINAL.telegram.bot.send_message(
        message.chat.id,
        "🛑 <b>Система аренды остановлена!</b>\n\n"
        "Автоматическая выдача аккаунтов при покупке лотов отключена.\n"
        "Активные аренды продолжат работать до завершения.",
        parse_mode="HTML"
    )

# Команды для управления аккаунтами
def add_account_cmd(message):
    """Добавляет аккаунт через команду"""
    try:
        text = message.text.strip()
        
        # Убираем команду из начала
        if text.startswith('/srent_add'):
            text = text[len('/srent_add'):].strip()
        
        # Парсим параметры, формат: логин пароль [тип]
        params = text.split()
        
        if len(params) < 2:
            CARDINAL.telegram.bot.send_message(
                message.chat.id, 
                "❌ <b>Неверный формат команды</b>\n\n"
                "Используйте: <code>/srent_add ЛОГИН ПАРОЛЬ [ТИП]</code>\n\n"
                "Пример: <code>/srent_add steamuser123 password123 pubg</code>\n"
                "Если тип не указан, будет использован 'standard'.",
                parse_mode="HTML"
            )
            return
        
        login = params[0]
        password = params[1]
        account_type = "standard"
        
        # Если указан тип аккаунта
        if len(params) > 2:
            account_type = params[2]
        
        # Добавляем аккаунт
        success, message_text = rental_manager.add_account(login, password, account_type)
        
        if success:
            CARDINAL.telegram.bot.send_message(
                message.chat.id,
                f"✅ {message_text}\n\n"
                f"Логин: <code>{login}</code>\n"
                f"Тип: <code>{account_type}</code>",
                parse_mode="HTML"
            )
        else:
            CARDINAL.telegram.bot.send_message(
                message.chat.id,
                f"❌ {message_text}",
                parse_mode="HTML"
            )
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка в обработчике add_account_cmd: {e}")
        try:
            CARDINAL.telegram.bot.send_message(message.chat.id, f"❌ Произошла ошибка: {e}")
        except:
            pass

def list_accounts_cmd(message):
    """Выводит список аккаунтов"""
    try:
        if not rental_manager.accounts:
            CARDINAL.telegram.bot.send_message(
                message.chat.id,
                "ℹ️ В системе еще нет добавленных аккаунтов.\n\n"
                "Для добавления используйте команду <code>/srent_add</code> или <code>/steam_add</code>",
                parse_mode="HTML"
            )
            return
        
        # Формируем список аккаунтов
        accounts_text = ""
        for login, account in rental_manager.accounts.items():
            status_emoji = "🟢" if account.status == "available" else "🔴" if account.status == "rented" else "⚫"
            accounts_text += f"{status_emoji} <b>{login}</b> ({account.type})\n"
            accounts_text += f"   Статус: {account.status}\n"
            
            # Если аккаунт в аренде, показываем информацию об аренде
            if account.status == "rented" and account.rental_id in rental_manager.rentals:
                rental = rental_manager.rentals[account.rental_id]
                remaining_time = rental.get_remaining_time()
                hours, remainder = divmod(remaining_time.seconds, 3600)
                minutes, _ = divmod(remainder, 60)
                accounts_text += f"   Арендатор: {rental.username}\n"
                accounts_text += f"   Осталось: {hours} ч. {minutes} мин.\n"
                accounts_text += f"   [<code>/srent_force {login}</code>]\n"
            
            accounts_text += f"   [<code>/srent_del {login}</code>]\n\n"
        
        # Если текст слишком длинный, обрезаем его
        if len(accounts_text) > 3500:
            accounts_text = accounts_text[:3500] + "...\n\n(Список слишком длинный, показаны не все аккаунты)"
        
        CARDINAL.telegram.bot.send_message(
            message.chat.id,
            "🎮 <b>Список аккаунтов</b>\n\n" + accounts_text,
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка в обработчике list_accounts_cmd: {e}")
        try:
            CARDINAL.telegram.bot.send_message(message.chat.id, f"❌ Произошла ошибка: {e}")
        except:
            pass

def list_rentals_cmd(message):
    """Выводит список активных аренд"""
    try:
        # Фильтруем только активные аренды
        active_rentals = [rental for rental in rental_manager.rentals.values() if rental.is_active]
        
        if not active_rentals:
            CARDINAL.telegram.bot.send_message(
                message.chat.id,
                "ℹ️ В данный момент нет активных аренд.",
                parse_mode="HTML"
            )
            return
        
        # Формируем список аренд
        rentals_text = ""
        for rental in active_rentals:
            account = None
            if rental.account_login in rental_manager.accounts:
                account = rental_manager.accounts[rental.account_login]
            
            remaining_time = rental.get_remaining_time()
            hours, remainder = divmod(remaining_time.seconds, 3600)
            minutes, _ = divmod(remainder, 60)
            
            rentals_text += f"👤 <b>{rental.username}</b>\n"
            rentals_text += f"🎮 Аккаунт: {rental.account_login}\n"
            if account:
                rentals_text += f"🔰 Тип: {account.type}\n"
            rentals_text += f"⏱ Срок: {rental.duration_hours} ч.\n"
            rentals_text += f"⌛ Осталось: {hours} ч. {minutes} мин.\n"
            rentals_text += f"🆔 ID заказа: {rental.order_id or 'N/A'}\n"
            rentals_text += f"[<code>/srent_force {rental.account_login}</code>]\n\n"
        
        # Если текст слишком длинный, обрезаем его
        if len(rentals_text) > 3500:
            rentals_text = rentals_text[:3500] + "...\n\n(Список слишком длинный, показаны не все аренды)"
        
        CARDINAL.telegram.bot.send_message(
            message.chat.id,
            "⏳ <b>Активные аренды</b>\n\n" + rentals_text,
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка в обработчике list_rentals_cmd: {e}")
        try:
            CARDINAL.telegram.bot.send_message(message.chat.id, f"❌ Произошла ошибка: {e}")
        except:
            pass

def force_return_account_cmd(message):
    """Принудительно возвращает аккаунт из аренды"""
    try:
        text = message.text.strip()
        
        # Убираем команду из начала
        if text.startswith('/srent_force'):
            text = text[len('/srent_force'):].strip()
        
        if not text:
            CARDINAL.telegram.bot.send_message(
                message.chat.id, 
                "❌ <b>Неверный формат команды</b>\n\n"
                "Используйте: <code>/srent_force ЛОГИН</code>\n\n"
                "Список активных аренд можно посмотреть командой <code>/steam_active</code>",
                parse_mode="HTML"
            )
            return
        
        login = text
        
        # Проверяем, существует ли аккаунт
        if login not in rental_manager.accounts:
            CARDINAL.telegram.bot.send_message(
                message.chat.id,
                "❌ Аккаунт не найден.",
                parse_mode="HTML"
            )
            return
            
        account = rental_manager.accounts[login]
        
        # Проверяем, арендован ли аккаунт
        if account.status != "rented" or not account.rental_id:
            CARDINAL.telegram.bot.send_message(
                message.chat.id,
                "❌ Аккаунт не арендован.",
                parse_mode="HTML"
            )
            return
            
        # Находим аренду
        rental = rental_manager.rentals.get(account.rental_id)
        if not rental:
            CARDINAL.telegram.bot.send_message(
                message.chat.id,
                "❌ Данные аренды не найдены.",
                parse_mode="HTML"
            )
            return
            
        # Запоминаем данные пользователя
        username = rental.username
        user_id = rental.user_id
        
        # Возвращаем аккаунт
        success, message_text, new_password = rental_manager.return_account(account.rental_id)
        
        if not success:
            CARDINAL.telegram.bot.send_message(
                message.chat.id,
                f"❌ {message_text}",
                parse_mode="HTML"
            )
            return
            
        CARDINAL.telegram.bot.send_message(
            message.chat.id,
            "✅ <b>Аккаунт успешно возвращен</b>\n\n"
            f"🎮 Логин: {login}\n"
            f"👤 Пользователь: {username}\n\n"
            "✅ Статус аккаунта изменен на 'available'\n"
            f"✅ Пароль изменен на: <code>{new_password}</code>\n"
            "✅ Текущие сессии завершены",
            parse_mode="HTML"
        )
        
        # Отправляем сообщение пользователю FunPay о завершении аренды
        try:
            message = format_message("rental_force_end", 
                login=login,
                username=username
            )
            
            if hasattr(CARDINAL, 'account') and hasattr(CARDINAL.account, 'send_message'):
                # Получаем данные чата
                chat_id = f"users-{user_id}-{CARDINAL.account.id}"
                interlocutor_id = user_id
                chat_name = f"Переписка с {username}"
                
                # Используем низкоуровневый метод отправки сообщений
                CARDINAL.account.send_message(chat_id, message, chat_name, interlocutor_id, None, True, False, False)
                logger.info(f"{LOGGER_PREFIX} Сообщение о принудительном завершении аренды отправлено")
            else:
                logger.warning(f"{LOGGER_PREFIX} Невозможно отправить сообщение: методы отправки недоступны")
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка отправки сообщения о завершении аренды: {e}")

    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка в обработчике force_return_account_cmd: {e}")
        try:
            CARDINAL.telegram.bot.send_message(message.chat.id, f"❌ Произошла ошибка: {e}")
        except:
            pass

def manual_rent_account_cmd(message):
    """Ручная выдача аккаунта в аренду"""
    try:
        text = message.text.strip()
        
        # Убираем команду из начала
        if text.startswith('/srent_manual'):
            text = text[len('/srent_manual'):].strip()
        
        # Парсим параметры, формат: username user_id [тип] [часы]
        params = text.split()
        
        if len(params) < 2:
            CARDINAL.telegram.bot.send_message(
                message.chat.id, 
                "❌ <b>Неверный формат команды</b>\n\n"
                "Используйте: <code>/srent_manual USERNAME USER_ID [ТИП] [ЧАСЫ]</code>\n\n"
                "Пример: <code>/srent_manual test_user 12345 pubg 2</code>",
                parse_mode="HTML"
            )
            return
        
        username = params[0]
        
        # Проверяем, что user_id - число
        try:
            user_id = int(params[1])
        except ValueError:
            CARDINAL.telegram.bot.send_message(
                message.chat.id,
                "❌ USER_ID должен быть числом.",
                parse_mode="HTML"
            )
            return
        
        # По умолчанию - любой тип, 1 час
        account_type = None
        duration_hours = 1
        
        # Если указан тип аккаунта
        if len(params) > 2:
            account_type = params[2]
        
        # Если указано количество часов
        if len(params) > 3:
            try:
                duration_hours = int(params[3])
                if duration_hours <= 0:
                    raise ValueError("Количество часов должно быть положительным")
            except ValueError:
                CARDINAL.telegram.bot.send_message(
                    message.chat.id,
                    "❌ Количество часов должно быть положительным числом.",
                    parse_mode="HTML"
                )
                return
        
        # Выбираем аккаунт нужного типа
        account = rental_manager.get_account_by_type(account_type)
        
        if not account:
            CARDINAL.telegram.bot.send_message(
                message.chat.id,
                f"❌ Нет доступных аккаунтов{' типа ' + account_type if account_type else ''}.",
                parse_mode="HTML"
            )
            return
        
        # Арендуем аккаунт
        success, message_text, account, rental = rental_manager.rent_account(
            user_id, username, duration_hours, account_type
        )
        
        if not success:
            CARDINAL.telegram.bot.send_message(
                message.chat.id,
                f"❌ {message_text}",
                parse_mode="HTML"
            )
            return
            
        end_time_str = datetime.fromtimestamp(rental.end_time).strftime("%d.%m.%Y %H:%M")
        
        # Отправляем сообщение в Telegram
        CARDINAL.telegram.bot.send_message(
            message.chat.id,
            "✅ <b>Аккаунт успешно выдан</b>\n\n"
            f"👤 Пользователь: <b>{username}</b> (ID: {user_id})\n"
            f"🎮 Аккаунт: <code>{account.login}</code>\n"
            f"🔑 Пароль: <code>{account.password}</code>\n"
            f"🔰 Тип: <code>{account.type}</code>\n"
            f"⏱ Срок: <code>{duration_hours} ч.</code>\n"
            f"⌛ Окончание: {end_time_str}",
            parse_mode="HTML"
        )
        
        # Отправляем сообщение пользователю FunPay
        try:
            message_text = f"Аренда аккаунта Steam\n\nЛогин: {account.login}\nПароль: {account.password}\nТип: {account.type}\n\nСрок аренды: {duration_hours} ч.\nДата окончания: {end_time_str}\n\nВажно:\n- По истечении срока доступ будет заблокирован\n- Пароль будет изменен\n- Не меняйте пароль от аккаунта\n- Не включайте двухфакторную аутентификацию"
            
            if hasattr(CARDINAL, 'account') and hasattr(CARDINAL.account, 'send_message'):
                # Получаем данные чата
                chat_id = f"users-{user_id}-{CARDINAL.account.id}"
                interlocutor_id = user_id
                chat_name = f"Переписка с {username}"
                
                # Используем низкоуровневый метод отправки сообщений
                CARDINAL.account.send_message(chat_id, message_text, chat_name, interlocutor_id, None, True, False, False)
                logger.info(f"{LOGGER_PREFIX} Сообщение о выдаче аккаунта отправлено")
            else:
                logger.warning(f"{LOGGER_PREFIX} Невозможно отправить сообщение: методы отправки недоступны")
        except Exception as e:
            logger.error(f"{LOGGER_PREFIX} Ошибка отправки сообщения о выдаче аккаунта: {e}")
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка в обработчике manual_rent_account_cmd: {e}")
        try:
            CARDINAL.telegram.bot.send_message(message.chat.id, f"❌ Произошла ошибка: {e}")
        except:
            pass

def return_account_cmd(message):
    """Обработчик команды возврата аккаунта"""
    try:
        text = message.text.strip()
        
        # Убираем команду из начала
        if text.startswith('/srent_return'):
            text = text[len('/srent_return'):].strip()
        
        if text:
            # Если указан логин аккаунта
            login = text
            
            # Проверяем, существует ли аккаунт
            if login not in rental_manager.accounts:
                CARDINAL.telegram.bot.send_message(
                    message.chat.id,
                    "❌ Аккаунт не найден.",
                    parse_mode="HTML"
                )
                return
                
            account = rental_manager.accounts[login]
            
            # Проверяем, арендован ли аккаунт
            if account.status != "rented" or not account.rental_id:
                CARDINAL.telegram.bot.send_message(
                    message.chat.id,
                    "❌ Аккаунт не арендован.",
                    parse_mode="HTML"
                )
                return
                
            # Возвращаем аккаунт
            success, message_text, new_password = rental_manager.return_account(account.rental_id)
            
            if not success:
                CARDINAL.telegram.bot.send_message(
                    message.chat.id,
                    f"❌ {message_text}",
                    parse_mode="HTML"
                )
                return
                
            CARDINAL.telegram.bot.send_message(
                message.chat.id,
                "✅ <b>Аккаунт успешно возвращен</b>\n\n"
                f"🎮 Логин: {login}\n\n"
                "✅ Статус аккаунта изменен на 'available'\n"
                f"✅ Пароль изменен на: <code>{new_password}</code>\n"
                "✅ Текущие сессии завершены",
                parse_mode="HTML"
            )
        else:
            # Если логин не указан, показываем список аккаунтов для возврата
            # Фильтруем только арендованные аккаунты
            rented_accounts = {login: account for login, account in rental_manager.accounts.items() if account.status == "rented"}
            
            if not rented_accounts:
                CARDINAL.telegram.bot.send_message(
                    message.chat.id,
                    "ℹ️ В данный момент нет арендованных аккаунтов для возврата.",
                    parse_mode="HTML"
                )
                return
            
            # Формируем список арендованных аккаунтов
            accounts_text = "🔄 <b>Выберите аккаунт для возврата</b>\n\n"
            for login, account in rented_accounts.items():
                rental = rental_manager.rentals.get(account.rental_id)
                if rental:
                    remaining_time = rental.get_remaining_time()
                    hours, remainder = divmod(remaining_time.seconds, 3600)
                    minutes, _ = divmod(remainder, 60)
                    accounts_text += f"🔴 <b>{login}</b> ({account.type})\n"
                    accounts_text += f"   Арендатор: {rental.username}\n"
                    accounts_text += f"   Осталось: {hours} ч. {minutes} мин.\n"
                    accounts_text += f"   Возврат: <code>/srent_return {login}</code>\n\n"
                else:
                    accounts_text += f"🔴 <b>{login}</b> ({account.type})\n"
                    accounts_text += f"   Возврат: <code>/srent_return {login}</code>\n\n"
            
            CARDINAL.telegram.bot.send_message(
                message.chat.id,
                accounts_text,
                parse_mode="HTML"
            )
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка в обработчике return_account_cmd: {e}")
        try:
            CARDINAL.telegram.bot.send_message(message.chat.id, f"❌ Произошла ошибка: {e}")
        except:
            pass

def del_account_cmd(message):
    """Удаляет аккаунт"""
    try:
        text = message.text.strip()
        
        # Убираем команду из начала
        if text.startswith('/srent_del'):
            text = text[len('/srent_del'):].strip()
        
        if not text:
            CARDINAL.telegram.bot.send_message(
                message.chat.id, 
                "❌ <b>Неверный формат команды</b>\n\n"
                "Используйте: <code>/srent_del ЛОГИН</code>\n\n"
                "Список аккаунтов можно посмотреть командой <code>/srent_list</code>",
                parse_mode="HTML"
            )
            return
        
        login = text
        
        # Проверяем, существует ли аккаунт
        if login not in rental_manager.accounts:
            CARDINAL.telegram.bot.send_message(
                message.chat.id,
                "❌ Аккаунт не найден.",
                parse_mode="HTML"
            )
            return
            
        account = rental_manager.accounts[login]
        
        # Проверяем, не арендован ли аккаунт
        if account.status == "rented":
            CARDINAL.telegram.bot.send_message(
                message.chat.id,
                "❌ Нельзя удалить арендованный аккаунт.",
                parse_mode="HTML"
            )
            return
            
        # Удаляем аккаунт
        success, message_text = rental_manager.remove_account(login)
        
        if not success:
            CARDINAL.telegram.bot.send_message(
                message.chat.id,
                f"❌ {message_text}",
                parse_mode="HTML"
            )
            return
            
        CARDINAL.telegram.bot.send_message(
            message.chat.id,
            "✅ <b>Аккаунт успешно удален</b>\n\n"
            f"🎮 Логин: {login}\n"
            f"🔰 Тип: {account.type}",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка в обработчике del_account_cmd: {e}")
        try:
            CARDINAL.telegram.bot.send_message(message.chat.id, f"❌ Произошла ошибка: {e}")
        except:
            pass

# Функции для работы с привязками лотов
def unbind_lot_cmd(message):
    """Удаляет привязку аккаунта к лоту"""
    try:
        text = message.text.strip()
        
        # Убираем команду из начала
        if text.startswith('/srent_unbind'):
            text = text[len('/srent_unbind'):].strip()
        
        if not text:
            # Если не указано название лота
            CARDINAL.telegram.bot.send_message(
                message.chat.id, 
                "❌ <b>Неверный формат команды</b>\n\n"
                "Используйте: <code>/srent_unbind НАЗВАНИЕ_ЛОТА</code>\n\n"
                "Список текущих привязок можно посмотреть командой <code>/srent_bindings</code>",
                parse_mode="HTML"
            )
            return
        
        # Проверяем, есть ли привязка для указанного лота
        found_lot_name = None
        
        # Проверяем точное совпадение
        if text in lot_bindings:
            found_lot_name = text
        else:
            # Проверяем любое начало строки
            for lot_name in lot_bindings:
                if lot_name.startswith(text):
                    found_lot_name = lot_name
                    break
                elif text.lower() in lot_name.lower():
                    found_lot_name = lot_name
                    break
        
        if not found_lot_name:
            CARDINAL.telegram.bot.send_message(
                message.chat.id,
                "❌ Привязка с указанным названием лота не найдена.",
                parse_mode="HTML"
            )
            return
        
        # Сохраняем данные перед удалением для отображения
        binding = lot_bindings[found_lot_name]
        account_type = binding.get("account_type", "Не указан")
        duration_hours = binding.get("duration_hours", 0)
        
        # Удаляем привязку
        del lot_bindings[found_lot_name]
        save_lot_bindings()
        
        CARDINAL.telegram.bot.send_message(
            message.chat.id,
            "✅ <b>Привязка удалена!</b>\n\n"
            f"🔹 Название лота: {found_lot_name}\n"
            f"🔹 Тип аккаунта: {account_type}\n"
            f"🔹 Срок аренды: {duration_hours} ч.",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка в обработчике unbind_lot_cmd: {e}")
        try:
            CARDINAL.telegram.bot.send_message(message.chat.id, f"❌ Произошла ошибка: {e}")
        except:
            pass

def list_bindings_cmd(message):
    """Показывает список привязок лотов"""
    try:
        if not lot_bindings:
            CARDINAL.telegram.bot.send_message(
                message.chat.id,
                "🔗 <b>ПРИВЯЗКИ ЛОТОВ</b> 🔗\n\n"
                f"{'='*30}\n\n"
                "📌 В данный момент нет привязок лотов.\n\n"
                "<b>КАК ДОБАВИТЬ:</b>\n"
                "Используйте команду:\n"
                "<code>/srent_bind НАЗВАНИЕ_ЛОТА | ТИП | ЧАСЫ</code>\n\n"
                "<b>ПРИМЕР:</b>\n"
                "<code>/srent_bind Аренда PUBG | PUBG | 2</code>\n\n"
                f"{'='*30}",
                parse_mode="HTML"
            )
            return
        
        # Группируем привязки по типу аккаунта
        bindings_by_type = {}
        for lot_name, binding in lot_bindings.items():
            acc_type = binding.get("account_type", "unknown")
            if acc_type not in bindings_by_type:
                bindings_by_type[acc_type] = []
            bindings_by_type[acc_type].append((lot_name, binding))
        
        # Формируем сообщение для отображения привязок
        bindings_text = "🔗 <b>ПРИВЯЗКИ ЛОТОВ</b> 🔗\n\n"
        bindings_text += f"{'='*30}\n\n"
        
        # Добавляем общую статистику
        bindings_text += "<b>📊 СТАТИСТИКА</b>\n\n"
        bindings_text += f"🔸 Всего привязок: <b>{len(lot_bindings)}</b>\n"
        bindings_text += f"🔸 Типов аккаунтов: <b>{len(bindings_by_type)}</b>\n\n"
        bindings_text += f"{'='*30}\n\n"
        
        # Выводим привязки по типам
        bindings_text += "<b>📋 СПИСОК ПРИВЯЗОК</b>\n\n"
        
        for acc_type, bindings in bindings_by_type.items():
            bindings_text += f"<b>📁 ТИП: {acc_type.upper()}</b>\n\n"
            
            # Выводим все привязки для данного типа
            for i, (lot_name, binding) in enumerate(bindings, 1):
                # Определяем доступность аккаунтов этого типа
                available_accounts = sum(1 for acc in rental_manager.accounts.values() 
                                      if acc.type == acc_type and acc.status == "available")
                
                # Эмодзи статуса в зависимости от наличия свободных аккаунтов
                status_emoji = "🟢" if available_accounts > 0 else "🔴"
                
                bindings_text += f"{status_emoji} <b>{lot_name}</b>\n"
                bindings_text += f"  ⏱ Аренда: <b>{binding.get('duration_hours', 1)} ч.</b>\n"
                bindings_text += f"  🖥️ Доступно: <b>{available_accounts}</b> аккаунтов\n"
                bindings_text += f"  ❌ <code>/srent_unbind {lot_name[:15]}</code>\n\n"
            
            bindings_text += f"{'-'*20}\n\n"
        
        # Если текст слишком длинный, обрезаем его
        if len(bindings_text) > 3900:
            bindings_text = bindings_text[:3900] + "...\n\n⚠️ Список слишком длинный, показаны не все привязки"
        
        bindings_text += f"{'='*30}\n\n"
        bindings_text += "<b>КОМАНДЫ:</b>\n"
        bindings_text += "• <code>/srent_bind ИМЯ | ТИП | ЧАСЫ</code> - добавить привязку\n"
        bindings_text += "• <code>/srent_unbind ИМЯ</code> - удалить привязку"
        
        CARDINAL.telegram.bot.send_message(
            message.chat.id,
            bindings_text,
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка в обработчике list_bindings_cmd: {e}")
        try:
            CARDINAL.telegram.bot.send_message(message.chat.id, f"❌ Произошла ошибка: {e}")
        except:
            pass

def bind_lot_cmd(message):
    """Добавляет привязку лота к типу аккаунта"""
    try:
        text = message.text.strip()
        
        # Убираем команду из начала
        if text.startswith('/srent_bind'):
            text = text[len('/srent_bind'):].strip()
        
        if not text or '|' not in text:
            CARDINAL.telegram.bot.send_message(
                message.chat.id, 
                "❌ <b>Неверный формат команды</b>\n\n"
                "Используйте: <code>/srent_bind НАЗВАНИЕ_ЛОТА | ТИП | ЧАСЫ</code>\n\n"
                "Пример: <code>/srent_bind Аренда PUBG на 2 часа | PUBG | 2</code>\n\n"
                "• НАЗВАНИЕ_ЛОТА - точное название лота на FunPay\n"
                "• ТИП - тип аккаунта (например: PUBG, CSGO, REPO)\n"
                "• ЧАСЫ - срок аренды в часах",
                parse_mode="HTML"
            )
            return
        
        # Разбиваем на части по разделителю '|'
        parts = [part.strip() for part in text.split('|')]
        
        if len(parts) < 2:
            CARDINAL.telegram.bot.send_message(
                message.chat.id,
                "❌ Не указан тип аккаунта.\n\n"
                "Используйте формат: <code>/srent_bind НАЗВАНИЕ_ЛОТА | ТИП | ЧАСЫ</code>",
                parse_mode="HTML"
            )
            return
            
        lot_name = parts[0]
        account_type = parts[1]
        
        # Проверяем, указано ли количество часов
        duration_hours = 1  # По умолчанию 1 час
        if len(parts) > 2:
            try:
                duration_hours = int(parts[2])
                if duration_hours <= 0:
                    raise ValueError("Количество часов должно быть положительным")
            except ValueError:
                CARDINAL.telegram.bot.send_message(
                    message.chat.id,
                    "❌ Количество часов должно быть положительным числом.",
                    parse_mode="HTML"
                )
                return
        
        # Создаем привязку
        lot_bindings[lot_name] = {
            "account_type": account_type,
            "duration_hours": duration_hours
        }
        save_lot_bindings()
        
        CARDINAL.telegram.bot.send_message(
            message.chat.id,
            "✅ <b>Привязка успешно создана!</b>\n\n"
            f"🔹 Название лота: {lot_name}\n"
            f"🔹 Тип аккаунта: {account_type}\n"
            f"🔹 Срок аренды: {duration_hours} ч.\n\n"
            "Теперь при покупке этого лота будет автоматически выдан аккаунт.",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка в обработчике bind_lot_cmd: {e}")
        try:
            CARDINAL.telegram.bot.send_message(message.chat.id, f"❌ Произошла ошибка: {e}")
        except:
            pass

def help_lot_binding_cmd(message):
    """Отображает справку по привязкам лотов"""
    try:
        CARDINAL.telegram.bot.send_message(
            message.chat.id,
            "📚 <b>ПОМОЩЬ ПО ПРИВЯЗКАМ ЛОТОВ</b> 📚\n\n"
            f"{'='*30}\n\n"
            "<b>🔍 КАК РАБОТАЮТ ПРИВЯЗКИ:</b>\n\n"
            "Привязка позволяет автоматически выдавать аккаунты определенного типа при покупке лота на FunPay.\n\n"
            "При создании заказа система ищет соответствующую привязку по точному названию лота, затем автоматически выбирает свободный аккаунт нужного типа и выдает его покупателю на указанный срок.\n\n"
            f"{'='*30}\n\n"
            "<b>📋 ДОСТУПНЫЕ КОМАНДЫ:</b>\n\n"
            "• <code>/srent_bind ИМЯ | ТИП | ЧАСЫ</code>\n"
            "  📌 Создать привязку\n\n"
            "• <code>/srent_unbind ИМЯ</code>\n"
            "  📌 Удалить привязку\n\n"
            "• <code>/srent_bindings</code>\n"
            "  📌 Просмотреть все привязки\n\n"
            f"{'='*30}\n\n"
            "<b>📝 ПРИМЕРЫ ИСПОЛЬЗОВАНИЯ:</b>\n\n"
            "• <code>/srent_bind АРЕНДА PUBG НА 3 ЧАСА | PUBG | 3</code>\n\n"
            "• <code>/srent_bind АРЕНДА STEAM | REPO | 12</code>\n\n"
            f"{'='*30}\n\n"
            "<b>⚠️ ВАЖНАЯ ИНФОРМАЦИЯ:</b>\n\n"
            "• Название лота должно <b>точно</b> совпадать с названием на FunPay\n\n"
            "• Тип должен соответствовать существующим аккаунтам\n\n"
            "• Часы аренды должны быть положительным целым числом",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка в обработчике help_lot_binding_cmd: {e}")
        try:
            CARDINAL.telegram.bot.send_message(message.chat.id, f"❌ Произошла ошибка: {e}")
        except:
            pass

def help_lot_binding_callback(call):
    """Отображает справку по привязкам лотов через callback"""
    try:
        markup = InlineKeyboardMarkup()
        markup.row(InlineKeyboardButton("⬅️ Назад к привязкам", callback_data="srent_lot_bindings"))
        markup.row(InlineKeyboardButton("⬅️ Назад в меню", callback_data="srent_menu"))
        
        CARDINAL.telegram.bot.edit_message_text(
            "📚 <b>ПОМОЩЬ ПО ПРИВЯЗКАМ ЛОТОВ</b> 📚\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "<b>🔍 КАК РАБОТАЮТ ПРИВЯЗКИ:</b>\n\n"
            "Привязка позволяет автоматически выдавать аккаунты определенного типа при покупке лота на FunPay.\n\n"
            "При создании заказа система ищет соответствующую привязку по точному названию лота, затем автоматически выбирает свободный аккаунт нужного типа и выдает его покупателю на указанный срок.\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "<b>📋 СОЗДАНИЕ ПРИВЯЗКИ:</b>\n\n"
            "1. В меню нажмите на кнопку 'Добавить привязку лота'\n"
            "2. Введите <b>точное</b> название лота с FunPay\n"
            "3. Укажите тип аккаунта, который будет выдаваться\n"
            "4. Укажите срок аренды в часах\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "<b>⚠️ ВАЖНАЯ ИНФОРМАЦИЯ:</b>\n\n"
            "• Название лота должно <b>точно</b> совпадать с названием на FunPay\n\n"
            "• Тип должен соответствовать существующим аккаунтам\n\n"
            "• Часы аренды должны быть положительным целым числом",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode="HTML"
        )
        CARDINAL.telegram.bot.answer_callback_query(call.id, "Справка по привязкам")
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка отображения справки по привязкам: {e}")
        try:
            CARDINAL.telegram.bot.answer_callback_query(call.id, f"Ошибка: {str(e)[:50]}")
        except:
            pass

def manage_binding_callback(call, binding_hash):
    """Отображает меню управления для конкретной привязки лота"""
    try:
        # Проверяем, существует ли хеш в нашем словаре
        if binding_hash not in binding_hash_map:
            CARDINAL.telegram.bot.answer_callback_query(call.id, "Привязка не найдена")
            return
        
        # Получаем название лота по хешу
        lot_name = binding_hash_map[binding_hash]
        
        # Проверяем, существует ли привязка для указанного лота
        if lot_name not in lot_bindings:
            CARDINAL.telegram.bot.answer_callback_query(call.id, "Привязка не найдена")
            return
        
        # Получаем данные привязки
        binding = lot_bindings[lot_name]
        account_type = binding["account_type"]
        duration_hours = binding["duration_hours"]
        
        # Сокращаем длинные названия лотов для отображения
        display_name = lot_name
        if len(display_name) > 40:
            display_name = display_name[:37] + "..."
        
        # Создаем сообщение с информацией о привязке
        binding_text = "🔗 <b>УПРАВЛЕНИЕ ПРИВЯЗКОЙ</b>\n\n"
        binding_text += "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        binding_text += f"<b>📝 Название лота:</b>\n{display_name}\n\n"
        binding_text += f"<b>🔹 Тип аккаунта:</b> {account_type}\n"
        binding_text += f"<b>🔹 Срок аренды:</b> {duration_hours} ч.\n\n"
        
        # Создаем клавиатуру с кнопками управления
        markup = InlineKeyboardMarkup(row_width=2)
        
        # Кнопки для изменения параметров привязки
        markup.row(
            InlineKeyboardButton("Изменить тип 💡", callback_data=f"srent_edit_binding_type_{binding_hash}"),
            InlineKeyboardButton("Изменить время ⏰", callback_data=f"srent_edit_binding_time_{binding_hash}")
        )
        
        # Кнопка для удаления привязки
        markup.row(InlineKeyboardButton("Удалить привязку 📕", callback_data=f"srent_delete_binding_{binding_hash}"))
        
        # Кнопки для возврата
        markup.row(InlineKeyboardButton("⬅️ Назад к привязкам", callback_data="srent_lot_bindings"))
        
        # Отправляем сообщение
        CARDINAL.telegram.bot.edit_message_text(
            binding_text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode="HTML"
        )
        
        CARDINAL.telegram.bot.answer_callback_query(call.id, "Управление привязкой")
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка отображения управления привязкой: {e}")
        try:
            CARDINAL.telegram.bot.answer_callback_query(call.id, f"Ошибка: {str(e)[:50]}")
        except:
            pass

def start_add_binding_callback(call):
    """Начинает процесс добавления новой привязки лота"""
    try:
        chat_id = call.message.chat.id
        
        # Инициализируем состояние
        ADD_BINDING_STATES[chat_id] = {"state": "name", "data": {}}
        
        # Создаем клавиатуру для отмены
        markup = InlineKeyboardMarkup()
        markup.row(InlineKeyboardButton("❌ Отмена", callback_data="srent_cancel_binding"))
        
        # Редактируем сообщение
        CARDINAL.telegram.bot.edit_message_text(
            "🔗 <b>Добавление привязки лота</b>\n\n"
            "Шаг 1: Укажите <b>точное</b> название лота с FunPay.\n\n"
            "⚠️ Название должно <b>полностью</b> совпадать с названием на FunPay!",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode="HTML"
        )
        
        CARDINAL.telegram.bot.answer_callback_query(call.id, "Введите название лота")
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка начала добавления привязки: {e}")
        try:
            CARDINAL.telegram.bot.answer_callback_query(call.id, f"Ошибка: {str(e)[:50]}")
        except:
            pass

def handle_binding_add_steps(message):
    """Обрабатывает шаги добавления привязки лота"""
    chat_id = message.chat.id
    
    # Проверяем, находится ли пользователь в процессе добавления привязки
    if chat_id not in ADD_BINDING_STATES:
        return False
    
    # Получаем текущее состояние и данные
    state = ADD_BINDING_STATES[chat_id]["state"]
    data = ADD_BINDING_STATES[chat_id]["data"]
    
    # Проверяем запрос на отмену
    if message.text.lower() in ["отмена", "cancel", "/cancel", "/отмена"]:
        del ADD_BINDING_STATES[chat_id]
        CARDINAL.telegram.bot.send_message(
            chat_id,
            "❌ Процесс добавления привязки отменен.",
            parse_mode="HTML"
        )
        return True
    
    try:
        if state == "name":
            # Получаем название лота
            lot_name = message.text.strip()
            
            # Проверяем, не существует ли уже такой привязки
            if lot_name in lot_bindings:
                markup = InlineKeyboardMarkup()
                markup.row(InlineKeyboardButton("⬅️ К привязкам", callback_data="srent_lot_bindings"))
                
                CARDINAL.telegram.bot.send_message(
                    chat_id,
                    "⚠️ <b>Привязка с таким названием уже существует</b>\n\n"
                    "Пожалуйста, удалите существующую привязку перед созданием новой.",
                    reply_markup=markup,
                    parse_mode="HTML"
                )
                del ADD_BINDING_STATES[chat_id]
                return True
            
            data["name"] = lot_name
            ADD_BINDING_STATES[chat_id]["state"] = "type"
            
            # Получаем доступные типы аккаунтов для выбора
            available_types = set()
            for acc in rental_manager.accounts.values():
                available_types.add(acc.type)
            
            # Создаем сообщение с доступными типами
            type_message = "✅ Название лота сохранено.\n\n"
            type_message += "Шаг 2: Укажите <b>тип</b> аккаунта для выдачи.\n\n"
            
            if available_types:
                type_message += "<b>Доступные типы аккаунтов:</b>\n"
                for acc_type in available_types:
                    type_message += f"• <code>{acc_type}</code>\n"
            else:
                type_message += "⚠️ <b>В системе нет ни одного аккаунта.</b>\n"
                type_message += "Вы можете продолжить создание привязки, но для ее работы потребуется добавить аккаунты указанного типа."
            
            # Создаем клавиатуру с кнопкой отмены
            markup = InlineKeyboardMarkup()
            markup.row(InlineKeyboardButton("❌ Отмена", callback_data="srent_cancel_binding"))
            
            CARDINAL.telegram.bot.send_message(
                chat_id,
                type_message,
                reply_markup=markup,
                parse_mode="HTML"
            )
        
        elif state == "type":
            # Получаем тип аккаунта
            account_type = message.text.strip()
            data["type"] = account_type
            ADD_BINDING_STATES[chat_id]["state"] = "duration"
            
            # Создаем клавиатуру с кнопкой отмены и стандартными вариантами времени
            markup = InlineKeyboardMarkup(row_width=3)
            markup.row(
                InlineKeyboardButton("1 час", callback_data="srent_binding_duration_1"),
                InlineKeyboardButton("2 часа", callback_data="srent_binding_duration_2"),
                InlineKeyboardButton("3 часа", callback_data="srent_binding_duration_3")
            )
            markup.row(
                InlineKeyboardButton("6 часов", callback_data="srent_binding_duration_6"),
                InlineKeyboardButton("12 часов", callback_data="srent_binding_duration_12"),
                InlineKeyboardButton("24 часа", callback_data="srent_binding_duration_24")
            )
            markup.row(InlineKeyboardButton("❌ Отмена", callback_data="srent_cancel_binding"))
            
            CARDINAL.telegram.bot.send_message(
                chat_id,
                "✅ Тип аккаунта сохранен.\n\n"
                "Шаг 3: Укажите <b>срок аренды</b> в часах.\n\n"
                "Вы можете выбрать готовый вариант или ввести свое значение.",
                reply_markup=markup,
                parse_mode="HTML"
            )
        
        elif state == "duration":
            # Получаем срок аренды
            try:
                duration_hours = int(message.text.strip())
                if duration_hours <= 0:
                    raise ValueError("Срок аренды должен быть положительным числом")
            except ValueError:
                CARDINAL.telegram.bot.send_message(
                    chat_id,
                    "❌ <b>Некорректный ввод</b>\n\n"
                    "Срок аренды должен быть указан в виде целого положительного числа часов.\n"
                    "Пожалуйста, введите число заново.",
                    parse_mode="HTML"
                )
                return True
            
            # Сохраняем привязку
            lot_name = data["name"]
            account_type = data["type"]
            
            lot_bindings[lot_name] = {
                "account_type": account_type,
                "duration_hours": duration_hours
            }
            save_lot_bindings()
            
            # Очищаем состояние
            del ADD_BINDING_STATES[chat_id]
            
            # Создаем клавиатуру для перехода к списку привязок
            markup = InlineKeyboardMarkup()
            markup.row(InlineKeyboardButton("⬅️ К привязкам", callback_data="srent_lot_bindings"))
            
            CARDINAL.telegram.bot.send_message(
                chat_id,
                f"✅ <b>Привязка успешно создана!</b>\n\n"
                f"<b>🔹 Название лота:</b> {lot_name}\n"
                f"<b>🔹 Тип аккаунта:</b> {account_type}\n"
                f"<b>🔹 Срок аренды:</b> {duration_hours} ч.\n\n"
                f"Теперь при покупке этого лота будет автоматически выдан аккаунт типа {account_type} на {duration_hours} ч.",
                reply_markup=markup,
                parse_mode="HTML"
            )
        
        elif state == "edit_type":
            # Получаем новый тип аккаунта
            new_type = message.text.strip()
            lot_name = data["name"]
            binding_hash = data["hash"]
            
            # Обновляем тип аккаунта
            lot_bindings[lot_name]["account_type"] = new_type
            save_lot_bindings()
            
            # Очищаем состояние
            del ADD_BINDING_STATES[chat_id]
            
            # Создаем клавиатуру для перехода к управлению привязкой
            markup = InlineKeyboardMarkup()
            markup.row(InlineKeyboardButton("⬅️ К привязке", callback_data=f"srent_binding_{binding_hash}"))
            markup.row(InlineKeyboardButton("⬅️ К списку привязок", callback_data="srent_lot_bindings"))
            
            CARDINAL.telegram.bot.send_message(
                chat_id,
                f"✅ <b>Тип аккаунта успешно изменен!</b>\n\n"
                f"<b>🔹 Название лота:</b> {lot_name}\n"
                f"<b>🔹 Новый тип аккаунта:</b> {new_type}",
                reply_markup=markup,
                parse_mode="HTML"
            )
            
        elif state == "edit_duration":
            # Получаем новую длительность аренды
            try:
                new_duration = int(message.text.strip())
                if new_duration <= 0:
                    raise ValueError("Срок аренды должен быть положительным числом")
            except ValueError:
                CARDINAL.telegram.bot.send_message(
                    chat_id,
                    "❌ <b>Некорректный ввод</b>\n\n"
                    "Срок аренды должен быть указан в виде целого положительного числа часов.\n"
                    "Пожалуйста, введите число заново.",
                    parse_mode="HTML"
                )
                return True
            
            lot_name = data["name"]
            binding_hash = data["hash"]
            
            # Обновляем длительность аренды
            lot_bindings[lot_name]["duration_hours"] = new_duration
            save_lot_bindings()
            
            # Очищаем состояние
            del ADD_BINDING_STATES[chat_id]
            
            # Создаем клавиатуру для перехода к управлению привязкой
            markup = InlineKeyboardMarkup()
            markup.row(InlineKeyboardButton("⬅️ К привязке", callback_data=f"srent_binding_{binding_hash}"))
            markup.row(InlineKeyboardButton("⬅️ К списку привязок", callback_data="srent_lot_bindings"))
            
            CARDINAL.telegram.bot.send_message(
                chat_id,
                f"✅ <b>Срок аренды успешно изменен!</b>\n\n"
                f"<b>🔹 Название лота:</b> {lot_name}\n"
                f"<b>🔹 Новый срок аренды:</b> {new_duration} ч.",
                reply_markup=markup,
                parse_mode="HTML"
            )
        
        return True
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка в обработчике handle_binding_add_steps: {e}")
        try:
            CARDINAL.telegram.bot.send_message(chat_id, f"❌ Произошла ошибка: {e}")
        except:
            pass
        
        # В случае ошибки удаляем состояние
        if chat_id in ADD_BINDING_STATES:
            del ADD_BINDING_STATES[chat_id]
        
        return True

def cancel_binding_callback(call):
    """Отменяет процесс добавления привязки"""
    try:
        chat_id = call.message.chat.id
        
        # Проверяем, находится ли пользователь в процессе добавления привязки
        if chat_id in ADD_BINDING_STATES:
            del ADD_BINDING_STATES[chat_id]
        
        # Создаем клавиатуру для возврата к списку привязок
        markup = InlineKeyboardMarkup()
        markup.row(InlineKeyboardButton("⬅️ К привязкам", callback_data="srent_lot_bindings"))
        
        CARDINAL.telegram.bot.edit_message_text(
            "❌ <b>Процесс добавления привязки отменен.</b>",
            chat_id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode="HTML"
        )
        CARDINAL.telegram.bot.answer_callback_query(call.id, "Добавление отменено")
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка в обработчике cancel_binding_callback: {e}")
        try:
            CARDINAL.telegram.bot.answer_callback_query(call.id, f"Ошибка: {str(e)[:50]}")
        except:
            pass

def binding_duration_callback(call):
    """Обрабатывает выбор стандартной длительности аренды при создании привязки"""
    try:
        chat_id = call.message.chat.id
        
        # Проверяем, находится ли пользователь в процессе добавления или редактирования привязки
        if chat_id not in ADD_BINDING_STATES:
            CARDINAL.telegram.bot.answer_callback_query(call.id, "Нет активного процесса работы с привязкой")
            return
        
        # Получаем длительность из callback data
        duration_str = call.data.replace("srent_binding_duration_", "")
        try:
            duration_hours = int(duration_str)
        except ValueError:
            CARDINAL.telegram.bot.answer_callback_query(call.id, "Некорректная длительность")
            return
        
        # Получаем данные привязки и состояние
        data = ADD_BINDING_STATES[chat_id]["data"]
        state = ADD_BINDING_STATES[chat_id]["state"]
        
        if state == "duration":
            # Создание новой привязки
            lot_name = data["name"]
            account_type = data["type"]
            
            # Создаем привязку
            lot_bindings[lot_name] = {
                "account_type": account_type,
                "duration_hours": duration_hours
            }
            save_lot_bindings()
            
            # Очищаем состояние
            del ADD_BINDING_STATES[chat_id]
            
            # Создаем клавиатуру для перехода к списку привязок
            markup = InlineKeyboardMarkup()
            markup.row(InlineKeyboardButton("⬅️ К привязкам", callback_data="srent_lot_bindings"))
            
            CARDINAL.telegram.bot.edit_message_text(
                f"✅ <b>Привязка успешно создана!</b>\n\n"
                f"<b>🔹 Название лота:</b> {lot_name}\n"
                f"<b>🔹 Тип аккаунта:</b> {account_type}\n"
                f"<b>🔹 Срок аренды:</b> {duration_hours} ч.\n\n"
                f"Теперь при покупке этого лота будет автоматически выдан аккаунт типа {account_type} на {duration_hours} ч.",
                chat_id,
                call.message.message_id,
                reply_markup=markup,
                parse_mode="HTML"
            )
            
        elif state == "edit_duration":
            # Редактирование длительности существующей привязки
            lot_name = data["name"]
            binding_hash = data["hash"]
            
            # Обновляем длительность аренды
            lot_bindings[lot_name]["duration_hours"] = duration_hours
            save_lot_bindings()
            
            # Очищаем состояние
            del ADD_BINDING_STATES[chat_id]
            
            # Создаем клавиатуру для перехода к управлению привязкой
            markup = InlineKeyboardMarkup()
            markup.row(InlineKeyboardButton("⬅️ К привязке", callback_data=f"srent_binding_{binding_hash}"))
            markup.row(InlineKeyboardButton("⬅️ К списку привязок", callback_data="srent_lot_bindings"))
            
            CARDINAL.telegram.bot.edit_message_text(
                f"✅ <b>Срок аренды успешно изменен!</b>\n\n"
                f"<b>🔹 Название лота:</b> {lot_name}\n"
                f"<b>🔹 Новый срок аренды:</b> {duration_hours} ч.",
                chat_id,
                call.message.message_id,
                reply_markup=markup,
                parse_mode="HTML"
            )
        
        CARDINAL.telegram.bot.answer_callback_query(call.id, "Длительность установлена!")
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка в обработчике binding_duration_callback: {e}")
        try:
            CARDINAL.telegram.bot.answer_callback_query(call.id, f"Ошибка: {str(e)[:50]}")
        except:
            pass

def show_all_bindings_callback(call):
    """Показывает полный список всех привязок"""
    try:
        # Пока просто отвечаем на callback, позже реализуем полный функционал
        CARDINAL.telegram.bot.answer_callback_query(call.id, "Функция просмотра всех привязок будет доступна в следующем обновлении")
        
        # Возвращаемся к стандартному списку привязок
        show_lot_bindings_callback(call)
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка отображения всех привязок: {e}")
        try:
            CARDINAL.telegram.bot.answer_callback_query(call.id, f"Ошибка: {str(e)[:50]}")
        except:
            pass

# Дополнительные функции для управления привязками лотов
def edit_binding_type_callback(call, binding_hash):
    """Редактирует тип аккаунта привязки лота"""
    try:
        # Проверяем, существует ли хеш в нашем словаре
        if binding_hash not in binding_hash_map:
            CARDINAL.telegram.bot.answer_callback_query(call.id, "Привязка не найдена")
            return
        
        # Получаем название лота по хешу
        lot_name = binding_hash_map[binding_hash]
        
        # Проверяем, существует ли привязка для указанного лота
        if lot_name not in lot_bindings:
            CARDINAL.telegram.bot.answer_callback_query(call.id, "Привязка не найдена")
            return
        
        # Получаем текущий тип и данные привязки
        binding = lot_bindings[lot_name]
        current_type = binding["account_type"]
        
        # Инициализируем состояние для редактирования типа
        chat_id = call.message.chat.id
        ADD_BINDING_STATES[chat_id] = {
            "state": "edit_type",
            "data": {
                "name": lot_name,
                "current_type": current_type,
                "hash": binding_hash
            }
        }
        
        # Получаем доступные типы аккаунтов для выбора
        available_types = set()
        for acc in rental_manager.accounts.values():
            available_types.add(acc.type)
        
        # Создаем сообщение с доступными типами
        type_message = f"✏️ <b>Редактирование типа привязки</b>\n\n"
        type_message += f"Лот: <b>{lot_name}</b>\n"
        type_message += f"Текущий тип: <b>{current_type}</b>\n\n"
        type_message += "Введите <b>новый тип</b> аккаунта для выдачи.\n\n"
        
        if available_types:
            type_message += "<b>Доступные типы аккаунтов:</b>\n"
            for acc_type in available_types:
                type_message += f"• <code>{acc_type}</code>\n"
        else:
            type_message += "⚠️ <b>В системе нет ни одного аккаунта.</b>\n"
        
        # Создаем клавиатуру с кнопкой отмены
        markup = InlineKeyboardMarkup()
        markup.row(InlineKeyboardButton("❌ Отмена", callback_data=f"srent_binding_{binding_hash}"))
        
        CARDINAL.telegram.bot.edit_message_text(
            type_message,
            chat_id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode="HTML"
        )
        
        CARDINAL.telegram.bot.answer_callback_query(call.id, "Введите новый тип аккаунта")
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка редактирования типа привязки: {e}")
        try:
            CARDINAL.telegram.bot.answer_callback_query(call.id, f"Ошибка: {str(e)[:50]}")
        except:
            pass

def edit_binding_time_callback(call, binding_hash):
    """Редактирует время аренды привязки лота"""
    try:
        # Проверяем, существует ли хеш в нашем словаре
        if binding_hash not in binding_hash_map:
            CARDINAL.telegram.bot.answer_callback_query(call.id, "Привязка не найдена")
            return
        
        # Получаем название лота по хешу
        lot_name = binding_hash_map[binding_hash]
        
        # Проверяем, существует ли привязка для указанного лота
        if lot_name not in lot_bindings:
            CARDINAL.telegram.bot.answer_callback_query(call.id, "Привязка не найдена")
            return
        
        # Получаем текущую длительность аренды
        binding = lot_bindings[lot_name]
        current_duration = binding["duration_hours"]
        
        # Инициализируем состояние для редактирования длительности
        chat_id = call.message.chat.id
        ADD_BINDING_STATES[chat_id] = {
            "state": "edit_duration",
            "data": {
                "name": lot_name,
                "current_duration": current_duration,
                "hash": binding_hash
            }
        }
        
        # Создаем клавиатуру с кнопкой отмены и стандартными вариантами времени
        markup = InlineKeyboardMarkup(row_width=3)
        markup.row(
            InlineKeyboardButton("1 час", callback_data="srent_binding_duration_1"),
            InlineKeyboardButton("2 часа", callback_data="srent_binding_duration_2"),
            InlineKeyboardButton("3 часа", callback_data="srent_binding_duration_3")
        )
        markup.row(
            InlineKeyboardButton("6 часов", callback_data="srent_binding_duration_6"),
            InlineKeyboardButton("12 часов", callback_data="srent_binding_duration_12"),
            InlineKeyboardButton("24 часа", callback_data="srent_binding_duration_24")
        )
        markup.row(InlineKeyboardButton("❌ Отмена", callback_data=f"srent_binding_{binding_hash}"))
        
        CARDINAL.telegram.bot.edit_message_text(
            f"⏱ <b>Редактирование времени аренды</b>\n\n"
            f"Лот: <b>{lot_name}</b>\n"
            f"Текущее время: <b>{current_duration} ч.</b>\n\n"
            "Укажите <b>новый срок аренды</b> в часах.\n\n"
            "Вы можете выбрать готовый вариант или ввести свое значение.",
            chat_id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode="HTML"
        )
        
        CARDINAL.telegram.bot.answer_callback_query(call.id, "Укажите новый срок аренды")
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка редактирования времени привязки: {e}")
        try:
            CARDINAL.telegram.bot.answer_callback_query(call.id, f"Ошибка: {str(e)[:50]}")
        except:
            pass

def delete_binding_callback(call, binding_hash):
    """Удаляет привязку лота"""
    try:
        # Проверяем, существует ли хеш в нашем словаре
        if binding_hash not in binding_hash_map:
            CARDINAL.telegram.bot.answer_callback_query(call.id, "Привязка не найдена")
            return
        
        # Получаем название лота по хешу
        lot_name = binding_hash_map[binding_hash]
        
        # Проверяем, существует ли привязка для указанного лота
        if lot_name not in lot_bindings:
            CARDINAL.telegram.bot.answer_callback_query(call.id, "Привязка не найдена")
            return
        
        # Сохраняем данные перед удалением для отображения
        binding = lot_bindings[lot_name]
        account_type = binding.get("account_type", "Не указан")
        duration_hours = binding.get("duration_hours", 0)
        
        # Удаляем привязку
        del lot_bindings[lot_name]
        save_lot_bindings()
        
        # Обновляем хеш-карту
        binding_hash_map.pop(binding_hash, None)
        
        # Создаем клавиатуру для возврата к списку привязок
        markup = InlineKeyboardMarkup()
        markup.row(InlineKeyboardButton("⬅️ К привязкам", callback_data="srent_lot_bindings"))
        
        CARDINAL.telegram.bot.edit_message_text(
            "✅ <b>Привязка удалена!</b>\n\n"
            f"<b>🔹 Название лота:</b> {lot_name}\n"
            f"<b>🔹 Тип аккаунта:</b> {account_type}\n"
            f"<b>🔹 Срок аренды:</b> {duration_hours} ч.",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup,
            parse_mode="HTML"
        )
        
        CARDINAL.telegram.bot.answer_callback_query(call.id, "Привязка успешно удалена")
    except Exception as e:
        logger.error(f"{LOGGER_PREFIX} Ошибка удаления привязки: {e}")
        try:
            CARDINAL.telegram.bot.answer_callback_query(call.id, f"Ошибка: {str(e)[:50]}")
        except:
            pass
