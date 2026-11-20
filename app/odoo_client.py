import xmlrpc.client
import os

COMPANY_ID = 9
ALLOWED_COMPANIES = [9, 1]

def _get_odoo_connection():
    url = os.getenv("ODOO_URL", "").rstrip("/")
    db = os.getenv("ODOO_DB", "odoo")
    username = os.getenv("ODOO_USER", "")
    password = os.getenv("ODOO_API_KEY", "")

    common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common", allow_none=True)
    uid = common.authenticate(db, username, password, {})
    if not uid:
        raise ConnectionError("No se pudo autenticar contra Odoo.")
    models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object", allow_none=True)
    ctx = {"allowed_company_ids": ALLOWED_COMPANIES, "company_id": COMPANY_ID}
    return url, db, uid, password, models, ctx

def get_quotation_options():
    _, db, uid, password, models, ctx = _get_odoo_connection()

    so_fields = models.execute_kw(
        db, uid, password,
        "sale.order", "fields_get",
        [["type_id", "route_id"]],
        {"attributes": ["type", "relation"]},
    )
    sol_fields = models.execute_kw(
        db, uid, password,
        "sale.order.line", "fields_get",
        [["route_id"]],
        {"attributes": ["type", "relation"]},
    )

    # 1. Tipos de pedido (sale.order.type) para Compañía 9 o globales
    order_types = []
    type_model = so_fields.get("type_id", {}).get("relation")
    if type_model:
        order_types = models.execute_kw(
            db, uid, password,
            type_model, "search_read",
            [["|", ("company_id", "=", False), ("company_id", "=", COMPANY_ID)]],
            {"fields": ["id", "name"], "order": "sequence asc, id asc", "context": ctx},
        )

    # 2. Rutas seleccionables en venta (stock.route) para Compañía 9 o globales
    routes = []
    route_model = (
        sol_fields.get("route_id", {}).get("relation")
        or so_fields.get("route_id", {}).get("relation")
    )
    if route_model:
        routes = models.execute_kw(
            db, uid, password,
            route_model, "search_read",
            [[
                ("sale_selectable", "=", True),
                "|", ("company_id", "=", False), ("company_id", "=", COMPANY_ID),
            ]],
            {"fields": ["id", "name"], "order": "sequence asc, id asc", "context": ctx},
        )

    return {"order_types": order_types, "routes": routes}

def search_customers(query: str, limit: int = 15):
    _, db, uid, password, models, ctx = _get_odoo_connection()
    domain = [
        "|", ("company_id", "=", False), ("company_id", "=", COMPANY_ID),
        "|", "|", ("name", "ilike", query), ("vat", "ilike", query), ("email", "ilike", query),
    ]
    partners = models.execute_kw(
        db, uid, password,
        "res.partner", "search_read",
        [domain],
        {
            "fields": ["id", "name", "vat", "city"],
            "limit": limit,
            "context": ctx,
        },
    )
    return partners

def create_draft_quotation(
    partner_id: int,
    lines: list,
    type_id: int = None,
    route_id: int = None,
    client_order_ref: str = "",
):
    url, db, uid, password, models, ctx = _get_odoo_connection()

    so_fields = models.execute_kw(
        db, uid, password,
        "sale.order", "fields_get",
        [["type_id", "route_id"]],
        {"attributes": ["type"]},
    )
    sol_fields = models.execute_kw(
        db, uid, password,
        "sale.order.line", "fields_get",
        [["route_id"]],
        {"attributes": ["type"]},
    )

    order_lines = []
    for line in lines:
        # Omitimos tax_id para que Odoo 16 asigne automáticamente los impuestos de la Compañía 9
        line_vals = {
            "product_id": int(line["product_id"]),
            "product_uom_qty": float(line["qty"]),
            "price_unit": float(line["price_unit"]),
            "discount": float(line.get("discount", 0.0)),
        }
        if route_id and "route_id" in sol_fields:
            line_vals["route_id"] = int(route_id)

        order_lines.append((0, 0, line_vals))

    order_vals = {
        "partner_id": int(partner_id),
        "company_id": COMPANY_ID,
        "state": "draft",
        "order_line": order_lines,
    }

    if type_id and "type_id" in so_fields:
        order_vals["type_id"] = int(type_id)

    # Si existe route_id en cabecera de sale.order, también lo asignamos allí
    if route_id and "route_id" in so_fields:
        order_vals["route_id"] = int(route_id)

    if client_order_ref.strip():
        order_vals["client_order_ref"] = client_order_ref.strip()

    order_id = models.execute_kw(
        db, uid, password,
        "sale.order", "create",
        [order_vals],
        {"context": ctx},
    )

    created = models.execute_kw(
        db, uid, password,
        "sale.order", "read",
        [[order_id]],
        {"fields": ["name", "amount_total"], "context": ctx},
    )[0]

    return {
        "order_id": order_id,
        "order_name": created["name"],
        "amount_total": created["amount_total"],
        "odoo_link": f"{url}/web#id={order_id}&model=sale.order&view_type=form",
    }