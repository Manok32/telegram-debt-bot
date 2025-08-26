import logging
import os
import time
from threading import Thread
import asyncio
from datetime import datetime, timezone
from collections import defaultdict
from functools import wraps
import json

import psycopg2
from urllib.parse import urlparse

from flask import Flask, request
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
MY_ADMIN_ID = os.environ.get('MY_ADMIN_ID', '0')
WEBHOOK_URL = os.environ.get('WEBHOOK_URL')
TELEGRAM_WEBHOOK_PATH = os.environ.get('TELEGRAM_WEBHOOK_PATH', 'telegram')

# --- 🪵 ЛОГИРОВАНИЕ ---
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

try:
    MY_ADMIN_ID = int(MY_ADMIN_ID)
except ValueError:
    logger.error("MY_ADMIN_ID в переменных окружения не является числом. Команда /clear_all_debts не будет работать.")
    MY_ADMIN_ID = 0

# --- 🎨 ЭМОДЗИ И СТРОКИ ---
EMOJI = {
    "money": "💰", "repay": "💸", "split": "🍕", "status": "📊",
    "my_debts": "👤", "history": "📜", "ok": "✅", "cancel": "❌",
    "back": "↩️", "user": "👤", "warning": "⚠️", "party": "🎉"
}
RUSSIAN_MONTHS_NOM = ["", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]

# --- 🔢 СОСТОЯНИЯ ДЛЯ ДИАЛОГОВ ---
(SELECT_CREDITOR, SELECT_DEBTOR, GET_AMOUNT, GET_COMMENT) = range(4)
(REPAY_SELECT_DEBTOR, REPAY_SELECT_CREDITOR, REPAY_GET_AMOUNT) = range(4, 7)
(SPLIT_SELECT_PAYER, SPLIT_GET_AMOUNT, SPLIT_GET_COMMENT) = range(7, 10)
CONFIRM_CLEAR = 10

# --- 🗃️ КЛАСС ДЛЯ РАБОТЫ С БАЗОЙ ДАННЫХ (PostgreSQL) ---
class Database:
    def __init__(self, conn_url):
        if not conn_url:
            raise ValueError("DATABASE_URL не найден. Убедитесь, что он добавлен в Environment Variables.")
        self.conn_url = conn_url
        self.conn = None
        self._connect()
        self.init_db()

    def _connect(self):
        logger.info("Connecting to PostgreSQL database...")
        try:
            result = urlparse(self.conn_url)
            self.conn = psycopg2.connect(
                dbname=result.path[1:],
                user=result.username,
                password=result.password,
                host=result.hostname,
                port=result.port,
                sslmode='require'
            )
            logger.info("Database connection successful.")
        except psycopg2.OperationalError as e:
            logger.critical(f"!!! КРИТИЧЕСКАЯ ОШИБКА ПОДКЛЮЧЕНИЯ К БАЗЕ: {e}")
            raise

    def execute(self, query, params=(), fetch=None, retries=3):
        for i in range(retries):
            try:
                if self.conn is None or self.conn.closed:
                    self._connect()
                with self.conn.cursor() as cur:
                    cur.execute(query, params)
                    self.conn.commit()
                    if fetch == "one":
                        return cur.fetchone()
                    if fetch == "all":
                        return cur.fetchall()
                return None
            except (psycopg2.InterfaceError, psycopg2.OperationalError) as e:
                logger.warning(f"Database connection lost ({e}). Attempting to reconnect (retry {i+1}/{retries})...")
                self._connect()
            except psycopg2.Error as e:
                logger.error(f"PostgreSQL error during query '{query}': {e}")
                self.conn.rollback()
                raise
        logger.error(f"Failed to execute query after {retries} attempts.")
        raise psycopg2.OperationalError("Failed to execute query after multiple retries.")

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
        try:
            res = self.execute("SELECT first_name FROM users WHERE user_id=%s AND chat_id=%s", (user_id, chat_id), fetch="one")
            if res and len(res) > 0:
                return res[0]
            logger.warning(f"User {user_id} not found in 'users' table for chat {chat_id}. Returning '???'.")
            return "???"
        except Exception as e:
            logger.error(f"Failed to retrieve user name for user_id={user_id}, chat_id={chat_id}: {e}")
            return "???"

    def add_transaction(self, chat_id, c_id, d_id, amount, comment):
        query = "INSERT INTO transactions (chat_id, creditor_id, debtor_id, amount, comment, timestamp) VALUES (%s, %s, %s, %s, %s, %s)"
        params = (chat_id, c_id, d_id, amount, comment, datetime.now(timezone.utc))
        self.execute(query, params)

    def get_all_transactions(self, chat_id):
        return self.execute("SELECT id, creditor_id, debtor_id, amount, comment, timestamp FROM transactions WHERE chat_id=%s ORDER BY timestamp ASC", (chat_id,), fetch="all")

    def clear_transactions_for_chat(self, chat_id: int):
        self.execute("DELETE FROM transactions WHERE chat_id = %s", (chat_id,))

# --- Инициализация DB и Application (Глобальные объекты) ---
app = Flask(__name__)
db: Database = None
application: Application = None

# --- 🧑‍🔧 ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def group_only(func):
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if not update.effective_chat or update.effective_chat.type == "private":
            text = f"{EMOJI['warning']} Эта команда работает только в группах."
            if update.callback_query:
                await update.callback_query.answer(text, show_alert=True)
            else:
                await update.message.reply_text(text)
            return ConversationHandler.END if 'conv' in str(func.__name__) else None
        if update.effective_user:
            await db.register_user(update.effective_user, update.effective_chat.id)
        return await func(update, context, *args, **kwargs)
    return wrapped

def escape_markdown(text: str) -> str:
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return "".join(f'\{char}' if char in escape_chars else char for char in str(text))

def get_user_mention(user_id, chat_id):
    name = db.get_user_name(user_id, chat_id)
    return f"[{escape_markdown(name)}](tg://user?id={user_id})"

def calculate_balances(chat_id: int):
    direct_debts = defaultdict(float)

    transactions = db.get_all_transactions(chat_id)
    for _, creditor_id, debtor_id, amount, _, _ in transactions:
        direct_debts[(debtor_id, creditor_id)] += float(amount)

    net_debts = defaultdict(float)
    processed_pairs = set()

    for (d1, c1), amount1 in direct_debts.items():
        if (d1, c1) in processed_pairs:
            continue

        amount2 = direct_debts.get((c1, d1), 0.0)

        if amount1 > amount2:
            net_amount = amount1 - amount2
            if net_amount > 0.005:
                net_debts[(d1, c1)] = net_amount
        elif amount2 > amount1:
            net_amount = amount2 - amount1
            if net_amount > 0.005:
                net_debts[(c1, d1)] = net_amount
        
        processed_pairs.add((d1, c1))
        processed_pairs.add((c1, d1))
            
    return net_debts

# --- ОБЩИЕ ФУНКЦИИ МЕНЮ И УПРАВЛЕНИЯ ДИАЛОГАМИ ---

async def send_main_menu(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton(f"{EMOJI['money']} Добавить долг", callback_data="add_debt"), InlineKeyboardButton(f"{EMOJI['repay']} Вернуть долг", callback_data="repay")],
        [InlineKeyboardButton(f"{EMOJI['split']} Разделить счет", callback_data="split"), InlineKeyboardButton(f"{EMOJI['status']} Баланс", callback_data="status")],
        [InlineKeyboardButton(f"{EMOJI['my_debts']} Мои долги", callback_data="my_debts"), InlineKeyboardButton(f"{EMOJI['history']} История", callback_data="history_menu")]
    ]
    await context.bot.send_message(chat_id, "Финансовый Помощник к вашим услугам:", reply_markup=InlineKeyboardMarkup(keyboard))

@group_only
async def start_menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.callback_query:
        try:
            await update.callback_query.message.edit_text("Финансовый Помощник к вашим услугам:", reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"{EMOJI['money']} Добавить долг", callback_data="add_debt"), InlineKeyboardButton(f"{EMOJI['repay']} Вернуть долг", callback_data="repay")],
                [InlineKeyboardButton(f"{EMOJI['split']} Разделить счет", callback_data="split"), InlineKeyboardButton(f"{EMOJI['status']} Баланс", callback_data="status")],
                [InlineKeyboardButton(f"{EMOJI['my_debts']} Мои долги", callback_data="my_debts"), InlineKeyboardButton(f"{EMOJI['history']} История", callback_data="history_menu")]
            ]))
        except BadRequest:
            await send_main_menu(update.effective_chat.id, context)
        await update.callback_query.answer()
    else:
        await send_main_menu(update.effective_chat.id, context)

async def end_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        try: await update.callback_query.message.delete()
        except BadRequest: pass
    elif context.user_data.get('dialog_message_id'):
        try: await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=context.user_data['dialog_message_id'])
        except BadRequest: pass
    context.user_data.clear()
    await send_main_menu(update.effective_chat.id, context)
    return ConversationHandler.END

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try: await update.message.delete()
    except BadRequest: pass
    if not context.user_data.get('dialog_message_id'):
        await send_main_menu(update.effective_chat.id, context)
    return ConversationHandler.END

async def back_to_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await start_menu_command(update, context)


# --- 💵 ДИАЛОГ: ДОБАВИТЬ ДОЛГ ---
@group_only
async def add_debt_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    try: await query.message.delete()
    except BadRequest: pass
    members = db.get_group_members(query.message.chat_id)
    if len(members) < 2:
        await context.bot.send_message(query.message.chat_id, f"{EMOJI['warning']} Необходимо как минимум два участника для добавления долга.")
        await send_main_menu(query.message.chat_id, context)
        return ConversationHandler.END
    keyboard = [[InlineKeyboardButton(name, callback_data=f"user_{uid}")] for uid, name in members] + [[InlineKeyboardButton(f"{EMOJI['cancel']} Отмена", callback_data="cancel")]]
    msg = await context.bot.send_message(query.message.chat_id, "💰 Кто заплатил?", reply_markup=InlineKeyboardMarkup(keyboard))
    context.user_data['dialog_message_id'] = msg.message_id
    return SELECT_CREDITOR

async def add_debt_select_creditor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    context.user_data['creditor_id'] = int(query.data.split('_')[1])
    members = db.get_group_members(query.message.chat_id)
    keyboard = [[InlineKeyboardButton(name, callback_data=f"user_{uid}")] for uid, name in members if uid != context.user_data['creditor_id']] + [[InlineKeyboardButton(f"{EMOJI['cancel']} Отмена", callback_data="cancel")]]
    await context.bot.edit_message_text("За кого заплатили?", chat_id=query.message.chat_id, message_id=context.user_data['dialog_message_id'], reply_markup=InlineKeyboardMarkup(keyboard))
    return SELECT_DEBTOR

async def add_debt_select_debtor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    context.user_data['debtor_id'] = int(query.data.split('_')[1])
    await context.bot.edit_message_text("Какая сумма?", chat_id=query.message.chat_id, message_id=context.user_data['dialog_message_id'])
    return GET_AMOUNT

async def add_debt_get_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.replace(',', '.'))
        if amount <= 0: raise ValueError
        context.user_data['amount'] = amount
        try: await update.message.delete()
        except BadRequest: pass
        await context.bot.edit_message_text("За что? (Комментарий или /skip)", chat_id=update.effective_chat.id, message_id=context.user_data['dialog_message_id'])
        return GET_COMMENT
    except (ValueError, TypeError):
        await update.message.reply_text("⚠️ Введите положительное число.", quote=True)
        return GET_AMOUNT

async def add_debt_save(update: Update, context: ContextTypes.DEFAULT_TYPE, is_skip=False):
    comment = "" if is_skip else update.message.text
    try: await update.message.delete()
    except BadRequest: pass
    db.add_transaction(update.effective_chat.id, context.user_data['creditor_id'], context.user_data['debtor_id'], context.user_data['amount'], comment)
    await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=context.user_data['dialog_message_id'])
    await send_main_menu(update.effective_chat.id, context)
    return ConversationHandler.END

# --- 💸 ДИАЛОГ: ВЕРНУТЬ ДОЛГ ---
async def repay_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    try: await query.message.delete()
    except BadRequest: pass
    members = db.get_group_members(query.message.chat_id)
    if len(members) < 2:
        await context.bot.send_message(query.message.chat_id, f"{EMOJI['warning']} Необходимо как минимум два участника для возврата долга.")
        await send_main_menu(query.message.chat_id, context)
        return ConversationHandler.END
    keyboard = [[InlineKeyboardButton(name, callback_data=f"user_{uid}")] for uid, name in members] + [[InlineKeyboardButton(f"{EMOJI['cancel']} Отмена", callback_data="cancel")]]
    msg = await context.bot.send_message(query.message.chat_id, "💸 Кто возвращает долг?", reply_markup=InlineKeyboardMarkup(keyboard))
    context.user_data['dialog_message_id'] = msg.message_id
    return REPAY_SELECT_DEBTOR

async def repay_select_debtor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    context.user_data['debtor_id'] = int(query.data.split('_')[1])
    members = db.get_group_members(query.message.chat_id)
    keyboard = [[InlineKeyboardButton(name, callback_data=f"user_{uid}")] for uid, name in members if uid != context.user_data['debtor_id']] + [[InlineKeyboardButton(f"{EMOJI['cancel']} Отмена", callback_data="cancel")]]
    await context.bot.edit_message_text("Кому возвращают?", chat_id=query.message.chat_id, message_id=context.user_data['dialog_message_id'], reply_markup=InlineKeyboardMarkup(keyboard))
    return REPAY_SELECT_CREDITOR

async def repay_select_creditor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    context.user_data['creditor_id'] = int(query.data.split('_')[1])
    await context.bot.edit_message_text("Какую сумму вернули?", chat_id=query.message.chat_id, message_id=context.user_data['dialog_message_id'])
    return REPAY_GET_AMOUNT

async def repay_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.replace(',', '.'))
        if amount <= 0: raise ValueError
        try: await update.message.delete()
        except BadRequest: pass
        db.add_transaction(update.effective_chat.id, context.user_data['debtor_id'], context.user_data['creditor_id'], amount, "Погашение долга")
        # ✅ ИСПРАВЛЕНИЕ SyntaxError: Эти строки должны быть внутри блока try
        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=context.user_data['dialog_message_id'])
        await send_main_menu(update.effective_chat.id, context)
        return ConversationHandler.END
    except (ValueError, TypeError):
        await update.message.reply_text("⚠️ Введите положительное число.", quote=True)
        return REPAY_GET_AMOUNT

# --- 🍕 ДИАЛОГ: РАЗДЕЛИТЬ СЧЕТ ---
@group_only
async def split_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    try: await query.message.delete()
    except BadRequest: pass
    members = db.get_group_members(query.message.chat_id)
    if len(members) < 2:
        await context.bot.send_message(query.message.chat_id, f"{EMOJI['warning']} Необходимо как минимум два участника для разделения счета.")
        await send_main_menu(query.message.chat_id, context)
        return ConversationHandler.END
    keyboard = [[InlineKeyboardButton(name, callback_data=f"user_{uid}")] for uid, name in members] + [[InlineKeyboardButton(f"{EMOJI['cancel']} Отмена", callback_data="cancel")]]
    msg = await context.bot.send_message(query.message.chat_id, "🍕 Кто заплатил за всех?", reply_markup=InlineKeyboardMarkup(keyboard))
    context.user_data['dialog_message_id'] = msg.message_id
    return SPLIT_SELECT_PAYER

async def split_select_payer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    context.user_data['payer_id'] = int(query.data.split('_')[1])
    await context.bot.edit_message_text("Какая общая сумма счета?", chat_id=query.message.chat_id, message_id=context.user_data['dialog_message_id'])
    return SPLIT_GET_AMOUNT

async def split_get_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.replace(',', '.'))
        if amount <= 0: raise ValueError
        context.user_data['amount'] = amount
        try: await update.message.delete()
        except BadRequest: pass
        await context.bot.edit_message_text("За что? (Комментарий или /skip)", chat_id=update.effective_chat.id, message_id=context.user_data['dialog_message_id'])
        return SPLIT_GET_COMMENT
    except (ValueError, TypeError):
        await update.message.reply_text("⚠️ Введите положительное число.", quote=True)
        return SPLIT_GET_AMOUNT

async def split_save(update: Update, context: ContextTypes.DEFAULT_TYPE, is_skip=False):
    comment = "" if is_skip else update.message.text
    try: await update.message.delete()
    except BadRequest: pass
    chat_id, payer_id, total_amount = update.effective_chat.id, context.user_data['payer_id'], context.user_data['amount']
    members = db.get_group_members(chat_id)
    if len(members) > 1:
        amount_per_person = total_amount / len(members)
        for debtor_id, _ in members:
            if debtor_id != payer_id:
                db.add_transaction(chat_id, payer_id, debtor_id, amount_per_person, comment)
    await context.bot.delete_message(chat_id=chat_id, message_id=context.user_data['dialog_message_id'])
    await send_main_menu(update.effective_chat.id, context)
    return ConversationHandler.END


# --- ✨ ФУНКЦИИ БЕЗ ДИАЛОГОВ ---
@group_only
async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query, chat_id = update.callback_query, update.effective_chat.id
    net_debts = calculate_balances(chat_id)
    text = f"*{EMOJI['status']} Текущий баланс:*\n\n"
    if not net_debts: text += f"{EMOJI['party']} Все в расчете\\!"
    else:
        for (d_id, c_id), amount in net_debts.items():
            text += f"{get_user_mention(d_id, chat_id)} должен {get_user_mention(c_id, chat_id)} *{escape_markdown(f'{amount:.2f}')} UAH*\n"
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"{EMOJI['back']} Назад в меню", callback_data="back_to_menu")]]), parse_mode=constants.ParseMode.MARKDOWN_V2)
    await query.answer()

@group_only
async def my_debts_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query, user_id, chat_id = update.callback_query, update.effective_user.id, update.effective_chat.id
    net_debts, i_owe, owe_me = calculate_balances(chat_id), "", ""
    for (d_id, c_id), amount in net_debts.items():
        if d_id == user_id: i_owe += f" • {get_user_mention(c_id, chat_id)}: *{escape_markdown(f'{amount:.2f}')} UAH*\n"
        if c_id == user_id: owe_me += f" • {get_user_mention(d_id, chat_id)}: *{escape_markdown(f'{amount:.2f}')} UAH*\n"
    text = f"*{EMOJI['my_debts']} Моя сводка:*\n\n*Я должен:*\n{i_owe or escape_markdown('Никому.')}\n\n*Мне должны:*\n{owe_me or escape_markdown('Никто.')}"
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"{EMOJI['back']} Назад в меню", callback_data="back_to_menu")]]), parse_mode=constants.ParseMode.MARKDOWN_V2)
    await query.answer()

@group_only
async def history_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query, chat_id = update.callback_query, update.effective_chat.id
    transactions_raw = db.get_all_transactions(chat_id)
    
    if not transactions_raw:
        await query.answer("История пуста.", show_alert=True)
        return
    
    months = set()
    for t in transactions_raw:
        ts_obj = t[5]
        if not isinstance(ts_obj, datetime):
            logger.warning(f"Timestamp {ts_obj} (type {type(ts_obj)}) for transaction ID {t[0]} is not datetime. Attempting to convert for history menu.")
            try:
                ts_obj = datetime.fromisoformat(str(ts_obj))
            except ValueError:
                logger.error(f"Failed to convert timestamp {t[5]} to datetime for history menu, transaction ID {t[0]}. Skipping this transaction.")
                continue
        months.add(ts_obj.strftime("%Y-%m"))

    sorted_months = sorted(list(months), reverse=True)
    keyboard = []
    for m in sorted_months:
        dt_object = datetime.strptime(m, '%Y-%m')
        keyboard.append([InlineKeyboardButton(f"{RUSSIAN_MONTHS_NOM[dt_object.month]} {dt_object.year}", callback_data=f"history_show_{m}")])
    
    await query.message.edit_text("Выберите месяц:", reply_markup=InlineKeyboardMarkup(keyboard + [[InlineKeyboardButton(f"{EMOJI['back']} Назад в меню", callback_data="back_to_menu")]]))
    await query.answer()

@group_only
async def history_show_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query, chat_id = update.callback_query, update.effective_chat.id
    year_month = query.data.split('_')[-1]
    year, month = map(int, year_month.split('-'))

    processed_transactions = []
    for tx in db.get_all_transactions(chat_id):
        tx_id, c_id, d_id, amount, comment, raw_ts = tx
        
        current_ts_obj = None
        if isinstance(raw_ts, datetime):
            current_ts_obj = raw_ts
        else:
            logger.warning(f"Timestamp {raw_ts} (type {type(raw_ts)}) for transaction ID {tx_id} is not datetime. Attempting to convert.")
            try:
                current_ts_obj = datetime.fromisoformat(str(raw_ts))
            except ValueError:
                logger.error(f"Failed to convert timestamp {raw_ts} to datetime for transaction ID {tx_id}. Skipping this transaction.")
                continue

        if current_ts_obj.year == year and current_ts_obj.month == month:
            processed_transactions.append((tx_id, c_id, d_id, amount, comment, current_ts_obj))
    
    text_header = f"*{EMOJI['history']} История за {escape_markdown(RUSSIAN_MONTHS_NOM[month])} {year}*\n\n"
    text_body = ""

    if not processed_transactions:
        text_body += escape_markdown("В этом месяце операций не было.") + "\n"
    else:
        for _, c_id, d_id, amount, comment, ts_obj in processed_transactions:
            date_str = escape_markdown(ts_obj.strftime('%d.%m'))
            amount_str = escape_markdown(f'{amount:.2f}')
            
            if comment == "Погашение долга":
                text_body += f"`{date_str}`: {get_user_mention(d_id, chat_id)} погасил(а) долг {get_user_mention(c_id, chat_id)} на *{amount_str} UAH*\n"
            else:
                comment_escaped = escape_markdown(comment if comment is not None else "")
                final_comment_part = f" \\({comment_escaped}\\)" if comment_escaped else ""
                text_body += f"`{date_str}`: {get_user_mention(d_id, chat_id)} занял(а) у {get_user_mention(c_id, chat_id)} на *{amount_str} UAH*{final_comment_part}\n"
    
    final_text = text_header + text_body

    await query.message.edit_text(
        final_text,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"{EMOJI['back']} К месяцам", callback_data="history_menu")]]),
        parse_mode=constants.ParseMode.MARKDOWN_V2
    )
    await query.answer()

# --- ✅ КОМАНДА ДЛЯ ОЧИСТКИ ИСТОРИИ (ТОЛЬКО ДЛЯ АДМИНА) ---
@group_only
async def clear_transactions_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    if user_id != MY_ADMIN_ID:
        await update.message.reply_text("Эту команду может использовать только владелец бота.")
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton("✅ Да, удалить все транзакции", callback_data="confirm_clear_yes")],
        [InlineKeyboardButton("❌ Нет, отмена", callback_data="confirm_clear_no")]
    ]
    await update.message.reply_text(
        f"{EMOJI['warning']} *ВНИМАНИЕ!* {EMOJI['warning']}\nВы уверены, что хотите удалить *ВСЕ* финансовые записи в этом чате?\n\nЭто действие необратимо.",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=constants.ParseMode.MARKDOWN_V2
    )
    return CONFIRM_CLEAR

async def clear_transactions_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    
    if query.data == "confirm_clear_yes":
        chat_id = update.effective_chat.id
        db.clear_transactions_for_chat(chat_id)
        await query.message.edit_text("✅ Все транзакции в этом чате были удалены.")
        await send_main_menu(chat_id, context)
    else:
        await query.message.edit_text("Очистка отменена.")
    return ConversationHandler.END


# --- ГЛОБАЛЬНЫЙ ОБРАБОТЧИК ОШИБОК ---
async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update:", exc_info=context.error)
    if update and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "Произошла непредвиденная ошибка. Попробуйте снова.\n"
                "Если ошибка повторяется, сообщите об этом разработчику."
            )
        except BadRequest:
            logger.error("Failed to send error message to user, original message deleted or inaccessible.")
    else:
        logger.error("Error occurred, but no effective message to reply to.")

# --- Flask для поддержания активности на Render и обработки вебхуков ---
@app.route('/')
def home():
    return "I'm alive!"

@app.post(f"/{TELEGRAM_WEBHOOK_PATH}")
async def telegram_webhook_handler():
    global application
    if application is None:
        logger.error("Telegram Application не инициализирован для вебхуков. Попытка инициализации.")
        try:
            await init_bot()
            if application is None:
                return "Error: Bot not ready after re-init", 500
        except Exception as e:
            logger.critical(f"Критическая ошибка при повторной инициализации бота: {e}", exc_info=True)
            return "Error: Critical bot re-init failure", 500

    try:
        update = Update.de_json(request.get_json(force=True), application.bot)
        await application.post_update(update) 
        return "ok"
    except Exception as e:
        logger.error(f"Ошибка при обработке вебхук-обновления: {e}", exc_info=True)
        return "Error", 500

def ping_database():
    global db 
    while True:
        try:
            logger.info("[DB Ping] Отправка запроса на проверку активности...")
            if db: 
                db.execute("SELECT 1")
                logger.info("[DB Ping] Запрос активности успешно выполнен.")
            else:
                logger.warning("[DB Ping] Объект базы данных ещё не инициализирован (неожиданно). Пропуск пинга.")
        except Exception as e:
            logger.error(f"[DB Ping] Ошибка во время запроса активности: {e}")
            try:
                if db: db._connect()
            except Exception as reconnect_e:
                logger.error(f"[DB Ping] Не удалось переподключиться к базе данных: {reconnect_e}")
        time.sleep(600)

# --- 🚀 ЗАПУСК БОТА ---
async def init_bot():
    global db, application
    
    if not TELEGRAM_BOT_TOKEN:
        logger.critical("!!! ОШИБКА: Токен не найден. Убедитесь, что он задан в переменных окружения.")
        return
    if not DATABASE_URL:
        logger.critical("!!! ОШИБКА: URL базы данных не найден. Добавьте DATABASE_URL в Environment Variables.")
        return
    if not WEBHOOK_URL:
        logger.critical("!!! ОШИБКА: WEBHOOK_URL не найден. Добавьте WEBHOOK_URL в Environment Variables (URL вашего сервиса Render).")
        return

    if application is None:
        application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        logger.info("Telegram Application builder запущен.")

    try:
        db = Database(DATABASE_URL)
        logger.info("Подключение к базе данных успешно установлено.")
    except ValueError as e:
        logger.critical(f"Ошибка инициализации базы данных: {e}")
        application = None
        return
    except Exception as e:
        logger.critical(f"Неизвестная ошибка при подключении к базе данных: {e}")
        application = None
        return

    if application is None:
        logger.critical("Application не был инициализирован из-за ошибок DB. Бот не может быть запущен.")
        return

    conv_fallbacks = [CallbackQueryHandler(end_conversation, pattern="^cancel$"), CommandHandler('cancel', cancel_command)]

    add_debt_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_debt_start, pattern="^add_debt$")],
        states={
            SELECT_CREDITOR: [CallbackQueryHandler(add_debt_select_creditor, pattern=r"^user_\d+$")],
            SELECT_DEBTOR: [CallbackQueryHandler(add_debt_select_debtor, pattern=r"^user_\d+$")],
            GET_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_debt_get_amount)],
            GET_COMMENT: [CommandHandler('skip', lambda u,c: add_debt_save(u,c,True)), MessageHandler(filters.TEXT & ~filters.COMMAND, add_debt_save)]
        }, fallbacks=conv_fallbacks, per_user=True
    )
    repay_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(repay_start, pattern="^repay$")],
        states={
            REPAY_SELECT_DEBTOR: [CallbackQueryHandler(repay_select_debtor, pattern=r"^user_\d+$")],
            REPAY_SELECT_CREDITOR: [CallbackQueryHandler(repay_select_creditor, pattern=r"^user_\d+$")],
            REPAY_GET_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, repay_save)]
        }, fallbacks=conv_fallbacks, per_user=True
    )
    split_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(split_start, pattern="^split$")],
        states={
            SPLIT_SELECT_PAYER: [CallbackQueryHandler(split_select_payer, pattern=r"^user_\d+$")],
            SPLIT_GET_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, split_get_amount)],
            SPLIT_GET_COMMENT: [CommandHandler('skip', lambda u,c: split_save(u,c,True)), MessageHandler(filters.TEXT & ~filters.COMMAND, split_save)]
        }, fallbacks=conv_fallbacks, per_user=True
    )
    clear_handler = ConversationHandler(
        entry_points=[CommandHandler("clear_all_debts", clear_transactions_start)],
        states={ CONFIRM_CLEAR: [CallbackQueryHandler(clear_transactions_confirm, pattern=r"^confirm_clear_(yes|no)$")] },
        fallbacks=[CommandHandler('cancel', clear_transactions_start)], per_user=True
    )

    application.add_handler(CommandHandler(["start", "menu"], start_menu_command))
    application.add_handler(CallbackQueryHandler(back_to_menu_handler, pattern="^back_to_menu$"))
    
    application.add_handler(add_debt_handler)
    application.add_handler(repay_handler)
    application.add_handler(split_handler)
    application.add_handler(clear_handler)
    
    application.add_handler(CallbackQueryHandler(status_handler, pattern="^status$"))
    application.add_handler(CallbackQueryHandler(my_debts_handler, pattern="^my_debts$"))
    
    application.add_handler(CallbackQueryHandler(history_menu_handler, pattern="^history_menu$"))
    application.add_handler(CallbackQueryHandler(history_show_handler, pattern=r"^history_show_"))

    application.add_error_handler(error_handler)

    logger.info("Удаление предыдущих вебхуков (если есть)...")
    await application.bot.delete_webhook()
    full_webhook_url = f"{WEBHOOK_URL}{TELEGRAM_WEBHOOK_PATH}"
    logger.info(f"Установка нового вебхука: {full_webhook_url}")
    await application.bot.set_webhook(url=full_webhook_url)

    await application.post_init()
    
    logger.info("Telegram бот успешно настроен. Flask приложение будет обслуживаться Gunicorn.")


if __name__ == "__main__":
    asyncio.run(init_bot())

    logger.info("Запуск потока пинга базы данных для поддержания активности...")
    db_ping_thread = Thread(target=ping_database)
    db_ping_thread.daemon = True
    db_ping_thread.start()
