"""APScheduler automation runner for scheduled ETL syncs, alert monitors, and report generation."""

import logging
import signal
import sys
import time
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.blocking import BlockingScheduler

from sellerpulse.automation.alerts import AlertManager
from sellerpulse.automation.reports import generate_reports
from sellerpulse.config import get_settings
from sellerpulse.db.session import get_db_context
from sellerpulse.etl.load import run_etl_pipeline
from sellerpulse.ingestion.synthetic import SyntheticConnector

logger = logging.getLogger(__name__)


def job_run_alerts() -> None:
    """Scheduled task: evaluate inventory triggers and dispatch alerts."""
    logger.info("Scheduler: Running automated alert evaluation...")
    try:
        with get_db_context() as session:
            mgr = AlertManager(session)
            triggered = mgr.run_all_checks()
            logger.info("Scheduler: Alert check finished. Triggered %d alerts.", len(triggered))
    except Exception as exc:
        logger.error("Scheduler: Alert evaluation job failed: %s", exc)


def job_run_etl() -> None:
    """Scheduled task: execute marketplace ETL sync."""
    logger.info("Scheduler: Running scheduled ETL pipeline...")
    try:
        connector = SyntheticConnector(
            seed=int(time.time()), days=30, total_skus=100, target_orders=1000
        )
        summary = run_etl_pipeline(connector)
        logger.info(
            "Scheduler: ETL completed in %.2fs (Orders synced: %d)",
            summary.duration_seconds,
            summary.orders_synced,
        )
    except Exception as exc:
        logger.error("Scheduler: ETL job failed: %s", exc)


def job_generate_weekly_reports() -> None:
    """Scheduled task: compile and store weekly Excel and PDF reports."""
    logger.info("Scheduler: Compiling weekly operational reports...")
    try:
        with get_db_context() as session:
            reports = generate_reports(session, report_format="both")
            logger.info("Scheduler: Reports generated: %s", {k: str(v) for k, v in reports.items()})
    except Exception as exc:
        logger.error("Scheduler: Report generation job failed: %s", exc)


def configure_scheduler(scheduler: Any) -> None:
    """Add recurring cron and interval jobs to APScheduler instance."""
    settings = get_settings()

    # Alerts check: configurable interval (default every 1 hour)
    scheduler.add_job(
        job_run_alerts,
        trigger="interval",
        hours=settings.ALERTS_CHECK_INTERVAL_HOURS,
        id="alerts_check_job",
        name="Inventory Alerts Monitor",
        replace_existing=True,
    )

    # ETL sync: configurable interval (default every 24 hours)
    scheduler.add_job(
        job_run_etl,
        trigger="interval",
        hours=settings.ETL_SYNC_INTERVAL_HOURS,
        id="etl_sync_job",
        name="Marketplace ETL Sync",
        replace_existing=True,
    )

    # Weekly report generation: Every Monday at 08:00 AM
    scheduler.add_job(
        job_generate_weekly_reports,
        trigger="cron",
        day_of_week="mon",
        hour=8,
        minute=0,
        id="weekly_reports_job",
        name="Weekly Executive Reports",
        replace_existing=True,
    )


def start_background_scheduler() -> BackgroundScheduler:
    """Instantiate and start non-blocking BackgroundScheduler."""
    scheduler = BackgroundScheduler()
    configure_scheduler(scheduler)
    scheduler.start()
    logger.info("SellerPulse BackgroundScheduler started.")
    return scheduler


def run_scheduler_forever() -> None:
    """Blocking scheduler execution for standalone Docker worker processes."""
    scheduler = BlockingScheduler()
    configure_scheduler(scheduler)

    def shutdown_handler(signum: int, frame: Any) -> None:
        logger.info("Received signal %d, shutting down scheduler...", signum)
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    logger.info("SellerPulse BlockingScheduler running in foreground...")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler process terminated.")
