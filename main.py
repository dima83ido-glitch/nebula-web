import os
os.environ["PYROGRAM_COMPILER"] = "0"

import asyncio
import json
import uuid
import bcrypt
import aiosqlite
from aiohttp import web
import aiohttp_cors
from pyrogram import Client
from pyrogram.errors import SessionPasswordNeeded, FloodWait

# ========================= CONFIG =========================
PORT = int(os.environ.get("PORT", 8080))
MAX_ACCOUNTS = 50      # Максимум аккаунтов
MAX_CHATS = 20000      # Максимум чатов в одну рассылку

os.makedirs("sessions", exist_ok=True)
os.makedirs("logs", exist_ok=True)
DATABASE = "nebula.db"

pending_auths = {}
active_mailings = {}

# ========================= DATABASE =========================
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

async def create_admin():
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute("SELECT * FROM users WHERE username=?", ("admin",))
        if not await cursor.fetchone():
            hashed = bcrypt.hashpw("orion123".encode(), bcrypt.gensalt()).decode()
            await db.execute(
                "INSERT INTO users (username, password, role) VALUES (?, ?, ?)",
                ("admin", hashed, "admin")
            )
            await db.commit()

# ========================= HELPERS =========================
def json_response(status=True, message="", **kwargs):
    return web.json_response({"status": status, "message": message, **kwargs})

async def get_user(username):
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute("SELECT * FROM users WHERE username=?", (username,))
        return await cursor.fetchone()

# ========================= AUTH =========================
async def register(request):
    try:
        data = await request.json()
        username = data.get("username")
        password = data.get("password")
        async with aiosqlite.connect(DATABASE) as db:
            if await (await db.execute("SELECT * FROM users WHERE username=?", (username,))).fetchone():
                return json_response(False, "Пользователь уже существует")
            hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
            await db.execute("INSERT INTO users (username, password) VALUES (?, ?)", (username, hashed))
            await db.commit()
        return json_response(True, "Регистрация успешна")
    except Exception as e:
        return json_response(False, str(e))

async def login(request):
    try:
        data = await request.json()
        username = data.get("username")
        password = data.get("password")
        remember = data.get("remember", False)

        if not username or not password:
            return json_response(False, "Введите логин и пароль")

        user = await get_user(username)
        if not user:
            return json_response(False, "Пользователь не найден")

        if not bcrypt.checkpw(password.encode(), user[2].encode()):
            return json_response(False, "Неверный пароль")

        token = str(uuid.uuid4())
        if remember:
            async with aiosqlite.connect(DATABASE) as db:
                await db.execute("UPDATE users SET remember_token=? WHERE username=?", (token, username))
                await db.commit()

        return json_response(True, "Успешный вход", token=token, role=user[3])
    except Exception as e:
        print("Login error:", str(e))
        return json_response(False, "Ошибка сервера")

# ========================= ACCOUNT =========================
async def send_code(request):
    try:
        data = await request.json()
        username = data["username"]

        user = await get_user(username)
        if not user:
            return json_response(False, "Пользователь не найден")

        # Проверка лимита
        async with aiosqlite.connect(DATABASE) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM accounts WHERE owner_id=?", (user[0],))
            count = (await cursor.fetchone())[0]

        if count >= MAX_ACCOUNTS:
            return json_response(False, f"Достигнут лимит {MAX_ACCOUNTS} аккаунтов")

        phone = data["phone"]
        api_id = int(data["api_id"])
        api_hash = data["api_hash"]

        session_name = f"sessions/{username}_{phone.replace('+', '')}"
        client = Client(session_name, api_id=api_id, api_hash=api_hash)

        await client.connect()
        sent_code = await client.send_code(phone)

        auth_id = str(uuid.uuid4())
        pending_auths[auth_id] = {
            "client": client, "phone": phone, "api_id": api_id,
            "api_hash": api_hash, "phone_code_hash": sent_code.phone_code_hash,
            "username": username
        }
        return json_response(True, "Код отправлен", auth_id=auth_id)
    except Exception as e:
        return json_response(False, str(e))

async def verify_code(request):
    try:
        data = await request.json()
        auth_id = data["auth_id"]
        code = data["code"]

        auth = pending_auths.get(auth_id)
        if not auth:
            return json_response(False, "Сессия истекла. Начните заново.")

        client = auth["client"]
        try:
            await client.sign_in(auth["phone"], auth["phone_code_hash"], code)
            me = await client.get_me()
            await save_account(auth, me.username)
            await client.disconnect()
            del pending_auths[auth_id]
            return json_response(True, "Аккаунт успешно добавлен")
        except SessionPasswordNeeded:
            return json_response(True, "Требуется 2FA пароль", need_password=True)
        except Exception as e:
            return json_response(False, f"Ошибка: {str(e)}")
    except Exception as e:
        return json_response(False, "Ошибка сервера")

async def verify_password(request):
    try:
        data = await request.json()
        auth_id = data["auth_id"]
        password = data["password"]

        auth = pending_auths.get(auth_id)
        if not auth:
            return json_response(False, "Сессия истекла. Начните заново.")

        client = auth["client"]
        await client.check_password(password)
        me = await client.get_me()
        await save_account(auth, me.username)
        await client.disconnect()
        del pending_auths[auth_id]
        return json_response(True, "Аккаунт успешно добавлен")
    except Exception as e:
        return json_response(False, f"Ошибка 2FA: {str(e)}")

async def save_account(auth, tg_username):
    try:
        user = await get_user(auth["username"])
        if not user:
            return

        async with aiosqlite.connect(DATABASE) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM accounts WHERE owner_id=?", (user[0],))
            count = (await cursor.fetchone())[0]

        if count >= MAX_ACCOUNTS:
            print(f"Лимит {MAX_ACCOUNTS} аккаунтов достигнут!")
            return

        session_name = f"{auth['username']}_{auth['phone'].replace('+', '')}"

        async with aiosqlite.connect(DATABASE) as db:
            await db.execute("""
                INSERT INTO accounts (owner_id, phone, api_id, api_hash, proxy, session_name)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (user[0], auth["phone"], str(auth["api_id"]), auth["api_hash"], None, session_name))
            await db.commit()
            print(f"Аккаунт сохранён: {auth['phone']} | Всего: {count+1}/{MAX_ACCOUNTS}")
    except Exception as e:
        print("Ошибка save_account:", str(e))

async def list_accounts(request):
    try:
        data = await request.json()
        user = await get_user(data["username"])
        if not user:
            return json_response(False, "Пользователь не найден")

        async with aiosqlite.connect(DATABASE) as db:
            cursor = await db.execute("""
                SELECT id, phone FROM accounts 
                WHERE owner_id = ? 
                ORDER BY created_at DESC
            """, (user[0],))
            rows = await cursor.fetchall()

        accounts = [{"id": r[0], "phone": r[1]} for r in rows]
        return json_response(True, accounts=accounts)
    except Exception as e:
        print("list_accounts error:", str(e))
        return json_response(False, str(e))

async def delete_account(request):
    try:
        data = await request.json()
        async with aiosqlite.connect(DATABASE) as db:
            cursor = await db.execute("SELECT session_name FROM accounts WHERE id=?", (data["account_id"],))
            acc = await cursor.fetchone()
            if acc and acc[0]:
                session_file = f"sessions/{acc[0]}.session"
                if os.path.exists(session_file):
                    os.remove(session_file)
            await db.execute("DELETE FROM accounts WHERE id=?", (data["account_id"],))
            await db.commit()
        return json_response(True, "Аккаунт удалён")
    except Exception as e:
        return json_response(False, str(e))

# ========================= GET CHATS (до 20к) =========================
async def get_chats(request):
    try:
        data = await request.json()
        account_id = data["account_id"]
        async with aiosqlite.connect(DATABASE) as db:
            cursor = await db.execute("SELECT * FROM accounts WHERE id=?", (account_id,))
            acc = await cursor.fetchone()
            if not acc:
                return json_response(False, "Аккаунт не найден")

        client = Client(f"sessions/{acc[6]}", api_id=int(acc[3]), api_hash=acc[4])
        await client.connect()

        chats = []
        async for dialog in client.get_dialogs(limit=MAX_CHATS):
            if dialog.chat.type in ["group", "supergroup", "channel", "private"]:
                chats.append({
                    "id": str(dialog.chat.id),
                    "title": dialog.chat.title or dialog.chat.first_name or dialog.chat.username or "Без названия"
                })

        await client.disconnect()
        return json_response(True, chats=chats)
    except Exception as e:
        print("get_chats error:", str(e))
        return json_response(False, str(e))

# ========================= MAILING =========================
async def mailing_worker(mailing_id):
    while True:
        try:
            async with aiosqlite.connect(DATABASE) as db:
                cursor = await db.execute("SELECT * FROM mailings WHERE id=?", (mailing_id,))
                mailing = await cursor.fetchone()
                if not mailing or mailing[9] != "active":
                    await asyncio.sleep(5)
                    continue

                acc_cursor = await db.execute("SELECT * FROM accounts WHERE id=?", (mailing[2],))
                account = await acc_cursor.fetchone()

                client = Client(f"sessions/{account[6]}", api_id=int(account[3]), api_hash=account[4])
                await client.connect()

                chats = json.loads(mailing[8])
                texts = [mailing[4], mailing[5], mailing[6]]
                sent = mailing[10]

                for chat_id in chats:
                    try:
                        text = texts[(sent // 50) % 3] or texts[0]
                        await client.send_message(int(chat_id), text)
                        sent += 1
                        await db.execute("UPDATE mailings SET sent_count=? WHERE id=?", (sent, mailing_id))
                        await db.commit()
                    except FloodWait as e:
                        await asyncio.sleep(e.value)
                    except Exception:
                        pass
                    await asyncio.sleep(mailing[7])

                await client.disconnect()
        except Exception:
            await asyncio.sleep(10)

async def create_mailing(request):
    try:
        data = await request.json()
        user = await get_user(data["username"])
        async with aiosqlite.connect(DATABASE) as db:
            await db.execute("""
                INSERT INTO mailings (owner_id, account_id, name, text1, text2, text3, 
                                    interval_seconds, chats, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'stopped')
            """, (user[0], data["account_id"], data["name"], data.get("text1",""), 
                  data.get("text2",""), data.get("text3",""), 
                  int(data.get("interval", 60)), json.dumps(data.get("chats", []))))
            await db.commit()
        return json_response(True, "Рассылка создана")
    except Exception as e:
        print("create_mailing error:", str(e))
        return json_response(False, str(e))

async def list_mailings(request):
    try:
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
                "id": r[0], "name": r[3], "status": r[9], "sent": r[10], "phone": r[11],
                "text1": r[4], "text2": r[5], "text3": r[6], "interval": r[7],
                "chats": json.loads(r[8]) if r[8] else []
            })
        return json_response(True, mailings=mailings)
    except Exception as e:
        print("list_mailings error:", str(e))
        return json_response(False, str(e))

async def delete_mailing(request):
    try:
        data = await request.json()
        m_id = data["id"]
        async with aiosqlite.connect(DATABASE) as db:
            await db.execute("DELETE FROM mailings WHERE id=?", (m_id,))
            await db.commit()
        if m_id in active_mailings:
            active_mailings[m_id].cancel()
            del active_mailings[m_id]
        return json_response(True)
    except Exception as e:
        return json_response(False, str(e))

async def update_mailing(request):
    try:
        data = await request.json()
        m_id = data["id"]
        async with aiosqlite.connect(DATABASE) as db:
            await db.execute("""
                UPDATE mailings SET name=?, text1=?, text2=?, text3=?, 
                                   interval_seconds=?, chats=?
                WHERE id=?
            """, (data["name"], data.get("text1",""), data.get("text2",""), 
                  data.get("text3",""), int(data.get("interval",60)), 
                  json.dumps(data.get("chats",[])), m_id))
            await db.commit()
        return json_response(True, "Рассылка обновлена")
    except Exception as e:
        return json_response(False, str(e))

async def toggle_mailing(request):
    try:
        data = await request.json()
        m_id = data["id"]
        new_status = data["status"]

        async with aiosqlite.connect(DATABASE) as db:
            await db.execute("UPDATE mailings SET status=? WHERE id=?", (new_status, m_id))
            await db.commit()

        if new_status == "active":
            if m_id not in active_mailings:
                active_mailings[m_id] = asyncio.create_task(mailing_worker(m_id))
        elif m_id in active_mailings:
            active_mailings[m_id].cancel()
            del active_mailings[m_id]

        return json_response(True)
    except Exception as e:
        return json_response(False, str(e))

# ========================= APP =========================
async def create_app():
    await init_db()
    app = web.Application()

    routes = {
        "/register": register,
        "/login": login,
        "/send_code": send_code,
        "/verify_code": verify_code,
        "/verify_password": verify_password,
        "/accounts": list_accounts,
        "/delete_account": delete_account,
        "/get_chats": get_chats,
        "/create_mailing": create_mailing,
        "/mailings": list_mailings,
        "/delete_mailing": delete_mailing,
        "/update_mailing": update_mailing,
        "/toggle_mailing": toggle_mailing,
    }

    for path, handler in routes.items():
        app.router.add_post(path, handler)

    app.router.add_get("/", lambda r: web.FileResponse('index.html'))

    cors = aiohttp_cors.setup(app, defaults={
        "*": aiohttp_cors.ResourceOptions(allow_headers="*", allow_methods="*", allow_credentials=True)
    })
    for route in list(app.router.routes()):
        cors.add(route)

    app.on_startup.append(start_background_tasks)
    return app

async def start_background_tasks(app):
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute("SELECT id FROM mailings WHERE status='active'")
        for row in await cursor.fetchall():
            active_mailings[row[0]] = asyncio.create_task(mailing_worker(row[0]))

if __name__ == "__main__":
    asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
    web.run_app(create_app(), host="0.0.0.0", port=PORT)
