import asyncio
from aiogram import Bot, Dispatcher
from pyrogram import Client
import aiohttp_cors
from aiohttp import web

# --- ТВОИ ДАННЫЕ ---
API_TOKEN = 'ТВОЙ_ТОКЕН_БОТА'
USERS_DB = {
    "dmitry": {"pass": "777", "session": "my_acc"},
}

bot = Bot(token=API_TOKEN)
dp = Dispatcher()

# --- ФУНКЦИЯ ДЛЯ ПОЛУЧЕНИЯ ЧАТОВ ---
async def get_user_chats(session_name):
    app = Client(f"sessions/{session_name}")
    await app.start()
    chats = []
    async for dialog in app.get_dialogs():
        if dialog.chat.type in ["group", "supergroup"]:
            chats.append({"id": dialog.chat.id, "title": dialog.chat.title})
    await app.stop()
    return chats

# --- НАСТРОЙКА WEB-СЕРВЕРА ---
async def handle_get_chats(request):
    data = await request.json()
    login = data.get("login")
    password = data.get("password")
    
    user = USERS_DB.get(login)
    if user and user["pass"] == password:
        try:
            chats = await get_user_chats(user["session"])
            return web.json_response({"status": "ok", "chats": chats})
        except Exception as e:
            return web.json_response({"status": "error", "message": str(e)})
    return web.json_response({"status": "error", "message": "Wrong login/pass"})

app = web.Application()
cors = aiohttp_cors.setup(app, defaults={
    "*": aiohttp_cors.ResourceOptions(allow_credentials=True, expose_headers="*", allow_headers="*")
})
resource = app.router.add_resource("/get_chats")
cors.add(resource.add_route("POST", handle_get_chats))

# --- ГЛАВНЫЙ ЗАПУСК ---
async def main():
    # Запускаем бота и веб-сервер одновременно
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 8080)
    
    print("🚀 Сервер запущен на порту 8080")
    await site.start()
    await dp.start_polling(bot)

if __name__ == '__main__':
    # Это исправит твою ошибку RuntimeError: No current event loop
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except RuntimeError:
        # Если asyncio.run не сработал, используем принудительный цикл
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(main())