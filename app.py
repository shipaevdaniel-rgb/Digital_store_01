import os
import uuid
import hmac
import hashlib
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import requests
from dotenv import load_dotenv
from flask import (
    Flask, abort, flash, jsonify, redirect, render_template, request,
    url_for, session
)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename

load_dotenv()

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-only-change-this")
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv(
    "DATABASE_URL", "sqlite:///" + os.path.join(BASE_DIR, "store.db")
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
app.config["UPLOAD_FOLDER"] = os.path.join(BASE_DIR, "static", "uploads")
app.config["ALLOWED_EXTENSIONS"] = {"png", "jpg", "jpeg", "webp", "gif"}
db = SQLAlchemy(app)


def now_utc():
    return datetime.now(timezone.utc)


class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(160), nullable=False)
    description = db.Column(db.Text, nullable=False)
    price_rub = db.Column(db.Numeric(12, 2), nullable=False)
    image = db.Column(db.String(300), nullable=True)
    category = db.Column(db.String(80), nullable=False, default="Цифровые товары")
    active = db.Column(db.Boolean, nullable=False, default=True)
    delivery_text = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=now_utc, nullable=False)


class Order(db.Model):
    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    product_id = db.Column(db.Integer, db.ForeignKey("product.id"), nullable=False)
    product_title = db.Column(db.String(160), nullable=False)
    amount_rub = db.Column(db.Numeric(12, 2), nullable=False)
    customer_email = db.Column(db.String(254), nullable=False)
    status = db.Column(db.String(24), nullable=False, default="pending")
    provider_payment_id = db.Column(db.String(128), unique=True, nullable=True)
    payment_url = db.Column(db.String(1000), nullable=True)
    delivery_text = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=now_utc, nullable=False)
    paid_at = db.Column(db.DateTime(timezone=True), nullable=True)

    product = db.relationship("Product")


def is_admin():
    return session.get("is_admin") is True


def require_admin():
    if not is_admin():
        abort(401)


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in app.config["ALLOWED_EXTENSIONS"]


def money(value):
    return f"{Decimal(value):,.2f}".replace(",", " ").replace(".", ",") + " ₽"


app.jinja_env.filters["money"] = money


def yookassa_configured():
    return bool(os.getenv("YOOKASSA_SHOP_ID") and os.getenv("YOOKASSA_SECRET_KEY"))


def yookassa_request(method, path, payload=None, idempotence_key=None):
    shop_id = os.getenv("YOOKASSA_SHOP_ID", "")
    secret = os.getenv("YOOKASSA_SECRET_KEY", "")
    if not shop_id or not secret:
        raise RuntimeError("Платёжный провайдер не настроен")
    headers = {"Content-Type": "application/json"}
    if idempotence_key:
        headers["Idempotence-Key"] = idempotence_key
    response = requests.request(
        method,
        "https://api.yookassa.ru/v3" + path,
        auth=(shop_id, secret),
        headers=headers,
        json=payload,
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


@app.get("/")
def index():
    products = Product.query.filter_by(active=True).order_by(Product.created_at.desc()).all()
    return render_template("index.html", products=products)


@app.get("/product/<int:product_id>")
def product_detail(product_id):
    product = Product.query.filter_by(id=product_id, active=True).first_or_404()
    return render_template("product.html", product=product)


@app.route("/checkout/<int:product_id>", methods=["GET", "POST"])
def checkout(product_id):
    product = Product.query.filter_by(id=product_id, active=True).first_or_404()
    if request.method == "GET":
        return render_template("checkout.html", product=product)

    email = request.form.get("email", "").strip().lower()
    if len(email) > 254 or "@" not in email or "." not in email.rsplit("@", 1)[-1]:
        flash("Укажи корректный email для доставки товара и/или электронного чека.", "error")
        return render_template("checkout.html", product=product), 400

    order = Order(
        product_id=product.id,
        product_title=product.title,
        amount_rub=product.price_rub,
        customer_email=email,
        status="pending",
        delivery_text=product.delivery_text,
    )
    db.session.add(order)
    db.session.commit()

    if not yookassa_configured():
        # Do not fake a payment link or mark the order as paid.
        return render_template("payment_unconfigured.html", order=order), 503

    payload = {
        "amount": {"value": f"{Decimal(order.amount_rub):.2f}", "currency": "RUB"},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": url_for("order_status", order_id=order.id, _external=True)},
        "description": f"{order.product_title} — заказ {order.id}",
        "metadata": {"order_id": order.id},
        "payment_method_data": {"type": "sbp"},
    }
    # Add receipt data only when merchant has configured the legally required
    # receipt/tax settings and collects all fields required by their fiscal provider.
    try:
        payment = yookassa_request(
            "POST", "/payments", payload,
            idempotence_key=order.id
        )
        order.provider_payment_id = payment["id"]
        order.payment_url = payment.get("confirmation", {}).get("confirmation_url")
        db.session.commit()
    except (requests.RequestException, RuntimeError, KeyError) as exc:
        app.logger.exception("Payment provider error: %s", exc)
        return render_template("payment_error.html", order=order), 502

    if not order.payment_url:
        return render_template("payment_error.html", order=order), 502
    return redirect(order.payment_url)


@app.get("/order/<order_id>")
def order_status(order_id):
    order = Order.query.get_or_404(order_id)
    # The return URL is not proof of payment. Payment status is updated only by
    # the signed/verified provider webhook below.
    return render_template("order_status.html", order=order)


@app.post("/webhooks/yookassa")
def yookassa_webhook():
    # YooKassa notifications should be verified by fetching the payment from
    # YooKassa API and comparing its ID/status/metadata, not trusting the body.
    event = request.get_json(silent=True) or {}
    obj = event.get("object") or {}
    payment_id = obj.get("id")
    if not payment_id:
        return jsonify({"ok": False, "error": "missing payment id"}), 400
    try:
        verified = yookassa_request("GET", f"/payments/{payment_id}")
    except (requests.RequestException, RuntimeError):
        app.logger.exception("Unable to verify payment webhook")
        return jsonify({"ok": False}), 503

    metadata = verified.get("metadata") or {}
    order_id = metadata.get("order_id")
    order = db.session.get(Order, order_id) if order_id else None
    if not order or order.provider_payment_id != verified.get("id"):
        return jsonify({"ok": False, "error": "order mismatch"}), 404

    if verified.get("status") == "succeeded" and verified.get("paid") is True:
        amount = verified.get("amount", {})
        if amount.get("currency") != "RUB" or Decimal(amount.get("value", "0")) != Decimal(order.amount_rub):
            return jsonify({"ok": False, "error": "amount mismatch"}), 400
        if order.status != "paid":
            order.status = "paid"
            order.paid_at = now_utc()
            db.session.commit()
    elif verified.get("status") == "canceled" and order.status == "pending":
        order.status = "canceled"
        db.session.commit()

    return jsonify({"ok": True})


@app.get("/receipt/<order_id>")
def receipt(order_id):
    order = Order.query.get_or_404(order_id)
    # This is an order receipt/confirmation page, not a fiscal cash-register receipt.
    return render_template("receipt.html", order=order)


# ---- Admin ----
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        username = os.getenv("ADMIN_USERNAME", "")
        password_hash = os.getenv("ADMIN_PASSWORD_HASH", "")
        if (username and password_hash
                and request.form.get("username") == username
                and check_password_hash(password_hash, request.form.get("password", ""))):
            session["is_admin"] = True
            return redirect(url_for("admin_products"))
        flash("Неверные учётные данные.", "error")
    return render_template("admin_login.html")


@app.get("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("index"))


@app.get("/admin")
def admin_products():
    require_admin()
    products = Product.query.order_by(Product.created_at.desc()).all()
    orders = Order.query.order_by(Order.created_at.desc()).limit(30).all()
    return render_template("admin.html", products=products, orders=orders)


@app.route("/admin/products/new", methods=["GET", "POST"])
@app.route("/admin/products/<int:product_id>/edit", methods=["GET", "POST"])
def admin_product_form(product_id=None):
    require_admin()
    product = db.session.get(Product, product_id) if product_id else Product()
    if product_id and not product:
        abort(404)

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        category = request.form.get("category", "Цифровые товары").strip()
        delivery_text = request.form.get("delivery_text", "").strip()
        try:
            price = Decimal(request.form.get("price_rub", ""))
        except InvalidOperation:
            price = Decimal("-1")

        if not title or not description or not price.is_finite() or price <= 0:
            flash("Заполни название, описание и корректную цену.", "error")
            return render_template("admin_product_form.html", product=product)

        product.title = title[:160]
        product.description = description
        product.category = category[:80] or "Цифровые товары"
        product.price_rub = price.quantize(Decimal("0.01"))
        product.delivery_text = delivery_text or None
        product.active = request.form.get("active") == "on"

        image = request.files.get("image")
        if image and image.filename:
            if not allowed_file(image.filename):
                flash("Разрешены изображения PNG, JPG, WEBP или GIF.", "error")
                return render_template("admin_product_form.html", product=product)
            filename = secure_filename(image.filename)
            stored = f"{uuid.uuid4().hex}_{filename}"
            image.save(os.path.join(app.config["UPLOAD_FOLDER"], stored))
            product.image = "uploads/" + stored

        db.session.add(product)
        db.session.commit()
        flash("Товар сохранён.", "success")
        return redirect(url_for("admin_products"))

    return render_template("admin_product_form.html", product=product)


@app.post("/admin/products/<int:product_id>/delete")
def admin_product_delete(product_id):
    require_admin()
    product = db.session.get(Product, product_id)
    if not product:
        abort(404)
    # Preserve order history; remove product from the storefront instead of
    # deleting a product referenced by old orders.
    product.active = False
    db.session.commit()
    flash("Товар скрыт из каталога. История заказов сохранена.", "success")
    return redirect(url_for("admin_products"))


@app.cli.command("init-db")
def init_db():
    """Create database tables and a few example products."""
    db.create_all()
    if Product.query.count() == 0:
        db.session.add_all([
            Product(
                title="AI Prompt Sales Kit",
                description="Пакет готовых промптов для офферов, карточек товаров и продающих текстов.",
                price_rub=Decimal("1490.00"),
                category="Промпты",
                active=True,
                delivery_text="Замените этот текст ссылкой на ваш файл или инструкцией по получению продукта."
            ),
            Product(
                title="Нейросети: практический курс",
                description="Практический курс по рабочим сценариям использования нейросетей.",
                price_rub=Decimal("4990.00"),
                category="Обучение",
                active=True,
                delivery_text="Замените этот текст ссылкой на ваш курс."
            ),
            Product(
                title="GPT Workflow Pack",
                description="Подборка шаблонов рабочих процессов и инструкций для AI-инструментов.",
                price_rub=Decimal("990.00"),
                category="Цифровые наборы",
                active=True,
                delivery_text="Замените этот текст ссылкой на ваш набор."
            ),
        ])
        db.session.commit()
    print("Database initialized.")


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5000")), debug=False)
