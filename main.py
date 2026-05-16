import asyncio
import sys

# ХАК ДЛЯ PYTHON 3.11+ И PYROGRAM
# Мы создаем loop до того, как библиотека попытается его найти при импорте
try:
    loop = asyncio.get_event_loop()
except RuntimeError:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

import os
import json
import logging
import bcrypt
import aiosqlite
from aiohttp import web
import aiohttp_cors

# Теперь импортируем Pyrogram — теперь он не упадет
from pyrogram import Client, filters
from pyrogram.errors import (
    SessionPasswordNeeded,
    FloodWait,
    PhoneNumberInvalid,
    ApiIdInvalid,
    PhoneCodeInvalid,
    PhoneCodeExpired,
    PasswordHashInvalid,
    AuthKeyUnregistered,
    UserDeactivated,
    SessionRevoked
)

# ==============================================================================
# CONFIGURATION & LOGGING
# ==============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("logs/system.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("NEBULA-CORE")

PORT = int(os.environ.get("PORT", 8080))
SESSIONS_DIR = "sessions"
DATABASE = "nebula.db"

for folder in [SESSIONS_DIR, "logs"]:
    if not os.path.exists(folder):
        os.makedirs(folder)

# ==============================================================================
# GLOBALS
# ==============================================================================

pending_auths = {}  # {auth_id: {client, phone, api_id, ...}}
active_workers = {} # {mailing_id: Task}

# ==============================================================================
# DATABASE ENGINE
# ==============================================================================

async def init_db():
    logger.info("Initializing NEBULA Database...")
    async with aiosqlite.connect(DATABASE) as db:
        # ТАБЛИЦА ПОЛЬЗОВАТЕЛЕЙ (ВОРКЕРОВ)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                role TEXT DEFAULT 'user',
                balance REAL DEFAULT 0.0,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # ТАБЛИЦА ТГ АККАУНТОВ
        await db.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER,
                phone TEXT UNIQUE,
                api_id TEXT,
                api_hash TEXT,
                proxy TEXT,
                session_name TEXT,
                status TEXT DEFAULT 'active',
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(owner_id) REFERENCES users(id)
            )
        """)
        # ТАБЛИЦА РАССЫЛОК (МАКСИМАЛЬНО ПОДРОБНО)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS mailings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER,
                account_id INTEGER,
                name TEXT,
                message_text_1 TEXT,
                message_text_2 TEXT,
                message_text_3 TEXT,
                delay_min INTEGER,
                delay_max INTEGER,
                chats_list TEXT,
                status TEXT DEFAULT 'stopped',
                total_sent INTEGER DEFAULT 0,
                last_error TEXT,
                last_run_at TIMESTAMP,
                FOREIGN KEY(owner_id) REFERENCES users(id),
                FOREIGN KEY(account_id) REFERENCES accounts(id)
            )
        """)
        await db.commit()
    await create_root_admin()

async def create_root_admin():
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute("SELECT * FROM users WHERE username=?", ("admin",))
        if not await cursor.fetchone():
            hashed = bcrypt.hashpw("orion123".encode(), bcrypt.gensalt()).decode()
            await db.execute(
                "INSERT INTO users (username, password, role) VALUES (?, ?, ?)",
                ("admin", hashed, "admin")
            )
            await db.commit()
            logger.info("Root admin 'admin' created with pass 'orion123'")

# ==============================================================================
# AUTH & WORKER MANAGEMENT
# ==============================================================================

async def admin_add_worker(request):
    try:
        data = await request.json()
        admin_auth = data.get("admin_nick")
        
        async with aiosqlite.connect(DATABASE) as db:
            c = await db.execute("SELECT role FROM users WHERE username=?", (admin_auth,))
            res = await c.fetchone()
            if not res or res[0] != 'admin':
                return web.json_response({"status": False, "message": "ERR_FORBIDDEN"})

            new_u = data.get("username")
            new_p = data.get("password")
            hashed = bcrypt.hashpw(new_p.encode(), bcrypt.gensalt()).decode()
            
            try:
                await db.execute(
                    "INSERT INTO users (username, password, role) VALUES (?, ?, 'user')",
                    (new_u, hashed)
                )
                await db.commit()
                return web.json_response({"status": True, "message": f"Worker {new_u} added"})
            except:
                return web.json_response({"status": False, "message": "User exists"})
    except Exception as e:
        return web.json_response({"status": False, "message": str(e)})

async def handle_login(request):
    try:
        data = await request.json()
        u, p = data.get("username"), data.get("password")
        
        async with aiosqlite.connect(DATABASE) as db:
            db.row_factory = aiosqlite.Row
            c = await db.execute("SELECT * FROM users WHERE username=?", (u,))
            user = await c.fetchone()
            
            if user and bcrypt.checkpw(p.encode(), user['password'].encode()):
                return web.json_response({
                    "status": True,
                    "role": user['role'],
                    "username": user['username'],
                    "token": str(uuid.uuid4())
                })
        return web.json_response({"status": False, "message": "Invalid Credentials"})
    except Exception as e:
        return web.json_response({"status": False, "message": str(e)})

# ==============================================================================
# TELEGRAM CORE LOGIC (CONNECT, 2FA, SESSIONS)
# ==============================================================================

async def tg_send_code(request):
    try:
        data = await request.json()
        username = data['username']
        phone = data['phone'].strip().replace(" ", "")
        api_id = int(data['api_id'])
        api_hash = data['api_hash']
        
        session_name = f"{username}_{phone}"
        session_path = os.path.join(SESSIONS_DIR, session_name)
        
        client = Client(session_path, api_id=api_id, api_hash=api_hash)
        await client.connect()
        
        code_info = await client.send_code(phone)
        auth_id = str(uuid.uuid4())
        
        pending_auths[auth_id] = {
            "client": client,
            "phone": phone,
            "phone_code_hash": code_info.phone_code_hash,
            "api_id": api_id,
            "api_hash": api_hash,
            "username": username,
            "session_name": session_name
        }
        return web.json_response({"status": True, "auth_id": auth_id})
    except Exception as e:
        logger.error(f"TG Send Code Error: {e}")
        return web.json_response({"status": False, "message": str(e)})

async def tg_verify(request):
    try:
        data = await request.json()
        auth = pending_auths.get(data['auth_id'])
        if not auth: return web.json_response({"status": False, "message": "Session Timeout"})
        
        client = auth['client']
        try:
            await client.sign_in(auth['phone'], auth['phone_code_hash'], data['code'])
        except SessionPasswordNeeded:
            return web.json_response({"status": True, "need_2fa": True})
        
        # Сохранение в БД
        async with aiosqlite.connect(DATABASE) as db:
            cur = await db.execute("SELECT id FROM users WHERE username=?", (auth['username'],))
            uid = (await cur.fetchone())[0]
            await db.execute("""
                INSERT INTO accounts (owner_id, phone, api_id, api_hash, session_name)
                VALUES (?, ?, ?, ?, ?)
            """, (uid, auth['phone'], auth['api_id'], auth['api_hash'], auth['session_name']))
            await db.commit()
            
        await client.disconnect()
        del pending_auths[data['auth_id']]
        return web.json_response({"status": True, "message": "Account Bound"})
    except Exception as e:
        return web.json_response({"status": False, "message": str(e)})

# ==============================================================================
# MAILING ENGINE (ПОЛНЫЙ ЦИКЛ С ОБРАБОТКОЙ ОШИБОК)
# ==============================================================================

async def mailing_task(mid):
    async with aiosqlite.connect(DATABASE) as db:
        db.row_factory = aiosqlite.Row
        c = await db.execute("SELECT * FROM mailings WHERE id=?", (mid,))
        m = await c.fetchone()
        
        acc_c = await db.execute("SELECT * FROM accounts WHERE id=?", (m['account_id'],))
        acc = await acc_c.fetchone()
        
    client = Client(os.path.join(SESSIONS_DIR, acc['session_name']), 
                    api_id=int(acc['api_id']), api_hash=acc['api_hash'])
    
    try:
        await client.connect()
        chats = json.loads(m['chats_list'])
        texts = [m['message_text_1'], m['message_text_2'], m['message_text_3']]
        texts = [t for t in texts if t] # только не пустые
        
        count = m['total_sent']
        
        for chat in chats:
            # Проверка статуса в реальном времени
            async with aiosqlite.connect(DATABASE) as db:
                check = await db.execute("SELECT status FROM mailings WHERE id=?", (mid,))
                if (await check.fetchone())[0] != 'active': break
            
            try:
                msg = random.choice(texts)
                await client.send_message(chat, msg)
                count += 1
                async with aiosqlite.connect(DATABASE) as db:
                    await db.execute("UPDATE mailings SET total_sent=? WHERE id=?", (count, mid))
                    await db.commit()
            except FloodWait as e:
                await asyncio.sleep(e.value)
            except Exception as e:
                logger.warning(f"Failed to send to {chat}: {e}")
            
            await asyncio.sleep(random.randint(m['delay_min'], m['delay_max']))
            
        await client.disconnect()
    except Exception as e:
        logger.error(f"Mailing {mid} Failed: {e}")
        async with aiosqlite.connect(DATABASE) as db:
            await db.execute("UPDATE mailings SET status='error', last_error=? WHERE id=?", (str(e), mid))
            await db.commit()

# ==============================================================================
# API ROUTING & STARTUP
# ==============================================================================

async def make_app():
    await init_db()
    app = web.Application(client_max_size=1024**2*10)
    
    app.router.add_post("/api/login", handle_login)
    app.router.add_post("/api/admin/add_worker", admin_add_worker)
    app.router.add_post("/api/tg/send_code", tg_send_code)
    app.router.add_post("/api/tg/verify", tg_verify)
    
    # CORS
    cors = aiohttp_cors.setup(app, defaults={
        "*": aiohttp_cors.ResourceOptions(
            allow_credentials=True,
            expose_headers="*",
            allow_headers="*"
        )
    })
    for route in list(app.router.routes()):
        cors.add(route)
        
    return app

if __name__ == "__main__":
    web.run_app(make_app(), port=PORT)
