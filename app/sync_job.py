from dotenv import load_dotenv
from app.database import init_db, get_sync_status, update_sync_status
from app.odoo_sync import run_full_sync

load_dotenv()

def main():
    init_db()
    current = get_sync_status()
    if current.get("status") == "running":
        print("Aviso: había una sincronización previa interrumpida; reiniciando estado...", flush=True)
        update_sync_status("idle", "Reiniciado por tarea programada")

    print("Iniciando sincronización programada con Odoo...", flush=True)
    run_full_sync()
    final_status = get_sync_status()
    print(f"Finalizado: {final_status.get('status')} - {final_status.get('message')}", flush=True)

    if final_status.get("status") == "error":
        raise SystemExit(1)

if __name__ == "__main__":
    main()