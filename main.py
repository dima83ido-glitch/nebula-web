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

PORT = int(os.environ.get("PORT", 8080))
MAX_ACCOUNTS = 50
MAX_CHATS = 20000

os.makedirs("sessions", exist_ok=True)
DATABASE = "nebula.db"

pending_auths = {}
active_mailings = {}

# ====================== HELPERS ======================
def json_response(status=True, message="", **kwargs):
    return web.json_response({"status": status, "message": message, **kwargs})

async def get_user(username):
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute("SELECT * FROM users WHERE username=?", (username,))
        return await cursor.fetchone()

# ====================== ADMIN ======================
async def create_admin():
    async with aiosqlite.connect(DATABASE) as db:
        hashed = bcrypt.hashpw("admin123".encode('utf-8'), bcrypt.gensalt())
        await db.execute("INSERT OR REPLACE INTO users (username, password, role) VALUES (?, ?, 'admin')", ("admin", hashed))
        await db.commit()
    print("✅ АДМИН создан: admin / admin123")

async def init_db():
    async with aiosqlite.connect(DATABASE) as db:
        await db.execute("PRAGMA journal_mode = WAL;")
        await db.execute("PRAGMA busy_timeout = 30000;")

        await db.execute("""CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, username TEXT UNIQUE, password TEXT, role TEXT)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS accounts (id INTEGER PRIMARY KEY, owner_id INTEGER, phone TEXT, api_id TEXT, api_hash TEXT, session_name TEXT)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS mailings (id INTEGER PRIMARY KEY, owner_id INTEGER, account_id INTEGER, name TEXT, text1 TEXT, text2 TEXT, text3 TEXT, interval INTEGER, chats TEXT, status TEXT, sent INTEGER)""")
        await db.commit()
    await create_admin()

# ====================== AUTH ======================
async def register(request):
    try:
        data = await request.json()
        u = data.get("username")
        p = data.get("password")
        async with aiosqlite.connect(DATABASE) as db:
            if await (await db.execute("SELECT 1 FROM users WHERE username=?", (u,))).fetchone():
                return json_response(False, "Пользователь уже существует")
            hashed = bcrypt.hashpw(p.encode(), bcrypt.gensalt()).decode()
            await db.execute("INSERT INTO users (username, password) VALUES (?, ?)", (u, hashed))
            await db.commit()
        return json_response(True, "Регистрация успешна")
    except:
        return json_response(False, "Ошибка регистрации")

async def login(request):
    try:
        data = await request.json()
        username = data.get("username")
        password = data.get("password")

        user = await get_user(username)
        if not user:
            return json_response(False, "Пользователь не найден")
        if not bcrypt.checkpw(password.encode(), user[2].encode()):
            return json_response(False, "Неверный пароль")

        return json_response(True, "Успешный вход")
    except Exception as e:
        print("Login error:", e)
        return json_response(False, "Ошибка сервера")

# ====================== APP ======================
async def create_app():
    await init_db()
    app = web.Application()

    app.router.add_post("/register", register)
    app.router.add_post("/login", login)

    app.router.add_get("/", lambda r: web.FileResponse('index.html'))

    cors = aiohttp_cors.setup(app, defaults={"*": aiohttp_cors.ResourceOptions(allow_headers="*", allow_methods="*", allow_credentials=True)})
    for route in list(app.router.routes()):
        cors.add(route)

    return app

if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=PORT)
