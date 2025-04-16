import os
import asyncio
import json
import logging
from typing import Dict
from environs import Env
from pyrogram import Client, errors
from pyrogram.errors import FloodWait
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, ConversationHandler


# Загрузка конфигурации
env = Env()
env.read_env(".env")

API_ID = env.int("APP_API_ID")
API_HASH = env.str("APP_API_HASH")
BOT_TOKEN = env.str("BOT_TOKEN")
LOGIN = env.str("LOGIN")


class BroadcastBot:

    def __init__(self):

        self.video_files = [
            "videos/video1.mp4",
            "videos/video2.mp4",
            "videos/video3.mp4",
            "videos/video4.mp4",
            "videos/video5.mp4"
        ]
        self.userbot = Client(
            "testme",
            api_id=API_ID,
            api_hash=API_HASH
        )
        self.ptb_app = Application.builder().token(BOT_TOKEN).build()
        self.current_tasks: Dict[int, asyncio.Task] = {}

        self.main_menu = ReplyKeyboardMarkup([
            [KeyboardButton("👤 Налаштування контактів")],
            [KeyboardButton("📩 Розіслати контактам")],
            [KeyboardButton("🔍 Переглянути налаштування")]
        ], resize_keyboard=True)

    async def load_config(self):
        try:
            with open("config.json", "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"Ошибка загрузки конфигурации: {e}")
            return {
                "contacts": [],
                "groups": [],
                "message": "",
                "interval": 5,
                "max_per_minute": 20
            }

    async def save_config(self, config):
        with open("config.json", "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)

    async def verify_contact(self, contact):
        try:
            user = await self.userbot.get_users(contact)
            return user.id
        except Exception as e:
            logging.error(f"Ошибка проверки контакта {contact}: {e}")
            return None

    async def send_to_users_task(self, user_id, update: Update):
        try:
            config = await self.load_config()
            success = errors = 0
            total_contacts = len(config["contacts"])
            total_videos = len(self.video_files)
            total_messages = total_contacts * total_videos

            start_time = asyncio.get_event_loop().time()
            estimated_total_time = (total_messages * config["interval"]) / 60

            await update.message.reply_text(
                f"🔍 Починаю розсилку {total_videos} відео для {total_contacts} контактів\n"
                f"📊 Всього повідомлень: {total_messages}\n"
                f"⏳ Приблизний час завершення: {estimated_total_time:.1f} хв"
            )

            for contact in config["contacts"]:
                if user_id not in self.current_tasks:
                    break

                try:
                    contact_id = await self.verify_contact(contact)
                    if not contact_id:
                        errors += 1
                        continue

                    for video_file in self.video_files:
                        video_path = os.path.abspath(video_file)
                        if not os.path.exists(video_path):
                            await update.message.reply_text(f"❌ Відеофайл {video_file} не знайдено!")
                            continue

                        await self.userbot.send_video_note(
                            chat_id=contact_id,
                            video_note=video_path
                        )
                        success += 1
                        await asyncio.sleep(config["interval"])

                except FloodWait as e:
                    wait = e.value
                    await update.message.reply_text(f"⏳ Пауза {wait} сек...")
                    await asyncio.sleep(wait)
                except Exception as e:
                    errors += 1
                    logging.error(f"Помилка: {e}")
                    await asyncio.sleep(5)

            await update.message.reply_text(
                f"🎉 Розсилку завершено!\n✅ Успішно: {success}\n❌ Помилок: {errors}",
                reply_markup=self.main_menu
            )

        except Exception as e:
            logging.critical(f"Критична помилка: {e}")
            await update.message.reply_text("❌ Під час розсилки сталася помилка!")
        finally:
            if user_id in self.current_tasks:
                del self.current_tasks[user_id]

    async def send_to_users(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.message.from_user.id

        if user_id in self.current_tasks:
            await update.message.reply_text("⚠ У вас вже активована розсилка!", reply_markup=self.main_menu)
            return

        config = await self.load_config()
        if not config["contacts"]:
            await update.message.reply_text("❌ Список контактів порожній!", reply_markup=self.main_menu)
            return

        self.current_tasks[user_id] = asyncio.create_task(
            self.send_to_users_task(user_id, update)
        )

        await update.message.reply_text(
            "⏳ Починаю розсилку... (/cancel для скасування)",
            reply_markup=ReplyKeyboardMarkup([[KeyboardButton("/cancel")]], resize_keyboard=True)
        )

    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.message.from_user.id
        if user_id in self.current_tasks:
            self.current_tasks[user_id].cancel()
            del self.current_tasks[user_id]
            await update.message.reply_text("❌ Розсилку скасовано!", reply_markup=self.main_menu)
        else:
            await update.message.reply_text("❌ Немає активних задач!", reply_markup=self.main_menu)

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "👋 Вітаю! Я бот для розсилки відео-повідомлень.",
            reply_markup=self.main_menu
        )

    async def setup_handlers(self):
        """Настройка всех обработчиков"""
        self.ptb_app.add_handler(CommandHandler("start", self.start))
        self.ptb_app.add_handler(CommandHandler("cancel", self.cancel))

        # Обработчик для кнопки "Розіслати контактам"
        self.ptb_app.add_handler(MessageHandler(
            filters.Regex("^📩 Розіслати контактам$"),
            self.send_to_users
        ))

    async def run(self):
        """Основной метод запуска"""
        await self.userbot.start()
        await self.setup_handlers()

        try:
            await self.ptb_app.initialize()
            await self.ptb_app.start()
            await self.ptb_app.updater.start_polling()

            logging.info("Бот запущений!")
            while True:
                await asyncio.sleep(1)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logging.critical(f"Помилка: {e}")
        finally:
            await self.shutdown()

    async def shutdown(self):
        """Корректное завершение работы"""
        await self.ptb_app.stop()
        await self.userbot.stop()
        logging.info("Бот зупинено")


async def main():
    bot = BroadcastBot()
    try:
        await bot.run()
    except KeyboardInterrupt:
        await bot.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nБот зупинено")