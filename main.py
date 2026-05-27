# =========================================
# NEBULA MAILER FIXED FULL BACKEND
# =========================================

import os
import json
import uuid
import asyncio
import bcrypt
import aiosqlite

from aiohttp import web
import aiohttp_cors

from pyrogram import Client
from pyrogram.errors import (
    FloodWait,
    SessionPasswordNeeded,
    AuthKeyUnregistered
)

# =========================================
# CONFIG
# =========================================

PORT = int(os.environ.get("PORT", 8080))

DATABASE = "nebula.db"

MAX_ACCOUNTS = 50

SESSIONS_DIR = "sessions"

os.makedirs(SESSIONS_DIR, exist_ok=True)

pending_auths = {}
active_mailings = {}

# =========================================
# DATABASE
# =========================================

async def init_db():

    async with aiosqlite.connect(DATABASE) as db:

        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=30000")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            password TEXT,
            role TEXT DEFAULT 'user'
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER,
            phone TEXT,
            api_id TEXT,
            api_hash TEXT,
            session_name TEXT
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

# =========================================
# HELPERS
# =========================================

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

# =========================================
# REGISTER
# =========================================

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

            if await cursor.fetchone():

                return json_response(False, "USER EXISTS")

            hashed = bcrypt.hashpw(
                password.encode(),
                bcrypt.gensalt()
            ).decode()

            await db.execute(
                "INSERT INTO users (username, password) VALUES (?, ?)",
                (username, hashed)
            )

            await db.commit()

        return json_response(True, "REGISTERED")

    except Exception as e:

        return json_response(False, str(e))

# =========================================
# LOGIN
# =========================================

async def login(request):

    try:

        data = await request.json()

        username = data["username"]
        password = data["password"]

        user = await get_user(username)

        if not user:

            return json_response(False, "USER NOT FOUND")

        if not bcrypt.checkpw(
            password.encode(),
            user[2].encode()
        ):

            return json_response(False, "INVALID PASSWORD")

        return json_response(
            True,
            "LOGIN SUCCESS",
            role=user[3]
        )

    except Exception as e:

        return json_response(False, str(e))

# =========================================
# TELEGRAM CLIENT
# =========================================

async def get_telegram_client(account):

    session_name = account[5]

    session_path = os.path.join(
        SESSIONS_DIR,
        session_name
    )

    app = Client(
        session_path,
        api_id=int(account[3]),
        api_hash=account[4],
        no_updates=True,
        workers=1
    )

    await app.start()

    await app.get_me()

    return app

# =========================================
# SEND CODE
# =========================================

async def send_code(request):

    try:

        data = await request.json()

        username = data["username"]

        user = await get_user(username)

        if not user:

            return json_response(False, "USER NOT FOUND")

        phone = data["phone"]
        api_id = int(data["api_id"])
        api_hash = data["api_hash"]

        clean_phone = ''.join(
            filter(str.isdigit, phone)
        )

        session_name = f"{username}_{clean_phone}"

        session_path = os.path.join(
            SESSIONS_DIR,
            session_name
        )

        app = Client(
            session_path,
            api_id=api_id,
            api_hash=api_hash,
            no_updates=True,
            workers=1
        )

        await app.connect()

        sent = await app.send_code(phone)

        auth_id = str(uuid.uuid4())

        pending_auths[auth_id] = {
            "client": app,
            "phone": phone,
            "api_id": api_id,
            "api_hash": api_hash,
            "phone_code_hash": sent.phone_code_hash,
            "username": username,
            "session_name": session_name
        }

        return json_response(
            True,
            "CODE SENT",
            auth_id=auth_id
        )

    except Exception as e:

        return json_response(False, str(e))

# =========================================
# VERIFY CODE
# =========================================

async def verify_code(request):

    try:

        data = await request.json()

        auth_id = data["auth_id"]
        code = data["code"]

        auth = pending_auths.get(auth_id)

        if not auth:

            return json_response(False, "AUTH EXPIRED")

        app = auth["client"]

        try:

            await app.sign_in(
                auth["phone"],
                auth["phone_code_hash"],
                code
            )

        except SessionPasswordNeeded:

            return json_response(
                True,
                "2FA REQUIRED",
                need_password=True
            )

        await save_account(auth)

        await app.disconnect()

        del pending_auths[auth_id]

        return json_response(True, "ACCOUNT ADDED")

    except Exception as e:

        return json_response(False, str(e))

# =========================================
# VERIFY PASSWORD
# =========================================

async def verify_password(request):

    try:

        data = await request.json()

        auth_id = data["auth_id"]
        password = data["password"]

        auth = pending_auths.get(auth_id)

        if not auth:

            return json_response(False, "AUTH EXPIRED")

        app = auth["client"]

        await app.check_password(password)

        await save_account(auth)

        await app.disconnect()

        del pending_auths[auth_id]

        return json_response(True, "ACCOUNT ADDED")

    except Exception as e:

        return json_response(False, str(e))

# =========================================
# SAVE ACCOUNT
# =========================================

async def save_account(auth):

    user = await get_user(auth["username"])

    async with aiosqlite.connect(DATABASE) as db:

        await db.execute("""
        INSERT INTO accounts (
            owner_id,
            phone,
            api_id,
            api_hash,
            session_name
        )
        VALUES (?, ?, ?, ?, ?)
        """, (
            user[0],
            auth["phone"],
            str(auth["api_id"]),
            auth["api_hash"],
            auth["session_name"]
        ))

        await db.commit()

# =========================================
# ACCOUNTS
# =========================================

async def list_accounts(request):

    try:

        data = await request.json()

        user = await get_user(data["username"])

        async with aiosqlite.connect(DATABASE) as db:

            cursor = await db.execute("""
            SELECT id, phone
            FROM accounts
            WHERE owner_id=?
            """, (user[0],))

            rows = await cursor.fetchall()

        return json_response(
            True,
            accounts=[
                {
                    "id": r[0],
                    "phone": r[1]
                }
                for r in rows
            ]
        )

    except Exception as e:

        return json_response(False, str(e))

# =========================================
# GET CHATS
# =========================================

async def get_chats(request):

    client = None

    try:

        data = await request.json()

        account_id = data["account_id"]

        async with aiosqlite.connect(DATABASE) as db:

            cursor = await db.execute(
                "SELECT * FROM accounts WHERE id=?",
                (account_id,)
            )

            account = await cursor.fetchone()

        if not account:

            return json_response(False, "ACCOUNT NOT FOUND")

        client = await get_telegram_client(account)

        chats = []

        async for dialog in client.get_dialogs():

            try:

                chat = dialog.chat

                chats.append({
                    "id": chat.id,
                    "title": (
                        chat.title
                        or chat.first_name
                        or "CHAT"
                    )
                })

            except:
                pass

        return json_response(
            True,
            chats=chats
        )

    except Exception as e:

        return json_response(False, str(e))

    finally:

        try:

            if client:
                await client.stop()
        except:
            pass

# =========================================
# CREATE MAILING
# =========================================

async def create_mailing(request):

    try:

        data = await request.json()

        user = await get_user(data["username"])

        async with aiosqlite.connect(DATABASE) as db:

            await db.execute("""
            INSERT INTO mailings (
                owner_id,
                account_id,
                name,
                text1,
                text2,
                text3,
                interval_seconds,
                chats,
                status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'stopped')
            """, (
                user[0],
                data["account_id"],
                data["name"],
                data.get("text1", ""),
                data.get("text2", ""),
                data.get("text3", ""),
                int(data.get("interval", 5)),
                json.dumps(data.get("chats", []))
            ))

            await db.commit()

        return json_response(True, "MAILING CREATED")

    except Exception as e:

        return json_response(False, str(e))

# =========================================
# LIST MAILINGS
# =========================================

async def list_mailings(request):

    try:

        data = await request.json()

        user = await get_user(data["username"])

        async with aiosqlite.connect(DATABASE) as db:

            cursor = await db.execute("""
            SELECT *
            FROM mailings
            WHERE owner_id=?
            """, (user[0],))

            rows = await cursor.fetchall()

        result = []

        for r in rows:

            result.append({
                "id": r[0],
                "account_id": r[2],
                "name": r[3],
                "text1": r[4],
                "text2": r[5],
                "text3": r[6],
                "interval": r[7],
                "chats": json.loads(r[8]),
                "status": r[9],
                "sent": r[10]
            })

        return json_response(
            True,
            mailings=result
        )

    except Exception as e:

        return json_response(False, str(e))

# =========================================
# MAILING WORKER
# =========================================

async def mailing_worker(mailing_id):

    print(f"🚀 MAILING STARTED {mailing_id}")

    client = None
    sent = 0

    try:

        while True:

            async with aiosqlite.connect(DATABASE) as db:

                cursor = await db.execute(
                    "SELECT * FROM mailings WHERE id=?",
                    (mailing_id,)
                )

                mailing = await cursor.fetchone()

            if not mailing:

                return

            if mailing[9] != "active":

                await asyncio.sleep(2)

                continue

            async with aiosqlite.connect(DATABASE) as db:

                cursor = await db.execute(
                    "SELECT * FROM accounts WHERE id=?",
                    (mailing[2],)
                )

                account = await cursor.fetchone()

            if not account:

                await asyncio.sleep(5)
                continue

            if client is None:

                try:

                    client = await get_telegram_client(account)

                    print("✅ CLIENT CONNECTED")

                except Exception as e:

                    print(e)

                    await asyncio.sleep(10)

                    continue

            try:

                chats = json.loads(mailing[8])

            except:

                chats = []

            texts = [
                mailing[4],
                mailing[5],
                mailing[6]
            ]

            texts = [x for x in texts if x.strip()]

            if not texts:

                await asyncio.sleep(5)
                continue

            interval = int(mailing[7] or 5)

            for raw_chat_id in chats:

                try:

                    chat_id = int(raw_chat_id)

                except:

                    continue

                try:

                    text = texts[sent % len(texts)]

                    await client.send_message(
                        chat_id,
                        text
                    )

                    sent += 1

                    print(f"✅ SENT {sent}")

                    async with aiosqlite.connect(DATABASE) as db:

                        await db.execute(
                            "UPDATE mailings SET sent_count=? WHERE id=?",
                            (sent, mailing_id)
                        )

                        await db.commit()

                    await asyncio.sleep(interval)

                except FloodWait as e:

                    print(f"FLOODWAIT {e.value}")

                    await asyncio.sleep(e.value)

                except AuthKeyUnregistered:

                    print("SESSION DEAD")

                    try:
                        await client.stop()
                    except:
                        pass

                    client = None

                    break

                except Exception as e:

                    print(f"SEND ERROR: {e}")

                    await asyncio.sleep(2)

            await asyncio.sleep(10)

    except asyncio.CancelledError:

        print("TASK CANCELLED")

    finally:

        try:

            if client:
                await client.stop()
        except:
            pass

# =========================================
# TOGGLE MAILING
# =========================================

async def toggle_mailing(request):

    try:

        data = await request.json()

        mailing_id = data["id"]
        status = data["status"]

        async with aiosqlite.connect(DATABASE) as db:

            await db.execute(
                "UPDATE mailings SET status=? WHERE id=?",
                (status, mailing_id)
            )

            await db.commit()

        if status == "active":

            if mailing_id not in active_mailings:

                task = asyncio.create_task(
                    mailing_worker(mailing_id)
                )

                active_mailings[mailing_id] = task

        else:

            if mailing_id in active_mailings:

                active_mailings[mailing_id].cancel()

                del active_mailings[mailing_id]

        return json_response(True)

    except Exception as e:

        return json_response(False, str(e))

# =========================================
# AUTO RESTORE
# =========================================

async def start_background_tasks(app):

    async with aiosqlite.connect(DATABASE) as db:

        cursor = await db.execute(
            "SELECT id FROM mailings WHERE status='active'"
        )

        rows = await cursor.fetchall()

    for row in rows:

        mailing_id = row[0]

        active_mailings[mailing_id] = asyncio.create_task(
            mailing_worker(mailing_id)
        )

# =========================================
# APP
# =========================================

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
        "/get_chats": get_chats,

        "/create_mailing": create_mailing,
        "/mailings": list_mailings,
        "/toggle_mailing": toggle_mailing,
    }

    for path, handler in routes.items():

        app.router.add_post(path, handler)

    cors = aiohttp_cors.setup(
        app,
        defaults={
            "*": aiohttp_cors.ResourceOptions(
                allow_headers="*",
                allow_methods="*",
                allow_credentials=True
            )
        }
    )

    for route in list(app.router.routes()):

        cors.add(route)

    app.on_startup.append(start_background_tasks)

    return app

# =========================================
# MAIN
# =========================================

if __name__ == "__main__":

    asyncio.set_event_loop_policy(
        asyncio.DefaultEventLoopPolicy()
    )

    web.run_app(
        create_app(),
        host="0.0.0.0",
        port=PORT
    )
