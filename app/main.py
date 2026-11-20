import os
import time
import threading
from fastapi import FastAPI, Request, Form, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel
from typing import List
from dotenv import load_dotenv

from app.database import (
    init_db,
    query_products_datatables,
    get_sync_status,
    log_quotation,
)
from app.odoo_sync import run_full_sync
from app.odoo_client import search_customers, create_draft_quotation

load_dotenv()

app = FastAPI(title="Cotizador Odoo")
SECRET_KEY = os.getenv("SECRET_KEY", "cambia-esta-clave-secreta-en-coolify")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, max_age=28800, https_only=False)

templates = Jinja2Templates(directory="app/templates")

# Protección simple contra fuerza bruta en login
login_attempts = {}

@app.on_event("startup")
def startup_event():
    init_db()

def get_current_user(request: Request):
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="No autenticado")
    return user

# --- RUTAS DE AUTENTICACIÓN ---
@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if request.session.get("user"):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})

@app.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    client_ip = request.client.host if request.client else "unknown"
    now = time.time()
    attempts = [t for t in login_attempts.get(client_ip, []) if now - t < 300]
    if len(attempts) >= 5:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Demasiados intentos fallidos. Esperá 5 minutos."},
            status_code=429,
        )

    expected_user = os.getenv("APP_USER", "admin")
    expected_pass = os.getenv("APP_PASSWORD", "")

    if username == expected_user and expected_pass and password == expected_pass:
        login_attempts.pop(client_ip, None)
        request.session["user"] = username
        return RedirectResponse(url="/", status_code=302)

    attempts.append(now)
    login_attempts[client_ip] = attempts
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": "Usuario o contraseña incorrectos."},
        status_code=401,
    )

@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)

# --- VISTA PRINCIPAL ---
@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    user = request.session.get("user")
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "user": user,
            "odoo_url": os.getenv("ODOO_URL", "").rstrip("/"),
        },
    )

# --- API DE PRODUCTOS (DATATABLES SERVER-SIDE) ---
@app.get("/api/products")
def api_products(request: Request, user: str = Depends(get_current_user)):
    params = request.query_params
    draw = int(params.get("draw", 1))
    start = int(params.get("start", 0))
    length = min(int(params.get("length", 25)), 200)
    search_value = params.get("search[value]", "")
    order_col_idx = int(params.get("order[0][column]", 0))
    order_dir = params.get("order[0][dir]", "asc")
    only_in_stock = params.get("only_in_stock", "false").lower() == "true"

    result = query_products_datatables(
        start=start,
        length=length,
        search_value=search_value,
        order_col_idx=order_col_idx,
        order_dir=order_dir,
        only_in_stock=only_in_stock,
    )
    return JSONResponse({
        "draw": draw,
        "recordsTotal": result["recordsTotal"],
        "recordsFiltered": result["recordsFiltered"],
        "data": result["data"],
    })

# --- API DE SINCRONIZACIÓN ---
@app.get("/api/sync/status")
def api_sync_status(user: str = Depends(get_current_user)):
    return JSONResponse(get_sync_status())

@app.post("/api/sync")
def api_trigger_sync(user: str = Depends(get_current_user)):
    current = get_sync_status()
    if current.get("status") == "running":
        return JSONResponse({"ok": False, "message": "Ya hay una sincronización en curso."})
    thread = threading.Thread(target=run_full_sync, daemon=True)
    thread.start()
    return JSONResponse({"ok": True, "message": "Sincronización iniciada en segundo plano."})

# --- API DE CLIENTES Y COTIZACIONES ---
@app.get("/api/customers")
def api_customers(q: str = "", user: str = Depends(get_current_user)):
    if len(q.strip()) < 2:
        return JSONResponse([])
    try:
        partners = search_customers(q.strip())
        return JSONResponse(partners)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class QuotationLineIn(BaseModel):
    product_id: int
    qty: float
    price_unit: float
    discount: float = 0.0
    tax_ids: List[int] = []

class QuotationCreateIn(BaseModel):
    partner_id: int
    customer_name: str
    warehouse_id: int
    client_order_ref: str = ""
    lines: List[QuotationLineIn]

@app.post("/api/quotations")
def api_create_quotation(payload: QuotationCreateIn, user: str = Depends(get_current_user)):
    if not payload.lines:
        raise HTTPException(status_code=400, detail="La cotización debe tener al menos un producto.")
    try:
        res = create_draft_quotation(
            partner_id=payload.partner_id,
            warehouse_id=payload.warehouse_id,
            lines=[line.model_dump() for line in payload.lines],
            client_order_ref=payload.client_order_ref,
        )
        log_quotation(
            username=user,
            order_id=res["order_id"],
            order_name=res["order_name"],
            customer_name=payload.customer_name,
            warehouse_id=payload.warehouse_id,
            items_count=len(payload.lines),
            amount_total=res["amount_total"],
        )
        return JSONResponse({"ok": True, **res})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al crear cotización en Odoo: {str(e)}")