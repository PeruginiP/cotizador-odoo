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
                taxes_id TEXT,
                tax_ids_json TEXT,
                partner_id TEXT,
                barcode_ids TEXT,
                estado TEXT,
                stock_5 REAL DEFAULT 0.0,
                stock_6 REAL DEFAULT 0.0,
                stock_7 REAL DEFAULT 0.0
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_prod_code ON products(default_code);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_prod_name ON products(name);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_prod_barcode ON products(barcode_ids);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_prod_partner ON products(partner_id);")

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
                odoo_order_id INTEGER,
                odoo_order_name TEXT,
                customer_name TEXT,
                warehouse_id INTEGER,
                items_count INTEGER,
                amount_total REAL
            )
        """)
    conn.close()

def replace_all_products(rows):
    conn = get_conn()
    try:
        with conn:
            conn.execute("DELETE FROM products")
            conn.executemany("""
                INSERT INTO products (
                    id, default_code, name, list_price, taxes_id, tax_ids_json,
                    partner_id, barcode_ids, estado, stock_5, stock_6, stock_7
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, rows)
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

def query_products_datatables(start: int, length: int, search_value: str, order_col_idx: int, order_dir: str, only_in_stock: bool = False):
    columns_map = {
        0: "default_code",
        1: "name",
        2: "list_price",
        3: "taxes_id",
        4: "partner_id",
        5: "barcode_ids",
        6: "estado",
        7: "stock_5",
        8: "stock_6",
        9: "stock_7",
    }
    order_col = columns_map.get(order_col_idx, "default_code")
    direction = "DESC" if order_dir.lower() == "desc" else "ASC"

    conn = get_conn()
    try:
        total_records = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]

        where_clauses = []
        params = []

        if only_in_stock:
            where_clauses.append("(stock_5 > 0 OR stock_6 > 0 OR stock_7 > 0)")

        if search_value and search_value.strip():
            # Búsqueda multi-palabra: cada término debe aparecer en código, nombre, barras o proveedor
            terms = search_value.strip().split()
            for term in terms:
                like_term = f"%{term}%"
                where_clauses.append(
                    "(default_code LIKE ? OR name LIKE ? OR barcode_ids LIKE ? OR partner_id LIKE ?)"
                )
                params.extend([like_term, like_term, like_term, like_term])

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

        filtered_records = conn.execute(
            f"SELECT COUNT(*) FROM products {where_sql}", params
        ).fetchone()[0]

        query_sql = f"""
            SELECT id, default_code, name, list_price, taxes_id, tax_ids_json,
                   partner_id, barcode_ids, estado, stock_5, stock_6, stock_7
            FROM products
            {where_sql}
            ORDER BY {order_col} {direction}
            LIMIT ? OFFSET ?
        """
        rows = conn.execute(query_sql, params + [length, start]).fetchall()

        data = []
        for r in rows:
            item = dict(r)
            item["tax_ids"] = json.loads(item["tax_ids_json"] or "[]")
            data.append(item)

        return {
            "recordsTotal": total_records,
            "recordsFiltered": filtered_records,
            "data": data,
        }
    finally:
        conn.close()

def log_quotation(username, order_id, order_name, customer_name, warehouse_id, items_count, amount_total):
    conn = get_conn()
    try:
        with conn:
            conn.execute("""
                INSERT INTO quotation_logs (
                    created_at, username, odoo_order_id, odoo_order_name,
                    customer_name, warehouse_id, items_count, amount_total
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                username, order_id, order_name, customer_name,
                warehouse_id, items_count, amount_total
            ))
    finally:
        conn.close()