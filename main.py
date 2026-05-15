import asyncio
import os
import sys
from aiohttp import web
import aiohttp_cors
from pyrogram import Client

# --- КОНФИГ ---
TOKEN = "8726777640:AAH8wXSZieKj1elSAqKCejWxS9Xok3ob5iM"
# Твоя база пользователей: "логин": {"pass": "пароль", "session": "имя_файла_без_extension"}
USERS_DB = {
    "dmitry": {"pass": "777", "session": "my_acc"},
    "pisbeos": {"pass": "123", "session": "test_acc"}
}

# --- Инициализация бота ---
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.types import WebAppInfo, ReplyKeyboardMarkup, KeyboardButton

bot = Bot(token=TOKEN)
dp = Dispatcher()

if not os.path.exists("sessions"):
    os.makedirs("sessions")

# --- API HANDLERS ---

async def handle_login(request):
    try:
        data = await request.json()
        login = data.get("login")
        password = data.get("password")
        user = USERS_DB.get(login)
        if user and user["pass"] == password:
            return web.json_response({"status": "ok", "session": user["session"]})
        return web.json_response({"status": "error", "message": "Отказ в доступе"}, status=403)
    except:
        return web.json_response({"status": "error"}, status=400)

async def handle_get_chats(request):
    session_name = request.query.get("session")
    if not session_name:
        return web.json_response({"error": "No session"}, status=400)
    
    session_path = f"sessions/{session_name}"
    chats = []
    try:
        # Используем твой API ID/HASH или стандартные
        async with Client(session_path, api_id=28415512, api_hash="a1b2c3d4e5f6") as app:
            async for dialog in app.get_dialogs():
                if dialog.chat.type.value in ["group", "supergroup"]:
                    chats.append({
                        "id": dialog.chat.id,
                        "title": dialog.chat.title or "Группа"
                    })
        return web.json_response({"chats": chats})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

# --- BOT HANDLERS ---
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    markup = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 ОТКРЫТЬ NEBULA", web_app=WebAppInfo(url="https://nebula-web-omega.vercel.app/"))]],
        resize_keyboard=True
    )
    await message.answer(f"Привет, Король писбеов! Твоя система готова.", reply_markup=markup)

# --- START SERVER ---
async def main():
    app = web.Application()
    
    # Регистрация путей
    app.router.add_post('/api/login', handle_login)
    app.router.add_get('/api/chats', handle_get_chats)
    
    # Настройка CORS (чтобы Vercel мог подключаться)
    cors = aiohttp_cors.setup(app, defaults={
        "*": aiohttp_cors.ResourceOptions(
            allow_credentials=True,
            expose_headers="*",
            allow_headers="*",
            allow_methods="*"
        )
    })
    for route in list(app.router.routes()):
        cors.add(route)

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    
    print(f"🚀 Сервер запущен на порту {port}")
    await asyncio.gather(site.start(), dp.start_polling(bot))

if __name__ == "__main__":
    asyncio.run(main())