from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select

from app.config import Settings
from app.db.models import Machine, utcnow
from app.db.session import SessionLocal
from app.services.machine_controller import MachineController
from app.services.machine_service import trigger_machine


class MachineScheduler:
    def __init__(self, settings: Settings, controller: MachineController) -> None:
        self.settings = settings
        self.controller = controller
        self.scheduler = BackgroundScheduler(timezone=settings.scheduler_timezone)

    @property
    def running(self) -> bool:
        return self.scheduler.running

    def start(self) -> None:
        if self.scheduler.running:
            return
        self.scheduler.add_job(self._tick, "interval", seconds=5, id="machine-scheduler-tick", replace_existing=True)
        self.scheduler.start()

    def stop(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def _tick(self) -> None:
        now = utcnow()
        with SessionLocal() as db:
            machines = db.execute(
                select(Machine).where(Machine.enabled.is_(True), Machine.next_run_at.is_not(None), Machine.next_run_at <= now)
            ).scalars()
            for machine in machines:
                trigger_machine(db, machine.id, self.controller, trigger_source="scheduler")
