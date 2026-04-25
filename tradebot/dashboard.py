from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .config import Settings, get_settings
from .db import Database
from .email_report import email_configured, send_daily_report
from .engine import TradingEngine
from .providers import build_broker

log = logging.getLogger(__name__)

_ET_OFFSET_EST = timezone(timedelta(hours=-5))
_ET_OFFSET_EDT = timezone(timedelta(hours=-4))


def _et_now() -> datetime:
    utc_now = datetime.now(timezone.utc)
    year = utc_now.year
    mar1 = datetime(year, 3, 1, tzinfo=timezone.utc)
    dst_start = mar1 + timedelta(days=(6 - mar1.weekday()) % 7 + 7)
    nov1 = datetime(year, 11, 1, tzinfo=timezone.utc)
    dst_end = nov1 + timedelta(days=(6 - nov1.weekday()) % 7)
    if dst_start <= utc_now.replace(tzinfo=timezone.utc) < dst_end:
        return utc_now.astimezone(_ET_OFFSET_EDT)
    return utc_now.astimezone(_ET_OFFSET_EST)


class TradingScheduler:
    def __init__(self, interval_seconds: int, callback: Callable[[], None]) -> None:
        self.interval_seconds = max(1, interval_seconds)
        self.callback = callback
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="tradebot-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._thread = None

    def run_cycle(self) -> None:
        self.callback()

    def _run_loop(self) -> None:
        while not self._stop_event.wait(self.interval_seconds):
            self.callback()


class MarketCloseReporter:
    def __init__(self, engine: TradingEngine, engine_lock: threading.Lock) -> None:
        self.engine = engine
        self.engine_lock = engine_lock
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_report_date: str | None = None

    def start(self) -> None:
        if not email_configured():
            log.info("Email not configured; market-close reporter disabled")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="market-close-reporter", daemon=True)
        self._thread.start()
        log.info("Market-close email reporter started")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)
        self._thread = None

    def _run(self) -> None:
        while not self._stop_event.wait(300):
            self._maybe_send()

    def _maybe_send(self) -> None:
        try:
            now_et = _et_now()
            today_str = now_et.strftime("%Y-%m-%d")
            if now_et.weekday() >= 5:
                return
            if now_et.hour < 16 or (now_et.hour == 16 and now_et.minute < 5):
                return
            if self._last_report_date == today_str:
                return

            with self.engine_lock:
                snapshot = self.engine.dashboard_snapshot()
            if send_daily_report(snapshot):
                self._last_report_date = today_str
        except Exception as exc:  # noqa: BLE001
            log.error("Market-close reporter error: %s", exc)

    def send_now(self) -> bool:
        with self.engine_lock:
            snapshot = self.engine.dashboard_snapshot()
        return send_daily_report(snapshot)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    db = Database(settings.db_path)
    broker = build_broker(settings)
    engine = TradingEngine(settings=settings, broker=broker, db=db)
    engine_lock = threading.Lock()

    def run_trade_cycle() -> None:
        with engine_lock:
            engine.trade_once_with_signal_refresh()

    reporter = MarketCloseReporter(engine, engine_lock)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        scheduler = None
        if settings.auto_trade_enabled:
            scheduler = TradingScheduler(settings.auto_trade_interval_minutes * 60, run_trade_cycle)
            scheduler.start()
        reporter.start()
        try:
            yield
        finally:
            reporter.stop()
            if scheduler:
                scheduler.stop()

    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.state.engine = engine
    app.state.engine_lock = engine_lock
    app.state.reporter = reporter
    app.state.auto_trade_enabled = settings.auto_trade_enabled
    app.state.auto_trade_interval_minutes = settings.auto_trade_interval_minutes
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        snapshot = engine.dashboard_snapshot()
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                **snapshot,
                "auto_trade_enabled": app.state.auto_trade_enabled,
                "auto_trade_interval_minutes": app.state.auto_trade_interval_minutes,
                "congress_max_price": settings.congress_max_price,
            },
        )

    @app.post("/scan")
    async def scan():
        with engine_lock:
            engine.scan_market()
        return RedirectResponse(url="/", status_code=303)

    @app.post("/refresh-congress")
    async def refresh_congress():
        with engine_lock:
            engine.refresh_congress_trades()
        return RedirectResponse(url="/", status_code=303)

    @app.post("/trade-once")
    async def trade_once():
        with engine_lock:
            engine.trade_once_with_signal_refresh()
        return RedirectResponse(url="/", status_code=303)

    @app.post("/refresh-signals")
    async def refresh_signals():
        with engine_lock:
            engine.refresh_all_signals()
        return RedirectResponse(url="/", status_code=303)

    @app.post("/send-report")
    async def send_report():
        import asyncio

        if not email_configured():
            return JSONResponse({"ok": False, "error": "Email not configured."}, status_code=400)
        try:
            success = await asyncio.get_event_loop().run_in_executor(None, reporter.send_now)
            if success:
                return JSONResponse({"ok": True, "message": "Report emailed."})
            return JSONResponse({"ok": False, "error": "Failed to send report."}, status_code=500)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    @app.get("/api/status")
    async def status():
        with engine_lock:
            return {
                **engine.dashboard_snapshot(),
                "auto_trade_enabled": app.state.auto_trade_enabled,
                "auto_trade_interval_minutes": app.state.auto_trade_interval_minutes,
                "congress_max_price": settings.congress_max_price,
                "email_reports_enabled": email_configured(),
            }

    @app.get("/health")
    async def health():
        return {"ok": True}

    return app


app = create_app()
