"""SellerPulse - Executive Marketplace SKU Analytics & Automation Dashboard.

Interactive multi-store analytics console built with Streamlit and Plotly.
"""

from datetime import UTC, datetime, timedelta

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from sqlalchemy import select

from sellerpulse.analytics.abc_analysis import ABCAnalysisService
from sellerpulse.analytics.dead_stock import DeadStockService
from sellerpulse.analytics.forecast import StockoutForecastService
from sellerpulse.analytics.sku_metrics import SKUMetricsService
from sellerpulse.db.models import Order, OrderItem, Store
from sellerpulse.db.session import get_db_context, init_db
from sellerpulse.etl.load import run_etl_pipeline
from sellerpulse.ingestion.synthetic import SyntheticConnector

# Configure Streamlit page
st.set_page_config(
    page_title="SellerPulse | Marketplace Analytics",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded",
)


def load_stores_list() -> list[tuple[int, str, str]]:
    """Fetch active stores list from database."""
    with get_db_context() as session:
        init_db(session.bind)
        stmt = select(Store.id, Store.store_key, Store.name).where(Store.is_active.is_(True))
        return session.execute(stmt).all()


def load_timeseries_data(
    store_id: int | None, start_dt: datetime, end_dt: datetime
) -> pd.DataFrame:
    """Fetch daily aggregated order metrics for trend visualizations."""
    with get_db_context() as session:
        stmt = (
            select(
                Order.order_date,
                OrderItem.total_price,
                OrderItem.quantity,
                OrderItem.is_returned,
            )
            .join(OrderItem, OrderItem.order_id == Order.id)
            .where(Order.order_date >= start_dt, Order.order_date <= end_dt)
        )
        if store_id is not None:
            stmt = stmt.where(Order.store_id == store_id)

        rows = session.execute(stmt).all()
        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows, columns=["order_date", "total_price", "quantity", "is_returned"])
        df["date"] = pd.to_datetime(df["order_date"]).dt.date
        df["net_revenue"] = df["total_price"] * (~df["is_returned"]).astype(int)

        daily = (
            df.groupby("date")
            .agg(
                gross_revenue=("total_price", "sum"),
                net_revenue=("net_revenue", "sum"),
                units_sold=("quantity", "sum"),
            )
            .reset_index()
        )
        daily["date"] = pd.to_datetime(daily["date"])
        return daily.sort_values("date")


def render_header():
    """Render top branding bar."""
    st.markdown(
        """
        <div style="background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%); padding: 1.5rem; border-radius: 10px; margin-bottom: 1.5rem; border: 1px solid #334155;">
            <div style="display: flex; justify-content: space-between; align-items: center;">
                <div>
                    <h1 style="color: #38bdf8; margin: 0; font-size: 1.8rem; font-weight: 700;">⚡ SellerPulse</h1>
                    <p style="color: #94a3b8; margin: 0.3rem 0 0 0; font-size: 0.95rem;">
                        Marketplace SKU Analytics & Automated Inventory Optimization
                    </p>
                </div>
                <div style="text-align: right; color: #64748b; font-size: 0.85rem;">
                    Platform Status: <span style="color: #22c55e; font-weight: 600;">● Online</span> | SQLite WAL Engine
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def main():
    render_header()

    # Sidebar controls
    with st.sidebar:
        st.header("⚙️ Marketplace Scope")
        stores = load_stores_list()

        store_options = {"All Stores (Consolidated)": None}
        for s_id, s_key, s_name in stores:
            store_options[f"{s_name} ({s_key})"] = s_id

        selected_store_label = st.selectbox("Select Store", list(store_options.keys()))
        selected_store_id = store_options[selected_store_label]

        st.subheader("📅 Timeframe")
        time_preset = st.radio(
            "Quick Date Filter",
            ["Last 30 Days", "Last 90 Days", "Last 365 Days", "All Time"],
            index=2,
        )

        anchor_end = datetime(2026, 10, 1, tzinfo=UTC)
        if time_preset == "Last 30 Days":
            start_date = anchor_end - timedelta(days=30)
        elif time_preset == "Last 90 Days":
            start_date = anchor_end - timedelta(days=90)
        elif time_preset == "Last 365 Days":
            start_date = anchor_end - timedelta(days=365)
        else:
            start_date = datetime(2020, 1, 1, tzinfo=UTC)

        st.markdown("---")
        st.subheader("🔄 Pipeline Operations")
        if st.button("🚀 Seed & Run Full ETL Pipeline", type="primary"):
            with st.spinner("Generating 500 SKUs & 50,000+ orders across 3 stores..."):
                connector = SyntheticConnector(
                    seed=42, days=365, total_skus=500, target_orders=52000
                )
                summary = run_etl_pipeline(connector)
                st.success(
                    f"ETL completed in {summary.duration_seconds}s! Synced {summary.stores_synced} stores, {summary.skus_synced} SKUs, {summary.orders_synced} orders."
                )
                st.rerun()

    # Database query execution
    with get_db_context() as session:
        metrics_svc = SKUMetricsService(session)
        abc_svc = ABCAnalysisService(session)
        dead_svc = DeadStockService(session)
        forecast_svc = StockoutForecastService(session)

        # High-level summary
        summary = metrics_svc.get_store_summary(
            store_id=selected_store_id, start_date=start_date, end_date=anchor_end
        )
        dead_summary = dead_svc.get_summary(store_id=selected_store_id, as_of_date=anchor_end)
        critical_stockouts = forecast_svc.get_critical_stockouts(
            store_id=selected_store_id, as_of_date=anchor_end, days_threshold=14.0
        )

        if summary["total_skus"] == 0:
            st.warning(
                "No catalog or transaction records detected in database. Please click **'Seed & Run Full ETL Pipeline'** in the left sidebar to initialize the dataset."
            )
            return

        # Top Executive KPI Cards
        kpi1, kpi2, kpi3, kpi4, kpi5, kpi6 = st.columns(6)
        with kpi1:
            st.metric(
                "Gross Revenue",
                f"${summary['total_gross_revenue']:,.2f}",
                f"${summary['total_net_revenue']:,.2f} Net",
            )
        with kpi2:
            st.metric(
                "Units Sold",
                f"{summary['total_units_sold']:,}",
                f"{summary['total_skus']} Catalog SKUs",
            )
        with kpi3:
            st.metric(
                "Gross Margin",
                f"{summary['overall_margin_pct']:.1f}%",
                f"${summary['total_gross_profit']:,.2f} Profit",
            )
        with kpi4:
            st.metric(
                "Return Rate",
                f"{summary['overall_return_rate_pct']:.1f}%",
                "Units Returned",
                delta_color="inverse",
            )
        with kpi5:
            st.metric(
                "Dead Capital",
                f"${dead_summary.dead_capital_tied_up:,.2f}",
                f"{dead_summary.total_dead_skus} Dead SKUs",
                delta_color="inverse",
            )
        with kpi6:
            st.metric(
                "Stockout Risk",
                f"{len(critical_stockouts)} SKUs",
                "<14 Days Stock Left",
                delta_color="inverse",
            )

        st.markdown("---")

        # Tabs for analytical views
        tab_trends, tab_abc, tab_forecast, tab_dead, tab_catalog = st.tabs(
            [
                "📈 Revenue & Sales Trends",
                "📊 ABC Inventory Analysis",
                "⚠️ Stockout Risk & Forecast",
                "🧊 Dead Stock & Liquidation",
                "📦 SKU Performance Explorer",
            ]
        )

        # Tab 1: Trends
        with tab_trends:
            st.subheader("Sales Velocity & Seasonality Trajectory")
            ts_df = load_timeseries_data(selected_store_id, start_date, anchor_end)
            if not ts_df.empty:
                col_chart1, col_chart2 = st.columns([2, 1])
                with col_chart1:
                    fig_trend = go.Figure()
                    fig_trend.add_trace(
                        go.Scatter(
                            x=ts_df["date"],
                            y=ts_df["gross_revenue"],
                            name="Gross Revenue ($)",
                            fill="tozeroy",
                            line={"color": "#38bdf8", "width": 2},
                        )
                    )
                    fig_trend.add_trace(
                        go.Scatter(
                            x=ts_df["date"],
                            y=ts_df["net_revenue"],
                            name="Net Revenue ($)",
                            line={"color": "#22c55e", "width": 1.5, "dash": "dot"},
                        )
                    )
                    fig_trend.update_layout(
                        title="Daily Revenue Trend (Seasonality Peaks in Q4)",
                        template="plotly_dark",
                        height=380,
                        margin={"l": 20, "r": 20, "t": 40, "b": 20},
                        legend={
                            "orientation": "h",
                            "yanchor": "bottom",
                            "y": 1.02,
                            "xanchor": "right",
                            "x": 1,
                        },
                    )
                    st.plotly_chart(fig_trend, use_container_width=True)

                with col_chart2:
                    sku_df = metrics_svc.get_sku_metrics_df(
                        store_id=selected_store_id, start_date=start_date, end_date=anchor_end
                    )
                    cat_summary = sku_df.groupby("category")["gross_revenue"].sum().reset_index()
                    fig_cat = px.pie(
                        cat_summary,
                        values="gross_revenue",
                        names="category",
                        title="Revenue Share by Category",
                        hole=0.45,
                        template="plotly_dark",
                    )
                    fig_cat.update_layout(height=380, margin={"l": 20, "r": 20, "t": 40, "b": 20})
                    st.plotly_chart(fig_cat, use_container_width=True)

        # Tab 2: ABC Analysis
        with tab_abc:
            st.subheader("Pareto 80/15/5 Inventory Segmentation")
            abc_summary = abc_svc.get_summary(
                store_id=selected_store_id, start_date=start_date, end_date=anchor_end
            )
            abc_df = abc_svc.calculate_abc_df(
                store_id=selected_store_id, start_date=start_date, end_date=anchor_end
            )

            c1, c2, c3 = st.columns(3)
            with c1:
                st.success(
                    f"**Class A (Top Drivers)**\n\n"
                    f"• **{abc_summary.class_a_skus} SKUs** ({abc_summary.class_a_sku_pct:.1f}% of catalog)\n"
                    f"• **${abc_summary.class_a_revenue:,.2f}** ({abc_summary.class_a_revenue_pct:.1f}% revenue)\n"
                    f"• Strategy: Maximum service level, continuous safety stock."
                )
            with c2:
                st.info(
                    f"**Class B (Stable Mid-Tier)**\n\n"
                    f"• **{abc_summary.class_b_skus} SKUs** ({abc_summary.class_b_sku_pct:.1f}% of catalog)\n"
                    f"• **${abc_summary.class_b_revenue:,.2f}** ({abc_summary.class_b_revenue_pct:.1f}% revenue)\n"
                    f"• Strategy: Standard automated replenishment, bi-weekly review."
                )
            with c3:
                st.warning(
                    f"**Class C (Tail / Low Volume)**\n\n"
                    f"• **{abc_summary.class_c_skus} SKUs** ({abc_summary.class_c_sku_pct:.1f}% of catalog)\n"
                    f"• **${abc_summary.class_c_revenue:,.2f}** ({abc_summary.class_c_revenue_pct:.1f}% revenue)\n"
                    f"• Strategy: Minimum batch orders, evaluate clearance if stagnant."
                )

            if not abc_df.empty:
                fig_abc = go.Figure()
                fig_abc.add_trace(
                    go.Bar(
                        x=abc_df["sku_code"],
                        y=abc_df["gross_revenue"],
                        name="Gross Revenue ($)",
                        marker_color="#0284c7",
                        yaxis="y1",
                    )
                )
                fig_abc.add_trace(
                    go.Scatter(
                        x=abc_df["sku_code"],
                        y=abc_df["cumulative_revenue_pct"],
                        name="Cumulative Revenue %",
                        line={"color": "#f59e0b", "width": 2.5},
                        yaxis="y2",
                    )
                )
                fig_abc.update_layout(
                    title="Pareto Revenue Concentration Curve (80/15/5)",
                    template="plotly_dark",
                    height=400,
                    margin={"l": 20, "r": 20, "t": 40, "b": 20},
                    yaxis={"title": "Gross Revenue ($)"},
                    yaxis2={
                        "title": "Cumulative Revenue %",
                        "overlaying": "y",
                        "side": "right",
                        "range": [0, 105],
                    },
                    legend={
                        "orientation": "h",
                        "yanchor": "bottom",
                        "y": 1.02,
                        "xanchor": "right",
                        "x": 1,
                    },
                )
                st.plotly_chart(fig_abc, use_container_width=True)

        # Tab 3: Stockout Forecasting
        with tab_forecast:
            st.subheader("Predictive Depletion & Stockout Risk Table")
            forecast_df = forecast_svc.get_forecast_df(
                store_id=selected_store_id, as_of_date=anchor_end
            )

            col_filter, col_stat = st.columns([3, 1])
            with col_filter:
                risk_filter = st.multiselect(
                    "Filter by Risk Level",
                    ["CRITICAL", "WARNING", "HEALTHY", "OVERSTOCKED", "OUT_OF_STOCK"],
                    default=["CRITICAL", "WARNING"],
                )
            with col_stat:
                st.info(f"Showing items matching: **{', '.join(risk_filter)}**")

            filtered_forecast = forecast_df[forecast_df["risk_level"].isin(risk_filter)]

            display_cols = [
                "sku_code",
                "title",
                "category",
                "current_stock",
                "lead_time_days",
                "effective_velocity",
                "days_of_stock_left",
                "risk_level",
                "suggested_reorder_qty",
            ]
            st.dataframe(
                filtered_forecast[display_cols].rename(
                    columns={
                        "sku_code": "SKU",
                        "title": "Title",
                        "category": "Category",
                        "current_stock": "On Hand",
                        "lead_time_days": "Lead Time (d)",
                        "effective_velocity": "Run Rate (units/d)",
                        "days_of_stock_left": "Days Left",
                        "risk_level": "Risk Status",
                        "suggested_reorder_qty": "Suggested Reorder",
                    }
                ),
                use_container_width=True,
                height=420,
            )

        # Tab 4: Dead Stock
        with tab_dead:
            st.subheader("Stagnant Inventory & Working Capital Liquidation")
            dead_df = dead_svc.detect_non_performing_stock_df(
                store_id=selected_store_id, as_of_date=anchor_end
            )

            st.markdown(
                f"""
                <div style="background: rgba(239, 68, 68, 0.1); border-left: 4px solid #ef4444; padding: 1rem; border-radius: 4px; margin-bottom: 1rem;">
                    <strong>Working Capital Alert:</strong>
                    ${dead_summary.total_non_performing_capital:,.2f} is currently locked in dead and slow-moving stock
                    ({dead_summary.capital_share_pct:.1f}% of total catalog inventory valuation).
                </div>
                """,
                unsafe_allow_html=True,
            )

            if not dead_df.empty:
                dead_display = [
                    "sku_code",
                    "title",
                    "category",
                    "current_stock",
                    "cost_price",
                    "retail_price",
                    "capital_tied_up",
                    "days_since_last_sale",
                    "classification",
                    "recommendation",
                ]
                st.dataframe(
                    dead_df[dead_display].rename(
                        columns={
                            "sku_code": "SKU",
                            "title": "Title",
                            "category": "Category",
                            "current_stock": "Units in Stock",
                            "cost_price": "Unit Cost",
                            "retail_price": "Retail Price",
                            "capital_tied_up": "Capital Tied ($)",
                            "days_since_last_sale": "Days No Sale",
                            "classification": "Classification",
                            "recommendation": "Recommended Liquidation Action",
                        }
                    ),
                    use_container_width=True,
                    height=400,
                )

        # Tab 5: Catalog Performance Explorer
        with tab_catalog:
            st.subheader("Comprehensive SKU Catalog Performance")
            sku_metrics_df = metrics_svc.get_sku_metrics_df(
                store_id=selected_store_id, start_date=start_date, end_date=anchor_end
            )

            top10 = sku_metrics_df.nlargest(10, "gross_revenue")[
                ["sku_code", "title", "category", "units_sold", "gross_revenue", "margin_pct"]
            ]
            bot10 = sku_metrics_df[sku_metrics_df["units_sold"] > 0].nsmallest(10, "gross_revenue")[
                ["sku_code", "title", "category", "units_sold", "gross_revenue", "margin_pct"]
            ]

            c_top, c_bot = st.columns(2)
            with c_top:
                st.write("🏆 **Top 10 Best Sellers by Revenue**")
                st.dataframe(top10, use_container_width=True, hide_index=True)
            with c_bot:
                st.write("⚠️ **Bottom 10 Active Performers**")
                st.dataframe(bot10, use_container_width=True, hide_index=True)

            st.write("📋 **Full Catalog Explorer**")
            st.dataframe(sku_metrics_df, use_container_width=True, height=350)


if __name__ == "__main__":
    main()
