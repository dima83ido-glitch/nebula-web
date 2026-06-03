import os
import asyncio
import json
import uuid
import bcrypt
import asyncpg
from aiohttp import web
import aiohttp_cors
from pyrogram import Client
from pyrogram.errors import SessionPasswordNeeded, FloodWait, AuthKeyUnregistered

# ========================= CONFIG =========================
PORT = int(os.environ.get("PORT", 8080))
MAX_ACCOUNTS = 50
MAX_CHATS = 20000

# Параметры БД через отдельные переменные (решает проблему со спецсимволами в пароле)
DB_HOST = os.environ.get("DB_HOST", "aws-1-eu-north-1.pooler.supabase.com")
DB_PORT = int(os.environ.get("DB_PORT", 5432))
DB_NAME = os.environ.get("DB_NAME", "postgres")
DB_USER = os.environ.get("DB_USER", "postgres.bhprvhgplvmueyxewgjs")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")

os.makedirs("logs", exist_ok=True)

pending_auths = {}
active_mailings = {}
db_pool = None

# ========================= DATABASE =========================
async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(
        host=DB_HOST,
        port=DB_PORT,
        database=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        min_size=2,
        max_size=10,
        ssl="require"
    )
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE,
                password TEXT,
                role TEXT DEFAULT 'user',
                remember_token TEXT
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id SERIAL PRIMARY KEY,
                owner_id INTEGER,
                phone TEXT,
                api_id TEXT,
                api_hash TEXT,
                proxy TEXT,
                session_name TEXT,
                session_string TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS mailings (
                id SERIAL PRIMARY KEY,
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
    await create_admin()
    print("✅ Database initialized")

async def create_admin():
    async with db_pool.acquire() as conn:
        existing = await conn.fetchrow("SELECT * FROM users WHERE username=$1", "admin")
        if not existing:
            hashed = bcrypt.hashpw("orion123".encode(), bcrypt.gensalt()).decode()
            await conn.execute(
                "INSERT INTO users (username, password, role) VALUES ($1, $2, $3)",
                "admin", hashed, "admin"
            )
            print("✅ Admin created")

# ========================= HELPERS =========================
def json_response(status=True, message="", **kwargs):
    return web.json_response({"status": status, "message": message, **kwargs})

async def get_user(username):
    async with db_pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM users WHERE username=$1", username)

# ========================= AUTH =========================
async def register(request):
    try:
        data = await request.json()
        username = data.get("username")
        password = data.get("password")
        async with db_pool.acquire() as conn:
            existing = await conn.fetchrow("SELECT id FROM users WHERE username=$1", username)
            if existing:
                return json_response(False, "Пользователь уже существует")
            hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
            await conn.execute("INSERT INTO users (username, password) VALUES ($1, $2)", username, hashed)
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
        if not bcrypt.checkpw(password.encode(), user["password"].encode()):
            return json_response(False, "Неверный пароль")
        token = str(uuid.uuid4())
        if remember:
            async with db_pool.acquire() as conn:
                await conn.execute("UPDATE users SET remember_token=$1 WHERE username=$2", token, username)
        return json_response(True, "Успешный вход", token=token, role=user["role"])
    except Exception as e:
        print("Login error:", str(e))
        return json_response(False, "Ошибка сервера")

async def auto_login(request):
    try:
        data = await request.json()
        token = data.get("token")
        async with db_pool.acquire() as conn:
            user = await conn.fetchrow("SELECT * FROM users WHERE remember_token=$1", token)
            if not user:
                return json_response(False)
            return json_response(True, role=user["role"])
    except Exception as e:
        return json_response(False, str(e))

# ========================= TELEGRAM CLIENT =========================
async def get_telegram_client(account):
    app = None
    try:
        session_string = account["session_string"]
        if not session_string:
            return None

        app = Client(
            name="nebula_session",
            api_id=int(account["api_id"]),
            api_hash=account["api_hash"],
            session_string=session_string,
            proxy=account["proxy"] if account["proxy"] else None,
            device_model="iPhone 15 Pro",
            system_version="iOS 17.0",
            app_version="10.6.0",
            lang_code="ru",
            in_memory=True,
            no_updates=True,
            sleep_threshold=60,
            workers=1
        )
        await app.start()
        await app.get_me()
        return app

    except AuthKeyUnregistered:
        print("❌ AUTH KEY DEAD — сессия невалидна")
        try:
            if app:
                await app.stop()
        except:
            pass
        return None
    except Exception as e:
        print(f"❌ CLIENT ERROR: {e}")
        try:
            if app:
                await app.stop()
        except:
            pass
        return None

# ========================= CREATE USER =========================
async def create_user(request):
    try:
        data = await request.json()
        username = data.get("username")
        password = data.get("password")
        if not username or not password:
            return json_response(False, "Введите логин и пароль")
        async with db_pool.acquire() as conn:
            existing = await conn.fetchrow("SELECT id FROM users WHERE username=$1", username)
            if existing:
                return json_response(False, "Пользователь с таким логином уже существует")
            hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
            await conn.execute(
                "INSERT INTO users (username, password, role) VALUES ($1, $2, 'user')",
                username, hashed
            )
        return json_response(True, "Пользователь создан", login=username, password=password)
    except Exception as e:
        print("create_user error:", str(e))
        return json_response(False, "Ошибка при создании пользователя")

# ========================= ACCOUNT =========================
async def send_code(request):
    try:
        data = await request.json()
        username = data["username"]
        user = await get_user(username)
        if not user:
            return json_response(False, "Пользователь не найден")

        async with db_pool.acquire() as conn:
            count = await conn.fetchval("SELECT COUNT(*) FROM accounts WHERE owner_id=$1", user["id"])
            if count >= MAX_ACCOUNTS:
                return json_response(False, f"Достигнут лимит {MAX_ACCOUNTS} аккаунтов")

        phone = data["phone"].strip()
        api_id = int(data["api_id"])
        api_hash = data["api_hash"].strip()
        proxy = data.get("proxy")
        clean_phone = ''.join(filter(str.isdigit, phone))
        session_name = f"{username}_{clean_phone}"

        print(f"🔄 Отправка кода на номер: {phone}")

        client = Client(
            name="auth_session",
            api_id=api_id,
            api_hash=api_hash,
            phone_number=phone,
            proxy=proxy if proxy else None,
            device_model="iPhone 15 Pro",
            system_version="iOS 17.0",
            app_version="10.6.0",
            lang_code="ru",
            in_memory=True,
            no_updates=True,
            workers=1,
            sleep_threshold=30
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
        print(f"✅ Код успешно отправлен: {phone}")
        return json_response(True, "Код отправлен", auth_id=auth_id)

    except FloodWait as e:
        print(f"⏳ FLOODWAIT: {e.value}")
        return json_response(False, f"FloodWait {e.value} сек")
    except Exception as e:
        import traceback
        print(f"❌ send_code ERROR: {e}")
        traceback.print_exc()
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
            await asyncio.sleep(2)
            session_string = await client.export_session_string()
            pending_auths.pop(auth_id, None)
            await save_account(auth, me.username, session_string)
            try:
                await client.stop()
            except:
                pass
            return json_response(True, "Аккаунт успешно добавлен")
        except SessionPasswordNeeded:
            return json_response(True, "Требуется 2FA пароль", need_password=True)
        except Exception as e:
            try:
                await client.stop()
            except:
                pass
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
        print(f"🔐 Проверка 2FA пароля для {auth['phone']}")
        await client.check_password(password)
        me = await client.get_me()
        await asyncio.sleep(2)
        session_string = await client.export_session_string()
        pending_auths.pop(auth_id, None)
        await save_account(auth, me.username, session_string)
        try:
            await client.stop()
        except:
            pass
        print(f"✅ 2FA успешно пройден для {auth['phone']}")
        return json_response(True, "Аккаунт успешно добавлен")
    except Exception as e:
        print("❌ verify_password ERROR:", str(e))
        return json_response(False, f"Ошибка 2FA: {str(e)}")
    
async def save_account(auth, tg_username, session_string=None):
    try:
        user = await get_user(auth["username"])
        if not user:
            return
        async with db_pool.acquire() as conn:
            count = await conn.fetchval("SELECT COUNT(*) FROM accounts WHERE owner_id=$1", user["id"])
            if count >= MAX_ACCOUNTS:
                print(f"Лимит {MAX_ACCOUNTS} аккаунтов достигнут!")
                return
            await conn.execute("""
                INSERT INTO accounts (owner_id, phone, api_id, api_hash, proxy, session_name, session_string)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
                user["id"],
                auth["phone"],
                str(auth["api_id"]),
                auth["api_hash"],
                auth.get("proxy"),
                auth["session_name"],
                session_string
            )
        print(f"✅ Аккаунт сохранён: {auth['phone']}")
    except Exception as e:
        print("Ошибка save_account:", str(e))

async def list_accounts(request):
    try:
        data = await request.json()
        user = await get_user(data["username"])
        if not user:
            return json_response(False, "Пользователь не найден")
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, phone FROM accounts WHERE owner_id=$1 ORDER BY created_at DESC",
                user["id"]
            )
        accounts = [{"id": r["id"], "phone": r["phone"]} for r in rows]
        return json_response(True, accounts=accounts)
    except Exception as e:
        print("list_accounts error:", str(e))
        return json_response(False, str(e))

async def delete_account(request):
    try:
        data = await request.json()
        account_id = data["account_id"]
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM accounts WHERE id=$1", account_id)
        return json_response(True, "Аккаунт успешно удалён")
    except Exception as e:
        print("delete_account error:", str(e))
        return json_response(False, str(e))

async def delete_session(request):
    try:
        data = await request.json()
        account_id = data.get("account_id")
        if not account_id:
            return json_response(False, "Не указан ID аккаунта")
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE accounts SET session_string=NULL WHERE id=$1", account_id)
        return json_response(True, "Сессия успешно удалена")
    except Exception as e:
        print("delete_session error:", str(e))
        return json_response(False, str(e))

# ========================= GET CHATS =========================
async def get_chats(request):
    client = None
    try:
        data = await request.json()
        account_id = data["account_id"]
        async with db_pool.acquire() as conn:
            acc = await conn.fetchrow("SELECT * FROM accounts WHERE id=$1", account_id)
        if not acc:
            return json_response(False, "Аккаунт не найден")

        client = await get_telegram_client(acc)
        if not client:
            return json_response(False, "Не удалось подключиться к Telegram. Удалите аккаунт и добавьте заново.")

        chats = []
        async for dialog in client.get_dialogs():
            try:
                chat = dialog.chat
                title = (chat.title or chat.first_name or chat.username or "Без названия")
                chats.append({"id": chat.id, "title": title})
            except:
                pass
        return json_response(True, chats=chats)

    except Exception as e:
        import traceback
        print(f"GET CHATS ERROR: {e}")
        traceback.print_exc()
        return json_response(False, str(e))
    finally:
        try:
            if client:
                await client.stop()
        except:
            pass

# ========================= MAILING =========================
async def mailing_worker(mailing_id):
    print(f"🚀 MAILING WORKER STARTED {mailing_id}")
    client = None

    try:
        while True:
            async with db_pool.acquire() as conn:
                mailing = await conn.fetchrow(
                    "SELECT status, sent_count FROM mailings WHERE id=$1", mailing_id
                )

            if not mailing or mailing["status"] != "active":
                print(f"🛑 Mailing {mailing_id} stopped (status check)")
                break

            sent = mailing["sent_count"]

            async with db_pool.acquire() as conn:
                account = await conn.fetchrow(
                    "SELECT * FROM accounts WHERE id=(SELECT account_id FROM mailings WHERE id=$1)",
                    mailing_id
                )

            if not account:
                print(f"⚠️ No account for mailing {mailing_id}, retry in 10s")
                await asyncio.sleep(10)
                continue

            if client is None:
                client = await get_telegram_client(account)
                if not client:
                    print(f"⚠️ Can't connect client for mailing {mailing_id}, retry in 15s")
                    await asyncio.sleep(15)
                    continue

            async with db_pool.acquire() as conn:
                data = await conn.fetchrow(
                    "SELECT text1, text2, text3, interval_seconds, chats FROM mailings WHERE id=$1",
                    mailing_id
                )

            if not data:
                await asyncio.sleep(10)
                continue

            texts = [t.strip() for t in [data["text1"], data["text2"], data["text3"]] if t and t.strip()]
            if not texts:
                await asyncio.sleep(10)
                continue

            interval = int(data["interval_seconds"] or 5)
            try:
                chats = json.loads(data["chats"] or "[]")
            except:
                chats = []

            if not chats:
                await asyncio.sleep(10)
                continue

            for raw_chat_id in chats:
                await asyncio.sleep(0)

                async with db_pool.acquire() as conn:
                    row = await conn.fetchrow("SELECT status FROM mailings WHERE id=$1", mailing_id)
                if not row or row["status"] != "active":
                    print(f"🛑 Mailing {mailing_id} stopped mid-loop")
                    return

                try:
                    chat_id = int(raw_chat_id)
                    text = texts[sent % len(texts)]
                    await client.send_message(chat_id, text)
                    sent += 1

                    async with db_pool.acquire() as conn:
                        await conn.execute(
                            "UPDATE mailings SET sent_count=$1 WHERE id=$2", sent, mailing_id
                        )

                    print(f"✅ SENT #{sent} -> {chat_id} (mailing {mailing_id})")

                except FloodWait as e:
                    print(f"⏳ FloodWait {e.value}s for mailing {mailing_id}")
                    await asyncio.sleep(e.value)
                except (AuthKeyUnregistered, ConnectionError) as e:
                    print(f"❌ AUTH/CONNECTION ERROR: {e}")
                    try:
                        if client:
                            await client.stop()
                    except:
                        pass
                    client = None
                    break
                except Exception as e:
                    print(f"❌ Send error to {chat_id}: {e}")
                    await asyncio.sleep(3)

                await asyncio.sleep(interval)

            await asyncio.sleep(5)

    except asyncio.CancelledError:
        print(f"🛑 Mailing {mailing_id} cancelled")
    except Exception as e:
        import traceback
        print(f"💥 Mailing worker crashed {mailing_id}: {e}")
        traceback.print_exc()
    finally:
        if client:
            try:
                await client.stop()
            except:
                pass
        try:
            async with db_pool.acquire() as conn:
                await conn.execute(
                    "UPDATE mailings SET status='stopped' WHERE id=$1 AND status='active'",
                    mailing_id
                )
        except:
            pass
        if mailing_id in active_mailings:
            del active_mailings[mailing_id]
        print(f"🏁 Mailing worker finished {mailing_id}")

# ========================= MAILING CRUD =========================
async def create_mailing(request):
    try:
        data = await request.json()
        user = await get_user(data["username"])
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO mailings (owner_id, account_id, name, text1, text2, text3, interval_seconds, chats, status)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'stopped')
            """,
                user["id"],
                data["account_id"],
                data["name"],
                data.get("text1", ""),
                data.get("text2", ""),
                data.get("text3", ""),
                int(data.get("interval", 60)),
                json.dumps(data.get("chats", []))
            )
        return json_response(True, "Рассылка создана")
    except Exception as e:
        print("create_mailing error:", str(e))
        return json_response(False, str(e))

async def list_mailings(request):
    try:
        data = await request.json()
        user = await get_user(data["username"])
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT m.*, a.phone
                FROM mailings m
                JOIN accounts a ON m.account_id = a.id
                WHERE m.owner_id=$1
            """, user["id"])
        mailings = []
        for r in rows:
            mailings.append({
                "id": r["id"],
                "account_id": r["account_id"],
                "name": r["name"],
                "status": r["status"],
                "sent": r["sent_count"],
                "phone": r["phone"],
                "text1": r["text1"],
                "text2": r["text2"],
                "text3": r["text3"],
                "interval": r["interval_seconds"],
                "chats": json.loads(r["chats"]) if r["chats"] else []
            })
        return json_response(True, mailings=mailings)
    except Exception as e:
        print("list_mailings error:", str(e))
        return json_response(False, str(e))

async def delete_mailing(request):
    try:
        data = await request.json()
        m_id = data["id"]
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM mailings WHERE id=$1", m_id)
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
        async with db_pool.acquire() as conn:
            await conn.execute("""
                UPDATE mailings
                SET name=$1, text1=$2, text2=$3, text3=$4, interval_seconds=$5, chats=$6
                WHERE id=$7
            """,
                data["name"],
                data.get("text1", ""),
                data.get("text2", ""),
                data.get("text3", ""),
                int(data.get("interval", 60)),
                json.dumps(data.get("chats", [])),
                m_id
            )
        return json_response(True, "Рассылка обновлена")
    except Exception as e:
        return json_response(False, str(e))

async def toggle_mailing(request):
    try:
        data = await request.json()
        m_id = int(data["id"])
        status = data["status"]

        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE mailings SET status=$1 WHERE id=$2", status, m_id)

        if status == "active":
            if m_id in active_mailings and active_mailings[m_id].done():
                del active_mailings[m_id]
            if m_id not in active_mailings:
                task = asyncio.create_task(mailing_worker(m_id))
                active_mailings[m_id] = task
                print(f"✅ MAILING STARTED {m_id}")
        else:
            if m_id in active_mailings:
                task = active_mailings[m_id]
                task.cancel()
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=5)
                except:
                    pass
                if m_id in active_mailings:
                    del active_mailings[m_id]
                print(f"🛑 MAILING STOPPED {m_id}")

        return json_response(True)

    except Exception as e:
        print("toggle_mailing ERROR:", str(e))
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
        "/delete_session": delete_session,
        "/get_chats": get_chats,
        "/create_mailing": create_mailing,
        "/mailings": list_mailings,
        "/delete_mailing": delete_mailing,
        "/update_mailing": update_mailing,
        "/toggle_mailing": toggle_mailing,
        "/create_user": create_user,
        "/auto_login": auto_login,
    }
    for path, handler in routes.items():
        app.router.add_post(path, handler)
    app.router.add_get("/", lambda r: web.FileResponse("index.html"))

    cors = aiohttp_cors.setup(app, defaults={"*": aiohttp_cors.ResourceOptions(
        allow_headers="*",
        allow_methods="*",
        allow_credentials=True
    )})
    for route in list(app.router.routes()):
        cors.add(route)

    app.on_startup.append(start_background_tasks)
    return app

async def start_background_tasks(app):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT id FROM mailings WHERE status='active'")
        for row in rows:
            mailing_id = row["id"]
            if mailing_id not in active_mailings:
                active_mailings[mailing_id] = asyncio.create_task(mailing_worker(mailing_id))
                print(f"♻️ RESTORED MAILING {mailing_id}")

if __name__ == "__main__":
    asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
    web.run_app(create_app(), host="0.0.0.0", port=PORT)
