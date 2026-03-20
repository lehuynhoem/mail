import random
import time
import asyncio
from typing import Dict, Optional
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import aiohttp
from bs4 import BeautifulSoup

app = FastAPI(title="Temp Mail VN - Email tạm thời miễn phí")

templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

BASE_URL = "https://api.mail.gw"
user_sessions: Dict[int, dict] = {}  # session_key -> account data

async def api_get(session: aiohttp.ClientSession, url: str, headers: dict = None) -> dict:
    async with session.get(url, headers=headers, timeout=10) as r:
        if r.status >= 400:
            try:
                err = await r.json()
            except:
                err = {"message": await r.text()}
            raise Exception(f"API error {r.status}: {err}")
        return await r.json()

async def api_post(session: aiohttp.ClientSession, url: str, json_data: dict, headers: dict = None) -> dict:
    async with session.post(url, json=json_data, headers=headers, timeout=10) as r:
        if r.status >= 400:
            try:
                err = await r.json()
            except:
                err = {"message": await r.text()}
            raise Exception(f"API error {r.status}: {err}")
        return await r.json()

async def get_domains(session: aiohttp.ClientSession) -> list:
    try:
        data = await api_get(session, f"{BASE_URL}/domains")
        return [d["domain"] for d in data.get("hydra:member", []) if d.get("isActive")]
    except Exception as e:
        print(f"Error domains: {e}")
        return []

async def create_account(session: aiohttp.ClientSession, address: str, password: str):
    payload = {"address": address, "password": password}
    return await api_post(session, f"{BASE_URL}/accounts", payload)

async def get_token(session: aiohttp.ClientSession, address: str, password: str):
    payload = {"address": address, "password": password}
    data = await api_post(session, f"{BASE_URL}/token", payload)
    return data.get("token")

async def get_messages(session: aiohttp.ClientSession, token: str, page: int = 1) -> list:
    headers = {"Authorization": f"Bearer {token}"}
    data = await api_get(session, f"{BASE_URL}/messages?page={page}", headers)
    return data.get("hydra:member", [])

async def get_message_detail(session: aiohttp.ClientSession, msg_id: str, token: str):
    headers = {"Authorization": f"Bearer {token}"}
    return await api_get(session, f"{BASE_URL}/messages/{msg_id}", headers)

# ─── Tạo email mới ───────────────────────────────────────────────
async def create_new_temp_mail(session_key: int) -> dict:
    async with aiohttp.ClientSession() as http:
        domains = await get_domains(http)
        if not domains:
            raise Exception("Không lấy được domain")

        domain = random.choice(domains)
        username = f"t{session_key}_{int(time.time()) % 1000000}"
        password = f"pw{random.randint(10000,99999)}{random.choice('!@#$%')}"

        acc = await create_account(http, f"{username}@{domain}", password)
        if not acc or "address" not in acc:
            raise Exception("Tạo tài khoản thất bại")

        token = await get_token(http, acc["address"], password)
        if not token:
            raise Exception("Lấy token thất bại")

        user_sessions[session_key] = {
            "address": acc["address"],
            "password": password,
            "token": token,
            "seen_ids": set(),
            "created_at": time.time(),
        }

        return user_sessions[session_key]

# ─── Background check mail (per user) ────────────────────────────────
async def mail_poller(session_key: int):
    while session_key in user_sessions:
        data = user_sessions.get(session_key)
        if not data:
            break

        try:
            async with aiohttp.ClientSession() as http:
                headers = {"Authorization": f"Bearer {data['token']}"}
                msgs = await get_messages(http, data["token"])

                new_msgs = [m for m in msgs if m["id"] not in data["seen_ids"]]

                if new_msgs:
                    new_msgs.sort(key=lambda x: x.get("createdAt", ""))
                    for msg in new_msgs:
                        data["seen_ids"].add(msg["id"])
                        detail = await get_message_detail(http, msg["id"], data["token"])
                        if not detail:
                            continue

                        sender = detail.get("from", {}).get("address", "Unknown")
                        subject = detail.get("subject", "(Không chủ đề)")
                        body = detail.get("text") or ""

                        if not body and detail.get("html"):
                            try:
                                soup = BeautifulSoup(detail["html"], "html.parser")
                                body = soup.get_text(separator="\n", strip=True)
                            except:
                                body = "[HTML không đọc được]"

                        # Ở đây bạn có thể lưu vào database hoặc queue để hiển thị
                        # Hiện tại chỉ in log (sẽ cải thiện ở phần frontend)
                        print(f"[{session_key}] New mail from {sender}: {subject}")

        except Exception as e:
            print(f"Poller error {session_key}: {e}")

        await asyncio.sleep(8 + random.uniform(0, 5))

# ─── Routes ──────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    session_key = request.client.host.__hash__()  # đơn giản, production nên dùng cookie/session id
    account = user_sessions.get(session_key)

    if not account:
        try:
            account = await create_new_temp_mail(session_key)
            asyncio.create_task(mail_poller(session_key))
        except Exception as e:
            return templates.TemplateResponse(
                "index.html", {"request": request, "error": str(e)}
            )

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "email": account["address"],
            "password": account["password"],
            "created": time.strftime("%H:%M:%S %d/%m/%Y", time.localtime(account["created_at"])),
        }
    )

@app.get("/new", response_class=HTMLResponse)
async def create_new(request: Request):
    session_key = request.client.host.__hash__()
    if session_key in user_sessions:
        del user_sessions[session_key]

    return RedirectResponse(url="/", status_code=302)

@app.get("/messages", response_class=HTMLResponse)
async def get_messages_html(request: Request, refresh: bool = Query(False)):
    session_key = request.client.host.__hash__()
    account = user_sessions.get(session_key)

    if not account:
        return "<div class='alert alert-warning'>Không có email đang hoạt động. <a href='/'>Tạo mới</a></div>"

    try:
        async with aiohttp.ClientSession() as http:
            headers = {"Authorization": f"Bearer {account['token']}"}
            msgs = await get_messages(http, account["token"])

            messages_html = []
            for msg in sorted(msgs, key=lambda x: x.get("createdAt", ""), reverse=True):
                detail = await get_message_detail(http, msg["id"], account["token"])
                if not detail:
                    continue

                sender = detail.get("from", {}).get("address", "Unknown")
                subject = detail.get("subject", "(Không chủ đề)")
                created = detail.get("createdAt", "").replace("T", " ").split(".")[0]
                body_text = detail.get("text") or "[No text content]"

                if detail.get("html") and not body_text.strip():
                    try:
                        soup = BeautifulSoup(detail["html"], "html.parser")
                        body_text = soup.get_text(separator="\n", strip=True)[:500] + "..."
                    except:
                        pass

                messages_html.append(
                    f"""
                    <div class="mail-item">
                        <div class="mail-header">
                            <strong>Từ:</strong> {sender}<br>
                            <strong>Chủ đề:</strong> {subject}<br>
                            <small>{created}</small>
                        </div>
                        <div class="mail-body">
                            <pre>{body_text}</pre>
                        </div>
                    </div>
                    <hr>
                    """
                )

            if not messages_html:
                return "<p class='text-muted'>Chưa có email nào. Đang chờ...</p>"

            return "\n".join(messages_html)

    except Exception as e:
        return f"<div class='alert alert-danger'>Lỗi: {str(e)}</div>"

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)