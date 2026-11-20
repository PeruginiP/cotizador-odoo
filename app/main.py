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
    get_all_warehouses,
    get_products_by_ids,
    compute_prices_from_local_pricelist,
    log_quotation,
)
from app.odoo_sync import run_full_sync
from app.odoo_client import (
    authenticate_odoo_user,
    get_all_companies,
    search_customers,
    get_customer_details,
    get_single_partner_card,
    search_quotations,
    get_quotation_details,
    save_quotation,
    get_quotation_options,
    compute_pricelist_prices_odoo_fallback,
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

def validate_user_company(user: dict, company_id: int) -> list:
    allowed = [int(c["id"]) for c in user.get("allowed_companies", [])]
    if company_id not in allowed:
        raise HTTPException(status_code=403, detail="No tenés permisos sobre la compañía seleccionada.")
    return allowed

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

    odoo_user, error_msg = authenticate_odoo_user(username, password)
    if odoo_user:
        login_attempts.pop(client_ip, None)
        request.session["user"] = odoo_user
        return RedirectResponse(url="/", status_code=302)

    expected_user = os.getenv("APP_USER", "")
    expected_pass = os.getenv("APP_PASSWORD", "")
    if expected_user and expected_pass and username == expected_user and password == expected_pass:
        login_attempts.pop(client_ip, None)
        all_comps = get_all_companies()
        request.session["user"] = {
            "uid": 0,
            "name": f"Admin ({username})",
            "login": username,
            "partner_id": None,
            "allowed_companies": all_comps,
            "default_company_id": all_comps[0]["id"] if all_comps else 1,
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
            "warehouses": get_all_warehouses(),
            "odoo_url": os.getenv("ODOO_URL", "").rstrip("/"),
        },
    )

@app.get("/api/warehouses")
def api_warehouses(user: dict = Depends(get_current_user)):
    return JSONResponse(get_all_warehouses())

# --- API DE PRODUCTOS (DATATABLES SERVER-SIDE) ---
@app.get("/api/products")
def api_products(request: Request, user: dict = Depends(get_current_user)):
    params = request.query_params
    draw = int(params.get("draw", 1))
    start = int(params.get("start", 0))
    length = min(int(params.get("length", 25)), 200)
    search_value = params.get("search[value]", "")
    order_col_idx = int(params.get("order[0][column]", 1))
    order_dir = params.get("order[0][dir]", "asc")
    only_in_stock = params.get("only_in_stock", "false").lower() == "true"
    company_id = int(params.get("company_id", user.get("default_company_id", 1)))

    validate_user_company(user, company_id)

    result = query_products_datatables(
        start=start,
        length=length,
        search_value=search_value,
        order_col_idx=order_col_idx,
        order_dir=order_dir,
        company_id=company_id,
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

# --- API DE OPCIONES, PRECIOS DE LISTA, CLIENTES Y COTIZACIONES ---
@app.get("/api/quotation-options")
def api_quotation_options(company_id: int, user: dict = Depends(get_current_user)):
    allowed = validate_user_company(user, company_id)
    try:
        return JSONResponse(get_quotation_options(company_id, allowed))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class PricelistItemIn(BaseModel):
    product_id: int
    qty: float = 1.0

class PricelistComputeIn(BaseModel):
    company_id: int
    pricelist_id: int
    partner_id: Optional[int] = None
    items: List[PricelistItemIn]

@app.post("/api/pricelist-prices")
def api_pricelist_prices(payload: PricelistComputeIn, user: dict = Depends(get_current_user)):
    allowed = validate_user_company(user, payload.company_id)
    items_list = [it.model_dump() for it in payload.items]
    try:
        # 1. Intentar cálculo local instantáneo desde SQLite (listas + reglas + tipos de cambio descargados)
        local_prices = compute_prices_from_local_pricelist(
            company_id=payload.company_id,
            pricelist_id=payload.pricelist_id,
            items=items_list,
        )
        if local_prices is not None:
            return JSONResponse({"prices": local_prices, "source": "sqlite"})

        # 2. Fallback directo a Odoo si aún no se ejecutó la sincronización de listas de precios
        fallback_prices = compute_pricelist_prices_odoo_fallback(
            company_id=payload.company_id,
            allowed_company_ids=allowed,
            pricelist_id=payload.pricelist_id,
            items=items_list,
            partner_id=payload.partner_id,
        )
        return JSONResponse({"prices": fallback_prices, "source": "odoo"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/customers")
def api_customers(company_id: int, q: str = "", include_children: bool = False, user: dict = Depends(get_current_user)):
    allowed = validate_user_company(user, company_id)
    if len(q.strip()) < 2:
        return JSONResponse([])
    try:
        partners = search_customers(q.strip(), company_id, allowed, include_children=include_children)
        return JSONResponse(partners)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/customers/{partner_id}")
def api_customer_details(partner_id: int, company_id: int, user: dict = Depends(get_current_user)):
    allowed = validate_user_company(user, company_id)
    try:
        details = get_customer_details(partner_id, company_id, allowed)
        return JSONResponse(details)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/partners/{partner_id}/card")
def api_single_partner_card(partner_id: int, company_id: int, user: dict = Depends(get_current_user)):
    allowed = validate_user_company(user, company_id)
    try:
        card = get_single_partner_card(partner_id, company_id, allowed)
        return JSONResponse(card)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/quotations/search")
def api_search_quotations(company_id: int, q: str = "", user: dict = Depends(get_current_user)):
    allowed = validate_user_company(user, company_id)
    try:
        orders = search_quotations(q, company_id, allowed)
        return JSONResponse(orders)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/quotations/{order_id}")
def api_get_quotation(order_id: int, company_id: int, user: dict = Depends(get_current_user)):
    allowed = validate_user_company(user, company_id)
    try:
        q_data = get_quotation_details(order_id, company_id, allowed)
        prod_ids = [ln["product_id"] for ln in q_data["lines"] if ln.get("row_type") == "product" and ln.get("product_id")]
        local_prods = get_products_by_ids(prod_ids)

        for ln in q_data["lines"]:
            if ln.get("row_type") == "product":
                pid = ln.get("product_id")
                lp = local_prods.get(pid, {})
                lbl = ln.get("product_label") or ""
                code_fallback = lbl.split("]")[0].lstrip("[") if lbl.startswith("[") else "S/C"
                ln["default_code"] = lp.get("default_code") or code_fallback
                ln["brand"] = lp.get("brand") or "-"
                ln["stocks"] = lp.get("stocks") or {}

        return JSONResponse(q_data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class QuotationLineIn(BaseModel):
    line_id: Optional[int] = None
    row_type: str = "product"
    product_id: Optional[int] = None
    name: str = ""
    qty: float = 1.0
    price_unit: float = 0.0
    discount: float = 0.0

class QuotationSaveIn(BaseModel):
    order_id: Optional[int] = None
    original_line_ids: List[int] = []
    company_id: int
    partner_id: int
    partner_invoice_id: Optional[int] = None
    partner_shipping_id: Optional[int] = None
    pricelist_id: Optional[int] = None
    customer_name: str
    type_id: Optional[int] = None
    route_id: Optional[int] = None
    purchase_order_number: str = ""
    client_order_ref: str = ""
    lines: List[QuotationLineIn]

@app.post("/api/quotations")
def api_save_quotation(payload: QuotationSaveIn, user: dict = Depends(get_current_user)):
    allowed = validate_user_company(user, payload.company_id)
    valid_lines = [
        ln.model_dump()
        for ln in payload.lines
        if (ln.row_type == "product" and ln.product_id is not None)
        or (ln.row_type in ("section", "note") and ln.name.strip())
    ]
    if not valid_lines:
        raise HTTPException(status_code=400, detail="La cotización debe tener al menos un producto o línea válida.")

    try:
        res = save_quotation(
            company_id=payload.company_id,
            allowed_company_ids=allowed,
            partner_id=payload.partner_id,
            partner_invoice_id=payload.partner_invoice_id or payload.partner_id,
            partner_shipping_id=payload.partner_shipping_id or payload.partner_id,
            lines=valid_lines,
            creator_user=user,
            pricelist_id=payload.pricelist_id,
            order_id=payload.order_id,
            original_line_ids=payload.original_line_ids,
            type_id=payload.type_id,
            route_id=payload.route_id,
            purchase_order_number=payload.purchase_order_number,
            client_order_ref=payload.client_order_ref,
        )
        prod_count = sum(1 for ln in valid_lines if ln["row_type"] == "product")
        log_quotation(
            username=user.get("login", "desconocido"),
            company_id=payload.company_id,
            order_id=res["order_id"],
            order_name=res["order_name"],
            customer_name=payload.customer_name,
            items_count=prod_count,
            amount_total=res["amount_total"],
        )
        return JSONResponse({"ok": True, **res})
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al guardar cotización en Odoo: {str(e)}")