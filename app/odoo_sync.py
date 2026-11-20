import xmlrpc.client
import time
import os
import json
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from app.database import replace_all_catalog, update_sync_status

BATCH_SIZE = 2500
MAX_WORKERS = 4

def run_full_sync():
    t0 = time.time()
    url = os.getenv("ODOO_URL", "").rstrip("/")
    db = os.getenv("ODOO_DB", "odoo")
    username = os.getenv("ODOO_USER", "")
    password = os.getenv("ODOO_API_KEY", "")

    update_sync_status("running", "Conectando con Odoo...")

    try:
        common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common", allow_none=True)
        uid = common.authenticate(db, username, password, {})
        if not uid:
            raise ConnectionError("Falló la autenticación XML-RPC con Odoo.")

        thread_local = threading.local()

        def get_models():
            if not hasattr(thread_local, "models"):
                thread_local.models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object", allow_none=True)
            return thread_local.models

        def execute_kw(model, method, args, kwargs=None):
            return get_models().execute_kw(db, uid, password, model, method, args, kwargs or {})

        # 0. Compañías y tipos de cambio por compañía
        companies = execute_kw("res.company", "search_read", [[]], {"fields": ["id", "name", "currency_id"]})
        all_company_ids = [c["id"] for c in companies]
        company_name_map = {c["id"]: c["name"] for c in companies}
        multi_ctx = {"allowed_company_ids": all_company_ids}

        def leer_en_paralelo(model, ids, fields, batch_size=BATCH_SIZE):
            if not ids:
                return []
            batches = [ids[i:i + batch_size] for i in range(0, len(ids), batch_size)]
            resultados = []
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = [
                    executor.submit(execute_kw, model, "read", [b], {"fields": fields, "context": multi_ctx})
                    for b in batches
                ]
                for future in as_completed(futures):
                    resultados.extend(future.result())
            return resultados

        # 1. Monedas, listas de precios, categorías y almacenes
        update_sync_status("running", "Descargando monedas, listas de precios y almacenes...")
        currency_rows = []
        for comp in companies:
            comp_id = int(comp["id"])
            base_curr_id = int(comp["currency_id"][0]) if isinstance(comp.get("currency_id"), list) else 1
            comp_ctx = {"allowed_company_ids": all_company_ids, "company_id": comp_id}
            currs = execute_kw(
                "res.currency",
                "search_read",
                [[("active", "=", True)]],
                {"fields": ["id", "name", "symbol", "rate"], "context": comp_ctx},
            )
            for c in currs:
                cid = int(c["id"])
                currency_rows.append((
                    comp_id,
                    cid,
                    c["name"],
                    c.get("symbol") or "$",
                    float(c.get("rate") or 1.0),
                    1 if cid == base_curr_id else 0,
                ))

        raw_pricelists = execute_kw(
            "product.pricelist",
            "search_read",
            [[("active", "=", True)]],
            {"fields": ["id", "name", "currency_id", "company_id", "sequence"], "context": multi_ctx},
        )
        pricelist_rows = []
        for pl in raw_pricelists:
            c_info = pl.get("currency_id")
            comp_info = pl.get("company_id")
            pricelist_rows.append((
                int(pl["id"]),
                pl["name"],
                int(c_info[0]) if isinstance(c_info, list) else 0,
                int(comp_info[0]) if isinstance(comp_info, list) else 0,
                int(pl.get("sequence") or 10),
            ))

        pli_meta = execute_kw("product.pricelist.item", "fields_get", [[]], {"attributes": ["type"]})
        wanted_pli_fields = [
            "id", "pricelist_id", "applied_on", "categ_id", "product_tmpl_id", "product_id",
            "min_quantity", "date_start", "date_end", "compute_price", "fixed_price",
            "percent_price", "price_discount", "price_surcharge", "price_round",
            "price_min_margin", "price_max_margin", "base", "base_pricelist_id", "currency_id",
        ]
        valid_pli_fields = [f for f in wanted_pli_fields if f in pli_meta]
        raw_pli = execute_kw(
            "product.pricelist.item",
            "search_read",
            [[]],
            {"fields": valid_pli_fields, "context": multi_ctx},
        )
        pricelist_item_rows = []
        for it in raw_pli:
            pl_info = it.get("pricelist_id")
            if not isinstance(pl_info, list):
                continue
            cat_info = it.get("categ_id")
            tmpl_info = it.get("product_tmpl_id")
            prod_info = it.get("product_id")
            bpl_info = it.get("base_pricelist_id")
            cur_info = it.get("currency_id")

            pricelist_item_rows.append((
                int(it["id"]),
                int(pl_info[0]),
                it.get("applied_on") or "3_global",
                int(cat_info[0]) if isinstance(cat_info, list) else 0,
                int(tmpl_info[0]) if isinstance(tmpl_info, list) else 0,
                int(prod_info[0]) if isinstance(prod_info, list) else 0,
                float(it.get("min_quantity") or 0.0),
                str(it.get("date_start") or ""),
                str(it.get("date_end") or ""),
                it.get("compute_price") or "fixed",
                float(it.get("fixed_price") or 0.0),
                float(it.get("percent_price") or 0.0),
                float(it.get("price_discount") or 0.0),
                float(it.get("price_surcharge") or 0.0),
                float(it.get("price_round") or 0.0),
                float(it.get("price_min_margin") or 0.0),
                float(it.get("price_max_margin") or 0.0),
                it.get("base") or "list_price",
                int(bpl_info[0]) if isinstance(bpl_info, list) else 0,
                int(cur_info[0]) if isinstance(cur_info, list) else 0,
            ))

        raw_categs = execute_kw("product.category", "search_read", [[]], {"fields": ["id", "parent_path"], "context": multi_ctx})
        category_rows = [(int(c["id"]), c.get("parent_path") or "") for c in raw_categs]

        # Metadatos de producto
        studio_field = "x_studio_selection_field_6ua_1j8tii7sp"
        all_prod_fields = execute_kw(
            "product.product",
            "fields_get",
            [[]],
            {"attributes": ["type", "relation", "selection"], "context": multi_ctx},
        )
        estado_selection_map = dict(all_prod_fields.get(studio_field, {}).get("selection", []))

        brand_field = None
        for candidate in ["product_brand_id", "brand_id", "x_studio_marca", "x_studio_brand_id"]:
            if candidate in all_prod_fields:
                brand_field = candidate
                break
        if not brand_field:
            for fname in all_prod_fields:
                if "brand" in fname or "marca" in fname:
                    brand_field = fname
                    break

        barcode_rel_model = all_prod_fields.get("barcode_ids", {}).get("relation")
        barcode_code_field = "name"
        if barcode_rel_model:
            bc_meta = execute_kw(barcode_rel_model, "fields_get", [[]], {"attributes": ["type"]})
            if "barcode" in bc_meta:
                barcode_code_field = "barcode"

        almacenes_info = execute_kw(
            "stock.warehouse",
            "search_read",
            [[]],
            {"fields": ["id", "name", "code", "company_id", "lot_stock_id"], "context": multi_ctx},
        )

        # 2. Stock disponible sin reservar (lot_stock_id) en TODOS los almacenes
        update_sync_status("running", f"Calculando stock en {len(almacenes_info)} almacenes...")
        warehouse_rows = []
        stock_rows = []

        for wh in almacenes_info:
            wh_id = wh["id"]
            wh_code = wh.get("code") or f"ALM-{wh_id}"
            wh_name = wh.get("name") or wh_code
            comp_info = wh.get("company_id")
            comp_id = comp_info[0] if isinstance(comp_info, list) else 0
            comp_name = company_name_map.get(comp_id, comp_info[1] if isinstance(comp_info, list) else "")

            warehouse_rows.append((wh_id, wh_name, wh_code, comp_id, comp_name))

            lot_stock = wh.get("lot_stock_id")
            if not lot_stock:
                continue
            lot_stock_id = lot_stock[0]

            quants_grouped = execute_kw(
                "stock.quant",
                "read_group",
                [
                    [["location_id", "child_of", lot_stock_id], ["location_id.usage", "=", "internal"]],
                    ["product_id", "quantity:sum", "reserved_quantity:sum"],
                    ["product_id"],
                ],
                {"lazy": False, "context": multi_ctx},
            )
            for g in quants_grouped:
                if g.get("product_id"):
                    prod_id = int(g["product_id"][0])
                    free_qty = round(float(g.get("quantity", 0.0)) - float(g.get("reserved_quantity", 0.0)), 4)
                    if free_qty != 0.0:
                        stock_rows.append((prod_id, wh_id, free_qty))

        # 3. Productos en paralelo
        product_ids = execute_kw("product.product", "search", [[]], {"context": multi_ctx})
        update_sync_status("running", f"Descargando {len(product_ids):,} productos en paralelo...")

        campos_lectura = [
            "default_code",
            "name",
            "list_price",
            "standard_price",
            "product_tmpl_id",
            "categ_id",
            "currency_id",
            "taxes_id",
            "barcode_ids",
            studio_field,
        ]
        if brand_field:
            campos_lectura.append(brand_field)

        productos_raw = leer_en_paralelo("product.product", product_ids, campos_lectura)

        # 4. Impuestos por compañía y códigos de barra
        update_sync_status("running", "Resolviendo impuestos por compañía y códigos de barra...")
        all_tax_ids = list({tid for p in productos_raw for tid in p.get("taxes_id", [])})
        taxes_records = leer_en_paralelo("account.tax", all_tax_ids, ["name", "company_id"], batch_size=5000)
        taxes_info_map = {}
        for t in taxes_records:
            c_info = t.get("company_id")
            if c_info:
                taxes_info_map[t["id"]] = (str(c_info[0]), t["name"])

        all_barcode_ids = list({bid for p in productos_raw for bid in p.get("barcode_ids", [])})
        barcodes_map = {}
        if all_barcode_ids and barcode_rel_model:
            bc_records = leer_en_paralelo(barcode_rel_model, all_barcode_ids, [barcode_code_field], batch_size=5000)
            barcodes_map = {b["id"]: str(b[barcode_code_field]) for b in bc_records if b.get(barcode_code_field)}

        # 5. Guardar todo en SQLite
        update_sync_status("running", "Guardando índice y listas de precios en base local...")
        product_rows = []
        for p in productos_raw:
            pid = int(p["id"])
            marca = ""
            if brand_field:
                b_val = p.get(brand_field)
                if isinstance(b_val, list) and len(b_val) > 1:
                    marca = str(b_val[1])
                elif isinstance(b_val, str):
                    marca = b_val

            taxes_by_comp = {}
            for tid in p.get("taxes_id", []):
                if tid in taxes_info_map:
                    c_str, t_name = taxes_info_map[tid]
                    taxes_by_comp.setdefault(c_str, []).append(t_name)
            taxes_json = json.dumps({k: ", ".join(v) for k, v in taxes_by_comp.items()})

            codigos_barra = ", ".join(barcodes_map[bid] for bid in p.get("barcode_ids", []) if bid in barcodes_map)

            estado_raw = p.get(studio_field)
            estado = estado_selection_map.get(estado_raw, estado_raw) if estado_raw else ""

            tmpl_info = p.get("product_tmpl_id")
            cat_info = p.get("categ_id")
            pcurr_info = p.get("currency_id")

            product_rows.append((
                pid,
                p.get("default_code") or "",
                p.get("name") or "",
                float(p.get("list_price") or 0.0),
                float(p.get("standard_price") or 0.0),
                int(tmpl_info[0]) if isinstance(tmpl_info, list) else 0,
                int(cat_info[0]) if isinstance(cat_info, list) else 0,
                int(pcurr_info[0]) if isinstance(pcurr_info, list) else 0,
                taxes_json,
                marca,
                codigos_barra,
                estado,
            ))

        replace_all_catalog(
            product_rows=product_rows,
            warehouse_rows=warehouse_rows,
            stock_rows=stock_rows,
            category_rows=category_rows,
            currency_rows=currency_rows,
            pricelist_rows=pricelist_rows,
            pricelist_item_rows=pricelist_item_rows,
        )
        elapsed = round(time.time() - t0, 1)
        now_str = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        update_sync_status(
            "idle",
            f"Sincronizado en {elapsed}s ({len(pricelist_rows)} listas, {len(warehouse_rows)} alm.)",
            last_sync=now_str,
            total_products=len(product_rows),
        )
    except Exception as e:
        update_sync_status("error", f"Error: {str(e)}")