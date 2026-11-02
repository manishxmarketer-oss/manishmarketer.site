from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from email import policy
from email.parser import BytesParser
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from urllib.parse import parse_qs, urlsplit


PRODUCTS = [
    {
        "id": 1,
        "name": "Wireless Headphones",
        "category": "Electronics",
        "price": 4199.00,
        "rating": 4.8,
        "emoji": "🎧",
        "color": "#e9e5ff",
        "description": "Immersive sound, all-day comfort, and a 30-hour battery.",
    },
    {
        "id": 2,
        "name": "Everyday Backpack",
        "category": "Fashion",
        "price": 3299.00,
        "rating": 4.6,
        "emoji": "🎒",
        "color": "#e3f2e8",
        "description": "A lightweight, roomy companion for work and weekends.",
    },
    {
        "id": 3,
        "name": "Ceramic Coffee Set",
        "category": "Home",
        "price": 2199.00,
        "rating": 4.9,
        "emoji": "☕",
        "color": "#f8e9dc",
        "description": "Four handmade-style mugs for your favorite daily ritual.",
    },
    {
        "id": 4,
        "name": "Smart Fitness Watch",
        "category": "Electronics",
        "price": 6499.00,
        "rating": 4.7,
        "emoji": "⌚",
        "color": "#e1eff8",
        "description": "Track your movement, sleep, and goals at a glance.",
    },
    {
        "id": 5,
        "name": "Botanical Desk Plant",
        "category": "Home",
        "price": 999.00,
        "rating": 4.5,
        "emoji": "🪴",
        "color": "#e8f0df",
        "description": "A cheerful, easy-care touch of green for any room.",
    },
    {
        "id": 6,
        "name": "Everyday Sunglasses",
        "category": "Fashion",
        "price": 2599.00,
        "rating": 4.4,
        "emoji": "🕶️",
        "color": "#f5e8e8",
        "description": "Lightweight frames with UV protection and timeless style.",
    },
]

MERCHANT_UPI_ID = os.environ.get("MERCHANT_UPI_ID", "").strip()
if MERCHANT_UPI_ID and not re.fullmatch(r"[A-Za-z0-9._-]{2,256}@[A-Za-z0-9.-]{2,64}", MERCHANT_UPI_ID):
    raise ValueError("MERCHANT_UPI_ID must be a valid UPI ID, such as shopname@bank")

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "manish.xmarketer@gmail.com").strip()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "manish2010@?123")
ADMIN_AUTH_CONFIGURED = bool(ADMIN_EMAIL and ADMIN_PASSWORD)
ADMIN_SESSION_SECONDS = 30 * 60
CUSTOMER_SESSION_SECONDS = 30 * 24 * 60 * 60
CUSTOMER_DATABASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "customers.sqlite3")
PRODUCT_IMAGE_DIRECTORY = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "product_images"
)
PRODUCT_IMAGE_URL_PREFIX = "/product-images/"
MAX_IMAGE_SIZE = 5 * 1024 * 1024
MAX_MULTIPART_SIZE = MAX_IMAGE_SIZE + 1024 * 1024
CUSTOMER_PASSWORD_ITERATIONS = 260_000
ADMIN_COOKIE_SECURE = os.environ.get("ADMIN_COOKIE_SECURE", "1").lower() in ("1", "true", "yes")
ADMIN_SESSIONS = {}
CUSTOMER_SESSIONS = {}
ADMIN_LOGIN_ATTEMPTS = {}
ADMIN_AUTH_LOCK = threading.Lock()
ORDER_STATUSES = (
    "confirmed",
    "processing",
    "shipped",
    "delivered",
    "cancelled",
)


def initialize_customer_database():
    with sqlite3.connect(CUSTOMER_DATABASE) as connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS customers (
                email TEXT PRIMARY KEY,
                password_salt TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                price REAL NOT NULL,
                rating REAL NOT NULL,
                emoji TEXT NOT NULL,
                color TEXT NOT NULL,
                description TEXT NOT NULL,
                image_url TEXT NOT NULL DEFAULT ''
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                customer_name TEXT NOT NULL,
                address TEXT NOT NULL,
                pincode TEXT NOT NULL,
                phone TEXT NOT NULL,
                payment_method TEXT NOT NULL,
                items_json TEXT NOT NULL,
                total REAL NOT NULL,
                email_status TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                customer_email TEXT
            )"""
        )
        order_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(orders)")
        }
        if "order_status" not in order_columns:
            connection.execute(
                "ALTER TABLE orders ADD COLUMN order_status TEXT NOT NULL DEFAULT 'pending'"
            )
            connection.execute(
                "UPDATE orders SET order_status = 'confirmed'"
            )
        if "customer_email" not in order_columns:
            connection.execute(
                "ALTER TABLE orders ADD COLUMN customer_email TEXT"
            )
        product_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(products)")
        }
        if "image_url" not in product_columns:
            connection.execute(
                "ALTER TABLE products ADD COLUMN image_url TEXT NOT NULL DEFAULT ''"
            )
        product_count = connection.execute(
            "SELECT COUNT(*) FROM products"
        ).fetchone()[0]
        if product_count == 0:
            connection.executemany(
                """INSERT INTO products
                (name, category, price, rating, emoji, color, description)
                VALUES (:name, :category, :price, :rating, :emoji, :color, :description)""",
                PRODUCTS,
            )


def load_products():
    with sqlite3.connect(CUSTOMER_DATABASE) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT id, name, category, price, rating, emoji, color, description, image_url "
            "FROM products ORDER BY id"
        ).fetchall()
    return [dict(row) for row in rows]


def save_order(order_id, order, customer_email=""):
    with sqlite3.connect(CUSTOMER_DATABASE) as connection:
        connection.execute(
            """INSERT INTO orders
            (order_id, customer_name, address, pincode, phone, payment_method,
             items_json, total, email_status, order_status, customer_email)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'not_required', 'confirmed', ?)""",
            (
                order_id,
                order["customer_name"],
                order["address"],
                order["pincode"],
                order["phone"],
                order["payment_method"],
                json.dumps(order["items"]),
                order["total"],
                customer_email or None,
            ),
        )


def load_customer_orders(customer_email):
    with sqlite3.connect(CUSTOMER_DATABASE) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """SELECT order_id, items_json, total, order_status, created_at
            FROM orders WHERE customer_email = ?
            ORDER BY created_at DESC, rowid DESC""",
            (customer_email,),
        ).fetchall()
    return [dict(row) for row in rows]


def update_order_status(order_id, status):
    with sqlite3.connect(CUSTOMER_DATABASE) as connection:
        cursor = connection.execute(
            "UPDATE orders SET order_status = ? WHERE order_id = ?",
            (status, order_id),
        )
        if cursor.rowcount != 1:
            raise sqlite3.Error("Order record was not found.")


def load_sales_data():
    with sqlite3.connect(CUSTOMER_DATABASE) as connection:
        connection.row_factory = sqlite3.Row
        summary = connection.execute(
            """SELECT COUNT(*) AS order_count,
                COALESCE(SUM(total), 0) AS revenue,
                COALESCE(AVG(total), 0) AS average_order
            FROM orders WHERE order_status != 'cancelled'"""
        ).fetchone()
        rows = connection.execute(
            """SELECT order_id, customer_name, address, pincode, phone,
                payment_method, items_json, total, order_status, customer_email,
                created_at
            FROM orders ORDER BY created_at DESC, rowid DESC LIMIT 100"""
        ).fetchall()
    return dict(summary), [dict(row) for row in rows]


def validate_product(fields):
    name = fields.get("name", [""])[0].strip()
    category = fields.get("category", [""])[0].strip()
    emoji = fields.get("emoji", [""])[0].strip()
    color = fields.get("color", [""])[0].strip()
    description = fields.get("description", [""])[0].strip()
    image_url = fields.get("image_url", [""])[0].strip()
    try:
        price = float(fields.get("price", [""])[0])
        rating = float(fields.get("rating", [""])[0])
    except ValueError as error:
        raise ValueError("Price and rating must be numbers.") from error

    if not name or len(name) > 120:
        raise ValueError("Product name is required and must be 120 characters or fewer.")
    if not category or len(category) > 60:
        raise ValueError("Category is required and must be 60 characters or fewer.")
    if not 0 <= price <= 1_000_000_000:
        raise ValueError("Price must be between 0 and 1,000,000,000.")
    if not 0 <= rating <= 5:
        raise ValueError("Rating must be between 0 and 5.")
    if not emoji or len(emoji) > 16:
        raise ValueError("Emoji is required and must be 16 characters or fewer.")
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
        raise ValueError("Choose a valid six-digit hex color.")
    if len(description) > 500:
        raise ValueError("Description must be 500 characters or fewer.")
    if len(image_url) > 2048:
        raise ValueError("Image URL must be 2048 characters or fewer.")
    if image_url:
        try:
            parsed_image_url = urlsplit(image_url)
        except ValueError as error:
            raise ValueError("Enter a valid HTTPS image URL.") from error
        if (
            parsed_image_url.scheme != "https"
            or not parsed_image_url.hostname
            or parsed_image_url.username
            or parsed_image_url.password
        ):
            raise ValueError("Image URL must use HTTPS and include a valid host.")
    return {
        "name": name,
        "category": category,
        "price": price,
        "rating": rating,
        "emoji": emoji,
        "color": color,
        "description": description,
        "image_url": image_url,
    }


def product_image_format(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg", "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif", "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    raise ValueError("Choose a valid PNG, JPEG, GIF, or WebP image.")


def save_product_image(upload):
    data = upload["data"]
    if not data or len(data) > MAX_IMAGE_SIZE:
        raise ValueError("Product images must be no larger than 5 MB.")
    extension, _ = product_image_format(data)
    filename = "%s.%s" % (secrets.token_hex(16), extension)
    os.makedirs(PRODUCT_IMAGE_DIRECTORY, exist_ok=True)
    image_path = os.path.join(PRODUCT_IMAGE_DIRECTORY, filename)
    try:
        with open(image_path, "xb") as image_file:
            image_file.write(data)
    except OSError:
        remove_product_image(PRODUCT_IMAGE_URL_PREFIX + filename)
        raise
    return PRODUCT_IMAGE_URL_PREFIX + filename


def product_image_path(image_url):
    if not image_url.startswith(PRODUCT_IMAGE_URL_PREFIX):
        return None
    filename = image_url[len(PRODUCT_IMAGE_URL_PREFIX):]
    if not re.fullmatch(r"[a-f0-9]{32}\.(?:png|jpg|gif|webp)", filename):
        return None
    return os.path.join(PRODUCT_IMAGE_DIRECTORY, filename)


def remove_product_image(image_url):
    image_path = product_image_path(image_url)
    if image_path is None:
        return
    try:
        os.remove(image_path)
    except FileNotFoundError:
        return
    except OSError as error:
        print("Unable to remove a product image: %s" % error)


def hash_customer_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    password_hash = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        CUSTOMER_PASSWORD_ITERATIONS,
    )
    return salt.hex(), password_hash.hex()


def verify_customer_password(email, password):
    with sqlite3.connect(CUSTOMER_DATABASE) as connection:
        customer = connection.execute(
            "SELECT password_salt, password_hash FROM customers WHERE email = ?",
            (email,),
        ).fetchone()
    if customer is None:
        return False
    salt = bytes.fromhex(customer[0])
    _, supplied_hash = hash_customer_password(password, salt)
    return hmac.compare_digest(supplied_hash, customer[1])


def validate_order(fields):
    name = fields.get("customer_name", [""])[0].strip()
    address = fields.get("address", [""])[0].strip()
    pincode = fields.get("pincode", [""])[0].strip()
    phone = fields.get("phone", [""])[0].strip()
    payment_method = fields.get("payment_method", [""])[0].strip()
    items_text = fields.get("items", [""])[0]

    if not name or len(name) > 120:
        raise ValueError("Enter your name (up to 120 characters).")
    if not address or len(address) > 300:
        raise ValueError("Enter your delivery location (up to 300 characters).")
    if not re.fullmatch(r"[0-9]{6}", pincode):
        raise ValueError("Enter a valid six-digit pincode.")
    if (
        not re.fullmatch(r"\+?[0-9][0-9\s()-]{6,19}", phone)
        or not 7 <= sum(character.isdigit() for character in phone) <= 15
    ):
        raise ValueError("Enter a valid phone number.")
    if payment_method not in ("upi", "cash_on_delivery"):
        raise ValueError("Choose a valid payment method.")
    if payment_method == "upi" and not MERCHANT_UPI_ID:
        raise ValueError("UPI is not available. Choose cash on delivery.")

    try:
        requested_items = json.loads(items_text)
    except json.JSONDecodeError as error:
        raise ValueError("Your cart is invalid. Refresh the page and try again.") from error
    if not isinstance(requested_items, list) or not requested_items:
        raise ValueError("Your cart is empty.")
    if len(requested_items) > 50:
        raise ValueError("Your cart contains too many different products.")

    products = {product["id"]: product for product in load_products()}
    items = []
    seen_product_ids = set()
    for requested in requested_items:
        if not isinstance(requested, dict):
            raise ValueError("Your cart is invalid. Refresh the page and try again.")
        product_id = requested.get("id")
        quantity = requested.get("quantity")
        if (
            type(product_id) is not int
            or type(quantity) is not int
            or quantity < 1
            or quantity > 99
            or product_id in seen_product_ids
            or product_id not in products
        ):
            raise ValueError("Your cart is invalid. Refresh the page and try again.")
        seen_product_ids.add(product_id)
        product = products[product_id]
        items.append({
            "name": product["name"],
            "quantity": quantity,
            "unit_price": product["price"],
            "line_total": product["price"] * quantity,
        })

    return {
        "customer_name": name,
        "address": address,
        "pincode": pincode,
        "phone": phone,
        "payment_method": payment_method,
        "items": items,
        "total": round(sum(item["line_total"] for item in items), 2),
    }


PAGE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#172b4d">
  <title>Manish Marketer | Find your next favorite</title>
  <style>
    :root {
      color-scheme: light;
      --ink: #172b4d;
      --muted: #68758a;
      --accent: #ffb547;
      --paper: #f5f7fa;
      --line: #e7ebf0;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--paper);
      color: var(--ink);
      font: 15px/1.5 "Segoe UI", Arial, sans-serif;
    }
    button, input { font: inherit; }
    button { cursor: pointer; }
    .topbar {
      align-items: center;
      background: #172b4d;
      color: white;
      display: flex;
      gap: 24px;
      justify-content: space-between;
      padding: 14px max(5vw, 22px);
    }
    .brand { color: white; font-size: 21px; font-weight: 800; text-decoration: none; white-space: nowrap; }
    .brand span { color: var(--accent); }
    .search {
      background: white;
      border-radius: 9px;
      display: flex;
      max-width: 610px;
      overflow: hidden;
      width: 100%;
    }
    .search input { border: 0; min-width: 0; outline: 0; padding: 12px 15px; width: 100%; }
    .search button { background: var(--accent); border: 0; color: var(--ink); font-weight: 700; padding: 0 18px; }
    .cart-button {
      background: transparent;
      border: 1px solid #71809a;
      border-radius: 8px;
      color: white;
      font-weight: 700;
      padding: 10px 14px;
      white-space: nowrap;
    }
    .account-menu {
      align-items: center;
      color: white;
      display: inline-flex;
      font-size: 14px;
      font-weight: 700;
      gap: 7px;
      text-decoration: none;
      white-space: nowrap;
    }
    .account-menu:hover { color: var(--accent); }
    .logout-form { margin: 0; }
    .logout-button {
      background: transparent;
      border: 0;
      cursor: pointer;
      font: inherit;
      padding: 0;
    }
    .account-icon { display: block; height: 23px; width: 23px; }
    .cart-count {
      background: var(--accent);
      border-radius: 20px;
      color: var(--ink);
      display: inline-block;
      margin-left: 6px;
      min-width: 22px;
      padding: 1px 6px;
      text-align: center;
    }
    main { margin: 0 auto; max-width: 1180px; padding: 28px 22px 60px; }
    .hero {
      align-items: center;
      background: linear-gradient(115deg, #eaf0fa, #fff4e3);
      border-radius: 18px;
      display: flex;
      justify-content: space-between;
      margin-bottom: 30px;
      min-height: 225px;
      overflow: hidden;
      padding: 34px clamp(22px, 6vw, 72px);
    }
    .hero-copy { max-width: 570px; }
    .eyebrow { color: #5b6d89; font-size: 12px; font-weight: 800; letter-spacing: 1.5px; text-transform: uppercase; }
    h1 { font-size: clamp(30px, 4vw, 46px); letter-spacing: -1.5px; line-height: 1.1; margin: 10px 0; }
    .hero p { color: #52627b; font-size: 16px; margin: 0 0 19px; }
    .shop-link { background: var(--ink); border-radius: 8px; color: white; display: inline-block; font-weight: 700; padding: 11px 17px; text-decoration: none; }
    .hero-art { font-size: clamp(80px, 13vw, 145px); padding: 0 20px; }
    .section-heading { align-items: end; display: flex; justify-content: space-between; margin: 0 0 14px; }
    h2 { font-size: 23px; letter-spacing: -.5px; margin: 0; }
    .result-count { color: var(--muted); font-size: 13px; }
    .filters { display: flex; flex-wrap: wrap; gap: 9px; margin-bottom: 22px; }
    .filter {
      background: white;
      border: 1px solid var(--line);
      border-radius: 24px;
      color: #52627b;
      padding: 8px 15px;
    }
    .filter.active { background: var(--ink); border-color: var(--ink); color: white; }
    .products { display: grid; gap: 18px; grid-template-columns: repeat(3, minmax(0, 1fr)); }
    .card { background: white; border: 1px solid var(--line); border-radius: 13px; overflow: hidden; }
    .product-art { align-items: center; display: flex; font-size: 70px; height: 175px; justify-content: center; overflow: hidden; position: relative; }
    .product-image { height: 100%; inset: 0; object-fit: cover; position: absolute; width: 100%; }
    .product-emoji { position: relative; }
    .product-emoji[hidden], .product-image[hidden] { display: none; }
    .card-body { padding: 16px; }
    .category { color: var(--muted); font-size: 12px; font-weight: 700; text-transform: uppercase; }
    .product-name { font-size: 17px; margin: 5px 0; }
    .description { color: var(--muted); font-size: 13px; min-height: 39px; }
    .rating { color: #986100; font-size: 13px; margin: 7px 0 12px; }
    .buy-row { align-items: center; display: flex; justify-content: space-between; }
    .price { font-size: 19px; font-weight: 800; }
    .add-button { background: var(--accent); border: 0; border-radius: 7px; color: var(--ink); font-weight: 800; padding: 9px 12px; }
    .empty { background: white; border-radius: 12px; color: var(--muted); grid-column: 1 / -1; padding: 35px; text-align: center; }
    .cart-panel {
      background: white;
      border: 1px solid var(--line);
      border-radius: 12px;
      box-shadow: 0 15px 40px #172b4d22;
      display: none;
      padding: 18px;
      position: fixed;
      right: max(22px, calc((100vw - 1180px) / 2));
      top: 76px;
      width: min(360px, calc(100vw - 32px));
      z-index: 2;
    }
    .cart-panel.open { display: block; }
    .cart-heading { align-items: center; display: flex; justify-content: space-between; }
    .cart-heading h2 { font-size: 19px; }
    .close-cart { background: transparent; border: 0; color: var(--muted); font-size: 23px; }
    .cart-items { max-height: 300px; overflow: auto; }
    .cart-item { align-items: center; border-bottom: 1px solid var(--line); display: flex; gap: 10px; padding: 12px 0; }
    .cart-item-name { flex: 1; font-weight: 650; }
    .cart-item small { color: var(--muted); display: block; font-weight: 400; }
    .remove-button { background: transparent; border: 0; color: #a23d3d; font-size: 12px; }
    .cart-total { border-top: 1px solid var(--line); display: flex; font-weight: 800; justify-content: space-between; margin-top: 10px; padding-top: 14px; }
    .checkout { background: var(--ink); border: 0; border-radius: 7px; color: white; font-weight: 700; margin-top: 13px; padding: 11px; width: 100%; }
    .payment-dialog {
      border: 0;
      border-radius: 14px;
      box-shadow: 0 18px 55px #172b4d44;
      color: var(--ink);
      max-height: calc(100vh - 32px);
      max-width: 520px;
      overflow: auto;
      padding: 24px;
      width: calc(100% - 32px);
    }
    .payment-dialog::backdrop { background: #172b4d88; }
    .payment-dialog h2 { margin-bottom: 12px; }
    .payment-dialog p { color: #52627b; }
    .payment-amount { display: block; font-size: 30px; margin: 14px 0; }
    .payment-warning { background: #fff4e3; border-radius: 8px; color: #62440d !important; padding: 11px; }
    .upi-link { background: #172b4d; border-radius: 8px; color: white; display: block; font-weight: 700; margin-top: 16px; padding: 12px; text-align: center; text-decoration: none; }
    .dialog-close { background: transparent; border: 0; color: var(--muted); float: right; font-size: 24px; }
    .checkout-fields label { display: block; font-size: 13px; font-weight: 700; margin: 13px 0 5px; }
    .checkout-fields input, .checkout-fields select, .checkout-fields textarea {
      border: 1px solid #cbd3df;
      border-radius: 7px;
      color: var(--ink);
      font: inherit;
      padding: 10px;
      width: 100%;
    }
    .checkout-fields textarea { resize: vertical; }
    .checkout-grid { display: grid; gap: 12px; grid-template-columns: 1fr 1fr; }
    .checkout-tracking-note { background: #eaf0fa; border-radius: 7px; color: #52627b; font-size: 13px; padding: 10px; }
    .checkout-tracking-note a { color: #172b4d; font-weight: 700; }
    .order-message { color: #a23d3d !important; }
    .order-success { background: #eaf5ec; border-radius: 8px; color: #315f3b !important; padding: 11px; }
    footer { border-top: 1px solid var(--line); color: var(--muted); padding: 20px; text-align: center; }
    @media (max-width: 760px) {
      .topbar { flex-wrap: wrap; gap: 12px; }
      .search { order: 3; max-width: none; }
      .products { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .hero-art { padding: 0; }
    }
    @media (max-width: 480px) {
      main { padding-left: 15px; padding-right: 15px; }
      .hero { min-height: 195px; padding: 24px 18px; }
      .hero-art { font-size: 65px; }
      .products { gap: 11px; grid-template-columns: 1fr; }
      .product-art { height: 150px; }
    }
  </style>
</head>
<body>
  <header class="topbar">
    <a class="brand" href="#" aria-label="Manish Marketer home">Manish <span>Marketer</span></a>
    <form class="search" id="search-form" role="search">
      <input id="search-input" type="search" placeholder="Search products..." aria-label="Search products">
      <button type="submit">Search</button>
    </form>
    __ACCOUNT_LINK__
    <button class="cart-button" id="cart-toggle" type="button" aria-expanded="false">
      🛒 Cart <span class="cart-count" id="cart-count">0</span>
    </button>
  </header>
  <main>
    <section class="hero">
      <div class="hero-copy">
        <div class="eyebrow">Good finds. Great prices.</div>
        <h1>Make room for things you love.</h1>
        <p>Discover everyday favorites, hand-picked for you.</p>
        <a class="shop-link" href="#products">Shop the collection</a>
      </div>
      <div class="hero-art" aria-hidden="true">🛍️</div>
    </section>
    <section id="products">
      <div class="section-heading">
        <h2>Popular picks</h2>
        <span class="result-count" id="result-count"></span>
      </div>
      <nav class="filters" id="filters" aria-label="Product categories"></nav>
      <div class="products" id="product-grid" aria-live="polite"></div>
    </section>
  </main>
  <aside class="cart-panel" id="cart-panel" aria-label="Shopping cart">
    <div class="cart-heading">
      <h2>Your cart</h2>
      <button class="close-cart" id="close-cart" type="button" aria-label="Close cart">×</button>
    </div>
    <div class="cart-items" id="cart-items"></div>
    <div class="cart-total"><span>Subtotal</span><span id="cart-total">₹0.00</span></div>
    <button class="checkout" id="checkout" type="button">Continue to checkout</button>
  </aside>
  <dialog class="payment-dialog" id="payment-dialog" aria-labelledby="payment-title">
    <button class="dialog-close" id="close-payment" type="button" aria-label="Close checkout">×</button>
    <h2 id="payment-title">Delivery and payment</h2>
    <span class="payment-amount" id="payment-amount">₹0.00</span>
    <p class="checkout-tracking-note">__ORDER_TRACKING_NOTICE__</p>
    <form class="checkout-fields" id="order-form">
      <label>Full name
        <input name="customer_name" type="text" autocomplete="name" maxlength="120" required>
      </label>
      <label>Delivery location / address
        <textarea name="address" autocomplete="street-address" maxlength="300" rows="3" required></textarea>
      </label>
      <div class="checkout-grid">
        <label>Pincode
          <input name="pincode" type="text" inputmode="numeric" autocomplete="postal-code"
            pattern="[0-9]{6}" maxlength="6" required>
        </label>
        <label>Phone number
          <input name="phone" type="tel" autocomplete="tel" maxlength="20" required>
        </label>
      </div>
      <label>Payment method
        <select name="payment_method" required>__PAYMENT_OPTIONS__</select>
      </label>
      <p class="order-message" id="order-message" role="status"></p>
      <button class="checkout" id="place-order" type="submit">Place order</button>
    </form>
    <p class="order-success" id="order-success" role="status" hidden></p>
    <div id="upi-payment-details" hidden>
      <p>Pay to <strong id="merchant-upi"></strong> using your UPI app.</p>
      <a class="upi-link" id="upi-pay-link" href="#">Open UPI app</a>
      <p class="payment-warning">Payment is not verified by this demo. Confirm receipt in your bank or UPI merchant app before fulfilling an order.</p>
    </div>
  </dialog>
  <footer>© Manish Marketer · Sample INR prices; update them to your actual prices.</footer>
  <script>
    const products = __PRODUCTS__;
    const merchantUpiId = __MERCHANT_UPI_ID__;
    const inr = new Intl.NumberFormat("en-IN", {style: "currency", currency: "INR"});
    const categories = ["All", ...new Set(products.map(product => product.category))];
    const cart = new Map();
    let activeCategory = "All";
    let searchQuery = "";

    const grid = document.getElementById("product-grid");
    const filters = document.getElementById("filters");
    const panel = document.getElementById("cart-panel");
    const escapeHtml = value => String(value).replace(/[&<>"']/g, character => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    })[character]);

    function renderFilters() {
      filters.innerHTML = categories.map(category =>
        `<button class="filter ${category === activeCategory ? "active" : ""}" type="button" data-category="${escapeHtml(category)}">${escapeHtml(category)}</button>`
      ).join("");
    }

    function renderProducts() {
      const visible = products.filter(product =>
        (activeCategory === "All" || product.category === activeCategory) &&
        `${product.name} ${product.category} ${product.description}`.toLowerCase().includes(searchQuery)
      );
      document.getElementById("result-count").textContent =
        `${visible.length} ${visible.length === 1 ? "item" : "items"}`;
      if (!visible.length) {
        grid.innerHTML = '<div class="empty">No products match your search. Try another term.</div>';
        return;
      }
      grid.innerHTML = visible.map(product => `
        <article class="card">
          <div class="product-art" style="background:${escapeHtml(product.color)}">
            <img class="product-image" src="${escapeHtml(product.image_url)}" alt="${escapeHtml(product.name)}" ${product.image_url ? "" : "hidden"}>
            <span class="product-emoji" ${product.image_url ? "hidden" : ""} aria-hidden="true">${escapeHtml(product.emoji)}</span>
          </div>
          <div class="card-body">
            <div class="category">${escapeHtml(product.category)}</div>
            <h3 class="product-name">${escapeHtml(product.name)}</h3>
            <div class="description">${escapeHtml(product.description)}</div>
            <div class="rating" aria-label="${product.rating} out of 5 stars">★ ${product.rating} / 5</div>
            <div class="buy-row">
              <span class="price">${inr.format(product.price)}</span>
              <button class="add-button" type="button" data-add="${product.id}">Add to cart</button>
            </div>
          </div>
        </article>`).join("");
    }

    function renderCart() {
      const entries = [...cart.entries()];
      const count = entries.reduce((sum, [, quantity]) => sum + quantity, 0);
      const total = entries.reduce((sum, [id, quantity]) =>
        sum + products.find(product => product.id === id).price * quantity, 0);
      document.getElementById("cart-count").textContent = count;
      document.getElementById("cart-total").textContent = inr.format(total);
      document.getElementById("cart-items").innerHTML = entries.length
        ? entries.map(([id, quantity]) => {
          const product = products.find(item => item.id === id);
          return `<div class="cart-item">
            <span aria-hidden="true">${escapeHtml(product.emoji)}</span>
            <span class="cart-item-name">${escapeHtml(product.name)}<small>Qty: ${quantity} · ${inr.format(product.price * quantity)}</small></span>
            <button class="remove-button" type="button" data-remove="${id}">Remove</button>
          </div>`;
        }).join("")
        : '<p class="description">Your cart is empty. Add something you love!</p>';
    }

    filters.addEventListener("click", event => {
      const button = event.target.closest("[data-category]");
      if (!button) return;
      activeCategory = button.dataset.category;
      renderFilters();
      renderProducts();
    });

    grid.addEventListener("click", event => {
      const button = event.target.closest("[data-add]");
      if (!button) return;
      const id = Number(button.dataset.add);
      cart.set(id, (cart.get(id) || 0) + 1);
      renderCart();
      button.textContent = "Added!";
      window.setTimeout(() => { button.textContent = "Add to cart"; }, 900);
    });
    grid.addEventListener("error", event => {
      if (!event.target.matches(".product-image")) return;
      event.target.hidden = true;
      event.target.nextElementSibling.hidden = false;
    }, true);

    document.getElementById("cart-items").addEventListener("click", event => {
      const button = event.target.closest("[data-remove]");
      if (!button) return;
      cart.delete(Number(button.dataset.remove));
      renderCart();
    });

    document.getElementById("search-form").addEventListener("submit", event => {
      event.preventDefault();
      searchQuery = document.getElementById("search-input").value.trim().toLowerCase();
      renderProducts();
    });
    document.getElementById("search-input").addEventListener("input", event => {
      searchQuery = event.target.value.trim().toLowerCase();
      renderProducts();
    });
    document.getElementById("cart-toggle").addEventListener("click", event => {
      const open = panel.classList.toggle("open");
      event.currentTarget.setAttribute("aria-expanded", open);
    });
    document.getElementById("close-cart").addEventListener("click", () => {
      panel.classList.remove("open");
      document.getElementById("cart-toggle").setAttribute("aria-expanded", "false");
    });
    const paymentDialog = document.getElementById("payment-dialog");
    const orderForm = document.getElementById("order-form");
    const orderMessage = document.getElementById("order-message");
    const orderSuccess = document.getElementById("order-success");
    const upiPaymentDetails = document.getElementById("upi-payment-details");
    document.getElementById("checkout").addEventListener("click", () => {
      const entries = [...cart.entries()];
      const total = entries.reduce((sum, [id, quantity]) =>
        sum + products.find(product => product.id === id).price * quantity, 0);
      document.getElementById("payment-amount").textContent = inr.format(total);
      orderForm.reset();
      orderForm.hidden = !entries.length;
      orderMessage.textContent = entries.length ? "" : "Your cart is empty. Add an item before checking out.";
      orderSuccess.hidden = true;
      orderSuccess.textContent = "";
      upiPaymentDetails.hidden = true;
      paymentDialog.showModal();
    });
    orderForm.addEventListener("submit", async event => {
      event.preventDefault();
      if (!cart.size) {
        orderMessage.textContent = "Your cart is empty. Add an item before checking out.";
        return;
      }
      const button = document.getElementById("place-order");
      button.disabled = true;
      orderMessage.textContent = "Placing your order...";
      try {
        const formData = new FormData(orderForm);
        formData.set("items", JSON.stringify(
          [...cart.entries()].map(([id, quantity]) => ({id, quantity}))
        ));
        const response = await fetch("/orders", {
          method: "POST",
          headers: {"Content-Type": "application/x-www-form-urlencoded"},
          body: new URLSearchParams(formData),
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || "Unable to place your order.");

        const paymentAmount = Number(result.total);
        document.getElementById("payment-amount").textContent = inr.format(paymentAmount);
        cart.clear();
        renderCart();
        orderForm.hidden = true;
        orderMessage.textContent = "";
        orderSuccess.hidden = false;
        orderSuccess.textContent = `Order ${result.order_id} is confirmed and saved.`;
        if (result.tracking) {
          orderSuccess.textContent += " Track its status in your account.";
        }
        if (result.payment_method === "upi") {
          orderSuccess.textContent += " Complete your UPI payment below; payment must be verified manually.";
          document.getElementById("merchant-upi").textContent = merchantUpiId;
          const payment = new URLSearchParams({
            pa: merchantUpiId,
            pn: "Manish Marketer",
            am: paymentAmount.toFixed(2),
            cu: "INR",
            tn: `Order ${result.order_id} - Manish Marketer`,
          });
          document.getElementById("upi-pay-link").href = `upi://pay?${payment}`;
          upiPaymentDetails.hidden = false;
        }
      } catch (error) {
        orderMessage.textContent = error.message || "Unable to place your order. Please try again.";
      } finally {
        button.disabled = false;
      }
    });
    document.getElementById("close-payment").addEventListener("click", () => {
      paymentDialog.close();
    });

    renderFilters();
    renderProducts();
    renderCart();
  </script>
</body>
</html>"""


LOGIN_PAGE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#172b4d">
  <title>Sign in | Manish Marketer</title>
  <style>
    :root { --ink: #172b4d; --muted: #68758a; --accent: #ffb547; --line: #e7ebf0; }
    * { box-sizing: border-box; }
    body {
      align-items: center;
      background: linear-gradient(135deg, #f3f6fb, #fff5e7);
      color: var(--ink);
      display: flex;
      flex-direction: column;
      font: 15px/1.5 "Segoe UI", Arial, sans-serif;
      justify-content: center;
      margin: 0;
      min-height: 100vh;
      padding: 28px 18px;
    }
    a { color: inherit; }
    .brand { font-size: 22px; font-weight: 800; margin-bottom: 22px; text-decoration: none; }
    .brand span { color: #b66b00; }
    .login-card {
      background: white;
      border: 1px solid var(--line);
      border-radius: 16px;
      box-shadow: 0 18px 55px #172b4d12;
      max-width: 430px;
      padding: clamp(24px, 6vw, 38px);
      width: 100%;
    }
    h1 { font-size: 27px; letter-spacing: -.7px; margin: 0 0 5px; }
    .intro { color: var(--muted); margin: 0 0 24px; }
    label { display: block; font-size: 13px; font-weight: 700; margin: 16px 0 6px; }
    input {
      border: 1px solid #cbd3df;
      border-radius: 8px;
      color: var(--ink);
      outline: none;
      padding: 12px;
      width: 100%;
    }
    input:focus { border-color: #42669c; box-shadow: 0 0 0 3px #42669c1c; }
    .password-wrap { display: flex; }
    .password-wrap input { border-radius: 8px 0 0 8px; }
    .show-password {
      background: #f7f8fa;
      border: 1px solid #cbd3df;
      border-left: 0;
      border-radius: 0 8px 8px 0;
      color: #52627b;
      padding: 0 12px;
    }
    .remember { align-items: center; color: #52627b; display: flex; font-size: 13px; gap: 8px; margin: 15px 0; }
    .remember input { accent-color: var(--ink); height: 16px; margin: 0; width: 16px; }
    .submit {
      background: var(--accent);
      border: 0;
      border-radius: 8px;
      color: var(--ink);
      font-weight: 800;
      padding: 12px;
      width: 100%;
    }
    .submit.secondary { background: white; border: 1px solid var(--line); margin-top: 9px; }
    .message { color: #a23d3d; font-size: 13px; margin: 14px 0 0; }
    .demo-note { background: #f3f6fb; border-radius: 8px; color: #52627b; font-size: 12px; margin-top: 20px; padding: 11px; }
    .back-link { color: #52627b; display: inline-block; font-size: 13px; margin-top: 20px; text-align: center; text-decoration: none; width: 100%; }
    .back-link:hover { text-decoration: underline; }
    footer { color: var(--muted); font-size: 12px; margin-top: 25px; text-align: center; }
  </style>
</head>
<body>
  <a class="brand" href="/">Manish <span>Marketer</span></a>
  <main class="login-card">
    <h1 id="auth-title">Sign in</h1>
    <p class="intro" id="auth-intro">Sign in to your customer account.</p>
    <form id="login-form" method="post" action="/login">
      <label for="email">Email address</label>
      <input id="email" name="email" type="email" autocomplete="email" placeholder="you@example.com" required>
      <label for="password">Password</label>
      <div class="password-wrap">
        <input id="password" name="password" type="password" autocomplete="current-password" minlength="8" maxlength="128" required>
        <button class="show-password" id="toggle-password" type="button" aria-label="Show password" aria-pressed="false">Show</button>
      </div>
      <label class="remember"><input name="remember" type="checkbox"> Remember me on this device</label>
      <button class="submit" id="login-submit" type="submit" name="action" value="login">Login</button>
      <button class="submit secondary" id="register-submit" type="submit" name="action" value="register">Sign up</button>
      <p class="message" id="login-message" role="status" hidden></p>
    </form>
    <div class="demo-note">Use Login if you already created an account. Sign up to create a new customer account. Email ownership is not verified.</div>
    <a class="back-link" href="/">← Back to shopping</a>
  </main>
  <footer>© Manish Marketer</footer>
  <script>
    const password = document.getElementById("password");
    const toggle = document.getElementById("toggle-password");
    const message = document.getElementById("login-message");
    const signupMode = window.location.pathname === "/signup";
    document.getElementById("login-form").action = signupMode ? "/signup" : "/login";
    const loginSubmit = document.getElementById("login-submit");
    const registerSubmit = document.getElementById("register-submit");
    document.getElementById("auth-title").textContent = signupMode ? "Sign up" : "Login";
    document.getElementById("auth-intro").textContent = signupMode
      ? "Create a new customer account."
      : "Enter the email and password you used when you signed up.";
    loginSubmit.hidden = signupMode;
    registerSubmit.hidden = !signupMode;
    document.title = (signupMode ? "Sign up" : "Login") + " | Manish Marketer";
    const query = new URLSearchParams(window.location.search);
    if (query.has("error")) {
      const errors = {
        invalid: "Email or password is incorrect.",
        exists: "This email is already created. Please log in instead.",
        reserved: "This email is reserved for the administrator account.",
        password: "Use a password between 8 and 128 characters.",
      };
      message.textContent = errors[query.get("error")] || "Unable to sign in. Please try again.";
      message.hidden = false;
    } else if (query.has("created")) {
      message.textContent = "Customer account created. You are now signed in.";
      message.hidden = false;
      message.style.color = "#315f3b";
    } else if (query.has("signed_in")) {
      message.textContent = "You are signed in.";
      message.hidden = false;
      message.style.color = "#315f3b";
    }
    toggle.addEventListener("click", () => {
      const showing = password.type === "password";
      password.type = showing ? "text" : "password";
      toggle.textContent = showing ? "Hide" : "Show";
      toggle.setAttribute("aria-label", showing ? "Hide password" : "Show password");
      toggle.setAttribute("aria-pressed", String(showing));
    });
  </script>
</body>
</html>"""


ADMIN_DASHBOARD_PAGE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#172b4d">
  <title>Admin dashboard | Manish Marketer</title>
  <style>
    * { box-sizing: border-box; }
    body { background: #f5f7fa; color: #172b4d; font: 15px/1.5 "Segoe UI", Arial, sans-serif; margin: 0; }
    header { align-items: center; background: #172b4d; color: white; display: flex; justify-content: space-between; padding: 16px max(5vw, 22px); }
    .brand { color: white; font-size: 20px; font-weight: 800; text-decoration: none; }
    .brand span { color: #ffb547; }
    button { background: #ffb547; border: 0; border-radius: 7px; color: #172b4d; cursor: pointer; font: inherit; font-weight: 700; padding: 9px 13px; }
    main { margin: 35px auto; max-width: 1000px; padding: 0 20px; }
    h1 { margin-bottom: 5px; }
    .intro, .note { color: #68758a; }
    .panel { background: white; border: 1px solid #e7ebf0; border-radius: 12px; margin-top: 22px; padding: 18px; }
    header nav { align-items: center; display: flex; gap: 10px; }
    .nav-link { background: #ffb547; border-radius: 7px; color: #172b4d; font-weight: 700; padding: 9px 13px; text-decoration: none; }
    .product-list { display: grid; gap: 16px; }
    .product-card { border: 1px solid #e7ebf0; border-radius: 10px; padding: 16px; }
    .fields { display: grid; gap: 12px; grid-template-columns: repeat(2, minmax(0, 1fr)); }
    label { color: #52627b; display: block; font-size: 12px; font-weight: 700; }
    input, textarea { border: 1px solid #cbd3df; border-radius: 6px; display: block; font: inherit; margin-top: 5px; padding: 9px; width: 100%; }
    textarea { min-height: 70px; resize: vertical; }
    .image-preview { align-items: center; background: #e9e5ff; border-radius: 8px; display: flex; height: 100px; justify-content: center; margin-top: 8px; overflow: hidden; width: 120px; }
    .image-preview img { height: 100%; object-fit: cover; width: 100%; }
    .image-preview span { font-size: 42px; }
    .image-preview img[hidden], .image-preview span[hidden] { display: none; }
    .image-remove input { display: inline; margin-right: 6px; width: auto; }
    .actions { display: flex; flex-wrap: wrap; gap: 9px; margin-top: 13px; }
    .delete { background: #f9e8e8; color: #8a2525; }
    .notice { background: #eaf5ec; border-radius: 8px; color: #315f3b; margin: 15px 0; padding: 11px; }
    .error { background: #fff0ef; color: #8a2525; }
    @media (max-width: 560px) { .fields { grid-template-columns: 1fr; } header { align-items: flex-start; gap: 12px; } header nav { flex-wrap: wrap; justify-content: flex-end; } }
    footer { color: #68758a; padding: 25px; text-align: center; }
  </style>
</head>
<body>
  <header>
    <a class="brand" href="/">Manish <span>Marketer</span> · Admin</a>
    <nav><a class="nav-link" href="/admin/sales">Sales dashboard</a>
      <form method="post" action="/admin/logout"><input type="hidden" name="csrf" value="__CSRF_TOKEN__"><button type="submit">Sign out</button></form>
    </nav>
  </header>
  <main>
    <h1>Store dashboard</h1>
    <p class="intro">You are signed in to the Manish Marketer admin area.</p>
    <div class="notice __NOTICE_CLASS__" id="dashboard-notice" __NOTICE_HIDDEN__>__NOTICE_TEXT__</div>
    <section class="panel">
      <h2>Products</h2>
      <p class="note">Add products or edit the fields below. Changes are saved for shoppers.</p>
      <div class="product-list">__ADMIN_PRODUCTS__</div>
    </section>
    <section class="panel">
      <h2>Add a product</h2>
      <form method="post" enctype="multipart/form-data" action="/admin/products/save">
        <input type="hidden" name="csrf" value="__CSRF_TOKEN__">
        <div class="fields">
          <label>Name<input name="name" maxlength="120" required></label>
          <label>Category<input name="category" maxlength="60" required></label>
          <label>Price (INR)<input name="price" type="number" min="0" max="1000000000" step="0.01" required></label>
          <label>Rating (0–5)<input name="rating" type="number" min="0" max="5" step="0.1" value="5" required></label>
          <label>Emoji<input name="emoji" maxlength="16" value="🛍️" required></label>
          <label>Image background color<input name="color" type="color" value="#e9e5ff" required></label>
          <label>Product image HTTPS URL
            <input class="image-url-input" data-preview="new-product-image-preview" name="image_url" type="url" maxlength="2048" placeholder="https://example.com/product.jpg">
          </label>
          <label>Choose image from device
            <input class="image-file-input" data-preview="new-product-image-preview" name="image_file" type="file" accept="image/png,image/jpeg,image/gif,image/webp">
          </label>
          <span class="image-preview" id="new-product-image-preview"><img alt="Product image preview" hidden><span>🛍️</span></span>
          <label>Description<textarea name="description" maxlength="500"></textarea></label>
        </div>
        <div class="actions"><button type="submit">Add product</button></div>
      </form>
    </section>
  </main>
  <footer>Admin sessions expire after 30 minutes.</footer>
  <script>
    document.querySelectorAll(".image-url-input").forEach(input => {
      const preview = document.getElementById(input.dataset.preview);
      const image = preview.querySelector("img");
      const fallback = preview.querySelector("span");
      const fileInput = input.closest("form").querySelector(".image-file-input");
      let previewUrl = "";
      const updatePreview = () => {
        if (previewUrl) {
          URL.revokeObjectURL(previewUrl);
          previewUrl = "";
        }
        const file = fileInput.files[0];
        if (file) {
          const allowedTypes = ["image/png", "image/jpeg", "image/gif", "image/webp"];
          if (!allowedTypes.includes(file.type)) {
            image.hidden = true;
            fallback.hidden = false;
            return;
          }
          previewUrl = URL.createObjectURL(file);
          image.src = previewUrl;
          image.hidden = false;
          fallback.hidden = true;
          return;
        }
        const value = input.value.trim();
        if (!value) {
          image.hidden = true;
          fallback.hidden = false;
          return;
        }
        try {
          const url = new URL(value);
          if (url.protocol !== "https:") throw new Error("HTTPS image URLs only");
          image.src = url.href;
          image.hidden = false;
          fallback.hidden = true;
        } catch {
          image.hidden = true;
          fallback.hidden = false;
        }
      };
      image.addEventListener("error", () => {
        image.hidden = true;
        fallback.hidden = false;
      });
      input.addEventListener("input", updatePreview);
      fileInput.addEventListener("change", updatePreview);
      updatePreview();
    });
  </script>
</body>
</html>"""


SALES_DASHBOARD_PAGE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#172b4d">
  <title>Sales dashboard | Manish Marketer</title>
  <style>
    * { box-sizing: border-box; }
    body { background: #f5f7fa; color: #172b4d; font: 15px/1.5 "Segoe UI", Arial, sans-serif; margin: 0; }
    header { align-items: center; background: #172b4d; color: white; display: flex; justify-content: space-between; padding: 16px max(5vw, 22px); }
    .brand { color: white; font-size: 20px; font-weight: 800; text-decoration: none; }
    .brand span { color: #ffb547; }
    button, .nav-link { background: #ffb547; border: 0; border-radius: 7px; color: #172b4d; cursor: pointer; font: inherit; font-weight: 700; padding: 9px 13px; text-decoration: none; }
    header nav { align-items: center; display: flex; gap: 10px; }
    main { margin: 35px auto; max-width: 1200px; padding: 0 20px; }
    .intro, .note { color: #68758a; }
    .metrics { display: grid; gap: 14px; grid-template-columns: repeat(3, minmax(0, 1fr)); margin: 22px 0; }
    .metric, .orders-panel { background: white; border: 1px solid #e7ebf0; border-radius: 12px; padding: 18px; }
    .metric-label { color: #68758a; font-size: 13px; }
    .metric-value { display: block; font-size: 27px; font-weight: 800; margin-top: 5px; }
    .table-wrap { overflow-x: auto; }
    table { border-collapse: collapse; min-width: 980px; text-align: left; width: 100%; }
    th, td { border-bottom: 1px solid #e7ebf0; padding: 12px 10px; vertical-align: top; }
    th { color: #52627b; font-size: 12px; text-transform: uppercase; }
    td { font-size: 13px; }
    .muted { color: #68758a; }
    .empty { color: #68758a; padding: 18px 0; }
    .order-status select { display: block; font: inherit; margin-top: 6px; max-width: 150px; }
    .order-status button { cursor: pointer; margin-top: 6px; }
    .dashboard-notice { background: #eaf5ec; border-radius: 8px; color: #315f3b; margin: 15px 0; padding: 11px; }
    .dashboard-notice.error { background: #fff0ef; color: #8a2525; }
    footer { color: #68758a; padding: 25px; text-align: center; }
    @media (max-width: 680px) { .metrics { grid-template-columns: 1fr; } header { align-items: flex-start; gap: 12px; } header nav { flex-wrap: wrap; justify-content: flex-end; } }
  </style>
</head>
<body>
  <header>
    <a class="brand" href="/">Manish <span>Marketer</span> · Sales</a>
    <nav>
      <a class="nav-link" href="/admin">Manage products</a>
      <form method="post" action="/admin/logout"><input type="hidden" name="csrf" value="__CSRF_TOKEN__"><button type="submit">Sign out</button></form>
    </nav>
  </header>
  <main>
    <h1>Sales dashboard</h1>
    <p class="intro">Orders are confirmed when customers submit their details. UPI payments are not verified automatically.</p>
    <div class="dashboard-notice __NOTICE_CLASS__" __NOTICE_HIDDEN__>__NOTICE_TEXT__</div>
    <section class="metrics" aria-label="Sales summary">
      <article class="metric"><span class="metric-label">Active orders</span><strong class="metric-value">__ORDER_COUNT__</strong></article>
      <article class="metric"><span class="metric-label">Order revenue (INR)</span><strong class="metric-value">₹__REVENUE__</strong></article>
      <article class="metric"><span class="metric-label">Average order (INR)</span><strong class="metric-value">₹__AVERAGE_ORDER__</strong></article>
    </section>
    <section class="orders-panel">
      <h2>Recent orders</h2>
      <p class="note">Showing up to 100 newest customer orders.</p>
      <div class="table-wrap">__ORDER_TABLE__</div>
    </section>
  </main>
  <footer>Admin sessions expire after 30 minutes.</footer>
</body>
</html>"""


CUSTOMER_ACCOUNT_PAGE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#172b4d">
  <title>Your account | Manish Marketer</title>
  <style>
    * { box-sizing: border-box; }
    body { background: #f5f7fa; color: #172b4d; font: 15px/1.5 "Segoe UI", Arial, sans-serif; margin: 0; }
    header { align-items: center; background: #172b4d; color: white; display: flex; justify-content: space-between; padding: 16px max(5vw, 22px); }
    .brand { color: white; font-size: 20px; font-weight: 800; text-decoration: none; }
    .brand span { color: #ffb547; }
    button { background: #ffb547; border: 0; border-radius: 7px; color: #172b4d; cursor: pointer; font: inherit; font-weight: 700; padding: 9px 13px; }
    main { background: white; border: 1px solid #e7ebf0; border-radius: 12px; margin: 40px auto; max-width: 1100px; padding: 28px; }
    .muted { color: #68758a; }
    .shop-link { color: #172b4d; display: inline-block; font-weight: 700; margin-right: 18px; }
  </style>
</head>
<body>
  <header>
    <a class="brand" href="/">Manish <span>Marketer</span></a>
    <form method="post" action="/customer/logout"><button type="submit">Sign out</button></form>
  </header>
  <main>
    <h1>Welcome to your account</h1>
    <p>Signed in as <strong>__CUSTOMER_EMAIL__</strong></p>
    <p class="muted">Manage your account and view your order history separately.</p>
    <a class="shop-link" href="/orders">View your orders</a>
    <a class="shop-link" href="/">Continue shopping</a>
  </main>
</body>
</html>"""


CUSTOMER_ORDERS_PAGE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#172b4d">
  <title>Your orders | Manish Marketer</title>
  <style>
    * { box-sizing: border-box; }
    body { background: #f5f7fa; color: #172b4d; font: 15px/1.5 "Segoe UI", Arial, sans-serif; margin: 0; }
    header { align-items: center; background: #172b4d; color: white; display: flex; justify-content: space-between; padding: 16px max(5vw, 22px); }
    .brand { color: white; font-size: 20px; font-weight: 800; text-decoration: none; }
    .brand span { color: #ffb547; }
    button { background: #ffb547; border: 0; border-radius: 7px; color: #172b4d; cursor: pointer; font: inherit; font-weight: 700; padding: 9px 13px; }
    main { background: white; border: 1px solid #e7ebf0; border-radius: 12px; margin: 40px auto; max-width: 1100px; padding: 28px; }
    .muted { color: #68758a; }
    .shop-link { color: #172b4d; display: inline-block; font-weight: 700; margin-right: 18px; }
    .orders { border-collapse: collapse; margin: 22px 0; text-align: left; width: 100%; }
    .orders th, .orders td { border-bottom: 1px solid #e7ebf0; padding: 12px 9px; vertical-align: top; }
    .orders th { color: #52627b; font-size: 12px; text-transform: uppercase; }
    .orders td { font-size: 13px; }
    .order-status { font-weight: 700; text-transform: capitalize; }
    @media (max-width: 700px) { .orders-wrap { overflow-x: auto; } .orders { min-width: 620px; } }
  </style>
</head>
<body>
  <header>
    <a class="brand" href="/">Manish <span>Marketer</span></a>
    <form method="post" action="/customer/logout"><button type="submit">Sign out</button></form>
  </header>
  <main>
    <h1>Your orders</h1>
    <p class="muted">Orders placed while you are signed in appear here. Guest orders are not linked to your account.</p>
    <div class="orders-wrap">__CUSTOMER_ORDERS__</div>
    <a class="shop-link" href="/account">My account</a>
    <a class="shop-link" href="/">Continue shopping</a>
  </main>
</body>
</html>"""


class StoreHandler(BaseHTTPRequestHandler):
    def _route(self):
        request_path = self.path.split("?", 1)[0]
        return "/" + "/".join(part for part in request_path.split("/") if part)

    def _admin_session_token(self):
        for item in self.headers.get("Cookie", "").split(";"):
            name, separator, value = item.strip().partition("=")
            if separator and name == "admin_session":
                return value
        return ""

    def _has_admin_session(self):
        token = self._admin_session_token()
        if not token:
            return False
        now = time.time()
        with ADMIN_AUTH_LOCK:
            expired = [
                session for session, expires_at in ADMIN_SESSIONS.items()
                if expires_at <= now
            ]
            for session in expired:
                del ADMIN_SESSIONS[session]
            return ADMIN_SESSIONS.get(token, 0) > now

    def _customer_session_email(self):
        token = self._session_token("customer_session")
        if not token:
            return ""
        now = time.time()
        with ADMIN_AUTH_LOCK:
            expired = [
                session for session, session_data in CUSTOMER_SESSIONS.items()
                if session_data[1] <= now
            ]
            for session in expired:
                del CUSTOMER_SESSIONS[session]
            session_data = CUSTOMER_SESSIONS.get(token)
            return session_data[0] if session_data and session_data[1] > now else ""

    def _session_token(self, cookie_name):
        for item in self.headers.get("Cookie", "").split(";"):
            name, separator, value = item.strip().partition("=")
            if separator and name == cookie_name:
                return value
        return ""

    def _redirect(self, location, cookie=None):
        self.send_response(303)
        self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _json_response(self, status, data):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _admin_dashboard(self):
        csrf_token = html.escape(self._admin_session_token(), quote=True)
        products = load_products()
        cards = []
        for product in products:
            product_id = product["id"]
            fields = (
                ("Name", "name", product["name"], "text", 120),
                ("Category", "category", product["category"], "text", 60),
                ("Price (INR)", "price", "%.2f" % product["price"], "number", None),
                ("Rating (0–5)", "rating", "%.1f" % product["rating"], "number", None),
                ("Emoji", "emoji", product["emoji"], "text", 16),
                ("Image background color", "color", product["color"], "color", None),
                (
                    "Product image HTTPS URL",
                    "image_url",
                    "" if product_image_path(product["image_url"]) else product["image_url"],
                    "url",
                    2048,
                ),
            )
            input_fields = []
            for label, name, value, input_type, max_length in fields:
                attributes = 'type="%s" name="%s" value="%s"' % (
                    input_type,
                    name,
                    html.escape(str(value), quote=True),
                )
                if name != "image_url":
                    attributes += " required"
                if max_length is not None:
                    attributes += ' maxlength="%s"' % max_length
                if name == "price":
                    attributes += ' min="0" max="1000000000" step="0.01"'
                elif name == "rating":
                    attributes += ' min="0" max="5" step="0.1"'
                elif name == "image_url":
                    preview_id = "product-image-preview-%s" % product_id
                    attributes += ' class="image-url-input" data-preview="%s" placeholder="https://example.com/product.jpg"' % preview_id
                if name == "image_url":
                    preview_markup = (
                        '<span class="image-preview" id="%s" style="background:%s">'
                        '<img src="%s" alt="%s image preview" %s>'
                        '<span %s>%s</span></span>' % (
                            preview_id,
                            html.escape(product["color"], quote=True),
                            html.escape(product["image_url"], quote=True),
                            html.escape(product["name"], quote=True),
                            "" if product["image_url"] else "hidden",
                            "hidden" if product["image_url"] else "",
                            html.escape(product["emoji"]),
                        )
                    )
                    input_fields.append(
                        '<label>%s<input %s></label>'
                        '<label>Choose image from device<input class="image-file-input" '
                        'data-preview="%s" name="image_file" type="file" '
                        'accept="image/png,image/jpeg,image/gif,image/webp"></label>'
                        '<label class="image-remove"><input type="checkbox" name="remove_image" value="1">'
                        'Remove current image</label>%s' % (
                            label,
                            attributes,
                            preview_id,
                            preview_markup,
                        )
                    )
                else:
                    input_fields.append(
                        "<label>%s<input %s></label>" % (label, attributes)
                    )
            description = html.escape(product["description"])
            cards.append(
                '<article class="product-card"><form method="post" enctype="multipart/form-data" action="/admin/products/save">'
                '<input type="hidden" name="csrf" value="%s">'
                '<input type="hidden" name="product_id" value="%s">'
                '<div class="fields">%s'
                '<label>Description<textarea name="description" maxlength="500">%s</textarea></label>'
                '</div><div class="actions"><button type="submit">Save changes</button></div>'
                '</form><form method="post" action="/admin/products/delete" '
                'onsubmit="return confirm(&quot;Delete this product?&quot;)">'
                '<input type="hidden" name="csrf" value="%s">'
                '<input type="hidden" name="product_id" value="%s">'
                '<div class="actions"><button class="delete" type="submit">Delete product</button></div>'
                '</form></article>' % (
                    csrf_token,
                    product_id,
                    "".join(input_fields),
                    description,
                    csrf_token,
                    product_id,
                )
            )
        notice_text = ""
        notice_class = ""
        notice_hidden = "hidden"
        result = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
        if result.get("saved") == ["1"]:
            notice_text, notice_hidden = "Product saved.", ""
        elif result.get("added") == ["1"]:
            notice_text, notice_hidden = "Product added.", ""
        elif result.get("deleted") == ["1"]:
            notice_text, notice_hidden = "Product deleted.", ""
        elif result.get("error"):
            notice_class, notice_hidden = "error", ""
            notice_text = {
                "invalid": "Please check the product fields and try again.",
                "csrf": "Your session expired or the request could not be verified. Reload and retry.",
                "not_found": "That product no longer exists.",
            }.get(result["error"][0], "Unable to update products.")

        page = ADMIN_DASHBOARD_PAGE.replace("__CSRF_TOKEN__", csrf_token)
        page = page.replace("__NOTICE_CLASS__", notice_class)
        page = page.replace("__NOTICE_HIDDEN__", notice_hidden)
        page = page.replace("__NOTICE_TEXT__", html.escape(notice_text))
        page = page.replace(
            "__ADMIN_PRODUCTS__",
            "".join(cards) or '<p class="note">No products yet. Add one below.</p>',
        )
        return page.encode("utf-8")

    def _customer_account_page(self, customer_email):
        return CUSTOMER_ACCOUNT_PAGE.replace(
            "__CUSTOMER_EMAIL__", html.escape(customer_email)
        ).encode("utf-8")

    def _customer_orders_page(self, customer_email):
        orders = load_customer_orders(customer_email)
        if orders:
            order_rows = []
            for order in orders:
                items = json.loads(order["items_json"])
                item_lines = "<br>".join(
                    "%s × %s (₹%.2f)" % (
                        html.escape(item["name"]),
                        item["quantity"],
                        item["line_total"],
                    )
                    for item in items
                )
                order_rows.append(
                    "<tr><td>%s<br><span class=\"muted\">%s</span></td>"
                    "<td>%s</td><td>₹%.2f</td><td class=\"order-status\">%s</td></tr>" % (
                        html.escape(order["order_id"]),
                        html.escape(order["created_at"]),
                        item_lines,
                        order["total"],
                        html.escape(order["order_status"].replace("_", " ")),
                    )
                )
            order_markup = (
                '<table class="orders"><thead><tr><th>Order / date</th>'
                '<th>Items</th><th>Total</th><th>Status</th></tr></thead>'
                '<tbody>%s</tbody></table>' % "".join(order_rows)
            )
        else:
            order_markup = (
                '<p class="muted">No orders are linked to your account yet. '
                'Place an order while signed in to track it here.</p>'
            )
        return CUSTOMER_ORDERS_PAGE.replace(
            "__CUSTOMER_ORDERS__", order_markup
        ).encode("utf-8")

    def _sales_dashboard(self):
        summary, orders = load_sales_data()
        rows = []
        for order in orders:
            items = json.loads(order["items_json"])
            item_lines = "<br>".join(
                "%s × %s (₹%.2f)" % (
                    html.escape(item["name"]),
                    item["quantity"],
                    item["line_total"],
                )
                for item in items
            )
            payment_method = (
                "UPI — unverified"
                if order["payment_method"] == "upi"
                else "Cash on delivery"
            )
            order_status = html.escape(order["order_status"].replace("_", " ").title())
            status_options = "".join(
                '<option value="%s"%s>%s</option>' % (
                    status,
                    " selected" if status == order["order_status"] else "",
                    status.replace("_", " ").title(),
                )
                for status in ORDER_STATUSES
            )
            order_status += (
                '<form method="post" action="/admin/orders/status">'
                '<input type="hidden" name="csrf" value="%s">'
                '<input type="hidden" name="order_id" value="%s">'
                '<select name="status" aria-label="Update status for order %s">%s</select>'
                '<button type="submit">Update</button></form>' % (
                    html.escape(self._admin_session_token(), quote=True),
                    html.escape(order["order_id"], quote=True),
                    html.escape(order["order_id"], quote=True),
                    status_options,
                )
            )
            rows.append(
                "<tr><td>%s<br><span class=\"muted\">%s</span></td>"
                "<td>%s<br>%s<br><span class=\"muted\">%s</span></td>"
                "<td>%s<br>%s<br>%s</td>"
                "<td>%s</td><td>₹%.2f</td><td>%s</td></tr>" % (
                    html.escape(order["order_id"]),
                    html.escape(order["created_at"]),
                    html.escape(order["customer_name"]),
                    html.escape(order["phone"]),
                    html.escape(order["customer_email"] or "Guest checkout"),
                    html.escape(order["address"]),
                    html.escape(order["pincode"]),
                    item_lines,
                    payment_method,
                    order["total"],
                    order_status,
                )
            )
        order_table = (
            '<table><thead><tr><th>Order / date</th><th>Customer / phone / account</th>'
            '<th>Delivery / items</th><th>Payment</th><th>Total</th>'
            '<th>Order status</th></tr></thead><tbody>%s</tbody></table>' % "".join(rows)
            if rows
            else '<p class="empty">No orders yet. Orders will appear here after checkout.</p>'
        )
        page = SALES_DASHBOARD_PAGE.replace(
            "__CSRF_TOKEN__", html.escape(self._admin_session_token(), quote=True)
        )
        page = page.replace("__ORDER_COUNT__", str(summary["order_count"]))
        page = page.replace("__REVENUE__", "%.2f" % summary["revenue"])
        page = page.replace("__AVERAGE_ORDER__", "%.2f" % summary["average_order"])
        page = page.replace("__ORDER_TABLE__", order_table)
        query = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
        notice_text = ""
        notice_class = ""
        if query.get("status_updated") == ["1"]:
            notice_text = "Order status updated."
        elif query.get("error"):
            notice_class = "error"
            notice_text = {
                "csrf": "Your session expired. Reload the page and retry.",
                "invalid_status": "The order status or order ID is invalid.",
                "order_update": "Unable to update that order status.",
            }.get(query["error"][0], "Unable to update the order.")
        page = page.replace("__NOTICE_CLASS__", notice_class)
        page = page.replace("__NOTICE_HIDDEN__", "hidden" if not notice_text else "")
        page = page.replace("__NOTICE_TEXT__", html.escape(notice_text))
        return page.encode("utf-8")

    def _read_form_fields(self):
        content_type = self.headers.get("Content-Type", "")
        is_urlencoded = content_type.lower().startswith(
            "application/x-www-form-urlencoded"
        )
        is_multipart = content_type.lower().startswith("multipart/form-data")
        if not is_urlencoded and not is_multipart:
            self.send_error(415, "Expected form data")
            return None
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400, "Invalid content length")
            return None
        max_request_size = MAX_MULTIPART_SIZE if is_multipart else 8192
        if content_length <= 0:
            self.send_error(400, "Invalid request size")
            return None
        if content_length > max_request_size:
            self.send_error(413, "Request body is too large")
            return None
        body = self.rfile.read(content_length)
        if len(body) != content_length:
            self.send_error(400, "Incomplete request body")
            return None
        if is_urlencoded:
            return parse_qs(
                body.decode("utf-8", errors="replace"), keep_blank_values=True
            )

        try:
            message_headers = (
                b"Content-Type: "
                + content_type.encode("ascii")
                + b"\r\nMIME-Version: 1.0\r\n\r\n"
            )
        except UnicodeEncodeError:
            self.send_error(400, "Invalid multipart content type")
            return None
        message = BytesParser(policy=policy.default).parsebytes(message_headers + body)
        if not message.is_multipart():
            self.send_error(400, "Invalid multipart form data")
            return None

        fields = {}
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if not name:
                continue
            filename = part.get_filename()
            payload = part.get_payload(decode=True) or b""
            if filename is not None:
                if name != "image_file":
                    self.send_error(400, "Unexpected file field")
                    return None
                if not filename:
                    continue
                if "_image_upload" in fields:
                    self.send_error(400, "Only one product image can be uploaded")
                    return None
                if len(payload) > MAX_IMAGE_SIZE:
                    self.send_error(413, "Product image must be no larger than 5 MB")
                    return None
                fields["_image_upload"] = [{
                    "filename": filename,
                    "data": payload,
                }]
                continue
            fields.setdefault(name, []).append(payload.decode("utf-8", errors="replace"))
        return fields

    def do_GET(self):
        route = self._route()
        if route.startswith(PRODUCT_IMAGE_URL_PREFIX):
            image_path = product_image_path(route)
            if image_path is None:
                self.send_error(404, "Not found")
                return
            try:
                with open(image_path, "rb") as image_file:
                    body = image_file.read()
            except FileNotFoundError:
                self.send_error(404, "Not found")
                return
            except OSError:
                self.send_error(500, "Unable to read product image")
                return
            extension = image_path.rsplit(".", 1)[-1]
            content_type = {
                "png": "image/png",
                "jpg": "image/jpeg",
                "gif": "image/gif",
                "webp": "image/webp",
            }[extension]
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "public, max-age=86400")
        elif route == "/api/products":
            body = json.dumps(load_products()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
        elif route == "/admin/login":
            self._redirect("/login")
            return
        elif route in ("/login", "/signup"):
            if self._has_admin_session():
                self._redirect("/admin")
                return
            if self._customer_session_email():
                self._redirect("/account")
                return
            body = LOGIN_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        elif route == "/admin":
            if not self._has_admin_session():
                self._redirect("/login")
                return
            body = self._admin_dashboard()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        elif route == "/admin/sales":
            if not self._has_admin_session():
                self._redirect("/login")
                return
            body = self._sales_dashboard()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        elif route == "/account":
            customer_email = self._customer_session_email()
            if not customer_email:
                self._redirect("/login")
                return
            body = self._customer_account_page(customer_email)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        elif route == "/orders":
            customer_email = self._customer_session_email()
            if not customer_email:
                self._redirect("/login")
                return
            body = self._customer_orders_page(customer_email)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        elif route in ("/", "/index.html"):
            page = PAGE.replace(
                "__PRODUCTS__",
                json.dumps(load_products()).replace("<", "\\u003c"),
            )
            page = page.replace(
                "__MERCHANT_UPI_ID__",
                json.dumps(MERCHANT_UPI_ID).replace("<", "\\u003c"),
            )
            payment_options = (
                '<option value="upi">UPI</option>' if MERCHANT_UPI_ID else ""
            )
            payment_options += (
                '<option value="cash_on_delivery">Cash on delivery</option>'
            )
            page = page.replace("__PAYMENT_OPTIONS__", payment_options)
            customer_email = self._customer_session_email()
            tracking_notice = (
                "This order will be linked to your signed-in account for tracking."
                if customer_email
                else (
                    'Want to track your order in your account? '
                    '<a href="/login">Sign in before placing your order</a>.'
                )
            )
            page = page.replace("__ORDER_TRACKING_NOTICE__", tracking_notice)
            if self._has_admin_session():
                account_link = (
                    '<a class="account-menu" href="/admin" aria-label="Admin dashboard" '
                    'title="Admin dashboard"><svg class="account-icon" viewBox="0 0 24 24" '
                    'fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">'
                    '<circle cx="12" cy="8" r="4"></circle>'
                    '<path d="M4.5 21a7.5 7.5 0 0 1 15 0"></path>'
                    '</svg><span>Admin</span></a>'
                )
            elif self._customer_session_email():
                account_link = (
                    '<a class="account-menu" href="/account" aria-label="Your account" '
                    'title="Your account"><svg class="account-icon" viewBox="0 0 24 24" '
                    'fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">'
                    '<circle cx="12" cy="8" r="4"></circle>'
                    '<path d="M4.5 21a7.5 7.5 0 0 1 15 0"></path>'
                    '</svg><span>Account</span></a>'
                    '<a class="account-menu" href="/orders" aria-label="Your orders" '
                    'title="Your orders"><span>Orders</span></a>'
                    '<form class="logout-form" method="post" action="/customer/logout">'
                    '<button class="account-menu logout-button" type="submit">'
                    'Log out</button></form>'
                )
            else:
                account_link = (
                    '<a class="account-menu" href="/login">Login</a>'
                    '<a class="account-menu" href="/signup">Sign up</a>'
                )
            page = page.replace("__ACCOUNT_LINK__", account_link)
            body = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        else:
            body = b"Not found"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        if route.startswith("/admin") or route in ("/account", "/orders"):
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        route = self._route()
        if route == "/admin/orders/status":
            if not self._has_admin_session():
                self._redirect("/login")
                return
            fields = self._read_form_fields()
            if fields is None:
                return
            session_token = self._admin_session_token()
            submitted_csrf = fields.get("csrf", [""])[0]
            if not hmac.compare_digest(
                submitted_csrf.encode("utf-8"), session_token.encode("utf-8")
            ):
                self._redirect("/admin/sales?error=csrf")
                return
            order_id = fields.get("order_id", [""])[0].strip()
            status = fields.get("status", [""])[0].strip()
            if not re.fullmatch(r"[A-F0-9]{8}", order_id) or status not in ORDER_STATUSES:
                self._redirect("/admin/sales?error=invalid_status")
                return
            try:
                update_order_status(order_id, status)
            except sqlite3.Error as error:
                print("Unable to update order status: %s" % error)
                self._redirect("/admin/sales?error=order_update")
                return
            self._redirect("/admin/sales?status_updated=1")
            return
        if route == "/orders":
            fields = self._read_form_fields()
            if fields is None:
                return
            try:
                order = validate_order(fields)
            except ValueError as error:
                self._json_response(400, {"error": str(error)})
                return
            order_id = secrets.token_hex(4).upper()
            customer_email = self._customer_session_email()
            try:
                save_order(order_id, order, customer_email)
            except sqlite3.Error as error:
                print("Unable to save order: %s" % error)
                self._json_response(
                    500,
                    {"error": "We couldn't save your order. Please try again later."},
                )
                return
            self._json_response(
                201,
                {
                    "order_id": order_id,
                    "payment_method": order["payment_method"],
                    "total": order["total"],
                    "status": "confirmed",
                    "tracking": bool(customer_email),
                },
            )
            return
        if route == "/admin/logout":
            fields = self._read_form_fields()
            if fields is None:
                return
            token = self._admin_session_token()
            submitted_csrf = fields.get("csrf", [""])[0]
            if not token or not hmac.compare_digest(
                submitted_csrf.encode("utf-8"), token.encode("utf-8")
            ):
                self._redirect("/login")
                return
            with ADMIN_AUTH_LOCK:
                ADMIN_SESSIONS.pop(token, None)
            cookie = "admin_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"
            if ADMIN_COOKIE_SECURE:
                cookie += "; Secure"
            self._redirect("/login", cookie)
            return
        if route == "/customer/logout":
            token = self._session_token("customer_session")
            with ADMIN_AUTH_LOCK:
                CUSTOMER_SESSIONS.pop(token, None)
            cookie = "customer_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"
            if ADMIN_COOKIE_SECURE:
                cookie += "; Secure"
            self._redirect("/login", cookie)
            return
        if route in ("/admin/products/save", "/admin/products/delete"):
            if not self._has_admin_session():
                self._redirect("/login")
                return
            fields = self._read_form_fields()
            if fields is None:
                return
            submitted_csrf = fields.get("csrf", [""])[0]
            session_token = self._admin_session_token()
            if not hmac.compare_digest(
                submitted_csrf.encode("utf-8"), session_token.encode("utf-8")
            ):
                self._redirect("/admin?error=csrf")
                return
            uploaded_image_url = ""
            try:
                product_id_text = fields.get("product_id", [""])[0].strip()
                if route == "/admin/products/delete":
                    product_id = int(product_id_text)
                    if product_id <= 0:
                        raise ValueError("Invalid product ID.")
                    with sqlite3.connect(CUSTOMER_DATABASE) as connection:
                        existing_product = connection.execute(
                            "SELECT image_url FROM products WHERE id = ?",
                            (product_id,),
                        ).fetchone()
                        cursor = connection.execute(
                            "DELETE FROM products WHERE id = ?", (product_id,)
                        )
                    if cursor.rowcount == 0:
                        self._redirect("/admin?error=not_found")
                    else:
                        remove_product_image(existing_product[0])
                        self._redirect("/admin?deleted=1")
                    return

                product = validate_product(fields)
                product_id = int(product_id_text) if product_id_text else None
                if product_id is not None and product_id <= 0:
                    raise ValueError("Invalid product ID.")
                previous_image_url = ""
                with sqlite3.connect(CUSTOMER_DATABASE) as connection:
                    if product_id is not None:
                        existing_product = connection.execute(
                            "SELECT image_url FROM products WHERE id = ?",
                            (product_id,),
                        ).fetchone()
                        if existing_product is None:
                            self._redirect("/admin?error=not_found")
                            return
                        previous_image_url = existing_product[0]

                    image_upload = fields.get("_image_upload", [None])[0]
                    if image_upload is not None:
                        uploaded_image_url = save_product_image(image_upload)
                        product["image_url"] = uploaded_image_url
                    elif fields.get("remove_image", [""])[0] == "1":
                        product["image_url"] = ""
                    elif (
                        product_id is not None
                        and product_image_path(previous_image_url)
                        and not product["image_url"]
                    ):
                        product["image_url"] = previous_image_url

                    if product_id is not None:
                        cursor = connection.execute(
                            """UPDATE products
                            SET name = :name, category = :category, price = :price,
                                rating = :rating, emoji = :emoji, color = :color,
                                description = :description, image_url = :image_url
                            WHERE id = :id""",
                            dict(product, id=product_id),
                        )
                        if cursor.rowcount == 0:
                            remove_product_image(uploaded_image_url)
                            self._redirect("/admin?error=not_found")
                            return
                        result = "saved"
                    else:
                        connection.execute(
                            """INSERT INTO products
                            (name, category, price, rating, emoji, color, description, image_url)
                            VALUES (:name, :category, :price, :rating, :emoji, :color, :description, :image_url)""",
                            product,
                        )
                        result = "added"
                if previous_image_url and previous_image_url != product["image_url"]:
                    remove_product_image(previous_image_url)
            except ValueError:
                remove_product_image(uploaded_image_url)
                self._redirect("/admin?error=invalid")
                return
            except OSError:
                remove_product_image(uploaded_image_url)
                print("Unable to save the uploaded product image.")
                self.send_error(500, "Unable to save product image")
                return
            except sqlite3.Error:
                remove_product_image(uploaded_image_url)
                print("Unable to save product due to a database error.")
                self.send_error(500, "Unable to update product")
                return
            self._redirect("/admin?%s=1" % result)
            return
        if route not in ("/login", "/signup"):
            self.send_error(404, "Not found")
            return
        if not self.headers.get("Content-Type", "").lower().startswith(
            "application/x-www-form-urlencoded"
        ):
            self.send_error(415, "Expected form data")
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400, "Invalid content length")
            return
        if content_length <= 0 or content_length > 8192:
            self.send_error(400, "Invalid request size")
            return
        body = self.rfile.read(content_length)
        if len(body) != content_length:
            self.send_error(400, "Incomplete request body")
            return

        client_ip = self.client_address[0]
        now = time.time()
        rate_limited = False
        with ADMIN_AUTH_LOCK:
            attempt_window, attempt_count = ADMIN_LOGIN_ATTEMPTS.get(client_ip, (now, 0))
            if now - attempt_window >= 900:
                attempt_window, attempt_count = now, 0
            if attempt_count >= 5:
                rate_limited = True
            else:
                ADMIN_LOGIN_ATTEMPTS[client_ip] = (attempt_window, attempt_count + 1)
        if rate_limited:
            self.send_error(429, "Too many login attempts. Try again in 15 minutes.")
            return

        fields = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
        submitted_email = fields.get("email", [""])[0].strip().casefold()
        submitted_password = fields.get("password", [""])[0]
        action = fields.get("action", ["login"])[0]
        if (route == "/signup") != (action == "register"):
            self._redirect("/signup" if action == "register" else "/login")
            return
        if not re.fullmatch(r"[^@\s]{1,64}@[^@\s]{1,253}", submitted_email):
            self._redirect(
                ("/signup" if action == "register" else "/login") + "?error=invalid"
            )
            return
        if not 8 <= len(submitted_password) <= 128:
            self._redirect(
                ("/signup" if action == "register" else "/login") + "?error=password"
            )
            return

        admin_email_matches = hmac.compare_digest(
            submitted_email.encode("utf-8"), ADMIN_EMAIL.casefold().encode("utf-8")
        )
        if admin_email_matches:
            if action == "register":
                self._redirect("/signup?error=reserved")
                return
            if not ADMIN_AUTH_CONFIGURED or not hmac.compare_digest(
                submitted_password.encode("utf-8"), ADMIN_PASSWORD.encode("utf-8")
            ):
                self._redirect("/login?error=invalid")
                return
            with ADMIN_AUTH_LOCK:
                ADMIN_LOGIN_ATTEMPTS.pop(client_ip, None)
                token = secrets.token_urlsafe(32)
                ADMIN_SESSIONS[token] = now + ADMIN_SESSION_SECONDS
            cookie = (
                "admin_session=%s; HttpOnly; SameSite=Strict; Path=/; Max-Age=%s"
                % (token, ADMIN_SESSION_SECONDS)
            )
            if ADMIN_COOKIE_SECURE:
                cookie += "; Secure"
            self._redirect("/admin", cookie)
            return

        if action == "register":
            salt, password_hash = hash_customer_password(submitted_password)
            try:
                with sqlite3.connect(CUSTOMER_DATABASE) as connection:
                    connection.execute(
                        "INSERT INTO customers (email, password_salt, password_hash) VALUES (?, ?, ?)",
                        (submitted_email, salt, password_hash),
                    )
            except sqlite3.IntegrityError:
                self._redirect("/signup?error=exists")
                return
            result_path = "/account"
        elif action == "login":
            if not verify_customer_password(submitted_email, submitted_password):
                self._redirect("/login?error=invalid")
                return
            result_path = "/account"
        else:
            self._redirect("/login?error=invalid")
            return

        with ADMIN_AUTH_LOCK:
            ADMIN_LOGIN_ATTEMPTS.pop(client_ip, None)
            token = secrets.token_urlsafe(32)
            CUSTOMER_SESSIONS[token] = (submitted_email, now + CUSTOMER_SESSION_SECONDS)
        cookie = (
            "customer_session=%s; HttpOnly; SameSite=Strict; Path=/; Max-Age=%s"
            % (token, CUSTOMER_SESSION_SECONDS)
        )
        if ADMIN_COOKIE_SECURE:
            cookie += "; Secure"
        if fields.get("remember", [""])[0] != "on":
            cookie = cookie.split("; Max-Age=", 1)[0]
        if action == "register":
            result_path += "?created=1"
        self._redirect(result_path, cookie)

    def log_message(self, format_string, *args):
        print("%s - %s" % (self.address_string(), format_string % args))


def main():
    address = ("127.0.0.1", 8000)
    initialize_customer_database()
    server = ThreadingHTTPServer(address, StoreHandler)
    print("Manish Marketer is running at http://%s:%s" % address)
    if not MERCHANT_UPI_ID:
        print("UPI is not configured. Set MERCHANT_UPI_ID before accepting payments.")
    if not ADMIN_AUTH_CONFIGURED:
        print("Admin login is unavailable. Set ADMIN_EMAIL and ADMIN_PASSWORD to enable it.")
    if not ADMIN_COOKIE_SECURE:
        print("Admin cookies are configured for local HTTP; enable HTTPS and set ADMIN_COOKIE_SECURE=1 for deployment.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down Manish Marketer.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
