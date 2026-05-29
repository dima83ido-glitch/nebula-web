import os
import asyncio
import json
import uuid
import bcrypt
import aiosqlite
from aiohttp import web
import aiohttp_cors
from pyrogram import Client
from pyrogram.errors import SessionPasswordNeeded, FloodWait, AuthKeyUnregistered, ConnectionError

# ========================= CONFIG =========================
PORT = int(os.environ.get("PORT", 8080))
MAX_ACCOUNTS = 50

SESSIONS_DIR = "sessions"
os.makedirs(SESSIONS_DIR, exist_ok=True)
os.makedirs("logs", exist_ok=True)
DATABASE = "nebula.db"

pending_auths = {}
active_mailings = {}  # mailing_id -> Task

# ========================= DATABASE =========================
async def init_db():
    async with aiosqlite.connect(DATABASE, timeout=30) as db:
        await db.execute("PRAGMA journal_mode = WAL;")
        await db.execute("PRAGMA busy_timeout = 30000;")
        await db.execute("PRAGMA cache_size = -64000;")
        await db.execute("PRAGMA synchronous = NORMAL;")

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
            interval_seconds INTEGER DEFAULT 3,
            chats TEXT,
            status TEXT DEFAULT 'stopped',
            sent_count INTEGER DEFAULT 0
        )
        """)
        await db.commit()
    await create_admin()

async def create_admin():
    async with aiosqlite.connect(DATABASE) as db:
        if not await (await db.execute("SELECT 1 FROM users WHERE username=?", ("admin",))).fetchone():
            hashed = bcrypt.hashpw("orion123".encode(), bcrypt.gensalt()).decode()
            await db.execute("INSERT INTO users (username, password, role) VALUES (?, ?, ?)",
                           ("admin", hashed, "admin"))
            await db.commit()

# ========================= HELPERS =========================
def json_response(status=True, message="", **kwargs):
    return web.json_response({"status": status, "message": message, **kwargs})

async def get_user(username):
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute("SELECT * FROM users WHERE username=?", (username,))
        return await cursor.fetchone()

async def get_telegram_client(account):
    try:
        session_name = account[6]
        session_path = f"{SESSIONS_DIR}/{session_name}"

        app = Client(
            name=session_path,
            api_id=int(account[3]),
            api_hash=account[4],
            proxy=account[5] if account[5] else None,
            device_model="iPhone 15 Pro",
            system_version="iOS 17.0",
            app_version="10.6.0",
            lang_code="ru",
            in_memory=False,
            no_updates=True,
            sleep_threshold=60,
            workers=1
        )

        await app.start()
        await asyncio.sleep(1)
        await app.get_me()
        print(f"✅ Client started for {account[1]}")
        return app
    except Exception as e:
        print(f"❌ CLIENT INIT ERROR: {e}")
        try:
            await app.stop()
        except:
            pass
        return None

# ========================= MAILING WORKER =========================
async def mailing_worker(mailing_id: int):
    print(f"🚀 MAILING WORKER STARTED: {mailing_id}")
    client = None
    sent = 0

    try:
        while True:
            # Проверка статуса
            async with aiosqlite.connect(DATABASE) as db:
                row = await (await db.execute(
                    "SELECT status, sent_count FROM mailings WHERE id=?", (mailing_id,)
                )).fetchone()
                
                if not row or row[0] != "active":
                    print(f"🛑 MAILING {mailing_id} STOPPED")
                    break
                sent = row[1]

            # Загрузка данных
            async with aiosqlite.connect(DATABASE) as db:
                mailing = await (await db.execute("SELECT * FROM mailings WHERE id=?", (mailing_id,))).fetchone()
                account = await (await db.execute("SELECT * FROM accounts WHERE id=?", (mailing[2],))).fetchone()

            if not account:
                await asyncio.sleep(10)
                continue

            # Переподключение клиента при необходимости
            if client is None:
                client = await get_telegram_client(account)
                if not client:
                    await asyncio.sleep(15)
                    continue

            # Подготовка данных
            try:
                chats = json.loads(mailing[8] or "[]")
            except:
                chats = []

            texts = [t.strip() for t in [mailing[4], mailing[5], mailing[6]] if t and t.strip()]
            if not texts or not chats:
                await asyncio.sleep(10)
                continue

            interval = int(mailing[7] or 3)

            # Основной цикл отправки
            for raw_id in chats:
                # Проверка статуса перед каждым сообщением
                async with aiosqlite.connect(DATABASE) as db:
                    status_row = await (await db.execute(
                        "SELECT status FROM mailings WHERE id=?", (mailing_id,)
                    )).fetchone()
                    if not status_row or status_row[0] != "active":
                        break

                try:
                    chat_id = int(raw_id)
                    text = texts[sent % len(texts)]

                    await client.send_message(chat_id, text)
                    sent += 1

                    async with aiosqlite.connect(DATABASE) as db:
                        await db.execute("UPDATE mailings SET sent_count=? WHERE id=?", (sent, mailing_id))
                        await db.commit()

                    print(f"✅ SENT {sent} → {chat_id}")
                    await asyncio.sleep(interval)

                except FloodWait as e:
                    print(f"⏳ FLOODWAIT {e.value} сек")
                    await asyncio.sleep(e.value)
                except (AuthKeyUnregistered, ConnectionError):
                    print("❌ SESSION DEAD → RECONNECT")
                    if client:
                        try: await client.stop()
                        except: pass
                    client = None
                    await asyncio.sleep(10)
                    break
                except Exception as e:
                    print(f"⚠️ SEND ERROR to {chat_id}: {e}")
                    await asyncio.sleep(3)

            await asyncio.sleep(5)  # пауза между полными кругами

    except asyncio.CancelledError:
        print(f"🛑 MAILING {mailing_id} WAS CANCELLED")
    except Exception as e:
        print(f"💥 MAILING CRASH {mailing_id}: {e}")
    finally:
        if client:
            try:
                await client.stop()
            except:
                pass
        active_mailings.pop(mailing_id, None)

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

async def auto_login(request):
    try:
        data = await request.json()
        token = data.get("token")
        async with aiosqlite.connect(DATABASE) as db:
            user = await (await db.execute("SELECT * FROM users WHERE remember_token=?", (token,))).fetchone()
        if not user:
            return json_response(False)
        return json_response(True, role=user[3])
    except Exception as e:
        return json_response(False, str(e))

# ========================= ACCOUNT =========================
async def send_code(request):
    try:
        data = await request.json()
        username = data["username"]
        user = await get_user(username)
        if not user:
            return json_response(False, "Пользователь не найден")

        async with aiosqlite.connect(DATABASE) as db:
            count = (await (await db.execute("SELECT COUNT(*) FROM accounts WHERE owner_id=?", (user[0],))).fetchone())[0]
        if count >= MAX_ACCOUNTS:
            return json_response(False, f"Достигнут лимит {MAX_ACCOUNTS} аккаунтов")

        phone = data["phone"].strip()
        api_id = int(data["api_id"])
        api_hash = data["api_hash"].strip()
        proxy = data.get("proxy")

        clean_phone = ''.join(filter(str.isdigit, phone))
        session_name = f"{username}_{clean_phone}"

        session_file = f"{SESSIONS_DIR}/{session_name}.session"
        if os.path.exists(session_file):
            try:
                os.remove(session_file)
            except:
                pass

        client = Client(
            name=f"{SESSIONS_DIR}/{session_name}",
            api_id=api_id,
            api_hash=api_hash,
            proxy=proxy if proxy else None,
            device_model="iPhone 15 Pro",
            system_version="iOS 17.0",
            app_version="10.6.0",
            lang_code="ru",
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
            "phone_code_hash": sent_code.phone_code_hash,
            "username": username,
            "session_name": session_name
        }

        return json_response(True, "Код отправлен", auth_id=auth_id)

    except FloodWait as e:
        return json_response(False, f"FloodWait {e.value} сек")
    except Exception as e:
        print(f"send_code ERROR: {e}")
        return json_response(False, str(e))

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
            await client.sign_in(auth["phone"], auth["phone_code_hash"], code)
            me = await client.get_me()
            await save_account(auth, me.username)
            await client.disconnect()
            del pending_auths[auth_id]
            return json_response(True, "Аккаунт добавлен")
        except SessionPasswordNeeded:
            return json_response(True, "Требуется 2FA", need_password=True)
        except Exception as e:
            await client.disconnect()
            return json_response(False, str(e))
    except Exception as e:
        return json_response(False, "Ошибка сервера")

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
        me = await client.get_me()
        await save_account(auth, me.username)
        await client.disconnect()
        del pending_auths[auth_id]
        return json_response(True, "Аккаунт добавлен")
    except Exception as e:
        print("verify_password ERROR:", e)
        try:
            if 'client' in locals():
                await client.disconnect()
        except:
            pass
        return json_response(False, str(e))

async def save_account(auth, tg_username):
    try:
        user = await get_user(auth["username"])
        if not user: return
        async with aiosqlite.connect(DATABASE) as db:
            await db.execute("""
                INSERT INTO accounts (owner_id, phone, api_id, api_hash, proxy, session_name)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (user[0], auth["phone"], str(auth["api_id"]), auth["api_hash"], None, auth["session_name"]))
            await db.commit()
    except Exception as e:
        print("save_account error:", e)

async def list_accounts(request):
    try:
        data = await request.json()
        user = await get_user(data["username"])
        if not user:
            return json_response(False, "Пользователь не найден")
        async with aiosqlite.connect(DATABASE) as db:
            rows = await (await db.execute("SELECT id, phone FROM accounts WHERE owner_id=? ORDER BY created_at DESC", (user[0],))).fetchall()
        accounts = [{"id": r[0], "phone": r[1]} for r in rows]
        return json_response(True, accounts=accounts)
    except Exception as e:
        return json_response(False, str(e))

async def delete_account(request):
    try:
        data = await request.json()
        account_id = data["account_id"]
        async with aiosqlite.connect(DATABASE) as db:
            acc = await (await db.execute("SELECT session_name FROM accounts WHERE id=?", (account_id,))).fetchone()
            if acc and acc[0]:
                session_file = f"{SESSIONS_DIR}/{acc[0]}.session"
                if os.path.exists(session_file):
                    os.remove(session_file)
            await db.execute("DELETE FROM accounts WHERE id=?", (account_id,))
            await db.commit()
        return json_response(True, "Аккаунт удалён")
    except Exception as e:
        return json_response(False, str(e))

# ========================= CHATS =========================
async def get_chats(request):
    client = None
    try:
        data = await request.json()
        account_id = data["account_id"]
        async with aiosqlite.connect(DATABASE) as db:
            acc = await (await db.execute("SELECT * FROM accounts WHERE id=?", (account_id,))).fetchone()
        if not acc:
            return json_response(False, "Аккаунт не найден")

        client = await get_telegram_client(acc)
        if not client:
            return json_response(False, "Не удалось подключиться к Telegram")

        chats = []
        async for dialog in client.get_dialogs():
            chat = dialog.chat
            title = chat.title or chat.first_name or chat.username or "Без названия"
            chats.append({"id": chat.id, "title": title})

        return json_response(True, chats=chats)
    except Exception as e:
        print(f"GET CHATS ERROR: {e}")
        return json_response(False, str(e))
    finally:
        if client:
            try: await client.stop()
            except: pass

# ========================= MAILINGS =========================
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
                  int(data.get("interval", 3)), json.dumps(data.get("chats", []))))
            await db.commit()
        return json_response(True, "Рассылка создана")
    except Exception as e:
        print("create_mailing error:", e)
        return json_response(False, str(e))

async def list_mailings(request):
    try:
        data = await request.json()
        user = await get_user(data["username"])
        async with aiosqlite.connect(DATABASE) as db:
            rows = await (await db.execute("""
                SELECT m.*, a.phone FROM mailings m 
                JOIN accounts a ON m.account_id = a.id 
                WHERE m.owner_id=?
            """, (user[0],))).fetchall()

        mailings = []
        for r in rows:
            mailings.append({
                "id": r[0],
                "account_id": r[2],
                "name": r[3],
                "status": r[9],
                "sent": r[10],
                "phone": r[11],
                "text1": r[4],
                "text2": r[5],
                "text3": r[6],
                "interval": r[7],
                "chats": json.loads(r[8]) if r[8] else []
            })
        return json_response(True, mailings=mailings)
    except Exception as e:
        print("list_mailings error:", e)
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
            active_mailings.pop(m_id, None)
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
                  data.get("text3",""), int(data.get("interval",3)), 
                  json.dumps(data.get("chats",[])), m_id))
            await db.commit()
        return json_response(True, "Рассылка обновлена")
    except Exception as e:
        return json_response(False, str(e))

async def toggle_mailing(request):
    try:
        data = await request.json()
        m_id = data["id"]
        status = data["status"]

        async with aiosqlite.connect(DATABASE) as db:
            await db.execute("UPDATE mailings SET status=? WHERE id=?", (status, m_id))
            await db.commit()

        if status == "active":
            if m_id not in active_mailings or active_mailings[m_id].done():
                task = asyncio.create_task(mailing_worker(m_id))
                active_mailings[m_id] = task
                print(f"✅ MAILING STARTED {m_id}")
        else:
            if m_id in active_mailings:
                active_mailings[m_id].cancel()
                try:
                    await active_mailings[m_id]
                except:
                    pass
                active_mailings.pop(m_id, None)
                print(f"🛑 MAILING STOPPED {m_id}")

        return json_response(True)
    except Exception as e:
        print("toggle_mailing ERROR:", e)
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
            if await (await db.execute("SELECT 1 FROM users WHERE username=?", (username,))).fetchone():
                return json_response(False, "Пользователь уже существует")
            hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
            await db.execute("INSERT INTO users (username, password, role) VALUES (?, ?, 'user')", 
                           (username, hashed))
            await db.commit()
        return json_response(True, "Пользователь создан", login=username, password=password)
    except Exception as e:
        print("create_user error:", e)
        return json_response(False, "Ошибка создания пользователя")

# ========================= APP =========================
async def create_app():
    await init_db()
    app = web.Application()
    
    routes = {
        "/login": login,
        "/auto_login": auto_login,
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
        "/create_user": create_user,
    }
    
    for path, handler in routes.items():
        app.router.add_post(path, handler)
    
    app.router.add_get("/", lambda r: web.FileResponse("index.html"))
    
    cors = aiohttp_cors.setup(app, defaults={"*": aiohttp_cors.ResourceOptions(
        allow_headers="*", allow_methods="*", allow_credentials=True
    )})
    for route in list(app.router.routes()):
        cors.add(route)

    app.on_startup.append(start_background_tasks)
    return app

async def start_background_tasks(app):
    async with aiosqlite.connect(DATABASE) as db:
        rows = await (await db.execute("SELECT id FROM mailings WHERE status='active'")).fetchall()
    for row in rows:
        mid = row[0]
        if mid not in active_mailings:
            active_mailings[mid] = asyncio.create_task(mailing_worker(mid))
            print(f"♻️ RESTORED MAILING {mid}")

if __name__ == "__main__":
    asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
    web.run_app(create_app(), host="0.0.0.0", port=PORT)
