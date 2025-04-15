from pyrogram.errors import FloodWait
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, ConversationHandler
from pyrogram import Client, errors
import json
import asyncio
from typing import Dict
from environs import Env
import logging
import os

env = Env()
env.read_env("config.env")

API_ID = env.int("APP_API_ID")
API_HASH = env.str("APP_API_HASH")
BOT_TOKEN = env.str("BOT_TOKEN")
LOGIN = env.str("LOGIN")

video_path = "video.mp4"

# Ініціалізація клієнтів
userbot = Client(
    name=LOGIN,
    api_id=API_ID,
    api_hash=API_HASH
)
ptb_app = Application.builder().token(BOT_TOKEN).build()

# Глобальні змінні
current_tasks: Dict[int, asyncio.Task] = {}
main_menu = ReplyKeyboardMarkup([
    [KeyboardButton("✏ Редагувати повідомлення"), KeyboardButton("⏱ Встановити інтервал")],
    [KeyboardButton("👤 Налаштування контактів"), KeyboardButton("👥 Налаштування груп")],
    [KeyboardButton("📩 Розіслати контактам"), KeyboardButton("📢 Розіслати у групи")],
    [KeyboardButton("🔍 Переглянути налаштування")]
], resize_keyboard=True)

# Стани для ConversationHandler
SET_MESSAGE, SET_INTERVAL = range(2)
SETUP_CONTACTS, SETUP_GROUPS = range(2, 4)


async def load_config():
    try:
        with open("config.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"Помилка завантаження конфігурації: {e}")
        return {
            "contacts": [],
            "groups": [],
            "message": "",
            "interval": 60  # в секундах
        }


async def save_config(config):
    with open("config.json", "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


async def verify_group_access(group_id):
    try:
        chat = await userbot.get_chat(group_id)
        if not chat.permissions.can_send_messages:
            print(f"Бот не має прав у групі {group_id}")
            return False
        return True
    except errors.PeerIdInvalid:
        print(f"Бот не має доступу до групи {group_id}")
        return False
    except Exception as e:
        print(f"Помилка перевірки групи {group_id}: {e}")
        return False



async def send_to_groups_task(user_id, update: Update):
    config = await load_config()
    groups = config["groups"]
    message = config["message"]
    interval = config["interval"]

    success = 0
    errors_count = 0

    for group in groups:
        if not current_tasks.get(user_id):
            break

        try:
            if isinstance(group, int) and group < 0 and abs(group) > 1000000000000:
                group_id = group
            elif isinstance(group, str) and group.startswith("-100"):
                group_id = int(group)
            else:
                await update.message.reply_text(f"Неправильний формат ID групи: {group}")
                print(f"Неправильний формат ID групи: {group}")
                continue

            if await verify_group_access(group_id):
                await userbot.send_message(group_id, message)
                success += 1
                await asyncio.sleep(interval)
        except Exception as e:
            errors_count += 1
            print(f"Помилка відправки {group}: {e}")

    if user_id in current_tasks:
        del current_tasks[user_id]
        await update.message.reply_text(
            f"📊 Результат розсилки групам:\n✅ Успішно: {success}\n❌ Помилок: {errors_count}",
            reply_markup=main_menu
        )


async def send_to_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = await load_config()
    contacts = config.get("contacts", [])
    video_path = os.path.abspath("video.mp4")

    if not os.path.isfile(video_path):
        await update.message.reply_text("❌ Відеофайл не знайдено", reply_markup=main_menu)
        return

    success = 0
    failed = 0

    for contact in contacts:
        try:
            await userbot.send_video_note(contact, video_path)
            success += 1
        except Exception as e:
            print(f"❌ Помилка для {contact}: {e}")
            failed += 1

    await update.message.reply_text(
        f"📊 Розсилка завершена:\n✅ Успішно: {success}\n❌ Помилки: {failed}",
        reply_markup=main_menu
    )






async def send_to_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id in current_tasks:
        await update.message.reply_text("⚠ У вас вже активована розсилка!", reply_markup=main_menu)
        return

    config = await load_config()
    if not config["groups"]:
        await update.message.reply_text("❌ Список груп порожній!", reply_markup=main_menu)
        return
    if not config["message"]:
        await update.message.reply_text("❌ Повідомлення не налаштоване!", reply_markup=main_menu)
        return

    current_tasks[user_id] = asyncio.create_task(send_to_groups_task(user_id, update))
    await update.message.reply_text(
        "⏳ Починаю розсилку групам...\nДля скасування: /cancel",
        reply_markup=ReplyKeyboardMarkup([[KeyboardButton("/cancel")]], resize_keyboard=True)
    )


async def view_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = await load_config()

    contacts_count = len(config["contacts"])
    groups_count = len(config["groups"])
    message_preview = config["message"][:100] + "..." if len(config["message"]) > 100 else config["message"]
    interval = config["interval"]

    contacts_time = contacts_count * interval / 60
    groups_time = groups_count * interval / 60

    await update.message.reply_text(
        f"⚙ Поточні налаштування:\n\n"
        f"📄 Повідомлення:\n{message_preview}\n\n"
        f"⏱ Інтервал: {interval} секунд\n\n"
        f"👤 Контакти: {contacts_count}\n"
        f"⏳ Час розсилки: {contacts_time:.1f} хв\n\n"
        f"👥 Групи: {groups_count}\n"
        f"⏳ Час розсилки: {groups_time:.1f} хв",
        reply_markup=main_menu
    )


async def set_message_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📝 Введіть новий текст повідомлення для розсилки:",
        reply_markup=ReplyKeyboardRemove()
    )
    return SET_MESSAGE


async def set_message_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = await load_config()
    config["message"] = update.message.text
    await save_config(config)

    await update.message.reply_text(
        "✅ Повідомлення збережено!",
        reply_markup=main_menu
    )
    return ConversationHandler.END


async def set_interval_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⏱ Введіть інтервал між повідомленнями (у секундах мін. 10): \nДля скасування: /cancel",
        reply_markup=ReplyKeyboardRemove()
    )
    return SET_INTERVAL


async def set_interval_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        interval = int(update.message.text)
        if interval < 10:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Будь ласка, введіть ціле число більше 10")
        return SET_INTERVAL

    config = await load_config()
    config["interval"] = interval
    await save_config(config)

    await update.message.reply_text(
        f"✅ Інтервал збережено: {interval} секунд",
        reply_markup=main_menu
    )
    return ConversationHandler.END


async def setup_contacts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👤 Введіть список контактів через кому (@username або ID):\n"
        "Приклад: @user1, 123456789, user2",
        reply_markup=ReplyKeyboardRemove()
    )
    return SETUP_CONTACTS


async def save_contacts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    contacts = [u.strip() for u in update.message.text.split(",") if u.strip()]
    config = await load_config()
    config["contacts"] = contacts
    await save_config(config)

    await update.message.reply_text(
        f"✅ Збережено {len(contacts)} контактів!",
        reply_markup=main_menu
    )
    return ConversationHandler.END


async def setup_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👥 Введіть ID груп через кому (починаючи з -100...):\n"
        "Приклад: -100123456789, -100987654321",
        reply_markup=ReplyKeyboardRemove()
    )
    return SETUP_GROUPS


async def save_groups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    groups = []
    for g in update.message.text.split(","):
        g = g.strip()
        if g.startswith("-100") and g[4:].isdigit():
            groups.append(int(g))
        elif g.isdigit() and len(g) > 9:
            groups.append(-1000000000000 + int(g))

    config = await load_config()
    config["groups"] = groups
    await save_config(config)

    await update.message.reply_text(
        f"✅ Збережено {len(groups)} груп!",
        reply_markup=main_menu
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id

    if user_id in current_tasks:
        task = current_tasks[user_id]
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        del current_tasks[user_id]
        await update.message.reply_text("❌ Розсилку скасовано!", reply_markup=main_menu)
        return ConversationHandler.END

    await update.message.reply_text(
        "❌ Поточну дію скасовано!",
        reply_markup=main_menu
    )
    return ConversationHandler.END


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id in current_tasks:
        await update.message.reply_text("🟢 Розсилка активна\nДля скасування: /cancel", reply_markup=main_menu)
    else:
        await update.message.reply_text("🔴 Розсилка неактивна", reply_markup=main_menu)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Вітаю!\nВиберіть дію:",
        reply_markup=main_menu
    )


async def main():
    await userbot.start()

    ptb_app.add_handler(CommandHandler("start", start))
    ptb_app.add_handler(CommandHandler("cancel", cancel))

    # Основні команди
    ptb_app.add_handler(MessageHandler(filters.Regex("^📩 Розіслати контактам$"), send_to_users))
    ptb_app.add_handler(MessageHandler(filters.Regex("^📢 Розіслати у групи$"), send_to_groups))
    ptb_app.add_handler(MessageHandler(filters.Regex("^🔍 Переглянути налаштування$"), view_settings))

    # Обробники налаштувань
    message_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^✏ Редагувати повідомлення$"), set_message_start)],
        states={
            SET_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_message_save)]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    interval_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^⏱ Встановити інтервал$"), set_interval_start)],
        states={
            SET_INTERVAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_interval_save)]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    contacts_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^👤 Налаштування контактів$"), setup_contacts)],
        states={
            SETUP_CONTACTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_contacts)]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    groups_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^👥 Налаштування груп$"), setup_groups)],
        states={
            SETUP_GROUPS: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_groups)]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    ptb_app.add_handler(message_handler)
    ptb_app.add_handler(interval_handler)
    ptb_app.add_handler(contacts_handler)
    ptb_app.add_handler(groups_handler)

    await ptb_app.initialize()
    await ptb_app.start()
    await ptb_app.updater.start_polling()

    print("🤖 Бот успішно запущений!")
    await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Бот зупинено")
    finally:
        print("👋 Завершення роботи")