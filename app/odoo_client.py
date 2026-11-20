import xmlrpc.client
import os

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
    return url, db, uid, password, models

def search_customers(query: str, limit: int = 15):
    _, db, uid, password, models = _get_odoo_connection()
    domain = ["|", "|", ("name", "ilike", query), ("vat", "ilike", query), ("email", "ilike", query)]
    partners = models.execute_kw(
        db, uid, password,
        "res.partner", "search_read",
        [domain],
        {
            "fields": ["id", "name", "vat", "city"],
            "limit": limit,
            "context": {"allowed_company_ids": [1], "company_id": 1},
        },
    )
    return partners

def create_draft_quotation(partner_id: int, warehouse_id: int, lines: list, client_order_ref: str = ""):
    url, db, uid, password, models = _get_odoo_connection()
    ctx = {"allowed_company_ids": [1], "company_id": 1}

    order_lines = []
    for line in lines:
        tax_ids = [int(t) for t in line.get("tax_ids", [])]
        order_lines.append((0, 0, {
            "product_id": int(line["product_id"]),
            "product_uom_qty": float(line["qty"]),
            "price_unit": float(line["price_unit"]),
            "discount": float(line.get("discount", 0.0)),
            "tax_id": [(6, 0, tax_ids)],
        }))

    order_vals = {
        "partner_id": int(partner_id),
        "company_id": 1,
        "warehouse_id": int(warehouse_id),
        "state": "draft",
        "order_line": order_lines,
    }
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