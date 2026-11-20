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

def _format_partner_address(p: dict) -> dict:
    parts_line1 = [x for x in [p.get("street"), p.get("street2")] if x]
    city_zip = " ".join(x for x in [p.get("zip"), p.get("city")] if x)
    state_name = p["state_id"][1] if isinstance(p.get("state_id"), list) else ""
    country_name = p["country_id"][1] if isinstance(p.get("country_id"), list) else ""
    parts_line2 = [x for x in [city_zip, state_name, country_name] if x]

    lines = []
    if parts_line1:
        lines.append(", ".join(parts_line1))
    if parts_line2:
        lines.append(", ".join(parts_line2))

    type_labels = {
        "contact": "Principal",
        "invoice": "Facturación",
        "delivery": "Entrega",
        "other": "Otro",
        "private": "Privado",
    }
    ptype = p.get("type") or "contact"
    internal_ref = p.get("internal_code") or p.get("ref") or ""

    return {
        "id": int(p["id"]),
        "name": p.get("name") or "",
        "type": ptype,
        "type_label": type_labels.get(ptype, ptype),
        "vat": p.get("vat") or "",
        "internal_code": internal_ref,
        "phone": p.get("phone") or p.get("mobile") or "",
        "email": p.get("email") or "",
        "address_text": " — ".join(lines) if lines else "Sin dirección cargada",
        "address_lines": lines,
    }

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

def _get_partner_code_fields(models, db, uid, password):
    p_meta = models.execute_kw(
        db, uid, password,
        "res.partner", "fields_get",
        [["ref", "internal_code", "x_studio_internal_code"]],
        {"attributes": ["type"]},
    )
    return [f for f in ["internal_code", "ref", "x_studio_internal_code"] if f in p_meta]

def search_customers(query: str, company_id: int, allowed_company_ids: list, limit: int = 20):
    _, db, uid, password, models = _get_odoo_service_connection()
    ctx = {"allowed_company_ids": allowed_company_ids, "company_id": int(company_id)}

    code_fields = _get_partner_code_fields(models, db, uid, password)
    search_fields = ["name", "vat"] + code_fields

    or_clauses = []
    for f in search_fields:
        or_clauses.append((f, "ilike", query))

    # Construir árbol OR en notación polaca de Odoo
    or_domain = ["|"] * (len(or_clauses) - 1) + or_clauses

    domain = [
        "&",
        "|", ("company_id", "=", False), ("company_id", "=", int(company_id)),
        ("parent_id", "=", False),
    ] + or_domain

    read_fields = ["id", "name", "vat", "city", "street"] + code_fields
    partners = models.execute_kw(
        db, uid, password,
        "res.partner", "search_read",
        [domain],
        {
            "fields": read_fields,
            "limit": limit,
            "context": ctx,
        },
    )

    # Si no trajo resultados filtrando parent_id=False (por si buscan una sucursal), buscar sin restricción de parent_id
    if not partners:
        fallback_domain = [
            "|", ("company_id", "=", False), ("company_id", "=", int(company_id)),
        ] + or_domain
        partners = models.execute_kw(
            db, uid, password,
            "res.partner", "search_read",
            [fallback_domain],
            {"fields": read_fields, "limit": limit, "context": ctx},
        )

    for p in partners:
        p["internal_code"] = p.get("internal_code") or p.get("ref") or p.get("x_studio_internal_code") or ""
    return partners

def get_customer_details(partner_id: int, company_id: int, allowed_company_ids: list, extra_address_ids: list = None):
    _, db, uid, password, models = _get_odoo_service_connection()
    ctx = {"allowed_company_ids": allowed_company_ids, "company_id": int(company_id)}
    code_fields = _get_partner_code_fields(models, db, uid, password)
    addr_fields = [
        "id", "name", "type", "street", "street2", "zip", "city",
        "state_id", "country_id", "vat", "phone", "mobile", "email", "commercial_partner_id"
    ] + code_fields

    main_list = models.execute_kw(
        db, uid, password,
        "res.partner", "read",
        [[int(partner_id)]],
        {"fields": addr_fields, "context": ctx},
    )
    if not main_list:
        raise ValueError("Cliente no encontrado en Odoo.")
    main_p = main_list[0]

    # Obtener direcciones por defecto según lógica nativa de Odoo
    default_addrs = models.execute_kw(
        db, uid, password,
        "res.partner", "address_get",
        [[int(partner_id)], ["invoice", "delivery"]],
        {"context": ctx},
    )
    default_invoice_id = int(default_addrs.get("invoice") or partner_id)
    default_delivery_id = int(default_addrs.get("delivery") or partner_id)

    comm_id = main_p["commercial_partner_id"][0] if isinstance(main_p.get("commercial_partner_id"), list) else int(partner_id)

    related_partners = models.execute_kw(
        db, uid, password,
        "res.partner", "search_read",
        [[
            "|", "|",
            ("id", "in", list({int(partner_id), comm_id, default_invoice_id, default_delivery_id} | set(extra_address_ids or []))),
            ("parent_id", "=", comm_id),
            ("commercial_partner_id", "=", comm_id),
        ]],
        {"fields": addr_fields, "context": ctx},
    )

    formatted_list = [_format_partner_address(rp) for rp in related_partners]
    # Ordenar poniendo primero al cliente elegido y luego por tipo
    formatted_list.sort(key=lambda x: (0 if x["id"] == int(partner_id) else 1, x["type"], x["name"]))

    return {
        "customer": _format_partner_address(main_p),
        "default_invoice_id": default_invoice_id,
        "default_delivery_id": default_delivery_id,
        "addresses": formatted_list,
    }

def search_quotations(query: str, company_id: int, allowed_company_ids: list, limit: int = 20):
    _, db, uid, password, models = _get_odoo_service_connection()
    ctx = {"allowed_company_ids": allowed_company_ids, "company_id": int(company_id)}

    so_meta = models.execute_kw(
        db, uid, password,
        "sale.order", "fields_get",
        [["purchase_order_number", "x_studio_purchase_order_number"]],
        {"attributes": ["type"]},
    )

    domain = [
        ("company_id", "=", int(company_id)),
        ("state", "in", ["draft", "sent"]),
    ]

    if query.strip():
        q = query.strip()
        or_clauses = [
            ("name", "ilike", q),
            ("partner_id.name", "ilike", q),
            ("client_order_ref", "ilike", q),
        ]
        if "purchase_order_number" in so_meta:
            or_clauses.append(("purchase_order_number", "ilike", q))
        elif "x_studio_purchase_order_number" in so_meta:
            or_clauses.append(("x_studio_purchase_order_number", "ilike", q))

        domain += ["|"] * (len(or_clauses) - 1) + or_clauses

    orders = models.execute_kw(
        db, uid, password,
        "sale.order", "search_read",
        [domain],
        {
            "fields": ["id", "name", "date_order", "partner_id", "amount_total", "state", "client_order_ref"],
            "order": "id desc",
            "limit": limit,
            "context": ctx,
        },
    )
    return orders

def get_quotation_details(order_id: int, company_id: int, allowed_company_ids: list):
    _, db, uid, password, models = _get_odoo_service_connection()
    ctx = {"allowed_company_ids": allowed_company_ids, "company_id": int(company_id)}

    so_meta = models.execute_kw(
        db, uid, password,
        "sale.order", "fields_get",
        [["type_id", "route_id", "purchase_order_number", "x_studio_purchase_order_number"]],
        {"attributes": ["type"]},
    )

    read_fields = [
        "id", "name", "state", "company_id",
        "partner_id", "partner_invoice_id", "partner_shipping_id",
        "client_order_ref", "order_line",
    ]
    for opt_f in ["type_id", "route_id", "purchase_order_number", "x_studio_purchase_order_number"]:
        if opt_f in so_meta:
            read_fields.append(opt_f)

    orders = models.execute_kw(
        db, uid, password,
        "sale.order", "read",
        [[int(order_id)]],
        {"fields": read_fields, "context": ctx},
    )
    if not orders:
        raise ValueError("Cotización no encontrada.")
    o = orders[0]

    partner_id = int(o["partner_id"][0])
    inv_id = int(o["partner_invoice_id"][0]) if isinstance(o.get("partner_invoice_id"), list) else partner_id
    ship_id = int(o["partner_shipping_id"][0]) if isinstance(o.get("partner_shipping_id"), list) else partner_id

    customer_bundle = get_customer_details(
        partner_id, company_id, allowed_company_ids, extra_address_ids=[inv_id, ship_id]
    )

    line_ids = o.get("order_line") or []
    lines_out = []
    if line_ids:
        raw_lines = models.execute_kw(
            db, uid, password,
            "sale.order.line", "read",
            [line_ids],
            {
                "fields": ["id", "sequence", "display_type", "product_id", "name", "product_uom_qty", "price_unit", "discount"],
                "context": ctx,
            },
        )
        raw_lines.sort(key=lambda x: (x.get("sequence", 10), x["id"]))

        for rl in raw_lines:
            dtype = rl.get("display_type") or False
            if dtype == "line_section":
                lines_out.append({
                    "line_id": int(rl["id"]),
                    "row_type": "section",
                    "name": rl.get("name") or "",
                })
            elif dtype == "line_note":
                lines_out.append({
                    "line_id": int(rl["id"]),
                    "row_type": "note",
                    "name": rl.get("name") or "",
                })
            else:
                prod_info = rl.get("product_id")
                lines_out.append({
                    "line_id": int(rl["id"]),
                    "row_type": "product",
                    "product_id": int(prod_info[0]) if isinstance(prod_info, list) else None,
                    "product_label": prod_info[1] if isinstance(prod_info, list) else "",
                    "name": rl.get("name") or "",
                    "qty": float(rl.get("product_uom_qty") or 1.0),
                    "price_unit": float(rl.get("price_unit") or 0.0),
                    "discount": float(rl.get("discount") or 0.0),
                })

    po_val = o.get("purchase_order_number") or o.get("x_studio_purchase_order_number") or ""

    return {
        "order_id": int(o["id"]),
        "order_name": o["name"],
        "state": o["state"],
        "partner_id": partner_id,
        "partner_invoice_id": inv_id,
        "partner_shipping_id": ship_id,
        "customer_bundle": customer_bundle,
        "type_id": int(o["type_id"][0]) if isinstance(o.get("type_id"), list) else None,
        "route_id": int(o["route_id"][0]) if isinstance(o.get("route_id"), list) else None,
        "purchase_order_number": po_val,
        "client_order_ref": o.get("client_order_ref") or "",
        "lines": lines_out,
    }

def save_quotation(
    company_id: int,
    allowed_company_ids: list,
    partner_id: int,
    partner_invoice_id: int,
    partner_shipping_id: int,
    lines: list,
    creator_user: dict,
    order_id: int = None,
    original_line_ids: list = None,
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

    order_line_commands = []
    kept_line_ids = set()

    for idx, line in enumerate(lines):
        seq = (idx + 1) * 10
        row_type = line.get("row_type", "product")
        existing_lid = line.get("line_id")

        if row_type == "section":
            vals = {
                "display_type": "line_section",
                "name": str(line.get("name") or "Sección"),
                "sequence": seq,
            }
        elif row_type == "note":
            vals = {
                "display_type": "line_note",
                "name": str(line.get("name") or "Nota"),
                "sequence": seq,
            }
        else:
            vals = {
                "display_type": False,
                "product_id": int(line["product_id"]),
                "product_uom_qty": float(line["qty"]),
                "price_unit": float(line["price_unit"]),
                "discount": float(line.get("discount", 0.0)),
                "sequence": seq,
            }
            if line.get("name"):
                vals["name"] = str(line["name"])
            if route_id and "route_id" in sol_fields:
                vals["route_id"] = int(route_id)

        if existing_lid:
            lid_int = int(existing_lid)
            kept_line_ids.add(lid_int)
            order_line_commands.append((1, lid_int, vals))
        else:
            order_line_commands.append((0, 0, vals))

    # Eliminar en Odoo las líneas que existían previamente y fueron quitadas de la grilla
    if order_id and original_line_ids:
        for old_id in original_line_ids:
            if int(old_id) not in kept_line_ids:
                order_line_commands.append((2, int(old_id), 0))

    order_vals = {
        "partner_id": int(partner_id),
        "partner_invoice_id": int(partner_invoice_id or partner_id),
        "partner_shipping_id": int(partner_shipping_id or partner_id),
        "order_line": order_line_commands,
    }

    if type_id and "type_id" in so_fields:
        order_vals["type_id"] = int(type_id)
    elif "type_id" in so_fields and order_id:
        order_vals["type_id"] = False

    if route_id and "route_id" in so_fields:
        order_vals["route_id"] = int(route_id)
    elif "route_id" in so_fields and order_id:
        order_vals["route_id"] = False

    po_val = purchase_order_number.strip()
    if "purchase_order_number" in so_fields:
        order_vals["purchase_order_number"] = po_val
    elif "x_studio_purchase_order_number" in so_fields:
        order_vals["x_studio_purchase_order_number"] = po_val

    order_vals["client_order_ref"] = client_order_ref.strip()

    if order_id:
        target_order_id = int(order_id)
        models.execute_kw(
            db, uid, password,
            "sale.order", "write",
            [[target_order_id], order_vals],
            {"context": ctx},
        )
        action_label = "actualizada"
    else:
        order_vals["company_id"] = int(company_id)
        order_vals["state"] = "draft"
        target_order_id = models.execute_kw(
            db, uid, password,
            "sale.order", "create",
            [order_vals],
            {"context": ctx},
        )
        action_label = "creada"

    creator_name = creator_user.get("name", "Usuario Web")
    creator_login = creator_user.get("login", "")
    creator_partner_id = creator_user.get("partner_id")

    post_kwargs = {
        "body": f"<p>Cotización <strong>{action_label}</strong> desde <strong>Cotizador Web</strong> por: <strong>{creator_name}</strong> ({creator_login})</p>",
        "message_type": "comment",
        "subtype_xmlid": "mail.mt_note",
        "context": ctx,
    }
    if creator_partner_id:
        post_kwargs["author_id"] = int(creator_partner_id)

    try:
        models.execute_kw(db, uid, password, "sale.order", "message_post", [[target_order_id]], post_kwargs)
    except Exception:
        pass

    created = models.execute_kw(
        db, uid, password,
        "sale.order", "read",
        [[target_order_id]],
        {"fields": ["name", "amount_total"], "context": ctx},
    )[0]

    return {
        "order_id": target_order_id,
        "order_name": created["name"],
        "amount_total": created["amount_total"],
        "action_label": action_label,
        "odoo_link": f"{url}/web#id={target_order_id}&model=sale.order&view_type=form",
    }