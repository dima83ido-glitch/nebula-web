import os
import sqlite3

os.environ["PYTHONASYNCIODEBUG"] = "0"
os.environ["SQLITE_BUSY_TIMEOUT"] = "30000"
os.environ["PYROGRAM_COMPILER"] = "0"

sqlite3.enable_shared_cache(True)

import asyncio
import json
import uuid
import bcrypt
import aiosqlite

from aiohttp import web
import aiohttp_cors

from pyrogram import Client
from pyrogram.errors import (
    SessionPasswordNeeded,
    FloodWait
)

# ========================= CONFIG =========================

PORT = int(os.environ.get("PORT", 8080))

DATABASE = "nebula.db"

MAX_ACCOUNTS = 50
MAX_CHATS = 20000

os.makedirs("sessions", exist_ok=True)
os.makedirs("logs", exist_ok=True)

pending_auths = {}
active_mailings = {}

# ========================= DATABASE =========================

async def init_db():

    async with aiosqlite.connect(
        DATABASE,
        timeout=30
    ) as db:

        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA busy_timeout=30000;")
        await db.execute("PRAGMA synchronous=NORMAL;")

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

# ========================= ADMIN =========================

async def create_admin():

    async with aiosqlite.connect(DATABASE) as db:

        cursor = await db.execute(
            "SELECT * FROM users WHERE username=?",
            ("admin",)
        )

        if not await cursor.fetchone():

            hashed = bcrypt.hashpw(
                "orion123".encode(),
                bcrypt.gensalt()
            ).decode()

            await db.execute("""
            INSERT INTO users
            (username, password, role)
            VALUES (?, ?, ?)
            """, (
                "admin",
                hashed,
                "admin"
            ))

            await db.commit()

# ========================= HELPERS =========================

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

# ========================= AUTH =========================

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

        if not bcrypt.checkpw(
            password.encode(),
            user[2].encode()
        ):
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

        print("LOGIN ERROR:", str(e))

        return json_response(False, str(e))

# ========================= CREATE USER =========================

async def create_user(request):

    try:

        data = await request.json()

        username = data.get("username")
        password = data.get("password")

        if not username or not password:
            return json_response(False, "Введите логин и пароль")

        async with aiosqlite.connect(DATABASE) as db:

            cursor = await db.execute(
                "SELECT * FROM users WHERE username=?",
                (username,)
            )

            if await cursor.fetchone():
                return json_response(False, "Пользователь уже существует")

            hashed = bcrypt.hashpw(
                password.encode(),
                bcrypt.gensalt()
            ).decode()

            await db.execute("""
            INSERT INTO users
            (username, password, role)
            VALUES (?, ?, 'user')
            """, (
                username,
                hashed
            ))

            await db.commit()

        return json_response(True)

    except Exception as e:

        print("CREATE USER ERROR:", str(e))

        return json_response(False, str(e))

# ========================= SEND CODE =========================

async def send_code(request):

    try:

        data = await request.json()

        username = data["username"]

        user = await get_user(username)

        if not user:
            return json_response(False, "Пользователь не найден")

        phone = data["phone"].strip()

        api_id = int(data["api_id"])
        api_hash = data["api_hash"].strip()

        proxy = data.get("proxy")

        clean_phone = ''.join(filter(str.isdigit, phone))

        session_name = f"{username}_{clean_phone}"

        client = Client(
            f"sessions/{session_name}",
            api_id=api_id,
            api_hash=api_hash,
            proxy=proxy if proxy else None,
            no_updates=True,
            workers=1
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
            "username": username,
            "session_name": session_name
        }

        return json_response(
            True,
            auth_id=auth_id
        )

    except Exception as e:

        print("SEND CODE ERROR:", str(e))

        return json_response(False, str(e))

# ========================= VERIFY CODE =========================

async def verify_code(request):

    try:

        data = await request.json()

        auth_id = data["auth_id"]
        code = data["code"]

        auth = pending_auths.get(auth_id)

        if not auth:
            return json_response(False, "Сессия истекла")

        client = auth["client"]

        try:

            await client.sign_in(
                auth["phone"],
                auth["phone_code_hash"],
                code
            )

            me = await client.get_me()

            await save_account(auth)

            await client.disconnect()

            del pending_auths[auth_id]

            return json_response(True)

        except SessionPasswordNeeded:

            return json_response(
                True,
                need_password=True
            )

    except Exception as e:

        print("VERIFY CODE ERROR:", str(e))

        return json_response(False, str(e))

# ========================= VERIFY PASSWORD =========================

async def verify_password(request):

    try:

        data = await request.json()

        auth_id = data["auth_id"]
        password = data["password"]

        auth = pending_auths.get(auth_id)

        if not auth:
            return json_response(False, "Сессия истекла")

        client = auth["client"]

        await client.check_password(password)

        await save_account(auth)

        await client.disconnect()

        del pending_auths[auth_id]

        return json_response(True)

    except Exception as e:

        print("VERIFY PASSWORD ERROR:", str(e))

        return json_response(False, str(e))

# ========================= SAVE ACCOUNT =========================

async def save_account(auth):

    try:

        user = await get_user(auth["username"])

        async with aiosqlite.connect(DATABASE) as db:

            await db.execute("""
            INSERT INTO accounts
            (
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
                auth["proxy"],
                auth["session_name"]
            ))

            await db.commit()

    except Exception as e:

        print("SAVE ACCOUNT ERROR:", str(e))
