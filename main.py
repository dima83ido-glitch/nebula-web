import os

# 1. СТРОГО ПЕРВОЙ СТРОЧКОЙ: Отключаем встроенную магию Pyrogram для совместимости с Python 3.14
os.environ["PYROGRAM_COMPILER"] = "0"

import asyncio
import json
import uuid
import bcrypt
import aiosqlite

from aiohttp import web
import aiohttp_cors

# 2. Теперь импортируем Pyrogram, когда переменная окружения уже задана
from pyrogram import Client
from pyrogram.errors import (
    SessionPasswordNeeded, 
    FloodWait
)

# =========================
# CONFIG
# =========================

PORT = int(os.environ.get("PORT", 8080))

os.makedirs("sessions", exist_ok=True)
os.makedirs("logs", exist_ok=True)

DATABASE = "nebula.db"

# =========================
# RUNTIME
# =========================

pending_auths = {}
active_mailings = {}

# =========================
# DATABASE
# =========================

async def index(request):
    return web.FileResponse('index.html')


async def create_app():
    await init_db()
    app = web.Application()

    app.router.add_get("/", index)

    # СТРОГО ДОБАВЬ ЭТО: чтобы сайт открывался
    app.router.add_get("/", index) 

    # Проверь, чтобы эти строки были именно такими:
    app.router.add_post("/mailings", list_mailings)
    app.router.add_post("/toggle_mailing", toggle_mailing)
    app.router.add_post("/create_mailing", create_mailing)
    app.router.add_post("/delete_mailing", delete_mailing)
    app.router.add_post("/accounts", list_accounts) # Проверь этот путь!
    
    app.router.add_post("/register", register)
    app.router.add_post("/login", login)
    app.router.add_post("/send_code", send_code)
    app.router.add_post("/verify_code", verify_code)
    app.router.add_post("/verify_password", verify_password)

    cors = aiohttp_cors.setup(app, defaults={
        "*": aiohttp_cors.ResourceOptions(
            allow_headers="*",
            allow_methods="*",
            allow_credentials=True
        )
    })
    for route in list(app.router.routes()):
        cors.add(route)

    app.on_startup.append(start_background_tasks)
    return app

async def init_db():
    async with aiosqlite.connect(DATABASE) as db:

        await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            password TEXT,
            role TEXT DEFAULT 'user',
            remember_token TEXT
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER,
            phone TEXT,
            api_id TEXT,
            api_hash TEXT,
            proxy TEXT,
            session_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS mailings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER,
            account_id INTEGER,
            name TEXT,
            text1 TEXT,
            text2 TEXT,
            text3 TEXT,
            interval_seconds INTEGER,
            chats TEXT,
            status TEXT DEFAULT 'stopped',
            sent_count INTEGER DEFAULT 0
        )
        """)

        await db.commit()

    await create_admin()

# =========================
# CREATE ADMIN
# =========================

async def create_admin():

    async with aiosqlite.connect(DATABASE) as db:

        cursor = await db.execute(
            "SELECT * FROM users WHERE username=?",
            ("admin",)
        )

        user = await cursor.fetchone()

        if not user:

            password = bcrypt.hashpw(
                "orion123".encode(),
                bcrypt.gensalt()
            ).decode()

            await db.execute(
                "INSERT INTO users (username, password, role) VALUES (?, ?, ?)",
                ("admin", password, "admin")
            )

            await db.commit()

async def create_mailing(request):
    try:
        data = await request.json()
        async with aiosqlite.connect(DATABASE) as db:
            await db.execute(
                "INSERT INTO mailings (name, text1, interval_seconds) VALUES (?, ?, ?)",
                (data['name'], data['text'], int(data['interval']))
            )
            await db.commit()
        return web.json_response({"status": True})
    except Exception as e:
        return web.json_response({"status": False, "message": str(e)})

async def get_mailings(request):
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute("SELECT id, name, interval_seconds FROM mailings")
        rows = await cursor.fetchall()
        mailings = [{"id": r[0], "name": r[1], "interval": r[2]} for r in rows]
    return web.json_response({"status": True, "mailings": mailings})

# =========================
# HELPERS
# =========================

def json_response(status=True, message="", **kwargs):
    return web.json_response({
        "status": status,
        "message": message,
        **kwargs
    })

async def get_user(username):

    async with aiosqlite.connect(DATABASE) as db:

        cursor = await db.execute(
            "SELECT * FROM users WHERE username=?",
            (username,)
        )

        return await cursor.fetchone()

# =========================
# AUTH
# =========================

async def register(request):

    try:

        data = await request.json()

        username = data["username"]
        password = data["password"]

        async with aiosqlite.connect(DATABASE) as db:

            cursor = await db.execute(
                "SELECT * FROM users WHERE username=?",
                (username,)
            )

            exists = await cursor.fetchone()

            if exists:
                return json_response(False, "Пользователь уже существует")

            hashed = bcrypt.hashpw(
                password.encode(),
                bcrypt.gensalt()
            ).decode()

            await db.execute(
                "INSERT INTO users (username, password) VALUES (?, ?)",
                (username, hashed)
            )

            await db.commit()

        return json_response(True, "Регистрация успешна")

    except Exception as e:
        return json_response(False, str(e))

async def login(request):

    try:

        data = await request.json()

        username = data["username"]
        password = data["password"]
        remember = data.get("remember", False)

        user = await get_user(username)

        if not user:
            return json_response(False, "Пользователь не найден")

        valid = bcrypt.checkpw(
            password.encode(),
            user[2].encode()
        )

        if not valid:
            return json_response(False, "Неверный пароль")

        token = str(uuid.uuid4())

        if remember:

            async with aiosqlite.connect(DATABASE) as db:

                await db.execute(
                    "UPDATE users SET remember_token=? WHERE username=?",
                    (token, username)
                )

                await db.commit()

        return json_response(
            True,
            "Успешный вход",
            token=token,
            role=user[3]
        )

    except Exception as e:
        return json_response(False, str(e))

# =========================
# ADD ACCOUNT STEP 1
# =========================

async def send_code(request):

    try:

        data = await request.json()

        username = data["username"]

        phone = data["phone"]
        api_id = int(data["api_id"])
        api_hash = data["api_hash"]

        use_proxy = data.get("use_proxy", False)

        proxy = None

        if use_proxy:
            proxy = data.get("proxy")

        session_name = f"sessions/{username}_{phone}"

        client = Client(
            session_name,
            api_id=api_id,
            api_hash=api_hash,
            proxy=proxy
        )

        await client.connect()

        sent_code = await client.send_code(phone)

        auth_id = str(uuid.uuid4())

        pending_auths[auth_id] = {
            "client": client,
            "phone": phone,
            "api_id": api_id,
            "api_hash": api_hash,
            "proxy": proxy,
            "phone_code_hash": sent_code.phone_code_hash,
            "username": username
        }

        return json_response(
            True,
            "Код отправлен",
            auth_id=auth_id
        )

    except Exception as e:
        return json_response(False, str(e))

# =========================
# ADD ACCOUNT STEP 2
# =========================

async def verify_code(request):

    try:

        data = await request.json()

        auth_id = data["auth_id"]
        code = data["code"]

        auth = pending_auths.get(auth_id)

        if not auth:
            return json_response(False, "AUTH NOT FOUND")

        client = auth["client"]

        try:

            await client.sign_in(
                auth["phone"],
                auth["phone_code_hash"],
                code
            )

        except SessionPasswordNeeded:

            return json_response(
                True,
                "Требуется 2FA пароль",
                need_password=True
            )

        me = await client.get_me()

        await save_account(auth, me.username)

        await client.disconnect()

        del pending_auths[auth_id]

        return json_response(True, "Аккаунт подключен")

    except Exception as e:
        return json_response(False, str(e))

# =========================
# ADD ACCOUNT STEP 3
# =========================

async def verify_password(request):

    try:

        data = await request.json()

        auth_id = data["auth_id"]
        password = data["password"]

        auth = pending_auths.get(auth_id)

        if not auth:
            return json_response(False, "AUTH NOT FOUND")

        client = auth["client"]

        await client.check_password(password)

        me = await client.get_me()

        await save_account(auth, me.username)

        await client.disconnect()

        del pending_auths[auth_id]

        return json_response(True, "Аккаунт успешно подключен")

    except Exception as e:
        return json_response(False, str(e))

# =========================
# SAVE ACCOUNT
# =========================

async def save_account(auth, tg_username):

    user = await get_user(auth["username"])

    async with aiosqlite.connect(DATABASE) as db:

        await db.execute("""
        INSERT INTO accounts (
            owner_id,
            phone,
            api_id,
            api_hash,
            proxy,
            session_name
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """, (
            user[0],
            auth["phone"],
            str(auth["api_id"]),
            auth["api_hash"],
            json.dumps(auth["proxy"]),
            f"{auth['username']}_{auth['phone']}"
        ))

        await db.commit()

# =========================
# LIST ACCOUNTS
# =========================

async def list_accounts(request):

    try:

        data = await request.json()

        user = await get_user(data["username"])

        async with aiosqlite.connect(DATABASE) as db:

            cursor = await db.execute("""
            SELECT id, phone, api_id
            FROM accounts
            WHERE owner_id=?
            """, (user[0],))

            rows = await cursor.fetchall()

        accounts = []

        for row in rows:

            accounts.append({
                "id": row[0],
                "phone": row[1],
                "api_id": row[2]
            })

        return json_response(True, accounts=accounts)

    except Exception as e:
        return json_response(False, str(e))

# =========================
# DELETE ACCOUNT
# =========================

async def delete_account(request):

    try:

        data = await request.json()

        account_id = data["account_id"]

        async with aiosqlite.connect(DATABASE) as db:

            cursor = await db.execute("""
            SELECT session_name
            FROM accounts
            WHERE id=?
            """, (account_id,))

            account = await cursor.fetchone()

            if account:

                session_file = f"sessions/{account[0]}.session"

                if os.path.exists(session_file):
                    os.remove(session_file)

            await db.execute(
                "DELETE FROM accounts WHERE id=?",
                (account_id,)
            )

            await db.commit()

        return json_response(True, "Удалено")

    except Exception as e:
        return json_response(False, str(e))

# =========================
# MAILING ENGINE
# =========================

async def mailing_worker(mailing_id):

    while True:

        try:

            async with aiosqlite.connect(DATABASE) as db:

                cursor = await db.execute("""
                SELECT *
                FROM mailings
                WHERE id=?
                """, (mailing_id,))

                mailing = await cursor.fetchone()

                if not mailing:
                    return

                if mailing[9] != "active":
                    await asyncio.sleep(5)
                    continue

                account_id = mailing[2]

                cursor = await db.execute("""
                SELECT *
                FROM accounts
                WHERE id=?
                """, (account_id,))

                account = await cursor.fetchone()

                session_name = f"sessions/{account[6]}"

                proxy = json.loads(account[5]) if account[5] else None

                client = Client(
                    session_name,
                    api_id=int(account[3]),
                    api_hash=account[4],
                    proxy=proxy
                )

                await client.connect()

                chats = json.loads(mailing[8])

                sent = mailing[10]

                texts = [
                    mailing[4],
                    mailing[5],
                    mailing[6]
                ]

                current_text = texts[(sent // 50) % 3]

                for chat_id in chats:

                    try:

                        await client.send_message(
                            chat_id,
                            current_text
                        )

                        sent += 1

                        await db.execute("""
                        UPDATE mailings
                        SET sent_count=?
                        WHERE id=?
                        """, (sent, mailing_id))

                        await db.commit()

                    except FloodWait as e:
                        await asyncio.sleep(e.value)

                    except Exception:
                        pass

                    await asyncio.sleep(mailing[7])

                await client.disconnect()

        except Exception:
            await asyncio.sleep(10)

# =========================
# MAILING ACTIONS
# =========================

async def get_chats(request):
    try:
        data = await request.json()
        account_id = data["account_id"]
        
        async with aiosqlite.connect(DATABASE) as db:
            cursor = await db.execute("SELECT * FROM accounts WHERE id=?", (account_id,))
            acc = await cursor.fetchone()
            
        client = Client(f"sessions/{acc[6]}", api_id=int(acc[3]), api_hash=acc[4])
        await client.connect()
        
        chats = []
        async for dialog in client.get_dialogs():
            chats.append({
                "id": dialog.chat.id,
                "title": dialog.chat.title or dialog.chat.first_name or "Unknown"
            })
            
        await client.disconnect()
        return json_response(True, chats=chats)
    except Exception as e:
        return json_response(False, str(e))

async def create_mailing(request):
    try:
        data = await request.json()
        user = await get_user(data["username"])
        
        async with aiosqlite.connect(DATABASE) as db:
            await db.execute("""
                INSERT INTO mailings (owner_id, account_id, name, text1, text2, text3, interval_seconds, chats)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (user[0], data["account_id"], data["name"], data["text1"], data["text2"], data["text3"], 
                  int(data["interval"]), json.dumps(data["chats"])))
            await db.commit()
        return json_response(True, "Рассылка создана")
    except Exception as e:
        return json_response(False, str(e))

async def list_mailings(request):
    data = await request.json()
    user = await get_user(data["username"])
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute("""
            SELECT m.*, a.phone FROM mailings m 
            JOIN accounts a ON m.account_id = a.id 
            WHERE m.owner_id=?
        """, (user[0],))
        rows = await cursor.fetchall()
    
    mailings = []
    for r in rows:
        mailings.append({
            "id": r[0], "name": r[3], "status": r[9], "sent": r[10], "phone": r[11]
        })
    return json_response(True, mailings=mailings)

async def toggle_mailing(request):
    data = await request.json()
    m_id = data["id"]
    new_status = data["status"]
    async with aiosqlite.connect(DATABASE) as db:
        await db.execute("UPDATE mailings SET status=? WHERE id=?", (new_status, m_id))
        await db.commit()
    
    if new_status == "active":
        active_mailings[m_id] = asyncio.create_task(mailing_worker(m_id))
    elif m_id in active_mailings:
        active_mailings[m_id].cancel()
        del active_mailings[m_id]
        
    return json_response(True)

async def delete_mailing(request):
    data = await request.json()
    m_id = data["id"]
    async with aiosqlite.connect(DATABASE) as db:
        await db.execute("DELETE FROM mailings WHERE id=?", (m_id,))
        await db.commit()
    if m_id in active_mailings:
        active_mailings[m_id].cancel()
    return json_response(True)

# =========================
# EDIT ACCOUNT & MAILING
# =========================

async def update_account(request):
    try:
        data = await request.json()
        acc_id = data["account_id"]
        
        # Данные для обновления
        phone = data.get("phone")
        api_id = data.get("api_id")
        api_hash = data.get("api_hash")
        proxy = data.get("proxy")

        async with aiosqlite.connect(DATABASE) as db:
            await db.execute("""
                UPDATE accounts 
                SET phone=?, api_id=?, api_hash=?, proxy=?
                WHERE id=?
            """, (phone, api_id, api_hash, json.dumps(proxy), acc_id))
            await db.commit()
            
        return json_response(True, "Данные аккаунта обновлены")
    except Exception as e:
        return json_response(False, str(e))

async def update_mailing(request):
    try:
        data = await request.json()
        m_id = data["id"]
        
        async with aiosqlite.connect(DATABASE) as db:
            await db.execute("""
                UPDATE mailings 
                SET name=?, text1=?, text2=?, text3=?, interval_seconds=?
                WHERE id=?
            """, (data["name"], data["text1"], data["text2"], data["text3"], int(data["interval"]), m_id))
            await db.commit()
            
        return json_response(True, "Рассылка обновлена")
    except Exception as e:
        return json_response(False, str(e))

# =========================
# START APP
# =========================

async def start_background_tasks(app):

    async with aiosqlite.connect(DATABASE) as db:

        cursor = await db.execute("""
        SELECT id
        FROM mailings
        WHERE status='active'
        """)

        mailings = await cursor.fetchall()

        for mailing in mailings:

            task = asyncio.create_task(
                mailing_worker(mailing[0])
            )

            active_mailings[mailing[0]] = task

# =========================
# APP
# =========================

async def create_app():

    await init_db()

    app = web.Application()

    app.router.add_post("/mailings", list_mailings)
    app.router.add_post("/toggle_mailing", toggle_mailing)
    app.router.add_post("/create_mailing", create_mailing)
    app.router.add_post("/delete_mailing", delete_mailing)

    app.router.add_post("/register", register)
    app.router.add_post("/login", login)

    app.router.add_post("/send_code", send_code)
    app.router.add_post("/verify_code", verify_code)
    app.router.add_post("/verify_password", verify_password)

    app.router.add_post("/accounts", list_accounts)
    app.router.add_post("/delete_account", delete_account)

    app.router.add_post("/update_account", update_account)
    app.router.add_post("/update_mailing", update_mailing)

    cors = aiohttp_cors.setup(app, defaults={
        "*": aiohttp_cors.ResourceOptions(
            allow_headers="*",
            allow_methods="*",
            allow_credentials=True
        )
    })

    for route in list(app.router.routes()):
        cors.add(route)

    app.on_startup.append(start_background_tasks)

    return app

# =========================
# RUN
# =========================

if __name__ == "__main__":

    asyncio.set_event_loop_policy(
        asyncio.DefaultEventLoopPolicy()
    )

    web.run_app(
        create_app(),
        host="0.0.0.0",
        port=PORT
    )
