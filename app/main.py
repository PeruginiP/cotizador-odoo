import os
import time
import threading
from fastapi import FastAPI, Request, Form, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel
from typing import List, Optional
from dotenv import load_dotenv

from app.database import (
    init_db,
    query_products_datatables,
    get_sync_status,
    log_quotation,
)
from app.odoo_sync import run_full_sync
from app.odoo_client import (
    authenticate_odoo_user,
    search_customers,
    create_draft_quotation,
    get_quotation_options,
)

load_dotenv()

app = FastAPI(title="Cotizador Odoo")
SECRET_KEY = os.getenv("SECRET_KEY", "cambia-esta-clave-secreta-en-coolify")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, max_age=28800, https_only=False)

templates = Jinja2Templates(directory="app/templates")

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
    if len(attempts) >= 6:
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Demasiados intentos fallidos. Esperá 5 minutos."},
            status_code=429,
        )

    # 1. Validar contra usuarios reales de Odoo
    odoo_user, error_msg = authenticate_odoo_user(username, password)
    if odoo_user:
        login_attempts.pop(client_ip, None)
        request.session["user"] = odoo_user
        return RedirectResponse(url="/", status_code=302)

    # 2. Respaldo de administrador local si está configurado en Coolify
    expected_user = os.getenv("APP_USER", "")
    expected_pass = os.getenv("APP_PASSWORD", "")
    if expected_user and expected_pass and username == expected_user and password == expected_pass:
        login_attempts.pop(client_ip, None)
        request.session["user"] = {
            "uid": 0,
            "name": f"Admin ({username})",
            "login": username,
            "partner_id": None,
        }
        return RedirectResponse(url="/", status_code=302)

    attempts.append(now)
    login_attempts[client_ip] = attempts
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": error_msg or "Usuario o contraseña incorrectos."},
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
def api_products(request: Request, user: dict = Depends(get_current_user)):
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
def api_sync_status(user: dict = Depends(get_current_user)):
    return JSONResponse(get_sync_status())

@app.post("/api/sync")
def api_trigger_sync(user: dict = Depends(get_current_user)):
    current = get_sync_status()
    if current.get("status") == "running":
        return JSONResponse({"ok": False, "message": "Ya hay una sincronización en curso."})
    thread = threading.Thread(target=run_full_sync, daemon=True)
    thread.start()
    return JSONResponse({"ok": True, "message": "Sincronización iniciada en segundo plano."})

# --- API DE OPCIONES, CLIENTES Y COTIZACIONES ---
@app.get("/api/quotation-options")
def api_quotation_options(user: dict = Depends(get_current_user)):
    try:
        return JSONResponse(get_quotation_options())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/customers")
def api_customers(q: str = "", user: dict = Depends(get_current_user)):
    if len(q.strip()) < 2:
        return JSONResponse([])
    try:
        partners = search_customers(q.strip())
        return JSONResponse(partners)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class QuotationLineIn(BaseModel):
    product_id: int
    name: str = ""
    qty: float
    price_unit: float
    discount: float = 0.0
    tax_ids: List[int] = []

class QuotationCreateIn(BaseModel):
    partner_id: int
    customer_name: str
    type_id: Optional[int] = None
    route_id: Optional[int] = None
    purchase_order_number: str = ""
    client_order_ref: str = ""
    lines: List[QuotationLineIn]

@app.post("/api/quotations")
def api_create_quotation(payload: QuotationCreateIn, user: dict = Depends(get_current_user)):
    if not payload.lines:
        raise HTTPException(status_code=400, detail="La cotización debe tener al menos un producto.")
    try:
        res = create_draft_quotation(
            partner_id=payload.partner_id,
            lines=[line.model_dump() for line in payload.lines],
            creator_user=user,
            type_id=payload.type_id,
            route_id=payload.route_id,
            purchase_order_number=payload.purchase_order_number,
            client_order_ref=payload.client_order_ref,
        )
        log_quotation(
            username=user.get("login", "desconocido"),
            order_id=res["order_id"],
            order_name=res["order_name"],
            customer_name=payload.customer_name,
            warehouse_id=payload.type_id or 0,
            items_count=len(payload.lines),
            amount_total=res["amount_total"],
        )
        return JSONResponse({"ok": True, **res})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al crear cotización en Odoo: {str(e)}")