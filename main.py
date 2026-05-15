import asyncio
import os
import json
from aiohttp import web
import aiohttp_cors
from pyrogram import Client
from pyrogram.errors import FloodWait

# Папки для данных
os.makedirs("sessions", exist_ok=True)
os.makedirs("data", exist_ok=True)

# База пользователей WebApp
USERS_DB = {"dmitry": "777", "admin": "orion123"}
active_auths = {}

# --- УПРАВЛЕНИЕ ДАННЫМИ ---
def get_user_data(login):
    path = f"data/{login}.json"
    if os.path.exists(path):
        with open(path, "r") as f: return json.load(f)
    return {"sessions": []}

def save_user_data(login, data):
    with open(f"data/{login}.json", "w") as f: json.dump(data, f)

# --- ЭНДПОИНТЫ ---
async def auth_step1(request):
    try:
        data = await request.json()
        phone, api_id, api_hash = data['phone'], data['api_id'], data['api_hash']
        client = Client(f"sessions/{phone}", api_id=int(api_id), api_hash=api_hash)
        await client.connect()
        sent_code = await client.send_code(phone)
        active_auths[phone] = {"client": client, "hash": sent_code.phone_code_hash, "user": data['login']}
        return web.json_response({"status": "code_sent"})
    except Exception as e: return web.json_response({"status": "error", "message": str(e)})

async def auth_step2(request):
    try:
        data = await request.json()
        phone, code = data['phone'], data['code']
        auth = active_auths.get(phone)
        await auth["client"].sign_in(phone, auth["hash"], code)
        await auth["client"].disconnect()
        
        user_info = get_user_data(auth["user"])
        if phone not in user_info["sessions"]:
            user_info["sessions"].append(phone)
            save_user_data(auth["user"], user_info)
        return web.json_response({"status": "success"})
    except Exception as e: return web.json_response({"status": "error", "message": str(e)})

async def list_accounts(request):
    data = await request.json()
    return web.json_response(get_user_data(data['login']))

async def delete_account(request):
    data = await request.json()
    info = get_user_data(data['login'])
    if data['phone'] in info['sessions']:
        info['sessions'].remove(data['phone'])
        save_user_data(data['login'], info)
        path = f"sessions/{data['phone']}.session"
        if os.path.exists(path): os.remove(path)
    return web.json_response({"status": "ok"})

# --- ЗАПУСК ---
async def make_app():
    app = web.Application()
    cors = aiohttp_cors.setup(app, defaults={"*": aiohttp_cors.ResourceOptions(allow_headers="*", allow_methods="*")})
    
    app.router.add_post("/auth1", auth_step1)
    app.router.add_post("/auth2", auth_step2)
    app.router.add_post("/list", list_accounts)
    app.router.add_post("/delete", delete_account)
    
    for route in list(app.router.routes()): cors.add(route)
    return app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    web.run_app(make_app(), host="0.0.0.0", port=port)
