import logging
import sqlite3
from datetime import datetime
from collections import defaultdict
from functools import wraps

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
TELEGRAM_BOT_TOKEN = "8368912432:AAGa-MJ68Idl2R-bSthILoxsXiZsDL635wQ" # Вставьте ваш токен
DB_NAME = "debt_book_v2.db"

# --- 🎨 ЭМОДЗИ И СТРОКИ ---
EMOJI = {
    "money": "💰", "repay": "💸", "split": "🍕", "status": "📊", "my_debts": "👤",
    "history": "📜", "ok": "✅", "cancel": "❌", "back": "↩️", "user": "👤",
    "warning": "⚠️", "party": "🎉", "lock": "🔒"
}
RUSSIAN_MONTHS_NOM = ["", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]

# --- 🪵 ЛОГИРОВАНИЕ ---
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)

# --- 🔢 СОСТОЯНИЯ ДЛЯ ДИАЛОГОВ ---
(SELECT_CREDITOR, SELECT_DEBTOR, GET_AMOUNT, GET_COMMENT) = range(4)
(REPAY_SELECT_DEBTOR, REPAY_SELECT_CREDITOR, REPAY_GET_AMOUNT) = range(4, 7)
(SPLIT_SELECT_PAYER, SPLIT_GET_AMOUNT, SPLIT_GET_COMMENT) = range(7, 10)

# --- 🗃️ КЛАСС ДЛЯ РАБОТЫ С БАЗОЙ ДАННЫХ ---
class Database:
    def __init__(self, db_name):
        self.conn = sqlite3.connect(db_name, check_same_thread=False)
        self.init_db()
    def execute(self, query, params=(), fetch=None):
        cursor = self.conn.cursor()
        cursor.execute(query, params)
        self.conn.commit()
        if fetch == "one": return cursor.fetchone()
        if fetch == "all": return cursor.fetchall()
    def init_db(self):
        self.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER, chat_id INTEGER, first_name TEXT, username TEXT, PRIMARY KEY (user_id, chat_id))")
        self.execute("CREATE TABLE IF NOT EXISTS transactions (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, creditor_id INTEGER, debtor_id INTEGER, amount REAL, comment TEXT, timestamp TEXT)")
    async def register_user(self, user: User, chat_id: int):
        self.execute("INSERT OR IGNORE INTO users (user_id, chat_id, first_name, username) VALUES (?, ?, ?, ?)", (user.id, chat_id, user.first_name, user.username or f"User{user.id}"))
    def get_group_members(self, chat_id: int): return self.execute("SELECT user_id, first_name FROM users WHERE chat_id = ?", (chat_id,), fetch="all")
    def get_user_name(self, user_id, chat_id):
        res = self.execute("SELECT first_name FROM users WHERE user_id=? AND chat_id=?", (user_id, chat_id), fetch="one")
        return res[0] if res else "???"
    def add_transaction(self, chat_id, c_id, d_id, amount, comment): self.execute("INSERT INTO transactions (chat_id, creditor_id, debtor_id, amount, comment, timestamp) VALUES (?, ?, ?, ?, ?, ?)",(chat_id, c_id, d_id, amount, comment, datetime.now().isoformat()))
    def get_all_transactions(self, chat_id): return self.execute("SELECT id, creditor_id, debtor_id, amount, comment, timestamp FROM transactions WHERE chat_id=? ORDER BY timestamp ASC", (chat_id,), fetch="all")

db = Database(DB_NAME)

# --- 🧑‍🔧 ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def group_only(func):
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if update.effective_chat.type == "private":
            text = f"{EMOJI['warning']} Эта команда работает только в группах."
            if update.callback_query: await update.callback_query.answer(text, show_alert=True)
            else: await update.message.reply_text(text)
            return ConversationHandler.END if 'conv' in str(func.__qualname__) else None
        await db.register_user(update.effective_user, update.effective_chat.id)
        return await func(update, context, *args, **kwargs)
    return wrapped

def escape_markdown(text: str) -> str:
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return "".join(f'\\{char}' if char in escape_chars else char for char in str(text))

def get_user_mention(user_id, chat_id):
    name = db.get_user_name(user_id, chat_id)
    return f"[{escape_markdown(name)}](tg://user?id={user_id})"

def calculate_balances(chat_id: int):
    balances = defaultdict(float)
    for _, c_id, d_id, amount, _, _ in db.get_all_transactions(chat_id):
        balances[c_id] += amount; balances[d_id] -= amount
    net_debts, users = defaultdict(float), [u[0] for u in db.get_group_members(chat_id)]
    while True:
        debtors = sorted([u for u in users if balances[u] < -0.01], key=lambda u: balances[u])
        creditors = sorted([u for u in users if balances[u] > 0.01], key=lambda u: balances[u], reverse=True)
        if not debtors or not creditors: break
        d, c = debtors[0], creditors[0]
        amount = min(abs(balances[d]), balances[c])
        net_debts[(d, c)] += amount; balances[d] += amount; balances[c] -= amount
    return net_debts

# --- ✅ УПРАВЛЕНИЕ МЕНЮ И ДИАЛОГАМИ ---
async def start_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton(f"{EMOJI['money']} Добавить долг", callback_data="add_debt"), InlineKeyboardButton(f"{EMOJI['repay']} Вернуть долг", callback_data="repay")],
        [InlineKeyboardButton(f"{EMOJI['split']} Разделить счет", callback_data="split"), InlineKeyboardButton(f"{EMOJI['status']} Баланс", callback_data="status")],
        [InlineKeyboardButton(f"{EMOJI['my_debts']} Мои долги", callback_data="my_debts"), InlineKeyboardButton(f"{EMOJI['history']} История", callback_data="history_menu")]
    ]
    text = "Финансовый Помощник к вашим услугам:"
    if update.callback_query:
        try:
            await update.callback_query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        except BadRequest as e:
            if "Message is not modified" not in str(e): raise
        await update.callback_query.answer()
    else:
        if 'main_menu_id' in context.chat_data:
            try: await context.bot.delete_message(update.effective_chat.id, context.chat_data.pop('main_menu_id'))
            except BadRequest: pass
        msg = await context.bot.send_message(chat_id=update.effective_chat.id, text=text, reply_markup=InlineKeyboardMarkup(keyboard))
        context.chat_data['main_menu_id'] = msg.message_id

async def end_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await start_menu(update, context)
    return ConversationHandler.END

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.delete()
    context.user_data.clear()
    await start_menu(update, context)
    return ConversationHandler.END

# --- 💵 ДИАЛОГ: ОБЩАЯ ЛОГИКА ---
async def process_final_step(update, context, db_action):
    prompt_msg_id = context.user_data.pop('prompt_msg_id', None)
    db_action()
    if prompt_msg_id:
        try:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=prompt_msg_id)
        except BadRequest:
            pass
    await start_menu(update, context)
    return ConversationHandler.END


# --- 💵 ДИАЛОГ: ДОБАВИТЬ ДОЛГ ---
@group_only
async def add_debt_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    context.user_data['prompt_msg_id'] = query.message.message_id
    members = db.get_group_members(query.message.chat_id)
    keyboard = [[InlineKeyboardButton(name, callback_data=f"user_{uid}")] for uid, name in members]
    keyboard.append([InlineKeyboardButton(f"{EMOJI['back']} Вернуться в меню", callback_data="cancel")])
    await query.message.edit_text("💰 Кто заплатил?", reply_markup=InlineKeyboardMarkup(keyboard))
    await query.answer()
    return SELECT_CREDITOR

async def add_debt_select_creditor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    context.user_data['creditor_id'] = int(query.data.split('_')[1])
    members = db.get_group_members(query.message.chat_id)
    keyboard = [[InlineKeyboardButton(name, callback_data=f"user_{uid}")] for uid, name in members if uid != context.user_data['creditor_id']]
    keyboard.append([InlineKeyboardButton(f"{EMOJI['back']} Вернуться в меню", callback_data="cancel")])
    await query.message.edit_text("За кого заплатили?", reply_markup=InlineKeyboardMarkup(keyboard))
    await query.answer()
    return SELECT_DEBTOR

async def add_debt_select_debtor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    context.user_data['debtor_id'] = int(query.data.split('_')[1])
    await query.message.edit_text("Какая сумма?\n(Можно отправить /cancel, чтобы вернуться в меню)")
    await query.answer()
    return GET_AMOUNT

async def add_debt_get_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data['amount'] = float(update.message.text.replace(',', '.'))
        msg_id = context.user_data.get('prompt_msg_id')
        await update.message.delete()
        if msg_id:
            await context.bot.edit_message_text("За что? (Комментарий или /skip для пропуска)", chat_id=update.effective_chat.id, message_id=msg_id)
        return GET_COMMENT
    except (ValueError, TypeError):
        await update.message.reply_text("⚠️ Введите число.", quote=True)
        return GET_AMOUNT

async def add_debt_save(update: Update, context: ContextTypes.DEFAULT_TYPE, is_skip=False):
    comment = "" if is_skip else update.message.text
    await update.message.delete()
    def action():
        db.add_transaction(update.effective_chat.id, context.user_data['creditor_id'], context.user_data['debtor_id'], context.user_data['amount'], comment)
    return await process_final_step(update, context, action)

# --- 💸 ДИАЛОГ: ВЕРНУТЬ ДОЛГ ---
@group_only
async def repay_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    context.user_data['prompt_msg_id'] = query.message.message_id
    members = db.get_group_members(query.message.chat_id)
    keyboard = [[InlineKeyboardButton(name, callback_data=f"user_{uid}")] for uid, name in members]
    keyboard.append([InlineKeyboardButton(f"{EMOJI['back']} Вернуться в меню", callback_data="cancel")])
    await query.message.edit_text("💸 Кто возвращает долг?", reply_markup=InlineKeyboardMarkup(keyboard))
    await query.answer()
    return REPAY_SELECT_DEBTOR

async def repay_select_debtor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    context.user_data['debtor_id'] = int(query.data.split('_')[1])
    members = db.get_group_members(query.message.chat_id)
    keyboard = [[InlineKeyboardButton(name, callback_data=f"user_{uid}")] for uid, name in members if uid != context.user_data['debtor_id']]
    keyboard.append([InlineKeyboardButton(f"{EMOJI['back']} Вернуться в меню", callback_data="cancel")])
    await query.message.edit_text("Кому возвращают?", reply_markup=InlineKeyboardMarkup(keyboard))
    await query.answer()
    return REPAY_SELECT_CREDITOR

async def repay_select_creditor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    context.user_data['creditor_id'] = int(query.data.split('_')[1])
    await query.message.edit_text("Какую сумму вернули?\n(Можно отправить /cancel, чтобы вернуться в меню)")
    await query.answer()
    return REPAY_GET_AMOUNT

async def repay_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.replace(',', '.'))
        await update.message.delete()
        def action():
            db.add_transaction(update.effective_chat.id, context.user_data['creditor_id'], context.user_data['debtor_id'], amount, "Погашение долга")
        return await process_final_step(update, context, action)
    except (ValueError, TypeError):
        await update.message.reply_text("⚠️ Введите число.", quote=True)
        return REPAY_GET_AMOUNT

# --- 🍕 ДИАЛОГ: РАЗДЕЛИТЬ СЧЕТ ---
@group_only
async def split_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    context.user_data['prompt_msg_id'] = query.message.message_id
    members = db.get_group_members(query.message.chat_id)
    keyboard = [[InlineKeyboardButton(name, callback_data=f"user_{uid}")] for uid, name in members]
    keyboard.append([InlineKeyboardButton(f"{EMOJI['back']} Вернуться в меню", callback_data="cancel")])
    await query.message.edit_text("🍕 Кто заплатил за всех?", reply_markup=InlineKeyboardMarkup(keyboard))
    await query.answer()
    return SPLIT_SELECT_PAYER

async def split_select_payer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    context.user_data['payer_id'] = int(query.data.split('_')[1])
    await query.message.edit_text("Какая общая сумма счета?\n(Можно отправить /cancel, чтобы вернуться в меню)")
    await query.answer()
    return SPLIT_GET_AMOUNT

async def split_get_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data['amount'] = float(update.message.text.replace(',', '.'))
        msg_id = context.user_data.get('prompt_msg_id')
        await update.message.delete()
        if msg_id:
            await context.bot.edit_message_text("За что? (Комментарий или /skip для пропуска)", chat_id=update.effective_chat.id, message_id=msg_id)
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
                if debtor_id != payer_id:
                    db.add_transaction(chat_id, payer_id, debtor_id, amount_per_person, comment)
    return await process_final_step(update, context, action)

# --- ✨ ФУНКЦИИ БЕЗ ДИАЛОГОВ ---
@group_only
async def status_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query, chat_id = update.callback_query, update.effective_chat.id
    net_debts = calculate_balances(chat_id)
    text = f"*{EMOJI['status']} Текущий баланс:*\n\n"
    if not net_debts:
        text += escape_markdown(f"{EMOJI['party']} Все в расчете!")
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
    i_owe_text = i_owe or escape_markdown('Никому.')
    owe_me_text = owe_me or escape_markdown('Никто.')
    text = f"*{EMOJI['my_debts']} Моя сводка:*\n\n*Я должен:*\n{i_owe_text}\n\n*Мне должны:*\n{owe_me_text}"
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"{EMOJI['back']} Назад в меню", callback_data="back_to_menu")]]), parse_mode=constants.ParseMode.MARKDOWN_V2)
    await query.answer()

@group_only
async def history_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query, chat_id = update.callback_query, update.effective_chat.id
    transactions = db.get_all_transactions(chat_id)
    if not transactions:
        await query.answer("История пуста.", show_alert=True)
        return
    months = sorted(list({datetime.fromisoformat(ts).strftime("%Y-%m") for _,_,_,_,_,ts in transactions}), reverse=True)
    keyboard = [[InlineKeyboardButton(f"{RUSSIAN_MONTHS_NOM[datetime.strptime(m, '%Y-%m').month]} {datetime.strptime(m, '%Y-%m').year}", callback_data=f"history_{m}")] for m in months]
    await query.message.edit_text("Выберите месяц:", reply_markup=InlineKeyboardMarkup(keyboard + [[InlineKeyboardButton(f"{EMOJI['back']} Назад в меню", callback_data="back_to_menu")]]))
    await query.answer()

@group_only
async def history_show_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query, chat_id = update.callback_query, update.effective_chat.id
    year_month = query.data.split('_')[1]
    year, month = map(int, year_month.split('-'))
    transactions = [tx for tx in db.get_all_transactions(chat_id) if datetime.fromisoformat(tx[5]).year == year and datetime.fromisoformat(tx[5]).month == month]
    text = f"*{EMOJI['history']} История за {escape_markdown(RUSSIAN_MONTHS_NOM[month])} {year}*\n\n"
    if not transactions:
        text += escape_markdown("В этом месяце операций не было.")
    else:
        for _, c_id, d_id, amount, comment, ts in transactions:
            date = datetime.fromisoformat(ts).strftime('%d.%m')
            if comment == "Погашение долга":
                text += f"`{escape_markdown(date)}`: {get_user_mention(d_id, chat_id)} погасил\\(а\\) долг перед {get_user_mention(c_id, chat_id)} на *{escape_markdown(f'{amount:.2f}')} UAH*\n"
            else:
                action_text = "занял\\(а\\) у"
                final_comment = f" \\({escape_markdown(comment)}\\)" if comment else ""
                text += f"`{escape_markdown(date)}`: {get_user_mention(d_id, chat_id)} {action_text} {get_user_mention(c_id, chat_id)} на *{escape_markdown(f'{amount:.2f}')} UAH*{final_comment}\n"
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"{EMOJI['back']} К месяцам", callback_data="history_menu")]]), parse_mode=constants.ParseMode.MARKDOWN_V2)
    await query.answer()

# --- 🚀 ЗАПУСК БОТА ---
def main():
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    fallbacks = [CallbackQueryHandler(end_conversation, pattern="^cancel$"), CommandHandler('cancel', cancel_command)]

    add_debt_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_debt_start, pattern="^add_debt$")],
        states={
            SELECT_CREDITOR: [CallbackQueryHandler(add_debt_select_creditor, pattern=r"^user_\d+$")],
            SELECT_DEBTOR: [CallbackQueryHandler(add_debt_select_debtor, pattern=r"^user_\d+$")],
            GET_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_debt_get_amount)],
            GET_COMMENT: [CommandHandler('skip', lambda u,c: add_debt_save(u,c,True)), MessageHandler(filters.TEXT & ~filters.COMMAND, add_debt_save)]
        }, fallbacks=fallbacks, per_user=False, per_chat=True, allow_reentry=True
    )
    repay_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(repay_start, pattern="^repay$")],
        states={
            REPAY_SELECT_DEBTOR: [CallbackQueryHandler(repay_select_debtor, pattern=r"^user_\d+$")],
            REPAY_SELECT_CREDITOR: [CallbackQueryHandler(repay_select_creditor, pattern=r"^user_\d+$")],
            REPAY_GET_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, repay_save)]
        }, fallbacks=fallbacks, per_user=False, per_chat=True, allow_reentry=True
    )
    split_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(split_start, pattern="^split$")],
        states={
            SPLIT_SELECT_PAYER: [CallbackQueryHandler(split_select_payer, pattern=r"^user_\d+$")],
            SPLIT_GET_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, split_get_amount)],
            SPLIT_GET_COMMENT: [CommandHandler('skip', lambda u,c: split_save(u,c,True)), MessageHandler(filters.TEXT & ~filters.COMMAND, split_save)]
        }, fallbacks=fallbacks, per_user=False, per_chat=True, allow_reentry=True
    )

    application.add_handler(CommandHandler(["start", "menu"], start_menu))
    application.add_handler(CallbackQueryHandler(start_menu, pattern="^back_to_menu$"))
    
    application.add_handler(add_debt_handler)
    application.add_handler(repay_handler)
    application.add_handler(split_handler)
    
    application.add_handler(CallbackQueryHandler(status_handler, pattern="^status$"))
    application.add_handler(CallbackQueryHandler(my_debts_handler, pattern="^my_debts$"))
    
    application.add_handler(CallbackQueryHandler(history_menu_handler, pattern="^history_menu$"))
    application.add_handler(CallbackQueryHandler(history_show_handler, pattern=r"^history_"))

    application.run_polling()

# --- ИЗМЕНЕНИЕ: ВЕСЬ БЛОК НИЖЕ ДОБАВЛЕН ДЛЯ РАБОТЫ НА СЕРВЕРЕ ---
if __name__ == "__main__":
    from flask import Flask
    import threading
    import os

    # Создаем простое веб-приложение Flask
    app = Flask(__name__)

    @app.route('/')
    def index():
        # Эта страница будет использоваться для проверки, что бот жив
        return "I'm alive!"

    def run_flask():
        # Запускаем Flask в отдельном потоке
        # 0.0.0.0 — чтобы он был виден извне
        # port — Replit сам подставит нужный порт
        port = int(os.environ.get('PORT', 8080))
        app.run(host='0.0.0.0', port=port)

    # Запускаем Flask в фоновом режиме
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.start()
    
    # Запускаем основную функцию бота
    main()