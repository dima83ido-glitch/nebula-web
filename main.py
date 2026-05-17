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
os.makedirs("sessions", exist_ok=True)
DATABASE = "nebula.db"

pending_auths = {}
active_mailings = {}

# ========================= DATABASE =========================
async def init_db():
    async with aiosqlite.connect(DATABASE) as db:
        # ... (оставляем твои таблицы без изменений)
        await db.execute("""CREATE TABLE IF NOT EXISTS ...""")  # все таблицы как было
        # (полный код init_db из предыдущей версии)
        await db.commit()
    await create_admin()

# ... (все функции register, login, send_code, verify_code и т.д. остаются)

# ==================== NEW: GET CHATS ====================
async def get_chats(request):
    try:
        data = await request.json()
        account_id = data["account_id"]
        
        async with aiosqlite.connect(DATABASE) as db:
            cursor = await db.execute("SELECT * FROM accounts WHERE id=?", (account_id,))
            acc = await cursor.fetchone()
            if not acc:
                return web.json_response({"status": False, "message": "Аккаунт не найден"})

        client = Client(f"sessions/{acc[6]}", api_id=int(acc[3]), api_hash=acc[4])
        await client.connect()

        chats = []
        async for dialog in client.get_dialogs(limit=200):
            if dialog.chat.type in ["group", "supergroup", "channel", "private"]:
                chats.append({
                    "id": str(dialog.chat.id),
                    "title": dialog.chat.title or dialog.chat.first_name or dialog.chat.username or "Без названия"
                })

        await client.disconnect()
        return web.json_response({"status": True, "chats": chats})
    except Exception as e:
        return web.json_response({"status": False, "message": str(e)})

# ==================== MAILING CRUD ====================
async def create_mailing(request):
    try:
        data = await request.json()
        user = await get_user(data["username"])
        
        async with aiosqlite.connect(DATABASE) as db:
            await db.execute("""
                INSERT INTO mailings 
                (owner_id, account_id, name, text1, text2, text3, interval_seconds, chats, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'stopped')
            """, (
                user[0], 
                data["account_id"], 
                data["name"], 
                data.get("text1",""), 
                data.get("text2",""), 
                data.get("text3",""), 
                int(data.get("interval", 60)), 
                json.dumps(data.get("chats", []))
            ))
            await db.commit()
        return web.json_response({"status": True, "message": "Рассылка создана"})
    except Exception as e:
        return web.json_response({"status": False, "message": str(e)})

async def list_mailings(request):
    try:
        data = await request.json()
        user = await get_user(data["username"])
        async with aiosqlite.connect(DATABASE) as db:
            cursor = await db.execute("""
                SELECT m.id, m.name, m.status, m.sent_count, a.phone, m.text1, m.text2, m.text3, m.interval_seconds, m.chats 
                FROM mailings m 
                JOIN accounts a ON m.account_id = a.id 
                WHERE m.owner_id=?
            """, (user[0],))
            rows = await cursor.fetchall()
        
        mailings = []
        for r in rows:
            mailings.append({
                "id": r[0],
                "name": r[1],
                "status": r[2],
                "sent": r[3],
                "phone": r[4],
                "text1": r[5],
                "text2": r[6],
                "text3": r[7],
                "interval": r[8],
                "chats": json.loads(r[9]) if r[9] else []
            })
        return web.json_response({"status": True, "mailings": mailings})
    except Exception as e:
        return web.json_response({"status": False, "message": str(e)})

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
        return web.json_response({"status": True})
    except Exception as e:
        return web.json_response({"status": False, "message": str(e)})

async def update_mailing(request):
    try:
        data = await request.json()
        m_id = data["id"]
        async with aiosqlite.connect(DATABASE) as db:
            await db.execute("""
                UPDATE mailings 
                SET name=?, text1=?, text2=?, text3=?, interval_seconds=?, chats=?
                WHERE id=?
            """, (
                data["name"], 
                data.get("text1",""), 
                data.get("text2",""), 
                data.get("text3",""), 
                int(data.get("interval",60)), 
                json.dumps(data.get("chats",[])),
                m_id
            ))
            await db.commit()
        return web.json_response({"status": True, "message": "Рассылка обновлена"})
    except Exception as e:
        return web.json_response({"status": False, "message": str(e)})

# ========================= APP =========================
async def create_app():
    await init_db()
    app = web.Application()

    app.router.add_post("/login", login)
    app.router.add_post("/send_code", send_code)
    app.router.add_post("/verify_code", verify_code)
    app.router.add_post("/verify_password", verify_password)
    app.router.add_post("/accounts", list_accounts)
    app.router.add_post("/delete_account", delete_account)
    app.router.add_post("/get_chats", get_chats)           # ← Новый
    app.router.add_post("/create_mailing", create_mailing)
    app.router.add_post("/mailings", list_mailings)
    app.router.add_post("/delete_mailing", delete_mailing) # ← Новый
    app.router.add_post("/update_mailing", update_mailing) # ← Новый

    app.router.add_get("/", lambda r: web.FileResponse('index.html'))

    # CORS
    cors = aiohttp_cors.setup(app, defaults={"*": aiohttp_cors.ResourceOptions(allow_headers="*", allow_methods="*", allow_credentials=True)})
    for route in list(app.router.routes()):
        cors.add(route)

    app.on_startup.append(start_background_tasks)
    return app

# ... (остальной код mailing_worker, start_background_tasks и т.д. как в предыдущей версии)

if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=PORT)
