from app.db.init_db import ensure_default_machine, init_db
from app.db.session import SessionLocal


if __name__ == "__main__":
    init_db()
    with SessionLocal() as db:
        machine = ensure_default_machine(db)
    print(f"Initialized database and default machine: {machine.id}")
