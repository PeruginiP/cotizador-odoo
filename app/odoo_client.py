import xmlrpc.client
import os
import unicodedata
from markupsafe import Markup
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


def _get_company_hierarchy(models, db, uid, password, company_id: int, allowed_company_ids: list = None) -> list:
    """Devuelve [company_id, parent_id, ...] subiendo por la jerarquía de sucursales."""
    try:
        ctx = {"allowed_company_ids": allowed_company_ids} if allowed_company_ids else {}
        companies = models.execute_kw(
            db, uid, password,
            "res.company", "search_read",
            [[]],
            {"fields": ["id", "parent_id"], "context": ctx},
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
    except Exception:
        return [int(company_id)]


def _extract_m2o_id(val):
    if isinstance(val, list) and len(val) > 0:
        return int(val[0])
    if isinstance(val, int) and not isinstance(val, bool) and val > 0:
        return int(val)
    return None

def _make_company_context(active_company_id: int, allowed_company_ids: list) -> dict:
    cid = int(active_company_id)
    ordered = [cid] + [int(x) for x in (allowed_company_ids or []) if int(x) != cid]
    return {
        "allowed_company_ids": ordered,
        "company_id": cid,
        "force_company": cid,
    }

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


def get_quotation_options(company_id: int, allowed_company_ids: list):
    _, db, uid, password, models = _get_odoo_service_connection()
    hierarchy_ids = _get_company_hierarchy(models, db, uid, password, int(company_id), allowed_company_ids)
    full_allowed = list(dict.fromkeys([int(company_id)] + hierarchy_ids + [int(x) for x in allowed_company_ids]))
    ctx = _make_company_context(int(company_id), full_allowed)

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

    # 1. Tipos de pedido de la empresa actual, padre o globales + su ruta por defecto (route_id)
    order_types = []
    linked_route_ids = set()
    type_model = so_fields.get("type_id", {}).get("relation")
    if type_model:
        type_meta = models.execute_kw(
            db, uid, password,
            type_model, "fields_get",
            [["route_id", "company_id"]],
            {"attributes": ["type", "relation"]},
        )
        has_type_route = "route_id" in type_meta
        if not route_model and has_type_route:
            route_model = type_meta["route_id"].get("relation")

        t_fields = ["id", "name"] + (["route_id"] if has_type_route else [])
        type_domain = (
            [["|", ("company_id", "=", False), ("company_id", "in", hierarchy_ids)]]
            if "company_id" in type_meta
            else [[]]
        )

        raw_types = models.execute_kw(
            db, uid, password,
            type_model, "search_read",
            type_domain,
            {"fields": t_fields, "order": "id asc", "context": ctx},
        )
        for t in raw_types:
            def_route_id = _extract_m2o_id(t.get("route_id")) if has_type_route else None
            if def_route_id:
                linked_route_ids.add(def_route_id)
            order_types.append({
                "id": int(t["id"]),
                "name": t["name"],
                "default_route_id": def_route_id,
            })

    # 2. Rutas seleccionables en venta + rutas vinculadas explícitamente a sale.order.type.route_id
    routes = []
    if route_model:
        if linked_route_ids:
            route_domain = [
                "|",
                "&", ("sale_selectable", "=", True),
                     "|", ("company_id", "=", False), ("company_id", "in", hierarchy_ids),
                ("id", "in", list(linked_route_ids)),
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

    # 3. Listas de precios y monedas desde SQLite local (incluyendo listas de la empresa padre)
    local_pl_bundle = get_local_pricelists_for_company(int(company_id), hierarchy_ids)
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


def compute_pricelist_prices_odoo_fallback(
    company_id: int,
    allowed_company_ids: list,
    pricelist_id: int,
    items: list,
    partner_id: int = None,
):
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
    hierarchy_ids = _get_company_hierarchy(models, db, uid, password, int(company_id), allowed_company_ids)
    full_allowed = list(set(allowed_company_ids + hierarchy_ids))
    ctx = {"allowed_company_ids": full_allowed, "company_id": int(company_id)}

    code_fields = _get_partner_code_fields(models, db, uid, password)
    search_fields = ["name", "vat"] + code_fields

    or_clauses = [(f, "ilike", query) for f in search_fields]
    or_domain = ["|"] * (len(or_clauses) - 1) + or_clauses

    read_fields = ["id", "name", "vat", "city", "street", "type", "parent_id"] + code_fields

    if include_children:
        domain = [
            "|", ("company_id", "=", False), ("company_id", "in", hierarchy_ids),
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
            "|", ("company_id", "=", False), ("company_id", "in", hierarchy_ids),
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
                "|", ("company_id", "=", False), ("company_id", "in", hierarchy_ids),
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
    hierarchy_ids = _get_company_hierarchy(models, db, uid, password, int(company_id), allowed_company_ids)
    full_allowed = list(set(allowed_company_ids + hierarchy_ids))
    ctx = {"allowed_company_ids": full_allowed, "company_id": int(company_id)}

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
    hierarchy_ids = _get_company_hierarchy(models, db, uid, password, int(company_id), allowed_company_ids)
    full_allowed = list(dict.fromkeys([int(company_id)] + hierarchy_ids + [int(x) for x in allowed_company_ids]))
    ctx = _make_company_context(int(company_id), full_allowed)

    code_fields = _get_partner_code_fields(models, db, uid, password)
    partner_meta = models.execute_kw(
        db, uid, password,
        "res.partner", "fields_get",
        [["sale_type"]],
        {"attributes": ["type", "relation"]},
    )
    has_sale_type = "sale_type" in partner_meta
    sale_type_model = partner_meta.get("sale_type", {}).get("relation") or "sale.order.type"

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

    comm_id = _extract_m2o_id(main_p.get("commercial_partner_id")) or int(partner_id)
    candidate_pids = [int(partner_id)] + ([comm_id] if comm_id != int(partner_id) else [])

    # 1. Resolver res.partner.sale_type cambiando allowed_company_ids[0] según la jerarquía
    default_order_type_id = _extract_m2o_id(main_p.get("sale_type")) if has_sale_type else None
    resolved_type_ctx = ctx

    if has_sale_type and not default_order_type_id:
        company_order = list(dict.fromkeys(hierarchy_ids + full_allowed))
        for cand_pid in candidate_pids:
            for cid in company_order:
                test_ctx = _make_company_context(cid, full_allowed)
                rec = models.execute_kw(
                    db, uid, password,
                    "res.partner", "read",
                    [[cand_pid]],
                    {"fields": ["sale_type"], "context": test_ctx},
                )
                if rec:
                    val = _extract_m2o_id(rec[0].get("sale_type"))
                    if val:
                        default_order_type_id = val
                        resolved_type_ctx = test_ctx
                        break
            if default_order_type_id:
                break

    # Obtener metadatos del sale.order.type y su route_id para garantizar que el frontend los tenga
    default_order_type = None
    if default_order_type_id:
        try:
            t_meta = models.execute_kw(
                db, uid, password,
                sale_type_model, "fields_get",
                [["route_id"]],
                {"attributes": ["type", "relation"]},
            )
            t_read_fields = ["id", "name"] + (["route_id"] if "route_id" in t_meta else [])
            t_recs = models.execute_kw(
                db, uid, password,
                sale_type_model, "read",
                [[int(default_order_type_id)]],
                {"fields": t_read_fields, "context": resolved_type_ctx},
            )
            if t_recs:
                t_rec = t_recs[0]
                r_raw = t_rec.get("route_id")
                def_r_id = _extract_m2o_id(r_raw)
                def_r_name = r_raw[1] if isinstance(r_raw, list) and len(r_raw) > 1 else ""
                default_order_type = {
                    "id": int(t_rec["id"]),
                    "name": t_rec.get("name") or "",
                    "default_route_id": def_r_id,
                    "default_route_name": def_r_name,
                }
        except Exception:
            pass

    # 2. Resolver lista de precios predeterminada del cliente en sucursal -> empresa padre
    default_pricelist_id = _extract_m2o_id(main_p.get("property_product_pricelist"))
    if not default_pricelist_id:
        contexts_to_try = [{"allowed_company_ids": full_allowed, "company_id": cid} for cid in hierarchy_ids]
        contexts_to_try.append({"allowed_company_ids": full_allowed})
        for cand_pid in candidate_pids:
            for test_ctx in contexts_to_try:
                rec = models.execute_kw(
                    db, uid, password,
                    "res.partner", "read",
                    [[cand_pid]],
                    {"fields": ["property_product_pricelist"], "context": test_ctx},
                )
                if rec:
                    val = _extract_m2o_id(rec[0].get("property_product_pricelist"))
                    if val:
                        default_pricelist_id = val
                        break
            if default_pricelist_id:
                break

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
    ctx = _make_company_context(int(company_id), allowed_company_ids)

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



import re
import base64
from difflib import SequenceMatcher


def _clean_cuit(val: str) -> str:
    return re.sub(r"\D", "", val or "")


def find_vendor_by_cuit_or_name(cuit: str, name: str, company_id: int, allowed_company_ids: list):
    _, db, uid, password, models = _get_odoo_service_connection()
    hierarchy_ids = _get_company_hierarchy(models, db, uid, password, int(company_id), allowed_company_ids)
    full_allowed = list(dict.fromkeys([int(company_id)] + hierarchy_ids + [int(x) for x in allowed_company_ids]))
    ctx = _make_company_context(int(company_id), full_allowed)

    comp_domain = ["|", ("company_id", "=", False), ("company_id", "in", hierarchy_ids)]
    fields = ["id", "name", "vat", "parent_id", "supplier_rank"]

    digits = _clean_cuit(cuit)
    if len(digits) >= 8:
        formatted = f"{digits[:2]}-{digits[2:10]}-{digits[10:]}" if len(digits) == 11 else cuit
        vat_domain = comp_domain + [
            ("parent_id", "=", False),
            "|", "|",
            ("vat", "ilike", digits),
            ("vat", "ilike", formatted),
            ("vat", "ilike", f"AR{digits}"),
        ]
        found = models.execute_kw(
            db, uid, password, "res.partner", "search_read",
            [vat_domain], {"fields": fields, "limit": 5, "order": "supplier_rank desc, id asc", "context": ctx}
        )
        if found:
            p = found[0]
            return {"id": int(p["id"]), "name": p["name"], "vat": p.get("vat") or ""}

    if name and len(name.strip()) >= 3:
        name_domain = comp_domain + [("parent_id", "=", False), ("name", "ilike", name.strip()[:35])]
        found = models.execute_kw(
            db, uid, password, "res.partner", "search_read",
            [name_domain], {"fields": fields, "limit": 5, "order": "supplier_rank desc, id asc", "context": ctx}
        )
        if found:
            p = found[0]
            return {"id": int(p["id"]), "name": p["name"], "vat": p.get("vat") or ""}

    return None


def _read_purchase_orders_with_lines(models, db, uid, password, po_records: list, ctx: dict) -> list:
    if not po_records:
        return []

    all_line_ids = []
    for po in po_records:
        all_line_ids.extend(po.get("order_line") or [])

    lines_by_id = {}
    prod_ids = set()
    tax_ids = set()

    if all_line_ids:
        pol_meta = models.execute_kw(db, uid, password, "purchase.order.line", "fields_get", [[]], {"attributes": ["type"]})
        pol_fields = ["id", "order_id", "sequence", "display_type", "product_id", "name", "product_qty", "qty_received", "qty_invoiced", "price_unit", "price_subtotal", "taxes_id"]
        if "discount" in pol_meta:
            pol_fields.append("discount")

        raw_lines = models.execute_kw(
            db, uid, password, "purchase.order.line", "read", [all_line_ids], {"fields": pol_fields, "context": ctx}
        )
        for rl in raw_lines:
            lines_by_id[int(rl["id"])] = rl
            if isinstance(rl.get("product_id"), list):
                prod_ids.add(int(rl["product_id"][0]))
            for tid in (rl.get("taxes_id") or []):
                tax_ids.add(int(tid))

    prod_map = {}
    tmpl_to_prod_ids = {}
    if prod_ids:
        prods = models.execute_kw(
            db, uid, password, "product.product", "read", [list(prod_ids)],
            {"fields": ["id", "default_code", "name", "barcode", "product_tmpl_id"], "context": ctx}
        )
        for p in prods:
            pid = int(p["id"])
            prod_map[pid] = p
            if isinstance(p.get("product_tmpl_id"), list):
                tid = int(p["product_tmpl_id"][0])
                tmpl_to_prod_ids.setdefault(tid, []).append(pid)

    # Leer códigos de proveedor (product.supplierinfo.product_code) asociados a estos artículos
    supplier_codes_by_prod = {}  # { (product_id, partner_id_or_0): set_of_codes }
    if tmpl_to_prod_ids:
        po_partner_ids = list({int(po["partner_id"][0]) for po in po_records if isinstance(po.get("partner_id"), list)})
        si_domain = [
            "|",
            ("product_id", "in", list(prod_ids)),
            ("product_tmpl_id", "in", list(tmpl_to_prod_ids.keys())),
        ]
        s_infos = models.execute_kw(
            db, uid, password, "product.supplierinfo", "search_read",
            [si_domain],
            {"fields": ["partner_id", "product_id", "product_tmpl_id", "product_code", "product_name"], "context": ctx}
        )
        for si in s_infos:
            scode = (si.get("product_code") or "").strip()
            if not scode:
                continue
            sup_pid = int(si["partner_id"][0]) if isinstance(si.get("partner_id"), list) else 0
            target_pids = []
            if isinstance(si.get("product_id"), list):
                target_pids = [int(si["product_id"][0])]
            elif isinstance(si.get("product_tmpl_id"), list):
                target_pids = tmpl_to_prod_ids.get(int(si["product_tmpl_id"][0]), [])

            for t_pid in target_pids:
                # Si pertenece al proveedor de la OC lo guardamos con prioridad y también en bolsa general (0)
                if sup_pid in po_partner_ids:
                    supplier_codes_by_prod.setdefault((t_pid, sup_pid), []).append(scode)
                supplier_codes_by_prod.setdefault((t_pid, 0), []).append(scode)

    tax_map = {}
    if tax_ids:
        taxes = models.execute_kw(
            db, uid, password, "account.tax", "read", [list(tax_ids)],
            {"fields": ["id", "name", "amount", "description"], "context": ctx}
        )
        tax_map = {int(t["id"]): t for t in taxes}

    out = []
    for po in po_records:
        po_lines = []
        po_partner_id = int(po["partner_id"][0]) if isinstance(po.get("partner_id"), list) else 0
        for lid in (po.get("order_line") or []):
            rl = lines_by_id.get(int(lid))
            if not rl or rl.get("display_type"):
                continue
            pid = int(rl["product_id"][0]) if isinstance(rl.get("product_id"), list) else None
            p_info = prod_map.get(pid, {})
            v_codes = list(dict.fromkeys(
                supplier_codes_by_prod.get((pid, po_partner_id), []) +
                supplier_codes_by_prod.get((pid, 0), [])
            )) if pid else []

            # Primero definimos t_list y después armamos t_labels
            t_list = [int(t) for t in (rl.get("taxes_id") or [])]
            t_labels = []
            for t in t_list:
                if t in tax_map:
                    amt = float(tax_map[t].get("amount") or 0.0)
                    t_labels.append(f"{amt:g}%" if amt > 0 else (tax_map[t].get("description") or "0%"))

            qty_ordered = float(rl.get("product_qty") or 0.0)
            qty_invoiced = float(rl.get("qty_invoiced") or 0.0)
            qty_pending = round(max(qty_ordered - qty_invoiced, 0.0), 4)

            po_lines.append({
                "po_line_id": int(rl["id"]),
                "po_id": int(po["id"]),
                "po_name": po["name"],
                "product_id": pid,
                "default_code": p_info.get("default_code") or "",
                "vendor_codes": v_codes,
                "product_name": p_info.get("name") or (rl["product_id"][1] if isinstance(rl.get("product_id"), list) else ""),
                "description": rl.get("name") or "",
                "product_qty": qty_ordered,
                "qty_received": float(rl.get("qty_received") or 0.0),
                "qty_invoiced": qty_invoiced,
                "qty_pending": qty_pending,
                "price_unit": float(rl.get("price_unit") or 0.0),
                "discount": float(rl.get("discount") or 0.0),
                "price_subtotal": float(rl.get("price_subtotal") or 0.0),
                "tax_ids": t_list,
                "tax_names": ", ".join(t_labels) if t_labels else "0%",
            })

        curr_id = int(po["currency_id"][0]) if isinstance(po.get("currency_id"), list) else None
        curr_name = po["currency_id"][1] if isinstance(po.get("currency_id"), list) else "ARS"
        partner_name = po["partner_id"][1] if isinstance(po.get("partner_id"), list) else ""
        partner_id = int(po["partner_id"][0]) if isinstance(po.get("partner_id"), list) else None

        out.append({
            "id": int(po["id"]),
            "name": po["name"],
            "partner_id": partner_id,
            "partner_name": partner_name,
            "partner_ref": po.get("partner_ref") or "",
            "date_order": str(po.get("date_order") or "")[:10],
            "currency_id": curr_id,
            "currency": curr_name,
            "amount_untaxed": float(po.get("amount_untaxed") or 0.0),
            "amount_tax": float(po.get("amount_tax") or 0.0),
            "amount_total": float(po.get("amount_total") or 0.0),
            "invoice_status": po.get("invoice_status") or "",
            "state": po.get("state") or "",
            "lines": po_lines,
            "payment_term_id": int(po["payment_term_id"][0]) if isinstance(po.get("payment_term_id"), list) else None,
        })
    return out


def search_purchase_orders_for_bill(
    company_id: int,
    allowed_company_ids: list,
    po_references: list = None,
    partner_id: int = None,
    query: str = "",
    limit: int = 15,
) -> list:
    _, db, uid, password, models = _get_odoo_service_connection()
    ctx = _make_company_context(int(company_id), allowed_company_ids)

    po_fields = [
        "id", "name", "partner_id", "partner_ref", "date_order",
        "currency_id", "amount_untaxed", "amount_tax", "amount_total",
        "state", "invoice_status", "order_line","payment_term_id",
    ]
    base_domain = [("company_id", "=", int(company_id)), ("state", "in", ["purchase", "done"])]

    # 1. Búsqueda libre manual desde el buscador del frontend
    if query and query.strip():
        q = query.strip()
        dom = base_domain + [
            "|", "|",
            ("name", "ilike", q),
            ("partner_ref", "ilike", q),
            ("partner_id.name", "ilike", q),
        ]
        if partner_id:
            dom = [("partner_id", "child_of", int(partner_id))] + dom
        recs = models.execute_kw(
            db, uid, password, "purchase.order", "search_read",
            [dom], {"fields": po_fields, "order": "id desc", "limit": limit, "context": ctx}
        )
        return _read_purchase_orders_with_lines(models, db, uid, password, recs, ctx)

    # 2. Búsqueda automática por referencias extraídas en el PDF
    matched_recs = []
    seen_ids = set()
    for ref in (po_references or []):
        clean_ref = ref.strip()
        if not clean_ref:
            continue
        digits_only = re.sub(r"\D", "", clean_ref)
        or_conds = [("name", "ilike", clean_ref), ("partner_ref", "ilike", clean_ref)]
        if digits_only and len(digits_only) >= 3:
            or_conds.append(("name", "ilike", digits_only))
        dom = base_domain + (["|"] * (len(or_conds) - 1)) + or_conds
        if partner_id:
            dom_with_vendor = [("partner_id", "child_of", int(partner_id))] + dom
            recs = models.execute_kw(
                db, uid, password, "purchase.order", "search_read",
                [dom_with_vendor], {"fields": po_fields, "order": "id desc", "limit": 5, "context": ctx}
            )
            if not recs:
                recs = models.execute_kw(
                    db, uid, password, "purchase.order", "search_read",
                    [dom], {"fields": po_fields, "order": "id desc", "limit": 5, "context": ctx}
                )
        else:
            recs = models.execute_kw(
                db, uid, password, "purchase.order", "search_read",
                [dom], {"fields": po_fields, "order": "id desc", "limit": 5, "context": ctx}
            )
        for r in recs:
            if int(r["id"]) not in seen_ids:
                seen_ids.add(int(r["id"]))
                matched_recs.append(r)

    if matched_recs:
        return _read_purchase_orders_with_lines(models, db, uid, password, matched_recs, ctx)

    # 3. Fallback: si el PDF no tenía número de OC pero encontramos el proveedor, traer sus OC pendientes de facturar
    if partner_id:
        fallback_dom = base_domain + [
            ("partner_id", "child_of", int(partner_id)),
            ("invoice_status", "!=", "invoiced"),
        ]
        recs = models.execute_kw(
            db, uid, password, "purchase.order", "search_read",
            [fallback_dom], {"fields": po_fields, "order": "id desc", "limit": 8, "context": ctx}
        )
        return _read_purchase_orders_with_lines(models, db, uid, password, recs, ctx)

    return []


def _strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", text or "")
        if unicodedata.category(c) != "Mn"
    )


def _normalize_ar_invoice_number(raw_num: str) -> str:
    s = (raw_num or "").strip()
    if not s:
        return ""
    if "-" in s:
        pos, num = s.split("-", 1)
        pos_d = re.sub(r"\D", "", pos)
        num_d = re.sub(r"\D", "", num)
        if pos_d and num_d:
            return f"{pos_d.zfill(5)[-5:]}-{num_d.zfill(8)[-8:]}"
    digits = re.sub(r"\D", "", s)
    if len(digits) > 8:
        return f"{digits[:-8].zfill(5)[-5:]}-{digits[-8:]}"
    return s


PROVINCE_ALIASES = {
    "caba": ["caba", "capital federal", "ciudad autonoma", "c.a.b.a", "cf", "901"],
    "buenos aires": ["buenos aires", "bs as", "bs.as", "bs. as", "pba", "arba", "902"],
    "catamarca": ["catamarca", "903"],
    "cordoba": ["cordoba", "cba", "904"],
    "corrientes": ["corrientes", "905"],
    "chaco": ["chaco", "906"],
    "chubut": ["chubut", "907"],
    "entre rios": ["entre rios", "e. rios", "e.rios", "erios", "908"],
    "formosa": ["formosa", "909"],
    "jujuy": ["jujuy", "910"],
    "la pampa": ["la pampa", "pampa", "911"],
    "la rioja": ["la rioja", "rioja", "912"],
    "mendoza": ["mendoza", "mza", "913"],
    "misiones": ["misiones", "914"],
    "neuquen": ["neuquen", "nqn", "915"],
    "rio negro": ["rio negro", "r. negro", "r.negro", "916"],
    "salta": ["salta", "917"],
    "san juan": ["san juan", "918"],
    "san luis": ["san luis", "919"],
    "santa cruz": ["santa cruz", "920"],
    "santa fe": ["santa fe", "sta fe", "sta. fe", "api", "921"],
    "santiago del estero": ["santiago del estero", "sgo del estero", "sgo. del estero", "922"],
    "tierra del fuego": ["tierra del fuego", "tdf", "923"],
    "tucuman": ["tucuman", "tuc", "924"],
}


def get_purchase_tax_options(company_id: int, allowed_company_ids: list) -> dict:
    """Devuelve impuestos de Compras activos en Odoo separados en IVA y Percepciones."""
    _, db, uid, password, models = _get_odoo_service_connection()
    hierarchy_ids = _get_company_hierarchy(models, db, uid, password, int(company_id), allowed_company_ids)
    ctx = _make_company_context(int(company_id), allowed_company_ids)
    
    taxes = models.execute_kw(
        db, uid, password, "account.tax", "search_read",
        [[
            ("type_tax_use", "=", "purchase"),
            ("active", "=", True),
            "|", ("company_id", "=", False), ("company_id", "in", hierarchy_ids),
        ]],
        {
            "fields": ["id", "name", "description", "invoice_label", "amount", "amount_type", "tax_group_id"],
            "order": "sequence asc, id asc",
            "context": ctx,
        },
    )

    vat_taxes = []
    perception_taxes = []
    
    for t in taxes:
        amt_val = float(t.get("amount") or 0.0)
        if amt_val > 0:
            short_lbl = f"IVA {amt_val:g}%"
        elif "exen" in t_name_norm:
            short_lbl = "Exento (0%)"
        elif "no grav" in t_name_norm:
            short_lbl = "No Grav. (0%)"
        else:
            short_lbl = "0%"
        grp_name = _strip_accents(t["tax_group_id"][1] if isinstance(t.get("tax_group_id"), list) else "").lower()
        t_name_norm = _strip_accents(t.get("name") or "").lower()
        item = {
            "id": int(t["id"]),
            "name": t["name"],
            "short_label": short_lbl,
            "description": t.get("description") or t.get("invoice_label") or t["name"],
            "amount": amt_val,
        }
        if any(k in t_name_norm or k in grp_name for k in ("perc", "iibb", "ingresos brutos", "ganancia", "adicional")):
            perception_taxes.append(item)
        else:
            vat_taxes.append(item)

    return {"vat_taxes": vat_taxes, "perception_taxes": perception_taxes}


def match_extracted_perceptions_to_odoo(parsed_perceptions: list, perception_taxes: list) -> list:
    """Cruza automáticamente cada percepción leída en el PDF contra el catálogo de impuestos de Argelec."""
    enriched = []
    for p in (parsed_perceptions or []):
        lbl = p.get("label") or ""
        amt = float(p.get("amount") or 0.0)
        norm_lbl = _strip_accents(lbl).lower()

        matched_tax = None

        # 1. Percepción de IVA (RG 2408 / RG 3337 / Perc. IVA) -> 'Percepcion IVA'
        if any(k in norm_lbl for k in ("2408", "3337", "perc. iva", "percep. iva", "percepcion iva", "percepcion de iva", "iva rg")):
            for t in perception_taxes:
                tn = _strip_accents(t["name"]).lower()
                if "percepcion iva" in tn or ("iva" in tn and "perc" in tn):
                    matched_tax = t
                    break

        # 2. Percepción de Ganancias -> 'Percepcion Ganancias'
        if not matched_tax and any(k in norm_lbl for k in ("ganancia", "rg 830", "2281")):
            for t in perception_taxes:
                if "ganancia" in _strip_accents(t["name"]).lower():
                    matched_tax = t
                    break

        # 3. Percepción IIBB por Provincia -> 'Percepcion IIBB Entre Rios', 'Percepcion IIBB Cordoba', etc.
        if not matched_tax:
            for prov_canonical, aliases in PROVINCE_ALIASES.items():
                if any(re.search(rf"(?<!\w){re.escape(a)}(?!\w)", norm_lbl) for a in aliases):
                    for t in perception_taxes:
                        tn = _strip_accents(f"{t['name']} {t.get('description', '')}").lower()
                        if "iibb" in tn and prov_canonical in tn:
                            matched_tax = t
                            break
                    if matched_tax:
                        break

        enriched.append({
            "label": lbl,
            "amount": amt,
            "tax_id": matched_tax["id"] if matched_tax else None,
            "tax_name": matched_tax["name"] if matched_tax else "",
        })
    return enriched


def _infer_default_vat_tax(header: dict, vat_taxes: list):
    """Si una línea no tiene OC asociada o la OC no tenía IVA, infiere el IVA de compra (ej. 1IVA21C) desde el pie del PDF."""
    letter = (header.get("invoice_letter") or "A").upper()
    if letter in ("C", "E"):
        for t in vat_taxes:
            if "no corresp" in t["name"].lower() or "exen" in t["name"].lower() or t["amount"] == 0.0:
                return t
        return None

    net = float(header.get("net_taxable_base") or 0.0)
    vat = float(header.get("total_vat") or 0.0)
    target_rate = 21.0
    if net > 0 and vat > 0:
        ratio = (vat / net) * 100.0
        for candidate in (21.0, 10.5, 27.0, 5.0, 2.5, 0.0):
            if abs(ratio - candidate) <= 1.2:
                target_rate = candidate
                break
    elif net > 0 and vat == 0:
        target_rate = 0.0

    for t in vat_taxes:
        if abs(float(t["amount"]) - target_rate) < 0.1 and "iva" in t["name"].lower():
            return t
    return vat_taxes[0] if vat_taxes else None

def _single_code_similarity(inv_code: str, odoo_code: str) -> float:
    """
    Calcula similitud difusa entre un código de factura (ej. 'D101CDT10' o 'D101CDT10-034')
    y un código de Odoo/proveedor (ej. 'CDT 10', 'MC-CDT10', 'CDT 10 034').
    """
    a = re.sub(r"[^a-z0-9]", "", (inv_code or "").lower())
    b = re.sub(r"[^a-z0-9]", "", (odoo_code or "").lower())
    if not a or not b or len(a) < 2 or len(b) < 2:
        return 0.0
    if a == b:
        return 1.0

    # Quitar prefijos típicos de rubro de proveedor (ej: D101, C101, M102, N201, S101 = 1 letra + 3 dígitos)
    a_noprefix = re.sub(r"^[a-z]\d{3}", "", a)
    # Quitar prefijo de letras de Argelec si existiera (ej: MC, IMSA) cuando precede al mismo código
    b_noprefix = re.sub(r"^[a-z]{1,4}(?=[a-z]\d|\d)", "", b)

    if a_noprefix and len(a_noprefix) >= 3:
        if a_noprefix == b or a_noprefix == b_noprefix:
            return 0.96

    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    len_ratio = len(shorter) / len(longer)

    # Contención exacta del código corto dentro del largo (ej: 'cdt10' dentro de 'd101cdt10')
    if len(shorter) >= 3 and shorter in longer:
        if longer.endswith(shorter):
            # Termina exactamente igual (solo tenía prefijo agregado adelante)
            return 0.85 + 0.10 * len_ratio
        if longer.startswith(shorter):
            # Empieza igual pero le sobran caracteres atrás (ej: 'cdt10' en 'cdt10034') -> puntaje moderado
            return 0.55 + 0.30 * len_ratio
        return 0.65 + 0.25 * len_ratio

    # Subcadena común continua más larga + SequenceMatcher general
    lcs = SequenceMatcher(None, a, b).find_longest_match(0, len(a), 0, len(b)).size
    seq_ratio = SequenceMatcher(None, a, b).ratio()
    if lcs >= 4:
        return max(seq_ratio * 0.85, (lcs / max(len(a), len(b))) * 0.88)

    return seq_ratio * 0.55


def _score_product_codes(inv_codes: list, vendor_codes: list, internal_code: str, pl_desc: str) -> float:
    if not inv_codes:
        return 0.0

    candidates_high = [c for c in (vendor_codes or []) if c]
    candidates_med = [internal_code] if internal_code else []

    # También extraer posibles códigos entre corchetes o tokens alfanuméricos de la descripción de la OC
    bracket_codes = re.findall(r"\[([^\]]+)\]", pl_desc or "")
    candidates_med.extend(bracket_codes)

    best = 0.0
    for ic in inv_codes:
        for vc in candidates_high:
            # Peso completo si matchea contra product.supplierinfo.product_code
            best = max(best, _single_code_similarity(ic, vc))
        for mc in candidates_med:
            # Peso apenas menor (x0.97) contra default_code interno con agregados
            best = max(best, _single_code_similarity(ic, mc) * 0.97)

    return best

def format_odoo_exception(exc: Exception) -> dict:
    """Convierte Faults crudos de XML-RPC en mensajes limpios y detecta si hay una factura duplicada."""
    if isinstance(exc, xmlrpc.client.Fault):
        msg = exc.faultString or str(exc)
    else:
        msg = str(exc)
        m = re.search(r"<Fault\s+\d+:\s*(['\"])(.*)\1>", msg, flags=re.DOTALL)
        if m:
            msg = m.group(2)

    msg = msg.replace("\\n", "\n")
    # Separar viñetas pegadas (ej. '00240848• Compañía')
    msg = re.sub(r"([^\n])\s*•", r"\1\n•", msg)

    if "Traceback (most recent call last):" in msg:
        non_empty = [ln.strip() for ln in msg.strip().splitlines() if ln.strip()]
        msg = non_empty[-1] if non_empty else msg

    is_duplicate = ("duplicado" in msg.lower()) or ("ya existe un comprobante" in msg.lower())
    return {"message": msg.strip(), "is_duplicate": is_duplicate}


def resolve_secondary_currency_and_rate(
    company_id: int,
    allowed_company_ids: list,
    header: dict,
    purchase_orders: list,
) -> dict:
    """
    Si la Factura está en ARS y la OC vinculada está en otra moneda (ej. USD):
    1) Determina move_currency_id = moneda de la OC (USD).
    2) Si la factura en ARS mencionaba un TC impreso (reference_exchange_rate > 1), usa ese.
    3) Si no, consulta en Odoo el tipo de cambio inverso (ARS por 1 USD) vigente a la fecha de la factura (invoice_date).
    """
    inv_curr = (header.get("currency") or "ARS").upper()
    if not purchase_orders:
        return {"secondary_currency": None, "secondary_currency_id": None, "comparison_rate": 1.0, "rate_source": "none"}

    # Buscar si alguna OC asociada tiene moneda distinta a la factura (ej. OC en USD y factura en ARS)
    po_diff = next((po for po in purchase_orders if (po.get("currency") or "ARS").upper() != inv_curr), None)
    if not po_diff:
        return {"secondary_currency": None, "secondary_currency_id": None, "comparison_rate": 1.0, "rate_source": "same_currency"}

    po_curr_code = (po_diff.get("currency") or "USD").upper()
    po_curr_id = po_diff.get("currency_id")

    # Caso principal: Factura en ARS contra Orden de Compra en USD/EUR
    if inv_curr == "ARS":
        # 1° prioridad: TC de referencia impreso en el PDF (ej. Zoloda 'u$s: 1525.0000')
        pdf_ref_rate = float(header.get("reference_exchange_rate") or 0.0)
        if pdf_ref_rate > 1.0:
            return {
                "secondary_currency": po_curr_code,
                "secondary_currency_id": po_curr_id,
                "comparison_rate": round(pdf_ref_rate, 4),
                "rate_source": "pdf",
            }

        # 2° prioridad: Consultar TC en Odoo a la fecha de emisión de la factura
        _, db, uid, password, models = _get_odoo_service_connection()
        hierarchy_ids = _get_company_hierarchy(models, db, uid, password, int(company_id), allowed_company_ids)
        ctx = _make_company_context(int(company_id), allowed_company_ids)
        inv_date = (header.get("invoice_date") or "").strip() or datetime.now(AR_TZ).strftime("%Y-%m-%d")

        if not po_curr_id:
            currs = models.execute_kw(
                db, uid, password, "res.currency", "search_read",
                [[("name", "=", po_curr_code)]], {"fields": ["id"], "limit": 1, "context": ctx}
            )
            po_curr_id = int(currs[0]["id"]) if currs else None

        odoo_inv_rate = 0.0
        if po_curr_id:
            rate_meta = models.execute_kw(
                db, uid, password, "res.currency.rate", "fields_get",
                [["rate", "inverse_company_rate", "company_rate"]], {"attributes": ["type"]}
            )
            r_fields = ["name", "rate", "company_id"] + [f for f in ("inverse_company_rate", "company_rate") if f in rate_meta]

            # Buscar cotización <= fecha de factura en la compañía o compañía padre
            rate_recs = models.execute_kw(
                db, uid, password, "res.currency.rate", "search_read",
                [[
                    ("currency_id", "=", int(po_curr_id)),
                    ("name", "<=", inv_date),
                    "|", ("company_id", "=", False), ("company_id", "in", hierarchy_ids),
                ]],
                {"fields": r_fields, "order": "name desc, id desc", "limit": 5, "context": ctx},
            )
            if not rate_recs:
                # Si la factura tiene fecha futura sin tasa cargada aún, tomar la última tasa disponible
                rate_recs = models.execute_kw(
                    db, uid, password, "res.currency.rate", "search_read",
                    [[
                        ("currency_id", "=", int(po_curr_id)),
                        "|", ("company_id", "=", False), ("company_id", "in", hierarchy_ids),
                    ]],
                    {"fields": r_fields, "order": "name desc, id desc", "limit": 1, "context": ctx},
                )

            if rate_recs:
                r0 = rate_recs[0]
                if r0.get("inverse_company_rate") and float(r0["inverse_company_rate"]) > 1.0:
                    odoo_inv_rate = float(r0["inverse_company_rate"])
                elif r0.get("rate") and float(r0["rate"]) > 0:
                    raw_r = float(r0["rate"])
                    odoo_inv_rate = round(1.0 / raw_r, 4) if raw_r < 1.0 else round(raw_r, 4)

        return {
            "secondary_currency": po_curr_code,
            "secondary_currency_id": po_curr_id,
            "comparison_rate": round(odoo_inv_rate, 4) if odoo_inv_rate > 0 else 1.0,
            "rate_source": f"odoo ({inv_date})",
        }

    # Caso inverso: Factura en USD y OC en ARS
    ex_rate = float(header.get("exchange_rate") or 1.0)
    return {
        "secondary_currency": po_curr_code,
        "secondary_currency_id": po_curr_id,
        "comparison_rate": ex_rate if ex_rate > 1.0 else 1.0,
        "rate_source": "pdf",
    }

COMMON_PACK_SIZES = (2, 4, 5, 6, 10, 12, 15, 20, 24, 25, 30, 40, 50, 60, 100, 120, 200, 250, 500, 1000)


def auto_match_invoice_lines_to_pos(
    invoice_lines: list,
    purchase_orders: list,
    header: dict = None,
    vat_taxes: list = None,
) -> list:
    pool = []
    for po in purchase_orders:
        for pl in po.get("lines", []):
            pool.append(pl)

    fallback_vat = _infer_default_vat_tax(header or {}, vat_taxes or [])
    fallback_lbl = (fallback_vat.get("short_label") or fallback_vat["name"]) if fallback_vat else "Seleccionar IVA"

    if not pool:
        return [
            {
                **inv_ln,
                "matched_po_line_id": None,
                "matched_po_name": "",
                "product_id": None,
                "odoo_product_label": "",
                "po_qty_pending": None,
                "po_price_unit": None,
                "po_discount": None,
                "po_eff_unit_price": None,
                "tax_ids": [fallback_vat["id"]] if fallback_vat else [],
                "tax_names": fallback_lbl,
                "match_confidence": 0,
            }
            for inv_ln in invoice_lines
        ]

    comp_rate = float((header or {}).get("comparison_rate") or 1.0)
    inv_curr = ((header or {}).get("currency") or "ARS").upper()

    candidate_pairs = []  # (score, inv_idx, pl_idx, should_unpack, inferred_pack)
    for inv_idx, inv_ln in enumerate(invoice_lines):
        inv_codes = inv_ln.get("product_codes") or [
            c.strip() for c in re.split(r"[/|;]+", inv_ln.get("product_code") or "") if c.strip()
        ]
        inv_desc = (inv_ln.get("description") or "").strip().lower()
        billed_qty = float(inv_ln.get("billed_qty") or inv_ln.get("quantity") or 1.0)
        explicit_pack = float(inv_ln.get("pack_size") or 1.0)
        # Subtotal neto fijo de la línea en moneda de la factura
        line_sub = float(inv_ln.get("line_net_total") or (
            float(inv_ln.get("quantity") or 1.0) * float(inv_ln.get("unit_price") or 0.0) * (1.0 - float(inv_ln.get("discount_percentage") or 0.0) / 100.0)
        ))
        billed_eff_unit = (line_sub / billed_qty) if billed_qty > 0 else 0.0
        comp_billed_unit = (billed_eff_unit / comp_rate) if (inv_curr == "ARS" and comp_rate > 1.0) else billed_eff_unit

        for pl_idx, pl in enumerate(pool):
            pl_desc = f"{pl.get('product_name', '')} {pl.get('description', '')}".strip().lower()
            code_sim = _score_product_codes(
                inv_codes=inv_codes,
                vendor_codes=pl.get("vendor_codes", []),
                internal_code=pl.get("default_code", ""),
                pl_desc=pl_desc,
            )
            desc_sim = SequenceMatcher(None, inv_desc, pl_desc).ratio() if (inv_desc and pl_desc) else 0.0

            ref_qty = pl["qty_pending"] if pl["qty_pending"] > 0 else pl["product_qty"]
            # Precio neto efectivo de la OC (considerando descuento cargado en la OC, ej. -37% o -44.79%)
            pl_eff_unit = float(pl["price_unit"] or 0.0) * (1.0 - float(pl.get("discount") or 0.0) / 100.0)

            # Opción A: Sin multiplicar por pack (1 a 1)
            qty_bonus_a = 0.0
            if ref_qty > 0 and billed_qty > 0:
                if abs(billed_qty - ref_qty) < 0.01:
                    qty_bonus_a = 0.18
                elif abs(billed_qty - ref_qty) / max(ref_qty, billed_qty) <= 0.15:
                    qty_bonus_a = 0.08

            price_bonus_a = 0.0
            if pl_eff_unit > 0 and comp_billed_unit > 0:
                if abs(comp_billed_unit - pl_eff_unit) / pl_eff_unit <= 0.04:
                    price_bonus_a = 0.14

            best_variant_bonus = qty_bonus_a + price_bonus_a
            use_unpack = False
            chosen_pack = explicit_pack

            # Opción B: Multiplicando por pack explícito del PDF (ej. 'Caja x 25 unid.' -> 2 * 25 = 50)
            pack_candidates = [explicit_pack] if explicit_pack > 1.0 else []
            # Si el PDF no aclaró pack pero la OC pide un múltiplo típico exacto y el código coincide, probar ese múltiplo
            if not pack_candidates and billed_qty > 0 and ref_qty > billed_qty and code_sim >= 0.68:
                ratio = round(ref_qty / billed_qty, 4)
                if any(abs(ratio - cp) < 0.01 for cp in COMMON_PACK_SIZES):
                    pack_candidates.append(float(round(ratio)))

            for pk in pack_candidates:
                unpacked_qty = billed_qty * pk
                comp_unpacked_unit = comp_billed_unit / pk if pk > 0 else comp_billed_unit
                qb = 0.0
                if ref_qty > 0:
                    if abs(unpacked_qty - ref_qty) < 0.01:
                        qb = 0.20
                    elif abs(unpacked_qty - ref_qty) / max(ref_qty, unpacked_qty) <= 0.20:
                        qb = 0.10
                pb = 0.0
                if pl_eff_unit > 0 and comp_unpacked_unit > 0:
                    if abs(comp_unpacked_unit - pl_eff_unit) / pl_eff_unit <= 0.05:
                        pb = 0.16
                # Si al desempaquetar mejora claramente la coincidencia de cantidad o precio con la OC, activamos el pack
                if (qb + pb) > best_variant_bonus and (qb > 0 or pb > 0):
                    best_variant_bonus = qb + pb
                    use_unpack = True
                    chosen_pack = pk

            total_score = (code_sim * 0.65) + (desc_sim * 0.25) + best_variant_bonus
            candidate_pairs.append((total_score, inv_idx, pl_idx, use_unpack, chosen_pack))

    candidate_pairs.sort(key=lambda x: x[0], reverse=True)

    assigned_inv = {}
    used_pl = set()
    for score, inv_idx, pl_idx, use_unpack, chosen_pack in candidate_pairs:
        if score < 0.26:
            break
        if inv_idx not in assigned_inv and pl_idx not in used_pl:
            assigned_inv[inv_idx] = (pool[pl_idx], score, use_unpack, chosen_pack)
            used_pl.add(pl_idx)

    matched_out = []
    for inv_idx, inv_ln in enumerate(invoice_lines):
        billed_qty = float(inv_ln.get("billed_qty") or inv_ln.get("quantity") or 1.0)
        billed_price = float(inv_ln.get("billed_unit_price") or inv_ln.get("unit_price") or 0.0)
        disc_pct = round(float(inv_ln.get("discount_percentage") or 0.0), 2)

        if inv_idx in assigned_inv:
            best_pl, best_score, use_unpack, chosen_pack = assigned_inv[inv_idx]
            eff_qty = round(billed_qty * chosen_pack, 4) if use_unpack else billed_qty
            eff_list_price = round(billed_price / chosen_pack, 4) if (use_unpack and chosen_pack > 0) else billed_price
            pl_eff_unit = round(float(best_pl["price_unit"] or 0.0) * (1.0 - float(best_pl.get("discount") or 0.0) / 100.0), 4)

            line_tax_ids = best_pl["tax_ids"] if best_pl["tax_ids"] else ([fallback_vat["id"]] if fallback_vat else [])
            line_tax_names = best_pl["tax_names"] if best_pl["tax_ids"] else fallback_lbl

            matched_out.append({
                **inv_ln,
                "billed_qty": billed_qty,
                "billed_unit_price": billed_price,
                "can_unpack": bool(use_unpack and chosen_pack > 1.0),
                "pack_size": chosen_pack,
                "pack_applied": use_unpack,
                "quantity": eff_qty,
                "unit_price": eff_list_price,
                "discount_percentage": disc_pct,
                "line_net_total": round(eff_qty * eff_list_price * (1.0 - disc_pct / 100.0), 2),
                "matched_po_line_id": best_pl["po_line_id"],
                "matched_po_name": best_pl["po_name"],
                "product_id": best_pl["product_id"],
                "odoo_product_label": f"[{best_pl['default_code']}] {best_pl['product_name']}" if best_pl.get("default_code") else best_pl["product_name"],
                "po_qty_pending": best_pl["qty_pending"],
                "po_price_unit": best_pl["price_unit"],
                "po_discount": best_pl["discount"],
                "po_eff_unit_price": pl_eff_unit,
                "tax_ids": line_tax_ids,
                "tax_names": line_tax_names,
                "match_confidence": round(min(best_score, 1.0) * 100),
            })
        else:
            matched_out.append({
                **inv_ln,
                "billed_qty": billed_qty,
                "billed_unit_price": billed_price,
                "can_unpack": bool(float(inv_ln.get("pack_size") or 1.0) > 1.0),
                "matched_po_line_id": None,
                "matched_po_name": "",
                "product_id": None,
                "odoo_product_label": "",
                "po_qty_pending": None,
                "po_price_unit": None,
                "po_discount": None,
                "po_eff_unit_price": None,
                "tax_ids": [fallback_vat["id"]] if fallback_vat else [],
                "tax_names": fallback_lbl,
                "match_confidence": 0,
            })

    return matched_out

def _resolve_ar_latam_document_type(models, db, uid, password, doc_type_str: str, letter: str, ctx: dict):
    afip_code_map = {
        ("FACTURA", "A"): ("1", "FA-A"),
        ("NOTA_DEBITO", "A"): ("2", "ND-A"),
        ("NOTA_CREDITO", "A"): ("3", "NC-A"),
        ("FACTURA", "B"): ("6", "FA-B"),
        ("NOTA_DEBITO", "B"): ("7", "ND-B"),
        ("NOTA_CREDITO", "B"): ("8", "NC-B"),
        ("FACTURA", "C"): ("11", "FA-C"),
        ("NOTA_DEBITO", "C"): ("12", "ND-C"),
        ("NOTA_CREDITO", "C"): ("13", "NC-C"),
        ("FACTURA", "M"): ("51", "FA-M"),
        ("NOTA_DEBITO", "M"): ("52", "ND-M"),
        ("NOTA_CREDITO", "M"): ("53", "NC-M"),
        ("FACTURA", "E"): ("19", "FA-E"),
    }
    norm_doc = "NOTA_CREDITO" if "CRED" in doc_type_str else ("NOTA_DEBITO" if "DEB" in doc_type_str else "FACTURA")
    afip_code, expected_prefix = afip_code_map.get((norm_doc, letter), (None, f"FA-{letter}"))

    try:
        ar_countries = models.execute_kw(db, uid, password, "res.country", "search", [[("code", "=", "AR")]], {"limit": 1})
        country_domain = [("country_id", "=", ar_countries[0])] if ar_countries else []

        if afip_code:
            recs = models.execute_kw(
                db, uid, password, "l10n_latam.document.type", "search_read",
                [country_domain + [("active", "=", True), ("code", "in", [afip_code, afip_code.zfill(2), afip_code.zfill(3)])]],
                {"fields": ["id", "name", "code", "doc_code_prefix"], "order": "id asc", "limit": 5, "context": ctx},
            )
            if recs:
                best = next((r for r in recs if (r.get("doc_code_prefix") or "").upper().startswith(expected_prefix)), recs[0])
                return int(best["id"])

        recs = models.execute_kw(
            db, uid, password, "l10n_latam.document.type", "search_read",
            [country_domain + [("active", "=", True), ("doc_code_prefix", "ilike", expected_prefix)]],
            {"fields": ["id", "name", "doc_code_prefix"], "limit": 3, "context": ctx},
        )
        if recs:
            return int(recs[0]["id"])
    except Exception as exc:
        print(f"[WARN] Error buscando l10n_latam.document.type AR: {exc}", flush=True)
    return None


def _apply_perception_amounts_in_odoo(
    models, db, uid, password, move_id: int, valid_perceptions: list, is_refund: bool, curr_code: str, ex_rate: float, ctx: dict
):
    """
    Ajusta el importe exacto de cada percepción en Odoo una vez creado el borrador:
    1) Soporta l10n_ar_ux (account.move.tax / move_tax_ids) si está instalado.
    2) Soporta Odoo 17/18 estándar / Enterprise editando el apunte de impuesto (tax_line_id) en moneda extranjera o ARS
       y recalibrando la línea por pagar (liability_payable) para que el asiento cuadre al centavo.
    """
    if not valid_perceptions:
        return

    am_meta = models.execute_kw(db, uid, password, "account.move", "fields_get", [[]], {"attributes": ["type", "relation"]})

    # Variante A: si tienen instalado ADHOC l10n_ar_ux con tabla move_tax_ids
    if "move_tax_ids" in am_meta:
        try:
            rel_model = am_meta["move_tax_ids"].get("relation") or "account.move.tax"
            existing_mt = models.execute_kw(
                db, uid, password, rel_model, "search_read",
                [[("move_id", "=", int(move_id))]],
                {"fields": ["id", "tax_id", "tax_amount"], "context": ctx},
            )
            mt_by_tax = {int(r["tax_id"][0]): int(r["id"]) for r in existing_mt if isinstance(r.get("tax_id"), list)}
            for p in valid_perceptions:
                tid = int(p["tax_id"])
                amt = float(p["amount"])
                if tid in mt_by_tax:
                    models.execute_kw(db, uid, password, rel_model, "write", [[mt_by_tax[tid]], {"tax_amount": amt}], {"context": ctx})
                else:
                    models.execute_kw(db, uid, password, rel_model, "create", [{"move_id": int(move_id), "tax_id": tid, "tax_amount": amt}], {"context": ctx})
            return
        except Exception:
            pass

    # Variante B: Odoo 17/18 nativo -> ajustar line_ids donde tax_line_id == tax_id y cuadrar cuenta a pagar
    try:
        move_data = models.execute_kw(
            db, uid, password, "account.move", "read",
            [[int(move_id)]],
            {"fields": ["line_ids", "currency_id", "company_currency_id"], "context": ctx},
        )[0]
        aml_ids = move_data.get("line_ids") or []
        if not aml_ids:
            return

        amls = models.execute_kw(
            db, uid, password, "account.move.line", "read",
            [aml_ids],
            {"fields": ["id", "display_type", "tax_line_id", "account_id", "amount_currency", "debit", "credit", "balance"], "context": ctx},
        )

        doc_curr_id = move_data["currency_id"][0] if isinstance(move_data.get("currency_id"), list) else None
        comp_curr_id = move_data["company_currency_id"][0] if isinstance(move_data.get("company_currency_id"), list) else doc_curr_id
        is_foreign = (doc_curr_id != comp_curr_id)

        # Identificar la línea de cuenta a pagar (liability_payable)
        acc_ids = list({int(l["account_id"][0]) for l in amls if isinstance(l.get("account_id"), list)})
        accs = models.execute_kw(db, uid, password, "account.account", "read", [acc_ids], {"fields": ["id", "account_type"], "context": ctx})
        payable_acc_ids = {int(a["id"]) for a in accs if a.get("account_type") == "liability_payable"}

        payable_line = next(
            (l for l in amls if l.get("display_type") == "payment_term" or (isinstance(l.get("account_id"), list) and int(l["account_id"][0]) in payable_acc_ids)),
            None,
        )
        tax_lines_by_tid = {
            int(l["tax_line_id"][0]): l
            for l in amls
            if isinstance(l.get("tax_line_id"), list)
        }

        line_commands = []
        delta_amt_currency = 0.0
        delta_balance = 0.0

        # Signo en Odoo: en in_invoice los gastos/impuestos tienen amount_currency > 0 y balance > 0 (debit)
        sign = -1.0 if is_refund else 1.0

        for p in valid_perceptions:
            tid = int(p["tax_id"])
            amt = round(float(p["amount"]), 2)
            if amt <= 0:
                continue

            target_amt_curr = round(sign * amt, 2)
            rate_to_use = ex_rate if (is_foreign and ex_rate > 0) else 1.0
            target_bal = round(target_amt_curr * rate_to_use, 2)

            if tid in tax_lines_by_tid:
                tl = tax_lines_by_tid[tid]
                old_amt_curr = float(tl.get("amount_currency") or 0.0)
                old_bal = float(tl.get("balance") or 0.0)

                delta_amt_currency += (target_amt_curr - old_amt_curr)
                delta_balance += (target_bal - old_bal)

                upd_vals = {
                    "amount_currency": target_amt_curr,
                    "debit": target_bal if target_bal > 0 else 0.0,
                    "credit": abs(target_bal) if target_bal < 0 else 0.0,
                }
                line_commands.append((1, int(tl["id"]), upd_vals))
            else:
                # Si el impuesto era 0% exacto y Odoo no creó tax_line, obtener su cuenta de repartición y crear el apunte
                reps = models.execute_kw(
                    db, uid, password, "account.tax.repartition.line", "search_read",
                    [[("tax_id", "=", tid), ("repartition_type", "=", "tax"), ("document_type", "=", "refund" if is_refund else "invoice")]],
                    {"fields": ["id", "account_id"], "limit": 1, "context": ctx},
                )
                if reps and isinstance(reps[0].get("account_id"), list):
                    delta_amt_currency += target_amt_curr
                    delta_balance += target_bal
                    line_commands.append((0, 0, {
                        "name": p.get("tax_name") or p.get("label") or "Percepción",
                        "display_type": "tax",
                        "tax_line_id": tid,
                        "tax_repartition_line_id": int(reps[0]["id"]),
                        "account_id": int(reps[0]["account_id"][0]),
                        "amount_currency": target_amt_curr,
                        "debit": target_bal if target_bal > 0 else 0.0,
                        "credit": abs(target_bal) if target_bal < 0 else 0.0,
                    }))

        if line_commands and payable_line:
            new_pay_curr = round(float(payable_line.get("amount_currency") or 0.0) - delta_amt_currency, 2)
            new_pay_bal = round(float(payable_line.get("balance") or 0.0) - delta_balance, 2)
            line_commands.append((1, int(payable_line["id"]), {
                "amount_currency": new_pay_curr,
                "debit": new_pay_bal if new_pay_bal > 0 else 0.0,
                "credit": abs(new_pay_bal) if new_pay_bal < 0 else 0.0,
            }))

            write_ctx = dict(ctx, check_move_validity=False, skip_invoice_sync=True)
            models.execute_kw(
                db, uid, password, "account.move", "write",
                [[int(move_id)], {"line_ids": line_commands}],
                {"context": write_ctx},
            )
    except Exception as exc:
        print(f"[WARN] No se pudo ajustar monto manual de percepción en line_ids: {exc}", flush=True)

def _calibrate_vat_exact_to_pdf(
    models, db, uid, password, move_id: int, target_vat_pdf: float, valid_perceptions: list, is_refund: bool, ex_rate: float, ctx: dict
):
    """Si el IVA calculado por Odoo difiere por centavos respecto al IVA impreso en el PDF, ajusta el apunte de IVA y la cuenta por pagar."""
    if target_vat_pdf <= 0:
        return
    perc_tax_ids = {int(p["tax_id"]) for p in (valid_perceptions or []) if p.get("tax_id")}
    am_meta = models.execute_kw(db, uid, password, "account.move", "fields_get", [[]], {"attributes": ["type", "relation"]})

    # Caso A: Si existe tabla move_tax_ids (ADHOC l10n_ar_ux)
    if "move_tax_ids" in am_meta:
        try:
            rel_model = am_meta["move_tax_ids"].get("relation") or "account.move.tax"
            mt_rows = models.execute_kw(
                db, uid, password, rel_model, "search_read",
                [[("move_id", "=", int(move_id))]],
                {"fields": ["id", "tax_id", "tax_amount"], "context": ctx},
            )
            vat_rows = [r for r in mt_rows if isinstance(r.get("tax_id"), list) and int(r["tax_id"][0]) not in perc_tax_ids]
            if vat_rows:
                curr_vat = round(sum(float(r.get("tax_amount") or 0.0) for r in vat_rows), 2)
                diff_vat = round(target_vat_pdf - curr_vat, 2)
                if 0 < abs(diff_vat) <= 25.0:
                    main_row = max(vat_rows, key=lambda r: float(r.get("tax_amount") or 0.0))
                    new_amt = round(float(main_row.get("tax_amount") or 0.0) + diff_vat, 2)
                    models.execute_kw(db, uid, password, rel_model, "write", [[int(main_row["id"])], {"tax_amount": new_amt}], {"context": ctx})
                    return
        except Exception:
            pass

    # Caso B: Ajuste directo sobre account.move.line (Odoo 17/18 estándar)
    try:
        move_data = models.execute_kw(
            db, uid, password, "account.move", "read", [[int(move_id)]],
            {"fields": ["line_ids", "currency_id", "company_currency_id"], "context": ctx},
        )[0]
        amls = models.execute_kw(
            db, uid, password, "account.move.line", "read", [move_data.get("line_ids") or []],
            {"fields": ["id", "display_type", "tax_line_id", "account_id", "amount_currency", "balance"], "context": ctx},
        )
        doc_curr = move_data["currency_id"][0] if isinstance(move_data.get("currency_id"), list) else None
        comp_curr = move_data["company_currency_id"][0] if isinstance(move_data.get("company_currency_id"), list) else doc_curr
        is_foreign = (doc_curr != comp_curr)

        vat_amls = [
            l for l in amls
            if isinstance(l.get("tax_line_id"), list) and int(l["tax_line_id"][0]) not in perc_tax_ids
        ]
        if not vat_amls:
            return

        sign = -1.0 if is_refund else 1.0
        current_vat_curr = round(sum(sign * float(l.get("amount_currency") or 0.0) for l in vat_amls), 2)
        diff_vat = round(target_vat_pdf - current_vat_curr, 2)
        if abs(diff_vat) == 0 or abs(diff_vat) > 25.0:
            return

        acc_ids = list({int(l["account_id"][0]) for l in amls if isinstance(l.get("account_id"), list)})
        accs = models.execute_kw(db, uid, password, "account.account", "read", [acc_ids], {"fields": ["id", "account_type"], "context": ctx})
        payable_ids = {int(a["id"]) for a in accs if a.get("account_type") == "liability_payable"}
        payable_line = next(
            (l for l in amls if l.get("display_type") == "payment_term" or (isinstance(l.get("account_id"), list) and int(l["account_id"][0]) in payable_ids)),
            None,
        )
        if not payable_line:
            return

        target_tl = max(vat_amls, key=lambda l: abs(float(l.get("amount_currency") or 0.0)))
        delta_curr = round(sign * diff_vat, 2)
        rate_to_use = ex_rate if (is_foreign and ex_rate > 0) else 1.0
        delta_bal = round(delta_curr * rate_to_use, 2)

        new_tl_curr = round(float(target_tl.get("amount_currency") or 0.0) + delta_curr, 2)
        new_tl_bal = round(float(target_tl.get("balance") or 0.0) + delta_bal, 2)

        new_pay_curr = round(float(payable_line.get("amount_currency") or 0.0) - delta_curr, 2)
        new_pay_bal = round(float(payable_line.get("balance") or 0.0) - delta_bal, 2)

        cmds = [
            (1, int(target_tl["id"]), {
                "amount_currency": new_tl_curr,
                "debit": new_tl_bal if new_tl_bal > 0 else 0.0,
                "credit": abs(new_tl_bal) if new_tl_bal < 0 else 0.0,
            }),
            (1, int(payable_line["id"]), {
                "amount_currency": new_pay_curr,
                "debit": new_pay_bal if new_pay_bal > 0 else 0.0,
                "credit": abs(new_pay_bal) if new_pay_bal < 0 else 0.0,
            }),
        ]
        write_ctx = dict(ctx, check_move_validity=False, skip_invoice_sync=True)
        models.execute_kw(db, uid, password, "account.move", "write", [[int(move_id)], {"line_ids": cmds}], {"context": write_ctx})
    except Exception as exc:
        print(f"[WARN] Ajuste fino de IVA omitido: {exc}", flush=True)

def create_draft_vendor_bill(
    company_id: int,
    allowed_company_ids: list,
    partner_id: int,
    header: dict,
    lines: list,
    perceptions: list,
    creator_user: dict,
    attachment_b64: str = None,
    attachment_filename: str = None,
    attachment_mimetype: str = None,
) -> dict:
    url, db, uid, password, models = _get_odoo_service_connection()
    ctx = _make_company_context(int(company_id), allowed_company_ids)

    doc_type_str = (header.get("document_type") or "FACTURA").upper()
    letter = (header.get("invoice_letter") or "A").upper()
    is_refund = ("CRED" in doc_type_str)
    move_type = "in_refund" if is_refund else "in_invoice"

    am_meta = models.execute_kw(db, uid, password, "account.move", "fields_get", [[]], {"attributes": ["type", "relation"]})
    aml_meta = models.execute_kw(db, uid, password, "account.move.line", "fields_get", [[]], {"attributes": ["type"]})

    # 1. Moneda y Diario de Compras (con l10n_latam_use_documents activo si existe)
    curr_code = (header.get("currency") or "ARS").upper()
    currs = models.execute_kw(
        db, uid, password, "res.currency", "search_read",
        [[("name", "=", curr_code), "|", ("active", "=", True), ("active", "=", False)]],
        {"fields": ["id", "name"], "limit": 1, "context": ctx},
    )
    currency_id = int(currs[0]["id"]) if currs else None
    

    
    journals = models.execute_kw(
        db, uid, password, "account.journal", "search_read",
        [[("company_id", "=", int(company_id)), ("type", "=", "purchase")]],
        {"fields": ["id", "name", "l10n_latam_use_documents"], "order": "sequence asc, id asc", "context": ctx},
    )
    # Preferir el diario de compras que usa documentos fiscales ARCA/AFIP
    latam_journals = [j for j in journals if j.get("l10n_latam_use_documents")]
    journal_id = int((latam_journals[0] if latam_journals else journals[0])["id"]) if journals else None

    # 2. Tipo de documento AFIP (Factura A = código 1) y Número normalizado 00033-00048461
    latam_doc_type_id = None
    if "l10n_latam_document_type_id" in am_meta:
        latam_doc_type_id = _resolve_ar_latam_document_type(models, db, uid, password, doc_type_str, letter, ctx)

    inv_num = _normalize_ar_invoice_number(header.get("invoice_number") or "")

    # 3. Percepciones válidas con tax_id seleccionado
    valid_perceptions = [
        p for p in (perceptions or [])
        if p.get("tax_id") and float(p.get("amount") or 0.0) > 0
    ]
    perc_tax_ids = [int(p["tax_id"]) for p in valid_perceptions]

    # 4. Construir líneas de factura: cada línea lleva su IVA (ej. 1IVA21C) + las percepciones en la primera línea gravada
    invoice_line_cmds = []
    linked_po_names = set()
    tax_field = "tax_ids" if "tax_ids" in aml_meta else "invoice_line_tax_ids"

    # Respetar Precio Lista + % Descuento en cada línea, y agregar 1 línea final de ajuste si hay diferencia de redondeo vs Neto PDF
    price_digits = 4
    try:
        dp_recs = models.execute_kw(
            db, uid, password, "decimal.precision", "search_read",
            [[("name", "=", "Product Price")]], {"fields": ["digits"], "limit": 1, "context": ctx}
        )
        if dp_recs and dp_recs[0].get("digits") is not None:
            price_digits = int(dp_recs[0]["digits"])
    except Exception:
        pass    

    

    for idx, ln in enumerate(lines):
        line_taxes = [int(t) for t in (ln.get("tax_ids") or []) if t]
        # Agregar los impuestos de percepción a las líneas gravadas para que Odoo vincule la base imponible
        if perc_tax_ids and idx == 0:
            line_taxes = list(dict.fromkeys(line_taxes + perc_tax_ids))

        vals = {
            "sequence": (idx + 1) * 10,
            "name": str(ln.get("description") or "Ítem facturado"),
            "quantity": float(ln.get("quantity") or 1.0),
            "price_unit": float(ln.get("unit_price") or 0.0),
            tax_field: [(6, 0, line_taxes)],
        }
        if "discount" in aml_meta:
            vals["discount"] = float(ln.get("discount_percentage") or 0.0)
        if ln.get("product_id"):
            vals["product_id"] = int(ln["product_id"])
        if ln.get("purchase_line_id") and "purchase_line_id" in aml_meta:
            vals["purchase_line_id"] = int(ln["purchase_line_id"])
        if ln.get("po_name"):
            linked_po_names.add(str(ln["po_name"]))

        invoice_line_cmds.append((0, 0, vals))

    # 5. Cabecera de account.move
    move_vals = {
        "move_type": move_type,
        "company_id": int(company_id),
        "partner_id": int(partner_id),
        "invoice_line_ids": invoice_line_cmds,
    }
    if journal_id:
        move_vals["journal_id"] = journal_id
    if currency_id:
        move_vals["currency_id"] = currency_id
    sec_curr_id = header.get("secondary_currency_id")
    comp_rate = float(header.get("comparison_rate") or 0.0)
    if curr_code == "ARS" and sec_curr_id and comp_rate > 1.0:
        if "move_currency_id" in am_meta:
            move_vals["move_currency_id"] = int(sec_curr_id)
        if "move_inverse_currency_rate" in am_meta:
            move_vals["move_inverse_currency_rate"] = comp_rate
    if header.get("invoice_date"):
        move_vals["invoice_date"] = header["invoice_date"]
        move_vals["date"] = header["invoice_date"]
    if header.get("due_date"):
        # Caso 1: La factura tiene fecha de vencimiento explícita (o calculada por días)
        move_vals["invoice_date_due"] =  header["due_date"]
    else:
        # Caso 2: El PDF no tiene fecha de vencimiento -> heredar Plazo de Pago de la OC o del Proveedor
        payment_term_id = None

        # A) Intentar tomar el plazo de pago de la Orden de Compra vinculada
        if linked_po_names and "invoice_payment_term_id" in am_meta:
            po_recs = models.execute_kw(
                db, uid, password, "purchase.order", "search_read",
                [[("company_id", "=", int(company_id)), ("name", "in", list(linked_po_names))]],
                {"fields": ["payment_term_id"], "limit": 1, "context": ctx},
            )
            if po_recs and isinstance(po_recs[0].get("payment_term_id"), list):
                payment_term_id = int(po_recs[0]["payment_term_id"][0])

        # B) Si no había OC o la OC no tenía plazo, tomar el plazo por defecto de la ficha del Proveedor
        if not payment_term_id and "invoice_payment_term_id" in am_meta:
            vend_recs = models.execute_kw(
                db, uid, password, "res.partner", "read",
                [[int(partner_id)]],
                {"fields": ["property_supplier_payment_term_id"], "context": ctx},
            )
            if vend_recs and isinstance(vend_recs[0].get("property_supplier_payment_term_id"), list):
                payment_term_id = int(vend_recs[0]["property_supplier_payment_term_id"][0])

        if payment_term_id:
            move_vals["invoice_payment_term_id"] = payment_term_id
  
    if latam_doc_type_id and "l10n_latam_document_type_id" in am_meta:
        move_vals["l10n_latam_document_type_id"] = latam_doc_type_id
    if inv_num:
        if "l10n_latam_document_number" in am_meta:
            move_vals["l10n_latam_document_number"] = inv_num
        else:
            move_vals["ref"] = inv_num

    if linked_po_names:
        move_vals["invoice_origin"] = ", ".join(sorted(linked_po_names))
    elif header.get("purchase_order_number"):
        move_vals["invoice_origin"] = header["purchase_order_number"]

    ex_rate = float(header.get("exchange_rate") or 1.0)
    if curr_code != "ARS" and ex_rate > 1.0:
        for rate_f in ("l10n_ar_currency_rate", "manual_currency_rate"):
            if rate_f in am_meta:
                move_vals[rate_f] = ex_rate
                break

    cae = (header.get("cae_number") or "").strip()
    if cae:
        for cae_f in ("l10n_ar_afip_auth_code", "afip_auth_code"):
            if cae_f in am_meta:
                move_vals[cae_f] = cae
                break

    # Crear factura en borrador
    move_id = models.execute_kw(db, uid, password, "account.move", "create", [move_vals], {"context": ctx})

    # Re-asegurar número y tipo de documento Latam post-create (por si un onchange de partner_id lo pisó)
    latam_post = {}
    if latam_doc_type_id and "l10n_latam_document_type_id" in am_meta:
        latam_post["l10n_latam_document_type_id"] = latam_doc_type_id
    if inv_num and "l10n_latam_document_number" in am_meta:
        latam_post["l10n_latam_document_number"] = inv_num
    if latam_post:
        try:
            models.execute_kw(db, uid, password, "account.move", "write", [[int(move_id)], latam_post], {"context": ctx})
        except Exception as e_lat:
            print(f"[WARN] l10n_latam post-write: {e_lat}", flush=True)

    # 6. Inyectar los montos exactos de las percepciones leídas en el PDF
    _apply_perception_amounts_in_odoo(
        models, db, uid, password, int(move_id), valid_perceptions, is_refund, curr_code, ex_rate, ctx
    )

    # 6b. Ajuste automático de Total General: comparar amount_total real en Odoo vs Total del Comprobante PDF
    # y agregar una línea por (diferencia_total / 1.21) con IVA 21% para que el Total General coincida al centavo.
    target_total_pdf = round(float(header.get("total_amount") or 0.0), 2)
    if target_total_pdf > 0:
        curr_move = models.execute_kw(
            db, uid, password, "account.move", "read",
            [[int(move_id)]],
            {"fields": ["amount_total", "amount_untaxed", "amount_tax"], "context": ctx},
        )[0]
        odoo_total_now = round(float(curr_move.get("amount_total") or 0.0), 2)
        diff_total = round(target_total_pdf - odoo_total_now, 2)

        if abs(diff_total) >= 0.01:
            # Buscar el impuesto IVA 21% de compras en esta compañía (preferir el ya usado en las líneas)
            vat21_id = None
            all_used_tids = list({int(t) for ln in lines for t in (ln.get("tax_ids") or []) if t and int(t) not in perc_tax_ids})
            if all_used_tids:
                used_tax_recs = models.execute_kw(
                    db, uid, password, "account.tax", "read", [all_used_tids],
                    {"fields": ["id", "amount"], "context": ctx},
                )
                t21 = next((r for r in used_tax_recs if abs(float(r.get("amount") or 0.0) - 21.0) < 0.1), None)
                if t21:
                    vat21_id = int(t21["id"])

            if not vat21_id:
                hierarchy_ids = _get_company_hierarchy(models, db, uid, password, int(company_id), allowed_company_ids)
                found_t21 = models.execute_kw(
                    db, uid, password, "account.tax", "search_read",
                    [[
                        ("type_tax_use", "=", "purchase"),
                        ("active", "=", True),
                        ("amount", "=", 21.0),
                        "|", ("company_id", "=", False), ("company_id", "in", hierarchy_ids),
                    ]],
                    {"fields": ["id"], "limit": 1, "context": ctx},
                )
                if found_t21:
                    vat21_id = int(found_t21[0]["id"])

            if vat21_id:
                raw_adj_net = round(diff_total / 1.21, 2)
                # Probar el centavo exacto por si al multiplicar x1.21 en Odoo el redondeo se desplaza $0.01
                adj_vals = {
                    "sequence": 999,
                    "name": "Ajuste por redondeo",
                    "quantity": 1.0,
                    "price_unit": raw_adj_net,
                    tax_field: [(6, 0, [vat21_id])],
                }
                models.execute_kw(
                    db, uid, password, "account.move", "write",
                    [[int(move_id)], {"invoice_line_ids": [(0, 0, adj_vals)]}],
                    {"context": ctx},
                )

                # Verificar si por redondeo del 21% quedó a +/- $0.01 del Total General y afinar ese centavo si hace falta
                recheck = models.execute_kw(
                    db, uid, password, "account.move", "read",
                    [[int(move_id)]],
                    {"fields": ["amount_total", "invoice_line_ids"], "context": ctx},
                )[0]
                rem_diff = round(target_total_pdf - float(recheck.get("amount_total") or 0.0), 2)
                if abs(rem_diff) in (0.01, 0.02) and recheck.get("invoice_line_ids"):
                    last_aml_id = int(recheck["invoice_line_ids"][-1])
                    fine_price = round(raw_adj_net + (0.01 if rem_diff > 0 else -0.01), 2)
                    models.execute_kw(
                        db, uid, password, "account.move", "write",
                        [[int(move_id)], {"invoice_line_ids": [(1, last_aml_id, {"price_unit": fine_price})]}],
                        {"context": ctx},
                    )
    

    # 7. Adjuntar PDF original como documento principal del visor derecho de Odoo
    if attachment_b64 and attachment_filename:
        try:
            att_id = models.execute_kw(
                db, uid, password, "ir.attachment", "create",
                [{
                    "name": attachment_filename,
                    "type": "binary",
                    "datas": attachment_b64,
                    "res_model": "account.move",
                    "res_id": int(move_id),
                    "mimetype": attachment_mimetype or "application/pdf",
                }],
                {"context": ctx},
            )
            if "message_main_attachment_id" in am_meta and att_id:
                models.execute_kw(
                    db, uid, password, "account.move", "write",
                    [[int(move_id)], {"message_main_attachment_id": int(att_id)}],
                    {"context": ctx},
                )
        except Exception:
            pass

    # 8. Mensaje limpio en el Chatter (usando body_is_html=True para que Odoo renderice el HTML en vez de escaparlo)
    creator_name = creator_user.get("name", "Usuario Web")
    perc_lines_html = "".join(
        f"<li>{p.get('label')} → Impuesto Odoo: <b>{p.get('tax_name') or 'Sin asignar'}</b>: <b>{curr_code} {float(p.get('amount') or 0):,.2f}</b></li>"
        for p in (perceptions or [])
    )
    body_html = (
        f"<p>Factura en borrador generada vía <b>Portal IA Gemini</b> por <b>{creator_name}</b>.</p>"
        f"<ul>"
        f"<li>Comprobante: <b>{doc_type_str} {letter} {inv_num}</b> (CAE: {cae or 'N/D'})</li>"
        f"<li>OC vinculadas: <b>{move_vals.get('invoice_origin') or 'Sin OC'}</b></li>"
        f"<li>Neto PDF: <b>{curr_code} {float(header.get('net_taxable_base') or 0):,.2f}</b> | "
        f"IVA PDF: <b>{curr_code} {float(header.get('total_vat') or 0):,.2f}</b> | "
        f"Percepciones PDF: <b>{curr_code} {float(header.get('other_taxes_perceptions') or 0):,.2f}</b> | "
        f"Total PDF: <b>{curr_code} {float(header.get('total_amount') or 0):,.2f}</b></li>"
        f"</ul>"
    )
    if perc_lines_html:
        body_html += f"<p><b>Percepciones detectadas y conciliadas:</b></p><ul>{perc_lines_html}</ul>"

    try:
        post_kw = {
            "body": body_html,
            "body_is_html": True,
            "message_type": "comment",
            "subtype_xmlid": "mail.mt_note",
            "context": ctx,
        }
        if creator_user.get("partner_id"):
            post_kw["author_id"] = int(creator_user["partner_id"])
        models.execute_kw(db, uid, password, "account.move", "message_post", [[int(move_id)]], post_kw)
    except Exception:
        # Si la versión de Odoo no acepta body_is_html por XML-RPC, enviar texto plano limpio sin tags HTML
        plain_text = re.sub(r"<li[^>]*>", "\n• ", body_html)
        plain_text = re.sub(r"</p>|</ul>", "\n", plain_text)
        plain_text = re.sub(r"<[^>]+>", "", plain_text).strip()
        try:
            models.execute_kw(
                db, uid, password, "account.move", "message_post",
                [[int(move_id)]],
                {"body": plain_text, "message_type": "comment", "subtype_xmlid": "mail.mt_note", "context": ctx},
            )
        except Exception:
            pass

    created = models.execute_kw(
        db, uid, password, "account.move", "read",
        [[int(move_id)]],
        {"fields": ["name", "ref", "l10n_latam_document_number", "amount_untaxed", "amount_tax", "amount_total", "state"], "context": ctx},
    )[0]

    return {
        "move_id": int(move_id),
        "move_name": created.get("name") or f"Borrador #{move_id}",
        "ref": created.get("l10n_latam_document_number") or created.get("ref") or inv_num,
        "amount_untaxed": float(created.get("amount_untaxed") or 0.0),
        "amount_tax": float(created.get("amount_tax") or 0.0),
        "amount_total": float(created.get("amount_total") or 0.0),
        "odoo_link": f"{url}/web#id={move_id}&model=account.move&view_type=form",
    }