import xmlrpc.client
import os

def _get_odoo_service_connection():
    url = os.getenv("ODOO_URL", "").rstrip("/")
    db = os.getenv("ODOO_DB", "odoo")
    username = os.getenv("ODOO_USER", "")
    password = os.getenv("ODOO_API_KEY", "")

    common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common", allow_none=True)
    uid = common.authenticate(db, username, password, {})
    if not uid:
        raise ConnectionError("No se pudo autenticar el servicio contra Odoo.")
    models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object", allow_none=True)
    return url, db, uid, password, models

def get_all_companies():
    _, db, svc_uid, svc_pass, models = _get_odoo_service_connection()
    companies = models.execute_kw(
        db, svc_uid, svc_pass,
        "res.company", "search_read",
        [[]],
        {"fields": ["id", "name"], "order": "id asc"},
    )
    return [{"id": int(c["id"]), "name": c["name"]} for c in companies]

def authenticate_odoo_user(login: str, password: str):
    """
    Valida usuario y contraseña directamente contra Odoo y obtiene sus compañías permitidas.
    """
    url = os.getenv("ODOO_URL", "").rstrip("/")
    db = os.getenv("ODOO_DB", "odoo")

    common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common", allow_none=True)
    user_uid = common.authenticate(db, login.strip(), password, {})
    if not user_uid:
        return None, "Usuario o contraseña de Odoo incorrectos."

    _, _, svc_uid, svc_pass, models = _get_odoo_service_connection()
    users = models.execute_kw(
        db, svc_uid, svc_pass,
        "res.users", "read",
        [[user_uid]],
        {"fields": ["id", "name", "login", "partner_id", "company_id", "company_ids", "groups_id", "share", "active"]},
    )
    if not users:
        return None, "No se encontró el perfil del usuario en Odoo."

    u = users[0]
    if not u.get("active") or u.get("share"):
        return None, "El usuario no es un usuario interno activo de Odoo."

    allowed_ids = [int(cid) for cid in (u.get("company_ids") or [])]
    if not allowed_ids:
        return None, "El usuario no tiene compañías habilitadas en Odoo."

    required_group = os.getenv("ALLOWED_ODOO_GROUP", "").strip()
    if required_group:
        user_groups = models.execute_kw(
            db, svc_uid, svc_pass,
            "res.groups", "read",
            [u.get("groups_id", [])],
            {"fields": ["id", "name", "full_name"]},
        )
        matched = any(
            required_group.lower() in (g.get("name") or "").lower()
            or required_group.lower() in (g.get("full_name") or "").lower()
            or required_group == str(g.get("id"))
            for g in user_groups
        )
        if not matched:
            return None, f"El usuario no pertenece al grupo habilitado ({required_group})."

    companies_records = models.execute_kw(
        db, svc_uid, svc_pass,
        "res.company", "read",
        [allowed_ids],
        {"fields": ["id", "name"]},
    )
    allowed_companies = sorted(
        [{"id": int(c["id"]), "name": c["name"]} for c in companies_records],
        key=lambda x: x["name"],
    )

    default_comp_info = u.get("company_id")
    default_comp_id = int(default_comp_info[0]) if isinstance(default_comp_info, list) else allowed_companies[0]["id"]
    if default_comp_id not in allowed_ids:
        default_comp_id = allowed_companies[0]["id"]

    partner_info = u.get("partner_id")
    return {
        "uid": int(u["id"]),
        "name": u["name"],
        "login": u["login"],
        "partner_id": int(partner_info[0]) if isinstance(partner_info, list) else None,
        "allowed_companies": allowed_companies,
        "default_company_id": default_comp_id,
    }, None

def get_quotation_options(company_id: int, allowed_company_ids: list):
    _, db, uid, password, models = _get_odoo_service_connection()
    ctx = {"allowed_company_ids": allowed_company_ids, "company_id": int(company_id)}

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

    order_types = []
    type_model = so_fields.get("type_id", {}).get("relation")
    if type_model:
        order_types = models.execute_kw(
            db, uid, password,
            type_model, "search_read",
            [["|", ("company_id", "=", False), ("company_id", "=", int(company_id))]],
            {"fields": ["id", "name"], "order": "sequence asc, id asc", "context": ctx},
        )

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
                "|", ("company_id", "=", False), ("company_id", "=", int(company_id)),
            ]],
            {"fields": ["id", "name"], "order": "sequence asc, id asc", "context": ctx},
        )

    return {"order_types": order_types, "routes": routes}

def search_customers(query: str, company_id: int, allowed_company_ids: list, limit: int = 15):
    _, db, uid, password, models = _get_odoo_service_connection()
    ctx = {"allowed_company_ids": allowed_company_ids, "company_id": int(company_id)}
    domain = [
        "|", ("company_id", "=", False), ("company_id", "=", int(company_id)),
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
    company_id: int,
    allowed_company_ids: list,
    partner_id: int,
    lines: list,
    creator_user: dict,
    type_id: int = None,
    route_id: int = None,
    purchase_order_number: str = "",
    client_order_ref: str = "",
):
    url, db, uid, password, models = _get_odoo_service_connection()
    ctx = {"allowed_company_ids": allowed_company_ids, "company_id": int(company_id)}

    so_fields = models.execute_kw(
        db, uid, password,
        "sale.order", "fields_get",
        [["type_id", "route_id", "purchase_order_number", "x_studio_purchase_order_number"]],
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
        line_vals = {
            "product_id": int(line["product_id"]),
            "product_uom_qty": float(line["qty"]),
            "price_unit": float(line["price_unit"]),
            "discount": float(line.get("discount", 0.0)),
        }
        if line.get("name"):
            line_vals["name"] = str(line["name"])

        if route_id and "route_id" in sol_fields:
            line_vals["route_id"] = int(route_id)

        order_lines.append((0, 0, line_vals))

    # NO enviamos user_id para respetar el vendedor asignado en la ficha del cliente
    order_vals = {
        "partner_id": int(partner_id),
        "company_id": int(company_id),
        "state": "draft",
        "order_line": order_lines,
    }

    if type_id and "type_id" in so_fields:
        order_vals["type_id"] = int(type_id)

    if route_id and "route_id" in so_fields:
        order_vals["route_id"] = int(route_id)

    if purchase_order_number.strip():
        po_val = purchase_order_number.strip()
        if "purchase_order_number" in so_fields:
            order_vals["purchase_order_number"] = po_val
        elif "x_studio_purchase_order_number" in so_fields:
            order_vals["x_studio_purchase_order_number"] = po_val

    if client_order_ref.strip():
        order_vals["client_order_ref"] = client_order_ref.strip()

    order_id = models.execute_kw(
        db, uid, password,
        "sale.order", "create",
        [order_vals],
        {"context": ctx},
    )

    # Registrar en el Chatter de Odoo quién creó la cotización sin alterar el vendedor
    creator_name = creator_user.get("name", "Usuario Web")
    creator_login = creator_user.get("login", "")
    creator_partner_id = creator_user.get("partner_id")

    post_kwargs = {
        "body": f"<p>Cotización creada desde <strong>Cotizador Web</strong> por: <strong>{creator_name}</strong> ({creator_login})</p>",
        "message_type": "comment",
        "subtype_xmlid": "mail.mt_note",
        "context": ctx,
    }
    if creator_partner_id:
        post_kwargs["author_id"] = int(creator_partner_id)

    try:
        models.execute_kw(db, uid, password, "sale.order", "message_post", [[order_id]], post_kwargs)
    except Exception:
        pass

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