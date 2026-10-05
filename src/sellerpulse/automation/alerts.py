"""Automated inventory and sales drop monitoring with console and webhook dispatch."""

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import requests
from sqlalchemy.orm import Session

from sellerpulse.analytics.dead_stock import DeadStockService
from sellerpulse.analytics.forecast import StockoutForecastService
from sellerpulse.config import get_settings
from sellerpulse.db.models import AlertLog

logger = logging.getLogger(__name__)


@dataclass
class AlertNotification:
    """Standardized alert payload."""

    store_id: int
    sku_id: int | None
    sku_code: str | None
    alert_type: str  # LOW_STOCK, SALES_DROP, DEAD_STOCK
    severity: str  # INFO, WARNING, CRITICAL
    message: str
    metrics: dict


class AlertDispatcher:
    """Dispatches notifications to console and optional external webhooks (Slack/Telegram)."""

    def __init__(self) -> None:
        self.settings = get_settings()

    def dispatch(self, alert: AlertNotification, session: Session) -> AlertLog:
        """Persist alert to SQLite and forward to active notification channels."""
        # 1. Persist to database
        db_alert = AlertLog(
            store_id=alert.store_id,
            sku_id=alert.sku_id,
            alert_type=alert.alert_type,
            severity=alert.severity,
            message=alert.message,
            metrics_payload=json.dumps(alert.metrics),
            triggered_at=datetime.now(UTC).replace(tzinfo=None),
        )
        session.add(db_alert)
        session.flush()

        # 2. Console notification
        log_msg = (
            f"[{alert.severity}] {alert.alert_type} for Store {alert.store_id}: {alert.message}"
        )
        if alert.severity == "CRITICAL":
            logger.error(log_msg)
        elif alert.severity == "WARNING":
            logger.warning(log_msg)
        else:
            logger.info(log_msg)

        # 3. Optional Webhook dispatch (Slack/Discord format)
        if self.settings.ALERTS_WEBHOOK_ENABLED and self.settings.ALERTS_WEBHOOK_URL:
            self._send_webhook(alert)

        # 4. Optional Telegram notification
        if (
            self.settings.ALERTS_WEBHOOK_ENABLED
            and self.settings.ALERTS_TELEGRAM_BOT_TOKEN
            and self.settings.ALERTS_TELEGRAM_CHAT_ID
        ):
            self._send_telegram(alert)

        return db_alert

    def _send_webhook(self, alert: AlertNotification) -> None:
        """Send formatted notification to webhook URL."""
        try:
            payload = {
                "text": f"*{alert.severity} Alert*: {alert.alert_type}\n{alert.message}",
                "severity": alert.severity,
                "sku_code": alert.sku_code,
                "timestamp": datetime.now(UTC).isoformat(),
            }
            resp = requests.post(
                self.settings.ALERTS_WEBHOOK_URL,
                json=payload,
                timeout=5.0,
            )
            if resp.status_code >= 400:
                logger.warning("Webhook dispatch returned status %d", resp.status_code)
        except Exception as exc:
            logger.warning("Webhook dispatch failed: %s", exc)

    def _send_telegram(self, alert: AlertNotification) -> None:
        """Send message via Telegram Bot API."""
        try:
            token = self.settings.ALERTS_TELEGRAM_BOT_TOKEN
            chat_id = self.settings.ALERTS_TELEGRAM_CHAT_ID
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": f"🚨 *{alert.severity}* - {alert.alert_type}\n\n{alert.message}",
                "parse_mode": "Markdown",
            }
            requests.post(url, json=payload, timeout=5.0)
        except Exception as exc:
            logger.warning("Telegram dispatch failed: %s", exc)


class AlertManager:
    """Evaluates business operational conditions and triggers alerts."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.dispatcher = AlertDispatcher()
        self.forecast_svc = StockoutForecastService(session)
        self.dead_svc = DeadStockService(session)

    def run_all_checks(
        self, store_id: int | None = None, as_of_date: datetime | None = None
    ) -> list[AlertLog]:
        """Execute all automated monitors: low stock, sales drops, and dead stock."""
        alerts: list[AlertLog] = []
        alerts.extend(self.check_low_stock(store_id=store_id, as_of_date=as_of_date))
        alerts.extend(self.check_sales_drops(store_id=store_id, as_of_date=as_of_date))
        alerts.extend(self.check_dead_stock(store_id=store_id, as_of_date=as_of_date))
        return alerts

    def check_low_stock(
        self, store_id: int | None = None, as_of_date: datetime | None = None
    ) -> list[AlertLog]:
        """Detect SKUs with imminent stockout risks."""
        critical_items = self.forecast_svc.get_critical_stockouts(
            store_id=store_id, as_of_date=as_of_date, days_threshold=14.0
        )
        created_alerts: list[AlertLog] = []

        for item in critical_items:
            severity = (
                "CRITICAL"
                if item.days_of_stock_left <= 7.0 or item.risk_level == "CRITICAL"
                else "WARNING"
            )
            msg = (
                f"Low Stock Alert: SKU {item.sku_code} ('{item.title}') has {item.current_stock} units left "
                f"(~{item.days_of_stock_left:.1f} days supply at {item.effective_velocity:.1f} units/day). "
                f"Suggested replenishment: {item.suggested_reorder_qty} units."
            )
            alert = AlertNotification(
                store_id=item.store_id,
                sku_id=item.sku_id,
                sku_code=item.sku_code,
                alert_type="LOW_STOCK",
                severity=severity,
                message=msg,
                metrics={
                    "current_stock": item.current_stock,
                    "effective_velocity": item.effective_velocity,
                    "days_of_stock_left": item.days_of_stock_left,
                    "suggested_reorder_qty": item.suggested_reorder_qty,
                },
            )
            created_alerts.append(self.dispatcher.dispatch(alert, self.session))

        return created_alerts

    def check_sales_drops(
        self, store_id: int | None = None, as_of_date: datetime | None = None
    ) -> list[AlertLog]:
        """Detect sudden sales drops on established sellers (7d velocity << 30d baseline)."""
        df = self.forecast_svc.get_forecast_df(store_id=store_id, as_of_date=as_of_date)
        if df.empty:
            return []

        created_alerts: list[AlertLog] = []
        for _, row in df.iterrows():
            vel_30 = float(row["velocity_30d"])
            vel_7 = float(row["velocity_7d"])

            # Only evaluate items with proven volume (baseline >= 0.5 units/day)
            if vel_30 >= 0.5:
                # Sudden crash: 7-day velocity down by 65%+ or completely zeroed out
                if vel_7 <= 0.35 * vel_30:
                    pct_drop = round((1.0 - (vel_7 / vel_30)) * 100.0, 1)
                    msg = (
                        f"Sales Drop Alert: SKU {row['sku_code']} ('{row['title']}') 7-day velocity crashed "
                        f"by {pct_drop}% ({vel_7:.2f} units/day vs 30-day baseline of {vel_30:.2f} units/day). "
                        "Investigate listing visibility, Buy Box loss, or out-of-stock events."
                    )
                    alert = AlertNotification(
                        store_id=int(row["store_id"]),
                        sku_id=int(row["sku_id"]),
                        sku_code=str(row["sku_code"]),
                        alert_type="SALES_DROP",
                        severity="WARNING",
                        message=msg,
                        metrics={
                            "velocity_7d": vel_7,
                            "velocity_30d": vel_30,
                            "pct_drop": pct_drop,
                        },
                    )
                    created_alerts.append(self.dispatcher.dispatch(alert, self.session))

        return created_alerts

    def check_dead_stock(
        self, store_id: int | None = None, as_of_date: datetime | None = None
    ) -> list[AlertLog]:
        """Detect significant working capital tied up in dead stock."""
        df = self.dead_svc.detect_non_performing_stock_df(store_id=store_id, as_of_date=as_of_date)
        if df.empty:
            return []

        created_alerts: list[AlertLog] = []
        # Flag items that are classified as DEAD_STOCK with capital >= $300
        dead_items = df[(df["classification"] == "DEAD_STOCK") & (df["capital_tied_up"] >= 300.0)]

        for _, row in dead_items.iterrows():
            msg = (
                f"Dead Stock Alert: SKU {row['sku_code']} ('{row['title']}') has 0 sales in {row['days_since_last_sale']} days. "
                f"${row['capital_tied_up']:,.2f} in working capital tied up across {row['current_stock']} units."
            )
            alert = AlertNotification(
                store_id=int(row["store_id"]),
                sku_id=int(row["sku_id"]),
                sku_code=str(row["sku_code"]),
                alert_type="DEAD_STOCK",
                severity="INFO",
                message=msg,
                metrics={
                    "capital_tied_up": float(row["capital_tied_up"]),
                    "days_since_last_sale": int(row["days_since_last_sale"]),
                    "current_stock": int(row["current_stock"]),
                },
            )
            created_alerts.append(self.dispatcher.dispatch(alert, self.session))

        return created_alerts
