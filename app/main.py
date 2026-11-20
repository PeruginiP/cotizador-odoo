import os
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="Cotizador Odoo")

@app.get("/", response_class=HTMLResponse)
def health_check():
    odoo_url = os.getenv("ODOO_URL", "NO CONFIGURADA")
    data_writable = os.access("/app/data", os.W_OK)
    return f"""
    <html>
        <body style="font-family: sans-serif; padding: 40px; background: #f8f9fa;">
            <div style="max-width: 600px; margin: auto; background: white; padding: 30px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1);">
                <h2 style="color: #198754;">Servidor activo en Coolify</h2>
                <p><strong>Odoo URL:</strong> {odoo_url}</p>
                <p><strong>Almacenamiento persistente (/app/data):</strong> {'OK (escribible)' if data_writable else 'ERROR'}</p>
                <p style="color: #6c757d; font-size: 0.9em;">Listo para cargar el motor de sincronización y DataTables.</p>
            </div>
        </body>
    </html>
    """