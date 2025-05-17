import os
import asyncio
import json
import re
import warnings
import sqlite3
import logging
from functools import wraps
from datetime import datetime, timezone, timedelta
from aiolimiter import AsyncLimiter
from telegram.warnings import PTBUserWarning
warnings.filterwarnings("ignore", category=PTBUserWarning)
from typing import Dict, List, Optional, Tuple
from pyrogram import Client
from handlers import setup_handlers
from telegram.error import BadRequest
from pyrogram.errors import FloodWait, PeerIdInvalid, UserDeactivated, UsernameNotOccupied
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, ContextTypes

# --- CONFIGURATION ---

BOT_TOKEN="7300919682:AAFIj1S5VF1IVBnrNhgOLqvZhNlSTL6qdJQ"
GROUP_ID=-1002525488393
ADMIN_ID = [543583405]
DB_PATH = "users.db"
MAX_USERS_PER_MANAGER = 10

limiter = AsyncLimiter(1, 3)

# ConversationHandler
(
    MAIN_MENU,
    INPUT_CONTACTS,
    INPUT_MESSAGE,
    INPUT_LINKS,
    SETTINGS_MENU,
    MANAGE_CONTACTS,
    BROADCAST_CONFIRM,
    EXTERNAL_INPUT_CONTACTS,
    EXTERNAL_INPUT_MESSAGE,
    EXTERNAL_INPUT_LINKS,
    EXTERNAL_INPUT_LIMIT,
    EXTERNAL_BROADCAST_CONFIRM,
    INTERNAL_BROADCAST_CONFIRM,
    DELETE_CONTACT
) = range(14)

def private_only(func):
    @wraps(func)
    async def wrapper(self, update, context, *args, **kwargs):
        if update.effective_chat.type != "private":
            return
        return await func(self, update, context, *args, **kwargs)
    return wrapper
def allowed_users_only(func):
    @wraps(func)
    async def wrapper(self, update, context, *args, **kwargs):
        user_id = update.effective_user.id if update.effective_user else None
        if user_id not in ADMIN_ID:
            if update.message:
                await update.message.reply_text("⛔ You are not an admin.")
            elif update.callback_query:
                await update.callback_query.answer("⛔ You are not an admin.", show_alert=True)
            return
        return await func(self, update, context, *args, **kwargs)
    return wrapper

class ExternalAccountManager:
    def __init__(self):
        self.active_accounts = {}
        self.account_configs = []
        self.session_dir = "external_sessions"

    async def load_accounts(self, config_file: str = "external_accounts.json"):
        if not os.path.exists(config_file):
            raise FileNotFoundError(f"Файл конфигурации {config_file} не найден")
        with open(config_file, "r", encoding="utf-8") as f:
            self.account_configs = json.load(f)
        print(f"Загружено {len(self.account_configs)} аккаунтов для внешней рассылки")

    async def initialize_accounts(self):
        if not os.path.exists(self.session_dir):
            os.makedirs(self.session_dir)
        for config in self.account_configs:
            try:
                client = Client(
                    name=config["name"],
                    api_id=config["api_id"],
                    api_hash=config["api_hash"],
                    workdir=self.session_dir,
                    no_updates=True,
                    proxy=config.get('proxy')
                )
                self.active_accounts[config["name"]] = client
                await client.start()
            except Exception as e:
                print(f"Failed to initialize external account {config['name']}: {e}")

    def get_account(self, name: str) -> Client:
        return self.active_accounts.get(name)

    async def stop_all(self):
        for name, client in self.active_accounts.items():
            if client.is_connected:
                await client.stop()
                print(f"External account {name} stopped")

class InternalDB:
    def __init__(self, db_path=DB_PATH):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.create_tables()

    def create_tables(self):
        with self.conn:
            self.conn.execute(
                """CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    manager_login TEXT NOT NULL,
                    joined_at TEXT NOT NULL
                )"""
            )
            self.conn.execute(
                """CREATE TABLE IF NOT EXISTS managers (
                    login TEXT PRIMARY KEY,
                    assigned_count INTEGER NOT NULL DEFAULT 0
                )"""
            )
            self.conn.execute(
                """CREATE TABLE IF NOT EXISTS external_broadcast_sent (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    sent_at TEXT
                )"""
            )

    def get_free_manager(self) -> Optional[str]:
        with self.conn:
            row = self.conn.execute(
                "SELECT login FROM managers WHERE assigned_count < ? ORDER BY assigned_count ASC LIMIT 1",
                (MAX_USERS_PER_MANAGER,)
            ).fetchone()
        return row[0] if row else None

    def add_manager(self, login: str):
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO managers (login, assigned_count) VALUES (?, 0)", (login,)
            )

    def assign_user(self, user_id: int, manager_login: str):
        now = datetime.now(timezone.utc).isoformat()
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO users (user_id, manager_login, joined_at) VALUES (?, ?, ?)",
                (user_id, manager_login, now)
            )
            self.conn.execute(
                "UPDATE managers SET assigned_count = assigned_count + 1 WHERE login = ?",
                (manager_login,)
            )

    def user_exists(self, user_id: int) -> bool:
        with self.conn:
            row = self.conn.execute(
                "SELECT 1 FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
        return bool(row)

    def get_user_manager(self, user_id: int) -> Optional[str]:
        with self.conn:
            row = self.conn.execute(
                "SELECT manager_login FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
        return row[0] if row else None

    def get_manager_users(self, manager_login: str) -> List[int]:
        with self.conn:
            rows = self.conn.execute(
                "SELECT user_id FROM users WHERE manager_login = ?", (manager_login,)
            ).fetchall()
        return [row[0] for row in rows]

    def was_external_broadcast_sent(self, user_id: int) -> bool:
        with self.conn:
            row = self.conn.execute(
                "SELECT 1 FROM external_broadcast_sent WHERE user_id = ?", (user_id,)
            ).fetchone()
        return bool(row)

    def mark_external_broadcast_sent(self, user_id: int, username: str = None):
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO external_broadcast_sent (user_id, username, sent_at) VALUES (?, ?, ?)",
                (user_id, username, datetime.now(timezone.utc).isoformat())
            )
    async def remove_user(self, identifier, pyrogram_client=None):
        if str(identifier).isdigit():
            user_id = int(identifier)
        else:
            if pyrogram_client is None:
                raise ValueError("pyrogram_client required for username lookup")
            username = str(identifier).lstrip("@")
            try:
                user = await pyrogram_client.get_users(username)
                user_id = user.id
            except Exception as e:
                print(f"⚠️ Не удалось получить user_id по username {identifier}: {e}")
                return False

        with self.conn:
            row = self.conn.execute(
                "SELECT manager_login FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            if row:
                manager_login = row[0]
                self.conn.execute(
                    "DELETE FROM users WHERE user_id = ?", (user_id,)
                )
                self.conn.execute(
                    "UPDATE managers SET assigned_count = assigned_count - 1 WHERE login = ? AND assigned_count > 0",
                    (manager_login,)
                )
                return True
        return False

    def user_in_db(self, user_id: int) -> bool:
        return self.user_exists(user_id)

    def all_users(self) -> List[Tuple[int, str]]:
        with self.conn:
            rows = self.conn.execute("SELECT user_id, manager_login FROM users").fetchall()
        return rows

    def close(self):
        self.conn.close()

class AccountManager:
    def __init__(self):
        self.active_accounts: Dict[str, Client] = {}
        self.account_configs: List[dict] = []
        self.current_account_index = 0
        self.session_dir = "sessions"

    async def load_accounts(self, config_file: str = "accounts.json"):
        if os.path.exists(config_file):
            with open(config_file, 'r') as f:
                self.account_configs = json.load(f)
            print(f"Загружено {len(self.account_configs)} аккаунтов")
        else:
            raise FileNotFoundError(f"Файл конфигурации {config_file} не найден")

    async def initialize_accounts(self):
        if not os.path.exists(self.session_dir):
            os.makedirs(self.session_dir)

        for config in self.account_configs:
            try:
                client = Client(
                    name=f"{config['name']}",
                    api_id=config['api_id'],
                    api_hash=config['api_hash'],
                    workdir=self.session_dir,
                    no_updates=True,
                    proxy = config.get('proxy')
                )
                self.active_accounts[config['name']] = client
                await client.start()
            except Exception as e:
                print(f"Ошибка инициализации аккаунта {config['name']}: {e}")

    async def switch_account(self, account_name: str):
        if account_name not in self.active_accounts:
            raise ValueError(f"Аккаунт {account_name} не найден")

        new_client = self.active_accounts[account_name]
        if not new_client.is_connected:
            await new_client.start()

        self.current_account_index = list(self.active_accounts.keys()).index(account_name)
        print(f"Переключено на аккаунт: {account_name}")

    def get_current_account(self) -> Client:
        account_names = list(self.active_accounts.keys())
        if not account_names:
            raise Exception("Нет активных аккаунтов. Авторизуйте хотя бы один аккаунт.")
        if self.current_account_index >= len(account_names) or self.current_account_index < 0:
            self.current_account_index = 0
        return self.active_accounts[account_names[self.current_account_index]]

    async def stop_all(self):
        for name, client in self.active_accounts.items():
            if client.is_connected:
                await client.stop()
                print(f"Аккаунт {name} остановлен")

    def get_accounts(self):
        return [
            {"name": name, "id": name}
            for name in self.active_accounts.keys()
        ]

    async def send_welcome_message(self, account_name: str, username: str, message_links: list):
        if account_name not in self.active_accounts:
            raise ValueError(f"Аккаунт {account_name} не найден")
        client = self.active_accounts[account_name]
        if not client.is_connected:
            await client.start()

        await asyncio.sleep(15)

        for link in message_links:
            try:
                chat_id, message_id = MessageProcessor.parse_telegram_link(link)
                await client.copy_message(
                    chat_id=username,
                    from_chat_id=chat_id,
                    message_id=message_id
                )
            except Exception as e:
                print(f"[ERROR] Не удалось переслать сообщение {link}: {e}")
            await asyncio.sleep(3)


class ConfigManager:
    def __init__(self, config_file="internal_config.json"):
        self.config_file = config_file
        self.default_config = {
            "message": "",
            "interval": 15,
            "message_links": [],
            "broadcast_mode": "message"
        }

    async def load(self) -> dict:
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, "r", encoding="utf-8") as f:
                    config = json.load(f)
                    if isinstance(config.get("message_links"), str):
                        config["message_links"] = [config["message_links"]]
                    return {**self.default_config, **config}
            return self.default_config.copy()
        except Exception as e:
            print(f"Ошибка загрузки конфигурации: {e}")
            return self.default_config.copy()

    async def save(self, config: dict):
        try:
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
            print("Конфигурация успешно сохранена")
        except Exception as e:
            print(f"Ошибка сохранения конфигурации: {e}")

class ContactManager:
    def __init__(self, config_manager: ConfigManager):
        self.config_manager = config_manager

    async def get_contacts(self) -> List[str]:
        config = await self.config_manager.load()
        return config["contacts"]

    async def has_contacts(self) -> bool:
        config = await self.config_manager.load()
        return bool(config["contacts"])


class MessageProcessor:
    def __init__(self, account_manager: AccountManager):
        self.account_manager = account_manager

    @staticmethod
    def parse_telegram_link(link: str) -> Optional[tuple]:
        if not isinstance(link, str):
            return None
        patterns = [
            r"https://t\.me/(?:c/)?(\d+)/(\d+)",
            r"https://t\.me/[a-zA-Z0-9_]+/(\d+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, link)
            if match:
                try:
                    chat_id = match.group(1)
                    if chat_id.isdigit():
                        chat_id = int("-100" + chat_id)
                    message_id = int(match.group(2))
                    return message_id
                except (IndexError, ValueError):
                    continue
        return None

    async def verify_contact(self, contact: str) -> Optional[int]:
        client = self.account_manager.get_current_account()
        try:
            user = await client.get_users(contact)
            return user.id
        except Exception as e:
            print(f"Ошибка проверки контакта {contact}: {e}")
            return None

    async def process_message_link(self, link: str, contact_id: int) -> bool:
        parsed = self.parse_telegram_link(link)
        if not parsed:
            print(f"Неверный формат ссылки: {link}")
            return False
        chat_id, message_id = parsed
        client = self.account_manager.get_current_account()
        try:
            await client.copy_message(
                chat_id=contact_id,
                from_chat_id=chat_id,
                message_id=message_id
            )
            return True
        except Exception as e:
            print(f"Ошибка при отправке {link}: {e}")
            return False

    async def send_text_message(self, contact_id: int, text: str) -> bool:
        client = self.account_manager.get_current_account()
        try:
            await client.send_message(contact_id, text)
            return True
        except Exception as e:
            print(f"Ошибка при отправке сообщения: {e}")
            return False

class BroadcastBot:
    def __init__(self):
        self.account_manager = AccountManager()
        self.external_account_manager = ExternalAccountManager()
        self.config_manager = ConfigManager()
        self.contact_manager = ContactManager(self.config_manager)
        self.message_processor = MessageProcessor(self.account_manager)
        self.ptb_app = Application.builder().token(BOT_TOKEN).build()
        self.current_tasks: Dict[int, asyncio.Task] = {}
        self.user_data: Dict[int, dict] = {}
        self.db = InternalDB()
        self.manager_names = []
    @staticmethod
    def is_telegram_link(link: str) -> bool:
        pattern = r"^https:\/\/t\.me\/((c\/\d+)|([a-zA-Z0-9_]+))\/\d+$"
        return bool(re.match(pattern, link.strip()))

    def setup_handlers(self):
        setup_handlers(self)
    async def sync_managers(self):
        self.manager_names = list(self.account_manager.active_accounts.keys())
        for login in self.manager_names:
            self.db.add_manager(login)
    @private_only
    @allowed_users_only
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user = update.effective_user
        await update.message.reply_text(
            f"👋 Приветствую, {user.first_name}! Здесь можно настроить и\n запустить авторассылку.",
            reply_markup=self._create_main_menu()
        )
        return MAIN_MENU

    async def on_new_member(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        for member in update.message.new_chat_members:
            user_id = member.id
            username = member.username
            if not username:
                admin_alert = (
                    f"❗В группу вступил пользователь без username.\n"
                    f"ID: {user_id}\n"
                    f"Имя: {member.full_name}"
                )
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=admin_alert,
                    parse_mode="HTML"
                )
            if self.db.user_exists(user_id):
                continue
            manager_login = self.db.get_free_manager()
            if not manager_login:
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text="❗В группе не осталось свободных менеджеров.",
                    parse_mode="HTML"
                )
                continue
            self.db.assign_user(user_id, manager_login)

            config = await self.config_manager.load()
            message_links = [""]
            asyncio.create_task(
                self.account_manager.send_welcome_message(manager_login, username, message_links)
            )

    async def show_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        try:
            with open("external_config.json", "r", encoding="utf-8") as f:
                external_config = json.load(f)
            ext_mode = external_config.get('broadcast_mode', 'message')
            ext_mode_text = "Текст" if ext_mode == "message" else "Пересылка"
            ext_contacts = external_config.get('contacts', [])
            ext_message = external_config.get('message', '')
            ext_links = external_config.get('message_links', [])
            ext_interval = external_config.get('interval', None)
            limit = external_config.get('limit_per_userbot', '—')
        except Exception as e:
            ext_mode_text = "—"
            ext_contacts = []
            ext_message = f"Ошибка чтения external_config.json: {e}"
            ext_links = []
            ext_interval = "—"
            limit = "—"

        if ext_mode == "message":
            content_block = (
                f"📝 Текст рассылки:\n"
                f"{ext_message[:70] + '...' if ext_message and len(ext_message) > 70 else ext_message or 'не задан'}"
            )
        else:
            links_str = (
                "\n".join(ext_links[:5]) + ("\n..." if len(ext_links) > 5 else "")
                if ext_links else "не заданы"
            )
            content_block = f"🔗 Ссылки для пересылки:\n{links_str}"

        text = (
            "⚙ Настройки внешней рассылки:\n\n"
            f"🌐 Аккаунтов внешней рассылки: {len(self.external_account_manager.active_accounts)}\n"
            f"🔰 Режим: {ext_mode_text}\n"
            f"{content_block}\n\n"
            f"👥 Количество контактов: {len(ext_contacts)}\n"
            f"⏱ Интервал отправки: {ext_interval} сек\n"
            f"🚫 Лимит на менеджера: {limit} аккаунтов"
        )
        try:
            await query.edit_message_text(
                text,
                reply_markup=self._create_main_menu()
            )
        except BadRequest as e:
            if "Message is not modified" in str(e):
                pass
        return MAIN_MENU

    async def external_settings_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(
            "⚙ Настройки внешней рассылки:",
            reply_markup=self._create_external_settings_menu()
        )
        return MAIN_MENU

    async def delete_contact_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(
            "Введите user_id или @username контакта, которого нужно удалить:",
            reply_markup=self._create_cancel_button()
        )
        return DELETE_CONTACT

    @staticmethod
    async def safe_resolve_username(pyrogram_client, username):
        async with limiter:
            try:
                user = await pyrogram_client.get_users(username)
                return user.id
            except FloodWait as e:
                print(f"FLOOD_WAIT {e.value}s для {username}")
                await asyncio.sleep(e.value)
            except UsernameNotOccupied:
                print(f"{username} не существует")
                return None
            except Exception as ex:
                print(f"Ошибка {username}: {ex}")
                return None
    async def get_valid_unsent_users(self, usernames: list, pyrogram_client) -> list:
        valid_users = []
        for username in usernames:
            uname = username.lstrip("@")
            user_id = await self.safe_resolve_username(pyrogram_client, uname)
            if user_id is not None:
                if not self.db.user_exists(user_id) and not self.db.was_external_broadcast_sent(user_id):
                    valid_users.append((user_id, uname))
        return valid_users

    def distribute_among_accounts(self, userbots: list, users: list, limit_per_userbot: int):
        distributed = []
        idx = 0
        for client in userbots:
            chunk = users[idx:idx + limit_per_userbot]
            if not chunk:
                break
            distributed.append((client, chunk))
            idx += limit_per_userbot
        return distributed

    async def external_broadcast_to_chunk(self, client, user_chunk, message, delay):
        sent, failed = 0, 0
        for user_id, username in user_chunk:
            try:
                await client.send_message(user_id, message)
                self.db.mark_external_broadcast_sent(user_id, username)
                sent += 1
            except Exception as e:
                print(f"Ошибка отправки {user_id}: {e}")
                failed += 1
            await asyncio.sleep(delay)
        return sent, failed

    async def internal_check_config_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()

        config = await self.config_manager.load()
        mode = config.get('broadcast_mode', 'message')
        mode_text = "Текст" if mode == "message" else "Пересылка"

        if mode == "message":
            content = (
                f"📝 Текст для рассылки:\n"
                f"{config.get('message', '')[:70] + '...' if config.get('message') and len(config.get('message')) > 70 else config.get('message', 'не задан')}"
            )
        else:
            links = config.get('message_links', [])
            content = "🔗 Ссылки для пересылки:\n" + (
                "\n".join(links[:5]) + ("\n..." if len(links) > 5 else "") if links else "не заданы")

        text = (
            f"⚙ Настройки внутренней рассылки:\n\n"
            f"🔰 Режим: {mode_text}\n"
            f"{content}\n"
            f"⏱ Интервал: {config.get('interval', '—')} сек"
        )
        try:
            await query.edit_message_text(
                text,
                reply_markup=self._create_internal_broadcast_panel()
            )
        except BadRequest as e:
            if "Message is not modified" in str(e):
                pass
        return MAIN_MENU
    async def settings_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(
            "⚙ Настройки внутренней рассылки:",
            reply_markup=await self._create_settings_menu()
        )
        return SETTINGS_MENU

    async def clear_db_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        try:
            with self.db.conn:
                self.db.conn.execute("DELETE FROM users")
                self.db.conn.execute("UPDATE managers SET assigned_count = 0")
            await query.edit_message_text(
                "✅ База данных очищена.",
                reply_markup=await self._create_settings_menu()
            )
        except Exception as e:
            await query.edit_message_text(
                f"❌ Ошибка при очистке базы: {e}",
                reply_markup=await self._create_settings_menu()
            )
        return SETTINGS_MENU

    async def external_switch_broadcast_mode_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        config_path = "external_config.json"
        try:
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
            else:
                config = {}
            current_mode = config.get("broadcast_mode", "message")
            new_mode = "links" if current_mode == "message" else "message"
            config["broadcast_mode"] = new_mode
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
            mode_text = "Текст" if new_mode == "message" else "Пересылка"
            await query.edit_message_text(
                f"Режим переключён на: <b>{mode_text}</b>.",
                reply_markup=self._create_external_settings_menu(),
                parse_mode="HTML"
            )
        except Exception as e:
            await query.edit_message_text(
                f"❌ Ошибка при переключении режима: {e}",
                reply_markup=self._create_external_settings_menu()
            )
        return MAIN_MENU

    def _create_external_settings_menu(self):
        try:
            with open("external_config.json", "r", encoding="utf-8") as f:
                config = json.load(f)
            mode = config.get("broadcast_mode", "message")
        except Exception:
            mode = "message"
        mode_text = "Текст" if mode == "message" else "Пересылка"
        keyboard = [
            [InlineKeyboardButton("✏ Задать текст", callback_data="external_edit_text"), InlineKeyboardButton("🔗 Задать ссылки", callback_data="external_edit_links"),],
            [InlineKeyboardButton("👥 Ввести/изменить контакты", callback_data="external_set_contacts")],
            [InlineKeyboardButton("🔢 Установить лимит", callback_data="external_edit_limit")],
            [InlineKeyboardButton(f"🔰 Режим: {mode_text}", callback_data="external_switch_broadcast_mode")],
            [InlineKeyboardButton("⬅ Назад", callback_data="main_menu")]
        ]
        return InlineKeyboardMarkup(keyboard)

    async def external_edit_limit_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        try:
            with open("external_config.json", "r", encoding="utf-8") as f:
                config = json.load(f)
            current_limit = config.get("limit_per_userbot", 5)
        except Exception:
            current_limit = 5
        await query.edit_message_text(
            f"Текущий лимит на одного менеджера: <b>{current_limit}</b>\n\n"
            "Введите новое число:",
            reply_markup=self._create_cancel_button(),
            parse_mode="HTML"
        )
        return EXTERNAL_INPUT_LIMIT

    @private_only
    async def process_external_limit(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        text = update.message.text.strip()
        if not text.isdigit() or int(text) <= 0:
            await update.message.reply_text(
                "❌ Введите целое число больше 0.",
                reply_markup=self._create_external_settings_menu()
            )
            return MAIN_MENU

        limit = int(text)
        config_path = "external_config.json"
        try:
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
            else:
                config = {}
            config["limit_per_userbot"] = limit
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
            await update.message.reply_text(
                f"✅ Лимит на менеджера установлен: {limit}",
                reply_markup=self._create_external_settings_menu()
            )
        except Exception as e:
            await update.message.reply_text(
                f"❌ Ошибка при сохранении лимита: {e}",
                reply_markup=self._create_external_settings_menu()
            )
        return MAIN_MENU

    @private_only
    async def process_delete_contact(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user_input = update.message.text.strip()
        client = self.account_manager.get_current_account()
        is_user_id = user_input.isdigit()
        is_username = re.match(r"^@[a-zA-Z0-9_]{4,32}$", user_input)
        if not is_user_id and not is_username:
            await update.message.reply_text(
                "❌ Введите корректный user_id (цифры) или @username (4-32 символа).",
                reply_markup=self._create_contacts_menu()
            )
            return MANAGE_CONTACTS
        try:
            result = await self.db.remove_user(user_input, pyrogram_client=client)
        except Exception as e:
            await update.message.reply_text(f"Ошибка: {e}", reply_markup=self._create_contacts_menu())
            return MANAGE_CONTACTS

        if result:
            await update.message.reply_text("✅ Контакт удалён.", reply_markup=self._create_contacts_menu())
        else:
            await update.message.reply_text("❌ Не удалось удалить контакт (не найден или ошибка).",
                                            reply_markup=self._create_contacts_menu())
        return MANAGE_CONTACTS
    async def external_edit_text_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        try:
            with open("external_config.json", "r", encoding="utf-8") as f:
                config = json.load(f)
            current_text = config.get("message", "")
        except Exception:
            current_text = ""
        await query.edit_message_text(
            f"✏️ Текущий текст рассылки:\n\n{current_text}\n\n"
            "Отправьте новый текст внешней рассылки:",
            reply_markup=self._create_cancel_button()
        )
        return EXTERNAL_INPUT_MESSAGE

    @private_only
    async def process_external_message_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        new_text = update.message.text
        config_path = "external_config.json"
        try:
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
            else:
                config = {}
            config["message"] = new_text
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
            await update.message.reply_text(
                "✅ Текст для внешней рассылки сохранён.",
                reply_markup=self._create_external_settings_menu()
            )
        except Exception as e:
            await update.message.reply_text(
                f"❌ Ошибка при сохранении текста: {e}",
                reply_markup=self._create_external_settings_menu()
            )
        return MAIN_MENU

    @private_only
    async def process_external_links(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        text = update.message.text
        links = [line.strip() for line in text.splitlines() if line.strip()]
        valid_links = []
        invalid_links = []
        for link in links:
            if self.is_telegram_link(link):
                valid_links.append(link)
            else:
                invalid_links.append(link)
        if invalid_links:
            invalids = "\n".join(invalid_links)
            await update.message.reply_text(
                f"❌ Обнаружены некорректные ссылки:\n{invalids}\n\n"
                "Проверьте формат (пример:\n"
                "https://t.me/c/123456789/12345).",
                reply_markup=self._create_external_settings_menu()
            )
            return EXTERNAL_INPUT_LINKS

        config_path = "external_config.json"
        try:
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
            else:
                config = {}
            config["message_links"] = valid_links
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
            await update.message.reply_text(
                f"✅ Ссылки для пересылки сохранены. Всего: {len(valid_links)}",
                reply_markup=self._create_external_settings_menu()
            )
        except Exception as e:
            await update.message.reply_text(
                f"❌ Ошибка при сохранении ссылок: {e}",
                reply_markup=self._create_external_settings_menu()
            )
        return MAIN_MENU

    @private_only
    async def process_contacts(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user_id = update.message.from_user.id
        contacts = []

        if update.message.text:
            text = update.message.text
            import re
            found_contacts = re.findall(r"(?<!\S)(?:@)?([a-zA-Z0-9_]{4,32}|[+\d]{7,15})(?!\S)", text)

            for contact in found_contacts:
                if isinstance(contact, tuple):
                    contact = next((x for x in contact if x), None)
                if contact:
                    if contact.isdigit() or contact.startswith('+'):
                        contacts.append(contact)
                    else:
                        contacts.append(f"@{contact}" if not contact.startswith('@') else contact)

        elif update.message.document:
            document = update.message.document
            if document.mime_type != "text/plain":
                await update.message.reply_text(
                    "❌ Файл должен быть в формате TXT!",
                    reply_markup=await self._create_settings_menu()
                )
                return SETTINGS_MENU

            file = await context.bot.get_file(document.file_id)
            file_path = f"temp_{user_id}.txt"
            await file.download_to_drive(file_path)

            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            if line.isdigit() or line.startswith('+'):
                                contacts.append(line)
                            else:
                                contacts.append(f"@{line}" if not line.startswith('@') else line)
            finally:
                import os
                if os.path.exists(file_path):
                    os.remove(file_path)

        if not contacts:
            await update.message.reply_text(
                "❌ Неверный формат ввода контактов!",
                reply_markup=await self._create_settings_menu()
            )
            return SETTINGS_MENU

        try:
            attached, skipped = await self.attach_contacts_to_managers(contacts)
            await update.message.reply_text(
                f"✅ Привязано {attached} контактов. Пропущено {skipped}.",
                reply_markup=await self._create_settings_menu()
            )
        except Exception as e:
            await update.message.reply_text(
                f"❌ Ошибка при добавлении контактов: {str(e)}",
                reply_markup=await self._create_settings_menu()
            )

        return SETTINGS_MENU

    @private_only
    async def process_message_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        new_text = update.message.text

        if new_text.lower() == "cancel":
            await update.message.reply_text(
                "❌ Изменение текста отменено",
                reply_markup=await self._create_settings_menu()
            )
            return SETTINGS_MENU

        config = await self.config_manager.load()
        config["message"] = new_text

        try:
            await self.config_manager.save(config)
            await update.message.reply_text(
                "✅ Текст сообщения сохранен!",
                reply_markup=await self._create_settings_menu()
            )
        except Exception as e:
            await update.message.reply_text(
                f"❌ Ошибка при сохранении текста: {str(e)}",
                reply_markup=await self._create_settings_menu()
            )

        return SETTINGS_MENU

    async def manage_contacts_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(
            "Меню контактов:",
            reply_markup=self._create_contacts_menu()
        )
        return MANAGE_CONTACTS

    async def _create_settings_menu(self):
        config = await self.config_manager.load()
        mode = config.get("broadcast_mode", "message")
        mode_text = "Текст" if mode == "message" else "Пересылка"
        keyboard = [
            [InlineKeyboardButton("✏ Задать текст", callback_data="edit_text"), InlineKeyboardButton("🔗 Задать ссылки", callback_data="edit_links")],
            [InlineKeyboardButton("👨‍💼 Управление контактами", callback_data="manage_contacts")],
            [InlineKeyboardButton(f"🔰 Режим: {mode_text}", callback_data="switch_broadcast_mode")],
            [InlineKeyboardButton("⬅ Назад", callback_data="internal_menu")]
        ]
        return InlineKeyboardMarkup(keyboard)

    async def switch_broadcast_mode_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        config = await self.config_manager.load()
        mode = config.get('broadcast_mode', 'message')
        new_mode = 'links' if mode == 'message' else 'message'
        config['broadcast_mode'] = new_mode
        await self.config_manager.save(config)
        mode_text = "Текст" if new_mode == "message" else "Пересылка"
        await query.edit_message_text(
            f"Режим переключён на: <b>{mode_text}</b>.",
            reply_markup=await self._create_settings_menu(),
            parse_mode="HTML"
        )
        return SETTINGS_MENU
    async def to_settings_menu_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(
            "⚙ Настройки внутренней рассылки:",
            reply_markup=await self._create_settings_menu()
        )
        return SETTINGS_MENU

    def _create_contacts_menu(self):
        from telegram import InlineKeyboardMarkup, InlineKeyboardButton

        keyboard = [
            [InlineKeyboardButton("➕ Добавить контакты", callback_data="add_contacts")],
            [InlineKeyboardButton("⛔ Удалить контакт", callback_data="delete_contact")],
            [InlineKeyboardButton("🗑 Очистить базу", callback_data="clear_db")],
            [InlineKeyboardButton("⬅ Назад", callback_data="to_settings_menu")]
        ]
        return InlineKeyboardMarkup(keyboard)

    async def external_set_contacts_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(
            "Отправьте список контактов через пробел или\nзагрузите файл .txt (каждый контакт в новой строке).\nФормат: @username или username.",
            reply_markup=self._create_cancel_button()
        )
        return EXTERNAL_INPUT_CONTACTS

    @private_only
    async def process_external_contacts(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        contacts = []

        if update.message.text:
            found_contacts = re.findall(r"(?<!\S)(?:@)?([a-zA-Z0-9_]{4,32}|[+\d]{7,15})(?!\S)", update.message.text)
            for contact in found_contacts:
                if contact.isdigit() or contact.startswith('+'):
                    contacts.append(contact)
                else:
                    contacts.append(f"@{contact}" if not contact.startswith('@') else contact)

        elif update.message.document:
            document = update.message.document
            if document.mime_type != "text/plain":
                await update.message.reply_text("❌ Только .txt-файлы!",
                                                reply_markup=self._create_external_settings_menu())
                return MAIN_MENU
            file = await context.bot.get_file(document.file_id)
            file_path = f"temp_ext_contacts.txt"
            await file.download_to_drive(file_path)
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            if line.isdigit() or line.startswith('+'):
                                contacts.append(line)
                            else:
                                contacts.append(f"@{line}" if not line.startswith('@') else line)
            finally:
                if os.path.exists(file_path):
                    os.remove(file_path)

        if not contacts:
            await update.message.reply_text("❌ Не найдено ни одного контакта!",
                                            reply_markup=self._create_external_settings_menu())
            return MAIN_MENU

        try:
            external_config = {
                "contacts": contacts,
                "message": "Привет",
                "interval": 15,
                "message_links": [
                    "https://t.me/c/2349894738/1478",
                    "https://t.me/c/2349894738/1479"
                ]
            }
            if os.path.exists("external_config.json"):
                with open("external_config.json", "r", encoding="utf-8") as f:
                    config = json.load(f)
                config["contacts"] = contacts
            else:
                config = external_config
            with open("external_config.json", "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
            await update.message.reply_text(
                f"✅ Контакты сохранены.\nВсего: {len(contacts)}",
                reply_markup=self._create_external_settings_menu()
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}", reply_markup=self._create_external_settings_menu())
        return MAIN_MENU
    def _create_cancel_button(self):
        from telegram import InlineKeyboardMarkup, InlineKeyboardButton
        keyboard = [
            [InlineKeyboardButton("❌ Отмена", callback_data="cancel")]
        ]
        return InlineKeyboardMarkup(keyboard)

    @private_only
    async def error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger = logging.getLogger(__name__)
        logger.error("Exception while handling an update:", exc_info=context.error)

        if update and hasattr(update, "message") and update.message:
            try:
                await update.message.reply_text("❌ Произошла непредвиденная ошибка. Попробуйте ещё раз позже.")
            except Exception:
                pass
        elif update and hasattr(update, "callback_query") and update.callback_query:
            try:
                await update.callback_query.answer("❌ Произошла непредвиденная ошибка.", show_alert=True)
            except Exception:
                pass

    async def confirm_broadcast(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()

        config = await self.config_manager.load()
        message = config.get("message", "")
        contacts = await self.contact_manager.get_contacts()

        if not message or not contacts:
            await query.edit_message_text(
                "❌ Нельзя начать рассылку: не задан текст сообщения или контакты.",
                reply_markup=self._create_main_menu()
            )
            return MAIN_MENU

        await query.edit_message_text(
            f"🔔 Вы уверены, что хотите начать рассылку?\n\n"
            f"Текст сообщения:\n{message[:100]}{'...' if len(message) > 100 else ''}\n\n"
            f"Количество контактов: {len(contacts)}",
            reply_markup=self._create_broadcast_confirm_menu()
        )
        return BROADCAST_CONFIRM

    def _create_broadcast_confirm_menu(self):
        from telegram import InlineKeyboardMarkup, InlineKeyboardButton

        keyboard = [
            [InlineKeyboardButton("✅ Подтвердить рассылку", callback_data="confirm_broadcast")],
            [InlineKeyboardButton("❌ Отменить", callback_data="cancel_broadcast")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="to_broadcast_menu")]
        ]
        return InlineKeyboardMarkup(keyboard)

    @private_only
    async def cancel_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        user = update.effective_user
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.edit_message_text(
                "❌ Действие отменено.",
                reply_markup=self._create_main_menu()
            )
        elif update.message:
            await update.message.reply_text(
                "❌ Действие отменено.",
                reply_markup=self._create_main_menu()
            )
        return MAIN_MENU

    async def switch_account(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        account_id = query.data.split(":")[-1]

        try:
            await self.account_manager.switch_account(account_id)
            await query.answer("✅ Аккаунт успешно переключён")
            await query.edit_message_text(
                f"Аккаунт {account_id} успешно активирован.",
                reply_markup=self._create_main_menu()
            )
        except Exception as e:
            await query.answer("❌ Ошибка при переключении аккаунта")
            await query.edit_message_text(
                f"❌ Не удалось переключить аккаунт: {str(e)}",
                reply_markup=self._create_main_menu()
            )
        return MAIN_MENU

    async def cancel_broadcast(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(
            "❌ Рассылка отменена.",
            reply_markup=self._create_main_menu()
        )
        return MAIN_MENU

    async def switch_account_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        accounts = self.account_manager.get_accounts()
        current_account = self.account_manager.get_current_account()

        await query.edit_message_text(
            "Выберите аккаунт:",
            reply_markup=self._create_accounts_menu(accounts=accounts, active_account_id=getattr(current_account, "name", None))
        )
        return SETTINGS_MENU

    async def main_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()

        await query.edit_message_text(
            "🌍 Меню внешней рассылки:",
            reply_markup=self._create_main_menu()
        )
        return MAIN_MENU

    async def add_contacts_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()

        await query.edit_message_text(
            "📎 Отправьте контакты через пробел или файл TXT:\n\n"
            "Формат: @username или id",
            reply_markup=self._create_cancel_button()
        )
        return INPUT_CONTACTS

    async def edit_text_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        config = await self.config_manager.load()
        current_text = config.get("message", "")

        await query.edit_message_text(
            f"✏️ Текущий текст:\n\n{current_text}\n\n"
            "Отправьте новый текст сообщения:",
            reply_markup=self._create_cancel_button()
        )
        return INPUT_MESSAGE

    async def attach_contacts_to_managers(self, contacts: List[str]):

        added = 0
        skipped = 0

        for contact in contacts:
            user_id = None
            try:
                user_id = await self.message_processor.verify_contact(contact)
            except Exception as e:
                print(f"Ошибка поиска user_id для {contact}: {e}")
            if not user_id:
                skipped += 1
                print(f"Контакт {contact} не найден")
                continue
            if self.db.user_exists(user_id):
                skipped += 1
                print(f"Пользователь {user_id} уже есть в базе")
                continue
            free_manager = self.db.get_free_manager()
            if not free_manager:
                skipped += 1
                print(f"Нет свободных менеджеров для {contact}")
                continue
            self.db.assign_user(user_id, free_manager)
            added += 1
            print(f"Пользователь {user_id} привязан к менеджеру {free_manager}")

        return added, skipped

    async def _internal_broadcast_for_manager(self, client, users, mode, message, message_links, login, delay=2):
        sent = 0
        failed = 0
        for user_id in users:
            try:
                if mode == "message":
                    await client.send_message(user_id, message)
                    sent += 1
                elif mode == "links":
                    for link in message_links:
                        chat_id, message_id = self.message_processor.parse_telegram_link(link)
                        await client.copy_message(
                            chat_id=user_id,
                            from_chat_id=chat_id,
                            message_id=message_id
                        )
                        sent += 1
                        await asyncio.sleep(delay)
            except FloodWait as e:
                await asyncio.sleep(e.value)
                try:
                    await client.send_message(user_id, message)
                    sent += 1
                except Exception as ex:
                    print(f"Ошибка {ex}")
                    failed += 1
            except (PeerIdInvalid, UserDeactivated):
                print(f"Пользователь {user_id} недоступен")
                failed += 1
            except Exception as e:

                print(f"Ошибка рассылки {e}")
                failed += 1
            await asyncio.sleep(delay)
        return (login, sent, failed)

    async def run_external_broadcast(self, usernames, message, limit_per_userbot, delay=10):
        userbot_clients = list(self.external_account_manager.active_accounts.values())
        valid_users = await self.get_valid_unsent_users(usernames, userbot_clients[0])
        max_to_send = limit_per_userbot * len(userbot_clients)
        users_to_send = valid_users[:max_to_send]
        distributed = self.distribute_among_accounts(userbot_clients, users_to_send, limit_per_userbot)

        tasks = []
        for client, user_chunk in distributed:
            tasks.append(asyncio.create_task(self.external_broadcast_to_chunk(client, user_chunk, message, delay)))
        results = await asyncio.gather(*tasks)
        total_sent = sum(sent for sent, _ in results)
        total_failed = sum(failed for _, failed in results)
        return total_sent, total_failed


    async def run_internal_broadcast(self, mode, message, message_links, delay: int = 5):
        tasks = []
        for login in self.manager_names:
            client = self.account_manager.active_accounts[login]
            users = self.db.get_manager_users(login)
            tasks.append(
                asyncio.create_task(
                    self._internal_broadcast_for_manager(client, users, mode, message, message_links, login, delay)
                )
            )
        results = await asyncio.gather(*tasks)
        return results

    def _create_internal_broadcast_panel(self) -> InlineKeyboardMarkup:
        buttons = [
            [InlineKeyboardButton("✉ Разослать сообщения", callback_data="internal_broadcast_confirm")],
            [InlineKeyboardButton("⚙ Настройки", callback_data="settings"), InlineKeyboardButton("🔍 Проверить настройки", callback_data="internal_check_config")],
            [InlineKeyboardButton("🔙 Назад", callback_data="main_menu")]
        ]
        return InlineKeyboardMarkup(buttons)

    def _create_main_menu(self) -> InlineKeyboardMarkup:
        buttons = [
            [
                InlineKeyboardButton("📨 Разослать сообщения", callback_data="external_broadcast_confirm")
            ],
            [
                InlineKeyboardButton("⚙ Настройки", callback_data="external_settings"),
                InlineKeyboardButton("🔍 Проверить настройки", callback_data="view_settings")
            ],
            [
                InlineKeyboardButton("🏘 Внутренняя рассылка", callback_data="internal_menu")
            ]
        ]
        return InlineKeyboardMarkup(buttons)

    async def show_internal_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(
            "🏘 Меню внутренней рассылки:",
            reply_markup=self._create_internal_broadcast_panel()
        )
        return MAIN_MENU

    def _create_accounts_menu(self, accounts=None, active_account_id=None):
        if accounts is None:
            accounts = self.account_manager.get_accounts()

        keyboard = []
        if accounts:
            for acc in accounts:
                acc_id = acc.get("id")
                acc_name = acc.get("name")
                prefix = "🟢 " if acc_id == active_account_id else ""
                keyboard.append([
                    InlineKeyboardButton(f"{prefix}{acc_name}", callback_data=f"switch_account:{acc_id}")
                ])
        keyboard.append([InlineKeyboardButton("⬅ Назад", callback_data="main_menu")])
        return InlineKeyboardMarkup(keyboard)

    async def to_main_menu_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        await query.edit_message_text(
            "Главное меню:",
            reply_markup=self._create_main_menu()
        )
        return MAIN_MENU

    async def external_broadcast_confirm_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()

        try:
            with open("external_config.json", "r", encoding="utf-8") as f:
                config = json.load(f)
            mode = config.get("broadcast_mode", "message")
            contacts = config.get("contacts", [])
            message = config.get("message", "")
            message_links = config.get("message_links", [])
            interval = config.get("interval", 15)
        except Exception as e:
            await query.edit_message_text(
                f"❌ Не удалось загрузить настройки: {e}",
                reply_markup=self._create_main_menu()
            )
            return MAIN_MENU

        if not contacts:
            await query.edit_message_text(
                "❌ Не указаны контакты для рассылки.",
                reply_markup=self._create_main_menu()
            )
            return MAIN_MENU

        if mode == "message":
            preview = f"📝 Текст сообщения:\n{message[:100]}{'...' if len(message) > 100 else ''}"
        else:
            links_str = "\n".join(message_links[:5]) + ("\n..." if len(message_links) > 5 else "")
            preview = f"🔗 Ссылки для пересылки:\n{links_str}"

        await query.edit_message_text(
            f"⚠️ Вы уверены, что хотите начать внешнюю рассылку?\n\n"
            f"{preview}\n\n"
            f"Количество контактов: {len(contacts)}\n",
            reply_markup=self._create_external_broadcast_confirm_menu(),
            parse_mode="HTML"
        )
        return EXTERNAL_BROADCAST_CONFIRM

    async def internal_broadcast_confirm_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        config = await self.config_manager.load()
        mode = config.get("broadcast_mode", "message")
        message = config.get("message", "")
        message_links = config.get("message_links", [])
        interval = config.get("interval", 15)

        all_users = self.db.all_users()
        n_users = len(all_users)
        if n_users == 0:
            await query.edit_message_text(
                "❌ Нет получателей для рассылки.",
                reply_markup=self._create_internal_broadcast_panel()
            )
            return MAIN_MENU

        if mode == "links":
            total_msgs = n_users * len(message_links)
        else:
            total_msgs = n_users
        duration_sec = total_msgs * interval
        finish_dt = datetime.now() + timedelta(seconds=duration_sec)
        finish_str = finish_dt.strftime('%d.%m.%Y %H:%M:%S')

        if mode == "message":
            preview = f"📝 Текст:\n{message[:100]}{'...' if len(message) > 100 else ''}"
        else:
            links_str = "\n".join(message_links[:5]) + ("\n..." if len(message_links) > 5 else "")
            preview = f"🔗 Ссылки:\n{links_str}"

        await query.edit_message_text(
            f"⚠️ Вы уверены, что хотите запустить внутреннюю рассылку?\n\n"
            f"{preview}\n\n"
            f"Получателей: {n_users}\n"
            f"⏱️ Время окончания: <b>{finish_str}</b>",
            reply_markup=self._create_internal_broadcast_confirm_menu(),
            parse_mode="HTML"
        )
        return INTERNAL_BROADCAST_CONFIRM

    def _create_internal_broadcast_confirm_menu(self):
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Подтвердить", callback_data="internal_broadcast_go")],
            [InlineKeyboardButton("❌ Отменить", callback_data="internal_menu")]
        ])


    def _create_external_broadcast_confirm_menu(self):
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Подтвердить", callback_data="external_broadcast")],
            [InlineKeyboardButton("❌ Отменить", callback_data="main_menu")]
        ])

    async def external_broadcast_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        try:
            with open("external_config.json", "r", encoding="utf-8") as f:
                config = json.load(f)
            mode = config.get("broadcast_mode", "message")
            contacts = config.get("contacts", [])
            message = config.get("message", "")
            message_links = config.get("message_links", [])
            interval = config.get("interval", 15)
            limit_per_userbot = config.get("limit_per_userbot", 5)
        except Exception as e:
            await query.edit_message_text(
                f"❌ Не удалось загрузить настройки: {e}",
                reply_markup=self._create_main_menu()
            )
            return MAIN_MENU

        if not contacts:
            await query.edit_message_text(
                "❌ Не указаны контакты для рассылки.",
                reply_markup=self._create_main_menu()
            )
            return MAIN_MENU

        userbot_clients = list(self.external_account_manager.active_accounts.values())
        if not userbot_clients:
            await query.edit_message_text(
                "❌ Нет активных user-ботов для внешней рассылки.",
                reply_markup=self._create_main_menu()
            )
            return MAIN_MENU

        await query.edit_message_text(
            "🚀 Внешняя рассылка запущена! Вы получите уведомление по завершении.",
            reply_markup=self._create_main_menu()
        )

        asyncio.create_task(
            self._external_broadcast_worker(config, userbot_clients, update, context)
        )
        return MAIN_MENU

    @private_only
    async def process_links(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        text = update.message.text
        links = [line.strip() for line in text.splitlines() if line.strip()]
        valid_links = []
        invalid_links = []
        for link in links:
            if self.is_telegram_link(link):
                valid_links.append(link)
            else:
                invalid_links.append(link)
        if invalid_links:
            invalids = "\n".join(invalid_links)
            await update.message.reply_text(
                f"❌ Обнаружены некорректные ссылки:\n{invalids}\n\n"
                "Проверьте формат (пример:\n"
                "https://t.me/c/123456789/12345).",
                reply_markup=await self._create_settings_menu()
            )
            return INPUT_LINKS

        config = await self.config_manager.load()
        config["message_links"] = valid_links
        await self.config_manager.save(config)
        await update.message.reply_text(
            f"✅ Ссылки для пересылки сохранены. Всего: {len(valid_links)}",
            reply_markup=await self._create_settings_menu()
        )
        return SETTINGS_MENU
    async def edit_links_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        config = await self.config_manager.load()
        links = config.get("message_links", [])
        links_text = "\n".join(links) if links else "нет"
        await query.edit_message_text(
            f"🔗 Текущие ссылки для пересылки:\n\n{links_text}\n\n"
            "Отправьте новые ссылки (каждая с новой строки):",
            reply_markup=self._create_cancel_button()
        )
        return INPUT_LINKS
    async def external_edit_links_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        try:
            with open("external_config.json", "r", encoding="utf-8") as f:
                config = json.load(f)
            links = config.get("message_links", [])
            links_text = "\n".join(links) if links else "нет"
        except Exception:
            links_text = "нет"
        await query.edit_message_text(
            f"🔗 Текущие ссылки для пересылки:\n\n{links_text}\n\n"
            "Отправьте новые ссылки (каждая с новой строки):",
            reply_markup=self._create_cancel_button()
        )
        return EXTERNAL_INPUT_LINKS
    async def _external_broadcast_worker(self, config, userbot_clients, update, context):
        mode = config.get("broadcast_mode", "message")
        contacts = config.get("contacts", [])
        interval = config.get("interval", 15)
        message_text = config.get("message")
        links = config.get("message_links", [])
        limit_per_userbot = config.get("limit_per_userbot", 5)
        total_limit = limit_per_userbot * len(userbot_clients)

        queue = asyncio.Queue()
        send_counters = {client: 0 for client in userbot_clients}

        async def resolver():
            sent_total = 0
            for username in contacts:
                if sent_total >= total_limit:
                    break
                uname = username.lstrip("@")
                user_id = await self.safe_resolve_username(userbot_clients[0], uname)
                if user_id is not None:
                    if (
                            not self.db.user_exists(user_id)
                            and not self.db.was_external_broadcast_sent(user_id)
                    ):
                        await queue.put((user_id, uname))
                        sent_total += 1
            for _ in userbot_clients:
                await queue.put(None)

        async def sender(client):
            while True:
                item = await queue.get()
                if item is None:
                    queue.task_done()
                    break
                if send_counters[client] >= limit_per_userbot:
                    queue.task_done()
                    continue
                user_id, uname = item
                try:
                    if mode == "message":
                        await client.send_message(uname, message_text)
                    elif mode == "links" and links:
                        for link in links:
                            parsed = self.message_processor.parse_telegram_link(link)
                            if not parsed:
                                print(f"Некорректная ссылка: {link}")
                                continue
                            message_id = parsed
                            try:
                                await client.copy_message(
                                    chat_id=uname,
                                    from_chat_id=GROUP_ID,
                                    message_id=message_id
                                )
                                await asyncio.sleep(interval)
                            except Exception as e:
                                print(f"Ошибка при пересылке {link} -> {uname}: {e}")
                    self.db.mark_external_broadcast_sent(user_id, uname)
                    send_counters[client] += 1
                except Exception as e:
                    print(f"Ошибка отправки {user_id}: {e}")
                await asyncio.sleep(interval)
                queue.task_done()

        resolver_task = asyncio.create_task(resolver())
        sender_tasks = [asyncio.create_task(sender(client)) for client in userbot_clients]
        await resolver_task
        await queue.join()
        for t in sender_tasks:
            t.cancel()

        total_sent = sum(send_counters.values())
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"Внешняя рассылка завершена!\n\n"
                 f"✅ Отправлено: {total_sent}",
            reply_markup=self._create_main_menu()
        )

    async def internal_broadcast_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        config = await self.config_manager.load()
        message = config["message"] if config["message"] else "Внутренняя рассылка"
        mode = config.get("broadcast_mode", "message")
        message_links = config.get("message_links", [])
        interval = config.get("interval", 15)

        all_users = self.db.all_users()
        n_users = len(all_users)

        if mode == "links":
            total_msgs = n_users * len(message_links)
        else:
            total_msgs = n_users
        duration_sec = total_msgs * interval
        from datetime import datetime, timedelta
        finish_dt = datetime.now() + timedelta(seconds=duration_sec)
        finish_str = finish_dt.strftime('%d.%m.%Y %H:%M:%S')

        await query.edit_message_text(
            f"🚀 Внутренняя рассылка запущена!\n"
            f"Отчёт появится по завершении.\n\n"
            f"⏱ Время окончания: <b>{finish_str}</b>",
            reply_markup=self._create_internal_broadcast_panel(),
            parse_mode="HTML"
        )
        asyncio.create_task(self._internal_broadcast_worker(message, mode, message_links, update, context))
        return MAIN_MENU

    async def _internal_broadcast_worker(self, message, mode, message_links, update, context):
        results = await self.run_internal_broadcast(mode=mode, message=message, message_links=message_links)
        text = "Результаты рассылки:\n\n"
        for login, sent, failed in results:
            text += f"{login}: отправлено — {sent}, ошибок — {failed}\n"
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=text,
                reply_markup=self._create_internal_broadcast_panel()
            )
        except BadRequest as e:
            if "Message is not modified" in str(e):
                pass
            else:
                raise


    async def run(self):
        try:
            await self.account_manager.load_accounts()
            await self.external_account_manager.load_accounts()
            await self.account_manager.initialize_accounts()
            await self.external_account_manager.initialize_accounts()
            await self.sync_managers()
            self.setup_handlers()
            await self.ptb_app.initialize()
            await self.ptb_app.start()
            if self.ptb_app.updater:
                await self.ptb_app.updater.start_polling()
            print("Бот успешно запущен")
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"Ошибка при запуске бота: {e}")
        finally:
            await self.shutdown()

    async def shutdown(self):
        try:
            if self.ptb_app.updater:
                await self.ptb_app.updater.stop()
            await self.ptb_app.stop()
            await self.account_manager.stop_all()
            self.db.close()
            print("Бот успешно остановлен")
        except Exception as e:
            print(f"Ошибка при остановке бота: {e}")


async def main():
    bot = BroadcastBot()
    try:
        await bot.run()
    except KeyboardInterrupt:
        print("Получен сигнал KeyboardInterrupt")
    finally:
        await bot.shutdown()

if __name__ == "__main__":
    asyncio.run(main())