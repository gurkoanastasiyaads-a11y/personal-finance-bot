import os
import logging
import base64
import json
import sqlite3
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
import anthropic
from groq import Groq

load_dotenv()

logging.basicConfig(level=logging.INFO)

TELEGRAM_TOKEN = os.getenv("PERSONAL_FINANCE_BOT_TOKEN")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

ALLOWED_USERS = [451779172]

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
groq_client = Groq(api_key=GROQ_API_KEY)

DB_PATH = "personal_finance.db"
MAX_HISTORY = 300

SYSTEM_PROMPT = """Ты — личный финансовый ассистент Анастасии. Помогаешь вести учёт личных доходов и расходов.

--- ВАЛЮТЫ ---
Анастасия работает с: рубли (₽), доллары ($), лари (₾), динары (د.إ — ОАЭ или سد — Сербия, уточняй если неясно).
Всегда конвертируй в рубли по актуальному курсу. Если не знаешь точный курс — используй приблизительный и предупреди об этом.
Примерные курсы (уточняй у пользователя если они изменились):
- 1 USD ≈ 90 ₽
- 1 ₾ (грузинский лари) ≈ 33 ₽  
- 1 د.إ (дирхам ОАЭ) ≈ 24 ₽
- 1 RSD (сербский динар) ≈ 0.8 ₽

--- КАТЕГОРИИ РАСХОДОВ ---
Определяй категорию сам: еда и продукты, кафе и рестораны, транспорт, жильё и коммунальные, одежда и красота, здоровье, развлечения и досуг, подписки и сервисы, путешествия, образование, другое.
Если не можешь определить категорию — спроси Анастасию.

--- КАК РАБОТАТЬ ---
1. Когда Анастасия пишет о трате или доходе — записывай в память, подтверждай запись с суммой в рублях
2. Всегда показывай: сумму в оригинальной валюте + сумму в рублях
3. По запросу "статистика" или "сводка" — выводи подробный отчёт за нужный период
4. Давай советы по экономии когда просят или видишь явные возможности

--- ФОРМАТ ЗАПИСИ ---
При каждой записи отвечай так:
✅ Записано: [категория]
[сумма в оригинальной валюте] = [сумма в ₽]
[тип: расход/доход]

--- СТАТИСТИКА ---
Когда просят статистику показывай:
📊 [период]
Доходы: X ₽
Расходы: X ₽
Баланс: X ₽

По категориям расходов:
• [категория]: X ₽ (X%)
...

💡 Советы по экономии: [конкретные советы на основе реальных данных]

--- ВАЖНО ---
- Помни все записи из истории чата
- Всегда указывай дату записи (используй текущую дату)
- Отвечай на русском языке
- Будь дружелюбной и поддерживающей, не осуждай траты"""


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS chat_history (
        chat_id INTEGER PRIMARY KEY, history TEXT NOT NULL,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    conn.commit()
    conn.close()
    print("✅ Personal finance bot DB initialized")


def load_history(chat_id):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT history FROM chat_history WHERE chat_id = ?", (chat_id,)).fetchone()
    conn.close()
    return json.loads(row[0]) if row else []


def save_history(chat_id, history):
    if len(history) > MAX_HISTORY:
        history = history[-MAX_HISTORY:]
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""INSERT INTO chat_history (chat_id, history, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(chat_id) DO UPDATE SET history=excluded.history, updated_at=CURRENT_TIMESTAMP""",
        (chat_id, json.dumps(history, ensure_ascii=False)))
    conn.commit()
    conn.close()


def clear_history(chat_id):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM chat_history WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()


def is_allowed(update):
    return update.effective_chat.id in ALLOWED_USERS


async def send_long(update, text):
    if len(text) > 4000:
        for part in [text[i:i+4000] for i in range(0, len(text), 4000)]:
            await update.message.reply_text(part)
    else:
        await update.message.reply_text(text)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_text(
        "Привет! Я твой личный финансовый ассистент 💰\n\n"
        "Просто пиши мне о своих тратах и доходах в любой валюте — я всё запомню и переведу в рубли.\n\n"
        "Например:\n"
        "• «Потратила $50 на одежду»\n"
        "• «Получила 30000₽ зарплата»\n"
        "• «Кофе 8₾»\n\n"
        "Команды:\n"
        "/stats — статистика за текущий месяц\n"
        "/clear — очистить историю\n\n"
        "Также понимаю фото чеков и голосовые сообщения! 🖼🎤"
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    now = datetime.now()
    month_name = now.strftime("%B %Y")
    history = load_history(chat_id)
    prompt = f"Дай подробную финансовую статистику за {month_name}: все доходы и расходы по категориям в рублях, баланс, и конкретные советы по экономии на основе моих трат."
    history.append({"role": "user", "content": prompt})
    try:
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-5", max_tokens=3000,
            system=SYSTEM_PROMPT, messages=history
        )
        reply = response.content[0].text
        history.append({"role": "assistant", "content": reply})
        save_history(chat_id, history)
        await send_long(update, reply)
    except Exception as e:
        print(f"Error: {e}")
        await update.message.reply_text("Что-то пошло не так 🙏")


async def clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    clear_history(update.effective_chat.id)
    await update.message.reply_text("🗑 История очищена!")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    now = datetime.now().strftime("%d.%m.%Y")
    history = load_history(chat_id)
    history.append({"role": "user", "content": f"[{now}] {update.message.text}"})
    try:
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-5", max_tokens=1000,
            system=SYSTEM_PROMPT, messages=history
        )
        reply = response.content[0].text
        history.append({"role": "assistant", "content": reply})
        save_history(chat_id, history)
        await update.message.reply_text(reply)
    except Exception as e:
        print(f"Error: {e}")
        await update.message.reply_text("Что-то пошло не так 🙏")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    file_bytes = await file.download_as_bytearray()
    image_data = base64.standard_b64encode(bytes(file_bytes)).decode("utf-8")
    now = datetime.now().strftime("%d.%m.%Y")
    caption = update.message.caption or f"[{now}] Это чек или фото расходов. Распознай сумму, определи категорию и запиши трату."
    history = load_history(chat_id)
    history.append({"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_data}},
        {"type": "text", "text": caption}
    ]})
    try:
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-5", max_tokens=1000,
            system=SYSTEM_PROMPT, messages=history
        )
        reply = response.content[0].text
        history[-1] = {"role": "user", "content": f"[{now}] [Фото чека] {caption}"}
        history.append({"role": "assistant", "content": reply})
        save_history(chat_id, history)
        await update.message.reply_text(reply)
    except Exception as e:
        print(f"Error: {e}")
        await update.message.reply_text("Что-то пошло не так с фото 🙏")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    chat_id = update.effective_chat.id
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    try:
        voice = update.message.voice
        file = await context.bot.get_file(voice.file_id)
        file_bytes = await file.download_as_bytearray()
        transcription = groq_client.audio.transcriptions.create(
            file=("voice.ogg", bytes(file_bytes), "audio/ogg"),
            model="whisper-large-v3",
            language="ru"
        )
        recognized_text = transcription.text
        now = datetime.now().strftime("%d.%m.%Y")
        history = load_history(chat_id)
        history.append({"role": "user", "content": f"[{now}] {recognized_text}"})
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-5", max_tokens=1000,
            system=SYSTEM_PROMPT, messages=history
        )
        reply = response.content[0].text
        history.append({"role": "assistant", "content": reply})
        save_history(chat_id, history)
        await update.message.reply_text(f"🎤 «{recognized_text}»\n\n{reply}")
    except Exception as e:
        print(f"Voice error: {e}")
        await update.message.reply_text("Не смогла распознать голосовое 🙏")


def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("clear", clear))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    print("💰 Personal finance bot started!")
    app.run_polling(stop_signals=None)


if __name__ == "__main__":
    main()
