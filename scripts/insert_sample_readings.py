from __future__ import annotations

import random
from datetime import timedelta

from app.db.init_db import init_db
from app.db.models import SensorReading, utcnow
from app.db.session import SessionLocal


if __name__ == "__main__":
    init_db()
    now = utcnow()
    with SessionLocal() as db:
        for sensor in ["arduino-1", "arduino-2"]:
            for idx in range(120):
                db.add(
                    SensorReading(
                        sensor_name=sensor,
                        temperature=round(70 + random.uniform(-3, 3), 2),
                        relative_humidity=round(45 + random.uniform(-8, 8), 2),
                        recorded_at=now - timedelta(minutes=120 - idx),
                        raw_payload="sample",
                    )
                )
        db.commit()
    print("Inserted sample sensor readings.")
