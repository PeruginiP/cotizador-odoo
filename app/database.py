import sqlite3
import os
import json
import time
from datetime import datetime, date, timezone, timedelta

try:
    from zoneinfo import ZoneInfo
    AR_TZ = ZoneInfo("America/Argentina/Buenos_Aires")
except Exception:
    AR_TZ = timezone(timedelta(hours=-3))

DB_PATH = os.getenv("DB_PATH", "/app/data/productos.db")

def get_conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    conn.execute("PRAGMA temp_store=MEMORY;")
    conn.execute("PRAGMA cache_size=-32000;")
    return conn

def init_db():
    conn = get_conn()
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY,
                default_code TEXT,
                name TEXT,
                list_price REAL DEFAULT 0.0,
                standard_price REAL DEFAULT 0.0,
                product_tmpl_id INTEGER DEFAULT 0,
                categ_id INTEGER DEFAULT 0,
                currency_id INTEGER DEFAULT 0,
                taxes_json TEXT DEFAULT '{}',
                brand TEXT DEFAULT '',
                barcode_ids TEXT,
                estado TEXT
            )
        """)
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(products)").fetchall()]
        if "brand" not in cols:
            conn.execute("ALTER TABLE products ADD COLUMN brand TEXT DEFAULT '';")
        if "taxes_json" not in cols:
            conn.execute("ALTER TABLE products ADD COLUMN taxes_json TEXT DEFAULT '{}';")
        if "standard_price" not in cols:
            conn.execute("ALTER TABLE products ADD COLUMN standard_price REAL DEFAULT 0.0;")
        if "product_tmpl_id" not in cols:
            conn.execute("ALTER TABLE products ADD COLUMN product_tmpl_id INTEGER DEFAULT 0;")
        if "categ_id" not in cols:
            conn.execute("ALTER TABLE products ADD COLUMN categ_id INTEGER DEFAULT 0;")
        if "currency_id" not in cols:
            conn.execute("ALTER TABLE products ADD COLUMN currency_id INTEGER DEFAULT 0;")

        if len(conn.execute("PRAGMA index_list(products)").fetchall()) == 0:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_prod_code ON products(default_code);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_prod_name ON products(name);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_prod_barcode ON products(barcode_ids);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_prod_brand ON products(brand);")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS product_categories (
                id INTEGER PRIMARY KEY,
                parent_path TEXT DEFAULT ''
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS warehouses (
                id INTEGER PRIMARY KEY,
                name TEXT,
                code TEXT,
                company_id INTEGER,
                company_name TEXT
            )
        """)
        if len(conn.execute("PRAGMA index_list(warehouses)").fetchall()) == 0:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_wh_company ON warehouses(company_id);")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS product_stock (
                product_id INTEGER,
                warehouse_id INTEGER,
                free_qty REAL DEFAULT 0.0,
                PRIMARY KEY (product_id, warehouse_id)
            )
        """)
        if len(conn.execute("PRAGMA index_list(product_stock)").fetchall()) <= 1:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ps_wh_prod ON product_stock(warehouse_id, product_id, free_qty);")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS company_currencies (
                company_id INTEGER,
                currency_id INTEGER,
                currency_name TEXT,
                currency_symbol TEXT,
                rate REAL DEFAULT 1.0,
                is_base INTEGER DEFAULT 0,
                PRIMARY KEY (company_id, currency_id)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS pricelists (
                id INTEGER PRIMARY KEY,
                name TEXT,
                currency_id INTEGER,
                company_id INTEGER DEFAULT 0,
                sequence INTEGER DEFAULT 10
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS pricelist_items (
                id INTEGER PRIMARY KEY,
                pricelist_id INTEGER,
                applied_on TEXT,
                categ_id INTEGER DEFAULT 0,
                product_tmpl_id INTEGER DEFAULT 0,
                product_id INTEGER DEFAULT 0,
                min_quantity REAL DEFAULT 0.0,
                date_start TEXT DEFAULT '',
                date_end TEXT DEFAULT '',
                compute_price TEXT DEFAULT 'fixed',
                fixed_price REAL DEFAULT 0.0,
                percent_price REAL DEFAULT 0.0,
                price_discount REAL DEFAULT 0.0,
                price_surcharge REAL DEFAULT 0.0,
                price_round REAL DEFAULT 0.0,
                price_min_margin REAL DEFAULT 0.0,
                price_max_margin REAL DEFAULT 0.0,
                base TEXT DEFAULT 'list_price',
                base_pricelist_id INTEGER DEFAULT 0,
                currency_id INTEGER DEFAULT 0
            )
        """)
        if len(conn.execute("PRAGMA index_list(pricelist_items)").fetchall()) == 0:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pli_pricelist ON pricelist_items(pricelist_id);")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS sync_status (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                status TEXT DEFAULT 'idle',
                message TEXT DEFAULT 'Sin sincronizar aún',
                last_sync TEXT,
                total_products INTEGER DEFAULT 0
            )
        """)
        conn.execute("""
            INSERT OR IGNORE INTO sync_status (id, status, message, total_products)
            VALUES (1, 'idle', 'Sin sincronizar aún', 0)
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS quotation_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT,
                username TEXT,
                company_id INTEGER,
                odoo_order_id INTEGER,
                odoo_order_name TEXT,
                customer_name TEXT,
                items_count INTEGER,
                amount_total REAL
            )
        """)
        log_cols = [r["name"] for r in conn.execute("PRAGMA table_info(quotation_logs)").fetchall()]
        if "company_id" not in log_cols:
            conn.execute("ALTER TABLE quotation_logs ADD COLUMN company_id INTEGER DEFAULT 0;")
    conn.close()

def _safe_rollback(conn):
    if conn.in_transaction:
        try:
            conn.execute("ROLLBACK;")
        except Exception:
            pass

def replace_all_catalog(
    product_rows,
    warehouse_rows,
    stock_rows,
    category_rows=None,
    currency_rows=None,
    pricelist_rows=None,
    pricelist_item_rows=None,
):
    product_rows.sort(key=lambda r: r[0])
    stock_rows.sort(key=lambda r: (r[0], r[1]))

    conn = get_conn()
    conn.isolation_level = None
    try:
        # FASE 1: Cargar datos en tablas *_staging sin bloquear las tablas en vivo
        conn.execute("BEGIN;")
        try:
            conn.execute("DROP TABLE IF EXISTS warehouses_staging;")
            conn.execute("""
                CREATE TABLE warehouses_staging (
                    id INTEGER PRIMARY KEY,
                    name TEXT,
                    code TEXT,
                    company_id INTEGER,
                    company_name TEXT
                )
            """)
            conn.executemany("""
                INSERT OR REPLACE INTO warehouses_staging (id, name, code, company_id, company_name)
                VALUES (?, ?, ?, ?, ?)
            """, warehouse_rows)

            conn.execute("DROP TABLE IF EXISTS products_staging;")
            conn.execute("""
                CREATE TABLE products_staging (
                    id INTEGER PRIMARY KEY,
                    default_code TEXT,
                    name TEXT,
                    list_price REAL DEFAULT 0.0,
                    standard_price REAL DEFAULT 0.0,
                    product_tmpl_id INTEGER DEFAULT 0,
                    categ_id INTEGER DEFAULT 0,
                    currency_id INTEGER DEFAULT 0,
                    taxes_json TEXT DEFAULT '{}',
                    brand TEXT DEFAULT '',
                    barcode_ids TEXT,
                    estado TEXT
                )
            """)
            conn.executemany("""
                INSERT OR REPLACE INTO products_staging (
                    id, default_code, name, list_price, standard_price,
                    product_tmpl_id, categ_id, currency_id,
                    taxes_json, brand, barcode_ids, estado
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, product_rows)

            conn.execute("DROP TABLE IF EXISTS product_stock_staging;")
            conn.execute("""
                CREATE TABLE product_stock_staging (
                    product_id INTEGER,
                    warehouse_id INTEGER,
                    free_qty REAL DEFAULT 0.0,
                    PRIMARY KEY (product_id, warehouse_id)
                )
            """)
            conn.executemany("""
                INSERT OR REPLACE INTO product_stock_staging (product_id, warehouse_id, free_qty)
                VALUES (?, ?, ?)
            """, stock_rows)

            if category_rows is not None:
                conn.execute("DROP TABLE IF EXISTS product_categories_staging;")
                conn.execute("CREATE TABLE product_categories_staging (id INTEGER PRIMARY KEY, parent_path TEXT DEFAULT '')")
                conn.executemany("INSERT OR REPLACE INTO product_categories_staging (id, parent_path) VALUES (?, ?)", category_rows)

            if currency_rows is not None:
                conn.execute("DROP TABLE IF EXISTS company_currencies_staging;")
                conn.execute("""
                    CREATE TABLE company_currencies_staging (
                        company_id INTEGER,
                        currency_id INTEGER,
                        currency_name TEXT,
                        currency_symbol TEXT,
                        rate REAL DEFAULT 1.0,
                        is_base INTEGER DEFAULT 0,
                        PRIMARY KEY (company_id, currency_id)
                    )
                """)
                conn.executemany("""
                    INSERT OR REPLACE INTO company_currencies_staging (company_id, currency_id, currency_name, currency_symbol, rate, is_base)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, currency_rows)

            if pricelist_rows is not None:
                conn.execute("DROP TABLE IF EXISTS pricelists_staging;")
                conn.execute("""
                    CREATE TABLE pricelists_staging (
                        id INTEGER PRIMARY KEY,
                        name TEXT,
                        currency_id INTEGER,
                        company_id INTEGER DEFAULT 0,
                        sequence INTEGER DEFAULT 10
                    )
                """)
                conn.executemany("""
                    INSERT OR REPLACE INTO pricelists_staging (id, name, currency_id, company_id, sequence)
                    VALUES (?, ?, ?, ?, ?)
                """, pricelist_rows)

            if pricelist_item_rows is not None:
                conn.execute("DROP TABLE IF EXISTS pricelist_items_staging;")
                conn.execute("""
                    CREATE TABLE pricelist_items_staging (
                        id INTEGER PRIMARY KEY,
                        pricelist_id INTEGER,
                        applied_on TEXT,
                        categ_id INTEGER DEFAULT 0,
                        product_tmpl_id INTEGER DEFAULT 0,
                        product_id INTEGER DEFAULT 0,
                        min_quantity REAL DEFAULT 0.0,
                        date_start TEXT DEFAULT '',
                        date_end TEXT DEFAULT '',
                        compute_price TEXT DEFAULT 'fixed',
                        fixed_price REAL DEFAULT 0.0,
                        percent_price REAL DEFAULT 0.0,
                        price_discount REAL DEFAULT 0.0,
                        price_surcharge REAL DEFAULT 0.0,
                        price_round REAL DEFAULT 0.0,
                        price_min_margin REAL DEFAULT 0.0,
                        price_max_margin REAL DEFAULT 0.0,
                        base TEXT DEFAULT 'list_price',
                        base_pricelist_id INTEGER DEFAULT 0,
                        currency_id INTEGER DEFAULT 0
                    )
                """)
                conn.executemany("""
                    INSERT OR REPLACE INTO pricelist_items_staging (
                        id, pricelist_id, applied_on, categ_id, product_tmpl_id, product_id,
                        min_quantity, date_start, date_end, compute_price, fixed_price,
                        percent_price, price_discount, price_surcharge, price_round,
                        price_min_margin, price_max_margin, base, base_pricelist_id, currency_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, pricelist_item_rows)

            conn.execute("COMMIT;")
        except Exception:
            _safe_rollback(conn)
            raise

        # FASE 2: Crear índices esenciales con nombre único sobre *_staging
        suffix = int(time.time())
        conn.execute("BEGIN;")
        try:
            conn.execute(f"CREATE INDEX idx_prod_code_{suffix} ON products_staging(default_code);")
            conn.execute(f"CREATE INDEX idx_prod_name_{suffix} ON products_staging(name);")
            conn.execute(f"CREATE INDEX idx_wh_company_{suffix} ON warehouses_staging(company_id);")
            conn.execute(f"CREATE INDEX idx_ps_wh_prod_{suffix} ON product_stock_staging(warehouse_id, product_id, free_qty);")
            if pricelist_item_rows is not None:
                conn.execute(f"CREATE INDEX idx_pli_pricelist_{suffix} ON pricelist_items_staging(pricelist_id);")
            conn.execute("COMMIT;")
        except Exception:
            _safe_rollback(conn)
            raise

        # FASE 3: Intercambio atómico instantáneo (< 5 ms)
        conn.execute("BEGIN IMMEDIATE;")
        try:
            conn.execute("DROP TABLE IF EXISTS warehouses;")
            conn.execute("ALTER TABLE warehouses_staging RENAME TO warehouses;")

            conn.execute("DROP TABLE IF EXISTS products;")
            conn.execute("ALTER TABLE products_staging RENAME TO products;")

            conn.execute("DROP TABLE IF EXISTS product_stock;")
            conn.execute("ALTER TABLE product_stock_staging RENAME TO product_stock;")

            if category_rows is not None:
                conn.execute("DROP TABLE IF EXISTS product_categories;")
                conn.execute("ALTER TABLE product_categories_staging RENAME TO product_categories;")

            if currency_rows is not None:
                conn.execute("DROP TABLE IF EXISTS company_currencies;")
                conn.execute("ALTER TABLE company_currencies_staging RENAME TO company_currencies;")

            if pricelist_rows is not None:
                conn.execute("DROP TABLE IF EXISTS pricelists;")
                conn.execute("ALTER TABLE pricelists_staging RENAME TO pricelists;")

            if pricelist_item_rows is not None:
                conn.execute("DROP TABLE IF EXISTS pricelist_items;")
                conn.execute("ALTER TABLE pricelist_items_staging RENAME TO pricelist_items;")

            conn.execute("COMMIT;")
        except Exception:
            _safe_rollback(conn)
            raise

        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        except Exception:
            pass
    finally:
        conn.close()
def get_all_warehouses():
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, name, code, company_id, company_name FROM warehouses ORDER BY company_id ASC, code ASC, id ASC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

def get_local_pricelists_for_company(company_id: int):
    conn = get_conn()
    try:
        curr_rows = conn.execute(
            "SELECT currency_id, currency_name, currency_symbol, rate, is_base FROM company_currencies WHERE company_id = ?",
            (company_id,),
        ).fetchall()
        if not curr_rows:
            return None

        curr_map = {int(c["currency_id"]): dict(c) for c in curr_rows}
        base_curr = next((dict(c) for c in curr_rows if int(c["is_base"]) == 1), None)
        if not base_curr:
            base_curr = dict(curr_rows[0])
        base_rate = float(base_curr["rate"] or 1.0)

        pl_rows = conn.execute(
            """
            SELECT id, name, currency_id, company_id, sequence
            FROM pricelists
            WHERE company_id = 0 OR company_id = ?
            ORDER BY sequence ASC, id ASC
            """,
            (company_id,),
        ).fetchall()

        pricelists = []
        for pl in pl_rows:
            cid = int(pl["currency_id"] or base_curr["currency_id"])
            c_obj = curr_map.get(cid, base_curr)
            pl_rate = float(c_obj["rate"] or 1.0)
            inverse_rate = round(base_rate / pl_rate, 4) if pl_rate else 1.0
            pricelists.append({
                "id": int(pl["id"]),
                "name": pl["name"],
                "currency_id": cid,
                "currency_name": c_obj["currency_name"],
                "currency_symbol": c_obj["currency_symbol"],
                "exchange_rate": inverse_rate,
                "is_foreign": cid != int(base_curr["currency_id"]),
            })

        return {
            "pricelists": pricelists,
            "base_currency": {
                "id": int(base_curr["currency_id"]),
                "name": base_curr["currency_name"],
                "symbol": base_curr["currency_symbol"],
                "rate": base_rate,
            },
        }
    finally:
        conn.close()

def compute_prices_from_local_pricelist(company_id: int, pricelist_id: int, items: list):
    if not items or not pricelist_id:
        return {}

    conn = get_conn()
    try:
        curr_rows = conn.execute(
            "SELECT currency_id, rate, is_base FROM company_currencies WHERE company_id = ?",
            (company_id,),
        ).fetchall()
        if not curr_rows:
            return None

        rate_map = {int(c["currency_id"]): float(c["rate"] or 1.0) for c in curr_rows}
        base_curr_row = next((c for c in curr_rows if int(c["is_base"]) == 1), curr_rows[0])
        base_curr_id = int(base_curr_row["currency_id"])

        def convert_amount(amount: float, from_cid: int, to_cid: int) -> float:
            if not from_cid or not to_cid or from_cid == to_cid:
                return amount
            r_from = rate_map.get(from_cid, 1.0)
            r_to = rate_map.get(to_cid, 1.0)
            if not r_from:
                return amount
            return amount * (r_to / r_from)

        pl_rows = conn.execute("SELECT id, currency_id FROM pricelists").fetchall()
        pl_curr_map = {int(p["id"]): int(p["currency_id"] or base_curr_id) for p in pl_rows}
        if pricelist_id not in pl_curr_map:
            return None

        categ_rows = conn.execute("SELECT id, parent_path FROM product_categories").fetchall()
        categ_path_map = {int(c["id"]): (c["parent_path"] or "") for c in categ_rows}

        prod_ids = list({int(it["product_id"]) for it in items if it.get("product_id")})
        if not prod_ids:
            return {}
        placeholders = ",".join("?" for _ in prod_ids)
        prod_rows = conn.execute(
            f"SELECT id, list_price, standard_price, product_tmpl_id, categ_id, currency_id FROM products WHERE id IN ({placeholders})",
            prod_ids,
        ).fetchall()
        prod_map = {int(p["id"]): dict(p) for p in prod_rows}

        all_items_rows = conn.execute("SELECT * FROM pricelist_items").fetchall()
        items_by_pl = {}
        for ir in all_items_rows:
            items_by_pl.setdefault(int(ir["pricelist_id"]), []).append(dict(ir))

        applied_order = {"0_product_variant": 0, "1_product": 1, "2_product_category": 2, "3_global": 3}
        for pl_id_k, rules in items_by_pl.items():
            rules.sort(key=lambda r: (
                applied_order.get(r["applied_on"], 9),
                -float(r["min_quantity"] or 0.0),
                -int(r["id"]),
            ))

        today_str = date.today().strftime("%Y-%m-%d")

        def eval_product_in_pricelist(prod: dict, qty: float, target_pl_id: int, visited=None) -> float:
            if visited is None:
                visited = set()
            if target_pl_id in visited:
                src_c = int(prod["currency_id"] or base_curr_id)
                return convert_amount(float(prod["list_price"] or 0.0), src_c, pl_curr_map.get(target_pl_id, base_curr_id))
            visited.add(target_pl_id)

            pl_currency_id = pl_curr_map.get(target_pl_id, base_curr_id)
            rules = items_by_pl.get(target_pl_id, [])
            prod_categ_path = categ_path_map.get(int(prod["categ_id"] or 0), "")

            matched_rule = None
            for r in rules:
                if qty < float(r["min_quantity"] or 0.0):
                    continue
                ds = (r["date_start"] or "")[:10]
                de = (r["date_end"] or "")[:10]
                if ds and today_str < ds:
                    continue
                if de and today_str > de:
                    continue

                ap = r["applied_on"]
                if ap == "0_product_variant" and int(r["product_id"] or 0) == prod["id"]:
                    matched_rule = r
                    break
                elif ap == "1_product" and int(r["product_tmpl_id"] or 0) == int(prod["product_tmpl_id"] or 0):
                    matched_rule = r
                    break
                elif ap == "2_product_category":
                    rcat = int(r["categ_id"] or 0)
                    if rcat and (rcat == int(prod["categ_id"] or 0) or f"/{rcat}/" in f"/{prod_categ_path}"):
                        matched_rule = r
                        break
                elif ap == "3_global":
                    matched_rule = r
                    break

            prod_src_curr = int(prod["currency_id"] or base_curr_id)

            if not matched_rule:
                return convert_amount(float(prod["list_price"] or 0.0), prod_src_curr, pl_currency_id)

            rule_curr_id = int(matched_rule["currency_id"] or pl_currency_id)
            base_type = matched_rule["base"] or "list_price"

            if base_type == "standard_price":
                price = convert_amount(float(prod["standard_price"] or 0.0), prod_src_curr, rule_curr_id)
            elif base_type == "pricelist" and int(matched_rule["base_pricelist_id"] or 0):
                b_pl_id = int(matched_rule["base_pricelist_id"])
                b_price = eval_product_in_pricelist(prod, qty, b_pl_id, visited.copy())
                price = convert_amount(b_price, pl_curr_map.get(b_pl_id, base_curr_id), rule_curr_id)
            else:
                price = convert_amount(float(prod["list_price"] or 0.0), prod_src_curr, rule_curr_id)

            c_type = matched_rule["compute_price"] or "fixed"
            if c_type == "fixed":
                price = float(matched_rule["fixed_price"] or 0.0)
            elif c_type == "percentage":
                pct = float(matched_rule["percent_price"] or 0.0)
                price = price - (price * (pct / 100.0))
            elif c_type == "formula":
                price_limit = price
                disc = float(matched_rule["price_discount"] or 0.0)
                surch = float(matched_rule["price_surcharge"] or 0.0)
                p_round = float(matched_rule["price_round"] or 0.0)
                min_m = float(matched_rule["price_min_margin"] or 0.0)
                max_m = float(matched_rule["price_max_margin"] or 0.0)

                price = price - (price * (disc / 100.0))
                if p_round:
                    price = round(price / p_round) * p_round
                price += surch
                if min_m:
                    price = max(price, price_limit + min_m)
                if max_m:
                    price = min(price, price_limit + max_m)

            return convert_amount(price, rule_curr_id, pl_currency_id)

        result = {}
        for it in items:
            pid = int(it["product_id"])
            qty = float(it.get("qty") or 1.0)
            prod = prod_map.get(pid)
            if prod:
                val = eval_product_in_pricelist(prod, qty, pricelist_id)
                result[str(pid)] = round(val, 4)

        return result
    finally:
        conn.close()

def get_products_by_ids(product_ids: list, company_id: int):
    if not product_ids:
        return {}
    unique_ids = list({int(pid) for pid in product_ids if pid})
    if not unique_ids:
        return {}

    conn = get_conn()
    try:
        placeholders = ",".join("?" for _ in unique_ids)
        rows = conn.execute(
            f"SELECT id, default_code, name, list_price, brand, taxes_json FROM products WHERE id IN ({placeholders})",
            unique_ids,
        ).fetchall()

        st_rows = conn.execute(
            f"SELECT product_id, warehouse_id, free_qty FROM product_stock WHERE product_id IN ({placeholders})",
            unique_ids,
        ).fetchall()
        stock_map = {}
        for sr in st_rows:
            stock_map.setdefault(int(sr["product_id"]), {})[str(sr["warehouse_id"])] = float(sr["free_qty"])

        comp_key = str(company_id)
        result = {}
        for r in rows:
            pid = int(r["id"])
            taxes_map = json.loads(r["taxes_json"] or "{}")
            taxes_list = taxes_map.get(comp_key, [])
            
            result[pid] = {
                "id": pid,
                "default_code": r["default_code"] or "",
                "name": r["name"] or "",
                "list_price": float(r["list_price"] or 0.0),
                "taxes": taxes_list,
                "brand": r["brand"] or "",
                "stocks": stock_map.get(pid, {}),
            }
        return result
    finally:
        conn.close()

def update_sync_status(status, message, last_sync=None, total_products=None):
    conn = get_conn()
    try:
        with conn:
            if last_sync is not None and total_products is not None:
                conn.execute("""
                    UPDATE sync_status
                    SET status = ?, message = ?, last_sync = ?, total_products = ?
                    WHERE id = 1
                """, (status, message, last_sync, total_products))
            else:
                conn.execute("""
                    UPDATE sync_status SET status = ?, message = ? WHERE id = 1
                """, (status, message))
    finally:
        conn.close()

def get_sync_status():
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM sync_status WHERE id = 1").fetchone()
        total_db = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        data = dict(row) if row else {}
        data["total_products"] = total_db
        return data
    finally:
        conn.close()

def query_products_datatables(
    start: int,
    length: int,
    search_value: str,
    order_col_idx: int,
    order_dir: str,
    company_id: int,
    only_in_stock: bool = False,
):
    conn = get_conn()
    try:
        wh_rows = conn.execute(
            "SELECT id, code, name FROM warehouses WHERE company_id = ? ORDER BY code ASC, id ASC",
            (company_id,),
        ).fetchall()
        company_wh_ids = [int(w["id"]) for w in wh_rows]

        fixed_cols = {
            1: "default_code",
            2: "name",
            3: "brand",
            4: "list_price",
            6: "barcode_ids",
            7: "estado",
        }
        direction = "DESC" if order_dir.lower() == "desc" else "ASC"

        if order_col_idx in fixed_cols:
            order_expr = f"{fixed_cols[order_col_idx]} {direction}"
        elif 8 <= order_col_idx < 8 + len(company_wh_ids):
            target_wh_id = company_wh_ids[order_col_idx - 8]
            order_expr = (
                f"(SELECT COALESCE(free_qty, 0) FROM product_stock ps "
                f"WHERE ps.product_id = products.id AND ps.warehouse_id = {target_wh_id}) {direction}"
            )
        elif order_col_idx == 8 + len(company_wh_ids) and company_wh_ids:
            wh_list_sql = ",".join(str(wid) for wid in company_wh_ids)
            order_expr = (
                f"(SELECT COALESCE(SUM(free_qty), 0) FROM product_stock ps "
                f"WHERE ps.product_id = products.id AND ps.warehouse_id IN ({wh_list_sql})) {direction}"
            )
        else:
            order_expr = f"default_code {direction}"

        total_records = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]

        where_clauses = []
        params = []

        if only_in_stock:
            if company_wh_ids:
                wh_list_sql = ",".join(str(wid) for wid in company_wh_ids)
                where_clauses.append(
                    f"EXISTS (SELECT 1 FROM product_stock ps WHERE ps.product_id = products.id "
                    f"AND ps.warehouse_id IN ({wh_list_sql}) AND ps.free_qty > 0)"
                )
            else:
                where_clauses.append("1 = 0")

        if search_value and search_value.strip():
            terms = search_value.strip().split()
            for term in terms:
                like_term = f"%{term}%"
                where_clauses.append(
                    "(default_code LIKE ? OR name LIKE ? OR barcode_ids LIKE ? OR brand LIKE ?)"
                )
                params.extend([like_term, like_term, like_term, like_term])

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

        filtered_records = conn.execute(
            f"SELECT COUNT(*) FROM products {where_sql}", params
        ).fetchone()[0]

        query_sql = f"""
            SELECT id, default_code, name, list_price, taxes_json, brand, barcode_ids, estado
            FROM products
            {where_sql}
            ORDER BY {order_expr}
            LIMIT ? OFFSET ?
        """
        rows = conn.execute(query_sql, params + [length, start]).fetchall()

        prod_ids = [int(r["id"]) for r in rows]
        stock_by_prod = {}
        if prod_ids:
            placeholders = ",".join("?" for _ in prod_ids)
            st_rows = conn.execute(
                f"SELECT product_id, warehouse_id, free_qty FROM product_stock WHERE product_id IN ({placeholders})",
                prod_ids,
            ).fetchall()
            for sr in st_rows:
                stock_by_prod.setdefault(int(sr["product_id"]), {})[str(sr["warehouse_id"])] = float(sr["free_qty"])

        comp_key = str(company_id)
        data = []
        for r in rows:
            pid = int(r["id"])
            taxes_map = json.loads(r["taxes_json"] or "{}")
            taxes_list = taxes_map.get(comp_key, [])
            
            display_arr = []
            for t in taxes_list:
                amt = t["amount"]
                if t.get("type") == "percent":
                    display_arr.append(f"{amt:g}%")
                else:
                    display_arr.append(f"${amt:g}")

            p_stocks = stock_by_prod.get(pid, {})
            total_company_stock = round(sum(p_stocks.get(str(wid), 0.0) for wid in company_wh_ids), 4)

            data.append({
                "id": pid,
                "default_code": r["default_code"] or "",
                "name": r["name"] or "",
                "list_price": float(r["list_price"] or 0.0),
                "taxes": taxes_list,
                "taxes_display": ", ".join(display_arr),
                "brand": r["brand"] or "",
                "barcode_ids": r["barcode_ids"] or "",
                "estado": r["estado"] or "",
                "stocks": p_stocks,
                "total_stock": total_company_stock,
            })

        return {
            "recordsTotal": total_records,
            "recordsFiltered": filtered_records,
            "data": data,
        }
    finally:
        conn.close()

def log_quotation(username, company_id, order_id, order_name, customer_name, items_count, amount_total):
    conn = get_conn()
    try:
        with conn:
            conn.execute("""
                INSERT INTO quotation_logs (
                    created_at, username, company_id, odoo_order_id, odoo_order_name,
                    customer_name, items_count, amount_total
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                datetime.now(AR_TZ).strftime("%Y-%m-%d %H:%M:%S"),
                username, company_id, order_id, order_name, customer_name,
                items_count, amount_total
            ))
    finally:
        conn.close()