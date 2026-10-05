# SellerPulse Architecture & System Design

**SellerPulse** is an end-to-end Marketplace SKU Analytics & Automation Platform engineered for high-volume multichannel e-commerce operations. It handles catalog ingestion, idempotent data transformation, predictive stockout forecasting, ABC Pareto revenue classification, dead stock detection, and automated repricing & alert dispatch.

---

## 1. High-Level System Architecture

```mermaid
flowchart TD
    subgraph Ingestion["1. Ingestion Layer"]
        SC[Synthetic Connector\n3 Stores, 500 SKUs, 52k Orders]
        SH[Shopify Connector Stub\nGraphQL Admin API 2024-04]
        AZ[Amazon SP-API Stub\nOrders & Inventory Reports]
        INT[BaseMarketplaceConnector\nAbstract DTO Interface]
        SC --> INT
        SH --> INT
        AZ --> INT
    end

    subgraph ETL["2. ETL & Cleaning Pipeline"]
        CL[DataCleaner\nValidation, Deduplication, Price Sanity]
        LD[ETLLoader\nIdempotent SQLite WAL Loader]
        INT --> CL
        CL --> LD
    end

    subgraph Storage["3. Persistent Storage Engine"]
        DB[(SQLite 3 + WAL Mode\nForeign Keys Enabled\nZero-Lock Read Concurrency)]
        LD --> DB
    end

    subgraph Analytics["4. Analytics Core Engine"]
        SKU_M[SKUMetricsService\nRevenue, Margin, Return %, ASP]
        ABC_S[ABCAnalysisService\n80/15/5 Pareto Revenue Split]
        DEAD_S[DeadStockService\nStagnant Inventory & Capital Tied Up]
        FC_S[StockoutForecastService\nMoving Average + Exp. Smoothing]
        DB --> SKU_M
        DB --> ABC_S
        DB --> DEAD_S
        DB --> FC_S
    end

    subgraph Automation["5. Automation & Operations"]
        AL[AlertManager\nLow Stock & Sales Drops]
        RP[RepricingEngine\nMargin Floor & Liquidation Rules]
        REP[ReportGenerator\nExcel openpyxl + PDF fpdf2]
        SCH[APScheduler Daemon\nPeriodic Sync, Alerts & Reports]
        
        SKU_M --> AL
        FC_S --> AL
        DEAD_S --> AL
        DEAD_S --> RP
        FC_S --> RP
        SKU_M --> REP
        ABC_S --> REP
        SCH --> AL
        SCH --> REP
    end

    subgraph Presentation["6. Presentation & API"]
        ST[Streamlit Dashboard :8501\nInteractive Executive Console]
        API[FastAPI REST Service :8000\nAPI-Key Auth + Prometheus /metrics]
        CLI[Typer CLI\nseed, run-etl, run-alerts, report]

        DB --> ST
        Analytics --> ST
        Analytics --> API
        Automation --> API
        API --> CLI
    end
```

---

## 2. Entity-Relationship Data Model

The data model maintains multi-store tenant isolation, referential integrity with cascading deletes where appropriate, and indexed lookup paths on `store_id`, `sku_code`, and `order_date`.

```mermaid
erDiagram
    STORE ||--o{ SKU : "manages catalog"
    STORE ||--o{ ORDER : "receives transactions"
    STORE ||--o{ ALERT_LOG : "triggers operational alerts"
    SKU ||--o{ ORDER_ITEM : "ordered in"
    SKU ||--o{ PRICE_HISTORY : "tracks price audits"
    SKU ||--o{ ALERT_LOG : "subject of alerts"
    ORDER ||--|{ ORDER_ITEM : "contains line items"

    STORE {
        int id PK
        string store_key UK
        string name
        string platform
        string currency
        boolean is_active
        datetime created_at
    }

    SKU {
        int id PK
        int store_id FK
        string sku_code
        string title
        string category
        float cost_price
        float retail_price
        int current_stock
        int reorder_point
        int lead_time_days
        string status
        datetime updated_at
    }

    ORDER {
        int id PK
        int store_id FK
        string order_number
        datetime order_date
        string customer_email
        float total_amount
        string status
        string channel
        datetime created_at
    }

    ORDER_ITEM {
        int id PK
        int order_id FK
        int sku_id FK
        int quantity
        float unit_price
        float unit_cost
        float total_price
        boolean is_returned
        datetime return_date
    }

    PRICE_HISTORY {
        int id PK
        int sku_id FK
        float old_price
        float new_price
        string reason
        datetime changed_at
    }

    ALERT_LOG {
        int id PK
        int store_id FK
        int sku_id FK
        string alert_type
        string severity
        string message
        string metrics_payload
        boolean acknowledged
        datetime triggered_at
    }
```

---

## 3. Automation & Repricing State Flow

```mermaid
stateDiagram-v2
    [*] --> IngestCatalogAndOrders
    IngestCatalogAndOrders --> CleanAndDeduplicate
    CleanAndDeduplicate --> IdempotentMerge

    state IdempotentMerge {
        [*] --> CheckExistingSKU
        CheckExistingSKU --> UpdatePriceAndStock
        UpdatePriceAndStock --> LogPriceHistory : If Price Changed
        CheckExistingSKU --> InsertNewSKU
    }

    IdempotentMerge --> RunAnalytics
    
    state RunAnalytics {
        [*] --> ComputeRunRate
        ComputeRunRate --> ProjectStockoutDate
        ProjectStockoutDate --> DetectDeadStock
        DetectDeadStock --> ClassifyParetoABC
    }

    RunAnalytics --> EvaluateAutomations

    state EvaluateAutomations {
        [*] --> CheckStockoutRisk
        CheckStockoutRisk --> DispatchAlert : If Days Left <= 14d
        [*] --> CheckSalesVelocityDrop
        CheckSalesVelocityDrop --> DispatchAlert : If 7d Drop > 65%
        [*] --> RepricingRules
        
        state RepricingRules {
            [*] --> MarginFloorGuard
            MarginFloorGuard --> EnforceFloor : If Price < Cost*(1+Floor)
            MarginFloorGuard --> LiquidationMarkdown : If Dead Stock > 90d
            MarginFloorGuard --> ScarcityPremium : If High Velocity & Low Stock
        }
    }

    DispatchAlert --> AlertLogPersistence
    RepricingRules --> ApplyPriceHistoryAudit
    EvaluateAutomations --> GenerateWeeklyReports : Mondays 08:00
```

---

## 4. Key Architectural Decisions

### 4.1 SQLite Write-Ahead Logging (WAL) Mode
- **Decision**: SQLite configured with `PRAGMA journal_mode=WAL;` and `PRAGMA synchronous=NORMAL;`.
- **Rationale**: Standard SQLite locks the entire database file during writes. WAL mode allows concurrent readers while a single writer thread commits transactions, providing high-throughput read operations for the Streamlit dashboard and FastAPI endpoints during heavy ETL load.

### 4.2 Idempotent Loading
- **Decision**: Primary keys and unique business keys (`(store_id, sku_code)` and `(store_id, order_number)`) are pre-indexed and queried before insert.
- **Rationale**: Marketplace sync jobs frequently fail midway due to network timeouts. Idempotency guarantees that re-running the sync will never duplicate revenue, double-count orders, or throw database integrity errors.

### 4.3 Margin Floor Guardrail
- **Decision**: Repricing recommendations must strictly adhere to `cost_price * (1 + margin_floor_pct)`.
- **Rationale**: Automated pricing algorithms in e-commerce can trigger race conditions with competitors (e.g., "race to the bottom"). The hardcoded margin floor prevents selling inventory at a loss.

### 4.4 Decoupled Connector Interface
- **Decision**: All data sources inherit from `BaseMarketplaceConnector` and return normalized Pydantic DTOs (`RawSKU`, `RawOrder`, `RawOrderItem`).
- **Rationale**: Allows instant substitution of the synthetic data generator with real Shopify Admin GraphQL API, Amazon Selling Partner API (SP-API), or BigCommerce connectors without touching downstream ETL, analytics, or UI code.
