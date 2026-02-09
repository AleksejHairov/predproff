from flask import Flask, render_template, request, redirect, session, send_file, abort
import sqlite3
import hashlib
import secrets
import os
import csv
from datetime import datetime, timedelta
from functools import wraps

# ======================================================
# CONFIG
# ======================================================

app = Flask(__name__)
app.secret_key = os.environ.get("APP_SECRET_KEY", secrets.token_hex(32))

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

DB_NAME = "canteen.db"
SUBSCRIPTION_PRICE = 2500
DEFAULT_DISH_PORTIONS = 20

# ======================================================
# DATABASE
# ======================================================


def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_columns(cursor, table_name, columns):
    existing_columns = {
        row[1] for row in cursor.execute(f'PRAGMA table_info("{table_name}")').fetchall()
    }
    for column_name, column_type in columns.items():
        if column_name not in existing_columns:
            cursor.execute(
                f'ALTER TABLE "{table_name}" ADD COLUMN "{column_name}" {column_type}'
            )


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def add_notification(cursor, message, role=None, user_id=None):
    cursor.execute(
        """
        INSERT INTO notifications (message, role, user_id, created_at, seen)
        VALUES (?, ?, ?, ?, 0)
        """,
        (message, role, user_id, now_iso()),
    )


def get_user_notifications(cursor, user_id, role, unseen_only=True):
    condition = "AND seen=0" if unseen_only else ""
    return cursor.execute(
        f"""
        SELECT *
        FROM notifications
        WHERE (
            (user_id IS NOT NULL AND user_id = ?)
            OR
            (user_id IS NULL AND role = ?)
        )
        {condition}
        ORDER BY id DESC
        LIMIT 20
        """,
        (user_id, role),
    ).fetchall()


def init_db():
    db = get_db()
    c = db.cursor()

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message TEXT NOT NULL,
            role TEXT,
            user_id INTEGER,
            created_at TEXT,
            seen INTEGER DEFAULT 0
        )
        """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            login TEXT UNIQUE,
            password TEXT,
            role TEXT,
            allergies TEXT,
            preferences TEXT
        )
        """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS menu (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            type TEXT,
            price INTEGER,
            allergens TEXT
        )
        """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount INTEGER,
            date TEXT,
            status TEXT,
            payment_type TEXT
        )
        """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            date TEXT,
            status TEXT,
            menu_id INTEGER
        )
        """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS purchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product TEXT,
            quantity INTEGER,
            status TEXT,
            estimated_cost REAL,
            created_by INTEGER,
            created_at TEXT,
            approved_by INTEGER
        )
        """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            valid_until TEXT
        )
        """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            menu_id INTEGER,
            rating INTEGER,
            comment TEXT,
            created_at TEXT
        )
        """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS inventory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE,
            quantity INTEGER DEFAULT 0,
            unit TEXT DEFAULT 'шт',
            low_threshold INTEGER DEFAULT 5
        )
        """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS dish_stock (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            menu_id INTEGER UNIQUE,
            portions INTEGER DEFAULT 0
        )
        """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS issued_meals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cook_id INTEGER,
            menu_id INTEGER,
            quantity INTEGER,
            date TEXT
        )
        """
    )

    # migrations for old DBs
    ensure_columns(c, "notifications", {"user_id": "INTEGER", "seen": "INTEGER DEFAULT 0", "created_at": "TEXT"})
    ensure_columns(c, "users", {"allergies": "TEXT", "preferences": "TEXT"})
    ensure_columns(c, "menu", {"allergens": "TEXT"})
    ensure_columns(c, "payments", {"status": "TEXT", "payment_type": "TEXT"})
    ensure_columns(c, "attendance", {"status": "TEXT", "menu_id": "INTEGER"})
    ensure_columns(
        c,
        "purchases",
        {
            "status": "TEXT",
            "estimated_cost": "REAL DEFAULT 0",
            "created_by": "INTEGER",
            "created_at": "TEXT",
            "approved_by": "INTEGER",
        },
    )
    ensure_columns(c, "inventory", {"unit": "TEXT DEFAULT 'шт'", "low_threshold": "INTEGER DEFAULT 5"})

    # default menu
    if not c.execute("SELECT 1 FROM menu").fetchone():
        c.execute(
            "INSERT INTO menu (name, type, price, allergens) VALUES (?, ?, ?, ?)",
            ("Овсяная каша", "Завтрак", 80, "Глютен, Молоко"),
        )
        c.execute(
            "INSERT INTO menu (name, type, price, allergens) VALUES (?, ?, ?, ?)",
            ("Куриный суп", "Обед", 120, "Сельдерей"),
        )
        c.execute(
            "INSERT INTO menu (name, type, price, allergens) VALUES (?, ?, ?, ?)",
            ("Макароны", "Обед", 100, "Глютен"),
        )

    # default stock for each dish
    menu_ids = c.execute("SELECT id FROM menu").fetchall()
    for row in menu_ids:
        c.execute(
            "INSERT OR IGNORE INTO dish_stock (menu_id, portions) VALUES (?, ?)",
            (row["id"], DEFAULT_DISH_PORTIONS),
        )

    # default product stock
    if not c.execute("SELECT 1 FROM inventory").fetchone():
        c.execute(
            "INSERT INTO inventory (name, quantity, unit, low_threshold) VALUES (?, ?, ?, ?)",
            ("Крупа овсяная", 20, "кг", 5),
        )
        c.execute(
            "INSERT INTO inventory (name, quantity, unit, low_threshold) VALUES (?, ?, ?, ?)",
            ("Курица", 15, "кг", 4),
        )
        c.execute(
            "INSERT INTO inventory (name, quantity, unit, low_threshold) VALUES (?, ?, ?, ?)",
            ("Макароны", 25, "кг", 6),
        )

    db.commit()
    db.close()


# ======================================================
# SECURITY
# ======================================================


def hash_password(password):
    salt = secrets.token_hex(8)
    hashed = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode(),
        salt.encode(),
        100_000,
    ).hex()
    return f"{salt}${hashed}"


def verify_password(password, stored):
    parts = stored.split("$")
    if len(parts) != 2:
        return False
    salt, hashed = parts
    return (
        hashlib.pbkdf2_hmac(
            "sha256",
            password.encode(),
            salt.encode(),
            100_000,
        ).hex()
        == hashed
    )


# ======================================================
# DECORATORS
# ======================================================


def login_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect("/")
        return func(*args, **kwargs)

    return wrapper


def role_required(role):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            if session.get("role") != role:
                abort(403)
            return func(*args, **kwargs)

        return wrapper

    return decorator


# ======================================================
# AUTH
# ======================================================


@app.route("/", methods=["GET", "POST"])
def login():
    error = None

    if request.method == "POST":
        login_value = request.form.get("login", "").strip()
        role = request.form.get("role", "").strip()
        password = request.form.get("password", "")

        db = get_db()
        c = db.cursor()
        user = c.execute(
            "SELECT * FROM users WHERE login = ? AND role = ?",
            (login_value, role),
        ).fetchone()
        db.close()

        if user and verify_password(password, user["password"]):
            session.clear()
            session["user_id"] = user["id"]
            session["role"] = user["role"]
            return redirect("/dashboard")

        error = "Неверный логин, пароль или роль"

    return render_template("login.html", error=error)


@app.route("/register", methods=["GET", "POST"])
def register():
    error = None

    if request.method == "POST":
        login_value = request.form.get("login", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "").strip()
        allergies = request.form.get("allergies", "").strip()
        preferences = request.form.get("preferences", "").strip()

        if not login_value or not password or role not in {"student", "cook", "admin"}:
            error = "Заполните обязательные поля"
        else:
            if role != "student":
                allergies = ""
                preferences = ""

            db = get_db()
            c = db.cursor()
            try:
                c.execute(
                    """
                    INSERT INTO users (login, password, role, allergies, preferences)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        login_value,
                        hash_password(password),
                        role,
                        allergies,
                        preferences,
                    ),
                )
                db.commit()
                db.close()
                return redirect("/")
            except sqlite3.IntegrityError:
                db.close()
                error = "Пользователь уже существует"

    return render_template("register.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


@app.route("/notifications/seen", methods=["POST"])
@login_required
def notifications_seen():
    db = get_db()
    c = db.cursor()
    c.execute(
        """
        UPDATE notifications
        SET seen = 1
        WHERE seen = 0
          AND (
            (user_id IS NOT NULL AND user_id = ?)
            OR
            (user_id IS NULL AND role = ?)
          )
        """,
        (session["user_id"], session["role"]),
    )
    db.commit()
    db.close()

    return redirect(request.referrer or "/dashboard")


# ======================================================
# DASHBOARD
# ======================================================


@app.route("/dashboard")
@login_required
def dashboard():
    return redirect(f"/{session['role']}")


# ======================================================
# STUDENT
# ======================================================


@app.route("/student")
@login_required
@role_required("student")
def student():
    db = get_db()
    c = db.cursor()

    student_data = c.execute(
        "SELECT id, login, allergies, preferences FROM users WHERE id = ?",
        (session["user_id"],),
    ).fetchone()

    menu = c.execute(
        """
        SELECT
            m.id,
            m.name,
            m.type,
            m.price,
            m.allergens,
            COALESCE(ds.portions, 0) AS portions,
            COALESCE(ROUND(AVG(r.rating), 1), 0) AS avg_rating,
            COUNT(r.id) AS review_count
        FROM menu m
        LEFT JOIN dish_stock ds ON ds.menu_id = m.id
        LEFT JOIN reviews r ON r.menu_id = m.id
        GROUP BY m.id
        ORDER BY CASE m.type WHEN 'Завтрак' THEN 0 ELSE 1 END, m.name
        """
    ).fetchall()

    reviews = c.execute(
        """
        SELECT
            r.id,
            r.rating,
            r.comment,
            r.created_at,
            u.login AS author,
            m.name AS dish_name
        FROM reviews r
        JOIN users u ON u.id = r.user_id
        JOIN menu m ON m.id = r.menu_id
        ORDER BY r.id DESC
        LIMIT 15
        """
    ).fetchall()

    today = datetime.now().date().isoformat()
    today_records = c.execute(
        """
        SELECT a.id, a.status, a.date, m.name AS dish_name, m.type AS dish_type
        FROM attendance a
        LEFT JOIN menu m ON m.id = a.menu_id
        WHERE a.user_id = ? AND a.date = ?
        ORDER BY a.id DESC
        """,
        (session["user_id"], today),
    ).fetchall()

    notes = get_user_notifications(c, session["user_id"], session["role"], unseen_only=True)
    db.close()

    return render_template(
        "student.html",
        student=student_data,
        menu=menu,
        reviews=reviews,
        today_records=today_records,
        notes=notes,
        today=today,
    )


@app.route("/student/profile", methods=["POST"])
@login_required
@role_required("student")
def update_student_profile():
    allergies = request.form.get("allergies", "").strip()
    preferences = request.form.get("preferences", "").strip()

    db = get_db()
    c = db.cursor()
    c.execute(
        "UPDATE users SET allergies = ?, preferences = ? WHERE id = ?",
        (allergies, preferences, session["user_id"]),
    )
    add_notification(c, "Профиль с пищевыми особенностями обновлен", user_id=session["user_id"])
    db.commit()
    db.close()
    return redirect("/student")


@app.route("/pay", methods=["POST"])
@login_required
@role_required("student")
def pay():
    try:
        amount = int(request.form.get("amount", "0"))
    except ValueError:
        return "Некорректная сумма", 400

    if amount <= 0:
        return "Сумма должна быть больше нуля", 400

    db = get_db()
    c = db.cursor()
    c.execute(
        """
        INSERT INTO payments (user_id, amount, date, status, payment_type)
        VALUES (?, ?, ?, ?, ?)
        """,
        (session["user_id"], amount, now_iso(), "оплачено", "single"),
    )

    add_notification(
        c,
        f"Поступила оплата от ученика ID {session['user_id']} на сумму {amount} ₽",
        role="admin",
    )
    add_notification(c, "Поступила оплата за питание", role="cook")
    add_notification(c, f"Ваш платеж на {amount} ₽ принят", user_id=session["user_id"])

    db.commit()
    db.close()
    return redirect("/student")


@app.route("/subscribe", methods=["POST"])
@login_required
@role_required("student")
def subscribe():
    valid_until = (datetime.now() + timedelta(days=30)).date().isoformat()

    db = get_db()
    c = db.cursor()
    c.execute(
        "INSERT INTO subscriptions (user_id, valid_until) VALUES (?, ?)",
        (session["user_id"], valid_until),
    )
    c.execute(
        """
        INSERT INTO payments (user_id, amount, date, status, payment_type)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            session["user_id"],
            SUBSCRIPTION_PRICE,
            now_iso(),
            "оплачено",
            "subscription",
        ),
    )

    add_notification(
        c,
        f"Ученик ID {session['user_id']} оформил абонемент до {valid_until}",
        role="admin",
    )
    add_notification(
        c,
        f"Абонемент оформлен до {valid_until}",
        user_id=session["user_id"],
    )

    db.commit()
    db.close()
    return redirect("/student")


@app.route("/eat", methods=["POST"])
@login_required
@role_required("student")
def eat():
    try:
        menu_id = int(request.form.get("menu_id", "0"))
    except ValueError:
        return "Некорректное блюдо", 400

    db = get_db()
    c = db.cursor()

    menu_item = c.execute("SELECT id, name, type FROM menu WHERE id = ?", (menu_id,)).fetchone()
    if not menu_item:
        db.close()
        return "Блюдо не найдено", 404

    today = datetime.now().date().isoformat()
    already_taken = c.execute(
        """
        SELECT 1
        FROM attendance a
        JOIN menu m ON m.id = a.menu_id
        WHERE a.user_id = ?
          AND a.date = ?
          AND a.status = 'получено'
          AND m.type = ?
        """,
        (session["user_id"], today, menu_item["type"]),
    ).fetchone()

    if already_taken:
        db.close()
        return f"{menu_item['type']} уже получен сегодня", 400

    stock = c.execute("SELECT portions FROM dish_stock WHERE menu_id = ?", (menu_id,)).fetchone()
    portions = stock["portions"] if stock else 0
    if portions <= 0:
        shortage_msg = f"Недостаточно готовых блюд: {menu_item['name']}"
        add_notification(c, shortage_msg, role="cook")
        add_notification(c, shortage_msg, role="admin")
        db.commit()
        db.close()
        return "Недостаточно готовых блюд для выдачи", 400

    c.execute("UPDATE dish_stock SET portions = portions - 1 WHERE menu_id = ?", (menu_id,))
    c.execute(
        "INSERT INTO attendance (user_id, date, status, menu_id) VALUES (?, ?, ?, ?)",
        (session["user_id"], today, "получено", menu_id),
    )

    add_notification(
        c,
        f"Ученик ID {session['user_id']} получил {menu_item['name']}",
        role="cook",
    )
    add_notification(
        c,
        f"Отметка о получении питания: {menu_item['name']}",
        user_id=session["user_id"],
    )

    db.commit()
    db.close()
    return redirect("/student")


@app.route("/cancel_eat", methods=["POST"])
@login_required
@role_required("student")
def cancel_eat():
    try:
        attendance_id = int(request.form.get("attendance_id", "0"))
    except ValueError:
        return "Некорректная запись", 400

    db = get_db()
    c = db.cursor()

    record = c.execute(
        """
        SELECT id, status, menu_id
        FROM attendance
        WHERE id = ? AND user_id = ?
        """,
        (attendance_id, session["user_id"]),
    ).fetchone()

    if not record:
        db.close()
        return "Запись не найдена", 404

    if record["status"] != "получено":
        db.close()
        return "Отменить можно только полученное питание", 400

    c.execute("UPDATE attendance SET status = 'отменено' WHERE id = ?", (attendance_id,))

    if record["menu_id"]:
        c.execute("UPDATE dish_stock SET portions = portions + 1 WHERE menu_id = ?", (record["menu_id"],))

    add_notification(c, f"Ученик ID {session['user_id']} отменил отметку о питании", role="cook")
    add_notification(c, "Отметка о питании отменена", user_id=session["user_id"])

    db.commit()
    db.close()
    return redirect("/student")


@app.route("/review", methods=["POST"])
@login_required
@role_required("student")
def leave_review():
    try:
        menu_id = int(request.form.get("menu_id", "0"))
        rating = int(request.form.get("rating", "0"))
    except ValueError:
        return "Некорректные данные отзыва", 400

    comment = request.form.get("comment", "").strip()

    if rating < 1 or rating > 5:
        return "Оценка должна быть от 1 до 5", 400

    db = get_db()
    c = db.cursor()
    menu_item = c.execute("SELECT name FROM menu WHERE id = ?", (menu_id,)).fetchone()
    if not menu_item:
        db.close()
        return "Блюдо не найдено", 404

    c.execute(
        """
        INSERT INTO reviews (user_id, menu_id, rating, comment, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (session["user_id"], menu_id, rating, comment, now_iso()),
    )

    add_notification(c, f"Новый отзыв на блюдо '{menu_item['name']}'", role="cook")
    db.commit()
    db.close()
    return redirect("/student")


# ======================================================
# COOK
# ======================================================


@app.route("/cook", methods=["GET"])
@login_required
@role_required("cook")
def cook():
    db = get_db()
    c = db.cursor()

    notes = get_user_notifications(c, session["user_id"], session["role"], unseen_only=True)

    products = c.execute(
        "SELECT * FROM inventory ORDER BY quantity ASC, name ASC"
    ).fetchall()

    dish_stock = c.execute(
        """
        SELECT ds.menu_id, ds.portions, m.name, m.type
        FROM dish_stock ds
        JOIN menu m ON m.id = ds.menu_id
        ORDER BY CASE m.type WHEN 'Завтрак' THEN 0 ELSE 1 END, m.name
        """
    ).fetchall()

    issued_today = c.execute(
        """
        SELECT m.name, m.type, SUM(im.quantity) AS qty
        FROM issued_meals im
        JOIN menu m ON m.id = im.menu_id
        WHERE im.date = ?
        GROUP BY m.id
        ORDER BY m.type, m.name
        """,
        (datetime.now().date().isoformat(),),
    ).fetchall()

    requests = c.execute(
        "SELECT * FROM purchases ORDER BY id DESC LIMIT 20"
    ).fetchall()

    menu = c.execute(
        "SELECT id, name, type FROM menu ORDER BY type, name"
    ).fetchall()

    db.close()

    return render_template(
        "cook.html",
        notes=notes,
        products=products,
        dish_stock=dish_stock,
        issued_today=issued_today,
        requests=requests,
        menu=menu,
    )


@app.route("/cook/purchase", methods=["POST"])
@login_required
@role_required("cook")
def create_purchase_request():
    product = request.form.get("product", "").strip()

    try:
        quantity = int(request.form.get("quantity", "0"))
        estimated_cost = float(request.form.get("estimated_cost", "0") or 0)
    except ValueError:
        return "Некорректные числовые значения", 400

    if not product or quantity <= 0 or estimated_cost < 0:
        return "Проверьте данные заявки", 400

    db = get_db()
    c = db.cursor()
    c.execute(
        """
        INSERT INTO purchases (product, quantity, status, estimated_cost, created_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            product,
            quantity,
            "ожидает",
            estimated_cost,
            session["user_id"],
            now_iso(),
        ),
    )

    add_notification(
        c,
        f"Новая заявка на закупку: {product}, {quantity} шт.",
        role="admin",
    )
    db.commit()
    db.close()
    return redirect("/cook")


@app.route("/cook/product", methods=["POST"])
@login_required
@role_required("cook")
def update_product_stock():
    name = request.form.get("name", "").strip()
    unit = request.form.get("unit", "шт").strip() or "шт"
    mode = request.form.get("mode", "add")

    try:
        quantity = int(request.form.get("quantity", "0"))
        low_threshold = int(request.form.get("low_threshold", "5"))
    except ValueError:
        return "Некорректные числовые значения", 400

    if not name or quantity < 0 or low_threshold < 0 or mode not in {"add", "set"}:
        return "Проверьте данные склада", 400

    db = get_db()
    c = db.cursor()

    row = c.execute("SELECT * FROM inventory WHERE name = ?", (name,)).fetchone()
    if row:
        new_quantity = row["quantity"] + quantity if mode == "add" else quantity
        if new_quantity < 0:
            db.close()
            return "Остаток не может быть отрицательным", 400

        c.execute(
            """
            UPDATE inventory
            SET quantity = ?, unit = ?, low_threshold = ?
            WHERE id = ?
            """,
            (new_quantity, unit, low_threshold, row["id"]),
        )
    else:
        c.execute(
            """
            INSERT INTO inventory (name, quantity, unit, low_threshold)
            VALUES (?, ?, ?, ?)
            """,
            (name, quantity, unit, low_threshold),
        )
        new_quantity = quantity

    if new_quantity <= low_threshold:
        add_notification(
            c,
            f"Низкий остаток продукта '{name}': {new_quantity} {unit}",
            role="admin",
        )
        add_notification(
            c,
            f"Низкий остаток продукта '{name}': {new_quantity} {unit}",
            role="cook",
        )

    db.commit()
    db.close()
    return redirect("/cook")


@app.route("/cook/dish_stock", methods=["POST"])
@login_required
@role_required("cook")
def set_dish_stock():
    try:
        menu_id = int(request.form.get("menu_id", "0"))
        portions = int(request.form.get("portions", "0"))
    except ValueError:
        return "Некорректные данные блюда", 400

    if portions < 0:
        return "Остаток порций не может быть отрицательным", 400

    db = get_db()
    c = db.cursor()

    menu_item = c.execute("SELECT id, name FROM menu WHERE id = ?", (menu_id,)).fetchone()
    if not menu_item:
        db.close()
        return "Блюдо не найдено", 404

    c.execute(
        """
        INSERT INTO dish_stock (menu_id, portions)
        VALUES (?, ?)
        ON CONFLICT(menu_id) DO UPDATE SET portions = excluded.portions
        """,
        (menu_id, portions),
    )

    if portions == 0:
        add_notification(
            c,
            f"Готовые порции закончились: {menu_item['name']}",
            role="admin",
        )

    db.commit()
    db.close()
    return redirect("/cook")


@app.route("/cook/issue", methods=["POST"])
@login_required
@role_required("cook")
def issue_meal():
    try:
        menu_id = int(request.form.get("menu_id", "0"))
        quantity = int(request.form.get("quantity", "0"))
    except ValueError:
        return "Некорректные данные выдачи", 400

    if quantity <= 0:
        return "Количество должно быть больше 0", 400

    db = get_db()
    c = db.cursor()

    menu_item = c.execute("SELECT id, name FROM menu WHERE id = ?", (menu_id,)).fetchone()
    if not menu_item:
        db.close()
        return "Блюдо не найдено", 404

    stock = c.execute("SELECT portions FROM dish_stock WHERE menu_id = ?", (menu_id,)).fetchone()
    available = stock["portions"] if stock else 0

    if available < quantity:
        msg = f"Недостаточно порций для выдачи '{menu_item['name']}'. Доступно: {available}"
        add_notification(c, msg, role="admin")
        add_notification(c, msg, role="cook")
        db.commit()
        db.close()
        return "Недостаточно порций", 400

    c.execute("UPDATE dish_stock SET portions = portions - ? WHERE menu_id = ?", (quantity, menu_id))
    c.execute(
        """
        INSERT INTO issued_meals (cook_id, menu_id, quantity, date)
        VALUES (?, ?, ?, ?)
        """,
        (session["user_id"], menu_id, quantity, datetime.now().date().isoformat()),
    )

    add_notification(
        c,
        f"Повар выдал {quantity} порц. блюда '{menu_item['name']}'",
        role="admin",
    )

    db.commit()
    db.close()
    return redirect("/cook")


# ======================================================
# ADMIN
# ======================================================


@app.route("/admin")
@login_required
@role_required("admin")
def admin():
    db = get_db()
    c = db.cursor()

    req = c.execute(
        "SELECT * FROM purchases ORDER BY id DESC"
    ).fetchall()

    notes = get_user_notifications(c, session["user_id"], session["role"], unseen_only=True)

    today = datetime.now().date().isoformat()

    payment_stats = c.execute(
        """
        SELECT
            COUNT(*) AS payments_count,
            COALESCE(SUM(amount), 0) AS payments_total,
            COALESCE(SUM(CASE WHEN payment_type = 'subscription' THEN 1 ELSE 0 END), 0) AS subs_count
        FROM payments
        WHERE status = 'оплачено' OR status IS NULL
        """
    ).fetchone()

    attendance_stats = c.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN status = 'получено' THEN 1 ELSE 0 END), 0) AS total_meals,
            COALESCE(SUM(CASE WHEN status = 'получено' AND date = ? THEN 1 ELSE 0 END), 0) AS today_meals
        FROM attendance
        """,
        (today,),
    ).fetchone()

    issued_stats = c.execute(
        "SELECT COALESCE(SUM(quantity), 0) AS issued_total FROM issued_meals"
    ).fetchone()

    purchase_stats = c.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN status = 'одобрено' THEN estimated_cost ELSE 0 END), 0) AS approved_costs,
            COALESCE(SUM(CASE WHEN status = 'ожидает' THEN 1 ELSE 0 END), 0) AS pending_count
        FROM purchases
        """
    ).fetchone()

    attendance_daily = c.execute(
        """
        SELECT date, COUNT(*) AS cnt
        FROM attendance
        WHERE status = 'получено'
        GROUP BY date
        ORDER BY date DESC
        LIMIT 7
        """
    ).fetchall()

    nutrition_by_dish = c.execute(
        """
        SELECT
            m.name,
            m.type,
            COALESCE(SUM(CASE WHEN a.status = 'получено' THEN 1 ELSE 0 END), 0) AS student_marks,
            COALESCE((
                SELECT SUM(im.quantity)
                FROM issued_meals im
                WHERE im.menu_id = m.id
            ), 0) AS cook_issued
        FROM menu m
        LEFT JOIN attendance a ON a.menu_id = m.id
        GROUP BY m.id
        ORDER BY CASE m.type WHEN 'Завтрак' THEN 0 ELSE 1 END, m.name
        """
    ).fetchall()

    db.close()

    return render_template(
        "admin.html",
        req=req,
        notes=notes,
        payment_stats=payment_stats,
        attendance_stats=attendance_stats,
        issued_stats=issued_stats,
        purchase_stats=purchase_stats,
        attendance_daily=attendance_daily,
        nutrition_by_dish=nutrition_by_dish,
    )


@app.route("/approve/<int:request_id>")
@login_required
@role_required("admin")
def approve(request_id):
    db = get_db()
    c = db.cursor()

    row = c.execute(
        "SELECT product, quantity FROM purchases WHERE id = ?",
        (request_id,),
    ).fetchone()
    if not row:
        db.close()
        return "Заявка не найдена", 404

    c.execute(
        "UPDATE purchases SET status = 'одобрено', approved_by = ? WHERE id = ?",
        (session["user_id"], request_id),
    )

    add_notification(
        c,
        f"Заявка на закупку одобрена: {row['product']} ({row['quantity']} шт.)",
        role="cook",
    )
    db.commit()
    db.close()
    return redirect("/admin")


@app.route("/reject/<int:request_id>")
@login_required
@role_required("admin")
def reject(request_id):
    db = get_db()
    c = db.cursor()

    row = c.execute(
        "SELECT product, quantity FROM purchases WHERE id = ?",
        (request_id,),
    ).fetchone()
    if not row:
        db.close()
        return "Заявка не найдена", 404

    c.execute("UPDATE purchases SET status = 'отклонено' WHERE id = ?", (request_id,))
    add_notification(
        c,
        f"Заявка на закупку отклонена: {row['product']} ({row['quantity']} шт.)",
        role="cook",
    )
    db.commit()
    db.close()
    return redirect("/admin")


@app.route("/report")
@login_required
@role_required("admin")
def report():
    db = get_db()
    c = db.cursor()

    payments = c.execute(
        """
        SELECT p.id, p.user_id, p.amount, p.date, p.status, p.payment_type
        FROM payments p
        ORDER BY p.id
        """
    ).fetchall()

    attendance = c.execute(
        """
        SELECT a.id, a.user_id, a.date, a.status, m.name AS dish_name, m.type AS meal_type
        FROM attendance a
        LEFT JOIN menu m ON m.id = a.menu_id
        ORDER BY a.id
        """
    ).fetchall()

    issued = c.execute(
        """
        SELECT i.id, i.cook_id, i.quantity, i.date, m.name AS dish_name, m.type AS meal_type
        FROM issued_meals i
        LEFT JOIN menu m ON m.id = i.menu_id
        ORDER BY i.id
        """
    ).fetchall()

    purchases = c.execute(
        """
        SELECT id, product, quantity, status, estimated_cost, created_at
        FROM purchases
        ORDER BY id
        """
    ).fetchall()

    payments_total = c.execute(
        "SELECT COALESCE(SUM(amount), 0) FROM payments WHERE status = 'оплачено' OR status IS NULL"
    ).fetchone()[0]
    purchase_costs_total = c.execute(
        "SELECT COALESCE(SUM(estimated_cost), 0) FROM purchases WHERE status = 'одобрено'"
    ).fetchone()[0]

    os.makedirs("data", exist_ok=True)
    path = "data/report.csv"

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["section", "date", "user_or_staff_id", "dish_or_product", "meal_type", "quantity", "amount", "status", "comment"])

        writer.writerow(["summary", "", "", "payments_total", "", "", payments_total, "", ""])
        writer.writerow(["summary", "", "", "approved_purchase_costs_total", "", "", purchase_costs_total, "", ""])

        writer.writerow([])
        writer.writerow(["payments"])
        for row in payments:
            writer.writerow([
                "payment",
                row["date"],
                row["user_id"],
                "",
                row["payment_type"],
                "",
                row["amount"],
                row["status"] or "",
                "",
            ])

        writer.writerow([])
        writer.writerow(["attendance_marks"])
        for row in attendance:
            writer.writerow([
                "attendance",
                row["date"],
                row["user_id"],
                row["dish_name"] or "",
                row["meal_type"] or "",
                1,
                "",
                row["status"],
                "",
            ])

        writer.writerow([])
        writer.writerow(["issued_by_cook"])
        for row in issued:
            writer.writerow([
                "issued",
                row["date"],
                row["cook_id"],
                row["dish_name"] or "",
                row["meal_type"] or "",
                row["quantity"],
                "",
                "выдано поваром",
                "",
            ])

        writer.writerow([])
        writer.writerow(["purchase_requests"])
        for row in purchases:
            writer.writerow([
                "purchase",
                row["created_at"] or "",
                "",
                row["product"],
                "",
                row["quantity"],
                row["estimated_cost"] or 0,
                row["status"],
                "",
            ])

    db.close()

    return send_file(path, as_attachment=True)


# ======================================================
# START
# ======================================================


init_db()

if __name__ == "__main__":
    app.run(debug=True)
