"""Command-line interface (CLI) for SellerPulse operations."""

import logging
from pathlib import Path
from typing import Annotated

import typer
import uvicorn

from sellerpulse.automation.alerts import AlertManager
from sellerpulse.automation.reports import generate_reports
from sellerpulse.automation.scheduler import run_scheduler_forever
from sellerpulse.db.session import get_db_context, init_db
from sellerpulse.etl.load import run_etl_pipeline
from sellerpulse.ingestion.synthetic import SyntheticConnector

app = typer.Typer(
    name="sellerpulse",
    help="SellerPulse - Marketplace SKU Analytics & Automation CLI",
    add_completion=False,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("sellerpulse.cli")


@app.command()
def seed(
    stores: Annotated[int, typer.Option(help="Number of stores to generate")] = 3,
    skus: Annotated[int, typer.Option(help="Number of catalog SKUs")] = 500,
    orders: Annotated[int, typer.Option(help="Number of historical orders")] = 52000,
    seed_val: Annotated[int, typer.Option("--seed", help="Random seed for reproducibility")] = 42,
    days: Annotated[int, typer.Option(help="Historical window in days")] = 365,
) -> None:
    """Generate deterministic synthetic marketplace data and load into SQLite."""
    typer.echo(f"[*] Seeding marketplace data: {stores} stores, {skus} SKUs, {orders} orders...")
    connector = SyntheticConnector(seed=seed_val, days=days, total_skus=skus, target_orders=orders)
    summary = run_etl_pipeline(connector)
    typer.secho(
        f"[OK] Successfully seeded and ingested in {summary.duration_seconds}s! "
        f"Stores: {summary.stores_synced}, SKUs: {summary.skus_synced}, Orders: {summary.orders_synced}",
        fg=typer.colors.GREEN,
        bold=True,
    )


@app.command("run-etl")
def run_etl(
    seed_val: Annotated[int, typer.Option("--seed", help="Random seed")] = 42,
    days: Annotated[int, typer.Option(help="Days of order history")] = 365,
) -> None:
    """Execute marketplace data extraction, validation, and idempotent loading."""
    typer.echo("[*] Running marketplace ETL pipeline...")
    connector = SyntheticConnector(seed=seed_val, days=days)
    summary = run_etl_pipeline(connector)
    typer.secho(
        f"[OK] ETL complete in {summary.duration_seconds}s! "
        f"Synced: {summary.stores_synced} stores, {summary.skus_synced} SKUs, {summary.orders_synced} orders.",
        fg=typer.colors.GREEN,
    )


@app.command("run-alerts")
def run_alerts(
    store_id: Annotated[int | None, typer.Option(help="Scope to specific store id")] = None,
) -> None:
    """Evaluate inventory triggers (stockout, sales drop, dead stock) and dispatch alerts."""
    typer.echo("[*] Evaluating inventory monitors and alert triggers...")
    with get_db_context() as session:
        mgr = AlertManager(session)
        alerts = mgr.run_all_checks(store_id=store_id)
        typer.secho(
            f"[OK] Alert evaluation finished. {len(alerts)} alerts generated and logged.",
            fg=typer.colors.YELLOW,
        )
        for a in alerts[:5]:
            typer.echo(f"  [{a.severity}] {a.alert_type}: {a.message}")


@app.command("generate-report")
def generate_report_cmd(
    report_format: Annotated[str, typer.Option("--format", help="excel, pdf, or both")] = "both",
    store_id: Annotated[int | None, typer.Option(help="Scope to specific store id")] = None,
    output_dir: Annotated[Path | None, typer.Option(help="Output directory")] = None,
) -> None:
    """Generate executive Excel and PDF reports."""
    typer.echo(f"[*] Generating {report_format} reports...")
    with get_db_context() as session:
        files = generate_reports(
            session, store_id=store_id, report_format=report_format, output_dir=output_dir
        )
        for fmt, path in files.items():
            typer.secho(f"[OK] Generated {fmt.upper()} report: {path}", fg=typer.colors.GREEN)


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="API bind host")] = "0.0.0.0",
    port: Annotated[int, typer.Option(help="API bind port")] = 8000,
    reload: Annotated[bool, typer.Option(help="Enable hot reload")] = False,
) -> None:
    """Start the FastAPI REST server."""
    typer.echo(f"[*] Starting SellerPulse FastAPI server on http://{host}:{port} ...")
    uvicorn.run("sellerpulse.api.main:app", host=host, port=port, reload=reload)


@app.command()
def dashboard(
    port: Annotated[int, typer.Option(help="Dashboard port")] = 8501,
) -> None:
    """Launch the Streamlit executive dashboard."""
    import subprocess
    import sys

    typer.echo(f"[*] Launching Streamlit dashboard on port {port}...")
    app_path = Path(__file__).parent / "dashboard" / "app.py"
    cmd = [sys.executable, "-m", "streamlit", "run", str(app_path), "--server.port", str(port)]
    subprocess.run(cmd)


@app.command()
def scheduler() -> None:
    """Run the APScheduler daemon worker process in foreground."""
    typer.echo("[*] Starting APScheduler worker daemon...")
    run_scheduler_forever()


@app.command("init-db")
def init_db_cmd() -> None:
    """Initialize database schema tables."""
    init_db()
    typer.secho("[OK] Database schema initialized successfully.", fg=typer.colors.GREEN)


if __name__ == "__main__":
    app()
