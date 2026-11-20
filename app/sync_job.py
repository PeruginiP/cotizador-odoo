from dotenv import load_dotenv
from app.database import init_db, get_sync_status
from app.odoo_sync import run_full_sync

load_dotenv()

def main():
    init_db()
    current = get_sync_status()
    if current.get("status") == "running":
        print("Sincronización omitida: ya hay un proceso en ejecución.")
        return

    print("Iniciando sincronización programada con Odoo...")
    run_full_sync()
    final_status = get_sync_status()
    print(f"Finalizado: {final_status.get('status')} - {final_status.get('message')}")

if __name__ == "__main__":
    main()