from pathlib import Path
import os

import telebot
import sqlite3
import threading
import secrets

TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = 9158813748
DELETED_CATEGORY_NAME = "محصولات حذف شده"

if not TOKEN:
    raise RuntimeError(
        "BOT_TOKEN تنظیم نشده است. توکن ربات را در Railway بخش Variables وارد کنید."
    )

bot = telebot.TeleBot(TOKEN)
admin_state = {}
active_order_chat = {}
pending_receipt_order = {}
pending_phone = set()
shop_state = {}

# Database
database_path = os.getenv("DATABASE_PATH", "/data/users.db")
Path(database_path).parent.mkdir(parents=True, exist_ok=True)
db = sqlite3.connect(database_path, check_same_thread=False)
cursor = db.cursor()
db_lock = threading.Lock()
cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY
)
""")
db.commit()

# Payment settings
cursor.execute("""
CREATE TABLE IF NOT EXISTS payment_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    bank_name TEXT DEFAULT '',
    account_holder TEXT DEFAULT '',
    card_number TEXT DEFAULT '',
    iban TEXT DEFAULT '',
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
)
""")

cursor.execute("""
INSERT OR IGNORE INTO payment_settings (id)
VALUES (1)
""")

db.commit()

# Migrate user columns once at startup; preserve existing records.
with db_lock:
    user_columns = {row[1] for row in cursor.execute("PRAGMA table_info(users)").fetchall()}
    for column, definition in (
        ("first_name", "TEXT"),
        ("last_name", "TEXT"),
        ("username", "TEXT"),
        ("phone", "TEXT"),
    ):
        if column not in user_columns:
            cursor.execute(f"ALTER TABLE users ADD COLUMN {column} {definition}")
    db.commit()

def save_user(user):
    with db_lock:
        cursor.execute(
            """
            INSERT OR IGNORE INTO users (user_id)
            VALUES (?)
            """,
            (user.id,)
        )

        cursor.execute(
            """
            UPDATE users
            SET first_name = ?, last_name = ?, username = ?
            WHERE user_id = ?
            """,
            (
                user.first_name or "",
                user.last_name or "",
                user.username or "",
                user.id
            )
        )

        db.commit()




def main_menu():
    markup = telebot.types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        row_width=2
    )
    markup.row(
        telebot.types.KeyboardButton("🛍️ فروشگاه", style="primary"),
        telebot.types.KeyboardButton("👤 حساب کاربری", style="primary")
    )
    markup.row(
        telebot.types.KeyboardButton("🆔 آیدی من", style="primary"),
        telebot.types.KeyboardButton("ℹ️ راهنما", style="primary")
    )
    return markup


def shop_menu():
    markup = telebot.types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        row_width=2
    )
    markup.row(
        telebot.types.KeyboardButton("🛠️ خدمات", style="primary"),
        telebot.types.KeyboardButton("📦 سفارش‌های من", style="primary")
    )
    markup.row(
        telebot.types.KeyboardButton("🧾 ارسال رسید پرداخت", style="primary")
    )
    markup.row(
        telebot.types.KeyboardButton("🔙 بازگشت به منوی اصلی", style="primary")
    )
    return markup


def show_categories(chat_id, page=0):
    per_page = 8

    with db_lock:
        cursor.execute(
            "SELECT COUNT(*) FROM categories "
            "WHERE id != 1 AND name != ?",
            (DELETED_CATEGORY_NAME,)
        )
        total = cursor.fetchone()[0]

        total_pages = (total + per_page - 1) // per_page

        if total == 0:
            categories = []
        else:
            page = max(0, min(page, total_pages - 1))
            shop_state.setdefault(chat_id, {})["category_page"] = page
            cursor.execute(
                """
                SELECT id, name
                FROM categories
                WHERE id != 1 AND name != ?
                ORDER BY id
                LIMIT ? OFFSET ?
                """,
                (DELETED_CATEGORY_NAME, per_page, page * per_page)
            )
            categories = cursor.fetchall()

    if total == 0:
        bot.send_message(
            chat_id,
            "هنوز دسته‌بندی‌ای برای فروشگاه ثبت نشده است.",
            reply_markup=shop_menu()
        )
        return

    markup = telebot.types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        row_width=2
    )

    category_buttons = [
        telebot.types.KeyboardButton(
            f"📂 {category_name}",
            style="primary"
        )
        for category_id, category_name in categories
    ]
    markup.add(*category_buttons, row_width=2)

    if page > 0:
        markup.add(
            telebot.types.KeyboardButton("⬅️ صفحه قبل دسته‌ها", style="primary")
        )

    if page < total_pages - 1:
        markup.add(
            telebot.types.KeyboardButton("➡️ صفحه بعد دسته‌ها", style="primary")
        )

    markup.add(
        telebot.types.KeyboardButton("🔙 بازگشت به فروشگاه", style="primary")
    )

    bot.send_message(
        chat_id,
        f"🛠️ خدمات فروشگاه\nصفحه {page + 1} از {total_pages}\n\n"
        "دستهٔ موردنظرت را انتخاب کن:",
        reply_markup=markup
    )


def show_products(chat_id, category_id, page=0):
    per_page = 8

    with db_lock:
        cursor.execute(
            "SELECT name FROM categories WHERE id = ?",
            (category_id,)
        )
        category = cursor.fetchone()

        if category is None:
            bot.send_message(chat_id, "این دسته‌بندی پیدا نشد.")
            return

        if category[0] == DELETED_CATEGORY_NAME:
            bot.send_message(
                chat_id,
                "⛔ این دسته‌بندی برای خرید در دسترس نیست."
            )
            return

        cursor.execute(
            "SELECT COUNT(*) FROM products WHERE category_id = ? AND active = 1",
            (category_id,)
        )
        total = cursor.fetchone()[0]

        total_pages = (total + per_page - 1) // per_page

        if total > 0:
            page = max(0, min(page, total_pages - 1))
            cursor.execute(
                """
                SELECT id, name, price
                FROM products
                WHERE category_id = ? AND active = 1
                ORDER BY id
                LIMIT ? OFFSET ?
                """,
                (category_id, per_page, page * per_page)
            )
            products = cursor.fetchall()
        else:
            products = []

    state = shop_state.setdefault(chat_id, {})
    state["category_id"] = category_id
    state["product_page"] = page

    if total == 0:
        bot.send_message(
            chat_id,
            f"📦 هنوز محصول فعالی در دسته «{category[0]}» ثبت نشده است.",
            reply_markup=telebot.types.ReplyKeyboardMarkup(
                resize_keyboard=True
            )
        )
        return

    markup = telebot.types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        row_width=2
    )

    product_buttons = [
        telebot.types.KeyboardButton(
            f"📦 {name} | {price:,} تومان [#{product_id}]",
            style="primary"
        )
        for product_id, name, price in products
    ]
    markup.add(*product_buttons, row_width=2)

    if page > 0:
        markup.add(
            telebot.types.KeyboardButton(
                "⬅️ صفحه قبل محصولات",
                style="primary"
            )
        )

    if page < total_pages - 1:
        markup.add(
            telebot.types.KeyboardButton(
                "➡️ صفحه بعد محصولات",
                style="primary"
            )
        )

    markup.add(
        telebot.types.KeyboardButton(
            "🔙 بازگشت به دسته‌ها",
            style="primary"
        )
    )

    bot.send_message(
        chat_id,
        f"📦 محصولات دسته «{category[0]}»\n"
        f"صفحه {page + 1} از {total_pages}\n\n"
        "محصول موردنظرت را انتخاب کن:",
        reply_markup=markup
    )


def show_my_orders(chat_id, user_id, page=0):
    per_page = 5

    with db_lock:
        cursor.execute(
            "SELECT COUNT(*) FROM orders WHERE user_id = ?",
            (user_id,)
        )
        total = cursor.fetchone()[0]

        total_pages = (total + per_page - 1) // per_page

        if total == 0:
            orders = []
        else:
            page = max(0, min(page, total_pages - 1))
            cursor.execute(
                """
                SELECT id, total_amount, status, tracking_code, created_at
                FROM orders
                WHERE user_id = ?
                ORDER BY id DESC
                LIMIT ? OFFSET ?
                """,
                (user_id, per_page, page * per_page)
            )
            orders = cursor.fetchall()

    state = shop_state.setdefault(user_id, {})
    state["orders_page"] = page

    if total == 0:
        bot.send_message(
            chat_id,
            "📭 هنوز سفارشی برای شما ثبت نشده است.",
            reply_markup=shop_menu()
        )
        return

    status_fa = {
        "pending": "🆕 در انتظار بررسی",
        "accepted": "⏳ در انتظار پرداخت",
        "receipt_submitted": "🧾 رسید ارسال شده؛ در انتظار بررسی",
        "payment_confirmed": "💳 پرداخت تأیید شده؛ در انتظار تحویل",
        "payment_rejected": "⚠️ رسید رد شده؛ نیازمند ارسال مجدد",
        "rejected": "❌ سفارش رد شده",
        "completed": "🏁 تکمیل شده"
    }

    for order_id, total_amount, status, tracking_code, created_at in orders:
        with db_lock:
            cursor.execute(
                """
                SELECT product_name, unit_price, quantity
                FROM order_items
                WHERE order_id = ?
                """,
                (order_id,)
            )
            items = cursor.fetchall()

        lines = [
            f"🧾 فاکتور سفارش #{order_id}",
            "━━━━━━━━━━━━━━━━━━━━",
            f"📌 وضعیت: {status_fa.get(status, status)}",
            f"🔑 کد پیگیری: {tracking_code or 'هنوز ثبت نشده'}",
            f"📅 زمان ثبت: {created_at}",
            "",
            "📦 اقلام سفارش",
            "────────────────────",
        ]

        for index, (name, price, quantity) in enumerate(items, start=1):
            lines.extend([
                f"{index}) {name}",
                f"   تعداد: {quantity}",
                f"   قیمت واحد: {price:,} تومان",
                f"   جمع قلم: {price * quantity:,} تومان",
                "────────────────────",
            ])

        lines.extend([
            "",
            f"💰 مبلغ نهایی: {total_amount:,} تومان",
            "━━━━━━━━━━━━━━━━━━━━",
        ])

        bot.send_message(chat_id, "\n".join(lines))

    markup = telebot.types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        row_width=2
    )

    if page > 0:
        markup.add(
            telebot.types.KeyboardButton(
                "⬅️ صفحه قبل سفارش‌ها",
                style="primary"
            )
        )

    if page < total_pages - 1:
        markup.add(
            telebot.types.KeyboardButton(
                "➡️ صفحه بعد سفارش‌ها",
                style="primary"
            )
        )

    markup.add(
        telebot.types.KeyboardButton(
            "🔙 بازگشت به فروشگاه",
            style="primary"
        )
    )

    bot.send_message(
        chat_id,
        f"📦 سفارش‌های شما | صفحه {page + 1} از {total_pages}",
        reply_markup=markup
    )


def admin_menu():
    markup = telebot.types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        row_width=2
    )
    markup.row(
        telebot.types.KeyboardButton("📊 آمار کاربران", style="primary"),
        telebot.types.KeyboardButton("👥 فهرست اعضا", style="primary")
    )
    markup.row(
        telebot.types.KeyboardButton("📢 ارسال همگانی", style="primary"),
        telebot.types.KeyboardButton("🛍️ مدیریت فروشگاه", style="primary")
    )
    markup.row(
        telebot.types.KeyboardButton("📦 مدیریت سفارش‌ها", style="primary"),
        telebot.types.KeyboardButton("💳 تنظیمات پرداخت", style="primary")
    )
    markup.row(
        telebot.types.KeyboardButton("🔙 بازگشت به منو", style="primary")
    )
    return markup

def shop_admin_menu():
    markup = telebot.types.ReplyKeyboardMarkup(
        resize_keyboard=True,
        row_width=2
    )
    markup.row(
        telebot.types.KeyboardButton("📦 مدیریت محصولات", style="primary"),
        telebot.types.KeyboardButton("♻️ بازیابی محصولات حذف‌شده", style="primary")
    )
    markup.row(
        telebot.types.KeyboardButton("➕ افزودن دسته‌بندی", style="primary"),
        telebot.types.KeyboardButton("✏️ تغییر نام دسته‌بندی", style="primary")
    )
    markup.row(
        telebot.types.KeyboardButton("🗑️ حذف دسته‌بندی", style="primary"),
        telebot.types.KeyboardButton("🔙 بازگشت به پنل مدیریت", style="primary")
    )
    return markup

@bot.message_handler(
    func=lambda m: m.text == "🛍️ مدیریت فروشگاه"
)
def shop_admin_panel(message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "⛔ شما اجازه دسترسی ندارید.")
        return

    bot.send_message(
        message.chat.id,
        "🛍️ مدیریت فروشگاه\n\n"
        "از منوی زیر بخش موردنظر رو انتخاب کن.",
        reply_markup=shop_admin_menu()
    )



@bot.message_handler(func=lambda m: m.text == "➕ افزودن دسته‌بندی")
def add_category_start(message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "⛔ شما اجازه دسترسی ندارید.")
        return

    admin_state[message.from_user.id] = "add_category"

    bot.send_message(
        message.chat.id,
        "📝 نام دسته‌بندی جدید را بفرست.\n"
        "برای لغو، /cancel را ارسال کن."
    )


@bot.message_handler(
    func=lambda m: m.text == "✏️ تغییر نام دسته‌بندی"
)
def rename_category_start(message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "⛔ شما اجازه دسترسی ندارید.")
        return

    with db_lock:
        cursor.execute(
            "SELECT id, name FROM categories "
            "WHERE id != 1 AND name != ? ORDER BY id",
            (DELETED_CATEGORY_NAME,)
        )
        categories = cursor.fetchall()

    if not categories:
        bot.send_message(
            message.chat.id,
            "هنوز دسته‌بندی‌ای ثبت نشده است."
        )
        return

    markup = telebot.types.InlineKeyboardMarkup()

    for category_id, category_name in categories:
        markup.add(
            telebot.types.InlineKeyboardButton(
                f"✏️ {category_name}",
                callback_data=f"rename_category_{category_id}"
            )
        )

    bot.send_message(
        message.chat.id,
        "دسته‌بندی موردنظر برای تغییر نام را انتخاب کن:",
        reply_markup=markup
    )


@bot.message_handler(
    func=lambda m: m.text == "🗑️ حذف دسته‌بندی"
)
def delete_category_start(message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "⛔ شما اجازه دسترسی ندارید.")
        return

    with db_lock:
        cursor.execute(
            "SELECT id, name FROM categories ORDER BY id"
        )
        categories = cursor.fetchall()

    if not categories:
        bot.send_message(
            message.chat.id,
            "هنوز دسته‌بندی‌ای ثبت نشده است."
        )
        return

    markup = telebot.types.InlineKeyboardMarkup()

    for category_id, category_name in categories:
        if category_id == 1 or category_name == DELETED_CATEGORY_NAME:
            continue

        markup.add(
            telebot.types.InlineKeyboardButton(
                f"🗑️ {category_name}",
                callback_data=f"delete_category_{category_id}"
            )
        )

    if not markup.keyboard:
        bot.send_message(
            message.chat.id,
            "دسته‌بندی دیگری برای حذف وجود ندارد."
        )
        return

    bot.send_message(
        message.chat.id,
        "دسته‌بندی موردنظر برای حذف را انتخاب کن:",
        reply_markup=markup
    )


@bot.message_handler(
    func=lambda m: m.text == "📦 مدیریت محصولات"
)
def manage_products_start(message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "⛔ شما اجازه دسترسی ندارید.")
        return

    with db_lock:
        cursor.execute(
            "SELECT id, name FROM categories ORDER BY id"
        )
        categories = cursor.fetchall()

    if not categories:
        bot.send_message(
            message.chat.id,
            "هنوز دسته‌بندی‌ای ثبت نشده است."
        )
        return

    markup = telebot.types.InlineKeyboardMarkup()

    for category_id, category_name in categories:
        markup.add(
            telebot.types.InlineKeyboardButton(
                f"📂 {category_name}",
                callback_data=f"manage_category_{category_id}",
                style="primary"
            )
        )

    bot.send_message(
        message.chat.id,
        "📦 مدیریت محصولات\n\n"
        "ابتدا دسته‌بندی موردنظر رو انتخاب کن:",
        reply_markup=markup
    )

@bot.message_handler(
    func=lambda m: m.text == "🔙 بازگشت به پنل مدیریت"
)

def return_admin_panel(message):
    if message.from_user.id != ADMIN_ID:
        return

    bot.send_message(
        message.chat.id,
        "🛠️ به پنل مدیریت برگشتی.",
        reply_markup=admin_menu()
    )

@bot.message_handler(commands=["start"])
def start(message):
    save_user(message.from_user)

    banner_path = Path(__file__).with_name("welcome_banner.png")
    if banner_path.is_file():
        with banner_path.open("rb") as banner:
            bot.send_photo(
                message.chat.id,
                banner,
                caption="💜 به فروشگاه دیجیتال ما خوش آمدید!"
            )

    bot.send_message(
        message.chat.id,
        "سلام رفیق! 🤖\nبرای مشاهده خدمات و محصولات، از منوی زیر استفاده کن.",
        reply_markup=main_menu()
    )


@bot.message_handler(commands=["myid"])
def my_id(message):
    bot.reply_to(
        message,
        f"آیدی عددی شما: {message.from_user.id}"
    )



# ORDER_MANAGEMENT_MENU_V2

ORDER_PAGE_SIZE = 5

ORDER_FILTERS = {
    "all": ("همه سفارش‌ها", None),
    "pending": ("سفارش‌های جدید", "pending"),
    "approved": ("سفارش‌های تأییدشده", "accepted"),
    "receipt_submitted": ("رسیدهای ارسالی", "receipt_submitted"),
    "payment_confirmed": ("پرداخت‌های تأییدشده", "payment_confirmed"),
    "rejected": ("سفارش‌های ردشده", "rejected"),
}


def order_menu_keyboard():
    keyboard = telebot.types.InlineKeyboardMarkup(row_width=2)

    with db_lock:
        cursor.execute("SELECT status, COUNT(*) FROM orders GROUP BY status")
        counts = dict(cursor.fetchall())

    all_count = sum(counts.values())
    pending_count = counts.get("pending", 0)
    approved_count = counts.get("accepted", 0) + counts.get("completed", 0)
    receipt_count = counts.get("receipt_submitted", 0)
    payment_confirmed_count = counts.get("payment_confirmed", 0)
    rejected_count = counts.get("rejected", 0)

    keyboard.add(
        telebot.types.InlineKeyboardButton(
            f"📋 همه سفارش‌ها ({all_count})",
            callback_data="ordpanel_list_all_0",
            style="primary"
        ),
        telebot.types.InlineKeyboardButton(
            f"🆕 سفارش‌های جدید ({pending_count})",
            callback_data="ordpanel_list_pending_0",
            style="primary"
        )
    )
    keyboard.add(
        telebot.types.InlineKeyboardButton(
            f"✅ تأییدشده‌ها ({approved_count})",
            callback_data="ordpanel_list_approved_0",
            style="primary"
        ),
        telebot.types.InlineKeyboardButton(
            f"🧾 رسیدهای ارسالی ({receipt_count})",
            callback_data="ordpanel_list_receipt_submitted_0",
            style="primary"
        )
    )
    keyboard.add(
        telebot.types.InlineKeyboardButton(
            f"💳 پرداخت‌های تأییدشده ({payment_confirmed_count})",
            callback_data="ordpanel_list_payment_confirmed_0",
            style="primary"
        ),
        telebot.types.InlineKeyboardButton(
            f"❌ ردشده‌ها ({rejected_count})",
            callback_data="ordpanel_list_rejected_0",
            style="primary"
        )
    )
    keyboard.add(
        telebot.types.InlineKeyboardButton(
            "🔙 بازگشت به پنل مدیریت",
            callback_data="ordpanel_back",
            style="primary"
        )
    )
    return keyboard

def order_list_keyboard(orders, category, page, total_pages):
    keyboard = telebot.types.InlineKeyboardMarkup(row_width=2)

    order_buttons = []

    for order in orders:
        order_id, user_id, total, status, created_at = order

        status_text = {
            "pending": "🆕 جدید",
            "accepted": "⏳ در انتظار پرداخت",
            "receipt_submitted": "🧾 رسید ارسال شده",
            "payment_confirmed": "💳 پرداخت تأیید شده",
            "payment_rejected": "⚠️ رسید رد شده",
            "rejected": "❌ ردشده",
            "completed": "🏁 تکمیل‌شده"
        }.get(status, status)

        order_buttons.append(
            telebot.types.InlineKeyboardButton(
                f"#{order_id} | {total:,} تومان | {status_text.split()[0]}",
                callback_data=f"ordpanel_view_{order_id}_{category}_{page}",
                    style="primary"
            )
        )

    if order_buttons:
        keyboard.add(*order_buttons)

    navigation = []

    if page > 0:
        navigation.append(
            telebot.types.InlineKeyboardButton(
                "⬅️ صفحه قبل",
                callback_data=f"ordpanel_list_{category}_{page - 1}",
                    style="primary"
            )
        )

    if page + 1 < total_pages:
        navigation.append(
            telebot.types.InlineKeyboardButton(
                "صفحه بعد ➡️",
                callback_data=f"ordpanel_list_{category}_{page + 1}",
                    style="primary"
            )
        )

    if navigation:
        keyboard.row(*navigation)

    keyboard.add(
        telebot.types.InlineKeyboardButton(
            "🔙 بازگشت به دسته‌بندی‌ها",
            callback_data="ordpanel_menu",
                    style="primary"
        )
    )

    return keyboard


def show_order_category(call, category, page):
    if category not in ORDER_FILTERS:
        bot.answer_callback_query(call.id, "دسته‌بندی نامعتبر است.")
        return

    title, status = ORDER_FILTERS[category]
    offset = page * ORDER_PAGE_SIZE

    try:
        with db_lock:
            if status is None:
                cursor.execute("SELECT COUNT(*) FROM orders")
            else:
                cursor.execute(
                    "SELECT COUNT(*) FROM orders WHERE status = ?",
                    (status,)
                )

            total_count = cursor.fetchone()[0]

            # سفارش‌های تکمیل‌شده نیز در دسته تأییدشده‌ها قرار می‌گیرند.
            if category == "approved":
                cursor.execute(
                    """SELECT COUNT(*) FROM orders
                       WHERE status IN ('accepted', 'completed')"""
                )
                total_count = cursor.fetchone()[0]

            if category == "all":
                cursor.execute(
                    """SELECT id, user_id, total_amount, status, created_at
                       FROM orders ORDER BY id DESC LIMIT ? OFFSET ?""",
                    (ORDER_PAGE_SIZE, offset)
                )
            elif category == "approved":
                cursor.execute(
                    """SELECT id, user_id, total_amount, status, created_at
                       FROM orders
                       WHERE status IN ('accepted', 'completed')
                       ORDER BY id DESC LIMIT ? OFFSET ?""",
                    (ORDER_PAGE_SIZE, offset)
                )
            else:
                cursor.execute(
                    """SELECT id, user_id, total_amount, status, created_at
                       FROM orders WHERE status = ?
                       ORDER BY id DESC LIMIT ? OFFSET ?""",
                    (status, ORDER_PAGE_SIZE, offset)
                )

            orders = cursor.fetchall()

        total_pages = max(
            1, (total_count + ORDER_PAGE_SIZE - 1) // ORDER_PAGE_SIZE
        )

        if not orders:
            text = f"📭 در دسته «{title}» سفارشی وجود ندارد."
            keyboard = telebot.types.InlineKeyboardMarkup()
            keyboard.add(
                telebot.types.InlineKeyboardButton(
                    "🔙 بازگشت",
                    callback_data="ordpanel_menu"
                )
            )
        else:
            text = (
                f"📦 {title}\n"
                f"━━━━━━━━━━━━━━\n"
                f"تعداد سفارش‌ها: {total_count}\n"
                f"صفحه {page + 1} از {total_pages}\n\n"
                "برای مشاهده جزئیات، یک سفارش را انتخاب کن:"
            )
            keyboard = order_list_keyboard(
                orders, category, page, total_pages
            )

        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=keyboard
        )

    except Exception as exc:
        print(f"Order category error: {exc}")
        bot.answer_callback_query(
            call.id, "خطا در نمایش سفارش‌ها.", show_alert=True
        )


def show_order_details(call, order_id, category, page):
    try:
        with db_lock:
            cursor.execute(
                """SELECT o.id, o.user_id, o.total_amount, o.status,
                          o.created_at, o.tracking_code,
                          u.first_name, u.last_name, u.username, u.phone
                   FROM orders AS o
                   LEFT JOIN users AS u ON u.user_id = o.user_id
                   WHERE o.id = ?""",
                (order_id,)
            )
            order = cursor.fetchone()

            if order:
                cursor.execute(
                    """SELECT product_name, unit_price, quantity
                       FROM order_items
                       WHERE order_id = ? ORDER BY id""",
                    (order_id,)
                )
                items = cursor.fetchall()
            else:
                items = []

        if not order:
            bot.answer_callback_query(
                call.id, "این سفارش پیدا نشد.", show_alert=True
            )
            return

        (
            order_id, user_id, total, status, created_at,
            tracking_code, first_name, last_name,
            username, phone
        ) = order

        full_name = " ".join(
            part for part in (first_name, last_name) if part
        ) or "ثبت نشده"

        username_text = f"@{username}" if username else "ندارد"

        status_fa = {
            "pending": "در انتظار بررسی اولیه",
            "accepted": "در انتظار پرداخت مشتری",
            "receipt_submitted": "رسید ارسال شده؛ در انتظار بررسی مدیر",
            "payment_confirmed": "پرداخت تأیید شده؛ در انتظار تحویل",
            "payment_rejected": "رسید رد شده؛ در انتظار ارسال مجدد",
            "rejected": "سفارش رد شده",
            "completed": "سفارش تکمیل شده"
        }.get(status, status)

        lines = [
            f"🧾 جزئیات سفارش شماره {order_id}",
            "━━━━━━━━━━━━━━━━",
            "👤 مشخصات مشتری",
            f"نام: {full_name}",
            f"نام کاربری: {username_text}",
            f"آیدی عددی تلگرام: {user_id}",
            f"شماره موبایل: {phone or 'ثبت نشده'}",
            "",
            f"🔑 کد پیگیری: {tracking_code or 'ندارد'}",
            f"📅 زمان ثبت: {created_at}",
            f"📌 وضعیت: {status_fa}",
            "",
            "📦 اقلام سفارش",
            "━━━━━━━━━━━━━━━━"
        ]

        for name, price, quantity in items:
            lines.extend([
                f"🔹 {name}",
                f"تعداد: {quantity}",
                f"قیمت واحد: {price:,} تومان",
                f"جمع: {price * quantity:,} تومان",
                ""
            ])

        lines.extend([
            "━━━━━━━━━━━━━━━━",
            f"💰 مبلغ نهایی: {total:,} تومان"
        ])

        keyboard = telebot.types.InlineKeyboardMarkup()

        if status == "pending":
            keyboard.row(
                telebot.types.InlineKeyboardButton(
                    "✅ تأیید اولیه سفارش",
                    callback_data=f"order_initial_approve_{order_id}",
                    style="success"
                ),
                telebot.types.InlineKeyboardButton(
                    "❌ رد سفارش",
                    callback_data=f"order_initial_reject_{order_id}",
                    style="danger"
                )
            )

        if status == "receipt_submitted":
            keyboard.row(
                telebot.types.InlineKeyboardButton(
                    "✅ تأیید پرداخت",
                    callback_data=f"receipt_confirm_{order_id}",
                    style="primary"
                ),
                telebot.types.InlineKeyboardButton(
                    "❌ رد رسید",
                    callback_data=f"receipt_reject_{order_id}",
                    style="primary"
                )
            )

        keyboard.row(
            telebot.types.InlineKeyboardButton(
                "📦 ارسال محصول / فایل",
                callback_data=f"orderdeliver_{order_id}",
                style="primary"
            )
        )
        keyboard.row(
            telebot.types.InlineKeyboardButton(
                "💬 گفت‌وگو با مشتری",
                callback_data=f"orderchat_{order_id}",
                style="primary"
            )
        )

        keyboard.add(
            telebot.types.InlineKeyboardButton(
                "🔙 بازگشت به فهرست سفارش‌ها",
                callback_data=f"ordpanel_list_{category}_{page}",
            style="primary"
            )
        )

        bot.edit_message_text(
            "\n".join(lines),
            call.message.chat.id,
            call.message.message_id,
            reply_markup=keyboard
        )

    except Exception as exc:
        print(f"Order details error: {exc}")
        bot.answer_callback_query(
            call.id, "خطا در نمایش جزئیات سفارش.", show_alert=True
        )


@bot.message_handler(func=lambda m: m.text == "📦 مدیریت سفارش‌ها")
def show_admin_orders(message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "⛔ شما اجازه دسترسی ندارید.")
        return

    bot.send_message(
        message.chat.id,
        "📦 پنل مدیریت سفارش‌ها\n\n"
        "لطفاً دسته‌بندی موردنظر را انتخاب کن:",
        reply_markup=order_menu_keyboard()
    )


@bot.callback_query_handler(
    func=lambda call: call.data.startswith("ordpanel_")
)
def order_panel_callback(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(
            call.id, "⛔ شما اجازه دسترسی ندارید.", show_alert=True
        )
        return

    data = call.data

    if data == "ordpanel_back":
        bot.answer_callback_query(call.id)
        bot.send_message(
            call.message.chat.id,
            "🛠️ پنل مدیریت",
            reply_markup=admin_menu()
        )
        return

    if data == "ordpanel_menu":
        bot.answer_callback_query(call.id)
        bot.edit_message_text(
            "📦 پنل مدیریت سفارش‌ها\n\n"
            "لطفاً دسته‌بندی موردنظر را انتخاب کن:",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=order_menu_keyboard()
        )
        return

    if data.startswith("ordpanel_list_"):
        try:
            parts = data.split("_")
            page = int(parts[-1])
            category = "_".join(parts[2:-1])
            bot.answer_callback_query(call.id)
            show_order_category(call, category, page)
        except Exception as exc:
            print(f"Order list callback error: {exc}")
            bot.answer_callback_query(call.id, "درخواست نامعتبر است.")
        return

    if data.startswith("ordpanel_view_"):
        try:
            parts = data.split("_")
            order_id = int(parts[2])
            page = int(parts[-1])
            category = "_".join(parts[3:-1])
            bot.answer_callback_query(call.id)
            show_order_details(
                call, order_id, category, page
            )
        except Exception as exc:
            print(f"Order detail callback error: {exc}")
            bot.answer_callback_query(call.id, "درخواست نامعتبر است.")
        return


@bot.callback_query_handler(func=lambda call: call.data.startswith("orderdeliver_"))
def start_order_delivery(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔ دسترسی غیرمجاز", show_alert=True)
        return

    try:
        order_id = int(call.data.split("_")[-1])

        with db_lock:
            cursor.execute(
                "SELECT user_id, status FROM orders WHERE id = ?",
                (order_id,)
            )
            order = cursor.fetchone()

        if not order:
            bot.answer_callback_query(call.id, "سفارش پیدا نشد.", show_alert=True)
            return

        customer_id, status = order

        if status != "payment_confirmed":
            bot.answer_callback_query(
                call.id,
                "ارسال محصول فقط برای سفارش‌های پرداخت‌شده فعال است.",
                show_alert=True
            )
            return

        active_order_chat.clear()
        active_order_chat.update({
            "mode": "delivery",
            "customer_id": customer_id,
            "order_id": order_id
        })

        end_keyboard = telebot.types.ReplyKeyboardMarkup(
            resize_keyboard=True
        )
        end_keyboard.add("🔚 پایان ارسال")

        bot.send_message(
            customer_id,
            f"📦 آماده‌سازی و ارسال سفارش شماره {order_id} آغاز شده است."
        )

        bot.answer_callback_query(call.id, "حالت ارسال فعال شد.")
        bot.send_message(
            ADMIN_ID,
            f"📦 حالت ارسال برای سفارش شماره {order_id} فعال شد.\n"
            "حالا متن، عکس، ویدئو یا فایل موردنظر را ارسال کن.\n"
            "برای پایان، دکمه پایان ارسال را بزن.",
            reply_markup=end_keyboard
        )

    except Exception as exc:
        print(f"Start order delivery error: {exc}")
        bot.answer_callback_query(
            call.id, "خطا در شروع ارسال.", show_alert=True
        )


@bot.callback_query_handler(func=lambda call: call.data.startswith("orderchat_"))
def start_order_chat(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "⛔ دسترسی غیرمجاز", show_alert=True)
        return

    try:
        order_id = int(call.data.split("_")[-1])

        with db_lock:
            cursor.execute(
                "SELECT user_id, status FROM orders WHERE id = ?",
                (order_id,)
            )
            order = cursor.fetchone()

        if not order:
            bot.answer_callback_query(call.id, "سفارش پیدا نشد.", show_alert=True)
            return

        customer_id, status = order

        active_order_chat.clear()
        active_order_chat.update({
            "mode": "chat",
            "customer_id": customer_id,
            "order_id": order_id
        })

        end_keyboard = telebot.types.ReplyKeyboardMarkup(
            resize_keyboard=True
        )
        end_keyboard.add("🔚 پایان گفت‌وگو")

        bot.send_message(
            customer_id,
            f"💬 گفت‌وگوی پشتیبانی سفارش شماره {order_id} آغاز شد. "
            "می‌توانی پیام خود را برای مدیر ارسال کنی."
        )

        bot.answer_callback_query(call.id, "گفت‌وگو فعال شد.")
        bot.send_message(
            ADMIN_ID,
            f"💬 گفت‌وگو با مشتری سفارش شماره {order_id} فعال شد.\n"
            "پیام‌هایت را ارسال کن. برای خروج، دکمه پایان گفت‌وگو را بزن.",
            reply_markup=end_keyboard
        )

    except Exception as exc:
        print(f"Start order chat error: {exc}")
        bot.answer_callback_query(
            call.id, "خطا در شروع گفت‌وگو.", show_alert=True
        )


@bot.message_handler(commands=["admin"])
def admin_panel(message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "⛔ شما اجازه دسترسی ندارید.")
        return

    bot.send_message(
        message.chat.id,
        "🛠️ به پنل مدیریت خوش اومدی.",
        reply_markup=admin_menu()
    )

def payment_settings_menu():
    markup = telebot.types.InlineKeyboardMarkup(row_width=2)

    markup.row(
        telebot.types.InlineKeyboardButton(
            "🏦 تغییر نام بانک",
            callback_data="payset_bank",
            style="primary"
        ),
        telebot.types.InlineKeyboardButton(
            "👤 تغییر صاحب حساب",
            callback_data="payset_holder",
            style="primary"
        )
    )

    markup.row(
        telebot.types.InlineKeyboardButton(
            "💳 تغییر شماره کارت",
            callback_data="payset_card",
            style="primary"
        ),
        telebot.types.InlineKeyboardButton(
            "🔢 تغییر شماره شبا",
            callback_data="payset_iban",
            style="primary"
        )
    )

    markup.row(
        telebot.types.InlineKeyboardButton(
            "🔄 به‌روزرسانی اطلاعات",
            callback_data="payset_refresh",
            style="primary"
        )
    )

    return markup

@bot.message_handler(
    func=lambda m: m.text == "💳 تنظیمات پرداخت"
)
def payment_settings_panel(message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "⛔ شما اجازه دسترسی ندارید.")
        return

    with db_lock:
        cursor.execute("""
            SELECT bank_name, account_holder, card_number, iban
            FROM payment_settings
            WHERE id = 1
        """)
        settings = cursor.fetchone()

    bank, holder, card, iban = settings

    text = (
        "💳 تنظیمات پرداخت\n\n"
        f"🏦 نام بانک: {bank or 'ثبت نشده'}\n"
        f"👤 صاحب حساب: {holder or 'ثبت نشده'}\n"
        f"💳 شماره کارت: {card or 'ثبت نشده'}\n"
        f"🔢 شماره شبا: {iban or 'ثبت نشده'}\n\n"
        "برای تغییر هر مورد، دکمه مربوطه را انتخاب کن."
    )

    bot.send_message(
        message.chat.id,
        text,
        reply_markup=payment_settings_menu()
    )


@bot.callback_query_handler(
    func=lambda call: call.data.startswith("payset_")
)
def payment_settings_callback(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(
            call.id,
            "⛔ دسترسی غیرمجاز"
        )
        return

    fields = {
        "payset_bank": ("bank_name", "نام بانک"),
        "payset_holder": ("account_holder", "نام صاحب حساب"),
        "payset_card": ("card_number", "شماره کارت"),
        "payset_iban": ("iban", "شماره شبا")
    }

    if call.data == "payset_refresh":
        bot.answer_callback_query(call.id)
        with db_lock:
            cursor.execute("""
                SELECT bank_name, account_holder, card_number, iban
                FROM payment_settings
                WHERE id = 1
            """)
            settings = cursor.fetchone()

        bank, holder, card, iban = settings

        text = (
            "💳 تنظیمات پرداخت\n\n"
            f"🏦 نام بانک: {bank or 'ثبت نشده'}\n"
            f"👤 صاحب حساب: {holder or 'ثبت نشده'}\n"
            f"💳 شماره کارت: {card or 'ثبت نشده'}\n"
            f"🔢 شماره شبا: {iban or 'ثبت نشده'}\n\n"
            "برای تغییر هر مورد، دکمه مربوطه را انتخاب کن."
        )

        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=payment_settings_menu()
        )
        return

    if call.data not in fields:
        bot.answer_callback_query(call.id, "درخواست نامعتبر است.")
        return

    column, title = fields[call.data]

    admin_state[ADMIN_ID] = {
        "action": "payment_setting",
        "field": column
    }

    bot.answer_callback_query(call.id)

    bot.send_message(
        call.message.chat.id,
        f"لطفاً {title} جدید را ارسال کن.\n\n"
        "برای لغو عملیات دستور /cancel را بفرست."
    )

@bot.message_handler(func=lambda m: m.text == "👤 حساب کاربری")
def account(message):
    save_user(message.from_user)
    user = message.from_user
    bot.send_message(
        message.chat.id,
        f"👤 نام: {user.first_name}\n"
        f"🆔 آیدی: {user.id}\n"
        f"🔹 نام کاربری: @{user.username or 'ندارد'}",
        reply_markup=main_menu()
    )


@bot.message_handler(func=lambda m: m.text == "🆔 آیدی من")
def show_id(message):
    bot.reply_to(
        message,
        f"آیدی تلگرام شما: {message.from_user.id}"
    )


@bot.message_handler(func=lambda m: m.text == "ℹ️ راهنما")
def help_user(message):
    bot.send_message(
        message.chat.id,
        "از دکمه‌های منو استفاده کن.\n"
        "برای مدیریت ربات، دستور /admin را بزن."
    )


@bot.message_handler(func=lambda m: m.text == "📊 آمار کاربران")
def user_stats(message):
    if message.from_user.id != ADMIN_ID:
        return

    with db_lock:
        cursor.execute("SELECT COUNT(*) FROM users")
        user_count = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM orders")
        total_orders = cursor.fetchone()[0]

        cursor.execute(
            "SELECT COUNT(*) FROM orders WHERE status = ?",
            ("pending",)
        )
        pending_orders = cursor.fetchone()[0]

        cursor.execute(
            "SELECT COUNT(*) FROM orders WHERE status = ?",
            ("accepted",)
        )
        accepted_orders = cursor.fetchone()[0]

        cursor.execute(
            "SELECT COUNT(*) FROM orders WHERE status = ?",
            ("payment_confirmed",)
        )
        confirmed_orders = cursor.fetchone()[0]

        cursor.execute(
            "SELECT COUNT(*) FROM orders WHERE status = ?",
            ("rejected",)
        )
        rejected_orders = cursor.fetchone()[0]

        cursor.execute(
            """
            SELECT COALESCE(SUM(total_amount), 0)
            FROM orders
            WHERE status = ?
            """,
            ("payment_confirmed",)
        )
        confirmed_amount = cursor.fetchone()[0]

        cursor.execute(
            """
            SELECT COALESCE(SUM(total_amount), 0)
            FROM orders
            WHERE status = ?
            """,
            ("pending",)
        )
        pending_amount = cursor.fetchone()[0]

    report = (
        "📊 گزارش آماری فروشگاه\n"
        "━━━━━━━━━━━━━━\n"
        f"👥 تعداد کاربران ثبت‌شده: {user_count}\n"
        "━━━━━━━━━━━━━━\n"
        f"🛍️ کل سفارش‌ها: {total_orders}\n"
        f"⏳ در انتظار پرداخت: {pending_orders}\n"
        f"📝 پذیرفته‌شده: {accepted_orders}\n"
        f"✅ پرداخت تأییدشده: {confirmed_orders}\n"
        f"❌ ردشده: {rejected_orders}\n"
        "━━━━━━━━━━━━━━\n"
        f"💰 مجموع پرداخت‌های تأییدشده: {confirmed_amount:,} تومان\n"
        f"💳 مبلغ سفارش‌های در انتظار: {pending_amount:,} تومان"
    )

    bot.send_message(
        message.chat.id,
        report,
        reply_markup=admin_menu()
    )


@bot.message_handler(func=lambda m: m.text == "👥 فهرست اعضا")
def list_users(message):
    if message.from_user.id != ADMIN_ID:
        return

    with db_lock:
        cursor.execute(
            """
            SELECT user_id, first_name, last_name, username
            FROM users
            ORDER BY user_id
            """
        )
        rows = cursor.fetchall()

    if not rows:
        bot.send_message(
            message.chat.id,
            "هنوز کاربری ثبت نشده است.",
            reply_markup=admin_menu()
        )
        return

    chunks = []
    current = "👥 فهرست کاربران ثبت‌شده:\n\n"

    for uid, first, last, username in rows:
        full_name = " ".join(
            part for part in [first, last] if part
        ) or "بدون نام"

        username_text = f"@{username}" if username else "ندارد"

        entry = (
            f"🆔 آیدی: {uid}\n"
            f"👤 نام: {full_name}\n"
            f"🔹 نام کاربری: {username_text}\n\n"
        )

        if len(current) + len(entry) > 3500:
            chunks.append(current)
            current = ""

        current += entry

    chunks.append(current)

    for i, chunk in enumerate(chunks):
        if i == len(chunks) - 1:
            bot.send_message(
                message.chat.id,
                chunk,
                reply_markup=admin_menu()
            )
        else:
            bot.send_message(message.chat.id, chunk)



@bot.message_handler(func=lambda m: m.text == "📢 ارسال همگانی")
def broadcast_start(message):
    if message.from_user.id != ADMIN_ID:
        return

    admin_state[message.from_user.id] = "broadcast"
    bot.send_message(
        message.chat.id,
        "متن پیام همگانی را بفرست.\n"
        "برای لغو، /cancel را ارسال کن."
    )


@bot.message_handler(commands=["cancel"])
def cancel(message):
    admin_state.pop(message.from_user.id, None)
    bot.send_message(
        message.chat.id,
        "عملیات لغو شد.",
        reply_markup=(
            admin_menu()
            if message.from_user.id == ADMIN_ID
            else main_menu()
        )
    )


@bot.message_handler(func=lambda m: m.text == "🔙 بازگشت به منو")
def back_menu(message):
    if message.from_user.id == ADMIN_ID:
        bot.send_message(
            message.chat.id,
            "به منوی اصلی برگشتی.",
            reply_markup=main_menu()
        )



@bot.message_handler(func=lambda m: m.text == "🛍️ فروشگاه")
def shop(message):
    bot.send_message(
        message.chat.id,
        "🛍️ به فروشگاه خوش آمدی!\n\n"
        "از منوی پایین، یکی از گزینه‌ها را انتخاب کن.",
        reply_markup=shop_menu()
    )



@bot.message_handler(content_types=["contact"])
def receive_order_contact(message):
    user_id = message.from_user.id
    contact = message.contact

    if user_id not in pending_phone:
        return

    if not contact or contact.user_id != user_id:
        bot.send_message(
            message.chat.id,
            "⛔ لطفاً شماره متعلق به حساب تلگرام خودت را با دکمه ارسال مخاطب بفرست."
        )
        return

    phone = (contact.phone_number or "").strip()
    if not phone:
        bot.send_message(
            message.chat.id,
            "شماره موبایل دریافت نشد؛ لطفاً دوباره تلاش کن."
        )
        return

    try:
        with db_lock:
            cursor.execute(
                "INSERT OR IGNORE INTO users (user_id) VALUES (?)",
                (user_id,)
            )
            cursor.execute(
                "UPDATE users SET phone = ? WHERE user_id = ?",
                (phone, user_id)
            )
            db.commit()
    except Exception as exc:
        with db_lock:
            db.rollback()
        print(f"Saving phone failed for user {user_id}: {exc}")
        bot.send_message(
            message.chat.id,
            "ذخیره شماره ناموفق بود؛ لطفاً دوباره تلاش کن."
        )
        return

    bot.send_message(
        message.chat.id,
        "✅ شماره ذخیره شد؛ در حال بررسی سبد خرید..."
    )
    complete_checkout(user_id, message.chat.id, customer=message.from_user)

@bot.message_handler(
    func=lambda m: m.text == "🧾 ارسال رسید پرداخت"
)
def show_receipt_orders(message):
    user_id = message.from_user.id

    if user_id == ADMIN_ID:
        bot.reply_to(message, "این گزینه مخصوص مشتریان است.")
        return

    with db_lock:
        cursor.execute(
            """
            SELECT id, total_amount, tracking_code, status
            FROM orders
            WHERE user_id = ?
            AND status IN ('accepted', 'payment_rejected')
            ORDER BY id DESC
            """,
            (user_id,)
        )
        waiting_orders = cursor.fetchall()

    if not waiting_orders:
        bot.send_message(
            message.chat.id,
            "📭 در حال حاضر سفارش تأییدشده‌ای برای پرداخت نداری."
        )
        return

    keyboard = telebot.types.InlineKeyboardMarkup(row_width=1)

    for order_id, total_amount, tracking_code, status in waiting_orders:
        button_text = (
            f"سفارش #{order_id} | {total_amount:,} تومان"
        )

        if tracking_code:
            button_text += f" | {tracking_code}"

        keyboard.add(
            telebot.types.InlineKeyboardButton(
                button_text,
                callback_data=f"receipt_select_{order_id}"
            )
        )

    bot.send_message(
        message.chat.id,
        "🧾 لطفاً سفارش موردنظر را برای ارسال رسید انتخاب کن:",
        reply_markup=keyboard
    )

@bot.callback_query_handler(
    func=lambda call: call.data.startswith("receipt_select_")
)
def select_receipt_order(call):
    user_id = call.from_user.id

    if user_id == ADMIN_ID:
        bot.answer_callback_query(
            call.id,
            "این گزینه مخصوص مشتریان است.",
            show_alert=True
        )
        return

    try:
        order_id = int(call.data.split("_")[-1])
    except ValueError:
        bot.answer_callback_query(
            call.id,
            "شناسه سفارش نامعتبر است.",
            show_alert=True
        )
        return

    with db_lock:
        cursor.execute(
            """
            SELECT user_id, status
            FROM orders
            WHERE id = ?
            """,
            (order_id,)
        )
        order = cursor.fetchone()

    if not order or order[0] != user_id:
        bot.answer_callback_query(
            call.id,
            "این سفارش متعلق به شما نیست یا وجود ندارد.",
            show_alert=True
        )
        return

    if order[1] not in ("accepted", "payment_rejected"):
        bot.answer_callback_query(
            call.id,
            "این سفارش در حال حاضر آماده دریافت رسید نیست.",
            show_alert=True
        )
        return

    pending_receipt_order[user_id] = order_id

    bot.answer_callback_query(call.id, "سفارش انتخاب شد.")

    bot.send_message(
        call.message.chat.id,
        f"📸 لطفاً تصویر رسید پرداخت سفارش شماره {order_id} را ارسال کن.\n"
        "پس از ارسال، رسید برای مدیر فرستاده می‌شود."
    )

@bot.message_handler(
    func=lambda m: m.text != "♻️ بازیابی محصولات حذف‌شده",
    content_types=["text", "photo", "document", "video", "audio",
                   "voice", "animation", "sticker"]
)
def other_messages(message):
    user_id = message.from_user.id
    save_user(message.from_user)

    # انتقال پیام‌ها در گفت‌وگوی خصوصی سفارش
    chat = active_order_chat

    if chat and chat.get("mode") == "delivery":
        customer_id = chat.get("customer_id")
        order_id = chat.get("order_id")

        if user_id == ADMIN_ID:
            if message.content_type == "text" and message.text.strip() == "🔚 پایان ارسال":
                active_order_chat.clear()
                bot.send_message(
                    customer_id,
                    f"📦 ارسال محصول سفارش شماره {order_id} پایان یافت."
                )
                bot.send_message(
                    ADMIN_ID,
                    f"ارسال محصول سفارش شماره {order_id} پایان یافت.",
                    reply_markup=admin_menu()
                )
                return

            try:
                bot.copy_message(
                    customer_id,
                    message.chat.id,
                    message.message_id
                )
                bot.send_message(
                    ADMIN_ID,
                    "✅ محتوا برای مشتری ارسال شد."
                )
            except Exception as exc:
                print(f"Order delivery forwarding error: {exc}")
                bot.send_message(
                    ADMIN_ID,
                    "⚠️ ارسال محتوا برای مشتری ناموفق بود."
                )
            return

        if user_id == customer_id:
            bot.send_message(
                customer_id,
                "در حال حاضر حالت ارسال محصول فعال است. برای پشتیبانی، پس از پایان ارسال پیام بده."
            )
            return

    # دریافت رسید سفارش انتخاب‌شده توسط مشتری
    if (
        user_id != ADMIN_ID
        and user_id in pending_receipt_order
        and not (
            active_order_chat
            and active_order_chat.get("mode") == "delivery"
        )
    ):
        order_id = pending_receipt_order[user_id]

        if message.content_type != "photo":
            bot.reply_to(
                message,
                "⚠️ لطفاً تصویر رسید پرداخت را به‌صورت عکس ارسال کن."
            )
            return

        receipt_file_id = message.photo[-1].file_id

        with db_lock:
            cursor.execute(
                """
                SELECT total_amount, tracking_code, status
                FROM orders
                WHERE id = ? AND user_id = ?
                """,
                (order_id, user_id)
            )
            order = cursor.fetchone()

        if not order or order[2] not in ("accepted", "payment_rejected"):
            bot.reply_to(
                message,
                "⚠️ این سفارش دیگر در وضعیت دریافت رسید نیست. لطفاً فهرست سفارش‌ها را دوباره بررسی کن."
            )
            pending_receipt_order.pop(user_id, None)
            return

        total_amount, tracking_code, previous_status = order

        with db_lock:
            cursor.execute(
                """
                UPDATE orders
                SET receipt_file_id = ?, status = 'receipt_submitted'
                WHERE id = ? AND user_id = ?
                AND status IN ('accepted', 'payment_rejected')
                """,
                (receipt_file_id, order_id, user_id)
            )
            updated = cursor.rowcount
            db.commit()

        if updated != 1:
            bot.reply_to(
                message,
                "⚠️ وضعیت سفارش تغییر کرده است. لطفاً دوباره فهرست سفارش‌ها را بررسی کن."
            )
            return

        try:
            receipt_keyboard = telebot.types.InlineKeyboardMarkup(row_width=2)
            receipt_keyboard.add(
                telebot.types.InlineKeyboardButton(
                    "✅ تأیید پرداخت",
                    callback_data=f"receipt_confirm_{order_id}"
                ),
                telebot.types.InlineKeyboardButton(
                    "❌ رد رسید",
                    callback_data=f"receipt_reject_{order_id}"
                )
            )

            bot.send_photo(
                ADMIN_ID,
                receipt_file_id,
                caption=(
                    "🧾 رسید پرداخت جدید دریافت شد.\n"
                    "━━━━━━━━━━━━━━\n"
                    f"📦 شماره سفارش: {order_id}\n"
                    f"👤 شناسه مشتری: {user_id}\n"
                    f"💰 مبلغ سفارش: {total_amount:,} تومان\n"
                    f"🔑 کد پیگیری: {tracking_code or 'ثبت نشده'}\n"
                    "📌 وضعیت: در انتظار بررسی رسید"
                ),
                reply_markup=receipt_keyboard
            )

        except Exception as exc:
            print(f"Receipt notification error: {exc}")

            with db_lock:
                cursor.execute(
                    """
                    UPDATE orders
                    SET status = ?, receipt_file_id = NULL
                    WHERE id = ? AND user_id = ?
                    AND status = 'receipt_submitted'
                    """,
                    (previous_status, order_id, user_id)
                )
                db.commit()

            bot.send_message(
                message.chat.id,
                "⚠️ ارسال رسید برای مدیر ناموفق بود. لطفاً دوباره تلاش کن."
            )
            return

        pending_receipt_order.pop(user_id, None)
        bot.send_message(
            message.chat.id,
            "✅ تصویر رسید پرداخت دریافت شد.\n"
            "پس از بررسی مدیر، نتیجه به تو اطلاع داده می‌شود."
        )
        return

    if chat and chat.get("mode") == "chat":
        customer_id = chat.get("customer_id")
        order_id = chat.get("order_id")

        if user_id == ADMIN_ID:
            if message.content_type == "text" and message.text.strip() == "🔚 پایان گفت‌وگو":
                active_order_chat.clear()
                bot.send_message(
                    customer_id,
                    "گفت‌وگوی پشتیبانی سفارش پایان یافت."
                )
                bot.send_message(
                    ADMIN_ID,
                    f"گفت‌وگوی سفارش شماره {order_id} پایان یافت.",
                    reply_markup=admin_menu()
                )
                return

            try:
                bot.copy_message(
                    customer_id,
                    message.chat.id,
                    message.message_id
                )
                bot.send_message(
                    ADMIN_ID,
                    "✅ پیام برای مشتری ارسال شد."
                )
            except Exception as exc:
                print(f"Admin chat forwarding error: {exc}")
                bot.send_message(
                    ADMIN_ID,
                    "⚠️ ارسال پیام برای مشتری ناموفق بود."
                )
            return

        if user_id == customer_id:
            try:
                with db_lock:
                    cursor.execute(
                        "SELECT first_name, last_name, username, phone FROM users WHERE user_id = ?",
                        (customer_id,)
                    )
                    customer_info = cursor.fetchone()

                if customer_info:
                    first_name, last_name, username, phone = customer_info
                else:
                    first_name, last_name, username, phone = "", "", "", ""

                full_name = f"{first_name} {last_name}".strip() or "ثبت نشده"
                username_text = f"@{username}" if username else "ثبت نشده"

                bot.send_message(
                    ADMIN_ID,
                    f"💬 پیام جدید مشتری\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"📦 شماره سفارش: {order_id}\n"
                    f"👤 نام مشتری: {full_name}\n"
                    f"🔹 نام کاربری: {username_text}\n"
                    f"🆔 شناسه تلگرام: {customer_id}\n"
                    f"📱 شماره همراه: {phone or 'ثبت نشده'}\n"
                    f"━━━━━━━━━━━━━━"
                )
                bot.copy_message(
                    ADMIN_ID,
                    message.chat.id,
                    message.message_id
                )
            except Exception as exc:
                print(f"Customer chat forwarding error: {exc}")
                bot.send_message(
                    customer_id,
                    "⚠️ ارسال پیام شما به مدیر ناموفق بود."
                )
            return

    # پردازش مراحل افزودن محصول
    state = admin_state.get(user_id)
    # ذخیره تنظیمات پرداخت
    if (
        user_id == ADMIN_ID
        and isinstance(state, dict)
        and state.get("action") == "payment_setting"
    ):
        if message.content_type != "text":
            bot.send_message(
                message.chat.id,
                "لطفاً اطلاعات را فقط به‌صورت متن ارسال کن."
            )
            return

        value = message.text.strip()

        if value == "/cancel":
            admin_state.pop(user_id, None)
            bot.send_message(
                message.chat.id,
                "عملیات لغو شد.",
                reply_markup=admin_menu()
            )
            return

        field = state["field"]

        if not value:
            bot.send_message(
                message.chat.id,
                "مقدار نمی‌تواند خالی باشد. دوباره ارسال کن."
            )
            return

        allowed_fields = {
            "bank_name",
            "account_holder",
            "card_number",
            "iban"
        }

        if field not in allowed_fields:
            admin_state.pop(user_id, None)
            bot.send_message(
                message.chat.id,
                "خطا در انتخاب فیلد."
            )
            return

        with db_lock:
            cursor.execute(
                f"UPDATE payment_settings SET {field} = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = 1",
                (value,)
            )
            db.commit()

        admin_state.pop(user_id, None)

        bot.send_message(
            message.chat.id,
            "✅ اطلاعات پرداخت با موفقیت ذخیره شد.",
            reply_markup=admin_menu()
        )
        return


    if (
        user_id == ADMIN_ID
        and isinstance(state, dict)
        and state.get("action") == "rename_category"
    ):
        if message.content_type != "text":
            bot.send_message(
                message.chat.id,
                "لطفاً نام جدید دسته‌بندی را به‌صورت متن بفرست."
            )
            return

        new_name = message.text.strip()

        if new_name == "/cancel":
            admin_state.pop(user_id, None)
            bot.send_message(
                message.chat.id,
                "تغییر نام دسته‌بندی لغو شد.",
                reply_markup=shop_admin_menu()
            )
            return

        if not new_name:
            bot.send_message(
                message.chat.id,
                "نام دسته‌بندی نمی‌تواند خالی باشد."
            )
            return

        if new_name == DELETED_CATEGORY_NAME:
            bot.send_message(
                message.chat.id,
                "این نام برای دسته‌بندی سیستمی رزرو شده است."
            )
            return

        category_id = state.get("category_id")

        try:
            with db_lock:
                cursor.execute(
                    "SELECT name FROM categories WHERE id = ?",
                    (category_id,)
                )
                current_category = cursor.fetchone()

                if (
                    current_category is None
                    or category_id == 1
                    or current_category[0] == DELETED_CATEGORY_NAME
                ):
                    rename_error = "این دسته‌بندی دیگر قابل تغییر نام نیست."
                else:
                    cursor.execute(
                        "UPDATE categories SET name = ? WHERE id = ?",
                        (new_name, category_id)
                    )
                    db.commit()
                    rename_error = None

            if rename_error:
                bot.send_message(message.chat.id, f"⚠️ {rename_error}")
                admin_state.pop(user_id, None)
            else:
                admin_state.pop(user_id, None)
                bot.send_message(
                    message.chat.id,
                    f"✅ نام دسته‌بندی به «{new_name}» تغییر کرد.",
                    reply_markup=shop_admin_menu()
                )

        except sqlite3.IntegrityError:
            bot.send_message(
                message.chat.id,
                "⚠️ این نام قبلاً وجود دارد. نام دیگری انتخاب کن."
            )
        except sqlite3.Error:
            with db_lock:
                db.rollback()
            bot.send_message(
                message.chat.id,
                "⚠️ خطای پایگاه داده هنگام تغییر نام."
            )

        return


    if (
        user_id == ADMIN_ID
        and isinstance(state, dict)
        and state.get("action") == "edit_product_photo"
    ):
        if message.content_type != "photo":
            bot.send_message(
                message.chat.id,
                "لطفاً عکس جدید را به‌صورت تصویر ارسال کن."
            )
            return

        photo_file_id = message.photo[-1].file_id
        product_id = state["product_id"]

        with db_lock:
            cursor.execute(
                "UPDATE products SET photo_file_id = ? WHERE id = ?",
                (photo_file_id, product_id)
            )
            db.commit()

        admin_state.pop(user_id, None)

        bot.send_message(
            message.chat.id,
            "✅ عکس محصول با موفقیت تغییر کرد.",
            reply_markup=shop_admin_menu()
        )
        return

    if (
        user_id == ADMIN_ID
        and isinstance(state, dict)
        and state.get("action") == "edit_product_name"
    ):
        if message.content_type != "text":
            bot.send_message(
                message.chat.id,
                "لطفاً نام جدید محصول را به‌صورت متن بفرست."
            )
            return

        new_name = message.text.strip()

        if not new_name:
            bot.send_message(
                message.chat.id,
                "نام محصول نمی‌تواند خالی باشد. دوباره بفرست."
            )
            return
        product_id = state["product_id"]

        with db_lock:
            cursor.execute(
                "UPDATE products SET name = ? WHERE id = ?",
                (new_name, product_id)
            )
            db.commit()

        admin_state.pop(user_id, None)

        bot.send_message(
            message.chat.id,
            f"✅ نام محصول با موفقیت تغییر کرد.\n\n"
            f"نام جدید: {new_name}",
            reply_markup=shop_admin_menu()
        )
        return


    if (
        user_id == ADMIN_ID
        and isinstance(state, dict)
        and state.get("action") == "edit_product_price"
    ):
        if message.content_type != "text":
            bot.send_message(
                message.chat.id,
                "لطفاً قیمت را فقط به‌صورت عدد بفرست."
            )
            return

        price_text = message.text.strip().replace(",", "").replace("٬", "")

        if not price_text.isdigit() or int(price_text) <= 0:
            bot.send_message(
                message.chat.id,
                "⚠️ قیمت معتبر نیست. عددی بزرگ‌تر از صفر وارد کن؛ مثلاً 350000"
            )
            return

        new_price = int(price_text)
        product_id = state["product_id"]

        with db_lock:
            cursor.execute(
                "UPDATE products SET price = ? WHERE id = ?",
                (new_price, product_id)
            )
            db.commit()

        admin_state.pop(user_id, None)

        bot.send_message(
            message.chat.id,
            f"✅ قیمت محصول با موفقیت تغییر کرد.\n\n"
            f"💰 قیمت جدید: {new_price:,} تومان",
            reply_markup=shop_admin_menu()
        )
        return


    if (
        user_id == ADMIN_ID
        and isinstance(state, dict)
        and state.get("action") == "edit_product_description"
    ):
        if message.content_type != "text":
            bot.send_message(
                message.chat.id,
                "لطفاً توضیحات را به‌صورت متن ارسال کن."
            )
            return

        new_description = message.text.strip()

        if new_description == "-":
            new_description = ""

        product_id = state["product_id"]

        with db_lock:
            cursor.execute(
                "UPDATE products SET description = ? WHERE id = ?",
                (new_description, product_id)
            )
            db.commit()

        admin_state.pop(user_id, None)

        bot.send_message(
            message.chat.id,
            "✅ توضیحات محصول با موفقیت تغییر کرد.\n\n"
            f"📝 توضیحات جدید: {new_description or 'توضیحات پاک شد.'}",
            reply_markup=shop_admin_menu()
        )
        return


    if user_id == ADMIN_ID and isinstance(state, dict) and state.get("action") == "add_product":
        if message.content_type == "text" and message.text.strip() == "/cancel":
            admin_state.pop(user_id, None)
            bot.send_message(
                message.chat.id,
                "افزودن محصول لغو شد.",
                reply_markup=shop_admin_menu()
            )
            return

        if state.get("step") == "name":
            if message.content_type != "text":
                bot.send_message(
                    message.chat.id,
                    "لطفاً نام محصول را به‌صورت متن بفرست."
                )
                return

            name = message.text.strip()

            if not name:
                bot.send_message(
                    message.chat.id,
                    "نام محصول نمی‌تواند خالی باشد. دوباره بفرست."
                )
                return

            state["name"] = name
            state["step"] = "price"

            bot.send_message(
                message.chat.id,
                "💰 قیمت محصول را به تومان و فقط به‌صورت عدد بفرست."
            )
            return
        if state.get("step") == "price":
            if message.content_type != "text":
                bot.send_message(
                    message.chat.id,
                    "لطفاً قیمت را فقط به‌صورت عدد بفرست."
                )
                return

            price_text = message.text.strip().replace(",", "").replace("٬", "")

            if not price_text.isdigit():
                bot.send_message(
                    message.chat.id,
                    "⚠️ قیمت معتبر نیست. فقط عدد وارد کن؛ مثلاً 250000"
                )
                return

            price = int(price_text)

            if price <= 0:
                bot.send_message(
                    message.chat.id,
                    "قیمت باید بیشتر از صفر باشد. دوباره بفرست."
                )
                return

            state["price"] = price
            state["step"] = "description"

            bot.send_message(
                message.chat.id,
                "📝 حالا توضیحات محصول را بفرست.\n"
                "اگر توضیحی نداری، یک خط تیره (-) بفرست."
            )
            return
        if state.get("step") == "description":
            if message.content_type != "text":
                bot.send_message(
                    message.chat.id,
                    "لطفاً توضیحات محصول را به‌صورت متن بفرست."
                )
                return

            description = message.text.strip()

            if description == "-":
                description = ""

            state["description"] = description
            state["step"] = "photo"

            bot.send_message(
                message.chat.id,
                "🖼️ حالا عکس محصول را به‌صورت تصویر بفرست."
            )
            return

        if state.get("step") == "photo":
            if message.content_type != "photo":
                bot.send_message(
                    message.chat.id,
                    "لطفاً عکس محصول را به‌صورت تصویر ارسال کن."
                )
                return

            photo_file_id = message.photo[-1].file_id

            category_id = state["category_id"]
            name = state["name"]
            price = state["price"]
            description = state.get("description", "")

            invalid_category = False

            with db_lock:
                cursor.execute(
                    "SELECT name FROM categories WHERE id = ?",
                    (category_id,)
                )
                target_category = cursor.fetchone()

                if (
                    target_category is None
                    or target_category[0] == DELETED_CATEGORY_NAME
                ):
                    invalid_category = True
                else:
                    cursor.execute(
                        """
                        INSERT INTO products
                        (name, description, price, category_id, active, photo_file_id)
                        VALUES (?, ?, ?, ?, 1, ?)
                        """,
                        (
                            name,
                            description,
                            price,
                            category_id,
                            photo_file_id
                        )
                    )
                    db.commit()

            if invalid_category:
                admin_state.pop(user_id, None)
                bot.send_message(
                    message.chat.id,
                    "⛔ این دسته‌بندی برای افزودن محصول معتبر نیست.",
                    reply_markup=shop_admin_menu()
                )
                return

            admin_state.pop(user_id, None)

            bot.send_message(
                message.chat.id,
                "✅ محصول با موفقیت ثبت شد!\n\n"
                f"📦 نام: {name}\n"
                f"💰 قیمت: {price:,} تومان\n"
                f"📝 توضیحات: {description or 'ثبت نشده'}",
                reply_markup=shop_admin_menu()
            )
            return

    if user_id == ADMIN_ID and admin_state.get(user_id) == "add_category":
        if message.content_type != "text":
            bot.send_message(
                message.chat.id,
                "لطفاً نام دسته‌بندی را به‌صورت متن بفرست."
            )
            return

        name = message.text.strip()

        if not name:
            bot.send_message(
                message.chat.id,
                "نام دسته‌بندی نمی‌تواند خالی باشد."
            )
            return

        if name == "/cancel":
            admin_state.pop(user_id, None)
            bot.send_message(
                message.chat.id,
                "افزودن دسته‌بندی لغو شد.",
                reply_markup=shop_admin_menu()
            )
            return

        if name == DELETED_CATEGORY_NAME:
            bot.send_message(
                message.chat.id,
                "⛔ این نام برای دسته‌بندی سیستمی رزرو شده است."
            )
            return

        try:
            with db_lock:
                cursor.execute(
                    "INSERT INTO categories (name) VALUES (?)",
                    (name,)
                )
                db.commit()

            admin_state.pop(user_id, None)
            bot.send_message(
                message.chat.id,
                f"✅ دسته‌بندی «{name}» با موفقیت اضافه شد.",
                reply_markup=shop_admin_menu()
            )

        except sqlite3.IntegrityError:
            bot.send_message(
                message.chat.id,
                "⚠️ این دسته‌بندی از قبل وجود دارد. نام دیگری بفرست."
            )

        return

    if user_id == ADMIN_ID and admin_state.get(user_id) == "broadcast":
        if message.content_type == "text" and message.text == "/cancel":
            admin_state.pop(user_id, None)
            bot.send_message(message.chat.id, "ارسال همگانی لغو شد.")
            return

        admin_state.pop(user_id, None)
        with db_lock:
            cursor.execute("SELECT user_id FROM users")
            users = cursor.fetchall()
        sent = 0
        failed = 0

        for row in users:
            try:
                bot.copy_message(
                    row[0],
                    message.chat.id,
                    message.message_id
                )
                sent += 1
            except Exception:
                failed += 1

        bot.send_message(
            message.chat.id,
            f"✅ ارسال تمام شد.\n"
            f"موفق: {sent}\n"
            f"ناموفق: {failed}",
            reply_markup=admin_menu()
        )
        return

    if message.content_type == "text":
        user_shop_state = shop_state.setdefault(user_id, {})
        if message.text.strip() == "➡️ صفحه بعد دسته‌ها":
            show_categories(message.chat.id, user_shop_state.get("category_page", 0) + 1)
            return
        if message.text.strip() == "⬅️ صفحه قبل دسته‌ها":
            show_categories(message.chat.id, user_shop_state.get("category_page", 0) - 1)
            return
        if message.text.strip() == "🔙 بازگشت به فروشگاه":
            bot.send_message(
                message.chat.id,
                "به منوی فروشگاه برگشتی.",
                reply_markup=shop_menu()
            )
            return

    if message.content_type == "text":
        text_value = message.text.strip()
        state = shop_state.setdefault(user_id, {})

        if text_value == "🧾 ارسال رسید پرداخت":
            show_receipt_orders(message)
            return

        if text_value == "📦 سفارش‌های من":
            show_my_orders(message.chat.id, user_id, page=0)
            return

        if text_value == "➡️ صفحه بعد سفارش‌ها":
            show_my_orders(
                message.chat.id,
                user_id,
                state.get("orders_page", 0) + 1
            )
            return

        if text_value == "⬅️ صفحه قبل سفارش‌ها":
            show_my_orders(
                message.chat.id,
                user_id,
                state.get("orders_page", 0) - 1
            )
            return

        if text_value == "➡️ صفحه بعد محصولات":
            category_id = state.get("category_id")
            if category_id:
                show_products(
                    message.chat.id,
                    category_id,
                    state.get("product_page", 0) + 1
                )
            return

        if text_value == "⬅️ صفحه قبل محصولات":
            category_id = state.get("category_id")
            if category_id:
                show_products(
                    message.chat.id,
                    category_id,
                    state.get("product_page", 0) - 1
                )
            return

        if text_value == "🔙 بازگشت به دسته‌ها":
            show_categories(message.chat.id, state.get("category_page", 0))
            return

        if " [#" in text_value and text_value.startswith("📦 "):
            try:
                product_id = int(text_value.rsplit("[#", 1)[1].rstrip("]"))
            except (ValueError, IndexError):
                bot.send_message(message.chat.id, "شناسه محصول معتبر نیست.")
                return

            send_product_details(message.chat.id, product_id)
            return

        if text_value.startswith("📂 "):
            try:
                category_name = text_value[2:].strip()
                with db_lock:
                    cursor.execute(
                        "SELECT id FROM categories WHERE name = ? AND id != 1",
                        (category_name,)
                    )
                    category_row = cursor.fetchone()

                if category_row is None:
                    bot.send_message(message.chat.id, "این خدمت پیدا نشد.")
                    return

                category_id = category_row[0]
            except (ValueError, IndexError):
                bot.send_message(message.chat.id, "شناسه دسته‌بندی معتبر نیست.")
                return

            show_products(message.chat.id, category_id, page=0)
            return

    if message.content_type == "text" and message.text.strip() == "🔙 بازگشت به منوی اصلی":
        bot.send_message(
            message.chat.id,
            "به منوی اصلی برگشتی.",
            reply_markup=main_menu()
        )
        return

    if message.content_type == "text" and message.text.strip() == "🛠️ خدمات":
        show_categories(message.chat.id, page=0)
        return

    if message.content_type == "text":
        bot.reply_to(
            message,
            "پیامت دریافت شد! ✅\n"
            "برای مشاهده امکانات از منو استفاده کن."
        )
    else:
        bot.reply_to(message, "پیامت دریافت شد! ✅")


def send_product_details(chat_id, product_id):
    with db_lock:
        cursor.execute(
            """
            SELECT name, description, price, photo_file_id
            FROM products
            WHERE id = ? AND active = 1
            """,
            (product_id,)
        )
        product = cursor.fetchone()

    if product is None:
        bot.send_message(
            chat_id,
            "متأسفانه این محصول پیدا نشد یا دیگر در دسترس نیست."
        )
        return

    name, description, price, photo_file_id = product

    caption = (
        f"📦 {name}\n\n"
        f"📝 توضیحات: {description or 'توضیحاتی ثبت نشده است.'}\n"
        f"💰 قیمت: {price:,} تومان"
    )

    markup = telebot.types.InlineKeyboardMarkup()
    markup.row(
        telebot.types.InlineKeyboardButton(
            "➕ افزودن یک عدد به سبد",
            callback_data=f"cart_add_{product_id}",
            style="primary"
        )
    )
    markup.row(
        telebot.types.InlineKeyboardButton(
            "🛒 مشاهده سبد خرید",
            callback_data="cart_show",
            style="primary"
        )
    )

    if photo_file_id:
        bot.send_photo(
            chat_id,
            photo_file_id,
            caption=caption,
            reply_markup=markup
        )
    else:
        bot.send_message(
            chat_id,
            caption,
            reply_markup=markup
        )


print("Bot started successfully!")


@bot.callback_query_handler(
    func=lambda call: call.data.startswith("product_")
)
def product_details(call):
    bot.answer_callback_query(call.id)

    try:
        product_id = int(call.data.split("_", 1)[1])
    except (ValueError, IndexError):
        bot.send_message(call.message.chat.id, "شناسه محصول معتبر نیست.")
        return

    with db_lock:
        cursor.execute(
            """
            SELECT name, description, price, photo_file_id
            FROM products
            WHERE id = ? AND active = 1
            """,
            (product_id,)
        )
        product = cursor.fetchone()

    if product is None:
        bot.send_message(
            call.message.chat.id,
            "متأسفانه این محصول پیدا نشد یا در دسترس نیست."
        )
        return

    name, description, price, photo_file_id = product
    caption = (
        f"📦 {name}\n\n"
        f"📝 توضیحات: {description or 'توضیحاتی ثبت نشده است.'}\n"
        f"💰 قیمت: {price:,} تومان"
    )

    markup = telebot.types.InlineKeyboardMarkup()
    markup.row(
        telebot.types.InlineKeyboardButton(
            "➕ افزودن یک عدد به سبد",
            callback_data=f"cart_add_{product_id}",
            style="primary"
        )
    )
    markup.row(
        telebot.types.InlineKeyboardButton(
            "🛒 مشاهده سبد خرید",
            callback_data="cart_show",
            style="primary"
        )
    )

    if photo_file_id:
        bot.send_photo(
            call.message.chat.id,
            photo_file_id,
            caption=caption,
            reply_markup=markup
        )
    else:
        bot.send_message(
            call.message.chat.id,
            caption,
            reply_markup=markup
        )


@bot.callback_query_handler(
    func=lambda call: call.data.startswith("cart_add_")
)
def cart_add_product(call):
    try:
        product_id = int(call.data.split("_")[-1])
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "شناسه محصول نامعتبر است.")
        return

    user_id = call.from_user.id

    with db_lock:
        cursor.execute(
            "SELECT id, price FROM products WHERE id = ? AND active = 1",
            (product_id,)
        )
        product = cursor.fetchone()

        if product is None:
            bot.answer_callback_query(
                call.id, "این محصول دیگر در دسترس نیست."
            )
            return

        cursor.execute(
            """
            INSERT INTO cart_items (user_id, product_id, quantity)
            VALUES (?, ?, 1)
            ON CONFLICT(user_id, product_id)
            DO UPDATE SET quantity = quantity + 1
            """,
            (user_id, product_id)
        )
        db.commit()

    bot.answer_callback_query(call.id, "یک عدد به سبد خرید اضافه شد.")


@bot.callback_query_handler(
    func=lambda call: call.data == "cart_show"
)
def cart_show(call):
    if call.data == "cart_show":
        bot.answer_callback_query(call.id)

    user_id = call.from_user.id
    chat_id = call.message.chat.id

    # شناسه پیام‌های قبلی سبد خرید را نگه می‌داریم
    if "cart_display_messages" not in globals():
        globals()["cart_display_messages"] = {}

    display_messages = globals()["cart_display_messages"]

    # حذف پیام‌های قبلی سبد خرید برای جلوگیری از شلوغی
    for message_id in display_messages.get(user_id, []):
        try:
            bot.delete_message(chat_id, message_id)
        except Exception:
            pass

    display_messages[user_id] = []

    with db_lock:
        cursor.execute(
            """
            SELECT p.name, p.price, c.quantity, p.id
            FROM cart_items AS c
            JOIN products AS p ON p.id = c.product_id
            WHERE c.user_id = ? AND p.active = 1
            ORDER BY p.id
            """,
            (user_id,)
        )
        items = cursor.fetchall()

    if not items:
        msg = bot.send_message(
            chat_id,
            "🛒 سبد خریدت خالی است."
        )
        display_messages[user_id].append(msg.message_id)
        return

    total = 0

    for i, (name, price, quantity, product_id) in enumerate(items, 1):
        subtotal = price * quantity
        total += subtotal

        text = (
            f"📦 محصول {i}: {name}\n"
            f"━━━━━━━━━━━━━━\n"
            f"🔢 تعداد: {quantity}\n"
            f"💵 قیمت واحد: {price:,} تومان\n"
            f"💰 جمع این محصول: {subtotal:,} تومان"
        )

        markup = telebot.types.InlineKeyboardMarkup()

        markup.row(
            telebot.types.InlineKeyboardButton(
                "➖",
                callback_data=f"cart_minus_{product_id}",
                style="danger"
            ),
            telebot.types.InlineKeyboardButton(
                f"تعداد: {quantity}",
                callback_data="cart_show",
                style="primary"
            ),
            telebot.types.InlineKeyboardButton(
                "➕",
                callback_data=f"cart_plus_{product_id}",
                style="success"
            )
        )

        markup.row(
            telebot.types.InlineKeyboardButton(
                "🗑️ حذف این محصول",
                callback_data=f"cart_remove_{product_id}",
                style="danger"
            )
        )

        msg = bot.send_message(
            chat_id,
            text,
            reply_markup=markup
        )
        display_messages[user_id].append(msg.message_id)

    checkout_markup = telebot.types.InlineKeyboardMarkup()
    checkout_markup.row(
        telebot.types.InlineKeyboardButton(
            "✅ ثبت سفارش",
            callback_data="cart_checkout",
            style="success"
        )
    )

    summary_lines = [
        "🛒 خلاصه سبد خرید",
        "━━━━━━━━━━━━━━",
        f"📦 تعداد محصولات: {len(items)}",
        "",
        "🧾 اقلام سفارش:"
    ]

    for i, (name, price, quantity, product_id) in enumerate(items, 1):
        subtotal = price * quantity
        summary_lines.extend([
            f"{i}. {name}",
            f"   🔢 تعداد: {quantity}",
            f"   💵 قیمت واحد: {price:,} تومان",
            f"   💰 جمع این محصول: {subtotal:,} تومان",
            ""
        ])

    summary_lines.extend([
        "━━━━━━━━━━━━━━",
        f"💰 مبلغ نهایی: {total:,} تومان",
        "",
        "برای نهایی‌کردن خرید، دکمه زیر را بزن:"
    ])

    summary = bot.send_message(
        chat_id,
        "\n".join(summary_lines),
        reply_markup=checkout_markup
    )
    display_messages[user_id].append(summary.message_id)



def complete_checkout(user_id, chat_id, call=None, customer=None):
    try:
        with db_lock:
            cursor.execute(
                "SELECT phone FROM users WHERE user_id = ?",
                (user_id,)
            )
            row = cursor.fetchone()
            if not row or not row[0]:
                pending_phone.add(user_id)
                if call:
                    bot.answer_callback_query(call.id)
                keyboard = telebot.types.ReplyKeyboardMarkup(
                    resize_keyboard=True,
                    one_time_keyboard=True
                )
                keyboard.add(
                    telebot.types.KeyboardButton(
                        "📱 ارسال شماره موبایل",
                        request_contact=True
                    )
                )
                bot.send_message(
                    chat_id,
                    "برای ادامه ثبت سفارش، شماره موبایل خودت را با دکمه زیر ارسال کن.",
                    reply_markup=keyboard
                )
                return

            cursor.execute(
                "SELECT p.id, p.name, p.price, c.quantity "
                "FROM cart_items AS c "
                "JOIN products AS p ON p.id = c.product_id "
                "WHERE c.user_id = ? AND p.active = 1 "
                "ORDER BY p.id",
                (user_id,)
            )
            items = cursor.fetchall()

            if not items:
                if call:
                    bot.answer_callback_query(
                        call.id,
                        "سبد خرید خالی است یا محصولات آن دیگر در دسترس نیستند."
                    )
                else:
                    bot.send_message(
                        chat_id,
                        "سبد خرید خالی است یا محصولات آن دیگر در دسترس نیستند."
                    )
                return

            total = sum(price * quantity for _, _, price, quantity in items)

            tracking_code = secrets.token_hex(4).upper()
            for _ in range(20):
                cursor.execute(
                    "SELECT 1 FROM orders WHERE tracking_code = ?",
                    (tracking_code,)
                )
                if not cursor.fetchone():
                    break
                tracking_code = secrets.token_hex(4).upper()
            else:
                raise RuntimeError("ساخت کد پیگیری یکتا ناموفق بود.")

            cursor.execute(
                "INSERT INTO orders "
                "(user_id, total_amount, status, tracking_code) "
                "VALUES (?, ?, 'pending', ?)",
                (user_id, total, tracking_code)
            )
            order_id = cursor.lastrowid

            for product_id, name, price, quantity in items:
                cursor.execute(
                    "INSERT INTO order_items "
                    "(order_id, product_id, product_name, unit_price, quantity) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (order_id, product_id, name, price, quantity)
                )

            cursor.execute(
                "DELETE FROM cart_items WHERE user_id = ?",
                (user_id,)
            )
            db.commit()

    except Exception as exc:
        with db_lock:
            db.rollback()
        print(f"Checkout failed for user {user_id}: {exc}")
        if call:
            bot.answer_callback_query(
                call.id,
                "ثبت سفارش ناموفق بود؛ دوباره تلاش کن."
            )
        else:
            bot.send_message(
                chat_id,
                "ثبت سفارش ناموفق بود؛ لطفاً دوباره تلاش کن."
            )
        return

    pending_phone.discard(user_id)
    if call:
        bot.answer_callback_query(call.id, "سفارش ثبت شد.")

    display_messages = globals().get("cart_display_messages", {})
    old_message_ids = display_messages.pop(user_id, [])
    for message_id in old_message_ids:
        try:
            bot.delete_message(chat_id, message_id)
        except Exception:
            pass

    with db_lock:
        cursor.execute(
            "SELECT phone FROM users WHERE user_id = ?",
            (user_id,)
        )
        phone_row = cursor.fetchone()

    customer_phone = (
        phone_row[0] if phone_row and phone_row[0] else "ثبت نشده"
    )

    customer_name = " ".join(
        part for part in (
            getattr(customer, "first_name", None),
            getattr(customer, "last_name", None)
        ) if part
    ) or "ثبت نشده"

    customer_username = getattr(customer, "username", None)
    customer_username = f"@{customer_username}" if customer_username else "ندارد"

    admin_lines = [
        f"🛍 سفارش جدید شماره {order_id}",
        "━━━━━━━━━━━━━━",
        f"👤 نام مشتری: {customer_name}",
        f"🆔 آیدی تلگرام: {user_id}",
        f"🔗 نام کاربری: {customer_username}",
        f"📱 شماره موبایل: {customer_phone}",
        f"🔑 کد پیگیری: {tracking_code}",
        f"💰 مبلغ کل: {total:,} تومان",
        "📦 اقلام سفارش:"
    ]
    for _, name, price, quantity in items:
        admin_lines.append(
            f"• {name} | تعداد: {quantity} | قیمت واحد: {price:,} تومان"
        )
    admin_lines.append("📌 وضعیت: در انتظار بررسی")
    admin_lines.append("⚠️ کد پیگیری به معنی تأیید پرداخت نیست.")

    try:
        admin_keyboard = telebot.types.InlineKeyboardMarkup(row_width=2)

        admin_keyboard.add(
            telebot.types.InlineKeyboardButton(
                "✅ تأیید اولیه سفارش",
                callback_data=f"order_initial_approve_{order_id}",
                style="success"
            ),
            telebot.types.InlineKeyboardButton(
                "❌ رد سفارش",
                callback_data=f"order_initial_reject_{order_id}",
                style="danger"
            )
        )
        
        bot.send_message(
            ADMIN_ID,
            "\n".join(admin_lines),
            reply_markup=admin_keyboard
        )
    except Exception as exc:
        print(f"Admin notification failed for order {order_id}: {exc}")

    customer_lines = [
        "🎉 سفارش شما با موفقیت ثبت شد!",
        "━━━━━━━━━━━━━━",
        f"🧾 شماره سفارش: {order_id}",
        f"🔑 کد پیگیری: {tracking_code}",
        "",
        "📦 اقلام سفارش:"
    ]

    for _, name, price, quantity in items:
        customer_lines.append(
            f"• {name} | تعداد: {quantity} | قیمت واحد: {price:,} تومان"
        )

    customer_lines.extend([
        "━━━━━━━━━━━━━━",
        f"💰 مبلغ کل: {total:,} تومان",
        "📌 وضعیت: در انتظار بررسی",
        "",
        "این کد برای پیگیری سفارش است و به معنی تأیید پرداخت نیست."
    ])

    bot.send_message(
        chat_id,
        "\n".join(customer_lines),
        reply_markup=telebot.types.ReplyKeyboardRemove()
    )

@bot.callback_query_handler(
    func=lambda call: call.data == "cart_checkout"
)
def checkout_cart(call):
    complete_checkout(call.from_user.id, call.message.chat.id, call, customer=call.from_user)

@bot.callback_query_handler(
    func=lambda call: call.data.startswith("cart_plus_")
)
def cart_increase_quantity(call):
    try:
        product_id = int(call.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "شناسه محصول نامعتبر است.")
        return

    with db_lock:
        cursor.execute(
            """
            UPDATE cart_items
            SET quantity = quantity + 1
            WHERE user_id = ? AND product_id = ?
            AND EXISTS (
                SELECT 1 FROM products
                WHERE id = ? AND active = 1
            )
            """,
            (call.from_user.id, product_id, product_id)
        )
        changed = cursor.rowcount
        db.commit()

    if not changed:
        bot.answer_callback_query(call.id, "محصول در سبد پیدا نشد.")
        return

    bot.answer_callback_query(call.id, "تعداد افزایش یافت.")
    cart_show(call)


@bot.callback_query_handler(
    func=lambda call: call.data.startswith("cart_minus_")
)
def cart_decrease_quantity(call):
    try:
        product_id = int(call.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "شناسه محصول نامعتبر است.")
        return

    with db_lock:
        cursor.execute(
            """
            SELECT quantity FROM cart_items
            WHERE user_id = ? AND product_id = ?
            """,
            (call.from_user.id, product_id)
        )
        item = cursor.fetchone()

        if item is None:
            bot.answer_callback_query(call.id, "محصول در سبد پیدا نشد.")
            return

        if item[0] <= 1:
            cursor.execute(
                """
                DELETE FROM cart_items
                WHERE user_id = ? AND product_id = ?
                """,
                (call.from_user.id, product_id)
            )
        else:
            cursor.execute(
                """
                UPDATE cart_items SET quantity = quantity - 1
                WHERE user_id = ? AND product_id = ?
                """,
                (call.from_user.id, product_id)
            )
        db.commit()

    bot.answer_callback_query(call.id, "تعداد کاهش یافت.")
    cart_show(call)


@bot.callback_query_handler(
    func=lambda call: call.data.startswith("cart_remove_")
)
def cart_remove_product(call):
    try:
        product_id = int(call.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        bot.answer_callback_query(call.id, "شناسه محصول نامعتبر است.")
        return

    with db_lock:
        cursor.execute(
            """
            DELETE FROM cart_items
            WHERE user_id = ? AND product_id = ?
            """,
            (call.from_user.id, product_id)
        )
        changed = cursor.rowcount
        db.commit()

    if not changed:
        bot.answer_callback_query(call.id, "این محصول قبلاً حذف شده است.")
        return

    bot.answer_callback_query(call.id, "محصول از سبد حذف شد.")
    cart_show(call)


@bot.callback_query_handler(
    func=lambda call: call.data.startswith("category_")
)
def show_category_products(call):
    bot.answer_callback_query(call.id)

    try:
        category_id = int(call.data.split("_", 1)[1])
    except (ValueError, IndexError):
        bot.send_message(
            call.message.chat.id,
            "شناسهٔ دسته‌بندی معتبر نیست."
        )
        return

    if category_id == 1:
        bot.send_message(
            call.message.chat.id,
            "⛔ این دسته‌بندی برای خرید در دسترس نیست."
        )
        return

    with db_lock:
        cursor.execute(
            "SELECT name FROM categories WHERE id = ?",
            (category_id,)
        )
        category = cursor.fetchone()

        cursor.execute(
            """
            SELECT id, name, price
            FROM products
            WHERE category_id = ? AND active = 1
            ORDER BY id
            """,
            (category_id,)
        )
        products = cursor.fetchall()

    if category is None:
        bot.send_message(
            call.message.chat.id,
            "این دسته‌بندی پیدا نشد."
        )
        return

    if not products:
        bot.send_message(
            call.message.chat.id,
            f"📂 دسته‌بندی «{category[0]}» هنوز محصولی ندارد."
        )
        return

    markup = telebot.types.InlineKeyboardMarkup()

    for product_id, name, price in products:
        markup.add(
            telebot.types.InlineKeyboardButton(
                f"📦 {name} | {price:,} تومان",
                callback_data=f"product_{product_id}"
            )
        )

    bot.send_message(
        call.message.chat.id,
        f"📂 دسته‌بندی: {category[0]}\n\n"
        "محصول موردنظرت رو انتخاب کن:",
        reply_markup=markup
    )


@bot.callback_query_handler(
    func=lambda call: call.data.startswith("rename_category_")
)
def rename_category_selected(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(
            call.id, "⛔ شما اجازه دسترسی ندارید.", show_alert=True
        )
        return

    try:
        category_id = int(call.data.split("rename_category_", 1)[1])
    except (ValueError, IndexError):
        bot.answer_callback_query(
            call.id, "شناسه دسته‌بندی نامعتبر است.", show_alert=True
        )
        return

    if category_id == 1:
        bot.answer_callback_query(
            call.id, "دسته‌بندی پیش‌فرض قابل تغییر نام نیست.",
            show_alert=True
        )
        return

    with db_lock:
        cursor.execute(
            "SELECT name FROM categories WHERE id = ?",
            (category_id,)
        )
        category = cursor.fetchone()

    if category is None or category[0] == DELETED_CATEGORY_NAME:
        bot.answer_callback_query(
            call.id, "این دسته‌بندی قابل تغییر نام نیست.",
            show_alert=True
        )
        return

    bot.answer_callback_query(call.id)
    admin_state[call.from_user.id] = {
        "action": "rename_category",
        "category_id": category_id
    }
    bot.send_message(
        call.message.chat.id,
        f"نام فعلی دسته‌بندی: {category[0]}\n"
        "نام جدید را ارسال کن.\nبرای لغو، /cancel را بفرست."
    )




@bot.callback_query_handler(
    func=lambda call: call.data.startswith("delete_category_")
)
def delete_category_selected(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(
            call.id, "⛔ شما اجازه دسترسی ندارید.", show_alert=True
        )
        return

    try:
        category_id = int(call.data.split("delete_category_", 1)[1])
    except (ValueError, IndexError):
        bot.answer_callback_query(
            call.id, "شناسه دسته‌بندی نامعتبر است.", show_alert=True
        )
        return

    if category_id == 1:
        bot.answer_callback_query(
            call.id, "دسته‌بندی پیش‌فرض قابل حذف نیست.",
            show_alert=True
        )
        return

    with db_lock:
        cursor.execute(
            "SELECT name FROM categories WHERE id = ?",
            (category_id,)
        )
        category = cursor.fetchone()

    if category is None or category[0] == DELETED_CATEGORY_NAME:
        bot.answer_callback_query(
            call.id, "این دسته‌بندی قابل حذف نیست.",
            show_alert=True
        )
        return

    markup = telebot.types.InlineKeyboardMarkup()
    markup.row(
        telebot.types.InlineKeyboardButton(
            "بله، حذف شود",
            callback_data=f"confirm_delete_category_{category_id}"
        ),
        telebot.types.InlineKeyboardButton(
            "انصراف",
            callback_data=f"cancel_delete_category_{category_id}"
        )
    )
    bot.answer_callback_query(call.id)
    bot.send_message(
        call.message.chat.id,
        f"⚠️ آیا از حذف دسته‌بندی «{category[0]}» مطمئنی؟\n"
        "محصولات فعال این دسته به دستهٔ پیش‌فرض منتقل می‌شوند.",
        reply_markup=markup
    )



@bot.callback_query_handler(
    func=lambda call: call.data.startswith("cancel_delete_category_")
)
def cancel_delete_category(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(
            call.id,
            "⛔ شما اجازه دسترسی ندارید.",
            show_alert=True
        )
        return

    bot.answer_callback_query(call.id, "عملیات لغو شد.")

    bot.edit_message_text(
        "❌ حذف دسته‌بندی لغو شد.",
        chat_id=call.message.chat.id,
        message_id=call.message.message_id
    )


@bot.callback_query_handler(
    func=lambda call: call.data.startswith("confirm_delete_category_")
)
def confirm_delete_category(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(
            call.id, "⛔ شما اجازه دسترسی ندارید.", show_alert=True
        )
        return

    try:
        category_id = int(
            call.data.split("confirm_delete_category_", 1)[1]
        )
    except (ValueError, IndexError):
        bot.answer_callback_query(
            call.id, "شناسه دسته‌بندی نامعتبر است.", show_alert=True
        )
        return

    if category_id == 1:
        bot.answer_callback_query(
            call.id, "دسته‌بندی پیش‌فرض قابل حذف نیست.",
            show_alert=True
        )
        return

    error_message = None
    deleted_name = None

    try:
        with db_lock:
            cursor.execute(
                "SELECT name FROM categories WHERE id = ?",
                (category_id,)
            )
            category = cursor.fetchone()

            if category is None:
                error_message = "این دسته‌بندی دیگر وجود ندارد."
            elif category[0] == DELETED_CATEGORY_NAME:
                error_message = "دسته‌بندی سیستمی قابل حذف نیست."
            else:
                cursor.execute(
                    "SELECT id FROM categories WHERE id = 1"
                )
                default_category = cursor.fetchone()

                if default_category is None:
                    error_message = "دسته‌بندی پیش‌فرض پیدا نشد."
                else:
                    cursor.execute(
                        """
                        UPDATE products
                        SET category_id = 1
                        WHERE category_id = ?
                        """,
                        (category_id,)
                    )
                    cursor.execute(
                        "DELETE FROM categories WHERE id = ?",
                        (category_id,)
                    )
                    deleted_name = category[0]
                    db.commit()

    except sqlite3.Error:
        with db_lock:
            db.rollback()
        error_message = "خطای پایگاه داده؛ حذف انجام نشد."

    if error_message:
        bot.answer_callback_query(
            call.id, error_message, show_alert=True
        )
        return

    bot.answer_callback_query(call.id, "دسته‌بندی حذف شد.")
    try:
        bot.edit_message_text(
            f"✅ دسته‌بندی «{deleted_name}» حذف شد.",
            call.message.chat.id,
            call.message.message_id
        )
    except Exception:
        bot.send_message(
            call.message.chat.id,
            f"✅ دسته‌بندی «{deleted_name}» حذف شد."
        )


@bot.callback_query_handler(
    func=lambda call: call.data.startswith("manage_category_")
)
def manage_category_products(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(
            call.id, "⛔ شما اجازه دسترسی ندارید.", show_alert=True
        )
        return

    try:
        category_id = int(call.data.split("manage_category_", 1)[1])
    except (ValueError, IndexError):
        bot.answer_callback_query(
            call.id, "شناسه دسته‌بندی نامعتبر است.", show_alert=True
        )
        return

    with db_lock:
        cursor.execute(
            "SELECT name FROM categories WHERE id = ?",
            (category_id,)
        )
        category = cursor.fetchone()

        if category is not None:
            is_deleted_category = category[0] == DELETED_CATEGORY_NAME
            if is_deleted_category:
                cursor.execute(
                    """
                    SELECT id, name, price
                    FROM products
                    WHERE category_id = ? AND active = 0
                    ORDER BY id
                    """,
                    (category_id,)
                )
            else:
                cursor.execute(
                    """
                    SELECT id, name, price
                    FROM products
                    WHERE category_id = ? AND active = 1
                    ORDER BY id
                    """,
                    (category_id,)
                )
            products = cursor.fetchall()
        else:
            is_deleted_category = False
            products = []

    bot.answer_callback_query(call.id)

    if category is None:
        bot.send_message(
            call.message.chat.id,
            "⚠️ این دسته‌بندی دیگر وجود ندارد."
        )
        return

    markup = telebot.types.InlineKeyboardMarkup()

    for product_id, name, price in products:
        markup.add(
            telebot.types.InlineKeyboardButton(
                f"📦 {name} — {price:,} تومان",
                callback_data=f"admin_product_{product_id}"
            )
        )

    if is_deleted_category:
        message_text = (
            f"📂 مدیریت دسته‌بندی: {category[0]}\n\n"
            f"تعداد محصولات حذف‌شده: {len(products)}\n"
            "برای مشاهده جزئیات یا بازیابی، محصول را انتخاب کن."
        )
    else:
        markup.add(
            telebot.types.InlineKeyboardButton(
                "➕ افزودن محصول به این دسته",
                callback_data=f"add_product_{category_id}"
            )
        )
        message_text = (
            f"📂 مدیریت دسته‌بندی: {category[0]}\n\n"
            f"تعداد محصولات: {len(products)}\n"
            "برای افزودن محصول، دکمهٔ زیر را انتخاب کن."
        )

    bot.send_message(
        call.message.chat.id,
        message_text,
        reply_markup=markup
    )


@bot.callback_query_handler(
    func=lambda call: call.data.startswith("admin_product_")
)
def admin_product_details(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(
            call.id, "⛔ شما اجازه دسترسی ندارید.", show_alert=True
        )
        return

    try:
        product_id = int(call.data.split("admin_product_", 1)[1])
    except (ValueError, IndexError):
        bot.answer_callback_query(
            call.id, "شناسه محصول نامعتبر است.", show_alert=True
        )
        return

    with db_lock:
        cursor.execute(
            """
            SELECT name, description, price, active, category_id
            FROM products
            WHERE id = ?
            """,
            (product_id,)
        )
        product = cursor.fetchone()

        cursor.execute(
            "SELECT id FROM categories WHERE name = ?",
            (DELETED_CATEGORY_NAME,)
        )
        special = cursor.fetchone()

    bot.answer_callback_query(call.id)

    if product is None:
        bot.send_message(
            call.message.chat.id,
            "⚠️ این محصول دیگر وجود ندارد."
        )
        return

    name, description, price, active, category_id = product
    special_id = special[0] if special else None
    is_deleted = (
        active == 0
        and special_id is not None
        and category_id == special_id
    )

    markup = telebot.types.InlineKeyboardMarkup()
    markup.row(
        telebot.types.InlineKeyboardButton(
            "✏️ تغییر نام",
            callback_data=f"edit_name_{product_id}"
        ),
        telebot.types.InlineKeyboardButton(
            "💰 تغییر قیمت",
            callback_data=f"edit_price_{product_id}"
        )
    )
    markup.row(
        telebot.types.InlineKeyboardButton(
            "📝 تغییر توضیحات",
            callback_data=f"edit_description_{product_id}"
        ),
        telebot.types.InlineKeyboardButton(
            "🖼 تغییر عکس",
            callback_data=f"edit_photo_{product_id}"
        )
    )

    if is_deleted:
        markup.add(
            telebot.types.InlineKeyboardButton(
                "♻️ بازیابی محصول",
                callback_data=f"restore_product_{product_id}"
            )
        )
    elif active == 1:
        markup.add(
            telebot.types.InlineKeyboardButton(
                "🗑 حذف محصول",
                callback_data=f"delete_product_{product_id}"
            )
        )

    status = "حذف‌شده" if is_deleted else (
        "فعال" if active == 1 else "غیرفعال"
    )
    text = (
        f"📦 نام محصول: {name}\n"
        f"💰 قیمت: {price:,} تومان\n"
        f"📌 وضعیت: {status}\n\n"
        f"📝 توضیحات:\n{description or 'بدون توضیحات'}"
    )

    bot.send_message(
        call.message.chat.id,
        text,
        reply_markup=markup
    )



@bot.callback_query_handler(
    func=lambda call: call.data.startswith("delete_product_")
)
def confirm_delete_product(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(
            call.id,
            "⛔ شما اجازه دسترسی ندارید.",
            show_alert=True
        )
        return

    bot.answer_callback_query(call.id)

    try:
        product_id = int(
            call.data.split("delete_product_", 1)[1]
        )
    except (ValueError, IndexError):
        bot.send_message(
            call.message.chat.id,
            "شناسهٔ محصول معتبر نیست."
        )
        return

    with db_lock:
        cursor.execute(
            "SELECT name FROM products WHERE id = ? AND active = 1",
            (product_id,)
        )
        product = cursor.fetchone()

    if product is None:
        bot.send_message(
            call.message.chat.id,
            "این محصول پیدا نشد یا قبلاً غیرفعال شده است."
        )
        return

    markup = telebot.types.InlineKeyboardMarkup()

    markup.row(
        telebot.types.InlineKeyboardButton(
            "✅ بله، حذف شود",
            callback_data=f"confirm_delete_{product_id}",
            style="danger"
        ),
        telebot.types.InlineKeyboardButton(
            "❌ لغو",
            callback_data=f"cancel_delete_{product_id}",
            style="primary"
        )
    )

    bot.send_message(
        call.message.chat.id,
        f"⚠️ آیا از حذف محصول «{product[0]}» مطمئن هستی؟\n\n"
        "این محصول از فروشگاه پنهان می‌شود، "
        "اما اطلاعات آن در پایگاه داده باقی می‌ماند.",
        reply_markup=markup
    )


@bot.callback_query_handler(
    func=lambda call: (
        call.data.startswith("confirm_delete_")
        and not call.data.startswith("confirm_delete_category_")
    )
)
def execute_delete_product(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(
            call.id,
            "⛔ شما اجازه دسترسی ندارید.",
            show_alert=True
        )
        return

    try:
        product_id = int(
            call.data.split("confirm_delete_", 1)[1]
        )
    except (ValueError, IndexError):
        bot.answer_callback_query(
            call.id,
            "شناسهٔ محصول معتبر نیست.",
            show_alert=True
        )
        return

    with db_lock:
        cursor.execute(
            "SELECT id FROM categories WHERE name = ?",
            (DELETED_CATEGORY_NAME,)
        )
        deleted_category = cursor.fetchone()

        deleted = False

        if deleted_category is not None:
            deleted_category_id = deleted_category[0]

            cursor.execute(
                """
                UPDATE products
                SET deleted_from_category_id = category_id,
                    category_id = ?,
                    active = 0
                WHERE id = ?
                  AND active = 1
                  AND category_id != ?
                """,
                (deleted_category_id, product_id, deleted_category_id)
            )

            deleted = cursor.rowcount > 0
            db.commit()

    if deleted:
        bot.answer_callback_query(call.id, "محصول غیرفعال شد.")
        bot.edit_message_text(
            "✅ محصول با موفقیت از فروشگاه حذف شد.",
            call.message.chat.id,
            call.message.message_id
        )
    else:
        bot.answer_callback_query(
            call.id,
            "محصول پیدا نشد یا قبلاً حذف شده است.",
            show_alert=True
        )


@bot.callback_query_handler(
    func=lambda call: call.data.startswith("cancel_delete_")
)
def cancel_delete_product(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(
            call.id,
            "⛔ شما اجازه دسترسی ندارید.",
            show_alert=True
        )
        return

    bot.answer_callback_query(call.id, "حذف لغو شد.")
    bot.edit_message_text(
        "❎ حذف محصول لغو شد.",
        call.message.chat.id,
        call.message.message_id
    )


@bot.message_handler(
    func=lambda m: m.text == "♻️ بازیابی محصولات حذف‌شده"
)
def restore_products_start(message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "⛔ شما اجازه دسترسی ندارید.")
        return

    with db_lock:
        cursor.execute(
            "SELECT id FROM categories WHERE name = ?",
            (DELETED_CATEGORY_NAME,)
        )
        special = cursor.fetchone()

        if special:
            cursor.execute(
                """
                SELECT id, name, price
                FROM products
                WHERE active = 0 AND category_id = ?
                ORDER BY id
                """,
                (special[0],)
            )
            products = cursor.fetchall()
        else:
            products = []

    if not products:
        bot.send_message(
            message.chat.id,
            "در حال حاضر محصول حذف‌شده‌ای برای بازیابی وجود ندارد."
        )
        return

    markup = telebot.types.InlineKeyboardMarkup()
    for product_id, name, price in products:
        markup.add(
            telebot.types.InlineKeyboardButton(
                f"♻️ {name} — {price:,} تومان",
                callback_data=f"restore_product_{product_id}"
            )
        )

    bot.send_message(
        message.chat.id,
        "♻️ محصول موردنظر را برای بازیابی انتخاب کن:",
        reply_markup=markup
    )



@bot.callback_query_handler(
    func=lambda call: call.data.startswith("restore_product_")
)
def restore_product_selected(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(
            call.id, "⛔ شما اجازه دسترسی ندارید.", show_alert=True
        )
        return

    try:
        product_id = int(call.data.split("restore_product_", 1)[1])
    except (ValueError, IndexError):
        bot.answer_callback_query(
            call.id, "شناسه محصول نامعتبر است.", show_alert=True
        )
        return

    with db_lock:
        cursor.execute(
            "SELECT id FROM categories WHERE name = ?",
            (DELETED_CATEGORY_NAME,)
        )
        special = cursor.fetchone()

        product = None
        categories = []
        if special:
            cursor.execute(
                """
                SELECT name, price, deleted_from_category_id
                FROM products
                WHERE id = ? AND active = 0 AND category_id = ?
                """,
                (product_id, special[0])
            )
            product = cursor.fetchone()

            if product:
                cursor.execute(
                    """
                    SELECT id, name
                    FROM categories
                    WHERE name != ?
                    ORDER BY id
                    """,
                    (DELETED_CATEGORY_NAME,)
                )
                categories = cursor.fetchall()

    if not product:
        bot.answer_callback_query(
            call.id,
            "این محصول در فهرست محصولات حذف‌شده وجود ندارد.",
            show_alert=True
        )
        return

    name, price, old_category_id = product
    category_map = {cid: cname for cid, cname in categories}

    if not categories:
        bot.answer_callback_query(
            call.id, "هیچ دسته‌بندی مقصدی وجود ندارد.",
            show_alert=True
        )
        return

    # دسته قبلی، اگر هنوز وجود داشته باشد، در ابتدای فهرست قرار می‌گیرد.
    ordered = []
    if old_category_id in category_map:
        ordered.append((old_category_id, category_map[old_category_id]))
    ordered.extend(
        (cid, cname)
        for cid, cname in categories
        if cid != old_category_id
    )

    markup = telebot.types.InlineKeyboardMarkup()
    for category_id, category_name in ordered:
        label = f"📂 {category_name}"
        if category_id == old_category_id:
            label += " (دسته قبلی)"
        markup.add(
            telebot.types.InlineKeyboardButton(
                label,
                callback_data=f"restore_to_{product_id}_{category_id}"
            )
        )

    markup.add(
        telebot.types.InlineKeyboardButton(
            "لغو",
            callback_data=f"cancel_restore_product_{product_id}"
        )
    )

    bot.answer_callback_query(call.id)
    bot.send_message(
        call.message.chat.id,
        f"♻️ بازیابی محصول: {name}\n"
        f"💰 قیمت: {price:,} تومان\n\n"
        "محصول در کدام دسته‌بندی قرار بگیرد؟",
        reply_markup=markup
    )



@bot.callback_query_handler(
    func=lambda call: call.data.startswith("cancel_restore_product_")
)
def cancel_restore_product(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(
            call.id, "⛔ شما اجازه دسترسی ندارید.", show_alert=True
        )
        return

    bot.answer_callback_query(call.id, "بازیابی لغو شد.")
    try:
        bot.edit_message_text(
            "بازیابی محصول لغو شد.",
            call.message.chat.id,
            call.message.message_id
        )
    except Exception:
        pass



@bot.callback_query_handler(
    func=lambda call: call.data.startswith("confirm_restore_product_")
)
def confirm_restore_product(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(
            call.id, "⛔ شما اجازه دسترسی ندارید.", show_alert=True
        )
        return

    bot.answer_callback_query(
        call.id,
        "این روش قدیمی بازیابی دیگر فعال نیست؛ محصول را از فهرست بازیابی انتخاب کن.",
        show_alert=True
    )


@bot.callback_query_handler(
    func=lambda call: call.data.startswith("edit_name_")
)
def edit_product_name(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(
            call.id,
            "⛔ شما اجازه دسترسی ندارید.",
            show_alert=True
        )
        return

    bot.answer_callback_query(call.id)

    try:
        product_id = int(
            call.data.split("edit_name_", 1)[1]
        )
    except (ValueError, IndexError):
        bot.send_message(
            call.message.chat.id,
            "شناسهٔ محصول معتبر نیست."
        )
        return

    with db_lock:
        cursor.execute(
            "SELECT name FROM products WHERE id = ?",
            (product_id,)
        )
        product = cursor.fetchone()

    if product is None:
        bot.send_message(
            call.message.chat.id,
            "این محصول پیدا نشد."
        )
        return

    admin_state[call.from_user.id] = {
        "action": "edit_product_name",
        "product_id": product_id
    }

    bot.send_message(
        call.message.chat.id,
        f"نام فعلی محصول: {product[0]}\n\n"
        "نام جدید را ارسال کن.\n"
        "برای لغو عملیات، /cancel را بفرست."
    )


@bot.callback_query_handler(
    func=lambda call: call.data.startswith("edit_price_")
)
def edit_product_price(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(
            call.id,
            "⛔ شما اجازه دسترسی ندارید.",
            show_alert=True
        )
        return

    bot.answer_callback_query(call.id)

    try:
        product_id = int(
            call.data.split("edit_price_", 1)[1]
        )
    except (ValueError, IndexError):
        bot.send_message(
            call.message.chat.id,
            "شناسهٔ محصول معتبر نیست."
        )
        return

    with db_lock:
        cursor.execute(
            "SELECT name, price FROM products WHERE id = ?",
            (product_id,)
        )
        product = cursor.fetchone()

    if product is None:
        bot.send_message(
            call.message.chat.id,
            "این محصول پیدا نشد."
        )
        return

    admin_state[call.from_user.id] = {
        "action": "edit_product_price",
        "product_id": product_id
    }

    bot.send_message(
        call.message.chat.id,
        f"📦 محصول: {product[0]}\n"
        f"💰 قیمت فعلی: {product[1]:,} تومان\n\n"
        "قیمت جدید را فقط به‌صورت عدد بفرست.\n"
        "مثال: 350000\n\n"
        "برای لغو عملیات، /cancel را ارسال کن."
    )


@bot.callback_query_handler(
    func=lambda call: call.data.startswith("edit_description_")
)
def edit_product_description(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(
            call.id,
            "⛔ شما اجازه دسترسی ندارید.",
            show_alert=True
        )
        return

    bot.answer_callback_query(call.id)

    try:
        product_id = int(
            call.data.split("edit_description_", 1)[1]
        )
    except (ValueError, IndexError):
        bot.send_message(
            call.message.chat.id,
            "شناسهٔ محصول معتبر نیست."
        )
        return

    with db_lock:
        cursor.execute(
            "SELECT name, description FROM products WHERE id = ?",
            (product_id,)
        )
        product = cursor.fetchone()

    if product is None:
        bot.send_message(
            call.message.chat.id,
            "این محصول پیدا نشد."
        )
        return

    admin_state[call.from_user.id] = {
        "action": "edit_product_description",
        "product_id": product_id
    }

    bot.send_message(
        call.message.chat.id,
        f"📦 محصول: {product[0]}\n"
        f"📝 توضیحات فعلی: {product[1] or 'ثبت نشده'}\n\n"
        "توضیحات جدید را ارسال کن.\n"
        "برای پاک کردن توضیحات، یک خط تیره (-) بفرست.\n"
        "برای لغو عملیات، /cancel را ارسال کن."
    )

@bot.callback_query_handler(
    func=lambda call: call.data.startswith("edit_photo_")
)
def edit_product_photo(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(
            call.id,
            "⛔ شما اجازه دسترسی ندارید.",
            show_alert=True
        )
        return

    bot.answer_callback_query(call.id)

    try:
        product_id = int(
            call.data.split("edit_photo_", 1)[1]
        )
    except (ValueError, IndexError):
        bot.send_message(
            call.message.chat.id,
            "شناسهٔ محصول معتبر نیست."
        )
        return

    with db_lock:
        cursor.execute(
            "SELECT name FROM products WHERE id = ?",
            (product_id,)
        )
        product = cursor.fetchone()

    if product is None:
        bot.send_message(
            call.message.chat.id,
            "این محصول پیدا نشد."
        )
        return

    admin_state[call.from_user.id] = {
        "action": "edit_product_photo",
        "product_id": product_id
    }

    bot.send_message(
        call.message.chat.id,
        f"🖼️ تغییر عکس محصول «{product[0]}»\n\n"
        "عکس جدید را به‌صورت تصویر ارسال کن.\n"
        "برای لغو عملیات، /cancel را بفرست."
    )

@bot.callback_query_handler(
    func=lambda call: call.data.startswith("add_product_")
)
def add_product_start(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(
            call.id,
            "⛔ شما اجازه دسترسی ندارید.",
            show_alert=True
        )
        return

    bot.answer_callback_query(call.id)

    try:
        category_id = int(
            call.data.split("add_product_", 1)[1]
        )
    except (ValueError, IndexError):
        bot.send_message(
            call.message.chat.id,
            "شناسهٔ دسته‌بندی معتبر نیست."
        )
        return

    with db_lock:
        cursor.execute(
            "SELECT name FROM categories WHERE id = ?",
            (category_id,)
        )
        category = cursor.fetchone()

    if category is None:
        bot.send_message(
            call.message.chat.id,
            "این دسته‌بندی پیدا نشد."
        )
        return

    if category[0] == DELETED_CATEGORY_NAME:
        bot.answer_callback_query(
            call.id,
            "⛔ افزودن محصول به دستهٔ محصولات حذف شده مجاز نیست.",
            show_alert=True
        )
        return

    admin_state[call.from_user.id] = {
        "action": "add_product",
        "category_id": category_id,
        "step": "name"
    }

    bot.send_message(
        call.message.chat.id,
        f"➕ افزودن محصول به دستهٔ «{category[0]}»\n\n"
        "نام محصول جدید را بفرست.\n"
        "برای لغو عملیات، /cancel را ارسال کن."
    )

@bot.callback_query_handler(
    func=lambda call: call.data.startswith("order_initial_")
)
def handle_initial_order_decision(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(
            call.id,
            "⛔ شما اجازه دسترسی ندارید.",
            show_alert=True
        )
        return

    try:
        action, order_id_text = call.data.rsplit("_", 1)
        order_id = int(order_id_text)

        if action not in (
            "order_initial_approve",
            "order_initial_reject"
        ):
            raise ValueError("Invalid action")

    except (ValueError, IndexError):
        bot.answer_callback_query(
            call.id,
            "درخواست نامعتبر است.",
            show_alert=True
        )
        return

    with db_lock:
        cursor.execute(
            """
            SELECT user_id, total_amount, status
            FROM orders
            WHERE id = ?
            """,
            (order_id,)
        )
        order = cursor.fetchone()

        if not order:
            bot.answer_callback_query(
                call.id,
                "این سفارش پیدا نشد.",
                show_alert=True
            )
            return

        customer_id, total, status = order

        if status != "pending":
            bot.answer_callback_query(
                call.id,
                "این سفارش قبلاً بررسی شده است.",
                show_alert=True
            )
            return

        if action == "order_initial_approve":
            new_status = "accepted"
        else:
            new_status = "rejected"

        cursor.execute(
            """
            UPDATE orders
            SET status = ?
            WHERE id = ? AND status = 'pending'
            """,
            (new_status, order_id)
        )
        db.commit()

    if action == "order_initial_approve":
        try:
            with db_lock:
                cursor.execute(
                    """
                    SELECT bank_name, account_holder,
                           card_number, iban
                    FROM payment_settings
                    WHERE id = 1
                    """
                )
                payment = cursor.fetchone()

            if payment:
                bank, holder, card, iban = payment
            else:
                bank, holder, card, iban = "", "", "", ""

            payment_lines = [
                "✅ سفارش شما توسط مدیر تأیید اولیه شد.",
                "",
                f"🧾 شماره سفارش: {order_id}",
                f"💰 مبلغ قابل پرداخت: {total:,} تومان",
                "",
                "💳 اطلاعات پرداخت",
                "━━━━━━━━━━━━━━",
                f"🏦 بانک: {bank or 'ثبت نشده'}",
                f"👤 صاحب حساب: {holder or 'ثبت نشده'}",
                f"💳 شماره کارت: {card or 'ثبت نشده'}",
                f"🔹 شماره شبا: {iban or 'ثبت نشده'}",
                "",
                "پس از پرداخت، تصویر رسید را برای ما ارسال کنید.",
                "توجه: تأیید اولیه سفارش به معنی تأیید پرداخت نیست."
            ]

            receipt_keyboard = telebot.types.InlineKeyboardMarkup()
            receipt_keyboard.add(
                telebot.types.InlineKeyboardButton(
                    "🧾 ارسال رسید این سفارش",
                    callback_data=f"receipt_select_{order_id}",
                    style="primary"
                )
            )

            bot.send_message(
                customer_id,
                "\n".join(payment_lines),
                reply_markup=receipt_keyboard
            )

            bot.answer_callback_query(
                call.id,
                "سفارش تأیید شد و اطلاعات پرداخت ارسال شد."
            )

            bot.send_message(
                ADMIN_ID,
                f"✅ سفارش شماره {order_id} تأیید اولیه شد."
            )

        except Exception as exc:
            print(f"Initial approval notification error: {exc}")
            bot.send_message(
                ADMIN_ID,
                f"⚠️ سفارش #{order_id} تأیید شد، "
                "اما ارسال پیام پرداخت به مشتری ناموفق بود."
            )

    else:
        try:
            bot.send_message(
                customer_id,
                f"❌ سفارش شماره {order_id} توسط مدیر رد شد."
            )
        except Exception as exc:
            print(f"Order rejection notification error: {exc}")

        bot.answer_callback_query(
            call.id,
            "سفارش رد شد."
        )

        bot.send_message(
            ADMIN_ID,
            f"❌ سفارش شماره {order_id} رد شد."
        )

    try:
        bot.edit_message_reply_markup(
            call.message.chat.id,
            call.message.message_id,
            reply_markup=None
        )
    except Exception as exc:
        print(f"Removing order buttons error: {exc}")

@bot.callback_query_handler(
    func=lambda call: call.data.startswith("receipt_")
)
def handle_receipt_decision(call):

    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(
            call.id,
            "⛔ شما اجازه دسترسی ندارید.",
            show_alert=True
        )
        return

    try:
        action, order_id_text = call.data.rsplit("_", 1)
        order_id = int(order_id_text)

        if action not in (
            "receipt_confirm",
            "receipt_reject"
        ):
            raise ValueError("Invalid action")

    except (ValueError, IndexError):
        bot.answer_callback_query(
            call.id,
            "درخواست نامعتبر است.",
            show_alert=True
        )
        return

    with db_lock:
        cursor.execute(
            """
            SELECT user_id, total_amount, status
            FROM orders
            WHERE id = ?
            """,
            (order_id,)
        )
        order = cursor.fetchone()

        if not order:
            bot.answer_callback_query(
                call.id,
                "سفارش پیدا نشد.",
                show_alert=True
            )
            return

        customer_id, total, status = order

        if status != "receipt_submitted":
            bot.answer_callback_query(
                call.id,
                "این رسید قبلاً بررسی شده یا وضعیت سفارش تغییر کرده است.",
                show_alert=True
            )
            return

        if action == "receipt_confirm":
            new_status = "payment_confirmed"
        else:
            new_status = "payment_rejected"

        cursor.execute(
            """
            UPDATE orders
            SET status = ?
            WHERE id = ? AND status = 'receipt_submitted'
            """,
            (new_status, order_id)
        )
        db.commit()

        updated = cursor.rowcount

    if updated != 1:
        bot.answer_callback_query(
            call.id,
            "وضعیت سفارش تغییر کرده است.",
            show_alert=True
        )
        return

    if action == "receipt_confirm":
        customer_text = (
            "✅ پرداخت شما تأیید شد.\n\n"
            f"🧾 شماره سفارش: {order_id}\n"
            f"💰 مبلغ پرداختی: {total:,} تومان\n\n"
            "سفارش شما برای آماده‌سازی و تحویل در حال پیگیری است."
        )

        admin_text = (
            f"✅ پرداخت سفارش شماره {order_id} تأیید شد.\n"
            "اکنون می‌توانید محصول یا خدمت را به مشتری تحویل دهید."
        )

    else:
        customer_text = (
            "❌ تصویر رسید پرداخت سفارش شما تأیید نشد.\n\n"
            f"🧾 شماره سفارش: {order_id}\n\n"
            "لطفاً تصویر واضح و صحیح رسید پرداخت را دوباره ارسال کن."
        )

        admin_text = (
            f"❌ رسید پرداخت سفارش شماره {order_id} رد شد.\n"
            "مشتری می‌تواند تصویر رسید جدید ارسال کند."
        )

    try:
        bot.send_message(customer_id, customer_text)
    except Exception as exc:
        print(f"Customer payment notification error: {exc}")

    bot.answer_callback_query(
        call.id,
        "وضعیت پرداخت ثبت شد."
    )

    bot.send_message(ADMIN_ID, admin_text)

    try:
        bot.edit_message_reply_markup(
            call.message.chat.id,
            call.message.message_id,
            reply_markup=None
        )
    except Exception as exc:
        print(f"Removing receipt buttons error: {exc}")

def setup_bot_commands():
    user_commands = [
        telebot.types.BotCommand("start", "استارت"),
        telebot.types.BotCommand("myid", "پروفایل"),
    ]

    admin_commands = user_commands + [
        telebot.types.BotCommand("admin", "ورود به پنل مدیریت"),
    ]

    bot.set_my_commands(
        user_commands,
        scope=telebot.types.BotCommandScopeAllPrivateChats()
    )

    bot.set_my_commands(
        admin_commands,
        scope=telebot.types.BotCommandScopeChat(chat_id=ADMIN_ID)
    )



@bot.callback_query_handler(
    func=lambda call: call.data.startswith("restore_to_")
)
def restore_to_category(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(
            call.id, "⛔ شما اجازه دسترسی ندارید.", show_alert=True
        )
        return

    try:
        parts = call.data.split("_")
        if len(parts) != 4:
            raise ValueError
        product_id = int(parts[2])
        target_category_id = int(parts[3])
    except (ValueError, IndexError):
        bot.answer_callback_query(
            call.id, "اطلاعات بازیابی نامعتبر است.", show_alert=True
        )
        return

    error_message = None
    product_name = None

    try:
        with db_lock:
            cursor.execute(
                "SELECT id FROM categories WHERE name = ?",
                (DELETED_CATEGORY_NAME,)
            )
            special = cursor.fetchone()

            cursor.execute(
                "SELECT name FROM categories WHERE id = ?",
                (target_category_id,)
            )
            target = cursor.fetchone()

            if special is None:
                error_message = "دسته‌بندی سیستمی پیدا نشد."
            elif target is None or target[0] == DELETED_CATEGORY_NAME:
                error_message = "دسته‌بندی مقصد معتبر نیست."
            else:
                cursor.execute(
                    """
                    SELECT name
                    FROM products
                    WHERE id = ? AND active = 0 AND category_id = ?
                    """,
                    (product_id, special[0])
                )
                product = cursor.fetchone()

                if product is None:
                    error_message = "این محصول دیگر در فهرست حذف‌شده‌ها نیست."
                else:
                    cursor.execute(
                        """
                        UPDATE products
                        SET category_id = ?,
                            active = 1,
                            deleted_from_category_id = NULL
                        WHERE id = ?
                          AND active = 0
                          AND category_id = ?
                        """,
                        (target_category_id, product_id, special[0])
                    )

                    if cursor.rowcount != 1:
                        db.rollback()
                        error_message = "بازیابی انجام نشد؛ وضعیت محصول تغییر کرده است."
                    else:
                        product_name = product[0]
                        db.commit()

    except sqlite3.Error:
        with db_lock:
            db.rollback()
        error_message = "خطای پایگاه داده هنگام بازیابی محصول."

    if error_message:
        bot.answer_callback_query(
            call.id, error_message, show_alert=True
        )
        return

    bot.answer_callback_query(call.id, "محصول با موفقیت بازیابی شد.")
    try:
        bot.edit_message_text(
            f"✅ محصول «{product_name}» بازیابی شد و به دسته‌بندی مقصد منتقل شد.",
            call.message.chat.id,
            call.message.message_id
        )
    except Exception:
        bot.send_message(
            call.message.chat.id,
            f"✅ محصول «{product_name}» بازیابی شد."
        )

setup_bot_commands()


if __name__ == "__main__":
    print("Telegram bot is starting...")
    bot.infinity_polling(
        timeout=30,
        long_polling_timeout=25,
        skip_pending=True
    )
