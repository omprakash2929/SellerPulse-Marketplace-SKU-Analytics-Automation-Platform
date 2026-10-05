"""Automated executive report generation in Excel and PDF formats."""

import logging
from datetime import UTC, datetime
from pathlib import Path

from fpdf import FPDF
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from sqlalchemy import select
from sqlalchemy.orm import Session

from sellerpulse.analytics.abc_analysis import ABCAnalysisService
from sellerpulse.analytics.dead_stock import DeadStockService
from sellerpulse.analytics.forecast import StockoutForecastService
from sellerpulse.analytics.sku_metrics import SKUMetricsService
from sellerpulse.config import get_settings
from sellerpulse.db.models import AlertLog, Store

logger = logging.getLogger(__name__)


class ExcelReportGenerator:
    """Generates styled multi-tab Excel reports using openpyxl."""

    HEADER_FILL = PatternFill(start_color="1E293B", end_color="1E293B", fill_type="solid")
    HEADER_FONT = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    THIN_BORDER = Border(
        left=Side(style="thin", color="E2E8F0"),
        right=Side(style="thin", color="E2E8F0"),
        top=Side(style="thin", color="E2E8F0"),
        bottom=Side(style="thin", color="E2E8F0"),
    )

    def generate(
        self,
        session: Session,
        store_id: int | None = None,
        output_path: Path | None = None,
    ) -> Path:
        """Create styled multi-tab Excel spreadsheet."""
        settings = get_settings()
        dest_dir = output_path.parent if output_path else settings.REPORTS_DIR
        dest_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        file_path = output_path or (dest_dir / f"sellerpulse_report_{timestamp}.xlsx")

        wb = Workbook()
        # Remove default sheet
        wb.remove(wb.active)

        # 1. Executive Summary Sheet
        self._build_summary_sheet(wb, session, store_id)

        # 2. ABC Inventory Analysis Sheet
        self._build_abc_sheet(wb, session, store_id)

        # 3. Stockout Risk Sheet
        self._build_stockout_sheet(wb, session, store_id)

        # 4. Dead Stock Sheet
        self._build_dead_stock_sheet(wb, session, store_id)

        # 5. Alerts Log Sheet
        self._build_alerts_sheet(wb, session, store_id)

        wb.save(file_path)
        logger.info("Excel report generated successfully at %s", file_path)
        return file_path

    def _style_headers(self, ws, headers: list[str]) -> None:
        ws.append(headers)
        for col_idx in range(1, len(headers) + 1):
            cell = ws.cell(row=ws.max_row, column=col_idx)
            cell.fill = self.HEADER_FILL
            cell.font = self.HEADER_FONT
            cell.alignment = Alignment(horizontal="center", vertical="center")

    def _auto_fit_columns(self, ws) -> None:
        for col in ws.columns:
            max_len = max(len(str(cell.value or "")) for cell in col)
            col_letter = get_column_letter(col[0].column)
            ws.column_dimensions[col_letter].width = max(max_len + 4, 12)

    def _build_summary_sheet(self, wb: Workbook, session: Session, store_id: int | None) -> None:
        ws = wb.create_sheet(title="Executive Summary")
        ws.views.sheetView[0].showGridLines = True

        metrics_svc = SKUMetricsService(session)
        summary = metrics_svc.get_store_summary(store_id=store_id)
        dead_svc = DeadStockService(session)
        dead_summary = dead_svc.get_summary(store_id=store_id)

        ws.append(["SellerPulse Executive KPI Scorecard"])
        ws.cell(1, 1).font = Font(name="Calibri", size=16, bold=True, color="0F172A")
        ws.append([f"Generated at: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}"])
        ws.append([])

        self._style_headers(ws, ["Metric", "Value", "Operational Context"])
        kpis = [
            ("Catalog Active SKUs", summary["total_skus"], "Total catalog count"),
            ("Total Units Sold", summary["total_units_sold"], "Across analyzed order window"),
            ("Gross Revenue", f"${summary['total_gross_revenue']:,.2f}", "Total sales value"),
            ("Net Revenue", f"${summary['total_net_revenue']:,.2f}", "Excluding returns"),
            ("Gross Profit", f"${summary['total_gross_profit']:,.2f}", "Net revenue minus COGS"),
            ("Gross Margin %", f"{summary['overall_margin_pct']:.1f}%", "Overall profitability"),
            ("Return Rate %", f"{summary['overall_return_rate_pct']:.1f}%", "Units returned / sold"),
            ("Total Inventory Valuation", f"${summary['total_inventory_value']:,.2f}", "Cost value on hand"),
            ("Dead Capital Tied Up", f"${dead_summary.dead_capital_tied_up:,.2f}", f"{dead_summary.total_dead_skus} SKUs with 0 sales >90d"),
        ]
        for row in kpis:
            ws.append(list(row))
        self._auto_fit_columns(ws)

    def _build_abc_sheet(self, wb: Workbook, session: Session, store_id: int | None) -> None:
        ws = wb.create_sheet(title="ABC Segmentation")
        ws.views.sheetView[0].showGridLines = True

        abc_svc = ABCAnalysisService(session)
        df = abc_svc.calculate_abc_df(store_id=store_id)

        headers = ["SKU Code", "Title", "Category", "ABC Class", "Gross Revenue", "Revenue Share %", "Cumulative %", "Strategy Recommendation"]
        self._style_headers(ws, headers)

        if not df.empty:
            for _, r in df.iterrows():
                ws.append([
                    r["sku_code"],
                    r["title"],
                    r["category"],
                    r["abc_class"],
                    r["gross_revenue"],
                    r["revenue_share_pct"],
                    r["cumulative_revenue_pct"],
                    r["recommendation"],
                ])
        self._auto_fit_columns(ws)

    def _build_stockout_sheet(self, wb: Workbook, session: Session, store_id: int | None) -> None:
        ws = wb.create_sheet(title="Stockout Risk")
        ws.views.sheetView[0].showGridLines = True

        forecast_svc = StockoutForecastService(session)
        df = forecast_svc.get_forecast_df(store_id=store_id)

        headers = ["SKU Code", "Title", "Category", "On Hand", "Run Rate (units/d)", "Days Stock Left", "Risk Level", "Suggested Reorder"]
        self._style_headers(ws, headers)

        if not df.empty:
            for _, r in df.iterrows():
                ws.append([
                    r["sku_code"],
                    r["title"],
                    r["category"],
                    r["current_stock"],
                    r["effective_velocity"],
                    r["days_of_stock_left"],
                    r["risk_level"],
                    r["suggested_reorder_qty"],
                ])
        self._auto_fit_columns(ws)

    def _build_dead_stock_sheet(self, wb: Workbook, session: Session, store_id: int | None) -> None:
        ws = wb.create_sheet(title="Dead Stock")
        ws.views.sheetView[0].showGridLines = True

        dead_svc = DeadStockService(session)
        df = dead_svc.detect_non_performing_stock_df(store_id=store_id)

        headers = ["SKU Code", "Title", "Category", "Stock", "Unit Cost", "Capital Tied ($)", "Days Since Sale", "Classification", "Action"]
        self._style_headers(ws, headers)

        if not df.empty:
            for _, r in df.iterrows():
                ws.append([
                    r["sku_code"],
                    r["title"],
                    r["category"],
                    r["current_stock"],
                    r["cost_price"],
                    r["capital_tied_up"],
                    r["days_since_last_sale"],
                    r["classification"],
                    r["recommendation"],
                ])
        self._auto_fit_columns(ws)

    def _build_alerts_sheet(self, wb: Workbook, session: Session, store_id: int | None) -> None:
        ws = wb.create_sheet(title="Recent Alerts")
        ws.views.sheetView[0].showGridLines = True

        stmt = select(AlertLog).order_by(AlertLog.triggered_at.desc()).limit(100)
        if store_id is not None:
            stmt = stmt.where(AlertLog.store_id == store_id)
        alerts = session.execute(stmt).scalars().all()

        headers = ["Alert ID", "Type", "Severity", "Triggered At", "Message"]
        self._style_headers(ws, headers)

        for a in alerts:
            ws.append([
                a.id,
                a.alert_type,
                a.severity,
                a.triggered_at.strftime("%Y-%m-%d %H:%M:%S"),
                a.message,
            ])
        self._auto_fit_columns(ws)


class PDFReportGenerator:
    """Generates executive PDF reports using fpdf2."""

    def generate(
        self,
        session: Session,
        store_id: int | None = None,
        output_path: Path | None = None,
    ) -> Path:
        """Create executive summary PDF report."""
        settings = get_settings()
        dest_dir = output_path.parent if output_path else settings.REPORTS_DIR
        dest_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        file_path = output_path or (dest_dir / f"sellerpulse_report_{timestamp}.pdf")

        metrics_svc = SKUMetricsService(session)
        summary = metrics_svc.get_store_summary(store_id=store_id)
        abc_svc = ABCAnalysisService(session)
        abc_summary = abc_svc.get_summary(store_id=store_id)
        forecast_svc = StockoutForecastService(session)
        critical_stockouts = forecast_svc.get_critical_stockouts(store_id=store_id, days_threshold=14.0)
        dead_svc = DeadStockService(session)
        dead_summary = dead_svc.get_summary(store_id=store_id)

        store_name = "All Stores (Consolidated)"
        if store_id is not None:
            st_ent = session.get(Store, store_id)
            if st_ent:
                store_name = st_ent.name

        pdf = FPDF(orientation="P", unit="mm", format="A4")
        pdf.set_auto_page_break(auto=True, margin=15)
        pdf.add_page()

        # Header Title Banner
        pdf.set_fill_color(30, 41, 59)  # Dark slate
        pdf.rect(0, 0, 210, 35, "F")
        pdf.set_font("Helvetica", "B", 20)
        pdf.set_text_color(56, 189, 248)  # Sky blue
        pdf.set_xy(12, 10)
        pdf.cell(0, 8, "SellerPulse Executive Report", ln=True)

        pdf.set_font("Helvetica", "", 10)
        pdf.set_text_color(203, 213, 225)
        pdf.set_xy(12, 20)
        pdf.cell(0, 6, f"Scope: {store_name} | Generated: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}", ln=True)

        pdf.set_y(42)
        pdf.set_text_color(15, 23, 42)

        # 1. Executive Scorecard
        pdf.set_font("Helvetica", "B", 13)
        pdf.cell(0, 8, "1. Executive Financial & Inventory Scorecard", ln=True)
        pdf.set_font("Helvetica", "", 10)

        pdf.set_fill_color(241, 245, 249)
        pdf.rect(12, pdf.get_y(), 186, 32, "F")

        pdf.set_xy(16, pdf.get_y() + 4)
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(60, 6, f"Gross Revenue: ${summary['total_gross_revenue']:,.2f}", ln=False)
        pdf.cell(60, 6, f"Net Revenue: ${summary['total_net_revenue']:,.2f}", ln=False)
        pdf.cell(60, 6, f"Gross Margin: {summary['overall_margin_pct']:.1f}%", ln=True)

        pdf.set_x(16)
        pdf.cell(60, 6, f"Units Sold: {summary['total_units_sold']:,}", ln=False)
        pdf.cell(60, 6, f"Gross Profit: ${summary['total_gross_profit']:,.2f}", ln=False)
        pdf.cell(60, 6, f"Return Rate: {summary['overall_return_rate_pct']:.1f}%", ln=True)

        pdf.set_x(16)
        pdf.cell(60, 6, f"Inventory Value: ${summary['total_inventory_value']:,.2f}", ln=False)
        pdf.cell(60, 6, f"Dead Capital: ${dead_summary.dead_capital_tied_up:,.2f}", ln=False)
        pdf.cell(60, 6, f"Stockout Risk: {len(critical_stockouts)} SKUs", ln=True)

        pdf.ln(10)

        # 2. ABC Inventory Breakdown
        pdf.set_font("Helvetica", "B", 13)
        pdf.cell(0, 8, "2. Pareto 80/15/5 ABC Revenue Segmentation", ln=True)
        pdf.set_font("Helvetica", "", 9)

        # Table Header
        pdf.set_fill_color(51, 65, 85)
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 9)
        pdf.cell(30, 7, "Segment", 1, 0, "C", True)
        pdf.cell(30, 7, "SKU Count", 1, 0, "C", True)
        pdf.cell(35, 7, "% Catalog", 1, 0, "C", True)
        pdf.cell(45, 7, "Revenue ($)", 1, 0, "C", True)
        pdf.cell(46, 7, "% Revenue", 1, 1, "C", True)

        pdf.set_text_color(15, 23, 42)
        pdf.set_font("Helvetica", "", 9)
        pdf.cell(30, 6, "Class A (Top)", 1, 0, "L")
        pdf.cell(30, 6, str(abc_summary.class_a_skus), 1, 0, "C")
        pdf.cell(35, 6, f"{abc_summary.class_a_sku_pct:.1f}%", 1, 0, "C")
        pdf.cell(45, 6, f"${abc_summary.class_a_revenue:,.2f}", 1, 0, "R")
        pdf.cell(46, 6, f"{abc_summary.class_a_revenue_pct:.1f}%", 1, 1, "R")

        pdf.cell(30, 6, "Class B (Mid)", 1, 0, "L")
        pdf.cell(30, 6, str(abc_summary.class_b_skus), 1, 0, "C")
        pdf.cell(35, 6, f"{abc_summary.class_b_sku_pct:.1f}%", 1, 0, "C")
        pdf.cell(45, 6, f"${abc_summary.class_b_revenue:,.2f}", 1, 0, "R")
        pdf.cell(46, 6, f"{abc_summary.class_b_revenue_pct:.1f}%", 1, 1, "R")

        pdf.cell(30, 6, "Class C (Tail)", 1, 0, "L")
        pdf.cell(30, 6, str(abc_summary.class_c_skus), 1, 0, "C")
        pdf.cell(35, 6, f"{abc_summary.class_c_sku_pct:.1f}%", 1, 0, "C")
        pdf.cell(45, 6, f"${abc_summary.class_c_revenue:,.2f}", 1, 0, "R")
        pdf.cell(46, 6, f"{abc_summary.class_c_revenue_pct:.1f}%", 1, 1, "R")

        pdf.ln(8)

        # 3. Critical Stockout Risks (Top 5)
        pdf.set_font("Helvetica", "B", 13)
        pdf.cell(0, 8, "3. Critical Stockout Risks (<14 Days Runway)", ln=True)

        if critical_stockouts:
            pdf.set_fill_color(225, 29, 72)  # Rose Red
            pdf.set_text_color(255, 255, 255)
            pdf.set_font("Helvetica", "B", 9)
            pdf.cell(35, 7, "SKU Code", 1, 0, "C", True)
            pdf.cell(65, 7, "Product Title", 1, 0, "L", True)
            pdf.cell(26, 7, "On Hand", 1, 0, "C", True)
            pdf.cell(30, 7, "Days Left", 1, 0, "C", True)
            pdf.cell(30, 7, "Reorder Qty", 1, 1, "C", True)

            pdf.set_text_color(15, 23, 42)
            pdf.set_font("Helvetica", "", 8)
            for item in critical_stockouts[:6]:
                title_cut = item.title[:35] + "..." if len(item.title) > 35 else item.title
                pdf.cell(35, 6, item.sku_code, 1, 0, "L")
                pdf.cell(65, 6, title_cut, 1, 0, "L")
                pdf.cell(26, 6, str(item.current_stock), 1, 0, "C")
                pdf.cell(30, 6, f"{item.days_of_stock_left:.1f}d", 1, 0, "C")
                pdf.cell(30, 6, str(item.suggested_reorder_qty), 1, 1, "C")
        else:
            pdf.set_font("Helvetica", "", 10)
            pdf.cell(0, 6, "No imminent stockouts detected across the catalog.", ln=True)

        pdf.output(str(file_path))
        logger.info("PDF report generated successfully at %s", file_path)
        return file_path


def generate_reports(
    session: Session,
    store_id: int | None = None,
    report_format: str = "both",
    output_dir: Path | None = None,
) -> dict[str, Path]:
    """Generate Excel and/or PDF reports for given store scope."""
    results: dict[str, Path] = {}
    if report_format in ("excel", "both"):
        excel_gen = ExcelReportGenerator()
        results["excel"] = excel_gen.generate(session, store_id=store_id, output_path=output_dir)
    if report_format in ("pdf", "both"):
        pdf_gen = PDFReportGenerator()
        results["pdf"] = pdf_gen.generate(session, store_id=store_id, output_path=output_dir)
    return results
