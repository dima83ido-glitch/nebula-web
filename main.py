import os
import json
import uuid
import bcrypt
import asyncio
import aiosqlite

from aiohttp import web
import aiohttp_cors
from pyrogram import Client
from pyrogram.errors import SessionPasswordNeeded, FloodWait

PORT = int(os.environ.get("PORT", 8080))
DB = "nebula.db"

os.makedirs("sessions", exist_ok=True)

# =========================
# RUNTIME STORAGE
# =========================

clients = {}          # account_id -> Client
mailing_tasks = {}    # mailing_id -> task
pending_auth = {}

# =========================
# DB INIT
# =========================

async def init_db():
    async with aiosqlite.connect(DB) as db:

        await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE,
            password TEXT,
            token TEXT
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
            session TEXT
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
            interval_sec INTEGER,
            chats TEXT,
            status TEXT DEFAULT 'stopped',
            sent INTEGER DEFAULT 0
        )
        """)

        await db.commit()


# =========================
# HELPERS
# =========================

def res(ok=True, **kwargs):
    return web.json_response({"status": ok, **kwargs})


async def user_by_token(request):
    token = request.headers.get("Authorization", "").replace("Bearer ", "")

    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT * FROM users WHERE token=?", (token,))
        return await cur.fetchone()


# =========================
# AUTH
# =========================

async def login(request):
    data = await request.json()

    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT * FROM users WHERE username=?", (data["username"],))
        user = await cur.fetchone()

    if not user:
        return res(False, message="User not found")

    if not bcrypt.checkpw(data["password"].encode(), user[2].encode()):
        return res(False, message="Wrong password")

    token = str(uuid.uuid4())

    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE users SET token=? WHERE id=?", (token, user[0]))
        await db.commit()

    return res(True, token=token, username=user[1])


# =========================
# PYROGRAM CLIENT
# =========================

async def get_client(acc):
    if acc[0] in clients:
        return clients[acc[0]]

    proxy = json.loads(acc[5]) if acc[5] else None

    client = Client(
        acc[6],
        api_id=int(acc[3]),
        api_hash=acc[4],
        proxy=proxy
    )

    await client.connect()
    clients[acc[0]] = client
    return client


# =========================
# SEND CODE
# =========================

async def send_code(request):
    user = await user_by_token(request)
    if not user:
        return res(False, message="Unauthorized")

    d = await request.json()

    client = Client(
        f"sessions/{user[1]}_{d['phone']}",
        api_id=int(d["api_id"]),
        api_hash=d["api_hash"],
        proxy=d.get("proxy")
    )

    await client.connect()
    sent = await client.send_code(d["phone"])

    auth_id = str(uuid.uuid4())

    pending_auth[auth_id] = {
        "client": client,
        "phone": d["phone"],
        "hash": sent.phone_code_hash,
        "user": user[0],
        "session": f"{user[1]}_{d['phone']}",
        "api_id": d["api_id"],
        "api_hash": d["api_hash"],
        "proxy": d.get("proxy")
    }

    return res(True, auth_id=auth_id)


# =========================
# VERIFY CODE
# =========================

async def verify_code(request):
    d = await request.json()

    auth = pending_auth.get(d["auth_id"])
    if not auth:
        return res(False, message="Auth expired")

    client = auth["client"]

    try:
        await client.sign_in(auth["phone"], auth["hash"], d["code"])

    except SessionPasswordNeeded:
        return res(True, need_password=True)

    async with aiosqlite.connect(DB) as db:
        await db.execute("""
        INSERT INTO accounts(owner_id, phone, api_id, api_hash, proxy, session)
        VALUES(?,?,?,?,?,?)
        """, (
            auth["user"],
            auth["phone"],
            auth["api_id"],
            auth["api_hash"],
            json.dumps(auth["proxy"]),
            auth["session"]
        ))
        await db.commit()

    await client.disconnect()

    return res(True, message="Account added")


# =========================
# ACCOUNTS
# =========================

async def accounts(request):
    user = await user_by_token(request)
    if not user:
        return res(False)

    async with aiosqlite.connect(DB) as db:
        cur = await db.execute("SELECT * FROM accounts WHERE owner_id=?", (user[0],))
        rows = await cur.fetchall()

    return res(True, accounts=[
        {"id": r[0], "phone": r[2]} for r in rows
    ])


# =========================
# MAILING WORKER
# =========================

async def mailing_worker(mid):
    while True:

        async with aiosqlite.connect(DB) as db:
            cur = await db.execute("SELECT * FROM mailings WHERE id=?", (mid,))
            m = await cur.fetchone()

        if not m or m[9] != "active":
            await asyncio.sleep(3)
            continue

        async with aiosqlite.connect(DB) as db:
            cur = await db.execute("SELECT * FROM accounts WHERE id=?", (m[2],))
            acc = await cur.fetchone()

        client = await get_client(acc)

        dialogs = await client.get_dialogs()

        groups = [
            d.chat.id for d in dialogs
            if d.chat.type in ("supergroup", "channel")
        ]

        texts = [m[4], m[5], m[6]]
        msg = texts[(m[10] // 50) % 3]

        for g in groups:
            try:
                await client.send_message(g, msg)
            except FloodWait as e:
                await asyncio.sleep(e.value)

        await asyncio.sleep(m[7])


# =========================
# MAILING CONTROL
# =========================

async def start_mailing(request):
    d = await request.json()

    task = asyncio.create_task(mailing_worker(d["id"]))
    mailing_tasks[d["id"]] = task

    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE mailings SET status='active' WHERE id=?", (d["id"],))
        await db.commit()

    return res(True)


async def stop_mailing(request):
    d = await request.json()

    task = mailing_tasks.get(d["id"])
    if task:
        task.cancel()

    async with aiosqlite.connect(DB) as db:
        await db.execute("UPDATE mailings SET status='stopped' WHERE id=?", (d["id"],))
        await db.commit()

    return res(True)


# =========================
# APP
# =========================

async def app_factory():
    await init_db()

    app = web.Application()

    app.router.add_post("/login", login)
    app.router.add_post("/send_code", send_code)
    app.router.add_post("/verify_code", verify_code)

    app.router.add_post("/accounts", accounts)

    app.router.add_post("/mailing/start", start_mailing)
    app.router.add_post("/mailing/stop", stop_mailing)

    cors = aiohttp_cors.setup(app, defaults={
        "*": aiohttp_cors.ResourceOptions(allow_headers="*", allow_methods="*", allow_credentials=True)
    })

    for r in list(app.router.routes()):
        cors.add(r)

    return app


if __name__ == "__main__":
    web.run_app(app_factory(), host="0.0.0.0", port=PORT)
