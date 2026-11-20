import xmlrpc.client
import time
import os
import json
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from app.database import replace_all_products, update_sync_status

ALMACENES_IDS = [5, 6, 7]
COMPANIA_IMPUESTOS_ID = 1
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

        def leer_en_paralelo(model, ids, fields, batch_size=BATCH_SIZE):
            if not ids:
                return []
            batches = [ids[i:i + batch_size] for i in range(0, len(ids), batch_size)]
            resultados = []
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = [
                    executor.submit(execute_kw, model, "read", [b], {"fields": fields})
                    for b in batches
                ]
                for future in as_completed(futures):
                    resultados.extend(future.result())
            return resultados

        # 1. Metadatos (incluyendo detección automática del campo de Marca)
        update_sync_status("running", "Leyendo metadatos y ubicaciones de almacenes...")
        studio_field = "x_studio_selection_field_6ua_1j8tii7sp"
        all_prod_fields = execute_kw("product.product", "fields_get", [[]], {"attributes": ["type", "relation", "selection"]})

        estado_selection_map = dict(all_prod_fields.get(studio_field, {}).get("selection", []))

        # Detectar campo de marca (product_brand_id, brand_id, o campo con 'brand'/'marca')
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
            [[["id", "in", ALMACENES_IDS]]],
            {"fields": ["id", "lot_stock_id"]},
        )

        # 2. Stock en lot_stock_id por almacén
        update_sync_status("running", "Calculando stock disponible sin reservar (Alm. 5, 6 y 7)...")
        stock_por_almacen = {wh_id: {} for wh_id in ALMACENES_IDS}
        for wh in almacenes_info:
            wh_id = wh["id"]
            lot_stock_id = wh["lot_stock_id"][0]
            quants_grouped = execute_kw(
                "stock.quant",
                "read_group",
                [
                    [["location_id", "child_of", lot_stock_id], ["location_id.usage", "=", "internal"]],
                    ["product_id", "quantity:sum", "reserved_quantity:sum"],
                    ["product_id"],
                ],
                {"lazy": False},
            )
            for g in quants_grouped:
                if g.get("product_id"):
                    prod_id = g["product_id"][0]
                    free_qty = round(g.get("quantity", 0.0) - g.get("reserved_quantity", 0.0), 4)
                    stock_por_almacen[wh_id][prod_id] = free_qty

        # 3. Productos en paralelo
        product_ids = execute_kw("product.product", "search", [[]])
        update_sync_status("running", f"Descargando {len(product_ids):,} productos en paralelo...")

        campos_lectura = [
            "default_code",
            "name",
            "list_price",
            "taxes_id",
            "barcode_ids",
            studio_field,
        ]
        if brand_field:
            campos_lectura.append(brand_field)

        productos_raw = leer_en_paralelo("product.product", product_ids, campos_lectura)

        # 4. Relaciones (impuestos Cía 1 y códigos de barra)
        update_sync_status("running", "Resolviendo impuestos y códigos de barra...")
        all_tax_ids = list({tid for p in productos_raw for tid in p.get("taxes_id", [])})
        taxes_records = leer_en_paralelo("account.tax", all_tax_ids, ["name", "company_id"], batch_size=5000)
        taxes_map = {
            t["id"]: t["name"]
            for t in taxes_records
            if t.get("company_id") and t["company_id"][0] == COMPANIA_IMPUESTOS_ID
        }

        all_barcode_ids = list({bid for p in productos_raw for bid in p.get("barcode_ids", [])})
        barcodes_map = {}
        if all_barcode_ids and barcode_rel_model:
            bc_records = leer_en_paralelo(barcode_rel_model, all_barcode_ids, [barcode_code_field], batch_size=5000)
            barcodes_map = {b["id"]: str(b[barcode_code_field]) for b in bc_records if b.get(barcode_code_field)}

        # 5. Guardar en SQLite
        update_sync_status("running", "Guardando índice en base de datos local...")
        rows_to_insert = []
        for p in productos_raw:
            pid = p["id"]
            marca = ""
            if brand_field:
                b_val = p.get(brand_field)
                if isinstance(b_val, list) and len(b_val) > 1:
                    marca = str(b_val[1])
                elif isinstance(b_val, str):
                    marca = b_val

            valid_tax_ids = [tid for tid in p.get("taxes_id", []) if tid in taxes_map]
            impuestos = ", ".join(taxes_map[tid] for tid in valid_tax_ids)
            codigos_barra = ", ".join(barcodes_map[bid] for bid in p.get("barcode_ids", []) if bid in barcodes_map)

            estado_raw = p.get(studio_field)
            estado = estado_selection_map.get(estado_raw, estado_raw) if estado_raw else ""

            rows_to_insert.append((
                pid,
                p.get("default_code") or "",
                p.get("name") or "",
                float(p.get("list_price") or 0.0),
                impuestos,
                json.dumps(valid_tax_ids),
                marca,
                codigos_barra,
                estado,
                stock_por_almacen[5].get(pid, 0.0),
                stock_por_almacen[6].get(pid, 0.0),
                stock_por_almacen[7].get(pid, 0.0),
            ))

        replace_all_products(rows_to_insert)
        elapsed = round(time.time() - t0, 1)
        now_str = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        update_sync_status(
            "idle",
            f"Sincronizado en {elapsed}s",
            last_sync=now_str,
            total_products=len(rows_to_insert),
        )
    except Exception as e:
        update_sync_status("error", f"Error: {str(e)}")