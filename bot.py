import asyncio
import html
import logging
import os

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

# Local development: reads .env if present. On Railway the variable comes
# from the service's Variables tab, so this is simply a no-op there.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

router = Router()

# Admin's numeric Telegram ID (can be overridden with the ADMIN_ID env variable)
ADMIN_ID = int(os.getenv("ADMIN_ID", "9158813748"))

# In-memory data (resets on every restart/redeploy - use a database for production)
PRODUCTS = {
    "1": {"name": "Laptop", "price": 999.99, "description": "High-performance laptop", "stock": 10},
    "2": {"name": "Smartphone", "price": 599.99, "description": "Latest smartphone model", "stock": 15},
    "3": {"name": "Headphones", "price": 199.99, "description": "Wireless noise-canceling headphones", "stock": 20},
    "4": {"name": "Tablet", "price": 399.99, "description": "10-inch tablet with stylus", "stock": 8},
    "5": {"name": "Smart Watch", "price": 299.99, "description": "Fitness tracking smartwatch", "stock": 12},
}
USER_CARTS: dict[int, dict[str, int]] = {}


# ---------- helpers ----------

async def safe_edit(callback: CallbackQuery, text: str, markup=None) -> None:
    """Edit the message; ignore Telegram's 'message is not modified' error."""
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


def main_menu_markup():
    b = InlineKeyboardBuilder()
    b.add(
        InlineKeyboardButton(text="🛍️ Browse Products", callback_data="browse_products"),
        InlineKeyboardButton(text="🛒 View Cart", callback_data="view_cart"),
        InlineKeyboardButton(text="ℹ️ Help", callback_data="help"),
    )
    b.adjust(1)
    return b.as_markup()


def back_markup():
    b = InlineKeyboardBuilder()
    b.add(InlineKeyboardButton(text="🔙 Back to Menu", callback_data="back_to_menu"))
    return b.as_markup()


def product_view(product_id: str):
    product = PRODUCTS[product_id]
    b = InlineKeyboardBuilder()
    if product["stock"] > 0:
        b.add(InlineKeyboardButton(text="➕ Add to Cart", callback_data=f"add_to_cart_{product_id}"))
    else:
        b.add(InlineKeyboardButton(text="❌ Out of Stock", callback_data="out_of_stock"))
    b.row(
        InlineKeyboardButton(text="🛍️ Browse Products", callback_data="browse_products"),
        InlineKeyboardButton(text="🔙 Back to Menu", callback_data="back_to_menu"),
    )
    text = (
        f"<b>{html.escape(product['name'])}</b>\n\n"
        f"💰 Price: ${product['price']:.2f}\n"
        f"📦 Stock: {product['stock']}\n"
        f"📝 Description: {html.escape(product['description'])}"
    )
    return text, b.as_markup()


def cart_view(user_id: int):
    cart = USER_CARTS.get(user_id, {})
    if not cart:
        return "🛒 Your cart is empty!\n\nStart shopping to add items to your cart.", back_markup()

    text = "🛒 <b>Your Cart:</b>\n\n"
    total = 0.0
    b = InlineKeyboardBuilder()
    for product_id, quantity in cart.items():
        product = PRODUCTS.get(product_id)
        if not product:
            continue
        item_total = product["price"] * quantity
        total += item_total
        text += f"• {html.escape(product['name'])} x{quantity} - ${item_total:.2f}\n"
        b.add(InlineKeyboardButton(text=f"➖ Remove {product['name']}", callback_data=f"remove_{product_id}"))
    text += f"\n💰 <b>Total: ${total:.2f}</b>"
    b.adjust(1)
    b.row(
        InlineKeyboardButton(text="💳 Checkout", callback_data="checkout"),
        InlineKeyboardButton(text="🛍️ Continue Shopping", callback_data="browse_products"),
    )
    b.row(InlineKeyboardButton(text="🔙 Back to Menu", callback_data="back_to_menu"))
    return text, b.as_markup()


# ---------- handlers ----------

@router.message(CommandStart())
async def start_handler(message: Message) -> None:
    name = html.escape(message.from_user.first_name or "there")
    await message.answer(
        f"Welcome to our E-Store, {name}! 🛒\n\nWhat would you like to do today?",
        reply_markup=main_menu_markup(),
    )


@router.callback_query(F.data == "browse_products")
async def browse_products(callback: CallbackQuery) -> None:
    b = InlineKeyboardBuilder()
    for product_id, p in PRODUCTS.items():
        b.add(InlineKeyboardButton(
            text=f"{p['name']} - ${p['price']} (Stock: {p['stock']})",
            callback_data=f"product_{product_id}",
        ))
    b.adjust(1)
    b.row(InlineKeyboardButton(text="🔙 Back to Menu", callback_data="back_to_menu"))
    await safe_edit(
        callback,
        "🛍️ <b>Available Products:</b>\n\nClick on any product to view details and add to cart:",
        b.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("product_"))
async def show_product(callback: CallbackQuery) -> None:
    product_id = callback.data.removeprefix("product_")
    if product_id not in PRODUCTS:
        await callback.answer("Product not found!", show_alert=True)
        return
    text, markup = product_view(product_id)
    await safe_edit(callback, text, markup)
    await callback.answer()


@router.callback_query(F.data.startswith("add_to_cart_"))
async def add_to_cart(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    product_id = callback.data.removeprefix("add_to_cart_")
    product = PRODUCTS.get(product_id)
    if not product or product["stock"] <= 0:
        await callback.answer("Sorry, this product is out of stock!", show_alert=True)
        return

    cart = USER_CARTS.setdefault(user_id, {})
    cart[product_id] = cart.get(product_id, 0) + 1
    product["stock"] -= 1

    await callback.answer(f"✅ {product['name']} added to cart!")
    text, markup = product_view(product_id)
    await safe_edit(callback, text, markup)


@router.callback_query(F.data == "view_cart")
async def view_cart(callback: CallbackQuery) -> None:
    text, markup = cart_view(callback.from_user.id)
    await safe_edit(callback, text, markup)
    await callback.answer()


@router.callback_query(F.data.startswith("remove_"))
async def remove_from_cart(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    product_id = callback.data.removeprefix("remove_")
    cart = USER_CARTS.get(user_id, {})

    if product_id in cart:
        if cart[product_id] > 1:
            cart[product_id] -= 1
        else:
            del cart[product_id]
        if product_id in PRODUCTS:
            PRODUCTS[product_id]["stock"] += 1
        await callback.answer("✅ Removed from cart!")
    else:
        await callback.answer("This item is not in your cart.")

    text, markup = cart_view(user_id)
    await safe_edit(callback, text, markup)


@router.callback_query(F.data == "checkout")
async def checkout(callback: CallbackQuery) -> None:
    user_id = callback.from_user.id
    cart = USER_CARTS.get(user_id, {})
    if not cart:
        await callback.answer("Your cart is empty!", show_alert=True)
        return

    total = sum(PRODUCTS[pid]["price"] * qty for pid, qty in cart.items() if pid in PRODUCTS)
    items = "\n".join(
        f"• {html.escape(PRODUCTS[pid]['name'])} x{qty}" for pid, qty in cart.items() if pid in PRODUCTS
    )
    USER_CARTS[user_id] = {}  # simulated payment

    # Notify the admin about the new order
    user = callback.from_user
    try:
        await callback.bot.send_message(
            ADMIN_ID,
            "🆕 <b>New order</b>\n\n"
            f"👤 {html.escape(user.full_name)} (<code>{user.id}</code>)\n\n"
            f"{items}\n\n💰 Total: ${total:.2f}",
        )
    except Exception as e:
        logger.warning("Could not notify admin: %s", e)

    await safe_edit(
        callback,
        "✅ <b>Order Confirmed!</b>\n\n"
        f"💰 Total Amount: ${total:.2f}\n"
        "📦 Your order will be delivered in 3-5 business days.\n\n"
        "Thank you for shopping with us! 🎉",
        back_markup(),
    )
    await callback.answer()


@router.callback_query(F.data == "help")
async def help_command(callback: CallbackQuery) -> None:
    await safe_edit(
        callback,
        "ℹ️ <b>How to use this bot:</b>\n\n"
        "🛍️ <b>Browse Products</b> - View all available items\n"
        "🛒 <b>View Cart</b> - Check items in your cart\n"
        "➕ <b>Add to Cart</b> - Add products you like\n"
        "➖ <b>Remove from Cart</b> - Remove unwanted items\n"
        "💳 <b>Checkout</b> - Complete your purchase\n\n"
        "Need more help? Contact support: @yoursupport",
        back_markup(),
    )
    await callback.answer()


@router.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: CallbackQuery) -> None:
    await safe_edit(
        callback,
        "🛒 <b>Welcome to our E-Store!</b>\n\nWhat would you like to do?",
        main_menu_markup(),
    )
    await callback.answer()


@router.callback_query(F.data == "out_of_stock")
async def out_of_stock(callback: CallbackQuery) -> None:
    await callback.answer("This item is currently out of stock!", show_alert=True)


# ---------- entrypoint ----------

async def main() -> None:
    token = (os.getenv("BOT_TOKEN") or "").strip()
    if not token:
        raise SystemExit(
            "BOT_TOKEN is not set. On Railway: service -> Variables -> add BOT_TOKEN."
        )

    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)

    # Make sure no old webhook blocks long polling, and skip updates queued while offline
    await bot.delete_webhook(drop_pending_updates=True)

    me = await bot.get_me()
    logger.info("Bot started as @%s", me.username)
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
