import asyncio
import os
import json
import uuid
import bcrypt
import aiosqlite
from aiohttp import web
import aiohttp_cors
from pyrogram import Client
from pyrogram.errors import SessionPasswordNeeded, FloodWait
import logging

logging.basicConfig(level=logging.INFO)

PORT = int(os.environ.get("PORT", 8080))
os.makedirs("sessions", exist_ok=True)
DATABASE = "nebula.db"

pending_auths = {}
active_mailings = {}

# ====================== DB ======================
async def init_db():
    async with aiosqlite.connect(DATABASE) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                remember_token TEXT
            );
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER,
                phone TEXT,
                api_id TEXT,
                api_hash TEXT,
                proxy TEXT,
                session_name TEXT,
                tg_username TEXT
            );
            CREATE TABLE IF NOT EXISTS mailings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER,
                account_id INTEGER,
                name TEXT,
                text1 TEXT,
                text2 TEXT,
                text3 TEXT,
                interval_seconds INTEGER DEFAULT 60,
                chats TEXT,
                status TEXT DEFAULT 'stopped',
                sent_count INTEGER DEFAULT 0
            );
        """)
        await db.commit()

# ====================== HELPERS ======================
def json_response(status=True, message="", **kwargs):
    return web.json_response({"status": status, "message": message, **kwargs})

async def get_user(username):
    async with aiosqlite.connect(DATABASE) as db:
        return await (await db.execute("SELECT * FROM users WHERE username=?", (username,))).fetchone()

# ====================== AUTH ======================
async def register(request):
    try:
        data = await request.json()
        username = data["username"]
        password = data["password"]
        hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        async with aiosqlite.connect(DATABASE) as db:
            await db.execute("INSERT INTO users (username, password) VALUES (?, ?)", (username, hashed))
            await db.commit()
        return json_response(True, "Пользователь создан")
    except Exception as e:
        return json_response(False, "Пользователь уже существует или ошибка")

async def login(request):
    try:
        data = await request.json()
        user = await get_user(data["username"])
        if not user or not bcrypt.checkpw(data["password"].encode(), user[2].encode()):
            return json_response(False, "Неверный логин или пароль")
        return json_response(True, "Успешный вход", username=data["username"])
    except Exception as e:
        return json_response(False, str(e))

# ====================== ACCOUNTS ======================
async def send_code(request):
    try:
        data = await request.json()
        username = data["username"]
        phone = data["phone"]
        api_id = int(data["api_id"])
        api_hash = data["api_hash"]
        use_proxy = data.get("use_proxy", False)
        proxy = data.get("proxy") if use_proxy else None

        session_name = f"sessions/{username}_{phone.replace('+', '')}"
        client = Client(session_name, api_id=api_id, api_hash=api_hash, proxy=proxy)

        await client.connect()
        sent = await client.send_code(phone)

        auth_id = str(uuid.uuid4())
        pending_auths[auth_id] = {
            "client": client, "phone": phone, "api_id": api_id, "api_hash": api_hash,
            "proxy": proxy, "phone_code_hash": sent.phone_code_hash, "username": username
        }
        return json_response(True, "Код отправлен", auth_id=auth_id)
    except Exception as e:
        return json_response(False, str(e))

async def verify_code(request):
    try:
        data = await request.json()
        auth = pending_auths.get(data["auth_id"])
        if not auth: return json_response(False, "Сессия истекла")
        client = auth["client"]
        try:
            await client.sign_in(auth["phone"], auth["phone_code_hash"], data["code"])
        except SessionPasswordNeeded:
            return json_response(True, "Нужен 2FA", need_password=True)
        except Exception as e:
            return json_response(False, str(e))
        me = await client.get_me()
        await save_account(auth, me.username or me.first_name)
        await client.disconnect()
        del pending_auths[data["auth_id"]]
        return json_response(True, "Аккаунт успешно подключен")
    except Exception as e:
        return json_response(False, str(e))

async def verify_password(request):
    try:
        data = await request.json()
        auth = pending_auths.get(data["auth_id"])
        if not auth: return json_response(False, "Сессия истекла")
        await auth["client"].check_password(data["password"])
        me = await auth["client"].get_me()
        await save_account(auth, me.username or me.first_name)
        await auth["client"].disconnect()
        del pending_auths[data["auth_id"]]
        return json_response(True, "Аккаунт успешно подключен")
    except Exception as e:
        return json_response(False, str(e))

async def save_account(auth, tg_username):
    user = await get_user(auth["username"])
    async with aiosqlite.connect(DATABASE) as db:
        await db.execute("""
            INSERT INTO accounts (owner_id, phone, api_id, api_hash, proxy, session_name, tg_username)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (user[0], auth["phone"], str(auth["api_id"]), auth["api_hash"],
              json.dumps(auth["proxy"]) if auth["proxy"] else None,
              f"{auth['username']}_{auth['phone'].replace('+','')}", tg_username))
        await db.commit()

async def list_accounts(request):
    try:
        data = await request.json()
        user = await get_user(data["username"])
        async with aiosqlite.connect(DATABASE) as db:
            rows = await (await db.execute("SELECT id, phone, tg_username FROM accounts WHERE owner_id=?", (user[0],))).fetchall()
        return json_response(True, accounts=[{"id":r[0], "phone":r[1], "name":r[2] or r[1]} for r in rows])
    except Exception as e:
        return json_response(False, str(e))

async def delete_account(request):
    try:
        data = await request.json()
        async with aiosqlite.connect(DATABASE) as db:
            row = await (await db.execute("SELECT session_name FROM accounts WHERE id=?", (data["account_id"],))).fetchone()
            if row and row[0]:
                for ext in ["", "-journal"]:
                    p = f"sessions/{row[0]}{ext}.session"
                    if os.path.exists(p): os.remove(p)
            await db.execute("DELETE FROM accounts WHERE id=?", (data["account_id"],))
            await db.commit()
        return json_response(True, "Аккаунт удалён")
    except Exception as e:
        return json_response(False, str(e))

# ====================== CHATS ======================
async def get_chats(request):
    try:
        data = await request.json()
        async with aiosqlite.connect(DATABASE) as db:
            acc = await (await db.execute("SELECT * FROM accounts WHERE id=?", (data["account_id"],))).fetchone()
        if not acc: return json_response(False, "Аккаунт не найден")
        client = Client(f"sessions/{acc[6]}", api_id=int(acc[3]), api_hash=acc[4], proxy=json.loads(acc[5]) if acc[5] else None)
        await client.connect()
        dialogs = await client.get_dialogs(limit=300)
        chats = [{"id": str(d.chat.id), "name": d.chat.title or d.chat.first_name or str(d.chat.id)} for d in dialogs]
        await client.disconnect()
        return json_response(True, chats=chats)
    except Exception as e:
        return json_response(False, str(e))

# ====================== MAILINGS ======================
async def create_mailing(request):
    try:
        data = await request.json()
        user = await get_user(data["username"])
        async with aiosqlite.connect(DATABASE) as db:
            await db.execute("""
                INSERT INTO mailings (owner_id, account_id, name, text1, text2, text3, interval_seconds, chats)
                VALUES (?,?,?,?,?,?,?,?)
            """, (user[0], data["account_id"], data["name"], data["text1"], data["text2"], data["text3"], int(data["interval"]), json.dumps(data["chats"])))
            await db.commit()
        return json_response(True, "Рассылка создана")
    except Exception as e:
        return json_response(False, str(e))

async def list_mailings(request):
    try:
        data = await request.json()
        user = await get_user(data["username"])
        async with aiosqlite.connect(DATABASE) as db:
            rows = await (await db.execute("""
                SELECT m.id, m.name, a.tg_username, m.status, m.sent_count 
                FROM mailings m JOIN accounts a ON m.account_id = a.id WHERE m.owner_id=?
            """, (user[0],))).fetchall()
        return json_response(True, mailings=[{"id":r[0],"name":r[1],"account":r[2],"status":r[3],"sent":r[4]} for r in rows])
    except Exception as e:
        return json_response(False, str(e))

async def toggle_mailing(request):
    try:
        data = await request.json()
        status = "active" if data["action"] == "start" else "stopped"
        async with aiosqlite.connect(DATABASE) as db:
            await db.execute("UPDATE mailings SET status=? WHERE id=?", (status, data["mailing_id"]))
            await db.commit()
        return json_response(True, "Статус изменён")
    except Exception as e:
        return json_response(False, str(e))

async def delete_mailing(request):
    try:
        data = await request.json()
        async with aiosqlite.connect(DATABASE) as db:
            await db.execute("DELETE FROM mailings WHERE id=?", (data["mailing_id"],))
            await db.commit()
        return json_response(True, "Удалено")
    except Exception as e:
        return json_response(False, str(e))

# ====================== WORKER ======================
async def mailing_worker(mailing_id):
    while True:
        try:
            async with aiosqlite.connect(DATABASE) as db:
                mailing = await (await db.execute("SELECT * FROM mailings WHERE id=?", (mailing_id,))).fetchone()
                if not mailing or mailing[9] != "active":
                    await asyncio.sleep(5)
                    continue
                acc = await (await db.execute("SELECT * FROM accounts WHERE id=?", (mailing[2],))).fetchone()
                client = Client(f"sessions/{acc[6]}", api_id=int(acc[3]), api_hash=acc[4], proxy=json.loads(acc[5]) if acc[5] else None)
                await client.connect()
                chats = json.loads(mailing[8])
                texts = [mailing[4], mailing[5], mailing[6]]
                sent = mailing[10]
                for chat_id in chats:
                    try:
                        text = texts[(sent // 50) % 3]
                        await client.send_message(int(chat_id), text)
                        sent += 1
                        await db.execute("UPDATE mailings SET sent_count=? WHERE id=?", (sent, mailing_id))
                        await db.commit()
                    except FloodWait as e:
                        await asyncio.sleep(e.value)
                    except:
                        pass
                    await asyncio.sleep(mailing[7])
                await client.disconnect()
        except:
            await asyncio.sleep(10)

async def start_background_tasks(app):
    async with aiosqlite.connect(DATABASE) as db:
        for row in await (await db.execute("SELECT id FROM mailings WHERE status='active'")).fetchall():
            asyncio.create_task(mailing_worker(row[0]))

# ====================== APP ======================
async def create_app():
    await init_db()
    app = web.Application()
    app.router.add_post('/register', register)
    app.router.add_post('/login', login)
    app.router.add_post('/send_code', send_code)
    app.router.add_post('/verify_code', verify_code)
    app.router.add_post('/verify_password', verify_password)
    app.router.add_post('/accounts', list_accounts)
    app.router.add_post('/delete_account', delete_account)
    app.router.add_post('/get_chats', get_chats)
    app.router.add_post('/create_mailing', create_mailing)
    app.router.add_post('/mailings', list_mailings)
    app.router.add_post('/toggle_mailing', toggle_mailing)
    app.router.add_post('/delete_mailing', delete_mailing)

    cors = aiohttp_cors.setup(app, defaults={"*": aiohttp_cors.ResourceOptions(allow_credentials=True, allow_headers="*", allow_methods="*")})
    for route in list(app.router.routes()):
        cors.add(route)

    app.on_startup.append(start_background_tasks)
    return app

if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=PORT)
