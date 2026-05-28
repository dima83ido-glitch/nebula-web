import os
import asyncio
import json
import uuid
import bcrypt
import aiosqlite
from aiohttp import web
import aiohttp_cors
from pyrogram import Client, client
from pyrogram.errors import SessionPasswordNeeded, FloodWait, AuthKeyUnregistered

# ========================= CONFIG =========================
PORT = int(os.environ.get("PORT", 8080))
MAX_ACCOUNTS = 50
MAX_CHATS = 20000

SESSIONS_DIR = "/opt/render/project/src/sessions"

os.makedirs(SESSIONS_DIR, exist_ok=True)
os.makedirs("logs", exist_ok=True)
DATABASE = "nebula.db"

pending_auths = {}
active_mailings = {}

# ========================= DATABASE =========================
async def init_db():
    async with aiosqlite.connect(
    DATABASE,
    timeout=30
) as db:
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
    
async def auto_login(request):
    try:
        data = await request.json()
        token = data.get("token")
        async with aiosqlite.connect(DATABASE) as db:
            cursor = await db.execute(
                "SELECT * FROM users WHERE remember_token=?",
                (token,)
            )
            user = await cursor.fetchone()
        if not user:
            return json_response(False)
        return json_response(
            True,
            role=user[3]
        )
    except Exception as e:
        return json_response(False, str(e))
    
async def get_telegram_client(account):
    session_name = account[6]
    session_path = f"sessions/{session_name}"
    session_file = f"{session_path}.session"

    # Если файла нет — сразу ошибка
    if not os.path.exists(session_file):
        raise Exception("SESSION_FILE_NOT_FOUND")

    try:
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
            sleep_threshold=120,      # увеличил
            workers=1
        )
        
        await app.start()
        me = await app.get_me()
        print(f"✅ СЕССИЯ УСПЕШНО ПОДКЛЮЧЕНА: {me.first_name} ({account[2]})")
        return app

    except AuthKeyUnregistered as e:
        print(f"❌ AUTH KEY DEAD: {session_file}")
        try:
            if 'app' in locals():
                await app.stop()
        except:
            pass
        # Удаляем битую сессию
        for ext in ["", ".session", ".session-journal"]:
            try:
                if os.path.exists(f"{session_path}{ext}"):
                    os.remove(f"{session_path}{ext}")
            except:
                pass
        raise Exception("SESSION_DEAD")
    
    except Exception as e:
        print(f"❌ CLIENT ERROR: {e}")
        try:
            if 'app' in locals():
                await app.stop()
        except:
            pass
        raise

# ========================= CREATE USER =========================
async def create_user(request):
    try:
        data = await request.json()
        username = data.get("username")
        password = data.get("password")
        if not username or not password:
            return json_response(False, "Введите логин и пароль")
        async with aiosqlite.connect(DATABASE) as db:
            cursor = await db.execute("SELECT * FROM users WHERE username=?", (username,))
            if await cursor.fetchone():
                return json_response(False, "Пользователь с таким логином уже существует")
            hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
            await db.execute("INSERT INTO users (username, password, role) VALUES (?, ?, 'user')", 
                           (username, hashed))
            await db.commit()
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
        async with aiosqlite.connect(DATABASE) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM accounts WHERE owner_id=?",
                (user[0],)
            )
            count = (await cursor.fetchone())[0]
        if count >= MAX_ACCOUNTS:
            return json_response(False, f"Достигнут лимит {MAX_ACCOUNTS} аккаунтов")
        phone = data["phone"].strip()
        api_id = int(data["api_id"])
        api_hash = data["api_hash"].strip()
        proxy = data.get("proxy")
        clean_phone = ''.join(filter(str.isdigit, phone))
        session_name = f"{username}_{clean_phone}"
        session_path = f"sessions/{session_name}"
        session_file = f"{session_path}.session"
        if os.path.exists(session_file):
            try:
                os.remove(session_file)
                print(f"🗑 OLD SESSION REMOVED: {session_file}")
            except Exception as e:
                print(f"❌ DELETE SESSION ERROR: {e}")
        print(f"🔄 Отправка кода на номер: {phone}")
        client = Client(
            session_path,
            api_id=api_id,
            api_hash=api_hash,
            proxy=proxy if proxy else None,
            device_model="iPhone 15 Pro",
            system_version="iOS 17.0",
            app_version="10.6.0",
            lang_code="ru",
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
            "phone_code_hash": sent_code.phone_code_hash,
            "username": username,
            "session_name": session_name
        }
        print(f"✅ Код успешно отправлен: {phone}")
        return json_response(True, "Код отправлен", auth_id=auth_id)
    except FloodWait as e:
        print(f"⏳ FLOODWAIT: {e.value}")
        return json_response(False, f"FloodWait {e.value} сек")
    except AuthKeyUnregistered:
        print("❌ AUTH KEY UNREGISTERED")
        return json_response(False, "Сессия Telegram повреждена. Попробуйте снова.")
    except Exception as e:
        import traceback
        print(f"❌ send_code ERROR: {str(e)}")
        traceback.print_exc()
        return json_response(False, f"Ошибка: {str(e)}")

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
            await client.storage.save()
            await client.disconnect()
            del pending_auths[auth_id]
            return json_response(True, "Аккаунт успешно добавлен")
        except SessionPasswordNeeded:
            return json_response(True, "Требуется 2FA пароль", need_password=True)
        except Exception as e:
            await client.disconnect()
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
        await save_account(auth, me.username)
        await client.storage.save()
        await client.disconnect()
        del pending_auths[auth_id]
        print(f"✅ 2FA успешно пройден для {auth['phone']}")
        return json_response(True, "Аккаунт успешно добавлен")
    except Exception as e:
        print("❌ verify_password ERROR:", str(e))
        try:
            if 'client' in locals():
                await client.disconnect()
        except:
            pass
        return json_response(False, f"Ошибка 2FA: {str(e)}")

async def save_account(auth, tg_username):
    try:
        user = await get_user(auth["username"])
        if not user:
            return
        async with aiosqlite.connect(DATABASE) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM accounts WHERE owner_id=?",
                (user[0],)
            )
            count = (await cursor.fetchone())[0]
            if count >= MAX_ACCOUNTS:
                print(f"Лимит {MAX_ACCOUNTS} аккаунтов достигнут!")
                return
            session_name = auth["session_name"].replace("sessions/", "")
            await db.execute("""
                INSERT INTO accounts (
                    owner_id, phone, api_id, api_hash, proxy, session_name
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, (
                user[0], auth["phone"], str(auth["api_id"]), 
                auth["api_hash"], None, session_name
            ))
            await db.commit()
        print(f"Аккаунт сохранён: {auth['phone']}")
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
        account_id = data["account_id"]
        async with aiosqlite.connect(DATABASE) as db:
            cursor = await db.execute("SELECT session_name FROM accounts WHERE id=?", (account_id,))
            acc = await cursor.fetchone()
            if acc and acc[0]:
                session_file = f"sessions/{acc[0]}.session"
                if os.path.exists(session_file):
                    os.remove(session_file)
                    print(f"✅ Сессия удалена: {session_file}")
            await db.execute("DELETE FROM accounts WHERE id=?", (account_id,))
            await db.commit()
        return json_response(True, "Аккаунт и сессия успешно удалены")
    except Exception as e:
        print("delete_account error:", str(e))
        return json_response(False, str(e))

async def delete_session(request):
    try:
        data = await request.json()
        account_id = data.get("account_id")
        if not account_id:
            return json_response(False, "Не указан ID аккаунта")
        async with aiosqlite.connect(DATABASE) as db:
            cursor = await db.execute("SELECT session_name FROM accounts WHERE id=?", (account_id,))
            acc = await cursor.fetchone()
            if acc and acc[0]:
                session_file = f"sessions/{acc[0]}.session"
                if os.path.exists(session_file):
                    os.remove(session_file)
                    print(f"✅ Сессия удалена: {session_file}")
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
        async with aiosqlite.connect(DATABASE) as db:
            cursor = await db.execute("SELECT * FROM accounts WHERE id=?", (account_id,))
            acc = await cursor.fetchone()
        if not acc:
            return json_response(False, "Аккаунт не найден")
        try:
            client = await get_telegram_client(acc)
        except Exception as e:
            if "SESSION_DEAD" in str(e):
                return json_response(False, "Сессия Telegram умерла. Перелогиньте аккаунт.")
            raise
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
    print(f"🚀 MAILING STARTED {mailing_id} — работает до остановки")
    client = None
    sent = 0

    try:
        while True:
            # Проверяем статус рассылки
            async with aiosqlite.connect(DATABASE) as db:
                cursor = await db.execute("SELECT * FROM mailings WHERE id=?", (mailing_id,))
                mailing = await cursor.fetchone()

            if not mailing:
                print("❌ MAILING DELETED")
                return

            if mailing[9] != "active":
                await asyncio.sleep(5)
                continue

            # Подключаем аккаунт
            async with aiosqlite.connect(DATABASE) as db:
                cursor = await db.execute("SELECT * FROM accounts WHERE id=?", (mailing[2],))
                account = await cursor.fetchone()

            if not account:
                print("❌ ACCOUNT NOT FOUND")
                await asyncio.sleep(10)
                continue

            # Подключаем клиент (с повторными попытками)
            if client is None:
                for attempt in range(3):  # 3 попытки переподключения
                    try:
                        client = await get_telegram_client(account)
                        break
                    except Exception as e:
                        print(f"❌ CLIENT CONNECT ATTEMPT {attempt+1} FAILED: {e}")
                        if "SESSION_DEAD" in str(e) or "SESSION_FILE_NOT_FOUND" in str(e):
                            print("🔄 Сессия умерла — нужно перелогинить аккаунт")
                            await asyncio.sleep(10)
                            continue
                        await asyncio.sleep(8)
                else:
                    print("❌ НЕ УДАЛОСЬ ПОДКЛЮЧИТЬСЯ К АККАУНТУ")
                    await asyncio.sleep(30)
                    continue

            # Загружаем чаты и тексты
            try:
                chats = json.loads(mailing[8] or "[]")
            except:
                chats = []

            if not chats:
                print("❌ NO CHATS")
                await asyncio.sleep(10)
                continue

            texts = [mailing[4] or "", mailing[5] or "", mailing[6] or ""]
            texts = [t.strip() for t in texts if t.strip()]
            if not texts:
                print("❌ NO TEXTS")
                await asyncio.sleep(10)
                continue

            interval = int(mailing[7] or 3)

            print(f"📨 НОВЫЙ КРУГ | Чатов: {len(chats)} | Текстов: {len(texts)}")

            # Отправка
            for raw_chat_id in chats:
                # Проверка остановки перед каждым сообщением
                async with aiosqlite.connect(DATABASE) as db:
                    cursor = await db.execute("SELECT status FROM mailings WHERE id=?", (mailing_id,))
                    current = await cursor.fetchone()
                if not current or current[0] != "active":
                    print(f"🛑 РАССЫЛКА {mailing_id} ОСТАНОВЛЕНА ПОЛЬЗОВАТЕЛЕМ")
                    return

                try:
                    chat_id = int(raw_chat_id)
                except:
                    continue

                try:
                    text = texts[sent % len(texts)]
                    await client.send_message(chat_id, text)
                    sent += 1
                    print(f"✅ SENT #{sent} → {chat_id}")

                    async with aiosqlite.connect(DATABASE) as db:
                        await db.execute("UPDATE mailings SET sent_count=? WHERE id=?", (sent, mailing_id))
                        await db.commit()

                    await asyncio.sleep(interval)

                except FloodWait as e:
                    await asyncio.sleep(int(e.value))
                except AuthKeyUnregistered:
                    print("❌ SESSION DEAD во время отправки")
                    if client:
                        try:
                            await client.stop()
                        except:
                            pass
                    client = None
                    break
                except Exception as e:
                    print(f"❌ SEND ERROR {chat_id}: {e}")
                    await asyncio.sleep(3)

            print(f"🔁 КРУГ ЗАВЕРШЁН (всего {sent}). Следующий через 10 сек...")
            await asyncio.sleep(10)

    except asyncio.CancelledError:
        print(f"🛑 MAILING {mailing_id} CANCELLED BY USER")
    except Exception as e:
        import traceback
        print(f"❌ MAILING CRASH: {e}")
        traceback.print_exc()
    finally:
        if client:
            try:
                await client.stop()
            except:
                pass

# ========================= Остальные функции (1:1) =========================
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
        status = data["status"]
        async with aiosqlite.connect(DATABASE) as db:
            await db.execute("UPDATE mailings SET status=? WHERE id=?", (status, m_id))
            await db.commit()

        if status == "active":
            if m_id in active_mailings and active_mailings[m_id].done():
                del active_mailings[m_id]
            if m_id not in active_mailings:
                task = asyncio.create_task(mailing_worker(m_id))
                active_mailings[m_id] = task
                print(f"✅ MAILING TASK CREATED {m_id}")
        else:
            if m_id in active_mailings:
                task = active_mailings[m_id]
                task.cancel()
                try:
                    await task
                except:
                    pass
                del active_mailings[m_id]
                print(f"🛑 MAILING TASK STOPPED {m_id}")
        return json_response(True)
    except Exception as e:
        import traceback
        print("toggle_mailing ERROR:", str(e))
        traceback.print_exc()
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
    cors = aiohttp_cors.setup(app, defaults={"*": aiohttp_cors.ResourceOptions(allow_headers="*", allow_methods="*", allow_credentials=True)})
    for route in list(app.router.routes()):
        cors.add(route)
    app.on_startup.append(start_background_tasks)
    return app

async def start_background_tasks(app):
    async with aiosqlite.connect(DATABASE) as db:
        cursor = await db.execute("SELECT id FROM mailings WHERE status='active'")
        rows = await cursor.fetchall()
    for row in rows:
        mailing_id = row[0]
        if mailing_id not in active_mailings:
            active_mailings[mailing_id] = asyncio.create_task(mailing_worker(mailing_id))
            print(f"♻️ RESTORED MAILING {mailing_id}")

if __name__ == "__main__":
    asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
    web.run_app(create_app(), host="0.0.0.0", port=PORT)
