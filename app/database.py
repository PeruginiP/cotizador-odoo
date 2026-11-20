import sqlite3
import os
import json
from datetime import datetime

DB_PATH = os.getenv("DB_PATH", "/app/data/productos.db")

def get_conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
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

        conn.execute("CREATE INDEX IF NOT EXISTS idx_prod_code ON products(default_code);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_prod_name ON products(name);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_prod_barcode ON products(barcode_ids);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_prod_brand ON products(brand);")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS warehouses (
                id INTEGER PRIMARY KEY,
                name TEXT,
                code TEXT,
                company_id INTEGER,
                company_name TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wh_company ON warehouses(company_id);")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS product_stock (
                product_id INTEGER,
                warehouse_id INTEGER,
                free_qty REAL DEFAULT 0.0,
                PRIMARY KEY (product_id, warehouse_id)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ps_wh_prod ON product_stock(warehouse_id, product_id, free_qty);")

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

def replace_all_catalog(product_rows, warehouse_rows, stock_rows):
    conn = get_conn()
    try:
        with conn:
            conn.execute("DELETE FROM warehouses")
            conn.executemany("""
                INSERT INTO warehouses (id, name, code, company_id, company_name)
                VALUES (?, ?, ?, ?, ?)
            """, warehouse_rows)

            conn.execute("DELETE FROM products")
            conn.executemany("""
                INSERT INTO products (
                    id, default_code, name, list_price, taxes_json,
                    brand, barcode_ids, estado
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, product_rows)

            conn.execute("DELETE FROM product_stock")
            conn.executemany("""
                INSERT INTO product_stock (product_id, warehouse_id, free_qty)
                VALUES (?, ?, ?)
            """, stock_rows)
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

        # Mapeo de columnas de DataTables:
        # 0: checkbox, 1: default_code, 2: name, 3: brand, 4: list_price,
        # 5: taxes_id, 6: barcode_ids, 7: estado,
        # 8 .. 8+N-1: almacenes de la compañía, 8+N: total stock de la compañía
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
            p_stocks = stock_by_prod.get(pid, {})
            total_company_stock = round(sum(p_stocks.get(str(wid), 0.0) for wid in company_wh_ids), 4)

            data.append({
                "id": pid,
                "default_code": r["default_code"] or "",
                "name": r["name"] or "",
                "list_price": float(r["list_price"] or 0.0),
                "taxes_id": taxes_map.get(comp_key, ""),
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
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                username, company_id, order_id, order_name, customer_name,
                items_count, amount_total
            ))
    finally:
        conn.close()