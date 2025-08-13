# --- ФАЙЛ: main.py (ФІНАЛЬНА ВЕРСІЯ З УСІМА ВИПРАВЛЕННЯМИ) ---

import logging
import psycopg2
from urllib.parse import urlparse
from datetime import datetime
from collections import defaultdict
from functools import wraps
import os
from threading import Thread
import time

from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, User, constants
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.error import BadRequest

# --- ⚙️ НАСТРОЙКИ ---
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
DATABASE_URL = os.environ.get('DATABASE_URL')
DB_NAME = "debt_book_v2.db"

# --- 🎨 ЭМОДЗИ И СТРОКИ ---
EMOJI = { "money": "💰", "repay": "💸", "split": "🍕", "status": "📊", "my_debts": "👤", "history": "📜", "ok": "✅", "cancel": "❌", "back": "↩️", "user": "👤", "warning": "⚠️", "party": "🎉", "lock": "🔒"}
RUSSIAN_MONTHS_NOM = ["", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]

# --- 🪵 ЛОГИРОВАНИЕ ---
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)

# --- 🔢 СОСТОЯНИЯ ДЛЯ ДИАЛОГОВ ---
(SELECT_CREDITOR, SELECT_DEBTOR, GET_AMOUNT, GET_COMMENT) = range(4)
(REPAY_SELECT_DEBTOR, REPAY_SELECT_CREDITOR, REPAY_GET_AMOUNT) = range(4, 7)
(SPLIT_SELECT_PAYER, SPLIT_GET_AMOUNT, SPLIT_GET_COMMENT) = range(7, 10)

# --- 🗃️ КЛАСС ДЛЯ РАБОТЫ С БАЗОЙ ДАННЫХ (PostgreSQL) ---
class Database:
    def __init__(self, conn_url):
        if not conn_url:
            raise ValueError("DATABASE_URL не найден. Убедитесь, что он добавлен в Environment Variables.")
        
        print("Connecting to PostgreSQL database with SSL require...")
        try:
            result = urlparse(conn_url)
            self.conn = psycopg2.connect(
                dbname=result.path[1:],
                user=result.username,
                password=result.password,
                host=result.hostname,
                port=result.port,
                sslmode='require'
            )
            self.init_db()
            print("Database connection successful.")
        except psycopg2.OperationalError as e:
            print(f"!!! КРИТИЧНА ПОМИЛКА ПІДКЛЮЧЕННЯ ДО БАЗИ: {e}")
            raise

    def execute(self, query, params=(), fetch=None):
        try:
            with self.conn.cursor() as cur:
                cur.execute(query, params)
                self.conn.commit()
                if fetch == "one":
                    return cur.fetchone()
                if fetch == "all":
                    return cur.fetchall()
        except (psycopg2.InterfaceError, psycopg2.OperationalError) as e:
            print(f"Database connection lost ({e}). Reconnecting...")
            result = urlparse(DATABASE_URL)
            self.conn = psycopg2.connect(dbname=result.path[1:], user=result.username, password=result.password, host=result.hostname, port=result.port, sslmode='require')
            # Повторюємо запит після перепідключення
            with self.conn.cursor() as cur:
                cur.execute(query, params)
                self.conn.commit()
                if fetch == "one": return cur.fetchone()
                if fetch == "all": return cur.fetchall()

    def init_db(self):
        self.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT, chat_id BIGINT, first_name TEXT,
                username TEXT, PRIMARY KEY (user_id, chat_id)
            )
        """)
        self.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id SERIAL PRIMARY KEY, chat_id BIGINT, creditor_id BIGINT,
                debtor_id BIGINT, amount REAL, comment TEXT, timestamp TIMESTAMPTZ
            )
        """)

    async def register_user(self, user: User, chat_id: int):
        query = "INSERT INTO users (user_id, chat_id, first_name, username) VALUES (%s, %s, %s, %s) ON CONFLICT (user_id, chat_id) DO NOTHING"
        params = (user.id, chat_id, user.first_name, user.username or f"User{user.id}")
        self.execute(query, params)

    def get_group_members(self, chat_id: int):
        return self.execute("SELECT user_id, first_name FROM users WHERE chat_id = %s", (chat_id,), fetch="all")

    def get_user_name(self, user_id, chat_id):
        res = self.execute("SELECT first_name FROM users WHERE user_id=%s AND chat_id=%s", (user_id, chat_id), fetch="one")
        return res[0] if res else "???"

    def add_transaction(self, chat_id, c_id, d_id, amount, comment):
        query = "INSERT INTO transactions (chat_id, creditor_id, debtor_id, amount, comment, timestamp) VALUES (%s, %s, %s, %s, %s, %s)"
        params = (chat_id, c_id, d_id, amount, comment, datetime.now())
        self.execute(query, params)

    def get_all_transactions(self, chat_id):
        return self.execute("SELECT id, creditor_id, debtor_id, amount, comment, timestamp FROM transactions WHERE chat_id=%s ORDER BY timestamp ASC", (chat_id,), fetch="all")

db = Database(DATABASE_URL)

# --- 🧑‍🔧 ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def group_only(func):
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if not update.effective_chat or update.effective_chat.type == "private":
            text = f"{EMOJI['warning']} Эта команда работает только в группах."
            if update.callback_query: await update.callback_query.answer(text, show_alert=True)
            elif update.message: await update.message.reply_text(text)
            return ConversationHandler.END if 'conv' in str(func.__qualname__) else None
        if update.effective_user:
            await db.register_user(update.effective_user, update.effective_chat.id)
        return await func(update, context, *args, **kwargs)
    return wrapped

def escape_markdown(text: str) -> str:
    escape_chars = r'_*[]()~`>#+-=|{}.!'; return "".join(f'\\{char}' if char in escape_chars else char for char in str(text))

def get_user_mention(user_id, chat_id):
    name = db.get_user_name(user_id, chat_id); return f"[{escape_markdown(name)}](tg://user?id={user_id})"

def calculate_balances(chat_id: int):
    balances = defaultdict(float)
    transactions = db.get_all_transactions(chat_id)
    if transactions:
        for _, c_id, d_id, amount, _, _ in transactions:
            balances[c_id] += float(amount); balances[d_id] -= float(amount)
    net_debts = defaultdict(float); users = [u[0] for u in db.get_group_members(chat_id)]
    while True:
        debtors = sorted([u for u in users if balances.get(u, 0) < -0.01], key=lambda u: balances.get(u, 0))
        creditors = sorted([u for u in users if balances.get(u, 0) > 0.01], key=lambda u: balances.get(u, 0), reverse=True)
        if not debtors or not creditors: break
        d, c = debtors[0], creditors[0]; amount = min(abs(balances.get(d, 0)), balances.get(c, 0)); net_debts[(d, c)] += amount; balances[d] = balances.get(d, 0) + amount; balances[c] = balances.get(c, 0) - amount
    return net_debts

async def start_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton(f"{EMOJI['money']} Добавить долг", callback_data="add_debt"), InlineKeyboardButton(f"{EMOJI['repay']} Вернуть долг", callback_data="repay")], [InlineKeyboardButton(f"{EMOJI['split']} Разделить счет", callback_data="split"), InlineKeyboardButton(f"{EMOJI['status']} Баланс", callback_data="status")], [InlineKeyboardButton(f"{EMOJI['my_debts']} Мои долги", callback_data="my_debts"), InlineKeyboardButton(f"{EMOJI['history']} История", callback_data="history_menu")]]
    text = "Финансовый Помощник к вашим услугам:";
    if update.callback_query:
        try: await update.callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        except BadRequest as e:
            if "Message is not modified" not in str(e): raise
        await update.callback_query.answer()
    else: msg = await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=InlineKeyboardMarkup(keyboard)); context.chat_data['main_menu_id'] = msg.message_id
async def end_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE): context.user_data.clear(); await start_menu(update, context); return ConversationHandler.END
async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message: await update.message.delete(); context.user_data.clear(); await start_menu(update, context); return ConversationHandler.END
async def process_final_step(update: Update, context: ContextTypes.DEFAULT_TYPE, db_action):
    prompt_msg_id = context.user_data.pop('prompt_msg_id', None);
    try: db_action()
    except Exception as e: print(f"Помилка при записі в базу: {e}")
    if prompt_msg_id:
        try: await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=prompt_msg_id)
        except BadRequest: pass
    await start_menu(update, context); return ConversationHandler.END

# --- 💵 ДИАЛОГ: ДОБАВИТЬ ДОЛГ ---
@group_only
async def add_debt_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; context.user_data['prompt_msg_id'] = query.message.message_id; members = db.get_group_members(query.message.chat_id); keyboard = [[InlineKeyboardButton(name, callback_data=f"user_{uid}")] for uid, name in members]; keyboard.append([InlineKeyboardButton(f"{EMOJI['back']} Вернуться в меню", callback_data="cancel")]); await query.message.edit_text("💰 Кто заплатил?", reply_markup=InlineKeyboardMarkup(keyboard)); await query.answer(); return SELECT_CREDITOR
async def add_debt_select_creditor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; context.user_data['creditor_id'] = int(query.data.split('_')[1]); members = db.get_group_members(query.message.chat_id); keyboard = [[InlineKeyboardButton(name, callback_data=f"user_{uid}")] for uid, name in members if uid != context.user_data['creditor_id']]; keyboard.append([InlineKeyboardButton(f"{EMOJI['back']} Вернуться в меню", callback_data="cancel")]); await query.message.edit_text("За кого заплатили?", reply_markup=InlineKeyboardMarkup(keyboard)); await query.answer(); return SELECT_DEBTOR
async def add_debt_select_debtor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; context.user_data['debtor_id'] = int(query.data.split('_')[1]); await query.message.edit_text("Какая сумма?\n(Можно отправить /cancel, чтобы вернуться в меню)"); await query.answer(); return GET_AMOUNT
async def add_debt_get_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data['amount'] = float(update.message.text.replace(',', '.')); msg_id = context.user_data.get('prompt_msg_id'); await update.message.delete()
        if msg_id: await context.bot.edit_message_text("За что? (Комментарий или /skip для пропуска)", chat_id=update.effective_chat.id, message_id=msg_id)
        return GET_COMMENT
    except (ValueError, TypeError):
        await update.message.reply_text("⚠️ Введите число.", quote=True); return GET_AMOUNT
async def add_debt_save(update: Update, context: ContextTypes.DEFAULT_TYPE, is_skip=False):
    comment = "" if is_skip else update.message.text; await update.message.delete();
    def action(): db.add_transaction(update.effective_chat.id, context.user_data['creditor_id'], context.user_data['debtor_id'], context.user_data['amount'], comment)
    return await process_final_step(update, context, action)

# --- 💸 ДИАЛОГ: ВЕРНУТЬ ДОЛГ (ИСПРАВЛЕН) ---
@group_only
async def repay_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; context.user_data['prompt_msg_id'] = query.message.message_id; members = db.get_group_members(query.message.chat_id); keyboard = [[InlineKeyboardButton(name, callback_data=f"user_{uid}")] for uid, name in members]; keyboard.append([InlineKeyboardButton(f"{EMOJI['back']} Вернуться в меню", callback_data="cancel")]); await query.message.edit_text("💸 Кто возвращает долг?", reply_markup=InlineKeyboardMarkup(keyboard)); await query.answer(); return REPAY_SELECT_DEBTOR
async def repay_select_debtor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; context.user_data['debtor_id'] = int(query.data.split('_')[1]); members = db.get_group_members(query.message.chat_id); keyboard = [[InlineKeyboardButton(name, callback_data=f"user_{uid}")] for uid, name in members if uid != context.user_data['debtor_id']]; keyboard.append([InlineKeyboardButton(f"{EMOJI['back']} Вернуться в меню", callback_data="cancel")]); await query.message.edit_text("Кому возвращают?", reply_markup=InlineKeyboardMarkup(keyboard)); await query.answer(); return REPAY_SELECT_CREDITOR
async def repay_select_creditor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; context.user_data['creditor_id'] = int(query.data.split('_')[1]); await query.message.edit_text("Какую сумму вернули?\n(Можно отправить /cancel, чтобы вернуться в меню)"); await query.answer(); return REPAY_GET_AMOUNT
async def repay_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.replace(',', '.'))
        await update.message.delete()
        def action():
            # --- ОСНОВНОЕ ЛОГИЧЕСКОЕ ИСПРАВЛЕНИЕ ---
            # Тот, кто возвращает (debtor_id), становится "кредитором" в этой транзакции, так как он ОТДАЕТ деньги.
            # Тот, кому возвращают (creditor_id), становится "должником", так как он ПОЛУЧАЕТ деньги (его баланс уменьшается).
            creditor_for_this_transaction = context.user_data['debtor_id']
            debtor_for_this_transaction = context.user_data['creditor_id']
            db.add_transaction(update.effective_chat.id, creditor_for_this_transaction, debtor_for_this_transaction, amount, "Погашение долга")
        return await process_final_step(update, context, action)
    except (ValueError, TypeError):
        await update.message.reply_text("⚠️ Введите число.", quote=True)
        return REPAY_GET_AMOUNT

# --- 🍕 ДИАЛОГ: РАЗДЕЛИТЬ СЧЕТ ---
@group_only
async def split_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; context.user_data['prompt_msg_id'] = query.message.message_id; members = db.get_group_members(query.message.chat_id); keyboard = [[InlineKeyboardButton(name, callback_data=f"user_{uid}")] for uid, name in members]; keyboard.append([InlineKeyboardButton(f"{EMOJI['back']} Вернуться в меню", callback_data="cancel")]); await query.message.edit_text("🍕 Кто заплатил за всех?", reply_markup=InlineKeyboardMarkup(keyboard)); await query.answer(); return SPLIT_SELECT_PAYER
async def split_select_payer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; context.user_data['payer_id'] = int(query.data.split('_')[1]); await query.message.edit_text("Какая общая сумма счета?\n(Можно отправить /cancel, чтобы вернуться в меню)"); await query.answer(); return SPLIT_GET_AMOUNT
async def split_get_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data['amount'] = float(update.message.text.replace(',', '.')); msg_id = context.user_data.get('prompt_msg_id'); await update.message.delete()
        if msg_id: await context.bot.edit_message_text("За что? (Комментарий или /skip для пропуска)", chat_id=update.effective_chat.id, message_id=msg_id)
        return SPLIT_GET_COMMENT
    except (ValueError, TypeError):
        await update.message.reply_text("⚠️ Введите число.", quote=True)
        return SPLIT_GET_AMOUNT
async def split_save(update: Update, context: ContextTypes.DEFAULT_TYPE, is_skip=False):
    comment = "" if is_skip else update.message.text
    await update.message.delete()
    def action():
        chat_id, payer_id, total_amount = update.effective_chat.id, context.user_data['payer_id'], context.user_data['amount']
        members = db.get_group_members(chat_id)
        if len(members) > 1:
            amount_per_person = total_amount / len(members)
            for debtor_id, _ in members:
                if debtor_id != payer_id: db.add_transaction(chat_id, payer_id, debtor_id, amount_per_person, comment)
    return await process_final_step(update, context, action)

# --- ✨ ФУНКЦИИ БЕЗ ДИАЛОГОВ ---
@group_only
async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query, chat_id = update.callback_query, update.effective_chat.id; net_debts = calculate_balances(chat_id); text = f"*{EMOJI['status']} Текущий баланс:*\n\n"
    if not net_debts: text += escape_markdown(f"{EMOJI['party']} Все в расчете!")
    else:
        for (d_id, c_id), amount in net_debts.items(): text += f"{get_user_mention(d_id, chat_id)} должен {get_user_mention(c_id, chat_id)} *{escape_markdown(f'{amount:.2f}')} UAH*\n"
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"{EMOJI['back']} Назад в меню", callback_data="back_to_menu")]]), parse_mode=constants.ParseMode.MARKDOWN_V2); await query.answer()
@group_only
async def my_debts_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query, user_id, chat_id = update.callback_query, update.effective_user.id, update.effective_chat.id; net_debts, i_owe, owe_me = calculate_balances(chat_id), "", ""
    for (d_id, c_id), amount in net_debts.items():
        if d_id == user_id: i_owe += f" • {get_user_mention(c_id, chat_id)}: *{escape_markdown(f'{amount:.2f}')} UAH*\n"
        if c_id == user_id: owe_me += f" • {get_user_mention(d_id, chat_id)}: *{escape_markdown(f'{amount:.2f}')} UAH*\n"
    i_owe_text = i_owe or escape_markdown('Никому.'); owe_me_text = owe_me or escape_markdown('Никто.')
    text = f"*{EMOJI['my_debts']} Моя сводка:*\n\n*Я должен:*\n{i_owe_text}\n\n*Мне должны:*\n{owe_me_text}"
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"{EMOJI['back']} Назад в меню", callback_data="back_to_menu")]]), parse_mode=constants.ParseMode.MARKDOWN_V2); await query.answer()
@group_only
async def history_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query, chat_id = update.callback_query, update.effective_chat.id; transactions = db.get_all_transactions(chat_id)
    if not transactions: await query.answer("История пуста.", show_alert=True); return
    months = sorted(list({t[5].strftime("%Y-%m") for t in transactions}), reverse=True)
    keyboard = [[InlineKeyboardButton(f"{RUSSIAN_MONTHS_NOM[datetime.strptime(m, '%Y-%m').month]} {datetime.strptime(m, '%Y-%m').year}", callback_data=f"history_{m}")] for m in months]
    await query.message.edit_text("Выберите месяц:", reply_markup=InlineKeyboardMarkup(keyboard + [[InlineKeyboardButton(f"{EMOJI['back']} Назад в меню", callback_data="back_to_menu")]])); await query.answer()
@group_only
async def history_show_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query, chat_id = update.callback_query, update.effective_chat.id; year_month = query.data.split('_')[1]; year, month = map(int, year_month.split('-'))
    transactions = [tx for tx in db.get_all_transactions(chat_id) if tx[5].year == year and tx[5].month == month]
    text = f"*{EMOJI['history']} История за {escape_markdown(RUSSIAN_MONTHS_NOM[month])} {year}*\n\n"
    if not transactions: text += escape_markdown("В этом месяце операций не было.")
    else:
        for _, c_id, d_id, amount, comment, ts in transactions:
            date = ts.strftime('%d.%m')
            # --- ИСПРАВЛЕНА ЛОГИКА ОТОБРАЖЕНИЯ ---
            if comment == "Погашение долга":
                # c_id - это тот, кто вернул; d_id - тот, кому вернули
                text += f"`{escape_markdown(date)}`: {get_user_mention(c_id, chat_id)} погасил\\(а\\) долг перед {get_user_mention(d_id, chat_id)} на *{escape_markdown(f'{amount:.2f}')} UAH*\n"
            else:
                # c_id - это кредитор; d_id - это должник
                action_text = "занял\\(а\\) у"; final_comment = f" \\({escape_markdown(comment)}\\)" if comment else ""
                text += f"`{escape_markdown(date)}`: {get_user_mention(d_id, chat_id)} {action_text} {get_user_mention(c_id, chat_id)} на *{escape_markdown(f'{amount:.2f}')} UAH*{final_comment}\n"
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"{EMOJI['back']} К месяцам", callback_data="history_menu")]]), parse_mode=constants.ParseMode.MARKDOWN_V2); await query.answer()

app = Flask('')
@app.route('/')
def home(): return "I'm alive!"
def run_flask(): app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))

def main():
    if not TELEGRAM_BOT_TOKEN:
        print("!!! ОШИБКА: Токен не найден. Добавьте TELEGRAM_BOT_TOKEN в Environment Variables.")
        return
    if not DATABASE_URL:
        print("!!! ОШИБКА: URL базы данных не найден. Добавьте DATABASE_URL в Environment Variables.")
        return

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    fallbacks = [CallbackQueryHandler(end_conversation, pattern="^cancel$"), CommandHandler('cancel', cancel_command)]
    add_debt_handler = ConversationHandler(entry_points=[CallbackQueryHandler(add_debt_start, pattern="^add_debt$")], states={ SELECT_CREDITOR: [CallbackQueryHandler(add_debt_select_creditor, pattern=r"^user_\d+$")], SELECT_DEBTOR: [CallbackQueryHandler(add_debt_select_debtor, pattern=r"^user_\d+$")], GET_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_debt_get_amount)], GET_COMMENT: [CommandHandler('skip', lambda u,c: add_debt_save(u,c,True)), MessageHandler(filters.TEXT & ~filters.COMMAND, add_debt_save)]}, fallbacks=fallbacks, per_user=False, per_chat=True, allow_reentry=True)
    repay_handler = ConversationHandler(entry_points=[CallbackQueryHandler(repay_start, pattern="^repay$")], states={ REPAY_SELECT_DEBTOR: [CallbackQueryHandler(repay_select_debtor, pattern=r"^user_\d+$")], REPAY_SELECT_CREDITOR: [CallbackQueryHandler(repay_select_creditor, pattern=r"^user_\d+$")], REPAY_GET_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, repay_save)]}, fallbacks=fallbacks, per_user=False, per_chat=True, allow_reentry=True)
    split_handler = ConversationHandler(entry_points=[CallbackQueryHandler(split_start, pattern="^split$")], states={ SPLIT_SELECT_PAYER: [CallbackQueryHandler(split_select_payer, pattern=r"^user_\d+$")], SPLIT_GET_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, split_get_amount)], SPLIT_GET_COMMENT: [CommandHandler('skip', lambda u,c: split_save(u,c,True)), MessageHandler(filters.TEXT & ~filters.COMMAND, split_save)]}, fallbacks=fallbacks, per_user=False, per_chat=True, allow_reentry=True)
    application.add_handler(CommandHandler(["start", "menu"], start_menu))
    application.add_handler(CallbackQueryHandler(start_menu, pattern="^back_to_menu$"))
    application.add_handler(add_debt_handler)
    application.add_handler(repay_handler)
    application.add_handler(split_handler)
    application.add_handler(CallbackQueryHandler(status_handler, pattern="^status$"))
    application.add_handler(CallbackQueryHandler(my_debts_handler, pattern="^my_debts$"))
    application.add_handler(CallbackQueryHandler(history_menu_handler, pattern="^history_menu$"))
    application.add_handler(CallbackQueryHandler(history_show_handler, pattern=r"^history_"))

    print("Бот успешно запущен и работает..."); application.run_polling()

def ping_database():
    while True:
        try:
            print("[DB Ping] Sending keep-alive query...")
            db.execute("SELECT 1")
            print("[DB Ping] Keep-alive query successful.")
        except Exception as e:
            print(f"[DB Ping] Error during keep-alive query: {e}")
        time.sleep(600)

if __name__ == "__main__":
    print("Запуск веб-сервера для поддержания активности...")
    flask_thread = Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()
    
    print("Запуск пінгу бази даних для підтримки активності...")
    db_ping_thread = Thread(target=ping_database)
    db_ping_thread.daemon = True
    db_ping_thread.start()

    print("Запуск телеграм-бота...")
    main()

