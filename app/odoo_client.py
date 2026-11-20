import xmlrpc.client
import os
from app.database import get_local_pricelists_for_company
from datetime import datetime, timezone, timedelta

try:
    from zoneinfo import ZoneInfo
    AR_TZ = ZoneInfo("America/Argentina/Buenos_Aires")
except Exception:
    AR_TZ = timezone(timedelta(hours=-3))

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
    parent_name = p["parent_id"][1] if isinstance(p.get("parent_id"), list) else ""

    return {
        "id": int(p["id"]),
        "name": p.get("name") or "",
        "parent_name": parent_name,
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

def _get_company_hierarchy(models, db, uid, password, company_id: int) -> list:
    """Devuelve [company_id, parent_id, ...] subiendo por la jerarquía de sucursales."""
    companies = models.execute_kw(
        db, uid, password,
        "res.company", "search_read",
        [[]],
        {"fields": ["id", "parent_id"]},
    )
    parent_map = {}
    for c in companies:
        p = c.get("parent_id")
        parent_map[int(c["id"])] = int(p[0]) if isinstance(p, list) else None

    chain = []
    curr = int(company_id)
    while curr and curr not in chain:
        chain.append(curr)
        curr = parent_map.get(curr)
    return chain


def _extract_m2o_id(val):
    if isinstance(val, list) and len(val) > 0:
        return int(val[0])
    if isinstance(val, int) and not isinstance(val, bool) and val > 0:
        return int(val)
    return None


def get_quotation_options(company_id: int, allowed_company_ids: list):
    _, db, uid, password, models = _get_odoo_service_connection()
    hierarchy_ids = _get_company_hierarchy(models, db, uid, password, int(company_id))
    full_allowed = list(set(allowed_company_ids + hierarchy_ids))
    ctx = {"allowed_company_ids": full_allowed, "company_id": int(company_id)}

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

    route_model = (
        sol_fields.get("route_id", {}).get("relation")
        or so_fields.get("route_id", {}).get("relation")
    )

    # 1. Tipos de pedido de la empresa actual, padre o globales + su ruta por defecto
    order_types = []
    linked_route_ids = set()
    type_model = so_fields.get("type_id", {}).get("relation")
    if type_model:
        type_meta = models.execute_kw(
            db, uid, password,
            type_model, "fields_get",
            [[]],
            {"attributes": ["type", "relation"]},
        )
        type_route_field = None
        for cand in ["route_id", "default_route_id", "stock_route_id"]:
            if cand in type_meta:
                type_route_field = cand
                break
        if not type_route_field and route_model:
            for fname, fmeta in type_meta.items():
                if fmeta.get("type") == "many2one" and fmeta.get("relation") == route_model:
                    type_route_field = fname
                    break

        t_fields = ["id", "name"] + ([type_route_field] if type_route_field else [])
        raw_types = models.execute_kw(
            db, uid, password,
            type_model, "search_read",
            [["|", ("company_id", "=", False), ("company_id", "in", hierarchy_ids)]],
            {"fields": t_fields, "order": "sequence asc, id asc", "context": ctx},
        )
        for t in raw_types:
            def_route_id = _extract_m2o_id(t.get(type_route_field)) if type_route_field else None
            if def_route_id:
                linked_route_ids.add(def_route_id)
            order_types.append({
                "id": int(t["id"]),
                "name": t["name"],
                "default_route_id": def_route_id,
            })

    # 2. Rutas seleccionables en venta (incluyendo las vinculadas a tipos de pedido y empresas padre)
    routes = []
    if route_model:
        if linked_route_ids:
            route_domain = [
                "|", ("sale_selectable", "=", True), ("id", "in", list(linked_route_ids)),
                "|", ("company_id", "=", False), ("company_id", "in", hierarchy_ids),
            ]
        else:
            route_domain = [
                ("sale_selectable", "=", True),
                "|", ("company_id", "=", False), ("company_id", "in", hierarchy_ids),
            ]
        routes = models.execute_kw(
            db, uid, password,
            route_model, "search_read",
            [route_domain],
            {"fields": ["id", "name"], "order": "sequence asc, id asc", "context": ctx},
        )

    # 3. Listas de precios y monedas desde SQLite local (o en vivo desde Odoo)
    local_pl_bundle = get_local_pricelists_for_company(int(company_id))
    if local_pl_bundle and local_pl_bundle.get("pricelists"):
        pricelists = local_pl_bundle["pricelists"]
        base_curr = local_pl_bundle["base_currency"]
    else:
        comp_rec = models.execute_kw(
            db, uid, password,
            "res.company", "read",
            [[int(company_id)]],
            {"fields": ["id", "currency_id"], "context": ctx},
        )[0]
        base_curr_id = int(comp_rec["currency_id"][0]) if isinstance(comp_rec.get("currency_id"), list) else 1

        currencies = models.execute_kw(
            db, uid, password,
            "res.currency", "search_read",
            [[("active", "=", True)]],
            {"fields": ["id", "name", "symbol", "rate"], "context": ctx},
        )
        curr_map = {
            int(c["id"]): {
                "id": int(c["id"]),
                "name": c["name"],
                "symbol": c.get("symbol") or "$",
                "rate": float(c.get("rate") or 1.0),
            }
            for c in currencies
        }
        base_curr = curr_map.get(base_curr_id, {"id": base_curr_id, "name": "ARS", "symbol": "$", "rate": 1.0})
        base_rate = base_curr["rate"] or 1.0

        raw_pricelists = models.execute_kw(
            db, uid, password,
            "product.pricelist", "search_read",
            [[
                ("active", "=", True),
                "|", ("company_id", "=", False), ("company_id", "in", hierarchy_ids),
            ]],
            {"fields": ["id", "name", "currency_id"], "order": "sequence asc, id asc", "context": ctx},
        )
        pricelists = []
        for pl in raw_pricelists:
            c_info = pl.get("currency_id")
            cid = int(c_info[0]) if isinstance(c_info, list) else base_curr_id
            c_obj = curr_map.get(cid, base_curr)
            pl_rate = c_obj["rate"] or 1.0
            inverse_rate = round(base_rate / pl_rate, 4) if pl_rate else 1.0
            pricelists.append({
                "id": int(pl["id"]),
                "name": pl["name"],
                "currency_id": cid,
                "currency_name": c_obj["name"],
                "currency_symbol": c_obj["symbol"],
                "exchange_rate": inverse_rate,
                "is_foreign": cid != base_curr_id,
            })

    # 4. Producto de descuento configurado en Odoo
    discount_product = None
    try:
        comp_meta = models.execute_kw(db, uid, password, "res.company", "fields_get", [["sale_discount_product_id"]], {"attributes": ["type"]})
        if "sale_discount_product_id" in comp_meta:
            c_disc = models.execute_kw(db, uid, password, "res.company", "read", [[int(company_id)]], {"fields": ["sale_discount_product_id"], "context": ctx})[0]
            if isinstance(c_disc.get("sale_discount_product_id"), list):
                dp_id = int(c_disc["sale_discount_product_id"][0])
                dp_rec = models.execute_kw(db, uid, password, "product.product", "read", [[dp_id]], {"fields": ["id", "default_code", "name"], "context": ctx})[0]
                discount_product = {
                    "id": int(dp_rec["id"]),
                    "default_code": dp_rec.get("default_code") or "DESC",
                    "name": dp_rec.get("name") or "Descuento",
                }
        if not discount_product:
            param_val = models.execute_kw(
                db, uid, password,
                "ir.config_parameter", "get_param",
                ["sale.default_discount_product_id"],
            )
            if param_val and str(param_val).isdigit():
                dp_rec = models.execute_kw(db, uid, password, "product.product", "read", [[int(param_val)]], {"fields": ["id", "default_code", "name"], "context": ctx})
                if dp_rec:
                    discount_product = {
                        "id": int(dp_rec[0]["id"]),
                        "default_code": dp_rec[0].get("default_code") or "DESC",
                        "name": dp_rec[0].get("name") or "Descuento",
                    }
        if not discount_product:
            found_dp = models.execute_kw(
                db, uid, password,
                "product.product", "search_read",
                [[("type", "=", "service"), "|", ("name", "ilike", "Descuento"), ("default_code", "ilike", "DESC")]],
                {"fields": ["id", "default_code", "name"], "limit": 1, "context": ctx},
            )
            if found_dp:
                discount_product = {
                    "id": int(found_dp[0]["id"]),
                    "default_code": found_dp[0].get("default_code") or "DESC",
                    "name": found_dp[0].get("name") or "Descuento",
                }
    except Exception:
        pass

    return {
        "order_types": order_types,
        "routes": routes,
        "pricelists": pricelists,
        "base_currency": base_curr,
        "discount_product": discount_product,
    }


def get_customer_details(partner_id: int, company_id: int, allowed_company_ids: list, extra_address_ids: list = None):
    _, db, uid, password, models = _get_odoo_service_connection()
    hierarchy_ids = _get_company_hierarchy(models, db, uid, password, int(company_id))
    full_allowed = list(set(allowed_company_ids + hierarchy_ids))
    ctx = {"allowed_company_ids": full_allowed, "company_id": int(company_id)}

    code_fields = _get_partner_code_fields(models, db, uid, password)
    partner_meta = models.execute_kw(
        db, uid, password,
        "res.partner", "fields_get",
        [["sale_type"]],
        {"attributes": ["type", "relation"]},
    )
    has_sale_type = "sale_type" in partner_meta

    addr_fields = [
        "id", "name", "parent_id", "type", "street", "street2", "zip", "city",
        "state_id", "country_id", "vat", "phone", "mobile", "email",
        "commercial_partner_id", "property_product_pricelist",
    ] + code_fields + (["sale_type"] if has_sale_type else [])

    main_list = models.execute_kw(
        db, uid, password,
        "res.partner", "read",
        [[int(partner_id)]],
        {"fields": addr_fields, "context": ctx},
    )
    if not main_list:
        raise ValueError("Cliente no encontrado en Odoo.")
    main_p = main_list[0]

    comm_id = main_p["commercial_partner_id"][0] if isinstance(main_p.get("commercial_partner_id"), list) else int(partner_id)

    # Resolver res.partner.sale_type y property_product_pricelist en la empresa actual, padre o contacto comercial
    default_order_type_id = _extract_m2o_id(main_p.get("sale_type")) if has_sale_type else None
    default_pricelist_id = _extract_m2o_id(main_p.get("property_product_pricelist"))

    if (has_sale_type and not default_order_type_id) or not default_pricelist_id:
        candidate_partner_ids = [int(partner_id)]
        if comm_id and comm_id != int(partner_id):
            candidate_partner_ids.append(int(comm_id))

        prop_fields = ["property_product_pricelist"] + (["sale_type"] if has_sale_type else [])
        for cand_pid in candidate_partner_ids:
            for cid_ctx in hierarchy_ids + [False]:
                if (not has_sale_type or default_order_type_id) and default_pricelist_id:
                    break
                test_ctx = {"allowed_company_ids": full_allowed}
                if cid_ctx:
                    test_ctx["company_id"] = cid_ctx
                rec = models.execute_kw(
                    db, uid, password,
                    "res.partner", "read",
                    [[cand_pid]],
                    {"fields": prop_fields, "context": test_ctx},
                )
                if rec:
                    if has_sale_type and not default_order_type_id:
                        default_order_type_id = _extract_m2o_id(rec[0].get("sale_type"))
                    if not default_pricelist_id:
                        default_pricelist_id = _extract_m2o_id(rec[0].get("property_product_pricelist"))

    default_addrs = models.execute_kw(
        db, uid, password,
        "res.partner", "address_get",
        [[int(partner_id)], ["invoice", "delivery"]],
        {"context": ctx},
    )
    default_invoice_id = int(default_addrs.get("invoice") or partner_id)
    default_delivery_id = int(default_addrs.get("delivery") or partner_id)

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
    formatted_list.sort(key=lambda x: (0 if x["id"] == int(partner_id) else 1, x["type"], x["name"]))
    addr_by_id = {a["id"]: a for a in formatted_list}

    main_card = _format_partner_address(main_p)
    return {
        "customer": main_card,
        "default_invoice_partner": addr_by_id.get(default_invoice_id, main_card),
        "default_delivery_partner": addr_by_id.get(default_delivery_id, main_card),
        "default_pricelist_id": default_pricelist_id,
        "default_order_type_id": default_order_type_id,
        "related_addresses": formatted_list,
    }

def compute_pricelist_prices_odoo_fallback(
    company_id: int,
    allowed_company_ids: list,
    pricelist_id: int,
    items: list,
    partner_id: int = None,
):
    """
    Fallback directo contra product.pricelist._get_product_price en Odoo 16 si SQLite aún no fue sincronizado.
    """
    if not items or not pricelist_id:
        return {}

    _, db, uid, password, models = _get_odoo_service_connection()
    ctx = {"allowed_company_ids": allowed_company_ids, "company_id": int(company_id)}
    prices_out = {}

    for it in items:
        pid = int(it["product_id"])
        qty = float(it.get("qty") or 1.0)
        try:
            val = models.execute_kw(
                db, uid, password,
                "product.pricelist", "_get_product_price",
                [[int(pricelist_id)], pid, qty],
                {"partner": int(partner_id) if partner_id else False, "context": ctx},
            )
            prices_out[str(pid)] = round(float(val or 0.0), 4)
        except Exception:
            pass

    return prices_out

def _get_partner_code_fields(models, db, uid, password):
    p_meta = models.execute_kw(
        db, uid, password,
        "res.partner", "fields_get",
        [["ref", "internal_code", "x_studio_internal_code"]],
        {"attributes": ["type"]},
    )
    return [f for f in ["internal_code", "ref", "x_studio_internal_code"] if f in p_meta]

def search_customers(query: str, company_id: int, allowed_company_ids: list, include_children: bool = False, limit: int = 20):
    _, db, uid, password, models = _get_odoo_service_connection()
    ctx = {"allowed_company_ids": allowed_company_ids, "company_id": int(company_id)}

    code_fields = _get_partner_code_fields(models, db, uid, password)
    search_fields = ["name", "vat"] + code_fields

    or_clauses = [(f, "ilike", query) for f in search_fields]
    or_domain = ["|"] * (len(or_clauses) - 1) + or_clauses

    read_fields = ["id", "name", "vat", "city", "street", "type", "parent_id"] + code_fields

    if include_children:
        domain = [
            "|", ("company_id", "=", False), ("company_id", "=", int(company_id)),
        ] + or_domain
        partners = models.execute_kw(
            db, uid, password,
            "res.partner", "search_read",
            [domain],
            {"fields": read_fields, "limit": limit, "context": ctx},
        )
    else:
        domain = [
            "&",
            "|", ("company_id", "=", False), ("company_id", "=", int(company_id)),
            ("parent_id", "=", False),
        ] + or_domain
        partners = models.execute_kw(
            db, uid, password,
            "res.partner", "search_read",
            [domain],
            {"fields": read_fields, "limit": limit, "context": ctx},
        )
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
        p["parent_name"] = p["parent_id"][1] if isinstance(p.get("parent_id"), list) else ""
    return partners

def get_single_partner_card(partner_id: int, company_id: int, allowed_company_ids: list):
    _, db, uid, password, models = _get_odoo_service_connection()
    ctx = {"allowed_company_ids": allowed_company_ids, "company_id": int(company_id)}
    code_fields = _get_partner_code_fields(models, db, uid, password)
    addr_fields = [
        "id", "name", "parent_id", "type", "street", "street2", "zip", "city",
        "state_id", "country_id", "vat", "phone", "mobile", "email"
    ] + code_fields

    recs = models.execute_kw(
        db, uid, password,
        "res.partner", "read",
        [[int(partner_id)]],
        {"fields": addr_fields, "context": ctx},
    )
    if not recs:
        raise ValueError("Contacto/cliente no encontrado en Odoo.")
    return _format_partner_address(recs[0])

def get_customer_details(partner_id: int, company_id: int, allowed_company_ids: list, extra_address_ids: list = None):
    _, db, uid, password, models = _get_odoo_service_connection()
    ctx = {"allowed_company_ids": allowed_company_ids, "company_id": int(company_id)}
    code_fields = _get_partner_code_fields(models, db, uid, password)

    # Detectar campo de Tipo de Pedido en res.partner (ej. sale_type -> sale.order.type)
    so_type_meta = models.execute_kw(db, uid, password, "sale.order", "fields_get", [["type_id"]], {"attributes": ["relation"]})
    type_model = so_type_meta.get("type_id", {}).get("relation")
    partner_meta = models.execute_kw(db, uid, password, "res.partner", "fields_get", [[]], {"attributes": ["type", "relation"]})

    partner_order_type_field = None
    for cand in ["sale_type", "sale_order_type_id", "type_id"]:
        if cand in partner_meta and (not type_model or partner_meta[cand].get("relation") == type_model):
            partner_order_type_field = cand
            break
    if not partner_order_type_field and type_model:
        for fname, fmeta in partner_meta.items():
            if fmeta.get("type") == "many2one" and fmeta.get("relation") == type_model:
                partner_order_type_field = fname
                break

    addr_fields = [
        "id", "name", "parent_id", "type", "street", "street2", "zip", "city",
        "state_id", "country_id", "vat", "phone", "mobile", "email",
        "commercial_partner_id", "property_product_pricelist",
    ] + code_fields + ([partner_order_type_field] if partner_order_type_field else [])

    main_list = models.execute_kw(
        db, uid, password,
        "res.partner", "read",
        [[int(partner_id)]],
        {"fields": addr_fields, "context": ctx},
    )
    if not main_list:
        raise ValueError("Cliente no encontrado en Odoo.")
    main_p = main_list[0]

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
    formatted_list.sort(key=lambda x: (0 if x["id"] == int(partner_id) else 1, x["type"], x["name"]))
    addr_by_id = {a["id"]: a for a in formatted_list}

    pl_info = main_p.get("property_product_pricelist")
    default_pricelist_id = int(pl_info[0]) if isinstance(pl_info, list) else None

    ot_info = main_p.get(partner_order_type_field) if partner_order_type_field else None
    default_order_type_id = int(ot_info[0]) if isinstance(ot_info, list) else None

    main_card = _format_partner_address(main_p)
    return {
        "customer": main_card,
        "default_invoice_partner": addr_by_id.get(default_invoice_id, main_card),
        "default_delivery_partner": addr_by_id.get(default_delivery_id, main_card),
        "default_pricelist_id": default_pricelist_id,
        "default_order_type_id": default_order_type_id,
        "related_addresses": formatted_list,
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
            "fields": ["id", "name", "date_order", "partner_id", "amount_total", "state", "client_order_ref", "pricelist_id"],
            "order": "id desc",
            "limit": limit,
            "context": ctx,
        },
    )

    for o in orders:
        raw_dt = o.get("date_order")
        if isinstance(raw_dt, str) and len(raw_dt) >= 19:
            try:
                dt_utc = datetime.strptime(raw_dt[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                o["date_order"] = dt_utc.astimezone(AR_TZ).strftime("%d/%m/%Y %H:%M")
            except Exception:
                pass

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
        "pricelist_id", "currency_id",
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

    # Asegurar tarjetas exactas de partner_invoice_id y partner_shipping_id (aunque pertenezcan a otro cliente)
    addr_map = {a["id"]: a for a in customer_bundle.get("related_addresses", [])}
    invoice_card = addr_map.get(inv_id) or get_single_partner_card(inv_id, company_id, allowed_company_ids)
    shipping_card = addr_map.get(ship_id) or get_single_partner_card(ship_id, company_id, allowed_company_ids)

    line_ids = o.get("order_line") or []
    lines_out = []
    if line_ids:
        raw_lines = models.execute_kw(
            db, uid, password,
            "sale.order.line", "read",
            [line_ids],
            {
                "fields": ["id", "sequence", "display_type", "product_id", "name", "product_uom_qty", "price_unit", "discount", "tax_id"],
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
        "invoice_card": invoice_card,
        "shipping_card": shipping_card,
        "pricelist_id": int(o["pricelist_id"][0]) if isinstance(o.get("pricelist_id"), list) else None,
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
    pricelist_id: int = None,
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
                "tax_id": [(6, 0, line.get("tax_ids") or [])],
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

    if pricelist_id:
        order_vals["pricelist_id"] = int(pricelist_id)

    if type_id and "type_id" in so_fields:
        order_vals["type_id"] = int(type_id)
    elif "type_id" in so_fields and order_id:
        order_vals["type_id"] = False

    if route_id and "route_id" in so_fields:
        order_vals["route_id"] = int(route_id)
    elif "route_id" in so_fields and order_id:
        order_vals["route_id"] = False

    # Evitar choque con restricción UNIQUE de PostgreSQL cuando el campo queda vacío (usar False/NULL en vez de "")
    po_val = purchase_order_number.strip()
    po_field = None
    if "purchase_order_number" in so_fields:
        po_field = "purchase_order_number"
    elif "x_studio_purchase_order_number" in so_fields:
        po_field = "x_studio_purchase_order_number"

    if po_field:
        if po_val:
            order_vals[po_field] = po_val
        elif order_id:
            order_vals[po_field] = False

    order_vals["client_order_ref"] = client_order_ref.strip() if client_order_ref.strip() else False

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

    # Reafirmar pricelist_id y direcciones para que el cálculo por defecto de partner_id en Odoo 16 no los sobrescriba
    post_write_vals = {
        "partner_invoice_id": int(partner_invoice_id or partner_id),
        "partner_shipping_id": int(partner_shipping_id or partner_id),
    }
    if pricelist_id:
        post_write_vals["pricelist_id"] = int(pricelist_id)

    models.execute_kw(
        db, uid, password,
        "sale.order", "write",
        [[target_order_id], post_write_vals],
        {"context": ctx},
    )

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