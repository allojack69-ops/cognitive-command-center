import asyncio
import copy
import hashlib
import hmac
import json
import math
import os
import sys
import urllib.parse
import urllib.request
import urllib.error
from collections import deque
from datetime import datetime, timezone

import websockets

SYMBOL = "BTCUSDT"
INTERVAL = "1m"
TRADE_HORIZON = 5
SHADOW_HORIZONS = (5, 15, 30, 60)
HORIZON = TRADE_HORIZON  # legacy/default prediction horizon
MINUTE_MS = 60_000

# Adaptive evidence gate: paper orders stay blocked until the same
# regime/strategy has enough multi-horizon shadow evidence.
MIN_ADAPTIVE_TRADE_SAMPLES = 10
REQUIRE_VALIDATED_POSITIVE_EDGE = True
POSITIVE_EDGE_EPSILON_PCT = 0.0

# ------------------------------------------------------------------
# v0.9 RESEARCH-ONLY PARAMETER SURFACE
# Production strategy thresholds and execution logic remain unchanged.
# The lab only freezes counterfactual policies and observes them later.
# ------------------------------------------------------------------
SURFACE_GRID_VERSION = "PS1"
SURFACE_TREND_THRESHOLDS_PCT = (
    0.005,
    0.010,
    0.020,
    0.030,
    0.050,
    0.075,
    0.100,
    0.150,
    0.200,
    0.300,
)
SURFACE_BREAKOUT_VOL_GATES_PCT = (
    0.001,
    0.002,
    0.005,
    0.010,
    0.020,
    0.050,
    0.100,
    0.200,
    0.350,
)
SURFACE_MIN_POLICY_SAMPLES = 20
SURFACE_MIN_ELIGIBLE_TRADE_SAMPLES = 10
SURFACE_SUMMARY_EVERY_STATES = 10
SURFACE_RESEARCH_ENABLED = False

# ------------------------------------------------------------------
# v1.0 STATE REPRESENTATION / TRADEABILITY LAB
# ------------------------------------------------------------------
STATE_REP_VERSION = "SR1"
FEATURE_HISTORY_CANDLES = 120
TRADEABILITY_HORIZONS = SHADOW_HORIZONS
TRADEABILITY_GATE_ENABLED = True
TRADEABILITY_SCORE_THRESHOLD = 1.0

# This margin is deliberately zero for the first preregistered SR1 phase.
# Costs themselves already define the break-even barrier.
TRADEABILITY_SAFETY_MARGIN_PCT = 0.0

TRADEABILITY_SUMMARY_EVERY_STATES = 10
TRADEABILITY_MATRIX_MIN_SAMPLES = 20

# ------------------------------------------------------------------
# v1.1 VOLATILITY CORRIDOR ENGINE — research-only parallel layer.
# It does NOT change SR1/Edge/Risk execution gates yet.
# ------------------------------------------------------------------
CORRIDOR_VERSION = "COR1"
CORRIDOR_WINDOWS = (5, 15, 30, 60, 120)
CORRIDOR_HORIZONS = TRADEABILITY_HORIZONS
CORRIDOR_RESEARCH_ENABLED = True
CORRIDOR_SCORE_THRESHOLD = 1.0
CORRIDOR_SUMMARY_EVERY_STATES = 10
CORRIDOR_MATRIX_MIN_SAMPLES = 20

# Preregistered descriptive state thresholds.
CORRIDOR_NESTED_MICRO_RATIO = 0.25
CORRIDOR_NESTED_MESO_RATIO = 0.40
CORRIDOR_EXPANSION_RATIO = 1.35
CORRIDOR_CONTRACTION_RATIO = 0.75
CORRIDOR_EDGE_PRESSURE_THRESHOLD = 0.75

# ------------------------------------------------------------------
# v1.2 COR2 MULTI-LABEL CORRIDOR STATE.
# COR1 remains intact as the independent scalar corridor predictor.
# COR2 is a parallel research layer: flags + transitions + outcomes.
# It is NOT connected to paper execution.
# ------------------------------------------------------------------
CORRIDOR2_VERSION = "COR2"
CORRIDOR2_RESEARCH_ENABLED = True
CORRIDOR2_SUMMARY_EVERY_STATES = 10
CORRIDOR2_MATRIX_MIN_SAMPLES = 20

# Edge direction is taken only when COR1 edge pressure is already high.
CORRIDOR2_POSITION_MID = 0.50

# Extreme contextual volume flag from SR1.
CORRIDOR2_VOLUME_Z_EXTREME = 2.0

# ------------------------------------------------------------------
# v1.3 COR3 SCALE-NORMALIZED TRANSITION + STATE AGE ENGINE.
#
# COR1 = scalar corridor geometry.
# COR2 = multi-label flags and discrete transitions.
# COR3 = how LARGE a transition is, on two distinct scales:
#
#   structural magnitude  = stable log change in corridor width
#   economic magnitude    = width change / round-trip cost barrier
#
# It also records dwell time of states and time since transitions.
# COR3 is RESEARCH ONLY and is not connected to execution.
# ------------------------------------------------------------------
CORRIDOR3_VERSION = "COR3"
CORRIDOR3_RESEARCH_ENABLED = True
CORRIDOR3_SUMMARY_EVERY_STATES = 10
CORRIDOR3_MATRIX_MIN_SAMPLES = 20

# Stabilizer for log(width_now / width_previous).
# Scale epsilon to economic break-even so near-zero widths cannot
# create meaningless 100x/1000x "expansion" singularities.
CORRIDOR3_EPSILON_COST_FRACTION = 0.01

# Age is measured from closed-candle timestamps, not process loops,
# so a websocket reconnect does not silently reset elapsed time.
CORRIDOR3_MINUTE_MS = 60_000

# ------------------------------------------------------------------
# v1.4.1 GEO1 — EXTREMUM MEMORY + CORRIDOR SPINE.
#
# GEO1 is a geometric context layer. It observes:
#   - local swing highs / lows
#   - clustered structural levels
#   - corridor centerlines on H5/H15/H30/H60/H120
#   - translation / contraction / expansion of boundaries
#   - nested "spine" alignment across scales
#
# GEO1 augments COR2 flags/transitions and therefore becomes available
# to COR3 scale/age research. It DOES NOT directly execute trades.
# ------------------------------------------------------------------
GEOMETRY_VERSION = "GEO3"
GEOMETRY_RESEARCH_ENABLED = True
GEOMETRY_HISTORY_CANDLES = 240
GEOMETRY_SWING_LOOKBACK = 2
GEOMETRY_CLUSTER_ATR_MULT = 0.35
GEOMETRY_CLUSTER_MIN_PCT = 0.035
GEOMETRY_NEAR_LEVEL_ATR_MULT = 0.60
GEOMETRY_MAX_LEVELS = 10

# v1.7 GEO2: extrema are clustered independently by scale so a dense
# H5 microstructure cannot swallow an H120 structural level.
GEOMETRY_LEVEL_SCALES = (15, 30, 60, 120, 240)
GEOMETRY_SCALE_LOOKBACK = {
    15: 1,
    30: 2,
    60: 3,
    120: 4,
    240: 5,
}
GEOMETRY_SCALE_ATR_TOL = {
    15: 0.12,
    30: 0.18,
    60: 0.24,
    120: 0.32,
    240: 0.42,
}
GEOMETRY_SCALE_MIN_PCT = {
    15: 0.003,
    30: 0.004,
    60: 0.006,
    120: 0.009,
    240: 0.012,
}
GEOMETRY_LEVELS_PER_SCALE = 5
GEOMETRY_BREAK_ATR_MULT = 0.18
GEOMETRY_RETEST_ATR_MULT = 0.28
GEOMETRY_CONFLUENCE_ATR_MULT = 0.45
GEOMETRY_PIVOT_ATR_MULT = 0.20

# v1.8 GEO3: nearby multiscale levels become one structural zone.
GEOMETRY_ZONE_MERGE_ATR_MULT = 0.38
GEOMETRY_ZONE_MERGE_MIN_PCT = 0.004
GEOMETRY_ZONE_HALF_ATR_MULT = 0.10
GEOMETRY_ZONE_ROLE_ATR_MULT = 0.12
GEOMETRY_ZONE_PRESSURE_DECAY_ATR = 0.85
GEOMETRY_ZONE_PRESSURE_THRESHOLD = 0.52
GEOMETRY_MAX_ZONES = 7

GEOMETRY_DASHBOARD_FILE = "storage/mor_geometry_dashboard.html"
GEOMETRY_SNAPSHOT_FILE = "storage/geometry_latest_geo3.json"
GEOMETRY_ASCII_WIDTH = 35
GEOMETRY_HTML_REFRESH_SECONDS = 25

# v1.5 visual future-cone layer. It does not influence execution.
CONE_VERSION = "CONE1"
CONE_HORIZONS = (5, 15, 30, 60, 120)
CONE_CENTER_X = 450
CONE_MAX_RX = 310.0
CONE_MIN_RX = 24.0

# ------------------------------------------------------------------
# v1.6 CONE2 — MOR CONE GEOMETRY MODEL.
#
# This is an observational 2D state-space proxy, NOT a literal oracle
# of future probability. For each horizon it builds a statistical
# ellipse in normalized (price displacement, velocity) coordinates.
#
# G_H = (mu, a, b, eccentricity, tilt, orientation, pressure,
#        area_change, spine_velocity, curvature)
#
# CONE2 remains RESEARCH ONLY. It feeds labels/transitions into
# COR2/COR3, but does not alter execution / Edge Gate / Risk Governor.
# ------------------------------------------------------------------
CONE_MODEL_VERSION = "CONE2.1"
CONE_MODEL_RESEARCH_ENABLED = True
CONE_TILT_THRESHOLD_DEG = 18.0
CONE_ECC_HIGH = 0.80
CONE_PRESSURE_HIGH = 0.78
CONE_AREA_LOG_THRESHOLD = 0.25

# v1.8 CONE2.1: bounded, reference-normalized area dynamics.
# This prevents tiny H5 ellipses from producing huge log-ratio pseudo-events.
CONE_AREA_REF_SEGMENTS = 4
CONE_AREA_FLOOR = 0.04
CONE_AREA_NORM_THRESHOLD = 0.24

CONE_ALIGN_DEG = 15.0
CONE_TWIST_HIGH_DEG = 40.0
CONE_CURVATURE_DEG = 12.0
CONE_SEQUENCE_MAX_MINUTES = 120.0
CONE_COV_REG = 1e-5
CONE_MODEL_STATES_FILE = "storage/cone_model_states_cone21.jsonl"
CONE_MODEL_EVENTS_FILE = "storage/cone_model_events_cone21.jsonl"

# Centerline displacement smaller than this fraction of ATR is treated
# as approximately stationary to reduce micro-jitter labels.
GEOMETRY_MOTION_ATR_FRACTION = 0.08

# ------------------------------------------------------------------
# v1.2.1 transport resilience.
# A websocket interruption must not terminate the experiment.
# ------------------------------------------------------------------
WS_RECONNECT_MIN_SECONDS = 2
WS_RECONNECT_MAX_SECONDS = 60
WS_RECONNECT_REFRESH_HISTORY = True

# Paper portfolio assumptions only — not live exchange fees.
INITIAL_USDT = 1000.0
PAPER_FEE_RATE = 0.0010      # 0.10% simulated fee
PAPER_SLIPPAGE_RATE = 0.0002 # 0.02% simulated slippage
TRADE_FRACTION = 0.10        # 10% of current equity per paper order
MAX_BTC_EXPOSURE = 0.25      # max 25% of equity in BTC
MAX_DRAWDOWN_PCT = 5.0       # block new BUYs at/above this paper DD
MIN_PAPER_NOTIONAL = 10.0

# ------------------------------------------------------------------
# v1.17 ERL1 — EXECUTION READINESS + BINANCE SPOT BRIDGE.
#
# Modes:
#   PAPER   = current simulator only (default)
#   TESTNET = Binance Spot Testnet MARKET orders, real API path / fake funds
#   LIVE    = real Binance Spot orders, hard opt-in + all research gates
#
# LIVE never bypasses Tradeability / Edge / Risk / ERL1. TESTNET can be
# optionally relaxed strictly for integration testing with fake funds.
# ------------------------------------------------------------------
EXECUTION_READINESS_VERSION = "ERL1"
EXECUTION_MODE = os.getenv("MOR_EXECUTION_MODE", "PAPER").strip().upper()
if EXECUTION_MODE not in ("PAPER", "TESTNET", "LIVE"):
    EXECUTION_MODE = "PAPER"

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "").strip()
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "").strip()
BINANCE_LIVE_BASE = "https://api.binance.com"
BINANCE_TESTNET_BASE = "https://testnet.binance.vision"
BINANCE_RECV_WINDOW_MS = min(5000, max(1000, int(os.getenv("MOR_RECV_WINDOW_MS", "5000"))))

# Explicit real-money arming phrase. This cannot be inferred or auto-enabled.
LIVE_ARM_PHRASE = "I_ACCEPT_REAL_MONEY_EXECUTION"
LIVE_ARMED = (os.getenv("MOR_LIVE_ARM", "").strip() == LIVE_ARM_PHRASE)

# Deliberately tiny default cap. User must explicitly override the env var.
EXCHANGE_MAX_NOTIONAL_USDT = max(1.0, float(os.getenv("MOR_MAX_ORDER_USDT", "10")))
EXCHANGE_BALANCE_RESERVE_FRACTION = min(0.20, max(0.0, float(os.getenv("MOR_BALANCE_RESERVE", "0.05"))))
EXCHANGE_ORDER_COOLDOWN_SECONDS = max(60, int(os.getenv("MOR_ORDER_COOLDOWN_SECONDS", "300")))
ERL1_MIN_SCORE = min(1.0, max(0.0, float(os.getenv("MOR_ERL1_MIN_SCORE", "0.72"))))
ERL1_MIN_FLAG_SAMPLES = max(10, int(os.getenv("MOR_ERL1_MIN_FLAG_SAMPLES", "20")))
ERL1_MIN_FLAG_RATE = min(1.0, max(0.0, float(os.getenv("MOR_ERL1_MIN_FLAG_RATE", "0.25"))))
ERL1_MIN_GEOMETRY_ALIGNMENT = min(1.0, max(0.0, float(os.getenv("MOR_ERL1_MIN_GEOMETRY_ALIGNMENT", "0.50"))))

# v1.17 GSR1 — Geometric Stability / Reversal layer.
# It separates continuation support from geometry-instability/reversal risk.
# GSR1 is an independent execution-readiness gate; it never creates a BUY/SELL
# signal by itself and it never bypasses Tradeability / Edge / exchange risk.
GEOMETRIC_STABILITY_REVERSAL_VERSION = "GSR1"
GSR1_MIN_CONTINUATION = min(1.0, max(0.0, float(os.getenv("MOR_GSR1_MIN_CONTINUATION", "0.42"))))
GSR1_MAX_REVERSAL_RISK = min(1.0, max(0.0, float(os.getenv("MOR_GSR1_MAX_REVERSAL", "0.55"))))
GSR1_TESTNET_MIN_CONTINUATION = min(1.0, max(0.0, float(os.getenv("MOR_GSR1_TESTNET_MIN_CONTINUATION", "0.20"))))
GSR1_TESTNET_MAX_REVERSAL_RISK = min(1.0, max(0.0, float(os.getenv("MOR_GSR1_TESTNET_MAX_REVERSAL", "0.72"))))
GSR1_PERSISTENCE_FULL_MINUTES = max(1.0, float(os.getenv("MOR_GSR1_PERSISTENCE_MINUTES", "5")))

# v1.17 EH1 — Execution Horizon Arbitration.
# EH1 scores H5/H15/H30/H60 for BUY and SELL. It may expose a research-only
# geometry preference while HOLD remains HOLD. For an existing BUY/SELL it may
# change only the frozen/execution horizon; it never invents a new direction.
EXECUTION_HORIZON_ARBITRATION_VERSION = "EH1"
EH1_MIN_CANDIDATE_SCORE = min(1.0, max(0.0, float(os.getenv("MOR_EH1_MIN_CANDIDATE_SCORE", "0.42"))))
EH1_MIN_CANDIDATE_COST_COVERAGE = max(0.0, float(os.getenv("MOR_EH1_MIN_CANDIDATE_COST_COVERAGE", "0.45")))
EH1_READY_SCORE = min(1.0, max(0.0, float(os.getenv("MOR_EH1_READY_SCORE", "0.65"))))
EH1_READY_COST_COVERAGE = max(0.0, float(os.getenv("MOR_EH1_READY_COST_COVERAGE", "1.00")))
EH1_LOCAL_TILT_FULL_DEG = max(10.0, float(os.getenv("MOR_EH1_LOCAL_TILT_FULL_DEG", "60")))

# v1.18 PFL1 — Phase Front Lag / propagation learner.
# Tracks how tilt-state changes travel through scale-space (H5→H120),
# preserving lag, direction and log-horizon velocity. It is research-only:
# it can support/oppose EH1 readiness but never creates BUY/SELL by itself.
PHASE_FRONT_LAG_VERSION = "PFL1"
PFL1_HORIZONS = (5, 15, 30, 60, 120)
PFL1_HISTORY = max(60, int(os.getenv("MOR_PFL1_HISTORY", "240")))
PFL1_LINK_WINDOW_MIN = max(5.0, float(os.getenv("MOR_PFL1_LINK_WINDOW_MIN", "30")))
PFL1_STALE_MIN = max(5.0, float(os.getenv("MOR_PFL1_STALE_MIN", "20")))
PFL1_STRONG = min(1.0, max(0.0, float(os.getenv("MOR_PFL1_STRONG", "0.60"))))
PFL1_CONFLICT_BLOCK = min(1.0, max(0.0, float(os.getenv("MOR_PFL1_CONFLICT_BLOCK", "0.68"))))

# v1.19 EFS1 — Economic Front Surface.
# Research-only layer over the EH1 horizon rows. It maps the estimated
# motion-budget / execution-cost coverage across H5/H15/H30/H60, tracks
# the peak horizon and its scale drift, and never creates or bypasses trades.
ECONOMIC_FRONT_SURFACE_VERSION = "EFS1"
EFS1_HORIZONS = SHADOW_HORIZONS
EFS1_HISTORY = max(60, int(os.getenv("MOR_EFS1_HISTORY", "240")))
EFS1_NEAR_COST = max(0.0, float(os.getenv("MOR_EFS1_NEAR_COST", "0.50")))
EFS1_COVERED = max(EFS1_NEAR_COST, float(os.getenv("MOR_EFS1_COVERED", "1.00")))


# ------------------------------------------------------------------
# v1.21 GDX1 — Geometry eXperimental TESTNET Direction Bridge.
#
# EH1/GSR1 already expose a research-only geometric BUY/SELL preference when
# the legacy strategy says HOLD. GDX1 may convert that preference into an
# explicitly armed TESTNET integration action. It can never originate LIVE.
# ------------------------------------------------------------------
GEOMETRY_TESTNET_BRIDGE_VERSION = "GDX1"
TESTNET_GEOMETRY_ACTIONS = os.getenv("MOR_TESTNET_GEOMETRY_ACTIONS", "0").strip() == "1"
GDX1_MIN_SCORE = min(1.0, max(0.0, float(os.getenv("MOR_GDX1_MIN_SCORE", "0.42"))))
GDX1_MIN_COST_COVERAGE = max(0.0, float(os.getenv("MOR_GDX1_MIN_COST_COVERAGE", "0.40")))
GDX1_MIN_LOCAL_GEOMETRY = min(1.0, max(0.0, float(os.getenv("MOR_GDX1_MIN_LOCAL_GEOMETRY", "0.60"))))
GDX1_MIN_GSR_QUALITY = min(1.0, max(0.0, float(os.getenv("MOR_GDX1_MIN_GSR_QUALITY", "0.50"))))
GDX1_MIN_PFL_SCORE = min(1.0, max(0.0, float(os.getenv("MOR_GDX1_MIN_PFL_SCORE", "0.50"))))
GDX1_REQUIRE_EFS_MATCH = os.getenv("MOR_GDX1_REQUIRE_EFS_MATCH", "1").strip() != "0"

# ------------------------------------------------------------------
# v1.21 CGE1 + AAL1
#
# CGE1 estimates *conditional geometric outcome evidence* from the existing
# GOL2 matrix. It is deliberately not treated as a guaranteed net-return
# estimator. AAL1 arbitrates between the legacy strategy direction and the
# geometric direction. Geometry may veto a conflicting strategy action in
# every execution mode, but an opposite-direction geometry action can be
# originated only in explicitly armed relaxed TESTNET mode. LIVE can never
# be reversed by AAL1.
# ------------------------------------------------------------------
CONDITIONAL_GEOMETRY_EDGE_VERSION = "CGE1"
ACTION_ARBITRATION_VERSION = "AAL1"
TESTNET_ACTION_ARBITRATION = os.getenv("MOR_TESTNET_ACTION_ARBITRATION", "0").strip() == "1"
CGE1_MIN_CELL_SAMPLES = max(3, int(os.getenv("MOR_CGE1_MIN_CELL_SAMPLES", "8")))
CGE1_MIN_MATCHED_CELLS = max(2, int(os.getenv("MOR_CGE1_MIN_MATCHED_CELLS", "3")))
CGE1_MIN_EFFECTIVE_WEIGHT = max(1.0, float(os.getenv("MOR_CGE1_MIN_EFFECTIVE_WEIGHT", "14")))
CGE1_SUPPORT_SCORE = min(1.0, max(0.0, float(os.getenv("MOR_CGE1_SUPPORT_SCORE", "0.56"))))
CGE1_OPPOSE_SCORE = min(CGE1_SUPPORT_SCORE, max(0.0, float(os.getenv("MOR_CGE1_OPPOSE_SCORE", "0.44"))))
AAL1_STRONG_GEOMETRY_SCORE = min(1.0, max(0.0, float(os.getenv("MOR_AAL1_STRONG_GEOMETRY_SCORE", "0.55"))))
AAL1_STRONG_GSR_QUALITY = min(1.0, max(0.0, float(os.getenv("MOR_AAL1_STRONG_GSR_QUALITY", "0.70"))))
AAL1_STRONG_LOCAL_GEOMETRY = min(1.0, max(0.0, float(os.getenv("MOR_AAL1_STRONG_LOCAL_GEOMETRY", "0.55"))))
AAL1_STRONG_COST_COVERAGE = max(0.0, float(os.getenv("MOR_AAL1_STRONG_COST_COVERAGE", "0.75")))
AAL1_OVERRIDE_COST_COVERAGE = max(AAL1_STRONG_COST_COVERAGE, float(os.getenv("MOR_AAL1_OVERRIDE_COST_COVERAGE", "1.00")))
AAL1_OVERRIDE_GSR_QUALITY = min(1.0, max(AAL1_STRONG_GSR_QUALITY, float(os.getenv("MOR_AAL1_OVERRIDE_GSR_QUALITY", "0.78"))))
AAL1_REQUIRE_VALIDATED_EDGE = os.getenv("MOR_AAL1_REQUIRE_VALIDATED_EDGE", "1").strip() != "0"
AAL1_REQUIRE_CGE_SUPPORT = os.getenv("MOR_AAL1_REQUIRE_CGE_SUPPORT", "1").strip() != "0"

# ------------------------------------------------------------------
# v1.23 BPM1 — Bipolar Pressure Model.
#
# Projects the current multi-module geometry into a common signed field
# q in [-1,1] while keeping activity/intensity I separate. The resulting
# structural push is P=q*I. A high-I / q≈0 state is explicitly represented
# as tense balance rather than "nothing". BPM1 is research-only in v1.23:
# it logs continuous q trajectories, derivatives and sign crossings but
# cannot create, reverse or bypass an order.
# ------------------------------------------------------------------
BIPOLAR_PRESSURE_VERSION = "BPM1"
BPM1_HORIZONS = (5, 15, 30, 60, 120)
BPM1_HISTORY = max(120, int(os.getenv("MOR_BPM1_HISTORY", "480")))
BPM1_ZERO_DEADBAND = min(0.30, max(0.01, float(os.getenv("MOR_BPM1_ZERO_DEADBAND", "0.08"))))
BPM1_TILT_FULL_DEG = max(10.0, float(os.getenv("MOR_BPM1_TILT_FULL_DEG", "60")))
BPM1_OMEGA_FULL_DEG_PER_MIN = max(5.0, float(os.getenv("MOR_BPM1_OMEGA_FULL", "30")))
BPM1_CROSS_FULL_Q_PER_MIN = max(0.02, float(os.getenv("MOR_BPM1_CROSS_FULL_SPEED", "0.25")))
BPM1_EFS_FULL_COVERAGE = max(0.25, float(os.getenv("MOR_BPM1_EFS_FULL_COVERAGE", "1.00")))
BPM1_STABLE_BALANCE_I = min(1.0, max(0.0, float(os.getenv("MOR_BPM1_TENSE_BALANCE_I", "0.55"))))
BPM1_STATES_FILE = "storage/bipolar_pressure_bpm1.jsonl"

# ------------------------------------------------------------------
# v1.22 SCR1 + GAP1 + GRC1 + RES1 — SESSION GAP LAB.
#
# A process/session gap is NOT silently interpolated as fact. Before any
# missing market candles are fetched, GAP1 freezes a blind probabilistic
# trajectory plane from the last known state and prior learned residuals.
# Only after that freeze may SCR1 fetch/replay the missing closed candles in
# an isolated research plane with execution disabled. GRC1 compares the
# frozen hypothesis plane with the observed geometry; RES1 stores the model's
# own systematic residuals for future GAP forecasts. None of these modules
# can originate or bypass an order.
# ------------------------------------------------------------------
SESSION_CONTINUITY_VERSION = "SCR1"
GAP_HYPOTHESIS_VERSION = "GAP1"
GAP_RECONCILIATION_VERSION = "GRC1"
MODEL_RESIDUAL_VERSION = "RES1"
GAP_MIN_MINUTES = max(2, int(os.getenv("MOR_GAP_MIN_MINUTES", "3")))
GAP_MAX_MINUTES = max(GAP_MIN_MINUTES, int(os.getenv("MOR_GAP_MAX_MINUTES", "10080")))
GAP_REPLAY_PROGRESS_EVERY = max(30, int(os.getenv("MOR_GAP_PROGRESS_EVERY", "60")))
GAP_RESIDUAL_ALPHA = min(1.0, max(0.01, float(os.getenv("MOR_GAP_RESIDUAL_ALPHA", "0.15"))))
GAP_RESIDUAL_DIRECTION_GAIN = min(0.50, max(0.0, float(os.getenv("MOR_GAP_RESIDUAL_GAIN", "0.20"))))
GAP_FORECASTS_FILE = "storage/gap_forecasts_gap1.jsonl"
GAP_OBSERVED_FILE = "storage/gap_observed_scr1.jsonl"
GAP_RECONCILIATIONS_FILE = "storage/gap_reconciliation_grc1.jsonl"
MODEL_RESIDUALS_FILE = "storage/model_residuals_res1.jsonl"
GAP_DETAIL_DIR = "storage/gaps"
os.makedirs(GAP_DETAIL_DIR, exist_ok=True)

# v1.19 MORX1 — analysis export. No API key/secret or auth headers are
# included. A compact snapshot is refreshed every closed candle; --export
# creates a larger one-file research dump suitable for upload/analysis.
ANALYSIS_EXPORT_VERSION = "MORX1"
ANALYSIS_EXPORT_FILE = "storage/mor_analysis_export_latest.json"
ANALYSIS_EXPORT_HISTORY_FILE = "storage/mor_analysis_export_history.jsonl"
ANALYSIS_EXPORT_DOWNLOAD_FILE = os.getenv(
    "MOR_EXPORT_DOWNLOAD_PATH", "/sdcard/Download/MOR_latest_export.json"
)
ANALYSIS_EXPORT_FULL_DOWNLOAD_FILE = os.getenv(
    "MOR_EXPORT_FULL_DOWNLOAD_PATH", "/sdcard/Download/MOR_export_full.json"
)
ANALYSIS_EXPORT_RECENT = max(10, int(os.getenv("MOR_EXPORT_RECENT", "80")))
ANALYSIS_EXPORT_FULL_RECENT = max(100, int(os.getenv("MOR_EXPORT_FULL_RECENT", "500")))

TESTNET_RELAX_GATES = os.getenv("MOR_TESTNET_RELAX_GATES", "0").strip() == "1"
EXCHANGE_PREFLIGHT_ORDER_TEST = os.getenv("MOR_PREFLIGHT_ORDER_TEST", "0").strip() == "1"

WS_URL = os.getenv(
    "MOR_MARKET_WS_URL",
    "wss://data-stream.binance.vision/ws/btcusdt@kline_1m",
).strip()
REST_BASE = os.getenv(
    "MOR_MARKET_REST_URL",
    "https://data-api.binance.vision/api/v3/klines",
).strip()

os.makedirs("storage", exist_ok=True)

RUNTIME_FILE = "storage/runtime_state.json"
STATES_FILE = "storage/states.jsonl"
PREDICTIONS_FILE = "storage/predictions.jsonl"
FACTS_FILE = "storage/facts.jsonl"
SHADOW_FILE = "storage/shadow_facts.jsonl"
PAPER_TRADES_FILE = "storage/paper_trades.jsonl"
PORTFOLIO_FILE = "storage/portfolio.jsonl"
EXCHANGE_TRADES_FILE = "storage/exchange_trades_erl1.jsonl"
EXECUTION_STATE_FILE = "storage/execution_state_erl1.json"
HORIZON_MATRIX_FILE = "storage/horizon_matrix.json"
SURFACE_MATRIX_FILE = "storage/parameter_surface.json"
SURFACE_FACTS_FILE = "storage/parameter_surface_facts.jsonl"
STATE_FEATURES_FILE = "storage/state_features_sr1.jsonl"
TRADEABILITY_FACTS_FILE = "storage/tradeability_facts_sr1.jsonl"
TRADEABILITY_MATRIX_FILE = "storage/tradeability_matrix_sr1.json"
CORRIDOR_STATES_FILE = "storage/corridor_states_cor1.jsonl"
CORRIDOR_FACTS_FILE = "storage/corridor_facts_cor1.jsonl"
CORRIDOR_MATRIX_FILE = "storage/corridor_matrix_cor1.json"
CORRIDOR2_STATES_FILE = "storage/corridor_states_cor2.jsonl"
CORRIDOR2_FACTS_FILE = "storage/corridor_facts_cor2.jsonl"
CORRIDOR2_MATRIX_FILE = "storage/corridor_multilabel_cor2.json"
CORRIDOR3_STATES_FILE = "storage/corridor_scale_states_cor3.jsonl"
CORRIDOR3_FACTS_FILE = "storage/corridor_scale_facts_cor3.jsonl"
CORRIDOR3_MATRIX_FILE = "storage/corridor_scale_matrix_cor3.json"
GEOMETRY_STATES_FILE = "storage/geometry_states_geo3.jsonl"
GEOMETRY_EVENTS_FILE = "storage/geometry_events_geo3.jsonl"
PHASE_FRONT_STATES_FILE = "storage/phase_front_states_pfl1.jsonl"
ECONOMIC_FRONT_STATES_FILE = "storage/economic_front_states_efs1.jsonl"

# ------------------------------------------------------------------
# v1.9 GOL1 — Geometric Outcome Learner.
#
# RESEARCH ONLY.
# It freezes the current cone/zone geometry BEFORE future candles are
# known, then observes the complete path over H5/H15/H30/H60:
# terminal return, max-up, max-down, MFE/MAE and net-after-cost outcome.
#
# No GOL1 statistic is connected to execution, Edge Gate or Risk Governor.
# ------------------------------------------------------------------
GEOMETRY_OUTCOME_VERSION = "GOL2"
GEOMETRY_OUTCOME_RESEARCH_ENABLED = True
GEOMETRY_OUTCOME_HORIZONS = (5, 15, 30, 60)
GEOMETRY_OUTCOME_SUMMARY_EVERY_STATES = 10
GEOMETRY_OUTCOME_MIN_LEADER_SAMPLES = 8

# v1.9.1 — a structural zone may exist without being locally relevant.
GOL_ZONE_NEAR_PRESSURE = 0.30
GOL_ZONE_ACTIVE_PRESSURE = 0.60

# v1.10 CTD1 — temporal geometry of cone slices.
CONE_DYNAMICS_VERSION = "CTD1"
CONE_DYNAMICS_RESEARCH_ENABLED = True
CONE_DYNAMICS_STATE_DEADZONE_DEG = 12.0
CONE_DYNAMICS_ROT_MED_DEG_PER_MIN = 4.0
CONE_DYNAMICS_ROT_FAST_DEG_PER_MIN = 12.0
CONE_DYNAMICS_ACCEL_FAST_DEG_PER_MIN2 = 8.0
CONE_DYNAMICS_PROPAGATION_WINDOW_MIN = 15.0
CONE_DYNAMICS_EVENT_HISTORY = 80

# v1.11 TOM1 — outcome learning for deformation routes through scale-space.
# It learns paths such as H5↑→H15↑→H30↑ separately from the static cone.
TRANSITION_OUTCOME_VERSION = "TOM1"
TRANSITION_OUTCOME_RESEARCH_ENABLED = True
TRANSITION_OUTCOME_MIN_LEADER_SAMPLES = 5
TRANSITION_OUTCOME_MATRIX_FILE = "storage/transition_outcome_matrix_tom1.json"

# v1.12 TEG1 — graph of transition EDGES rather than destination states only.
# Node = scale-specific state transition, e.g. H15:UP->FLAT.
# Edge = temporal relation between two node events in log-horizon scale-space.
TRANSITION_EDGE_VERSION = "TEG1"
TRANSITION_EDGE_RESEARCH_ENABLED = True
TRANSITION_EDGE_MIN_LEADER_SAMPLES = 5
TRANSITION_EDGE_MATRIX_FILE = "storage/transition_edge_matrix_teg1.json"
TRANSITION_EDGE_SLOW_LOG2_PER_MIN = 0.35
TRANSITION_EDGE_FAST_LOG2_PER_MIN = 1.00

# v1.13 SPT1 — scale-space phase topology.
# Contiguous UP/DOWN scales are tracked as domains with moving boundaries.
PHASE_TOPOLOGY_VERSION = "SPT2"
PHASE_TOPOLOGY_RESEARCH_ENABLED = True
PHASE_TOPOLOGY_EVENT_HISTORY = 160

# v1.14 PBD1 — explicit phase-boundary dynamics inside scale-space.
PHASE_BOUNDARY_VERSION = "PBD1"
PHASE_BOUNDARY_MOVE_EPS = 1e-9
PHASE_BOUNDARY_EVENT_HISTORY = 160

# GOL2 keeps the strict executable-after-cost label, but also learns
# gross/latent future motion so sub-cost geometry is not collapsed to zero.
GOL2_GROSS_FLAT_EPS_PCT = 1e-9
GOL2_COST_COVERAGE_NEAR = 0.50

GEOMETRY_OUTCOME_STATES_FILE = "storage/geometry_outcome_states_gol2.jsonl"
GEOMETRY_OUTCOME_FACTS_FILE = "storage/geometry_outcome_facts_gol2.jsonl"
GEOMETRY_OUTCOME_MATRIX_FILE = "storage/geometry_outcome_matrix_gol2.json"

candles = deque(maxlen=max(FEATURE_HISTORY_CANDLES, GEOMETRY_HISTORY_CANDLES))

state_id = 0
candle_seq = 0
pending_predictions = []
pending_shadows = []
stats = {}

shadow_metrics = {
    "resolved": 0,
    "directional_hits": 0,
    "economic_evaluable": 0,
    "economic_hits": 0,
}

# Matrix key: REGIME|STRATEGY|HORIZON
# It learns observed net edge of each strategy policy at each horizon.
horizon_matrix = {}

# System-level metrics are separated by horizon so that 5m/15m/30m/60m
# can be compared without mixing their opportunity structure.
horizon_system_metrics = {}

adaptive_metrics = {
    "signals_seen": 0,
    "edge_gate_blocked_unvalidated": 0,
    "edge_gate_blocked_nonpositive": 0,
    "edge_gate_allowed": 0,
    "adaptive_horizon_used": 0,
}

# v0.9 research state. This does NOT feed the production edge gate.
pending_surface_shadows = []
parameter_surface = {}
surface_metrics = {
    "resolved_probes": 0,
    "policy_evaluations": 0,
    "active_signal_evaluations": 0,
    "eligible_trade_evaluations": 0,
    "positive_trade_evaluations": 0,
}

# v1.0 SR1 tradeability research.
pending_tradeability_probes = []

tradeability_metrics = {
    str(h): {
        "horizon_candles": int(h),
        "samples": 0,
        "predicted_positive": 0,
        "observed_market_positive": 0,
        "tp": 0,
        "fp": 0,
        "tn": 0,
        "fn": 0,
        "brier_sum": 0.0,
        "abs_move_sum_pct": 0.0,
        "best_net_sum_pct": 0.0,
    }
    for h in TRADEABILITY_HORIZONS
}

tradeability_feature_matrix = {}

# v1.1 parallel corridor research state.
pending_corridor_probes = []

corridor_metrics = {
    str(h): {
        "horizon_candles": int(h),
        "samples": 0,
        "predicted_positive": 0,
        "observed_market_positive": 0,
        "tp": 0,
        "fp": 0,
        "tn": 0,
        "fn": 0,
        "brier_sum": 0.0,
        "best_net_sum_pct": 0.0,
        "abs_move_sum_pct": 0.0,
    }
    for h in CORRIDOR_HORIZONS
}

corridor_feature_matrix = {}

corridor_compare = {
    str(h): {
        "samples": 0,
        "both_correct": 0,
        "both_wrong": 0,
        "sr1_only_correct": 0,
        "corridor_only_correct": 0,
        "disagreements": 0,
    }
    for h in CORRIDOR_HORIZONS
}

# v1.2 multi-label research.
pending_corridor2_probes = []

# Matrix contains FLAG:, TRANS:, and SIG: cells, separated by horizon.
corridor2_feature_matrix = {}

corridor2_metrics = {
    str(h): {
        "horizon_candles": int(h),
        "samples": 0,
        "observed_market_positive": 0,
        "best_net_sum_pct": 0.0,
        "active_flag_sum": 0,
        "active_transition_sum": 0,
    }
    for h in CORRIDOR_HORIZONS
}

# Previous multi-label state is needed to freeze actual transitions.
last_corridor2_flags = {}

# v1.3 scale/age research.
pending_corridor3_probes = []

corridor3_feature_matrix = {}

corridor3_metrics = {
    str(h): {
        "horizon_candles": int(h),
        "samples": 0,
        "observed_market_positive": 0,
        "best_net_sum_pct": 0.0,
        "structural_magnitude_sum": 0.0,
        "economic_magnitude_sum": 0.0,
        "signature_age_sum_minutes": 0.0,
        "transition_age_sum_minutes": 0.0,
    }
    for h in CORRIDOR_HORIZONS
}

# Timestamp-based persistent dwell tracker.
corridor3_tracker = {
    "flag_onsets_ms": {},
    "signature": None,
    "signature_onset_ms": None,
    "last_transition_ms": None,
    "last_transition_names": [],
}

geometry_tracker = {
    "flags": {},
    "last_signature": None,
    "last_close_time_ms": None,
    "last_level_ids": [],
}

cone_tracker = {
    "flags": {},
    "last_signature": None,
    "last_close_time_ms": None,
    "sequence": {},
}

cone_transition_tracker = {
    "last_close_time_ms": None,
    "horizons": {},
    "events": [],
    "last_signature": "CTD_BASE",
}

# v1.9 frozen geometry -> future path learner.
pending_geometry_outcome_probes = []

geometry_outcome_matrix = {}

# TOM1 is separate from GOL1 so route statistics cannot be confused with
# static/low-dimensional geometry cells.
transition_outcome_matrix = {}
transition_edge_matrix = {}

phase_topology_tracker = {
    "last_close_time_ms": None,
    "domains": [],
    "boundaries": [],
    "events": [],
    "boundary_events": [],
    "next_domain_id": 1,
    "next_boundary_id": 1,
    "last_topology_class": "UNOBSERVED",
    "last_signature": "SPT_BASE",
}

phase_front_tracker = {
    "last_close_time_ms": None,
    "samples": [],
    "events": [],
    "front_links": [],
    "next_event_id": 1,
    "last_pattern": "F-F-F-F-F",
}

economic_front_tracker = {
    "last_close_time_ms": None,
    "last_peak_horizon": None,
    "last_peak_coverage": 0.0,
    "history": [],
}


bipolar_pressure_tracker = {
    "version": BIPOLAR_PRESSURE_VERSION,
    "last_close_time_ms": None,
    "by_horizon": {
        str(h): {
            "q": 0.0, "dq_per_min": 0.0, "last_nonzero_sign": 0,
            "sign_age_minutes": 0.0,
        } for h in BPM1_HORIZONS
    },
    "global": {
        "q": 0.0, "dq_per_min": 0.0, "last_nonzero_sign": 0,
        "sign_age_minutes": 0.0,
    },
    "history": [],
    "cross_count": 0,
}


session_continuity_tracker = {
    "version": SESSION_CONTINUITY_VERSION,
    "current_session_id": None,
    "session_count": 0,
    "gaps_detected": 0,
    "gaps_completed": 0,
    "replayed_minutes": 0,
    "last_gap_id": None,
    "last_gap": None,
    "pending_gap": None,
}

model_residual_tracker = {
    "version": MODEL_RESIDUAL_VERSION,
    "comparisons": 0,
    "ema_total_error": 0.0,
    "ema_pattern_error": 0.0,
    "ema_direction_error": 0.0,
    "reliability": 1.0,
    "bias_by_horizon": {str(h): 0.0 for h in (5, 15, 30, 60, 120)},
    "last_gap_id": None,
}

latest_gap_forecast = {
    "version": GAP_HYPOTHESIS_VERSION,
    "ready": False,
    "status": "NO_GAP",
}
latest_gap_reconciliation = {
    "version": GAP_RECONCILIATION_VERSION,
    "ready": False,
    "status": "NO_GAP",
}
latest_model_residual = dict(model_residual_tracker)

geometry_outcome_metrics = {
    str(h): {
        "horizon_candles": int(h),
        "samples": 0,
        "path_tradeable": 0,
        "terminal_tradeable": 0,
        "up_path_wins": 0,
        "down_path_wins": 0,
        "flat_path_wins": 0,
        "max_up_sum_pct": 0.0,
        "max_down_abs_sum_pct": 0.0,
        "buy_mfe_net_sum_pct": 0.0,
        "sell_mfe_net_sum_pct": 0.0,
        "best_mfe_net_sum_pct": 0.0,
        "terminal_best_net_sum_pct": 0.0,
        "gross_up_wins": 0,
        "gross_down_wins": 0,
        "gross_flat_wins": 0,
        "gross_cost_covered": 0,
        "gross_near_cost": 0,
        "gross_best_excursion_sum_pct": 0.0,
        "cost_coverage_ratio_sum": 0.0,
        "path_asymmetry_sum": 0.0,
    }
    for h in GEOMETRY_OUTCOME_HORIZONS
}

last_close_time_ms = 0

portfolio = {
    "usdt": INITIAL_USDT,
    "btc": 0.0,
    "btc_cost_basis_usdt": 0.0,
    "realized_pnl_usdt": 0.0,
    "total_fees_usdt": 0.0,
    "paper_trades": 0,
    "peak_equity_usdt": INITIAL_USDT,
    "max_drawdown_pct": 0.0,
}

execution_runtime = {
    "preflight_ok": EXECUTION_MODE == "PAPER",
    "preflight_reason": "PAPER_MODE" if EXECUTION_MODE == "PAPER" else "NOT_CHECKED",
    "server_time_offset_ms": 0,
    "last_order_epoch_ms": 0,
    "last_order_id": None,
    "orders_sent": 0,
}

latest_geometric_stability_reversal = {
    "version": GEOMETRIC_STABILITY_REVERSAL_VERSION,
    "ready": False,
    "action": "HOLD",
    "continuation_index": 0.0,
    "reversal_index": 0.0,
    "verdict": "WARMUP",
    "counterfactual": {},
    "geometry_preferred_action": "NONE",
}

latest_execution_horizon_arbitration = {
    "version": EXECUTION_HORIZON_ARBITRATION_VERSION,
    "ready": False,
    "selected_action": "NONE",
    "selected_horizon": TRADE_HORIZON,
    "selected_score": 0.0,
    "status": "WARMUP",
    "rows": [],
}

latest_phase_front_lag = {
    "version": PHASE_FRONT_LAG_VERSION,
    "ready": False,
    "state": "WARMUP",
    "front_direction": "NONE",
    "propagation_mode": "NONE",
    "strength": 0.0,
    "sequence": [],
}

latest_economic_front_surface = {
    "version": ECONOMIC_FRONT_SURFACE_VERSION,
    "ready": False,
    "state": "WARMUP",
    "peak_horizon": None,
    "peak_cost_coverage": 0.0,
    "peak_direction": "NONE",
    "peak_drift": "NONE",
    "rows": [],
}

latest_bipolar_pressure = {
    "version": BIPOLAR_PRESSURE_VERSION,
    "ready": False,
    "state": "WARMUP",
    "q": 0.0,
    "I": 0.0,
    "P": 0.0,
    "tension": 0.0,
    "horizons": {},
    "crossings": [],
}

latest_geometry_testnet_bridge = {
    "version": GEOMETRY_TESTNET_BRIDGE_VERSION,
    "ready": False,
    "allowed": False,
    "action": "HOLD",
    "horizon": TRADE_HORIZON,
    "score": 0.0,
    "cost_coverage": 0.0,
    "blockers": ["WARMUP"],
}

latest_conditional_geometry_edge = {
    "version": CONDITIONAL_GEOMETRY_EDGE_VERSION,
    "ready": False,
    "selected_action": "NONE",
    "selected_horizon": TRADE_HORIZON,
    "status": "WARMUP",
    "rows": [],
}

latest_action_arbitration = {
    "version": ACTION_ARBITRATION_VERSION,
    "ready": False,
    "strategy_action": "HOLD",
    "geometry_action": "NONE",
    "final_action": "HOLD",
    "final_source": "NONE",
    "status": "WARMUP",
    "blockers": ["WARMUP"],
}

latest_execution_readiness = {
    "version": EXECUTION_READINESS_VERSION,
    "mode": EXECUTION_MODE,
    "ready": False,
    "score": 0.0,
    "action": "HOLD",
    "blockers": ["WARMUP"],
}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def append_jsonl(filename, obj):
    with open(filename, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def save_runtime():
    payload = {
        "version": "1.23",
        "saved_at": now_iso(),
        "state_id": state_id,
        "candle_seq": candle_seq,
        "pending_predictions": pending_predictions,
        "pending_shadows": pending_shadows,
        "stats": stats,
        "shadow_metrics": shadow_metrics,
        "horizon_matrix": horizon_matrix,
        "horizon_system_metrics": horizon_system_metrics,
        "adaptive_metrics": adaptive_metrics,
        "pending_surface_shadows": pending_surface_shadows,
        "parameter_surface": parameter_surface,
        "surface_metrics": surface_metrics,
        "pending_tradeability_probes": pending_tradeability_probes,
        "tradeability_metrics": tradeability_metrics,
        "tradeability_feature_matrix": tradeability_feature_matrix,
        "pending_corridor_probes": pending_corridor_probes,
        "corridor_metrics": corridor_metrics,
        "corridor_feature_matrix": corridor_feature_matrix,
        "corridor_compare": corridor_compare,
        "pending_corridor2_probes": pending_corridor2_probes,
        "corridor2_feature_matrix": corridor2_feature_matrix,
        "corridor2_metrics": corridor2_metrics,
        "last_corridor2_flags": last_corridor2_flags,
        "pending_corridor3_probes": pending_corridor3_probes,
        "corridor3_feature_matrix": corridor3_feature_matrix,
        "corridor3_metrics": corridor3_metrics,
        "corridor3_tracker": corridor3_tracker,
        "geometry_tracker": geometry_tracker,
        "cone_tracker": cone_tracker,
        "cone_transition_tracker": cone_transition_tracker,
        "pending_geometry_outcome_probes": pending_geometry_outcome_probes,
        "geometry_outcome_matrix": geometry_outcome_matrix,
        "geometry_outcome_metrics": geometry_outcome_metrics,
        "transition_outcome_matrix": transition_outcome_matrix,
        "transition_edge_matrix": transition_edge_matrix,
        "phase_topology_tracker": phase_topology_tracker,
        "phase_front_tracker": phase_front_tracker,
        "economic_front_tracker": economic_front_tracker,
        "bipolar_pressure_tracker": bipolar_pressure_tracker,
        "session_continuity_tracker": session_continuity_tracker,
        "model_residual_tracker": model_residual_tracker,
        "last_close_time_ms": last_close_time_ms,
        "portfolio": portfolio,
    }

    tmp = RUNTIME_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    os.replace(tmp, RUNTIME_FILE)
    save_horizon_matrix_snapshot()
    save_parameter_surface_snapshot()
    save_tradeability_snapshot()
    save_corridor_snapshot()
    save_corridor2_snapshot()
    save_corridor3_snapshot()
    save_geometry_outcome_snapshot()
    save_transition_outcome_snapshot()
    save_transition_edge_snapshot()


def save_transition_edge_snapshot():
    payload = {
        "version": "1.23",
        "learner": TRANSITION_EDGE_VERSION,
        "saved_at": now_iso(),
        "symbol": SYMBOL,
        "research_only": True,
        "matrix": transition_edge_matrix,
    }

    tmp = TRANSITION_EDGE_MATRIX_FILE + ".tmp"

    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(
            payload,
            f,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )

    os.replace(
        tmp,
        TRANSITION_EDGE_MATRIX_FILE,
    )


def save_transition_outcome_snapshot():
    payload = {
        "version": "1.23",
        "learner": TRANSITION_OUTCOME_VERSION,
        "saved_at": now_iso(),
        "symbol": SYMBOL,
        "research_only": True,
        "matrix": transition_outcome_matrix,
    }

    tmp = TRANSITION_OUTCOME_MATRIX_FILE + ".tmp"

    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(
            payload,
            f,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )

    os.replace(
        tmp,
        TRANSITION_OUTCOME_MATRIX_FILE,
    )


def save_geometry_outcome_snapshot():
    payload = {
        "version": "1.23",
        "learner": GEOMETRY_OUTCOME_VERSION,
        "saved_at": now_iso(),
        "symbol": SYMBOL,
        "research_only": True,
        "horizons": list(GEOMETRY_OUTCOME_HORIZONS),
        "pending": len(pending_geometry_outcome_probes),
        "metrics": geometry_outcome_metrics,
        "matrix": geometry_outcome_matrix,
    }

    tmp = GEOMETRY_OUTCOME_MATRIX_FILE + ".tmp"

    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(
            payload,
            f,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )

    os.replace(
        tmp,
        GEOMETRY_OUTCOME_MATRIX_FILE,
    )


def save_horizon_matrix_snapshot():
    payload = {
        "version": "1.23",
        "saved_at": now_iso(),
        "symbol": SYMBOL,
        "shadow_horizons": list(SHADOW_HORIZONS),
        "min_adaptive_trade_samples": MIN_ADAPTIVE_TRADE_SAMPLES,
        "matrix": horizon_matrix,
        "system_metrics_by_horizon": horizon_system_metrics,
        "adaptive_metrics": adaptive_metrics,
    }

    tmp = HORIZON_MATRIX_FILE + ".tmp"

    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(
            payload,
            f,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )

    os.replace(tmp, HORIZON_MATRIX_FILE)



def surface_grid_definition():
    return {
        "version": SURFACE_GRID_VERSION,
        "trend_thresholds_pct": list(
            SURFACE_TREND_THRESHOLDS_PCT
        ),
        "breakout_vol_gates_pct": list(
            SURFACE_BREAKOUT_VOL_GATES_PCT
        ),
        "horizons": list(SHADOW_HORIZONS),
        "strategies": [
            "MEAN_REVERSION",
            "MOMENTUM",
            "BREAKOUT",
        ],
        "paper_fee_rate": PAPER_FEE_RATE,
        "paper_slippage_rate":
            PAPER_SLIPPAGE_RATE,
    }


def surface_grid_hash():
    raw = json.dumps(
        surface_grid_definition(),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()

    return hashlib.sha256(raw).hexdigest()


def surface_leaderboard(limit=10):
    candidates = []

    for key, cell in parameter_surface.items():
        samples = int(
            cell.get("samples", 0)
        )

        trade_n = int(
            cell.get(
                "eligible_trade_samples",
                0,
            )
        )

        if (
            samples
            < SURFACE_MIN_POLICY_SAMPLES
            or trade_n
            < SURFACE_MIN_ELIGIBLE_TRADE_SAMPLES
        ):
            continue

        edge = cell.get(
            "avg_trade_net_edge_pct"
        )

        if edge is None:
            continue

        candidates.append(
            (
                float(edge),
                float(
                    cell.get(
                        "trade_positive_rate",
                        0.0,
                    )
                    or 0.0
                ),
                -float(
                    cell.get(
                        "avg_regret_pct",
                        0.0,
                    )
                    or 0.0
                ),
                key,
                cell,
            )
        )

    candidates.sort(reverse=True)

    out = []

    for edge, pos_rate, neg_regret, key, cell in candidates[:limit]:
        out.append(
            {
                "key": key,
                "regime": cell["regime"],
                "strategy": cell["strategy"],
                "horizon_candles":
                    cell["horizon_candles"],
                "trend_threshold_pct":
                    cell["trend_threshold_pct"],
                "vol_gate_pct":
                    cell.get("vol_gate_pct"),
                "samples":
                    cell["samples"],
                "eligible_trade_samples":
                    cell[
                        "eligible_trade_samples"
                    ],
                "avg_trade_net_edge_pct":
                    cell[
                        "avg_trade_net_edge_pct"
                    ],
                "trade_positive_rate":
                    cell.get(
                        "trade_positive_rate"
                    ),
                "trade_coverage":
                    cell.get(
                        "trade_coverage"
                    ),
                "avg_regret_pct":
                    cell.get(
                        "avg_regret_pct"
                    ),
                "opportunity_capture_rate":
                    cell.get(
                        "opportunity_capture_rate"
                    ),
            }
        )

    return out


def save_parameter_surface_snapshot():
    payload = {
        "version": "1.23",
        "saved_at": now_iso(),
        "symbol": SYMBOL,
        "research_only": True,
        "production_loop_unchanged": True,
        "grid_definition":
            surface_grid_definition(),
        "grid_hash": surface_grid_hash(),
        "metrics": surface_metrics,
        "matrix": parameter_surface,
        "leaderboard": surface_leaderboard(
            limit=20
        ),
    }

    tmp = SURFACE_MATRIX_FILE + ".tmp"

    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(
            payload,
            f,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )

    os.replace(
        tmp,
        SURFACE_MATRIX_FILE,
    )



def tradeability_leaderboard(limit=10):
    rows = []

    for key, cell in tradeability_feature_matrix.items():
        n = int(cell.get("samples", 0))

        if n < TRADEABILITY_MATRIX_MIN_SAMPLES:
            continue

        rate = float(
            cell.get(
                "market_tradeable_rate",
                0.0,
            )
            or 0.0
        )

        avg_net = float(
            cell.get(
                "avg_best_net_pct",
                0.0,
            )
            or 0.0
        )

        rows.append(
            (
                rate,
                avg_net,
                n,
                key,
                cell,
            )
        )

    rows.sort(reverse=True)

    return [
        {
            "key": key,
            **cell,
        }
        for _, _, _, key, cell
        in rows[:limit]
    ]


def save_tradeability_snapshot():
    payload = {
        "version": "1.23",
        "state_representation":
            STATE_REP_VERSION,
        "saved_at": now_iso(),
        "symbol": SYMBOL,
        "research_only": True,
        "gate_enabled":
            TRADEABILITY_GATE_ENABLED,
        "score_threshold":
            TRADEABILITY_SCORE_THRESHOLD,
        "safety_margin_pct":
            TRADEABILITY_SAFETY_MARGIN_PCT,
        "horizons": list(
            TRADEABILITY_HORIZONS
        ),
        "metrics_by_horizon":
            tradeability_metrics,
        "feature_matrix":
            tradeability_feature_matrix,
        "leaderboard":
            tradeability_leaderboard(
                limit=20
            ),
    }

    tmp = (
        TRADEABILITY_MATRIX_FILE
        + ".tmp"
    )

    with open(
        tmp,
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            payload,
            f,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )

    os.replace(
        tmp,
        TRADEABILITY_MATRIX_FILE,
    )



def corridor_leaderboard(limit=10):
    rows = []

    for key, cell in corridor_feature_matrix.items():
        n = int(cell.get("samples", 0))

        if n < CORRIDOR_MATRIX_MIN_SAMPLES:
            continue

        rate = float(
            cell.get(
                "market_tradeable_rate",
                0.0,
            )
            or 0.0
        )

        avg_net = float(
            cell.get(
                "avg_best_net_pct",
                0.0,
            )
            or 0.0
        )

        rows.append(
            (
                rate,
                avg_net,
                n,
                key,
                cell,
            )
        )

    rows.sort(reverse=True)

    return [
        {
            "key": key,
            **cell,
        }
        for _, _, _, key, cell
        in rows[:limit]
    ]


def save_corridor_snapshot():
    payload = {
        "version": "1.23",
        "corridor_version":
            CORRIDOR_VERSION,
        "saved_at": now_iso(),
        "symbol": SYMBOL,
        "research_only": True,
        "connected_to_execution":
            False,
        "windows": list(
            CORRIDOR_WINDOWS
        ),
        "horizons": list(
            CORRIDOR_HORIZONS
        ),
        "score_threshold":
            CORRIDOR_SCORE_THRESHOLD,
        "metrics_by_horizon":
            corridor_metrics,
        "sr1_vs_corridor":
            corridor_compare,
        "feature_matrix":
            corridor_feature_matrix,
        "leaderboard":
            corridor_leaderboard(
                limit=20
            ),
    }

    tmp = CORRIDOR_MATRIX_FILE + ".tmp"

    with open(
        tmp,
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            payload,
            f,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )

    os.replace(
        tmp,
        CORRIDOR_MATRIX_FILE,
    )



def corridor2_leaderboard(prefix, limit=10):
    rows = []

    for key, cell in corridor2_feature_matrix.items():
        if not key.startswith(prefix):
            continue

        n = int(cell.get("samples", 0))

        if n < CORRIDOR2_MATRIX_MIN_SAMPLES:
            continue

        rate = float(
            cell.get(
                "market_tradeable_rate",
                0.0,
            )
            or 0.0
        )

        avg_net = float(
            cell.get(
                "avg_best_net_pct",
                0.0,
            )
            or 0.0
        )

        rows.append(
            (
                rate,
                avg_net,
                n,
                key,
                cell,
            )
        )

    rows.sort(reverse=True)

    return [
        {
            "key": key,
            **cell,
        }
        for _, _, _, key, cell
        in rows[:limit]
    ]


def save_corridor2_snapshot():
    payload = {
        "version": "1.23",
        "corridor2_version":
            CORRIDOR2_VERSION,
        "saved_at": now_iso(),
        "symbol": SYMBOL,
        "research_only": True,
        "connected_to_execution":
            False,
        "horizons": list(
            CORRIDOR_HORIZONS
        ),
        "metrics_by_horizon":
            corridor2_metrics,
        "flag_leaders":
            corridor2_leaderboard(
                "FLAG:",
                limit=20,
            ),
        "transition_leaders":
            corridor2_leaderboard(
                "TRANS:",
                limit=20,
            ),
        "signature_leaders":
            corridor2_leaderboard(
                "SIG:",
                limit=20,
            ),
        "matrix":
            corridor2_feature_matrix,
        "last_flags":
            last_corridor2_flags,
    }

    tmp = (
        CORRIDOR2_MATRIX_FILE
        + ".tmp"
    )

    with open(
        tmp,
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            payload,
            f,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )

    os.replace(
        tmp,
        CORRIDOR2_MATRIX_FILE,
    )



def corridor3_leaderboard(prefix, limit=10):
    rows = []

    for key, cell in corridor3_feature_matrix.items():
        if not key.startswith(prefix):
            continue

        n = int(cell.get("samples", 0))

        if n < CORRIDOR3_MATRIX_MIN_SAMPLES:
            continue

        rate = float(
            cell.get(
                "market_tradeable_rate",
                0.0,
            )
            or 0.0
        )

        avg_net = float(
            cell.get(
                "avg_best_net_pct",
                0.0,
            )
            or 0.0
        )

        rows.append(
            (
                rate,
                avg_net,
                n,
                key,
                cell,
            )
        )

    rows.sort(reverse=True)

    return [
        {
            "key": key,
            **cell,
        }
        for _, _, _, key, cell
        in rows[:limit]
    ]


def save_corridor3_snapshot():
    payload = {
        "version": "1.23",
        "corridor3_version":
            CORRIDOR3_VERSION,
        "saved_at": now_iso(),
        "symbol": SYMBOL,
        "research_only": True,
        "connected_to_execution":
            False,
        "epsilon_cost_fraction":
            CORRIDOR3_EPSILON_COST_FRACTION,
        "horizons": list(
            CORRIDOR_HORIZONS
        ),
        "metrics_by_horizon":
            corridor3_metrics,
        "transition_scale_leaders":
            corridor3_leaderboard(
                "TRANS_SCALE:",
                limit=20,
            ),
        "flag_age_leaders":
            corridor3_leaderboard(
                "FLAG_AGE:",
                limit=20,
            ),
        "signature_age_leaders":
            corridor3_leaderboard(
                "SIG_AGE:",
                limit=20,
            ),
        "matrix":
            corridor3_feature_matrix,
        "tracker":
            corridor3_tracker,
    }

    tmp = (
        CORRIDOR3_MATRIX_FILE
        + ".tmp"
    )

    with open(
        tmp,
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            payload,
            f,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )

    os.replace(
        tmp,
        CORRIDOR3_MATRIX_FILE,
    )


def load_runtime():
    global state_id
    global candle_seq
    global pending_predictions
    global pending_shadows
    global stats
    global shadow_metrics
    global horizon_matrix
    global horizon_system_metrics
    global adaptive_metrics
    global pending_surface_shadows
    global parameter_surface
    global surface_metrics
    global pending_tradeability_probes
    global tradeability_metrics
    global tradeability_feature_matrix
    global pending_corridor_probes
    global corridor_metrics
    global corridor_feature_matrix
    global corridor_compare
    global pending_corridor2_probes
    global corridor2_feature_matrix
    global corridor2_metrics
    global last_corridor2_flags
    global pending_corridor3_probes
    global corridor3_feature_matrix
    global corridor3_metrics
    global corridor3_tracker
    global geometry_tracker
    global cone_tracker
    global cone_transition_tracker
    global pending_geometry_outcome_probes
    global geometry_outcome_matrix
    global geometry_outcome_metrics
    global transition_outcome_matrix
    global transition_edge_matrix
    global phase_topology_tracker
    global phase_front_tracker
    global economic_front_tracker
    global bipolar_pressure_tracker
    global latest_bipolar_pressure
    global session_continuity_tracker
    global model_residual_tracker
    global latest_model_residual
    global last_close_time_ms
    global portfolio

    if not os.path.exists(RUNTIME_FILE):
        print("Runtime: fresh session.")
        return

    try:
        with open(RUNTIME_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)

        state_id = int(payload.get("state_id", 0))
        candle_seq = int(payload.get("candle_seq", 0))
        pending_predictions = list(payload.get("pending_predictions", []))
        pending_shadows = list(payload.get("pending_shadows", []))
        stats = dict(payload.get("stats", {}))

        restored_shadow_metrics = payload.get("shadow_metrics")
        if isinstance(restored_shadow_metrics, dict):
            shadow_metrics.update(restored_shadow_metrics)

        restored_horizon_matrix = payload.get("horizon_matrix")
        if isinstance(restored_horizon_matrix, dict):
            horizon_matrix = restored_horizon_matrix

        restored_horizon_system_metrics = payload.get(
            "horizon_system_metrics"
        )
        if isinstance(restored_horizon_system_metrics, dict):
            horizon_system_metrics = restored_horizon_system_metrics

        restored_adaptive_metrics = payload.get("adaptive_metrics")
        if isinstance(restored_adaptive_metrics, dict):
            adaptive_metrics.update(restored_adaptive_metrics)

        restored_surface_pending = payload.get(
            "pending_surface_shadows"
        )
        if isinstance(
            restored_surface_pending,
            list,
        ):
            pending_surface_shadows = (
                restored_surface_pending
            )

        restored_surface = payload.get(
            "parameter_surface"
        )
        if isinstance(restored_surface, dict):
            parameter_surface = (
                restored_surface
            )

        restored_surface_metrics = payload.get(
            "surface_metrics"
        )
        if isinstance(
            restored_surface_metrics,
            dict,
        ):
            surface_metrics.update(
                restored_surface_metrics
            )

        restored_tradeability_pending = payload.get(
            "pending_tradeability_probes"
        )
        if isinstance(
            restored_tradeability_pending,
            list,
        ):
            pending_tradeability_probes = (
                restored_tradeability_pending
            )

        restored_tradeability_metrics = payload.get(
            "tradeability_metrics"
        )
        if isinstance(
            restored_tradeability_metrics,
            dict,
        ):
            for h, values in (
                restored_tradeability_metrics.items()
            ):
                if isinstance(values, dict):
                    tradeability_metrics.setdefault(
                        str(h),
                        {},
                    ).update(values)

        restored_tradeability_matrix = payload.get(
            "tradeability_feature_matrix"
        )
        if isinstance(
            restored_tradeability_matrix,
            dict,
        ):
            tradeability_feature_matrix = (
                restored_tradeability_matrix
            )

        restored_corridor_pending = payload.get(
            "pending_corridor_probes"
        )
        if isinstance(
            restored_corridor_pending,
            list,
        ):
            pending_corridor_probes = (
                restored_corridor_pending
            )

        restored_corridor_metrics = payload.get(
            "corridor_metrics"
        )
        if isinstance(
            restored_corridor_metrics,
            dict,
        ):
            for h, values in (
                restored_corridor_metrics.items()
            ):
                if isinstance(values, dict):
                    corridor_metrics.setdefault(
                        str(h),
                        {},
                    ).update(values)

        restored_corridor_matrix = payload.get(
            "corridor_feature_matrix"
        )
        if isinstance(
            restored_corridor_matrix,
            dict,
        ):
            corridor_feature_matrix = (
                restored_corridor_matrix
            )

        restored_corridor_compare = payload.get(
            "corridor_compare"
        )
        if isinstance(
            restored_corridor_compare,
            dict,
        ):
            for h, values in (
                restored_corridor_compare.items()
            ):
                if isinstance(values, dict):
                    corridor_compare.setdefault(
                        str(h),
                        {},
                    ).update(values)

        restored_corridor2_pending = payload.get(
            "pending_corridor2_probes"
        )
        if isinstance(
            restored_corridor2_pending,
            list,
        ):
            pending_corridor2_probes = (
                restored_corridor2_pending
            )

        restored_corridor2_matrix = payload.get(
            "corridor2_feature_matrix"
        )
        if isinstance(
            restored_corridor2_matrix,
            dict,
        ):
            corridor2_feature_matrix = (
                restored_corridor2_matrix
            )

        restored_corridor2_metrics = payload.get(
            "corridor2_metrics"
        )
        if isinstance(
            restored_corridor2_metrics,
            dict,
        ):
            for h, values in (
                restored_corridor2_metrics.items()
            ):
                if isinstance(values, dict):
                    corridor2_metrics.setdefault(
                        str(h),
                        {},
                    ).update(values)

        restored_last_corridor2_flags = payload.get(
            "last_corridor2_flags"
        )
        if isinstance(
            restored_last_corridor2_flags,
            dict,
        ):
            last_corridor2_flags = (
                restored_last_corridor2_flags
            )

        restored_corridor3_pending = payload.get(
            "pending_corridor3_probes"
        )
        if isinstance(
            restored_corridor3_pending,
            list,
        ):
            pending_corridor3_probes = (
                restored_corridor3_pending
            )

        restored_corridor3_matrix = payload.get(
            "corridor3_feature_matrix"
        )
        if isinstance(
            restored_corridor3_matrix,
            dict,
        ):
            corridor3_feature_matrix = (
                restored_corridor3_matrix
            )

        restored_corridor3_metrics = payload.get(
            "corridor3_metrics"
        )
        if isinstance(
            restored_corridor3_metrics,
            dict,
        ):
            for h, values in (
                restored_corridor3_metrics.items()
            ):
                if isinstance(values, dict):
                    corridor3_metrics.setdefault(
                        str(h),
                        {},
                    ).update(values)

        restored_corridor3_tracker = payload.get(
            "corridor3_tracker"
        )
        if isinstance(
            restored_corridor3_tracker,
            dict,
        ):
            corridor3_tracker.update(
                restored_corridor3_tracker
            )

        restored_geometry_tracker = payload.get(
            "geometry_tracker"
        )
        if isinstance(
            restored_geometry_tracker,
            dict,
        ):
            geometry_tracker.update(
                restored_geometry_tracker
            )

        restored_cone_tracker = payload.get(
            "cone_tracker"
        )
        if isinstance(
            restored_cone_tracker,
            dict,
        ):
            cone_tracker.update(
                restored_cone_tracker
            )

        restored_ctd_tracker = payload.get(
            "cone_transition_tracker"
        )
        if isinstance(restored_ctd_tracker, dict):
            cone_transition_tracker.update(
                restored_ctd_tracker
            )

        restored_spt_tracker = payload.get(
            "phase_topology_tracker"
        )
        if isinstance(restored_spt_tracker, dict):
            phase_topology_tracker.update(
                restored_spt_tracker
            )

        restored_pfl_tracker = payload.get(
            "phase_front_tracker"
        )
        if isinstance(restored_pfl_tracker, dict):
            phase_front_tracker.update(
                restored_pfl_tracker
            )

        restored_efs_tracker = payload.get(
            "economic_front_tracker"
        )
        if isinstance(restored_efs_tracker, dict):
            economic_front_tracker.update(
                restored_efs_tracker
            )

        restored_bpm_tracker = payload.get("bipolar_pressure_tracker")
        if isinstance(restored_bpm_tracker, dict):
            bipolar_pressure_tracker.update(restored_bpm_tracker)
            if not isinstance(bipolar_pressure_tracker.get("by_horizon"), dict):
                bipolar_pressure_tracker["by_horizon"] = {}
            for _h in BPM1_HORIZONS:
                bipolar_pressure_tracker["by_horizon"].setdefault(str(_h), {
                    "q": 0.0, "dq_per_min": 0.0, "last_nonzero_sign": 0,
                    "sign_age_minutes": 0.0,
                })

        restored_session_tracker = payload.get("session_continuity_tracker")
        if isinstance(restored_session_tracker, dict):
            session_continuity_tracker.update(restored_session_tracker)

        restored_residual_tracker = payload.get("model_residual_tracker")
        if isinstance(restored_residual_tracker, dict):
            model_residual_tracker.update(restored_residual_tracker)
            if not isinstance(model_residual_tracker.get("bias_by_horizon"), dict):
                model_residual_tracker["bias_by_horizon"] = {str(h): 0.0 for h in (5, 15, 30, 60, 120)}
            latest_model_residual = dict(model_residual_tracker)

        restored_gol_pending = payload.get(
            "pending_geometry_outcome_probes"
        )
        if isinstance(
            restored_gol_pending,
            list,
        ):
            pending_geometry_outcome_probes = (
                restored_gol_pending
            )

        restored_gol_matrix = payload.get(
            "geometry_outcome_matrix"
        )
        if isinstance(
            restored_gol_matrix,
            dict,
        ):
            geometry_outcome_matrix = (
                restored_gol_matrix
            )

        restored_tom_matrix = payload.get(
            "transition_outcome_matrix"
        )
        if isinstance(
            restored_tom_matrix,
            dict,
        ):
            transition_outcome_matrix = (
                restored_tom_matrix
            )

        restored_teg_matrix = payload.get(
            "transition_edge_matrix"
        )
        if isinstance(
            restored_teg_matrix,
            dict,
        ):
            transition_edge_matrix = (
                restored_teg_matrix
            )

        restored_gol_metrics = payload.get(
            "geometry_outcome_metrics"
        )
        if isinstance(
            restored_gol_metrics,
            dict,
        ):
            for h, values in (
                restored_gol_metrics.items()
            ):
                if isinstance(values, dict):
                    geometry_outcome_metrics.setdefault(
                        str(h),
                        {},
                    ).update(values)

        last_close_time_ms = int(payload.get("last_close_time_ms", 0))

        restored_portfolio = payload.get("portfolio")
        if isinstance(restored_portfolio, dict):
            portfolio.update(restored_portfolio)

        print(
            "Runtime restored:",
            f"X={state_id}",
            f"seq={candle_seq}",
            f"pending={len(pending_predictions)}",
            f"shadows={len(pending_shadows)}",
            f"surface={len(pending_surface_shadows)}",
            f"tradeability={len(pending_tradeability_probes)}",
            f"corridor={len(pending_corridor_probes)}",
            f"cor2={len(pending_corridor2_probes)}",
            f"cor3={len(pending_corridor3_probes)}",
            f"gol={len(pending_geometry_outcome_probes)}",
            f"USDT={portfolio['usdt']:.2f}",
            f"BTC={portfolio['btc']:.8f}",
        )

    except Exception as e:
        print("Runtime restore error:", e)
        print("Starting with fresh runtime state.")


def rest_klines(limit=31, start_time=None):
    params = {
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "limit": limit,
    }

    if start_time is not None:
        params["startTime"] = int(start_time)

    url = REST_BASE + "?" + urllib.parse.urlencode(params)

    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read().decode())


def load_history():
    print("Loading historical candles...")

    data = rest_klines(limit=FEATURE_HISTORY_CANDLES + 1)

    # Last REST candle can still be open.
    for k in data[:-1][-FEATURE_HISTORY_CANDLES:]:
        candles.append({
            "open_time_ms": int(k[0]),
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
            "close_time_ms": int(k[6]),
        })

    print(f"Loaded {len(candles)} closed candles.")


def fetch_exact_horizon_close(due_close_time_ms):
    # Ask Binance for candles beginning just before the due close time.
    start = max(0, int(due_close_time_ms) - 2 * MINUTE_MS)
    data = rest_klines(limit=5, start_time=start)

    best = None

    for k in data:
        close_time = int(k[6])

        if close_time >= due_close_time_ms:
            best = {
                "close": float(k[4]),
                "close_time_ms": close_time,
            }
            break

    return best


def portfolio_metrics(market_price):
    btc_value = portfolio["btc"] * market_price
    equity = portfolio["usdt"] + btc_value

    if equity > portfolio["peak_equity_usdt"]:
        portfolio["peak_equity_usdt"] = equity

    peak = max(portfolio["peak_equity_usdt"], 1e-12)
    drawdown_pct = max(
        0.0,
        ((peak - equity) / peak) * 100,
    )

    if drawdown_pct > portfolio["max_drawdown_pct"]:
        portfolio["max_drawdown_pct"] = drawdown_pct

    exposure_pct = (
        (btc_value / equity) * 100
        if equity > 0
        else 0.0
    )

    unrealized = (
        btc_value
        - portfolio["btc_cost_basis_usdt"]
    )

    return {
        "equity_usdt": equity,
        "btc_value_usdt": btc_value,
        "exposure_pct": exposure_pct,
        "drawdown_pct": drawdown_pct,
        "unrealized_pnl_usdt": unrealized,
    }


def portfolio_snapshot(market_price, state_id_value, reason):
    m = portfolio_metrics(market_price)

    snap = {
        "time": now_iso(),
        "state_id": state_id_value,
        "reason": reason,
        "market_price": market_price,
        "usdt": round(portfolio["usdt"], 8),
        "btc": round(portfolio["btc"], 12),
        "btc_cost_basis_usdt": round(
            portfolio["btc_cost_basis_usdt"], 8
        ),
        "realized_pnl_usdt": round(
            portfolio["realized_pnl_usdt"], 8
        ),
        "unrealized_pnl_usdt": round(
            m["unrealized_pnl_usdt"], 8
        ),
        "equity_usdt": round(
            m["equity_usdt"], 8
        ),
        "exposure_pct": round(
            m["exposure_pct"], 5
        ),
        "drawdown_pct": round(
            m["drawdown_pct"], 5
        ),
        "max_drawdown_pct": round(
            portfolio["max_drawdown_pct"], 5
        ),
        "total_fees_usdt": round(
            portfolio["total_fees_usdt"], 8
        ),
        "paper_trades": portfolio["paper_trades"],
    }

    append_jsonl(PORTFOLIO_FILE, snap)
    return snap



def save_execution_state():
    tmp = EXECUTION_STATE_FILE + ".tmp"
    payload = {
        "version": "1.23",
        "saved_at": now_iso(),
        "mode": EXECUTION_MODE,
        "runtime": execution_runtime,
    }
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, EXECUTION_STATE_FILE)


def load_execution_state():
    if not os.path.exists(EXECUTION_STATE_FILE):
        return
    try:
        with open(EXECUTION_STATE_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
        runtime = payload.get("runtime")
        if isinstance(runtime, dict):
            # Never restore preflight=True across a fresh process. Auth/network
            # must be checked again. Restore only cooldown/order counters.
            execution_runtime["last_order_epoch_ms"] = int(runtime.get("last_order_epoch_ms", 0) or 0)
            execution_runtime["last_order_id"] = runtime.get("last_order_id")
            execution_runtime["orders_sent"] = int(runtime.get("orders_sent", 0) or 0)
    except Exception as e:
        print("Execution state restore warning:", e)


def _exchange_base(mode=None):
    mode = (mode or EXECUTION_MODE).upper()
    return BINANCE_TESTNET_BASE if mode == "TESTNET" else BINANCE_LIVE_BASE


def _http_json(method, url, headers=None, timeout=10):
    req = urllib.request.Request(url, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8")
        except Exception:
            body = ""
        raise RuntimeError(f"HTTP {e.code}: {body[:500]}") from e


def binance_server_time_ms(mode=None):
    data = _http_json("GET", _exchange_base(mode) + "/api/v3/time")
    return int(data.get("serverTime", 0))


def binance_signed_request(method, path, params=None, mode=None):
    if not BINANCE_API_KEY or not BINANCE_API_SECRET:
        raise RuntimeError("BINANCE_API_KEY/BINANCE_API_SECRET missing")

    mode = (mode or EXECUTION_MODE).upper()
    params = dict(params or {})
    local_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    server_ms = local_ms + int(execution_runtime.get("server_time_offset_ms", 0))
    params["timestamp"] = server_ms
    params["recvWindow"] = BINANCE_RECV_WINDOW_MS

    query = urllib.parse.urlencode(params)
    signature = hmac.new(
        BINANCE_API_SECRET.encode("utf-8"),
        query.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    url = _exchange_base(mode) + path + "?" + query + "&signature=" + signature
    return _http_json(method, url, headers={"X-MBX-APIKEY": BINANCE_API_KEY})


def binance_account(mode=None):
    return binance_signed_request("GET", "/api/v3/account", {}, mode=mode)


def binance_free_balance(account, asset):
    for row in account.get("balances", []):
        if str(row.get("asset")) == asset:
            try:
                return float(row.get("free", 0.0))
            except Exception:
                return 0.0
    return 0.0


def binance_symbol_min_notional(mode=None):
    try:
        data = _http_json(
            "GET",
            _exchange_base(mode) + "/api/v3/exchangeInfo?symbol=" + urllib.parse.quote(SYMBOL),
        )
        symbols = data.get("symbols", [])
        if not symbols:
            return MIN_PAPER_NOTIONAL
        for f in symbols[0].get("filters", []):
            if f.get("filterType") in ("NOTIONAL", "MIN_NOTIONAL"):
                value = float(f.get("minNotional", 0.0) or 0.0)
                if value > 0:
                    return value
    except Exception:
        pass
    return MIN_PAPER_NOTIONAL


def exchange_preflight(mode=None):
    mode = (mode or EXECUTION_MODE).upper()

    if mode == "PAPER":
        execution_runtime["preflight_ok"] = True
        execution_runtime["preflight_reason"] = "PAPER_MODE"
        return dict(execution_runtime)

    if not BINANCE_API_KEY or not BINANCE_API_SECRET:
        execution_runtime["preflight_ok"] = False
        execution_runtime["preflight_reason"] = "MISSING_API_CREDENTIALS"
        return dict(execution_runtime)

    if mode == "LIVE" and not LIVE_ARMED:
        execution_runtime["preflight_ok"] = False
        execution_runtime["preflight_reason"] = "LIVE_NOT_ARMED"
        return dict(execution_runtime)

    try:
        local_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        server_ms = binance_server_time_ms(mode)
        execution_runtime["server_time_offset_ms"] = int(server_ms - local_ms)
        account = binance_account(mode)
        execution_runtime["preflight_ok"] = bool(account.get("canTrade", True))
        execution_runtime["preflight_reason"] = "OK" if execution_runtime["preflight_ok"] else "ACCOUNT_CANNOT_TRADE"
        execution_runtime["free_usdt"] = round(binance_free_balance(account, "USDT"), 8)
        execution_runtime["free_btc"] = round(binance_free_balance(account, "BTC"), 12)

        if execution_runtime["preflight_ok"] and EXCHANGE_PREFLIGHT_ORDER_TEST:
            probe_notional = max(binance_symbol_min_notional(mode), min(EXCHANGE_MAX_NOTIONAL_USDT, 10.0))
            params = {
                "symbol": SYMBOL,
                "side": "BUY",
                "type": "MARKET",
                "quoteOrderQty": f"{probe_notional:.8f}",
            }
            binance_signed_request("POST", "/api/v3/order/test", params, mode=mode)
            execution_runtime["preflight_order_test"] = "OK"

        save_execution_state()
    except Exception as e:
        execution_runtime["preflight_ok"] = False
        execution_runtime["preflight_reason"] = "API_ERROR: " + str(e)[:220]

    return dict(execution_runtime)


def erl1_flag_evidence(state, horizon):
    cor2 = state.get("corridor_multilabel", {})
    active = list(cor2.get("active_flags", []) or [])
    rows = []
    for flag in active:
        key = f"FLAG:H{int(horizon)}|{flag}"
        cell = corridor2_feature_matrix.get(key)
        if not isinstance(cell, dict):
            continue
        n = int(cell.get("samples", 0) or 0)
        rate = float(cell.get("market_tradeable_rate", 0.0) or 0.0)
        avg_net = float(cell.get("avg_best_net_pct", 0.0) or 0.0)
        if n >= ERL1_MIN_FLAG_SAMPLES and rate >= ERL1_MIN_FLAG_RATE and avg_net > 0.0:
            rows.append({
                "flag": flag,
                "samples": n,
                "rate": round(rate, 6),
                "avg_best_net_pct": round(avg_net, 6),
            })
    rows.sort(key=lambda x: (x["avg_best_net_pct"], x["rate"], x["samples"]), reverse=True)
    return rows[:5]


def erl1_geometry_alignment(state, action_override=None):
    action = str(action_override if action_override is not None else state.get("action", "HOLD"))
    geometry = state.get("geometry_state", {})
    cone = geometry.get("cone_model", {})
    models = cone.get("horizons", {}) if isinstance(cone, dict) else {}

    target_sign = 1 if action == "BUY" else -1 if action == "SELL" else 0
    observed = []
    for h in (15, 30, 60):
        model = models.get(str(h), {})
        if not isinstance(model, dict) or "tilt_deg" not in model:
            continue
        tilt = float(model.get("tilt_deg", 0.0))
        sign = 1 if tilt >= CONE_DYNAMICS_STATE_DEADZONE_DEG else -1 if tilt <= -CONE_DYNAMICS_STATE_DEADZONE_DEG else 0
        observed.append((h, tilt, sign))

    if not observed or target_sign == 0:
        return 0.0, observed

    aligned = sum(1 for _, _, sign in observed if sign == target_sign)
    anti = sum(1 for _, _, sign in observed if sign == -target_sign)
    raw = (aligned - 0.5 * anti) / max(1, len(observed))
    return max(0.0, min(1.0, raw)), observed


def erl1_topology_support(state, action_override=None):
    action = str(action_override if action_override is not None else state.get("action", "HOLD"))
    ctd = state.get("geometry_state", {}).get("cone_transition_dynamics", {})
    topology = ctd.get("phase_topology", {}) if isinstance(ctd, dict) else {}
    klass = str(topology.get("topology_class", "UNRESOLVED_BOUNDARY_STATE"))
    propagation = ctd.get("propagation", {}) if isinstance(ctd, dict) else {}
    prop_dir = str(propagation.get("direction", "NONE"))

    buy_classes = {
        "FULL_UP_DOMAIN", "MICRO_UP_IN_MACRO_DOWN", "UP_ISLAND_IN_DOWN",
        "MICRO_MESO_UP_IN_MACRO_DOWN", "UP_DOMAIN_BELOW_MACRO_DOWN",
        "SINGLE_FRONT_DOWN_TO_UP", "MICRO_STABLE_IN_MACRO_UP",
    }
    sell_classes = {
        "FULL_DOWN_DOMAIN", "MICRO_DOWN_IN_MACRO_UP", "DOWN_ISLAND_IN_UP",
        "MICRO_MESO_DOWN_IN_MACRO_UP", "DOWN_DOMAIN_BELOW_MACRO_UP",
        "SINGLE_FRONT_UP_TO_DOWN", "MICRO_STABLE_IN_MACRO_DOWN",
    }

    class_ok = klass in (buy_classes if action == "BUY" else sell_classes if action == "SELL" else set())
    prop_ok = prop_dir == ("UP" if action == "BUY" else "DOWN" if action == "SELL" else "NONE")
    score = (0.65 if class_ok else 0.0) + (0.35 if prop_ok else 0.0)
    return min(1.0, score), klass, prop_dir


def gsr1_domain_persistence(state, action_override=None):
    action = str(action_override if action_override is not None else state.get("action", "HOLD"))
    target = "UP" if action == "BUY" else "DOWN" if action == "SELL" else None
    if target is None:
        return 0.0, None

    ctd = state.get("geometry_state", {}).get("cone_transition_dynamics", {})
    topology = ctd.get("phase_topology", {}) if isinstance(ctd, dict) else {}
    domains = topology.get("domains", []) if isinstance(topology, dict) else []
    matches = [d for d in domains if str(d.get("direction")) == target]
    if not matches:
        return 0.0, None

    best = max(matches, key=lambda d: (len(d.get("scales", [])), float(d.get("age_minutes", 0.0))))
    age = max(0.0, float(best.get("age_minutes", 0.0)))
    # New domains are not treated as zero-information: 0.25 floor, then age ramps
    # toward 1.0 over GSR1_PERSISTENCE_FULL_MINUTES.
    score = 0.25 + 0.75 * min(1.0, age / max(GSR1_PERSISTENCE_FULL_MINUTES, 1e-9))
    return min(1.0, score), {
        "domain_id": best.get("domain_id"),
        "direction": best.get("direction"),
        "start_h": best.get("start_h"),
        "end_h": best.get("end_h"),
        "age_minutes": round(age, 4),
        "lifecycle": best.get("lifecycle"),
    }


def _gsr1_direction_eval(state, action):
    action = str(action).upper()
    ctd = state.get("geometry_state", {}).get("cone_transition_dynamics", {})
    if action not in ("BUY", "SELL") or not isinstance(ctd, dict) or not ctd.get("ready", False):
        return {
            "action": action, "ready": False,
            "target_direction": "UP" if action == "BUY" else "DOWN" if action == "SELL" else "NONE",
            "continuation_index": 0.0, "reversal_index": 0.0, "stability_index": 0.0,
            "verdict": "WARMUP", "testnet_safe": False, "strict_safe": False,
        }

    alignment, tilts = erl1_geometry_alignment(state, action)
    topology_score, topology_class, prop_dir = erl1_topology_support(state, action)
    coherence = max(0.0, min(1.0, float(ctd.get("rotation_coherence", 0.0))))
    rotation = max(0.0, float(ctd.get("rotation_energy_deg_per_min", 0.0)))
    rotation_norm = min(1.0, rotation / max(CONE_DYNAMICS_ROT_FAST_DEG_PER_MIN, 1e-9))
    shock_h = ctd.get("shock_horizon")
    shock_speed = max(0.0, float(ctd.get("shock_speed_deg_per_min", 0.0)))
    shock_norm = min(1.0, shock_speed / max(CONE_DYNAMICS_ROT_FAST_DEG_PER_MIN, 1e-9)) if shock_h is not None else 0.0
    persistence, domain = gsr1_domain_persistence(state, action)

    target_dir = "UP" if action == "BUY" else "DOWN"
    prop_match = prop_dir == target_dir
    prop_opp = prop_dir in ("UP", "DOWN") and prop_dir != target_dir
    prop_factor = 1.10 if prop_match else 0.78 if prop_opp else 0.95

    continuation = (
        alignment
        * (0.35 + 0.65 * coherence)
        * (0.40 + 0.60 * persistence)
        * prop_factor
        * (1.0 - 0.25 * rotation_norm * (1.0 - coherence))
    )
    continuation = max(0.0, min(1.0, continuation))

    shock_factor = 0.35 + 0.65 * shock_norm if shock_h is not None else 0.20
    reversal = rotation_norm * (1.0 - coherence) * shock_factor * (0.55 + 0.45 * alignment)
    if prop_opp:
        reversal += 0.15
    if topology_score < 0.35:
        reversal += 0.08
    if shock_h in (15, 30):
        reversal += 0.05 * shock_norm
    reversal = max(0.0, min(1.0, reversal))

    stability = coherence * (1.0 - 0.50 * rotation_norm) * (0.50 + 0.50 * persistence)
    stability = max(0.0, min(1.0, stability))

    if reversal >= 0.65:
        verdict = "REVERSAL_RISK"
    elif coherence < 0.25 and rotation_norm >= 0.50:
        verdict = "GEOMETRY_UNSTABLE"
    elif continuation >= 0.55 and reversal <= 0.35:
        verdict = "CONTINUATION_COHERENT"
    elif continuation >= 0.35 and reversal <= 0.55:
        verdict = "CONTINUATION_WEAK"
    else:
        verdict = "MIXED"

    return {
        "action": action, "ready": True, "target_direction": target_dir,
        "continuation_index": round(continuation, 4), "reversal_index": round(reversal, 4),
        "stability_index": round(stability, 4), "direction_alignment": round(alignment, 4),
        "rotation_coherence": round(coherence, 4),
        "rotation_energy_deg_per_min": round(rotation, 4), "rotation_norm": round(rotation_norm, 4),
        "shock_horizon": shock_h, "shock_norm": round(shock_norm, 4),
        "propagation_direction": prop_dir, "propagation_match": prop_match,
        "topology_class": topology_class, "topology_support": round(topology_score, 4),
        "persistence": round(persistence, 4), "domain": domain,
        "tilts": [{"h": h, "tilt_deg": round(t, 3), "state": sign} for h, t, sign in tilts],
        "verdict": verdict,
        "strict_safe": continuation >= GSR1_MIN_CONTINUATION and reversal <= GSR1_MAX_REVERSAL_RISK,
        "testnet_safe": continuation >= GSR1_TESTNET_MIN_CONTINUATION and reversal <= GSR1_TESTNET_MAX_REVERSAL_RISK,
    }


def compute_geometric_stability_reversal(state):
    raw_action = str(state.get("action", "HOLD")).upper()
    ctd = state.get("geometry_state", {}).get("cone_transition_dynamics", {})
    if not isinstance(ctd, dict) or not ctd.get("ready", False):
        return {
            "version": GEOMETRIC_STABILITY_REVERSAL_VERSION, "ready": False,
            "action": raw_action, "continuation_index": 0.0, "reversal_index": 0.0,
            "stability_index": 0.0, "verdict": "WARMUP", "testnet_safe": False,
            "strict_safe": False, "counterfactual": {}, "geometry_preferred_action": "NONE",
        }

    buy = _gsr1_direction_eval(state, "BUY")
    sell = _gsr1_direction_eval(state, "SELL")
    buy_q = float(buy.get("continuation_index", 0.0)) * (1.0 - 0.70 * float(buy.get("reversal_index", 0.0)))
    sell_q = float(sell.get("continuation_index", 0.0)) * (1.0 - 0.70 * float(sell.get("reversal_index", 0.0)))
    preferred = "BUY" if buy_q > sell_q + 1e-9 else "SELL" if sell_q > buy_q + 1e-9 else "NONE"

    if raw_action in ("BUY", "SELL"):
        selected = buy if raw_action == "BUY" else sell
        verdict = selected.get("verdict", "MIXED")
        strict_safe = bool(selected.get("strict_safe", False))
        testnet_safe = bool(selected.get("testnet_safe", False))
    else:
        selected = buy if preferred == "BUY" else sell if preferred == "SELL" else buy
        verdict = "OBSERVE_" + preferred + "_BIAS" if preferred in ("BUY", "SELL") else "OBSERVE"
        strict_safe = False
        testnet_safe = False

    result = dict(selected)
    result.update({
        "version": GEOMETRIC_STABILITY_REVERSAL_VERSION, "ready": True, "action": raw_action,
        "verdict": verdict, "strict_safe": strict_safe, "testnet_safe": testnet_safe,
        "geometry_preferred_action": preferred, "geometry_bias_score": round(max(buy_q, sell_q), 4),
        "counterfactual": {
            "BUY": {
                "continuation_index": buy.get("continuation_index", 0.0),
                "reversal_index": buy.get("reversal_index", 0.0),
                "stability_index": buy.get("stability_index", 0.0),
                "direction_alignment": buy.get("direction_alignment", 0.0),
                "topology_support": buy.get("topology_support", 0.0),
                "persistence": buy.get("persistence", 0.0),
                "strict_safe": bool(buy.get("strict_safe", False)),
                "testnet_safe": bool(buy.get("testnet_safe", False)),
                "quality": round(buy_q, 4),
            },
            "SELL": {
                "continuation_index": sell.get("continuation_index", 0.0),
                "reversal_index": sell.get("reversal_index", 0.0),
                "stability_index": sell.get("stability_index", 0.0),
                "direction_alignment": sell.get("direction_alignment", 0.0),
                "topology_support": sell.get("topology_support", 0.0),
                "persistence": sell.get("persistence", 0.0),
                "strict_safe": bool(sell.get("strict_safe", False)),
                "testnet_safe": bool(sell.get("testnet_safe", False)),
                "quality": round(sell_q, 4),
            },
        },
        "research_only_signal": True,
    })
    return result


def _pfl1_state_token(value):
    v = str(value or "FLAT").upper()
    if v in ("UP", "U"):
        return "U", 1
    if v in ("DOWN", "D"):
        return "D", -1
    return "F", 0


def _pfl1_ctd_states(ctd):
    hs = ctd.get("horizons", {}) if isinstance(ctd, dict) else {}
    tokens = []
    numeric = []
    for h in PFL1_HORIZONS:
        row = hs.get(str(h), {}) if isinstance(hs, dict) else {}
        token, number = _pfl1_state_token(row.get("tilt_state", "FLAT"))
        tokens.append(token)
        numeric.append(number)
    return tokens, numeric


def _pfl1_compact_sequence(samples, current_pattern):
    seq = []
    for sample in list(samples)[-8:] + [{"pattern": current_pattern}]:
        p = str(sample.get("pattern", "F-F-F-F-F"))
        if not seq or seq[-1] != p:
            seq.append(p)
    return seq[-6:]


def compute_phase_front_lag(state, close_time_ms):
    global phase_front_tracker
    geometry = state.get("geometry_state", {})
    ctd = geometry.get("cone_transition_dynamics", {}) if isinstance(geometry, dict) else {}
    if not isinstance(ctd, dict) or not ctd.get("ready", False):
        return {
            "version": PHASE_FRONT_LAG_VERSION, "ready": False, "state": "WARMUP",
            "front_direction": "NONE", "propagation_mode": "NONE", "strength": 0.0,
            "sequence": [], "research_only": True,
        }

    now_ms = int(close_time_ms)
    tokens, numbers = _pfl1_ctd_states(ctd)
    pattern = "-".join(tokens)
    samples = list(phase_front_tracker.get("samples", []) or [])
    prev = samples[-1] if samples else None
    prev_tokens = list(prev.get("tokens", [])) if isinstance(prev, dict) else []
    current_events = []
    next_id = int(phase_front_tracker.get("next_event_id", 1) or 1)

    if len(prev_tokens) == len(PFL1_HORIZONS):
        for idx, h in enumerate(PFL1_HORIZONS):
            old_token = str(prev_tokens[idx])
            new_token = tokens[idx]
            if old_token == new_token:
                continue
            old_n = 1 if old_token == "U" else -1 if old_token == "D" else 0
            new_n = numbers[idx]
            direction = "UP" if new_n > old_n else "DOWN"
            event = {
                "event_id": f"PF{next_id}", "time_ms": now_ms, "horizon": int(h),
                "from": old_token, "to": new_token, "direction": direction,
                "delta": int(new_n - old_n),
            }
            next_id += 1
            current_events.append(event)

    past_events = list(phase_front_tracker.get("events", []) or [])
    new_links = []
    for event in current_events:
        candidates = []
        for old in reversed(past_events[-PFL1_HISTORY:]):
            if str(old.get("direction")) != event["direction"]:
                continue
            if int(old.get("horizon", 0) or 0) == int(event["horizon"]):
                continue
            dt = (now_ms - int(old.get("time_ms", now_ms))) / 60000.0
            if dt <= 0.0 or dt > PFL1_LINK_WINDOW_MIN:
                continue
            velocity = math.log2(float(event["horizon"]) / float(old["horizon"])) / dt
            candidates.append((dt, abs(velocity), old, velocity))
        if candidates:
            candidates.sort(key=lambda x: (x[0], -x[1]))
            dt, _, old, velocity = candidates[0]
            mode = "MICRO_TO_MACRO" if velocity > 1e-9 else "MACRO_TO_MICRO" if velocity < -1e-9 else "CROSS_SCALE"
            new_links.append({
                "time_ms": now_ms, "direction": event["direction"], "mode": mode,
                "from_h": int(old["horizon"]), "to_h": int(event["horizon"]),
                "latency_minutes": round(dt, 4),
                "velocity_log2h_per_min": round(velocity, 6),
                "from_event": old.get("event_id"), "to_event": event.get("event_id"),
            })

    # Multiple same-direction transitions in one candle are a synchronous front.
    if len(current_events) >= 2:
        dirs = [e["direction"] for e in current_events]
        if len(set(dirs)) == 1:
            h_values = sorted(int(e["horizon"]) for e in current_events)
            new_links.append({
                "time_ms": now_ms, "direction": dirs[0], "mode": "CROSS_SCALE",
                "from_h": h_values[0], "to_h": h_values[-1], "latency_minutes": 0.0,
                "velocity_log2h_per_min": 0.0,
                "from_event": current_events[0].get("event_id"),
                "to_event": current_events[-1].get("event_id"),
                "simultaneous": True,
            })

    prior_links = list(phase_front_tracker.get("front_links", []) or [])
    link_pool = prior_links + new_links
    active_link = None
    active_age = None
    if link_pool:
        valid = []
        for link in link_pool[-PFL1_HISTORY:]:
            age = max(0.0, (now_ms - int(link.get("time_ms", now_ms))) / 60000.0)
            if age <= PFL1_STALE_MIN:
                valid.append((age, link))
        if valid:
            valid.sort(key=lambda x: (x[0], -abs(float(x[1].get("velocity_log2h_per_min", 0.0)))))
            active_age, active_link = valid[0]

    topology = ctd.get("phase_topology", {}) if isinstance(ctd, dict) else {}
    coherence = max(0.0, min(1.0, float(ctd.get("rotation_coherence", 0.0) or 0.0)))
    rot_e = max(0.0, float(ctd.get("rotation_energy_deg_per_min", 0.0) or 0.0))
    rot_norm = min(1.0, rot_e / max(CONE_DYNAMICS_ROT_FAST_DEG_PER_MIN, 1e-9))
    boundary_count = int(topology.get("boundary_count", 0) or 0) if isinstance(topology, dict) else 0
    boundary_factor = min(1.0, boundary_count / 2.0)

    if active_link is not None:
        direction = str(active_link.get("direction", "NONE"))
        mode = str(active_link.get("mode", "NONE"))
        from_h = int(active_link.get("from_h", 0) or 0)
        to_h = int(active_link.get("to_h", 0) or 0)
        latency = float(active_link.get("latency_minutes", 0.0) or 0.0)
        velocity = float(active_link.get("velocity_log2h_per_min", 0.0) or 0.0)
        age = float(active_age or 0.0)
        span = min(1.0, abs(math.log2(max(to_h, 1) / max(from_h, 1))) / max(math.log2(120 / 5), 1e-9))
        if current_events:
            consistency = sum(1 for e in current_events if e["direction"] == direction) / len(current_events)
        else:
            consistency = coherence
        freshness = max(0.0, 1.0 - age / max(PFL1_STALE_MIN, 1e-9))
        strength = freshness * (
            0.30 * coherence + 0.22 * rot_norm + 0.22 * span
            + 0.16 * consistency + 0.10 * boundary_factor
        )
        front_h = to_h
        state_name = "PROPAGATING" if mode != "CROSS_SCALE" else "SYNCHRONOUS_FRONT"
    elif current_events:
        counts = {"UP": 0, "DOWN": 0}
        for event in current_events:
            counts[event["direction"]] += 1
        direction = "UP" if counts["UP"] > counts["DOWN"] else "DOWN" if counts["DOWN"] > counts["UP"] else "MIXED"
        front_h = int(max(current_events, key=lambda e: abs(int(e.get("delta", 0))))["horizon"])
        mode = "LOCAL"
        latency = 0.0
        velocity = 0.0
        age = 0.0
        consistency = max(counts.values()) / max(1, len(current_events))
        strength = 0.45 * coherence + 0.30 * rot_norm + 0.25 * consistency
        state_name = "LOCAL_DEFORMATION"
        from_h = front_h
        to_h = front_h
    else:
        prop = ctd.get("propagation", {}) if isinstance(ctd, dict) else {}
        direction = str(prop.get("direction", "NONE"))
        mode = str(prop.get("mode", "NONE"))
        front_h = int(ctd.get("shock_horizon") or 0)
        latency = 0.0
        velocity = float(prop.get("graph", {}).get("net_scale_velocity_log2_per_min", 0.0) or 0.0) if isinstance(prop, dict) else 0.0
        age = PFL1_STALE_MIN
        strength = 0.20 * coherence if direction in ("UP", "DOWN") else 0.0
        state_name = "QUIET"
        from_h = front_h
        to_h = front_h

    strength = max(0.0, min(1.0, strength))
    sequence = _pfl1_compact_sequence(samples, pattern)
    forecast_bias = direction if direction in ("UP", "DOWN") and strength >= 0.35 else "NONE"

    sample = {
        "time_ms": now_ms, "pattern": pattern, "tokens": tokens,
        "rotation_energy": round(rot_e, 4), "coherence": round(coherence, 4),
        "topology_class": str(topology.get("topology_class", "UNRESOLVED_BOUNDARY_STATE")) if isinstance(topology, dict) else "UNRESOLVED_BOUNDARY_STATE",
    }
    samples.append(sample)
    past_events.extend(current_events)
    prior_links.extend(new_links)
    phase_front_tracker = {
        "last_close_time_ms": now_ms,
        "samples": samples[-PFL1_HISTORY:],
        "events": past_events[-PFL1_HISTORY:],
        "front_links": prior_links[-PFL1_HISTORY:],
        "next_event_id": next_id,
        "last_pattern": pattern,
    }

    result = {
        "version": PHASE_FRONT_LAG_VERSION, "ready": True, "state": state_name,
        "pattern": pattern, "sequence": sequence,
        "sequence_path": ">".join(sequence),
        "front_direction": direction, "forecast_bias": forecast_bias,
        "propagation_mode": mode, "from_horizon": from_h, "front_horizon": to_h,
        "latency_minutes": round(latency, 4),
        "velocity_log2h_per_min": round(velocity, 6),
        "age_minutes": round(age, 4), "strength": round(strength, 4),
        "coherence": round(coherence, 4), "rotation_energy_deg_per_min": round(rot_e, 4),
        "boundary_count": boundary_count, "current_events": current_events,
        "new_links": new_links, "research_only": True,
    }
    return result


def _eh1_pfl_score(state, horizon, action):
    pfl = state.get("phase_front_lag", {})
    if not isinstance(pfl, dict) or not pfl.get("ready", False):
        return 0.0, False, {"direction": "NONE", "strength": 0.0, "mode": "NONE"}
    direction = str(pfl.get("front_direction", "NONE"))
    strength = max(0.0, min(1.0, float(pfl.get("strength", 0.0) or 0.0)))
    target = "UP" if action == "BUY" else "DOWN"
    front_h = int(pfl.get("front_horizon", 0) or 0)
    mode = str(pfl.get("propagation_mode", "NONE"))
    conflict = direction in ("UP", "DOWN") and direction != target and strength >= PFL1_STRONG
    if direction not in ("UP", "DOWN"):
        return 0.0, conflict, {"direction": direction, "strength": round(strength,4), "mode": mode, "front_horizon": front_h}
    if direction != target:
        return 0.0, conflict, {"direction": direction, "strength": round(strength,4), "mode": mode, "front_horizon": front_h}
    reach = 0.65
    if front_h > 0:
        if mode == "MICRO_TO_MACRO":
            reach = 1.0 if int(horizon) >= front_h else 0.45
        elif mode == "MACRO_TO_MICRO":
            reach = 1.0 if int(horizon) <= front_h else 0.45
        elif mode == "CROSS_SCALE":
            reach = 0.90
    score = max(0.0, min(1.0, strength * (0.55 + 0.45 * reach)))
    return score, conflict, {"direction": direction, "strength": round(strength,4), "mode": mode, "front_horizon": front_h}


def _eh1_evidence_score(evidence):
    if not evidence:
        return 0.0
    best = evidence[0]
    return min(1.0,
        0.5 * float(best.get("rate", 0.0)) / max(ERL1_MIN_FLAG_RATE, 1e-9)
        + 0.5 * min(1.0, float(best.get("samples", 0.0)) / 50.0))


def _eh1_local_geometry(state, horizon, action):
    cone = state.get("geometry_state", {}).get("cone_model", {})
    models = cone.get("horizons", {}) if isinstance(cone, dict) else {}
    model = models.get(str(int(horizon)), {}) if isinstance(models, dict) else {}
    if not isinstance(model, dict) or "tilt_deg" not in model:
        return 0.0, {"tilt_deg": 0.0, "eccentricity": 0.0, "pressure": 0.0, "state": "NONE"}
    tilt = float(model.get("tilt_deg", 0.0) or 0.0)
    ecc = max(0.0, min(1.0, float(model.get("eccentricity", 0.0) or 0.0)))
    pressure = max(0.0, min(1.0, float(model.get("pressure", 0.0) or 0.0)))
    sign = 1 if tilt >= CONE_DYNAMICS_STATE_DEADZONE_DEG else -1 if tilt <= -CONE_DYNAMICS_STATE_DEADZONE_DEG else 0
    target = 1 if action == "BUY" else -1
    strength = min(1.0, abs(tilt) / max(EH1_LOCAL_TILT_FULL_DEG, 1e-9))
    if sign == target:
        directional = 0.30 + 0.70 * strength
    elif sign == 0:
        directional = 0.20
    else:
        directional = max(0.0, 0.15 * (1.0 - strength))
    score = max(0.0, min(1.0, directional * (0.65 + 0.20 * ecc + 0.15 * pressure)))
    return score, {
        "tilt_deg": round(tilt, 3), "eccentricity": round(ecc, 4),
        "pressure": round(pressure, 4), "state": "UP" if sign > 0 else "DOWN" if sign < 0 else "FLAT",
    }


def edge_gate_at_horizon(regime, strategy, action, horizon):
    horizon = int(horizon)
    if action == "HOLD":
        return {"allowed": True, "reason": "HOLD", "horizon_info": {
            "horizon": horizon, "source": "EH1_HOLD", "validated": True,
            "avg_trade_net_edge_pct": 0.0, "min_samples_across_horizons": 0}}
    if not REQUIRE_VALIDATED_POSITIVE_EDGE:
        return {"allowed": True, "reason": "EDGE_GATE_DISABLED", "horizon_info": {
            "horizon": horizon, "source": "EH1_MATRIX", "validated": False,
            "avg_trade_net_edge_pct": None, "min_samples_across_horizons": 0}}
    cell = get_matrix_cell(regime, strategy, horizon)
    samples = int(cell.get("eligible_trade_samples", 0) or 0) if isinstance(cell, dict) else 0
    edge = cell.get("avg_trade_net_edge_pct") if isinstance(cell, dict) else None
    validated = samples >= MIN_ADAPTIVE_TRADE_SAMPLES
    info = {"horizon": horizon, "source": "EH1_MATRIX", "validated": validated,
            "avg_trade_net_edge_pct": edge, "min_samples_across_horizons": samples}
    if not validated:
        return {"allowed": False, "reason": "EDGE_NOT_VALIDATED", "horizon_info": info}
    if edge is None or float(edge) <= POSITIVE_EDGE_EPSILON_PCT:
        return {"allowed": False, "reason": "NO_POSITIVE_ESTIMATED_EDGE", "horizon_info": info}
    return {"allowed": True, "reason": "POSITIVE_EDGE_VALIDATED", "horizon_info": info}


def compute_execution_horizon_arbitration(state):
    raw_action = str(state.get("action", "HOLD")).upper()
    features = state.get("state_features", {})
    regime = str(state.get("regime", "RANGE"))
    strategy = str(state.get("chosen_strategy", "NONE"))
    gsr = state.get("geometric_stability_reversal", {})
    cf = gsr.get("counterfactual", {}) if isinstance(gsr, dict) else {}
    rows = []

    for horizon in SHADOW_HORIZONS:
        evidence = erl1_flag_evidence(state, horizon)
        evidence_score = _eh1_evidence_score(evidence)
        cell = get_matrix_cell(regime, strategy, horizon) if strategy != "NONE" else None
        samples = int(cell.get("eligible_trade_samples", 0) or 0) if isinstance(cell, dict) else 0
        edge = cell.get("avg_trade_net_edge_pct") if isinstance(cell, dict) else None
        edge_validated = samples >= MIN_ADAPTIVE_TRADE_SAMPLES
        edge_positive = edge_validated and edge is not None and float(edge) > POSITIVE_EDGE_EPSILON_PCT
        edge_score = 1.0 if edge_positive else 0.25 if edge_validated else 0.0

        for action in ("BUY", "SELL"):
            tg = tradeability_gate(features, horizon, action)
            cost_cov = max(0.0, float(tg.get("score", 0.0) or 0.0))
            econ_score = min(1.0, cost_cov)
            local_geom, geom_info = _eh1_local_geometry(state, horizon, action)
            side_gsr = cf.get(action, {}) if isinstance(cf, dict) else {}
            gi = max(0.0, min(1.0, float(side_gsr.get("continuation_index", 0.0) or 0.0)))
            ri = max(0.0, min(1.0, float(side_gsr.get("reversal_index", 0.0) or 0.0)))
            gsr_quality = gi * (1.0 - 0.70 * ri)
            pfl_score, pfl_conflict, pfl_info = _eh1_pfl_score(state, horizon, action)
            score = max(0.0, min(1.0,
                0.29 * econ_score + 0.16 * evidence_score + 0.16 * local_geom
                + 0.20 * gsr_quality + 0.10 * edge_score + 0.09 * pfl_score))
            strict_geometry = bool(side_gsr.get("strict_safe", False))
            testnet_geometry = bool(side_gsr.get("testnet_safe", False))
            ready = (cost_cov >= EH1_READY_COST_COVERAGE and score >= EH1_READY_SCORE
                     and strict_geometry and bool(evidence) and edge_positive and not pfl_conflict)
            candidate = (cost_cov >= EH1_MIN_CANDIDATE_COST_COVERAGE
                         and score >= EH1_MIN_CANDIDATE_SCORE and testnet_geometry and not pfl_conflict)
            status = "READY" if ready else "CANDIDATE" if candidate else "OBSERVE" if score >= 0.25 else "REJECT"
            rows.append({
                "horizon": int(horizon), "action": action, "score": round(score, 4), "status": status,
                "cost_coverage": round(cost_cov, 4), "motion_budget_pct": tg.get("motion_budget_pct"),
                "required_move_pct": tg.get("required_move_pct"), "evidence_score": round(evidence_score, 4),
                "validated_flags": evidence[:3], "local_geometry_score": round(local_geom, 4),
                "local_geometry": geom_info, "gsr_continuation": round(gi, 4),
                "gsr_reversal": round(ri, 4), "gsr_quality": round(gsr_quality, 4),
                "edge_validated": edge_validated, "edge_positive": edge_positive,
                "edge_samples": samples, "avg_trade_net_edge_pct": edge,
                "pfl_score": round(pfl_score, 4), "pfl_conflict": bool(pfl_conflict),
                "pfl": pfl_info,
            })

    rows.sort(key=lambda r: (r["score"], r["cost_coverage"], -r["horizon"]), reverse=True)
    selected = rows[0] if rows else None
    raw_rows = [r for r in rows if r.get("action") == raw_action] if raw_action in ("BUY", "SELL") else []
    execution_candidate = raw_rows[0] if raw_rows else None
    if not selected:
        return {"version": EXECUTION_HORIZON_ARBITRATION_VERSION, "ready": False,
                "raw_action": raw_action, "selected_action": "NONE", "selected_horizon": TRADE_HORIZON,
                "selected_score": 0.0, "status": "WARMUP", "rows": []}
    return {
        "version": EXECUTION_HORIZON_ARBITRATION_VERSION, "ready": True, "raw_action": raw_action,
        "selected_action": selected["action"], "selected_horizon": selected["horizon"],
        "selected_score": selected["score"], "status": selected["status"],
        "direction_conflict": raw_action in ("BUY", "SELL") and selected["action"] != raw_action,
        "execution_horizon": execution_candidate["horizon"] if execution_candidate else TRADE_HORIZON,
        "execution_score": execution_candidate["score"] if execution_candidate else 0.0,
        "execution_status": execution_candidate["status"] if execution_candidate else "HOLD",
        "execution_candidate": dict(execution_candidate) if execution_candidate else None,
        "rows": rows, "research_only_direction": raw_action == "HOLD",
    }


def compute_economic_front_surface(state, close_time_ms):
    """Research-only economic surface across execution horizons.

    Uses only information available at the current state (EH1 rows / motion
    budget / required move). It does not use future GOL2 outcomes and never
    changes the action or any hard execution gate.
    """
    global economic_front_tracker
    eh = state.get("execution_horizon_arbitration", {})
    eh_rows = eh.get("rows", []) if isinstance(eh, dict) else []
    if not eh_rows:
        return {
            "version": ECONOMIC_FRONT_SURFACE_VERSION, "ready": False,
            "state": "WARMUP", "peak_horizon": None,
            "peak_cost_coverage": 0.0, "peak_direction": "NONE",
            "peak_drift": "NONE", "rows": [], "research_only": True,
        }

    rows = []
    for h in EFS1_HORIZONS:
        candidates = [r for r in eh_rows if int(r.get("horizon", 0) or 0) == int(h)]
        if not candidates:
            continue
        # Direction follows best current structural/economic quality; coverage
        # stays explicit so direction quality cannot masquerade as economics.
        best_q = max(candidates, key=lambda r: (float(r.get("score", 0.0) or 0.0), float(r.get("cost_coverage", 0.0) or 0.0)))
        best_cov = max(candidates, key=lambda r: (float(r.get("cost_coverage", 0.0) or 0.0), float(r.get("score", 0.0) or 0.0)))
        coverage = max(0.0, float(best_cov.get("cost_coverage", 0.0) or 0.0))
        motion = float(best_cov.get("motion_budget_pct", 0.0) or 0.0)
        required = float(best_cov.get("required_move_pct", 0.0) or 0.0)
        surplus = motion - required
        classification = "COVERED" if coverage >= EFS1_COVERED else "NEAR_COST" if coverage >= EFS1_NEAR_COST else "SUBCOST"
        geom = best_q.get("local_geometry", {}) if isinstance(best_q.get("local_geometry", {}), dict) else {}
        rows.append({
            "horizon": int(h),
            "direction": str(best_q.get("action", "NONE")),
            "quality": round(float(best_q.get("score", 0.0) or 0.0), 4),
            "cost_coverage": round(coverage, 4),
            "motion_budget_pct": round(motion, 6),
            "required_move_pct": round(required, 6),
            "surplus_to_cost_pct": round(surplus, 6),
            "classification": classification,
            "tilt_deg": round(float(geom.get("tilt_deg", 0.0) or 0.0), 3),
            "eccentricity": round(float(geom.get("eccentricity", 0.0) or 0.0), 4),
            "pressure": round(float(geom.get("pressure", 0.0) or 0.0), 4),
            "geometry_state": str(geom.get("state", "NONE")),
            "pfl_score": round(float(best_q.get("pfl_score", 0.0) or 0.0), 4),
            "gsr_continuation": round(float(best_q.get("gsr_continuation", 0.0) or 0.0), 4),
            "gsr_reversal": round(float(best_q.get("gsr_reversal", 0.0) or 0.0), 4),
        })

    rows.sort(key=lambda r: r["horizon"])
    for i, row in enumerate(rows):
        if i == 0:
            row["coverage_gradient_log2h"] = 0.0
            continue
        prev = rows[i - 1]
        dx = math.log2(float(row["horizon"]) / float(prev["horizon"]))
        row["coverage_gradient_log2h"] = round((row["cost_coverage"] - prev["cost_coverage"]) / max(dx, 1e-9), 6)

    peak = max(rows, key=lambda r: (r["cost_coverage"], r["quality"])) if rows else None
    now_ms = int(close_time_ms)
    prev_h = economic_front_tracker.get("last_peak_horizon")
    peak_h = int(peak["horizon"]) if peak else None
    if prev_h is None or peak_h is None or int(prev_h) == peak_h:
        drift = "STABLE"
        drift_v = 0.0
    else:
        prev_t = economic_front_tracker.get("last_close_time_ms")
        dt = max(1e-9, (now_ms - int(prev_t or now_ms)) / 60000.0)
        drift_v = math.log2(float(peak_h) / float(prev_h)) / dt
        drift = "MICRO_TO_MACRO" if drift_v > 1e-9 else "MACRO_TO_MICRO"

    history = list(economic_front_tracker.get("history", []) or [])
    history.append({
        "time_ms": now_ms, "peak_horizon": peak_h,
        "peak_cost_coverage": round(float(peak.get("cost_coverage", 0.0) if peak else 0.0), 4),
        "peak_direction": str(peak.get("direction", "NONE") if peak else "NONE"),
        "peak_drift": drift,
    })
    economic_front_tracker = {
        "last_close_time_ms": now_ms,
        "last_peak_horizon": peak_h,
        "last_peak_coverage": round(float(peak.get("cost_coverage", 0.0) if peak else 0.0), 4),
        "history": history[-EFS1_HISTORY:],
    }
    state_name = "COVERED" if peak and peak["cost_coverage"] >= EFS1_COVERED else "NEAR_COST" if peak and peak["cost_coverage"] >= EFS1_NEAR_COST else "SUBCOST"
    return {
        "version": ECONOMIC_FRONT_SURFACE_VERSION,
        "ready": bool(rows), "state": state_name,
        "peak_horizon": peak_h,
        "peak_cost_coverage": round(float(peak.get("cost_coverage", 0.0) if peak else 0.0), 4),
        "peak_direction": str(peak.get("direction", "NONE") if peak else "NONE"),
        "peak_quality": round(float(peak.get("quality", 0.0) if peak else 0.0), 4),
        "peak_drift": drift,
        "peak_drift_log2h_per_min": round(drift_v, 6),
        "rows": rows,
        "research_only": True,
    }


def _bpm_clamp(value, lo=-1.0, hi=1.0):
    try:
        return max(lo, min(hi, float(value)))
    except Exception:
        return 0.0


def _bpm_dir(value):
    v = str(value or "").upper()
    if v in ("UP", "BUY", "+", "POSITIVE"):
        return 1.0
    if v in ("DOWN", "SELL", "-", "NEGATIVE"):
        return -1.0
    return 0.0


def _bpm_discrete_sign(q):
    q = float(q or 0.0)
    if q > BPM1_ZERO_DEADBAND:
        return 1
    if q < -BPM1_ZERO_DEADBAND:
        return -1
    return 0


def _bpm_state_label(q, intensity):
    q = float(q or 0.0)
    intensity = max(0.0, min(1.0, float(intensity or 0.0)))
    if abs(q) <= BPM1_ZERO_DEADBAND:
        return "TENSE_BALANCE" if intensity >= BPM1_STABLE_BALANCE_I else "QUIET_BALANCE"
    if q > 0.0:
        return "UP_PRESSURE"
    return "DOWN_PRESSURE"


def _bpm_source(name, q, intensity, weight):
    q = _bpm_clamp(q)
    intensity = max(0.0, min(1.0, float(intensity or 0.0)))
    weight = max(0.0, float(weight or 0.0))
    signed = q * intensity * weight
    return {
        "name": str(name),
        "q": round(q, 6),
        "intensity": round(intensity, 6),
        "weight": round(weight, 6),
        "signed_contribution": round(signed, 6),
    }


def _bpm_combine_sources(sources):
    pos = 0.0
    neg = 0.0
    capacity = 0.0
    for src in sources:
        w = max(0.0, float(src.get("weight", 0.0) or 0.0))
        capacity += w
        c = float(src.get("signed_contribution", 0.0) or 0.0)
        if c >= 0.0:
            pos += c
        else:
            neg += -c
    raw = pos + neg
    q = (pos - neg) / max(raw, 1e-12) if raw > 1e-12 else 0.0
    intensity = min(1.0, raw / max(capacity, 1e-12)) if capacity > 0.0 else 0.0
    push = q * intensity
    tension = intensity * (1.0 - abs(q))
    return {
        "positive_mass": round(pos, 6),
        "negative_mass": round(neg, 6),
        "q": round(_bpm_clamp(q), 6),
        "I": round(intensity, 6),
        "P": round(_bpm_clamp(push), 6),
        "tension": round(max(0.0, min(1.0, tension)), 6),
    }


def compute_bipolar_pressure(state, close_time_ms):
    """BPM1: common signed field for geometry modules.

    q encodes direction only, I encodes how much directional evidence/activity
    is present, and P=q*I is the resulting structural push. The model is
    observational/research-only in v1.23 and is deliberately excluded from
    execution gates until its outcome calibration is measured.
    """
    global bipolar_pressure_tracker
    state = state if isinstance(state, dict) else {}
    geometry = state.get("geometry_state", {}) or {}
    cone = geometry.get("cone_model", {}) or {}
    cone_h = cone.get("horizons", {}) if isinstance(cone.get("horizons", {}), dict) else {}
    ctd = geometry.get("cone_transition_dynamics", {}) or {}
    ctd_h = ctd.get("horizons", {}) if isinstance(ctd.get("horizons", {}), dict) else {}
    spt = ctd.get("phase_topology", {}) or {}
    pattern_tokens = str(spt.get("state_pattern", ctd.get("state_pattern", "F-F-F-F-F"))).split("-")
    pfl = state.get("phase_front_lag", {}) or {}
    gsr = state.get("geometric_stability_reversal", {}) or {}
    efs = state.get("economic_front_surface", {}) or {}
    efs_rows = efs.get("rows", []) or []
    cf = gsr.get("counterfactual", {}) if isinstance(gsr.get("counterfactual", {}), dict) else {}
    buy_q = float((cf.get("BUY", {}) or {}).get("quality", 0.0) or 0.0)
    sell_q = float((cf.get("SELL", {}) or {}).get("quality", 0.0) or 0.0)
    gsr_den = buy_q + sell_q
    gsr_signed = (buy_q - sell_q) / max(gsr_den, 1e-12) if gsr_den > 1e-12 else 0.0
    gsr_intensity = min(1.0, max(buy_q, sell_q))
    rotation_coh = max(0.0, min(1.0, float(ctd.get("rotation_coherence", 0.0) or 0.0)))
    pfl_sign = _bpm_dir(pfl.get("front_direction"))
    pfl_strength = max(0.0, min(1.0, float(pfl.get("strength", 0.0) or 0.0)))
    pfl_front_h = int(pfl.get("front_horizon", 0) or 0)
    pfl_from_h = int(pfl.get("from_horizon", 0) or 0)
    pfl_anchor_h = pfl_front_h or pfl_from_h or 5
    action = str(state.get("action", "HOLD")).upper()
    strategy_q = _bpm_dir(action)
    strategy_I = max(0.0, min(1.0, float(state.get("p_success", 0.0) or 0.0))) if strategy_q else 0.0

    now_ms = int(close_time_ms)
    prev_t = bipolar_pressure_tracker.get("last_close_time_ms")
    dt = max(1e-9, (now_ms - int(prev_t or now_ms)) / 60000.0) if prev_t is not None else 1.0
    rows = {}
    crossings = []

    for idx, h in enumerate(BPM1_HORIZONS):
        ch = cone_h.get(str(h), {}) or {}
        dh = ctd_h.get(str(h), {}) or {}
        tilt = float(ch.get("tilt_deg", 0.0) or 0.0)
        ecc = max(0.0, min(1.0, float(ch.get("eccentricity", 0.0) or 0.0)))
        pressure = max(0.0, min(1.0, float(ch.get("pressure", 0.0) or 0.0)))
        tilt_q = _bpm_clamp(tilt / BPM1_TILT_FULL_DEG)
        tilt_I = min(1.0, 0.45 + 0.30 * ecc + 0.25 * pressure) if ch.get("ready", True) else 0.0

        omega = float(dh.get("omega_deg_per_min", 0.0) or 0.0)
        rot_q = _bpm_clamp(omega / BPM1_OMEGA_FULL_DEG_PER_MIN)
        rot_I = rotation_coh

        token = pattern_tokens[idx] if idx < len(pattern_tokens) else "F"
        spt_q = 1.0 if token == "U" else (-1.0 if token == "D" else 0.0)
        spt_I = 1.0 if token in ("U", "D") else 0.25

        if pfl_sign != 0.0 and pfl_strength > 0.0:
            dist = abs(math.log2(float(h) / float(max(1, pfl_anchor_h)))) if h != pfl_anchor_h else 0.0
            pfl_relevance = math.exp(-dist / 1.5)
        else:
            pfl_relevance = 0.0
        pfl_q = pfl_sign
        pfl_I = pfl_strength * pfl_relevance

        erow = next((r for r in efs_rows if int(r.get("horizon", 0) or 0) == int(h)), {})
        efs_dir = _bpm_dir(erow.get("direction"))
        efs_cov = max(0.0, float(erow.get("cost_coverage", 0.0) or 0.0))
        efs_quality = max(0.0, min(1.0, float(erow.get("quality", 0.0) or 0.0)))
        efs_q = efs_dir * min(1.0, efs_cov / BPM1_EFS_FULL_COVERAGE)
        efs_I = efs_quality

        sources = [
            _bpm_source("TILT", tilt_q, tilt_I, 1.00),
            _bpm_source("CTD_ROT", rot_q, rot_I, 0.85),
            _bpm_source("SPT", spt_q, spt_I, 0.65),
            _bpm_source("PFL", pfl_q, pfl_I, 0.80),
            _bpm_source("GSR", gsr_signed, gsr_intensity, 0.90),
            _bpm_source("EFS", efs_q, efs_I, 0.80),
        ]
        combined = _bpm_combine_sources(sources)
        q = float(combined["q"])
        I = float(combined["I"])
        prev = (bipolar_pressure_tracker.get("by_horizon", {}) or {}).get(str(h), {}) or {}
        prev_q = float(prev.get("q", q) or q)
        prev_dq = float(prev.get("dq_per_min", 0.0) or 0.0)
        dq = (q - prev_q) / dt if prev_t is not None else 0.0
        ddq = (dq - prev_dq) / dt if prev_t is not None else 0.0
        sign = _bpm_discrete_sign(q)
        last_nonzero = int(prev.get("last_nonzero_sign", 0) or 0)
        crossed = bool(sign and last_nonzero and sign != last_nonzero)
        sign_age = float(prev.get("sign_age_minutes", 0.0) or 0.0)
        if sign == 0:
            sign_age = 0.0
        elif sign == _bpm_discrete_sign(prev_q):
            sign_age += dt
        else:
            sign_age = 0.0
        cross_score = 0.0
        if crossed:
            cross_score = min(1.0, abs(dq) / BPM1_CROSS_FULL_Q_PER_MIN) * I
            crossings.append({
                "horizon": h,
                "from_sign": last_nonzero,
                "to_sign": sign,
                "dq_per_min": round(dq, 6),
                "I": round(I, 6),
                "score": round(cross_score, 6),
            })
        next_nonzero = sign if sign else last_nonzero
        bipolar_pressure_tracker.setdefault("by_horizon", {})[str(h)] = {
            "q": round(q, 6),
            "dq_per_min": round(dq, 6),
            "last_nonzero_sign": next_nonzero,
            "sign_age_minutes": round(sign_age, 6),
        }
        rows[str(h)] = {
            "horizon": h,
            **combined,
            "state": _bpm_state_label(q, I),
            "dq_per_min": round(dq, 6),
            "ddq_per_min2": round(ddq, 6),
            "sign": sign,
            "sign_age_minutes": round(sign_age, 4),
            "zero_cross": crossed,
            "cross_score": round(cross_score, 6),
            "sources": sources,
        }

    weighted = sum(float(r["q"]) * float(r["I"]) for r in rows.values())
    mass = sum(float(r["I"]) for r in rows.values())
    global_q = weighted / max(mass, 1e-12) if mass > 1e-12 else 0.0
    global_I = min(1.0, mass / max(1.0, float(len(rows))))
    global_P = global_q * global_I
    global_tension = global_I * (1.0 - abs(global_q))
    gp = bipolar_pressure_tracker.get("global", {}) or {}
    prev_gq = float(gp.get("q", global_q) or global_q)
    prev_gdq = float(gp.get("dq_per_min", 0.0) or 0.0)
    gdq = (global_q - prev_gq) / dt if prev_t is not None else 0.0
    gddq = (gdq - prev_gdq) / dt if prev_t is not None else 0.0
    gsign = _bpm_discrete_sign(global_q)
    glast = int(gp.get("last_nonzero_sign", 0) or 0)
    gcross = bool(gsign and glast and gsign != glast)
    gage = float(gp.get("sign_age_minutes", 0.0) or 0.0)
    if gsign == 0:
        gage = 0.0
    elif gsign == _bpm_discrete_sign(prev_gq):
        gage += dt
    else:
        gage = 0.0
    gcross_score = min(1.0, abs(gdq) / BPM1_CROSS_FULL_Q_PER_MIN) * global_I if gcross else 0.0
    if gcross:
        crossings.append({
            "horizon": "GLOBAL", "from_sign": glast, "to_sign": gsign,
            "dq_per_min": round(gdq, 6), "I": round(global_I, 6),
            "score": round(gcross_score, 6),
        })
    bipolar_pressure_tracker["global"] = {
        "q": round(global_q, 6), "dq_per_min": round(gdq, 6),
        "last_nonzero_sign": gsign if gsign else glast,
        "sign_age_minutes": round(gage, 6),
    }
    if crossings:
        bipolar_pressure_tracker["cross_count"] = int(bipolar_pressure_tracker.get("cross_count", 0) or 0) + len(crossings)
    hist = list(bipolar_pressure_tracker.get("history", []) or [])
    hist.append({
        "time_ms": now_ms,
        "q": round(global_q, 6), "I": round(global_I, 6), "P": round(global_P, 6),
        "tension": round(global_tension, 6),
        "q_by_horizon": {h: r["q"] for h, r in rows.items()},
        "I_by_horizon": {h: r["I"] for h, r in rows.items()},
        "crossings": crossings,
    })
    bipolar_pressure_tracker["history"] = hist[-BPM1_HISTORY:]
    bipolar_pressure_tracker["last_close_time_ms"] = now_ms

    result = {
        "version": BIPOLAR_PRESSURE_VERSION,
        "ready": bool(rows),
        "research_only": True,
        "state": _bpm_state_label(global_q, global_I),
        "q": round(_bpm_clamp(global_q), 6),
        "I": round(global_I, 6),
        "P": round(_bpm_clamp(global_P), 6),
        "tension": round(max(0.0, min(1.0, global_tension)), 6),
        "dq_per_min": round(gdq, 6),
        "ddq_per_min2": round(gddq, 6),
        "sign": gsign,
        "sign_age_minutes": round(gage, 4),
        "zero_cross": gcross,
        "cross_score": round(gcross_score, 6),
        "strategy_q": round(strategy_q, 6),
        "strategy_I": round(strategy_I, 6),
        "horizons": rows,
        "crossings": crossings,
        "definition": {"q": "directional asymmetry [-1,1]", "I": "normalized evidence/activity [0,1]", "P": "q*I", "zero": "balance, not absence"},
    }
    append_jsonl(BPM1_STATES_FILE, {"time": now_iso(), "state_id": state.get("state_id"), **result})
    return result


def run_bpm_selftest():
    global bipolar_pressure_tracker
    saved = copy.deepcopy(bipolar_pressure_tracker)
    try:
        bipolar_pressure_tracker = {
            "version": BIPOLAR_PRESSURE_VERSION, "last_close_time_ms": None,
            "by_horizon": {str(h): {"q":0.0,"dq_per_min":0.0,"last_nonzero_sign":0,"sign_age_minutes":0.0} for h in BPM1_HORIZONS},
            "global": {"q":0.0,"dq_per_min":0.0,"last_nonzero_sign":0,"sign_age_minutes":0.0},
            "history": [], "cross_count": 0,
        }
        def mk(direction):
            sg = 1 if direction == "UP" else -1
            act = "BUY" if sg > 0 else "SELL"
            tok = "U" if sg > 0 else "D"
            return {
                "state_id": "BPMTEST", "action": "HOLD", "p_success": 0.0,
                "geometry_state": {
                    "cone_model": {"horizons": {str(h): {"ready":True,"tilt_deg":sg*48.0,"eccentricity":0.9,"pressure":0.8} for h in BPM1_HORIZONS}},
                    "cone_transition_dynamics": {
                        "rotation_coherence":0.9,
                        "state_pattern":"-".join([tok]*5),
                        "horizons": {str(h): {"omega_deg_per_min":sg*20.0} for h in BPM1_HORIZONS},
                        "phase_topology":{"state_pattern":"-".join([tok]*5)},
                    },
                },
                "phase_front_lag":{"front_direction":direction,"strength":0.8,"front_horizon":60,"from_horizon":5},
                "geometric_stability_reversal":{"counterfactual": {"BUY":{"quality":0.9 if sg>0 else 0.05},"SELL":{"quality":0.9 if sg<0 else 0.05}}},
                "economic_front_surface":{"rows":[{"horizon":h,"direction":act,"cost_coverage":1.2,"quality":0.8} for h in (5,15,30,60)]},
            }
        t0 = 1_800_000_000_000
        down = compute_bipolar_pressure(mk("DOWN"), t0)
        up = compute_bipolar_pressure(mk("UP"), t0 + MINUTE_MS)
        ok = (
            down.get("q", 0) < -0.5 and up.get("q", 0) > 0.5
            and 0.0 <= down.get("I", -1) <= 1.0 and 0.0 <= up.get("I", -1) <= 1.0
            and bool(up.get("zero_cross")) and any(str(x.get("horizon")) == "GLOBAL" for x in up.get("crossings", []))
        )
        return {"ok": ok, "down": {k:down.get(k) for k in ("q","I","P","state")}, "up": {k:up.get(k) for k in ("q","I","P","state","zero_cross","cross_score")}, "crossings": up.get("crossings", [])}
    finally:
        bipolar_pressure_tracker = saved


def _tail_jsonl(filename, limit):
    """Read the last N valid JSONL objects without scanning the whole file."""
    if limit <= 0 or not os.path.exists(filename):
        return []
    try:
        with open(filename, "rb") as f:
            f.seek(0, os.SEEK_END)
            pos = f.tell()
            buf = b""
            chunks = []
            while pos > 0 and len(chunks) <= limit:
                step = min(65536, pos)
                pos -= step
                f.seek(pos)
                buf = f.read(step) + buf
                chunks = buf.splitlines()
                if len(chunks) > limit + 1:
                    break
        out = []
        for raw in chunks[-limit:]:
            try:
                out.append(json.loads(raw.decode("utf-8")))
            except Exception:
                continue
        return out
    except Exception:
        return []


def _safe_write_json(filename, payload):
    try:
        parent = os.path.dirname(filename)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = filename + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, filename)
        return True, filename
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _latest_state_from_storage():
    rows = _tail_jsonl(STATES_FILE, 1)
    return rows[-1] if rows else None


def _cge1_specificity_weight(key):
    key = str(key)
    if "|COMBO_" in key:
        return 1.60
    if "|SPT_" in key or "|PBD_" in key:
        return 1.35
    if "|PFL_" in key or "|PROP_" in key or "|FRONT_" in key or "|SHOCK_" in key:
        return 1.25
    if "|ZONE_" in key or "|ROT_" in key:
        return 1.12
    return 1.0


def compute_conditional_geometry_edge(state):
    """CGE1: current-geometry-conditioned GOL2 evidence, research only.

    The score combines directional gross dominance, observed cost coverage,
    path-level net-positive directional wins and path asymmetry across the
    low-dimensional GOL2 cells active in the current state. It is intentionally
    conservative and does NOT claim a tradable expected net return.
    """
    features = gol_geometry_features(state)
    if not isinstance(features, dict) or not features.get("ready", False):
        return {
            "version": CONDITIONAL_GEOMETRY_EDGE_VERSION,
            "ready": False,
            "status": "WARMUP",
            "selected_action": "NONE",
            "selected_horizon": TRADE_HORIZON,
            "selected_score": 0.0,
            "rows": [],
            "net_edge_claim": False,
        }

    rows = []
    for horizon in SHADOW_HORIZONS:
        keys = gol_feature_keys(features, horizon)
        for action in ("BUY", "SELL"):
            sign = 1.0 if action == "BUY" else -1.0
            matched = []
            wsum = 0.0
            dir_sum = cost_sum = near_sum = path_sum = asym_sum = cov_sum = 0.0
            raw_samples = 0

            for key in keys:
                cell = geometry_outcome_matrix.get(key)
                if not isinstance(cell, dict):
                    continue
                n = int(cell.get("samples", 0) or 0)
                if n < CGE1_MIN_CELL_SAMPLES:
                    continue
                spec = _cge1_specificity_weight(key)
                w = (min(100, n) ** 0.5) * spec
                gross_dir = float(cell.get("gross_up_win_rate" if action == "BUY" else "gross_down_win_rate", 0.0) or 0.0)
                directional_path = float(cell.get("up_path_wins" if action == "BUY" else "down_path_wins", 0) or 0) / max(1, n)
                cost_rate = float(cell.get("gross_cost_covered_rate", 0.0) or 0.0)
                near_rate = float(cell.get("gross_near_cost_rate", 0.0) or 0.0)
                oriented_asym = sign * float(cell.get("avg_path_asymmetry", 0.0) or 0.0)
                avg_cov = float(cell.get("avg_cost_coverage_ratio", 0.0) or 0.0)

                wsum += w
                raw_samples += n
                dir_sum += w * gross_dir
                cost_sum += w * cost_rate
                near_sum += w * near_rate
                path_sum += w * directional_path
                asym_sum += w * oriented_asym
                cov_sum += w * avg_cov
                matched.append({
                    "key": key,
                    "samples": n,
                    "weight": round(w, 4),
                    "gross_direction_rate": round(gross_dir, 4),
                    "directional_path_rate": round(directional_path, 4),
                    "gross_cost_covered_rate": round(cost_rate, 4),
                    "avg_cost_coverage_ratio": round(avg_cov, 4),
                    "oriented_asymmetry": round(oriented_asym, 4),
                })

            cell_count = len(matched)
            validated = cell_count >= CGE1_MIN_MATCHED_CELLS and wsum >= CGE1_MIN_EFFECTIVE_WEIGHT
            if wsum <= 0:
                raw_dir = raw_cost = raw_near = raw_path = raw_asym = raw_cov = 0.0
            else:
                raw_dir = dir_sum / wsum
                raw_cost = cost_sum / wsum
                raw_near = near_sum / wsum
                raw_path = path_sum / wsum
                raw_asym = asym_sum / wsum
                raw_cov = cov_sum / wsum

            # Shrink noisy rates toward neutral / zero rather than letting a
            # handful of overlapping cells look overconfident.
            confidence = min(1.0, wsum / max(CGE1_MIN_EFFECTIVE_WEIGHT * 2.5, 1e-9))
            dir_rate = 0.5 + confidence * (raw_dir - 0.5)
            cost_rate = confidence * raw_cost
            near_rate = confidence * raw_near
            path_rate = confidence * raw_path
            oriented_asym = confidence * raw_asym
            avg_cov = confidence * raw_cov
            asym_score = max(0.0, min(1.0, 0.5 + 0.5 * oriented_asym))
            cov_score = min(1.0, max(0.0, avg_cov))
            score = max(0.0, min(1.0,
                0.38 * dir_rate
                + 0.18 * cost_rate
                + 0.12 * near_rate
                + 0.12 * path_rate
                + 0.10 * asym_score
                + 0.10 * cov_score
            ))

            if not validated:
                status = "UNVALIDATED"
            elif score >= CGE1_SUPPORT_SCORE and dir_rate >= 0.52 and oriented_asym >= -0.05:
                status = "SUPPORT"
            elif score <= CGE1_OPPOSE_SCORE or dir_rate <= 0.44:
                status = "OPPOSE"
            else:
                status = "NEUTRAL"

            matched.sort(key=lambda x: (x["weight"], x["samples"]), reverse=True)
            rows.append({
                "horizon": int(horizon),
                "action": action,
                "validated": bool(validated),
                "status": status,
                "score": round(score, 4),
                "confidence": round(confidence, 4),
                "matched_cells": cell_count,
                "raw_samples_overlapping": raw_samples,
                "effective_weight": round(wsum, 4),
                "directional_gross_rate": round(dir_rate, 4),
                "gross_cost_covered_rate": round(cost_rate, 4),
                "gross_near_cost_rate": round(near_rate, 4),
                "directional_path_rate": round(path_rate, 4),
                "oriented_asymmetry": round(oriented_asym, 4),
                "avg_cost_coverage_ratio": round(avg_cov, 4),
                "top_cells": matched[:8],
            })

    rows.sort(key=lambda r: (r["validated"], r["score"], r["confidence"]), reverse=True)
    selected = rows[0] if rows else None
    return {
        "version": CONDITIONAL_GEOMETRY_EDGE_VERSION,
        "ready": bool(selected),
        "status": selected.get("status", "WARMUP") if selected else "WARMUP",
        "selected_action": selected.get("action", "NONE") if selected else "NONE",
        "selected_horizon": int(selected.get("horizon", TRADE_HORIZON)) if selected else TRADE_HORIZON,
        "selected_score": float(selected.get("score", 0.0)) if selected else 0.0,
        "rows": rows,
        "net_edge_claim": False,
        "research_only": True,
    }


def _aal1_find_eh_row(eh, action, horizon=None):
    rows = list(eh.get("rows", []) or []) if isinstance(eh, dict) else []
    candidates = [r for r in rows if str(r.get("action", "")).upper() == str(action).upper()]
    if horizon is not None:
        exact = [r for r in candidates if int(r.get("horizon", -1) or -1) == int(horizon)]
        if exact:
            return exact[0]
    return candidates[0] if candidates else None


def _aal1_find_cge_row(cge, action, horizon):
    for row in (cge.get("rows", []) or []) if isinstance(cge, dict) else []:
        if str(row.get("action", "")).upper() == str(action).upper() and int(row.get("horizon", -1) or -1) == int(horizon):
            return row
    return None


def compute_action_arbitration(state):
    """AAL1: strategy-vs-geometry decision arbitration.

    Geometry is allowed to veto a conflicting strategy action as a safety
    action (final HOLD). It may originate/reverse direction only in explicitly
    armed relaxed TESTNET and only when the independent economic/edge evidence
    agrees. LIVE never receives a geometry-originated opposite action.
    """
    raw_action = str(state.get("action", "HOLD")).upper()
    strategy = str(state.get("chosen_strategy", "NONE"))
    regime = str(state.get("regime", "RANGE"))
    eh = state.get("execution_horizon_arbitration", {})
    efs = state.get("economic_front_surface", {})
    gsr = state.get("geometric_stability_reversal", {})
    cge = state.get("conditional_geometry_edge", {})

    geometry_action = str(eh.get("selected_action", "NONE")).upper() if isinstance(eh, dict) else "NONE"
    geometry_horizon = int(eh.get("selected_horizon", TRADE_HORIZON) or TRADE_HORIZON) if isinstance(eh, dict) else TRADE_HORIZON
    geo_row = _aal1_find_eh_row(eh, geometry_action, geometry_horizon) or {}
    geo_score = float(geo_row.get("score", 0.0) or 0.0)
    geo_cost = float(geo_row.get("cost_coverage", 0.0) or 0.0)
    geo_local = float(geo_row.get("local_geometry_score", 0.0) or 0.0)
    geo_pfl_conflict = bool(geo_row.get("pfl_conflict", False))
    geo_status = str(geo_row.get("status", "REJECT"))

    cf = gsr.get("counterfactual", {}) if isinstance(gsr, dict) else {}
    geo_gsr = cf.get(geometry_action, {}) if geometry_action in ("BUY", "SELL") and isinstance(cf, dict) else {}
    geo_gsr_quality = float(geo_gsr.get("quality", 0.0) or 0.0)
    geo_gsr_safe = bool(geo_gsr.get("testnet_safe", False))

    efs_h = int(efs.get("peak_horizon", 0) or 0) if isinstance(efs, dict) else 0
    efs_dir = str(efs.get("peak_direction", "NONE")).upper() if isinstance(efs, dict) else "NONE"
    efs_match = geometry_action in ("BUY", "SELL") and efs_h == geometry_horizon and efs_dir == geometry_action

    cge_row = _aal1_find_cge_row(cge, geometry_action, geometry_horizon) or {}
    cge_status = str(cge_row.get("status", "UNVALIDATED"))
    cge_score = float(cge_row.get("score", 0.0) or 0.0)
    cge_validated = bool(cge_row.get("validated", False))
    cge_support = cge_validated and cge_status == "SUPPORT"

    if geometry_action in ("BUY", "SELL") and strategy != "NONE":
        geometry_edge = edge_gate_at_horizon(regime, strategy, geometry_action, geometry_horizon)
    else:
        geometry_edge = {"allowed": False, "reason": "NO_STRATEGY_EDGE_CONTEXT", "horizon_info": {}}
    geometry_edge_allowed = bool(geometry_edge.get("allowed", False))

    strong_geometry = (
        geometry_action in ("BUY", "SELL")
        and geo_score >= AAL1_STRONG_GEOMETRY_SCORE
        and geo_gsr_quality >= AAL1_STRONG_GSR_QUALITY
        and geo_local >= AAL1_STRONG_LOCAL_GEOMETRY
        and geo_cost >= AAL1_STRONG_COST_COVERAGE
        and geo_status in ("OBSERVE", "CANDIDATE", "READY")
    )

    override_evidence = (
        strong_geometry
        and geo_cost >= AAL1_OVERRIDE_COST_COVERAGE
        and geo_gsr_quality >= AAL1_OVERRIDE_GSR_QUALITY
        and geo_gsr_safe
        and efs_match
        and not geo_pfl_conflict
        and (geometry_edge_allowed or not AAL1_REQUIRE_VALIDATED_EDGE)
        and (cge_support or not AAL1_REQUIRE_CGE_SUPPORT)
    )

    conflict = raw_action in ("BUY", "SELL") and geometry_action in ("BUY", "SELL") and raw_action != geometry_action
    blockers = []
    final_action = raw_action if raw_action in ("BUY", "SELL") else "HOLD"
    final_horizon = int(state.get("prediction_horizon", TRADE_HORIZON) or TRADE_HORIZON)
    final_source = "STRATEGY" if final_action in ("BUY", "SELL") else "NONE"
    status = "STRATEGY"

    if not geometry_action in ("BUY", "SELL"):
        status = "NO_GEOMETRY_DIRECTION"
    elif conflict and strong_geometry:
        # A strong opposite geometry is always allowed to veto. Reversal is a
        # separate, much stricter TESTNET-only decision.
        final_action = "HOLD"
        final_source = ACTION_ARBITRATION_VERSION
        final_horizon = geometry_horizon
        status = "CONFLICT_HOLD"
        blockers.append("STRATEGY_GEOMETRY_CONFLICT")
        if geo_pfl_conflict:
            blockers.append("PHASE_FRONT_CONFLICT")
        if not geometry_edge_allowed and AAL1_REQUIRE_VALIDATED_EDGE:
            blockers.append("GEOMETRY_EDGE_NOT_POSITIVE")
        if not cge_support and AAL1_REQUIRE_CGE_SUPPORT:
            blockers.append("CGE1_NOT_SUPPORTIVE")
        if not efs_match:
            blockers.append("EFS_PEAK_MISMATCH")
        if (
            EXECUTION_MODE == "TESTNET"
            and TESTNET_RELAX_GATES
            and TESTNET_ACTION_ARBITRATION
            and TESTNET_GEOMETRY_ACTIONS
            and override_evidence
        ):
            final_action = geometry_action
            final_source = ACTION_ARBITRATION_VERSION
            status = "GEOMETRY_OVERRIDE_TESTNET"
            blockers = []
    elif raw_action == "HOLD" and strong_geometry:
        final_action = "HOLD"
        final_source = ACTION_ARBITRATION_VERSION
        final_horizon = geometry_horizon
        status = "GEOMETRY_ONLY_HOLD"
        if geo_pfl_conflict:
            blockers.append("PHASE_FRONT_CONFLICT")
        if not geometry_edge_allowed and AAL1_REQUIRE_VALIDATED_EDGE:
            blockers.append("GEOMETRY_EDGE_NOT_POSITIVE")
        if not cge_support and AAL1_REQUIRE_CGE_SUPPORT:
            blockers.append("CGE1_NOT_SUPPORTIVE")
        if not efs_match:
            blockers.append("EFS_PEAK_MISMATCH")
        if (
            EXECUTION_MODE == "TESTNET"
            and TESTNET_RELAX_GATES
            and TESTNET_ACTION_ARBITRATION
            and TESTNET_GEOMETRY_ACTIONS
            and override_evidence
        ):
            final_action = geometry_action
            final_source = ACTION_ARBITRATION_VERSION
            status = "GEOMETRY_ONLY_TESTNET"
            blockers = []
    elif raw_action in ("BUY", "SELL") and geometry_action == raw_action:
        status = "CONSENSUS"
    elif conflict:
        status = "STRATEGY_WEAK_GEO_CONFLICT"
    elif raw_action == "HOLD":
        status = "HOLD"

    if status in ("GEOMETRY_OVERRIDE_TESTNET", "GEOMETRY_ONLY_TESTNET") and EXECUTION_MODE != "TESTNET":
        final_action = "HOLD"
        final_source = ACTION_ARBITRATION_VERSION
        status = "GEOMETRY_TESTNET_ONLY_BLOCK"
        blockers.append("TESTNET_MODE_REQUIRED")

    return {
        "version": ACTION_ARBITRATION_VERSION,
        "ready": bool(eh.get("ready", False)) if isinstance(eh, dict) else False,
        "mode": EXECUTION_MODE,
        "strategy": strategy,
        "strategy_action": raw_action,
        "geometry_action": geometry_action,
        "geometry_horizon": geometry_horizon,
        "geometry_status": geo_status,
        "geometry_score": round(geo_score, 4),
        "geometry_cost_coverage": round(geo_cost, 4),
        "geometry_local_score": round(geo_local, 4),
        "geometry_gsr_quality": round(geo_gsr_quality, 4),
        "geometry_pfl_conflict": bool(geo_pfl_conflict),
        "efs_match": bool(efs_match),
        "cge1_status": cge_status,
        "cge1_score": round(cge_score, 4),
        "cge1_validated": bool(cge_validated),
        "geometry_edge_allowed": geometry_edge_allowed,
        "geometry_edge_reason": geometry_edge.get("reason", "UNKNOWN"),
        "strong_geometry": bool(strong_geometry),
        "override_evidence": bool(override_evidence),
        "direction_conflict": bool(conflict),
        "final_action": final_action,
        "final_horizon": int(final_horizon),
        "final_source": final_source,
        "status": status,
        "blockers": blockers,
        "testnet_override_armed": bool(TESTNET_ACTION_ARBITRATION and TESTNET_GEOMETRY_ACTIONS),
        "live_geometry_reverse_allowed": False,
    }


def compute_geometry_testnet_bridge(state):
    """GDX1 adapter for AAL1 geometry-originated TESTNET actions."""
    raw_action = str(state.get("action", "HOLD")).upper()
    aal = state.get("action_arbitration", {})
    eh = state.get("execution_horizon_arbitration", {})
    efs = state.get("economic_front_surface", {})
    gsr = state.get("geometric_stability_reversal", {})

    action = str(aal.get("final_action", "HOLD")).upper() if isinstance(aal, dict) else "HOLD"
    horizon = int(aal.get("final_horizon", TRADE_HORIZON) or TRADE_HORIZON) if isinstance(aal, dict) else TRADE_HORIZON
    aal_status = str(aal.get("status", "WARMUP")) if isinstance(aal, dict) else "WARMUP"
    candidate = _aal1_find_eh_row(eh, action, horizon) or {}

    blockers = []
    if EXECUTION_MODE != "TESTNET":
        blockers.append("TESTNET_MODE_REQUIRED")
    if not TESTNET_RELAX_GATES:
        blockers.append("RELAX_GATES_REQUIRED")
    if not TESTNET_GEOMETRY_ACTIONS:
        blockers.append("GEOMETRY_ACTIONS_NOT_ARMED")
    if not TESTNET_ACTION_ARBITRATION:
        blockers.append("AAL1_OVERRIDE_NOT_ARMED")
    if aal_status not in ("GEOMETRY_OVERRIDE_TESTNET", "GEOMETRY_ONLY_TESTNET"):
        blockers.append("AAL1_NOT_GEOMETRY_ACTION")
    if action not in ("BUY", "SELL"):
        blockers.append("NO_DIRECTION")

    score = float(candidate.get("score", 0.0) or 0.0)
    cost_cov = float(candidate.get("cost_coverage", 0.0) or 0.0)
    local_geom = float(candidate.get("local_geometry_score", 0.0) or 0.0)
    pfl_score = float(candidate.get("pfl_score", 0.0) or 0.0)
    pfl_conflict = bool(candidate.get("pfl_conflict", False))
    cf = gsr.get("counterfactual", {}) if isinstance(gsr, dict) else {}
    side_gsr = cf.get(action, {}) if isinstance(cf, dict) and action in ("BUY", "SELL") else {}
    gsr_quality = float(side_gsr.get("quality", 0.0) or 0.0)
    gsr_safe = bool(side_gsr.get("testnet_safe", False))

    if score < GDX1_MIN_SCORE:
        blockers.append("EH1_SCORE_LOW")
    if cost_cov < GDX1_MIN_COST_COVERAGE:
        blockers.append("COST_COVERAGE_LOW")
    if local_geom < GDX1_MIN_LOCAL_GEOMETRY:
        blockers.append("LOCAL_GEOMETRY_WEAK")
    if gsr_quality < GDX1_MIN_GSR_QUALITY:
        blockers.append("GSR1_QUALITY_LOW")
    if not gsr_safe:
        blockers.append("GSR1_NOT_TESTNET_SAFE")
    if pfl_conflict:
        blockers.append("PHASE_FRONT_CONFLICT")
    if pfl_score < GDX1_MIN_PFL_SCORE:
        blockers.append("PHASE_FRONT_WEAK")

    efs_h = int(efs.get("peak_horizon", 0) or 0) if isinstance(efs, dict) else 0
    efs_dir = str(efs.get("peak_direction", "NONE")).upper() if isinstance(efs, dict) else "NONE"
    efs_cov = float(efs.get("peak_cost_coverage", 0.0) or 0.0) if isinstance(efs, dict) else 0.0
    efs_match = efs_h == horizon and efs_dir == action
    if GDX1_REQUIRE_EFS_MATCH and not efs_match:
        blockers.append("EFS_PEAK_MISMATCH")
    if not bool(execution_runtime.get("preflight_ok", False)):
        blockers.append("EXCHANGE_PREFLIGHT")

    allowed = len(blockers) == 0
    return {
        "version": GEOMETRY_TESTNET_BRIDGE_VERSION,
        "ready": bool(candidate),
        "allowed": allowed,
        "raw_action": raw_action,
        "aal1_status": aal_status,
        "action": action if action in ("BUY", "SELL") else "HOLD",
        "horizon": horizon,
        "score": round(score, 4),
        "cost_coverage": round(cost_cov, 4),
        "local_geometry_score": round(local_geom, 4),
        "gsr_quality": round(gsr_quality, 4),
        "gsr_testnet_safe": gsr_safe,
        "pfl_score": round(pfl_score, 4),
        "pfl_conflict": pfl_conflict,
        "efs_peak_horizon": efs_h,
        "efs_peak_direction": efs_dir,
        "efs_peak_cost_coverage": round(efs_cov, 4),
        "efs_match": efs_match,
        "blockers": blockers,
        "candidate": dict(candidate),
        "testnet_only": True,
        "research_only_direction": True,
    }


def _iso_from_ms(value):
    try:
        return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc).isoformat()
    except Exception:
        return None


def _latest_closed_market_ms(now_ms=None):
    if now_ms is None:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    # 1m Binance candles close at open_time + 59_999ms.
    return (int(now_ms) // MINUTE_MS) * MINUTE_MS - 1


def _gap_sign(label):
    label = str(label or "NONE").upper()
    if label in ("UP", "BUY"):
        return 1.0
    if label in ("DOWN", "SELL"):
        return -1.0
    return 0.0


def _gap_code(label):
    label = str(label or "F").upper()
    if label.startswith("U"):
        return 1.0
    if label.startswith("D"):
        return -1.0
    return 0.0


def _gap_token(score, deadzone=0.18):
    score = float(score or 0.0)
    if score > deadzone:
        return "U"
    if score < -deadzone:
        return "D"
    return "F"


def _gap_pattern_class(pattern):
    p = str(pattern or "")
    if p == "U-U-U-U-U":
        return "FULL_UP_DOMAIN"
    if p == "D-D-D-D-D":
        return "FULL_DOWN_DOMAIN"
    parts = p.split("-")
    if len(parts) != 5:
        return "MIXED_STACK"
    if parts[0] == "U" and all(x == "D" for x in parts[1:]):
        return "UP_ISLAND_IN_DOWN"
    if parts[0] in ("F", "U") and all(x == "D" for x in parts[1:]):
        return "MICRO_STABLE_IN_MACRO_DOWN"
    if parts[0] == "D" and all(x == "U" for x in parts[1:]):
        return "DOWN_ISLAND_IN_UP"
    if parts[0] in ("F", "D") and all(x == "U" for x in parts[1:]):
        return "MICRO_STABLE_IN_MACRO_UP"
    return "MIXED_STACK"


def _gap_probabilities(score, uncertainty):
    score = max(-1.5, min(1.5, float(score or 0.0)))
    uncertainty = max(0.0, min(0.90, float(uncertainty or 0.0)))
    # These are normalized hypothesis weights, not calibrated probabilities.
    up = math.exp(2.20 * score)
    down = math.exp(-2.20 * score)
    flat = math.exp(1.30 * (uncertainty - abs(score)))
    total = max(1e-12, up + down + flat)
    return {
        "UP": up / total,
        "FLAT": flat / total,
        "DOWN": down / total,
    }


def _gap_branch_weights(prior_state):
    gsr = prior_state.get("geometric_stability_reversal", {}) or {}
    pfl = prior_state.get("phase_front_lag", {}) or {}
    ctd = prior_state.get("geometry_state", {}).get("cone_transition_dynamics", {}) or {}
    cf = gsr.get("counterfactual", {}) if isinstance(gsr.get("counterfactual", {}), dict) else {}
    preferred = str(gsr.get("geometry_preferred_action", "NONE")).upper()
    preferred_q = 0.0
    if preferred in ("BUY", "SELL"):
        preferred_q = float((cf.get(preferred, {}) or {}).get("quality", 0.0) or 0.0)
    reversal = float(gsr.get("reversal_index", 0.0) or 0.0)
    pfl_strength = float(pfl.get("strength", 0.0) or 0.0)
    coherence = float(ctd.get("rotation_coherence", 0.0) or 0.0)
    continuation = 0.25 + 0.55 * preferred_q + 0.20 * coherence
    front = 0.10 + 0.75 * pfl_strength
    reversal_w = 0.10 + 0.75 * reversal
    compression = 0.20 + 0.30 * (1.0 - coherence)
    raw = {
        "CONTINUATION": max(0.01, continuation),
        "FRONT_PROPAGATION": max(0.01, front),
        "REVERSAL": max(0.01, reversal_w),
        "COMPRESSION": max(0.01, compression),
    }
    z = sum(raw.values())
    return {k: round(v / z, 6) for k, v in raw.items()}


def build_gap_blind_plane(prior_state, start_close_ms, end_close_ms, gap_minutes):
    """Freeze a no-peek trajectory hypothesis using only pre-gap information."""
    prior_state = prior_state if isinstance(prior_state, dict) else {}
    geometry = prior_state.get("geometry_state", {}) or {}
    cone = geometry.get("cone_model", {}) or {}
    ctd = geometry.get("cone_transition_dynamics", {}) or {}
    pfl = prior_state.get("phase_front_lag", {}) or {}
    efs = prior_state.get("economic_front_surface", {}) or {}
    gsr = prior_state.get("geometric_stability_reversal", {}) or {}
    horizon_rows = cone.get("horizons", {}) if isinstance(cone.get("horizons", {}), dict) else {}
    residual_bias = model_residual_tracker.get("bias_by_horizon", {}) if isinstance(model_residual_tracker.get("bias_by_horizon"), dict) else {}
    horizons = (5, 15, 30, 60, 120)

    initial_score = {}
    prior_bpm = prior_state.get("bipolar_pressure_model", {}) or {}
    prior_bpm_h = prior_bpm.get("horizons", {}) if isinstance(prior_bpm.get("horizons", {}), dict) else {}
    for h in horizons:
        if str(h) in prior_bpm_h:
            initial_score[h] = _bpm_clamp((prior_bpm_h.get(str(h), {}) or {}).get("q", 0.0))
        else:
            tilt = float((horizon_rows.get(str(h), {}) or {}).get("tilt_deg", 0.0) or 0.0)
            initial_score[h] = max(-1.0, min(1.0, tilt / 60.0))

    macro_score = sum(initial_score[h] for h in (30, 60, 120)) / 3.0
    ctd_sign = _gap_sign(ctd.get("rotation_direction"))
    ctd_coh = float(ctd.get("rotation_coherence", 0.0) or 0.0)
    pfl_sign = _gap_sign(pfl.get("front_direction"))
    pfl_strength = float(pfl.get("strength", 0.0) or 0.0)
    pfl_mode = str(pfl.get("propagation_mode", "NONE")).upper()
    pfl_from = int(pfl.get("from_horizon", 5) or 5)
    pfl_front = int(pfl.get("front_horizon", pfl_from) or pfl_from)
    preferred = str(gsr.get("geometry_preferred_action", "NONE")).upper()
    preferred_sign = _gap_sign(preferred)
    cf = gsr.get("counterfactual", {}) if isinstance(gsr.get("counterfactual", {}), dict) else {}
    preferred_quality = float((cf.get(preferred, {}) or {}).get("quality", 0.0) or 0.0) if preferred in ("BUY", "SELL") else 0.0

    states = []
    gap_minutes = max(1, int(gap_minutes))
    for minute in range(1, gap_minutes + 1):
        progress = minute / float(gap_minutes)
        pattern = []
        score_by_h = {}
        confidence_by_h = {}
        weights_by_h = {}
        for h in horizons:
            memory_tau = max(20.0, float(h) * 3.0)
            memory = math.exp(-float(minute) / memory_tau)
            score = initial_score[h] * memory + macro_score * (1.0 - memory) * 0.55
            score += ctd_sign * ctd_coh * 0.22 * math.exp(-float(minute) / 75.0)
            score += preferred_sign * preferred_quality * 0.10 * math.exp(-float(minute) / 90.0)

            if pfl_sign != 0.0 and pfl_strength > 0.0:
                origin = max(1, pfl_from)
                dist = abs(math.log2(float(h) / float(origin))) if h != origin else 0.0
                if pfl_mode == "MICRO_TO_MACRO":
                    reachable = h >= origin
                elif pfl_mode == "MACRO_TO_MICRO":
                    reachable = h <= max(origin, pfl_front)
                else:
                    reachable = True
                arrival = 1.0 + dist * 4.0
                if reachable and minute >= arrival:
                    age = minute - arrival
                    score += pfl_sign * pfl_strength * 0.48 * math.exp(-age / max(15.0, h * 1.5))

            rb = max(-1.0, min(1.0, float(residual_bias.get(str(h), 0.0) or 0.0)))
            score += GAP_RESIDUAL_DIRECTION_GAIN * rb
            score = max(-1.5, min(1.5, score))
            uncertainty = min(0.85, 0.10 + 0.62 * progress + 0.12 * (1.0 - abs(score)))
            probs = _gap_probabilities(score, uncertainty)
            token = max(probs, key=probs.get)
            pattern.append({"UP": "U", "DOWN": "D", "FLAT": "F"}[token])
            score_by_h[str(h)] = round(score, 6)
            confidence_by_h[str(h)] = round(max(probs.values()), 6)
            weights_by_h[str(h)] = {k: round(v, 6) for k, v in probs.items()}

        pat = "-".join(pattern)
        states.append({
            "minute_offset": minute,
            "close_time_ms": int(start_close_ms + minute * MINUTE_MS),
            "pattern": pat,
            "pattern_class": _gap_pattern_class(pat),
            "mean_confidence": round(sum(confidence_by_h.values()) / len(confidence_by_h), 6),
            "score_by_horizon": score_by_h,
            "bpm_q_by_horizon": {k: round(_bpm_clamp(v), 6) for k, v in score_by_h.items()},
            "hypothesis_weights_by_horizon": weights_by_h,
        })

    peak_dir = str(efs.get("peak_direction", "NONE")).upper()
    peak_h = int(efs.get("peak_horizon", 0) or 0)
    peak_row = {}
    for row in (efs.get("rows", []) or []):
        if int(row.get("horizon", 0) or 0) == peak_h:
            peak_row = row
            break
    motion = float(peak_row.get("motion_budget_pct", 0.0) or 0.0)
    if motion <= 0.0:
        sf = prior_state.get("state_features", {}) or {}
        motion = float(sf.get(f"motion_budget_h{peak_h}_pct", 0.0) or 0.0) if peak_h else 0.0
    scale = math.sqrt(max(1.0, gap_minutes) / max(1.0, float(peak_h or 60)))
    predicted_terminal_return_pct = _gap_sign(peak_dir) * motion * min(2.0, max(0.35, scale))

    gap_id = "GAP-" + hashlib.sha256(f"{SYMBOL}|{start_close_ms}|{end_close_ms}".encode()).hexdigest()[:14]
    return {
        "version": GAP_HYPOTHESIS_VERSION,
        "ready": True,
        "status": "BLIND_FROZEN",
        "gap_id": gap_id,
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "frozen_at": now_iso(),
        "start_close_time_ms": int(start_close_ms),
        "end_close_time_ms": int(end_close_ms),
        "start_market_time": _iso_from_ms(start_close_ms),
        "end_market_time": _iso_from_ms(end_close_ms),
        "gap_minutes": int(gap_minutes),
        "prior_state_id": prior_state.get("state_id"),
        "prior_price": prior_state.get("price"),
        "branch_weights": _gap_branch_weights(prior_state),
        "initial_pattern": str(ctd.get("state_pattern", "F-F-F-F-F")),
        "initial_topology": str((ctd.get("phase_topology", {}) or {}).get("topology_class", "UNOBSERVED")),
        "initial_pfl": {
            "direction": pfl.get("front_direction", "NONE"),
            "mode": pfl.get("propagation_mode", "NONE"),
            "strength": round(pfl_strength, 6),
            "from_horizon": pfl_from,
            "front_horizon": pfl_front,
        },
        "initial_efs": {
            "direction": peak_dir,
            "horizon": peak_h,
            "cost_coverage": round(float(efs.get("peak_cost_coverage", 0.0) or 0.0), 6),
        },
        "initial_bpm": {
            "ready": bool(prior_bpm.get("ready")),
            "q": round(float(prior_bpm.get("q", 0.0) or 0.0), 6),
            "I": round(float(prior_bpm.get("I", 0.0) or 0.0), 6),
            "P": round(float(prior_bpm.get("P", 0.0) or 0.0), 6),
            "state": str(prior_bpm.get("state", "UNAVAILABLE")),
        },
        "predicted_terminal_return_pct": round(predicted_terminal_return_pct, 6),
        "residual_context": {
            "reliability": round(float(model_residual_tracker.get("reliability", 1.0) or 1.0), 6),
            "bias_by_horizon": {str(h): round(float(residual_bias.get(str(h), 0.0) or 0.0), 6) for h in horizons},
        },
        "states": states,
        "probability_note": "Weights are model hypothesis weights, not calibrated market probabilities.",
        "research_only": True,
        "execution_disabled": True,
    }


def build_gap_bridge_plane(blind, endpoint_price):
    """Endpoint-conditioned reconstruction; created only after blind freeze."""
    if not isinstance(blind, dict) or not blind.get("ready"):
        return {"ready": False, "status": "NO_BLIND"}
    start_price = float(blind.get("prior_price", 0.0) or 0.0)
    endpoint_price = float(endpoint_price or 0.0)
    actual_return = ((endpoint_price / start_price) - 1.0) * 100.0 if start_price > 0 and endpoint_price > 0 else 0.0
    end_sign = 1.0 if actual_return > 0 else (-1.0 if actual_return < 0 else 0.0)
    magnitude = min(1.0, abs(actual_return) / max(roundtrip_buy_break_even_move_pct(), 1e-6))
    out_states = []
    source_states = blind.get("states", []) or []
    n = max(1, len(source_states))
    for i, row in enumerate(source_states, start=1):
        progress = i / float(n)
        pattern = []
        score_by_h = {}
        for h in (5, 15, 30, 60, 120):
            base = float((row.get("score_by_horizon", {}) or {}).get(str(h), 0.0) or 0.0)
            # Condition progressively toward the known endpoint, while preserving
            # early blind geometry. This is reconstruction, never a forecast.
            score = base * (1.0 - 0.55 * progress) + end_sign * magnitude * 0.75 * progress
            score = max(-1.5, min(1.5, score))
            pattern.append(_gap_token(score))
            score_by_h[str(h)] = round(score, 6)
        pat = "-".join(pattern)
        out_states.append({
            "minute_offset": int(row.get("minute_offset", i)),
            "close_time_ms": int(row.get("close_time_ms", 0) or 0),
            "pattern": pat,
            "pattern_class": _gap_pattern_class(pat),
            "score_by_horizon": score_by_h,
            "bpm_q_by_horizon": {k: round(_bpm_clamp(v), 6) for k, v in score_by_h.items()},
        })
    return {
        "version": GAP_HYPOTHESIS_VERSION + "-BRIDGE",
        "ready": True,
        "status": "ENDPOINT_CONDITIONED",
        "gap_id": blind.get("gap_id"),
        "created_at": now_iso(),
        "endpoint_price": endpoint_price,
        "endpoint_return_pct": round(actual_return, 6),
        "states": out_states,
        "research_only": True,
        "not_blind": True,
    }


def _fetch_kline_range(start_open_ms, end_close_ms):
    """Fetch chronological 1m klines with pagination; public market data only."""
    out = []
    cursor = max(0, int(start_open_ms))
    end_close_ms = int(end_close_ms)
    safety = 0
    while cursor <= end_close_ms and safety < 1000:
        safety += 1
        batch = rest_klines(limit=1000, start_time=cursor)
        if not batch:
            break
        advanced = False
        for k in batch:
            open_ms = int(k[0])
            close_ms = int(k[6])
            if close_ms > end_close_ms:
                break
            if open_ms < cursor:
                continue
            out.append({
                "open_time_ms": open_ms,
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
                "close_time_ms": close_ms,
            })
            cursor = open_ms + MINUTE_MS
            advanced = True
        if not advanced:
            last_open = int(batch[-1][0])
            next_cursor = last_open + MINUTE_MS
            if next_cursor <= cursor:
                break
            cursor = next_cursor
        if int(batch[-1][6]) >= end_close_ms:
            break
    # Deduplicate by close timestamp because overlapping REST pages are possible.
    dedup = {}
    for row in out:
        dedup[int(row["close_time_ms"])] = row
    return [dedup[k] for k in sorted(dedup)]


def _gap_replay_actual(start_close_ms, end_close_ms):
    """Replay missing candles through geometry in an isolated, non-execution plane."""
    global candles
    global geometry_tracker, cone_tracker, cone_transition_tracker
    global phase_topology_tracker, phase_front_tracker, economic_front_tracker
    global bipolar_pressure_tracker
    global corridor3_tracker, last_corridor2_flags

    warmup = max(FEATURE_HISTORY_CANDLES, GEOMETRY_HISTORY_CANDLES)
    last_open = int(start_close_ms) - (MINUTE_MS - 1)
    first_open = max(0, last_open - (warmup - 1) * MINUTE_MS)
    market = _fetch_kline_range(first_open, end_close_ms)
    pre = [x for x in market if int(x["close_time_ms"]) <= int(start_close_ms)]
    missing = [x for x in market if int(start_close_ms) < int(x["close_time_ms"]) <= int(end_close_ms)]
    expected = int(round((int(end_close_ms) - int(start_close_ms)) / float(MINUTE_MS)))

    saved_candles = list(candles)
    saved = {
        "geometry_tracker": copy.deepcopy(geometry_tracker),
        "cone_tracker": copy.deepcopy(cone_tracker),
        "cone_transition_tracker": copy.deepcopy(cone_transition_tracker),
        "phase_topology_tracker": copy.deepcopy(phase_topology_tracker),
        "phase_front_tracker": copy.deepcopy(phase_front_tracker),
        "economic_front_tracker": copy.deepcopy(economic_front_tracker),
        "bipolar_pressure_tracker": copy.deepcopy(bipolar_pressure_tracker),
        "corridor3_tracker": copy.deepcopy(corridor3_tracker),
        "last_corridor2_flags": copy.deepcopy(last_corridor2_flags),
    }
    # Clone the pre-gap trackers so replay starts from exactly what the model knew.
    geometry_tracker = copy.deepcopy(saved["geometry_tracker"])
    cone_tracker = copy.deepcopy(saved["cone_tracker"])
    cone_transition_tracker = copy.deepcopy(saved["cone_transition_tracker"])
    phase_topology_tracker = copy.deepcopy(saved["phase_topology_tracker"])
    phase_front_tracker = copy.deepcopy(saved["phase_front_tracker"])
    economic_front_tracker = copy.deepcopy(saved["economic_front_tracker"])
    bipolar_pressure_tracker = copy.deepcopy(saved["bipolar_pressure_tracker"])
    corridor3_tracker = copy.deepcopy(saved["corridor3_tracker"])
    last_corridor2_flags = copy.deepcopy(saved["last_corridor2_flags"])

    candles.clear()
    for x in pre[-warmup:]:
        candles.append(dict(x))

    observed = []
    reanchor = None
    try:
        total = len(missing)
        for idx, candle in enumerate(missing, start=1):
            candles.append(dict(candle))
            regime, trend, vol = classify_market()
            state_features = compute_state_features()
            corridor_features = compute_corridor_features()
            cor2 = compute_corridor2_multilabel(corridor_features, state_features)
            geometry = compute_geometry_layer(corridor_features, state_features, candle["close_time_ms"])
            cor2 = augment_corridor2_with_geometry(cor2, geometry)
            cor3 = compute_corridor3_scale_age(corridor_features, cor2, candle["close_time_ms"])
            signals = strategy_signals(regime, trend)
            strategy, action, probability = cognitive_loop(regime, signals)
            st = {
                "state_id": f"{SESSION_CONTINUITY_VERSION}-{idx}",
                "time": _iso_from_ms(candle["close_time_ms"]),
                "processed_at": now_iso(),
                "market_time_ms": int(candle["close_time_ms"]),
                "market_time": _iso_from_ms(candle["close_time_ms"]),
                "recovered": True,
                "recovery_plane": SESSION_CONTINUITY_VERSION,
                "symbol": SYMBOL,
                "price": candle["close"],
                "regime": regime,
                "trend_pct": round(trend, 5),
                "volatility_pct": round(vol, 5),
                "signals": signals,
                "chosen_strategy": strategy,
                "action": action,
                "p_success": round(probability, 4),
                "prediction_horizon": TRADE_HORIZON,
                "state_features": state_features,
                "corridor_features": corridor_features,
                "corridor_multilabel": cor2,
                "corridor_scale_age": cor3,
                "geometry_state": geometry,
            }
            pfl = compute_phase_front_lag(st, candle["close_time_ms"])
            st["phase_front_lag"] = pfl
            gsr = compute_geometric_stability_reversal(st)
            st["geometric_stability_reversal"] = gsr
            eh = compute_execution_horizon_arbitration(st)
            st["execution_horizon_arbitration"] = eh
            efs = compute_economic_front_surface(st, candle["close_time_ms"])
            st["economic_front_surface"] = efs
            bpm = compute_bipolar_pressure(st, candle["close_time_ms"])
            st["bipolar_pressure_model"] = bpm
            ctd = geometry.get("cone_transition_dynamics", {}) or {}
            topology = ctd.get("phase_topology", {}) or {}
            observed.append({
                "minute_offset": idx,
                "close_time_ms": int(candle["close_time_ms"]),
                "market_time": _iso_from_ms(candle["close_time_ms"]),
                "price": round(float(candle["close"]), 8),
                "pattern": str(ctd.get("state_pattern", "F-F-F-F-F")),
                "topology_class": str(topology.get("topology_class", "UNOBSERVED")),
                "shock_horizon": ctd.get("shock_horizon"),
                "rotation_direction": ctd.get("rotation_direction", "NONE"),
                "rotation_coherence": round(float(ctd.get("rotation_coherence", 0.0) or 0.0), 6),
                "propagation_mode": str((ctd.get("propagation", {}) or {}).get("mode", "NONE")),
                "propagation_direction": str((ctd.get("propagation", {}) or {}).get("direction", "NONE")),
                "pfl_direction": str(pfl.get("front_direction", "NONE")),
                "pfl_mode": str(pfl.get("propagation_mode", "NONE")),
                "pfl_strength": round(float(pfl.get("strength", 0.0) or 0.0), 6),
                "efs_peak_horizon": efs.get("peak_horizon"),
                "efs_peak_direction": efs.get("peak_direction", "NONE"),
                "efs_peak_cost_coverage": round(float(efs.get("peak_cost_coverage", 0.0) or 0.0), 6),
                "bpm_q": round(float(bpm.get("q", 0.0) or 0.0), 6),
                "bpm_I": round(float(bpm.get("I", 0.0) or 0.0), 6),
                "bpm_P": round(float(bpm.get("P", 0.0) or 0.0), 6),
                "bpm_q_by_horizon": {str(h): round(float(((bpm.get("horizons", {}) or {}).get(str(h), {}) or {}).get("q", 0.0) or 0.0), 6) for h in BPM1_HORIZONS},
                "bpm_I_by_horizon": {str(h): round(float(((bpm.get("horizons", {}) or {}).get(str(h), {}) or {}).get("I", 0.0) or 0.0), 6) for h in BPM1_HORIZONS},
                "tilt_by_horizon": {
                    str(h): round(float(((geometry.get("cone_model", {}) or {}).get("horizons", {}).get(str(h), {}) or {}).get("tilt_deg", 0.0) or 0.0), 6)
                    for h in (5, 15, 30, 60, 120)
                },
            })
            if idx % GAP_REPLAY_PROGRESS_EVERY == 0 or idx == total:
                print(f"SCR1 replay: {idx}/{total} closed candles | execution=DISABLED")

        reanchor = {
            "geometry_tracker": copy.deepcopy(geometry_tracker),
            "cone_tracker": copy.deepcopy(cone_tracker),
            "cone_transition_tracker": copy.deepcopy(cone_transition_tracker),
            "phase_topology_tracker": copy.deepcopy(phase_topology_tracker),
            "phase_front_tracker": copy.deepcopy(phase_front_tracker),
            "economic_front_tracker": copy.deepcopy(economic_front_tracker),
            "bipolar_pressure_tracker": copy.deepcopy(bipolar_pressure_tracker),
            "corridor3_tracker": copy.deepcopy(corridor3_tracker),
            "last_corridor2_flags": copy.deepcopy(last_corridor2_flags),
        }
    finally:
        candles.clear()
        for x in saved_candles:
            candles.append(x)
        geometry_tracker = saved["geometry_tracker"]
        cone_tracker = saved["cone_tracker"]
        cone_transition_tracker = saved["cone_transition_tracker"]
        phase_topology_tracker = saved["phase_topology_tracker"]
        phase_front_tracker = saved["phase_front_tracker"]
        economic_front_tracker = saved["economic_front_tracker"]
        bipolar_pressure_tracker = saved["bipolar_pressure_tracker"]
        corridor3_tracker = saved["corridor3_tracker"]
        last_corridor2_flags = saved["last_corridor2_flags"]

    return {
        "expected_minutes": expected,
        "fetched_minutes": len(missing),
        "complete": len(missing) == expected,
        "states": observed,
        "reanchor": reanchor,
    }


def _apply_gap_reanchor(reanchor):
    global geometry_tracker, cone_tracker, cone_transition_tracker
    global phase_topology_tracker, phase_front_tracker, economic_front_tracker
    global bipolar_pressure_tracker
    global corridor3_tracker, last_corridor2_flags
    if not isinstance(reanchor, dict):
        return False
    geometry_tracker = copy.deepcopy(reanchor.get("geometry_tracker", geometry_tracker))
    cone_tracker = copy.deepcopy(reanchor.get("cone_tracker", cone_tracker))
    cone_transition_tracker = copy.deepcopy(reanchor.get("cone_transition_tracker", cone_transition_tracker))
    phase_topology_tracker = copy.deepcopy(reanchor.get("phase_topology_tracker", phase_topology_tracker))
    phase_front_tracker = copy.deepcopy(reanchor.get("phase_front_tracker", phase_front_tracker))
    economic_front_tracker = copy.deepcopy(reanchor.get("economic_front_tracker", economic_front_tracker))
    bipolar_pressure_tracker = copy.deepcopy(reanchor.get("bipolar_pressure_tracker", bipolar_pressure_tracker))
    corridor3_tracker = copy.deepcopy(reanchor.get("corridor3_tracker", corridor3_tracker))
    last_corridor2_flags = copy.deepcopy(reanchor.get("last_corridor2_flags", last_corridor2_flags))
    return True


def reconcile_gap_planes(blind, bridge, actual):
    bstates = (blind or {}).get("states", []) or []
    rstates = (bridge or {}).get("states", []) or []
    astates = (actual or {}).get("states", []) or []
    n = min(len(bstates), len(astates))
    if n <= 0:
        return {"version": GAP_RECONCILIATION_VERSION, "ready": False, "status": "NO_OVERLAP"}
    horizon_index = {5: 0, 15: 1, 30: 2, 60: 3, 120: 4}
    per_h = {str(h): {"mismatch": 0, "direction_abs_error": 0.0, "signed_residual_sum": 0.0} for h in horizon_index}
    blind_mismatches = 0
    bridge_mismatches = 0
    direction_error = 0.0
    bridge_direction_error = 0.0
    for i in range(n):
        bp = str(bstates[i].get("pattern", "F-F-F-F-F")).split("-")
        ap = str(astates[i].get("pattern", "F-F-F-F-F")).split("-")
        rp = str(rstates[i].get("pattern", "F-F-F-F-F")).split("-") if i < len(rstates) else ["F"] * 5
        if len(bp) != 5 or len(ap) != 5:
            continue
        blind_mismatches += sum(1 for x, y in zip(bp, ap) if x != y)
        bridge_mismatches += sum(1 for x, y in zip(rp, ap) if x != y) if len(rp) == 5 else 5
        for h, j in horizon_index.items():
            pred_score = float((bstates[i].get("bpm_q_by_horizon", bstates[i].get("score_by_horizon", {})) or {}).get(str(h), _gap_code(bp[j])) or 0.0)
            bridge_score = float((rstates[i].get("bpm_q_by_horizon", rstates[i].get("score_by_horizon", {})) or {}).get(str(h), _gap_code(rp[j]) if len(rp) == 5 else 0.0) or 0.0) if i < len(rstates) else 0.0
            actual_code = float((astates[i].get("bpm_q_by_horizon", {}) or {}).get(str(h), _gap_code(ap[j])) or 0.0)
            actual_code = max(-1.0, min(1.0, actual_code))
            pred_score = max(-1.0, min(1.0, pred_score))
            bridge_score = max(-1.0, min(1.0, bridge_score))
            direction_error += abs(pred_score - actual_code) / 2.0
            bridge_direction_error += abs(bridge_score - actual_code) / 2.0
            ph = per_h[str(h)]
            ph["mismatch"] += int(bp[j] != ap[j])
            ph["direction_abs_error"] += abs(pred_score - actual_code) / 2.0
            ph["signed_residual_sum"] += actual_code - pred_score

    denom = max(1, n * 5)
    pattern_error = blind_mismatches / denom
    bridge_pattern_error = bridge_mismatches / denom
    dir_error = direction_error / denom
    bridge_dir_error = bridge_direction_error / denom
    start_price = float((blind or {}).get("prior_price", 0.0) or 0.0)
    final_price = float(astates[n - 1].get("price", 0.0) or 0.0)
    actual_return = ((final_price / start_price) - 1.0) * 100.0 if start_price > 0 and final_price > 0 else 0.0
    pred_return = float((blind or {}).get("predicted_terminal_return_pct", 0.0) or 0.0)
    required = max(roundtrip_buy_break_even_move_pct(), 1e-6)
    endpoint_error = min(1.0, abs(pred_return - actual_return) / required)
    terminal_bp = str(bstates[n - 1].get("pattern", "F-F-F-F-F"))
    terminal_ap = str(astates[n - 1].get("pattern", "F-F-F-F-F"))
    terminal_mismatch = sum(1 for x, y in zip(terminal_bp.split("-"), terminal_ap.split("-")) if x != y) / 5.0
    total_error = 0.50 * pattern_error + 0.20 * dir_error + 0.15 * endpoint_error + 0.15 * terminal_mismatch
    bridge_total = 0.70 * bridge_pattern_error + 0.20 * bridge_dir_error + 0.10 * terminal_mismatch
    per_h_out = {}
    for h, vals in per_h.items():
        per_h_out[h] = {
            "pattern_error": round(vals["mismatch"] / max(1, n), 6),
            "direction_error": round(vals["direction_abs_error"] / max(1, n), 6),
            "mean_signed_residual": round(vals["signed_residual_sum"] / max(1, n), 6),
        }
    return {
        "version": GAP_RECONCILIATION_VERSION,
        "ready": True,
        "status": "COMPARED",
        "gap_id": (blind or {}).get("gap_id"),
        "compared_at": now_iso(),
        "minutes_compared": n,
        "blind_pattern_error": round(pattern_error, 6),
        "blind_direction_error": round(dir_error, 6),
        "blind_endpoint_return_error": round(endpoint_error, 6),
        "blind_terminal_pattern_error": round(terminal_mismatch, 6),
        "blind_total_error": round(total_error, 6),
        "bridge_pattern_error": round(bridge_pattern_error, 6),
        "bridge_direction_error": round(bridge_dir_error, 6),
        "bridge_total_error": round(bridge_total, 6),
        "actual_terminal_return_pct": round(actual_return, 6),
        "predicted_terminal_return_pct": round(pred_return, 6),
        "per_horizon": per_h_out,
        "research_only": True,
    }


def update_model_residual_from_gap(comparison):
    global model_residual_tracker, latest_model_residual
    if not isinstance(comparison, dict) or not comparison.get("ready"):
        return dict(model_residual_tracker)
    a = GAP_RESIDUAL_ALPHA
    count = int(model_residual_tracker.get("comparisons", 0) or 0)
    def ema(old, new):
        return float(new) if count == 0 else (1.0 - a) * float(old or 0.0) + a * float(new or 0.0)
    model_residual_tracker["comparisons"] = count + 1
    model_residual_tracker["ema_total_error"] = round(ema(model_residual_tracker.get("ema_total_error", 0.0), comparison.get("blind_total_error", 0.0)), 6)
    model_residual_tracker["ema_pattern_error"] = round(ema(model_residual_tracker.get("ema_pattern_error", 0.0), comparison.get("blind_pattern_error", 0.0)), 6)
    model_residual_tracker["ema_direction_error"] = round(ema(model_residual_tracker.get("ema_direction_error", 0.0), comparison.get("blind_direction_error", 0.0)), 6)
    bias = model_residual_tracker.setdefault("bias_by_horizon", {})
    for h, vals in (comparison.get("per_horizon", {}) or {}).items():
        old = float(bias.get(str(h), 0.0) or 0.0)
        new = float(vals.get("mean_signed_residual", 0.0) or 0.0)
        bias[str(h)] = round(max(-1.0, min(1.0, ema(old, new))), 6)
    model_residual_tracker["reliability"] = round(max(0.0, min(1.0, 1.0 - float(model_residual_tracker["ema_total_error"]))), 6)
    model_residual_tracker["last_gap_id"] = comparison.get("gap_id")
    latest_model_residual = dict(model_residual_tracker)
    append_jsonl(MODEL_RESIDUALS_FILE, {"time": now_iso(), **latest_model_residual})
    return latest_model_residual


def _gap_detail_write(gap_id, kind, payload):
    filename = os.path.join(GAP_DETAIL_DIR, f"{gap_id}_{kind}.json")
    _safe_write_json(filename, payload)
    return filename


def _gap_summary(obj):
    if not isinstance(obj, dict):
        return {}
    return {k: obj.get(k) for k in (
        "version", "ready", "status", "gap_id", "gap_minutes", "start_market_time", "end_market_time",
        "prior_state_id", "prior_price", "predicted_terminal_return_pct", "frozen_at",
        "minutes_compared", "blind_pattern_error", "blind_direction_error", "blind_total_error",
        "bridge_pattern_error", "bridge_total_error", "actual_terminal_return_pct", "compared_at"
    ) if k in obj}


def start_session_continuity():
    global session_continuity_tracker
    count = int(session_continuity_tracker.get("session_count", 0) or 0) + 1
    sid = datetime.now(timezone.utc).strftime("S%Y%m%dT%H%M%SZ") + f"-{count}"
    session_continuity_tracker["current_session_id"] = sid
    session_continuity_tracker["session_count"] = count
    return sid


def run_gap_experiment_if_needed(trigger="STARTUP", now_ms=None):
    """Detect, freeze, reveal, replay and compare a market-time session gap.

    The blind freeze is durable. If the process dies after BLIND_FROZEN but
    before comparison, the next session resumes that exact frozen gap first;
    it never silently replaces it with a hindsight-aware forecast.
    """
    global latest_gap_forecast, latest_gap_reconciliation, latest_model_residual
    global session_continuity_tracker, last_close_time_ms
    if not last_close_time_ms:
        return {"detected": False, "reason": "NO_PRIOR_CLOSE"}

    clock_target = _latest_closed_market_ms(now_ms)
    pending = session_continuity_tracker.get("pending_gap")
    resuming = False
    blind = None

    if isinstance(pending, dict) and pending.get("gap_id"):
        p_start = int(pending.get("start_close_time_ms", 0) or 0)
        p_end = int(pending.get("end_close_time_ms", 0) or 0)
        if p_start == int(last_close_time_ms) and p_end > p_start and p_end <= clock_target:
            gap_id = str(pending.get("gap_id"))
            path = os.path.join(GAP_DETAIL_DIR, f"{gap_id}_blind.json")
            try:
                with open(path, "r", encoding="utf-8") as f:
                    blind = json.load(f)
                if isinstance(blind, dict) and blind.get("ready"):
                    resuming = True
                    start_ms = p_start
                    target = p_end
                    gap_minutes = int(pending.get("gap_minutes", round((target-start_ms)/float(MINUTE_MS))) or 0)
                    latest_gap_forecast = blind
                    print(f"GAP1: RESUME durable BLIND {gap_id} ({gap_minutes}m); no new forecast substituted.")
                else:
                    blind = None
            except Exception as e:
                print("GAP1 pending blind load warning:", e)
                blind = None

    if not resuming:
        target = clock_target
        gap_minutes = int(round((target - int(last_close_time_ms)) / float(MINUTE_MS)))
        if gap_minutes < GAP_MIN_MINUTES:
            return {"detected": False, "reason": "BELOW_THRESHOLD", "gap_minutes": gap_minutes}
        if gap_minutes > GAP_MAX_MINUTES:
            print(f"SCR1: gap {gap_minutes}m exceeds MOR_GAP_MAX_MINUTES={GAP_MAX_MINUTES}; no partial replay is fabricated.")
            return {"detected": True, "completed": False, "reason": "GAP_TOO_LARGE", "gap_minutes": gap_minutes}

        prior_state = _latest_state_from_storage()
        if not isinstance(prior_state, dict):
            return {"detected": True, "completed": False, "reason": "NO_PRIOR_STATE", "gap_minutes": gap_minutes}

        start_ms = int(last_close_time_ms)
        blind = build_gap_blind_plane(prior_state, start_ms, target, gap_minutes)
        gap_id = blind["gap_id"]
        print(f"GAP1: detected {gap_minutes}m | {gap_id} | trigger={trigger}")
        print("GAP1: freezing BLIND trajectory before missing candles are fetched...")
        latest_gap_forecast = blind
        _gap_detail_write(gap_id, "blind", blind)
        append_jsonl(GAP_FORECASTS_FILE, _gap_summary(blind))
        session_continuity_tracker["gaps_detected"] = int(session_continuity_tracker.get("gaps_detected", 0) or 0) + 1
        session_continuity_tracker["pending_gap"] = {
            "gap_id": gap_id, "start_close_time_ms": start_ms, "end_close_time_ms": target,
            "gap_minutes": gap_minutes, "blind_frozen_at": blind.get("frozen_at"), "trigger": trigger,
        }
        save_runtime()  # durability boundary: BLIND exists before reveal.
        print("GAP1: BLIND FROZEN. Market gap may now be revealed.")
    else:
        gap_id = str(blind.get("gap_id"))

    try:
        endpoint = fetch_exact_horizon_close(target)
        bridge = build_gap_bridge_plane(blind, endpoint.get("close") if endpoint else 0.0)
        _gap_detail_write(gap_id, "bridge", bridge)
    except Exception as e:
        bridge = {"ready": False, "status": "ENDPOINT_FETCH_FAILED", "reason": str(e)[:300], "gap_id": gap_id}
        print("GAP1 bridge endpoint warning:", e)

    print(f"SCR1: replaying {gap_minutes} missing closed candles in ISOLATED plane; execution=DISABLED")
    try:
        actual = _gap_replay_actual(start_ms, target)
    except Exception as e:
        if isinstance(session_continuity_tracker.get("pending_gap"), dict):
            session_continuity_tracker["pending_gap"]["replay_error"] = str(e)[:500]
        save_runtime()
        print("SCR1 replay error:", e)
        return {"detected": True, "completed": False, "reason": "REPLAY_ERROR", "error": str(e)}

    actual_record = {
        "version": SESSION_CONTINUITY_VERSION,
        "ready": bool(actual.get("complete")),
        "status": "REPLAY_COMPLETE" if actual.get("complete") else "REPLAY_INCOMPLETE",
        "gap_id": gap_id,
        "gap_minutes": gap_minutes,
        "expected_minutes": actual.get("expected_minutes"),
        "fetched_minutes": actual.get("fetched_minutes"),
        "start_close_time_ms": start_ms,
        "end_close_time_ms": target,
        "start_market_time": _iso_from_ms(start_ms),
        "end_market_time": _iso_from_ms(target),
        "states": actual.get("states", []),
        "execution_disabled": True,
        "separate_plane": True,
    }
    _gap_detail_write(gap_id, "observed", actual_record)
    append_jsonl(GAP_OBSERVED_FILE, {
        "version": SESSION_CONTINUITY_VERSION, "gap_id": gap_id, "time": now_iso(),
        "gap_minutes": gap_minutes, "expected_minutes": actual.get("expected_minutes"),
        "fetched_minutes": actual.get("fetched_minutes"), "complete": bool(actual.get("complete")),
    })

    if not actual.get("complete"):
        if isinstance(session_continuity_tracker.get("pending_gap"), dict):
            session_continuity_tracker["pending_gap"]["status"] = "REPLAY_INCOMPLETE"
        save_runtime()
        print(f"SCR1: incomplete {actual.get('fetched_minutes')}/{actual.get('expected_minutes')}; runtime not re-anchored.")
        return {"detected": True, "completed": False, "reason": "REPLAY_INCOMPLETE"}

    comparison = reconcile_gap_planes(blind, bridge, actual_record)
    latest_gap_reconciliation = comparison
    _gap_detail_write(gap_id, "comparison", comparison)
    append_jsonl(GAP_RECONCILIATIONS_FILE, _gap_summary(comparison))
    residual = update_model_residual_from_gap(comparison)

    # Re-anchor only temporal/geometry trackers to the observed endpoint. The
    # missing states remain tagged SCR1 and are not injected as ordinary X ids.
    _apply_gap_reanchor(actual.get("reanchor"))
    last_close_time_ms = target
    session_continuity_tracker["gaps_completed"] = int(session_continuity_tracker.get("gaps_completed", 0) or 0) + 1
    session_continuity_tracker["replayed_minutes"] = int(session_continuity_tracker.get("replayed_minutes", 0) or 0) + gap_minutes
    session_continuity_tracker["last_gap_id"] = gap_id
    session_continuity_tracker["last_gap"] = {
        "gap_id": gap_id,
        "gap_minutes": gap_minutes,
        "trigger": trigger,
        "resumed_blind": bool(resuming),
        "blind_total_error": comparison.get("blind_total_error"),
        "bridge_total_error": comparison.get("bridge_total_error"),
        "actual_terminal_return_pct": comparison.get("actual_terminal_return_pct"),
        "completed_at": now_iso(),
    }
    session_continuity_tracker["pending_gap"] = None
    latest_model_residual = dict(residual)
    save_runtime()
    print(
        "GRC1:",
        f"blindErr={float(comparison.get('blind_total_error',0)):.3f}",
        f"bridgeErr={float(comparison.get('bridge_total_error',0)):.3f}",
        f"actualRet={float(comparison.get('actual_terminal_return_pct',0)):+.4f}%",
    )
    print(
        "RES1:",
        f"EMAerr={float(residual.get('ema_total_error',0)):.3f}",
        f"reliability={float(residual.get('reliability',1)):.3f}",
        "bias=" + ",".join(f"H{h}:{float((residual.get('bias_by_horizon',{}) or {}).get(str(h),0)):+.2f}" for h in (5,15,30,60,120)),
    )
    print("SCR1: continuity re-anchored to observed endpoint; ordinary execution may resume after startup gates.")

    result = {"detected": True, "completed": True, "gap_id": gap_id, "comparison": comparison, "resumed_blind": bool(resuming)}
    # If a crash left a durable pending blind and time continued advancing,
    # finish that audited gap first, then create a fresh blind for the remainder.
    remaining = int(round((clock_target - int(last_close_time_ms)) / float(MINUTE_MS)))
    if remaining >= GAP_MIN_MINUTES:
        print(f"SCR1: {remaining}m additional gap remains after durable-gap recovery; starting a new blind experiment.")
        result["followup"] = run_gap_experiment_if_needed(trigger=str(trigger)+"_FOLLOWUP", now_ms=now_ms)
    return result



def gap_lab_dashboard_html():
    g = session_continuity_tracker if isinstance(session_continuity_tracker, dict) else {}
    last = g.get("last_gap") if isinstance(g.get("last_gap"), dict) else {}
    r = latest_model_residual if isinstance(latest_model_residual, dict) else model_residual_tracker
    pending = g.get("pending_gap") if isinstance(g.get("pending_gap"), dict) else None
    status = "PENDING" if pending else ("COMPARED" if last else "NO GAP YET")
    return (
        '<section class="card erl-card">'
        '<h2>SCR1 · GAP1 · GRC1 · RES1 — SESSION GAP LAB</h2>'
        '<div class="model-note">A session gap is a separate hypothesis/observation plane. GAP1 freezes before missing candles are fetched; SCR1 replays facts with execution disabled; GRC1 measures trajectory mismatch; RES1 learns the model’s own residual bias. Research-only.</div>'
        '<div class="ctd-summary">'
        f'<div class="ctd-kpi"><b>SESSION</b><br>{geo_escape_html(str(g.get("current_session_id","-")))}</div>'
        f'<div class="ctd-kpi"><b>STATUS</b><br>{geo_escape_html(status)}</div>'
        f'<div class="ctd-kpi"><b>LAST GAP</b><br>{int((last or pending or {}).get("gap_minutes",0) or 0)}m</div>'
        f'<div class="ctd-kpi"><b>BLIND ERR</b><br>{float(last.get("blind_total_error",0) or 0):.3f}</div>'
        f'<div class="ctd-kpi"><b>BRIDGE ERR</b><br>{float(last.get("bridge_total_error",0) or 0):.3f}</div>'
        f'<div class="ctd-kpi"><b>RES1 EMA</b><br>{float(r.get("ema_total_error",0) or 0):.3f}</div>'
        f'<div class="ctd-kpi"><b>RELIABILITY</b><br>{float(r.get("reliability",1) or 1):.3f}</div>'
        f'<div class="ctd-kpi"><b>REPLAYED</b><br>{int(g.get("replayed_minutes",0) or 0)}m</div>'
        '</div>'
        f'<div class="flags" style="margin-top:10px"><b>RESIDUAL DIRECTION BIAS</b><br>{geo_escape_html(" · ".join("H"+str(h)+":"+format(float((r.get("bias_by_horizon",{}) or {}).get(str(h),0) or 0), "+.2f") for h in (5,15,30,60,120)))}</div>'
        '</section>'
    )


def run_gap_selftest():
    global model_residual_tracker, latest_model_residual
    saved = copy.deepcopy(model_residual_tracker)
    try:
        model_residual_tracker = {
            "version": MODEL_RESIDUAL_VERSION, "comparisons": 0,
            "ema_total_error": 0.0, "ema_pattern_error": 0.0, "ema_direction_error": 0.0,
            "reliability": 1.0, "bias_by_horizon": {str(h): 0.0 for h in (5,15,30,60,120)}, "last_gap_id": None,
        }
        prior = {
            "state_id": "XTEST", "price": 63000.0,
            "state_features": {"motion_budget_h60_pct": 0.18},
            "geometry_state": {
                "cone_model": {"horizons": {str(h): {"tilt_deg": v} for h, v in {5:-8,15:-28,30:-40,60:-48,120:-52}.items()}},
                "cone_transition_dynamics": {"state_pattern":"F-D-D-D-D","rotation_direction":"DOWN","rotation_coherence":0.82,"phase_topology":{"topology_class":"MICRO_STABLE_IN_MACRO_DOWN"}},
            },
            "phase_front_lag": {"front_direction":"DOWN","propagation_mode":"MICRO_TO_MACRO","strength":0.78,"from_horizon":5,"front_horizon":30},
            "economic_front_surface": {"peak_direction":"SELL","peak_horizon":60,"peak_cost_coverage":1.25,"rows":[{"horizon":60,"motion_budget_pct":0.18}]},
            "geometric_stability_reversal": {"geometry_preferred_action":"SELL","reversal_index":0.08,"counterfactual":{"SELL":{"quality":0.86}}},
        }
        start = 1_800_000_000_000 - 1
        blind = build_gap_blind_plane(prior, start, start + 30*MINUTE_MS, 30)
        bridge = build_gap_bridge_plane(blind, 62920.0)
        actual_states = []
        for i in range(1,31):
            actual_states.append({"minute_offset":i,"close_time_ms":start+i*MINUTE_MS,"pattern":"D-D-D-D-D","price":63000.0 - (80.0*i/30.0)})
        actual = {"states": actual_states}
        comp = reconcile_gap_planes(blind, bridge, actual)
        res = update_model_residual_from_gap(comp)
        ok = bool(blind.get("ready")) and bool(comp.get("ready")) and len(blind.get("states",[])) == 30 and 0 <= float(comp.get("blind_total_error",2)) <= 1.0
        return {"ok": ok, "blind_terminal_pattern": blind["states"][-1]["pattern"], "blind_error": comp.get("blind_total_error"), "bridge_error": comp.get("bridge_total_error"), "residual_reliability": res.get("reliability"), "gap_id": blind.get("gap_id")}
    finally:
        model_residual_tracker = saved
        latest_model_residual = dict(saved)



# ------------------------------------------------------------------
# Universe Lab bridge — lightweight operator status snapshot.
# This file is deliberately tiny compared with MORX1 and contains no
# API keys, secrets, signed headers, or exchange credentials.
# ------------------------------------------------------------------
OBSERVER_STATUS_FILE = os.getenv(
    "MOR_OBSERVER_STATUS_FILE",
    "storage/observer_status.json",
)


def write_observer_status(state):
    state = dict(state) if isinstance(state, dict) else {}
    features = dict(state.get("state_features") or {})

    current_keys = (
        "state_id", "time", "market_time_ms", "market_time", "processed_at",
        "session_id", "gap_from_prev_minutes", "recovered", "symbol", "price",
        "regime", "trend_pct", "volatility_pct", "chosen_strategy", "action",
        "p_success", "prediction_horizon",
    )
    current = {k: state.get(k) for k in current_keys}
    current["state_features"] = features

    for key in (
        "tradeability_gate", "edge_gate", "phase_front_lag",
        "geometric_stability_reversal", "execution_horizon_arbitration",
        "economic_front_surface", "bipolar_pressure_model",
        "conditional_geometry_edge", "action_arbitration",
        "geometry_testnet_bridge", "execution_readiness",
    ):
        current[key] = state.get(key) or {}

    recent_states = []
    try:
        for row in _tail_jsonl(STATES_FILE, 60):
            if not isinstance(row, dict):
                continue
            recent_states.append({
                "state_id": row.get("state_id"),
                "time": row.get("time"),
                "market_time": row.get("market_time"),
                "price": row.get("price"),
                "trend_pct": row.get("trend_pct"),
                "volatility_pct": row.get("volatility_pct"),
                "action": row.get("action"),
            })
    except Exception:
        recent_states = []

    payload = {
        "export_version": "MORX1-UI",
        "trader_version": "1.23",
        "generated_at": now_iso(),
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "scope": "OBSERVER_OPERATOR_STATUS",
        "security": "No API keys, secrets, signed headers, or exchange credentials.",
        "current_state": current,
        "derived": {
            "PFL1": state.get("phase_front_lag", latest_phase_front_lag),
            "EFS1": state.get("economic_front_surface", latest_economic_front_surface),
            "BPM1": state.get("bipolar_pressure_model", latest_bipolar_pressure),
            "EH1": state.get("execution_horizon_arbitration", latest_execution_horizon_arbitration),
            "GSR1": state.get("geometric_stability_reversal", latest_geometric_stability_reversal),
            "CGE1": state.get("conditional_geometry_edge", latest_conditional_geometry_edge),
            "AAL1": state.get("action_arbitration", latest_action_arbitration),
            "ERL1": state.get("execution_readiness", latest_execution_readiness),
            "GDX1": state.get("geometry_testnet_bridge", latest_geometry_testnet_bridge),
            "SCR1": session_continuity_tracker,
            "GAP1": latest_gap_forecast,
            "GRC1": latest_gap_reconciliation,
            "RES1": latest_model_residual,
        },
        "runtime_counts": {
            "state_id": state_id,
            "candle_seq": candle_seq,
            "pending_predictions": len(pending_predictions),
            "pending_shadows": len(pending_shadows),
            "pending_tradeability": len(pending_tradeability_probes),
            "pending_corridor": len(pending_corridor_probes),
            "pending_corridor2": len(pending_corridor2_probes),
            "pending_corridor3": len(pending_corridor3_probes),
            "pending_gol2": len(pending_geometry_outcome_probes),
            "gol2_cells": len(geometry_outcome_matrix),
            "tom1_cells": len(transition_outcome_matrix),
            "teg1_cells": len(transition_edge_matrix),
        },
        "metrics": {
            "model_residual_tracker": model_residual_tracker,
        },
        "recent": {
            "states": recent_states,
        },
    }

    ok, info = _safe_write_json(OBSERVER_STATUS_FILE, payload)
    return {"ok": ok, "path": info}


def build_analysis_export(state=None, full=False):
    state = dict(state) if isinstance(state, dict) else (_latest_state_from_storage() or {})
    recent_n = ANALYSIS_EXPORT_FULL_RECENT if full else ANALYSIS_EXPORT_RECENT
    payload = {
        "export_version": ANALYSIS_EXPORT_VERSION,
        "trader_version": "1.23",
        "generated_at": now_iso(),
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "scope": "FULL_RESEARCH_EXPORT" if full else "LATEST_ANALYSIS_SNAPSHOT",
        "security": "API keys, secrets and signed request headers are intentionally excluded.",
        "current_state": state,
        "derived": {
            "PFL1": state.get("phase_front_lag", latest_phase_front_lag),
            "EFS1": state.get("economic_front_surface", latest_economic_front_surface),
            "BPM1": state.get("bipolar_pressure_model", latest_bipolar_pressure),
            "EH1": state.get("execution_horizon_arbitration", latest_execution_horizon_arbitration),
            "GSR1": state.get("geometric_stability_reversal", latest_geometric_stability_reversal),
            "CGE1": state.get("conditional_geometry_edge", latest_conditional_geometry_edge),
            "AAL1": state.get("action_arbitration", latest_action_arbitration),
            "ERL1": state.get("execution_readiness", latest_execution_readiness),
            "GDX1": state.get("geometry_testnet_bridge", latest_geometry_testnet_bridge),
            "SCR1": session_continuity_tracker,
            "GAP1": latest_gap_forecast,
            "GRC1": latest_gap_reconciliation,
            "RES1": latest_model_residual,
        },
        "runtime_counts": {
            "state_id": state_id,
            "candle_seq": candle_seq,
            "pending_predictions": len(pending_predictions),
            "pending_shadows": len(pending_shadows),
            "pending_tradeability": len(pending_tradeability_probes),
            "pending_corridor": len(pending_corridor_probes),
            "pending_corridor2": len(pending_corridor2_probes),
            "pending_corridor3": len(pending_corridor3_probes),
            "pending_gol2": len(pending_geometry_outcome_probes),
            "gol2_cells": len(geometry_outcome_matrix),
            "tom1_cells": len(transition_outcome_matrix),
            "teg1_cells": len(transition_edge_matrix),
        },
        "metrics": {
            "shadow_metrics": shadow_metrics,
            "horizon_system_metrics": horizon_system_metrics,
            "adaptive_metrics": adaptive_metrics,
            "tradeability_metrics": tradeability_metrics,
            "corridor_metrics": corridor_metrics,
            "corridor2_metrics": corridor2_metrics,
            "corridor3_metrics": corridor3_metrics,
            "geometry_outcome_metrics": geometry_outcome_metrics,
            "economic_front_tracker": economic_front_tracker,
            "bipolar_pressure_tracker": bipolar_pressure_tracker,
            "session_continuity_tracker": session_continuity_tracker,
            "model_residual_tracker": model_residual_tracker,
        },
        "recent": {
            "states": _tail_jsonl(STATES_FILE, min(recent_n, 120 if not full else recent_n)),
            "phase_front_states": _tail_jsonl(PHASE_FRONT_STATES_FILE, recent_n),
            "economic_front_states": _tail_jsonl(ECONOMIC_FRONT_STATES_FILE, recent_n),
            "bipolar_pressure_states": _tail_jsonl(BPM1_STATES_FILE, recent_n),
            "geometry_outcomes": _tail_jsonl(GEOMETRY_OUTCOME_FACTS_FILE, recent_n),
            "tradeability_facts": _tail_jsonl(TRADEABILITY_FACTS_FILE, recent_n),
            "corridor_facts": _tail_jsonl(CORRIDOR_FACTS_FILE, recent_n),
            "corridor2_facts": _tail_jsonl(CORRIDOR2_FACTS_FILE, recent_n),
            "corridor3_facts": _tail_jsonl(CORRIDOR3_FACTS_FILE, recent_n),
            "paper_trades": _tail_jsonl(PAPER_TRADES_FILE, min(recent_n, 100)),
            "exchange_trades": _tail_jsonl(EXCHANGE_TRADES_FILE, min(recent_n, 100)),
            "gap_forecasts": _tail_jsonl(GAP_FORECASTS_FILE, min(recent_n, 50)),
            "gap_observed": _tail_jsonl(GAP_OBSERVED_FILE, min(recent_n, 50)),
            "gap_reconciliations": _tail_jsonl(GAP_RECONCILIATIONS_FILE, min(recent_n, 50)),
            "model_residuals": _tail_jsonl(MODEL_RESIDUALS_FILE, min(recent_n, 50)),
        },
    }
    if full:
        payload["matrices"] = {
            "horizon_matrix": horizon_matrix,
            "tradeability_feature_matrix": tradeability_feature_matrix,
            "corridor_feature_matrix": corridor_feature_matrix,
            "corridor2_feature_matrix": corridor2_feature_matrix,
            "corridor3_feature_matrix": corridor3_feature_matrix,
            "geometry_outcome_matrix": geometry_outcome_matrix,
            "transition_outcome_matrix": transition_outcome_matrix,
            "transition_edge_matrix": transition_edge_matrix,
        }
        payload["trackers"] = {
            "geometry_tracker": geometry_tracker,
            "cone_tracker": cone_tracker,
            "cone_transition_tracker": cone_transition_tracker,
            "phase_topology_tracker": phase_topology_tracker,
            "phase_front_tracker": phase_front_tracker,
            "economic_front_tracker": economic_front_tracker,
            "bipolar_pressure_tracker": bipolar_pressure_tracker,
            "session_continuity_tracker": session_continuity_tracker,
            "model_residual_tracker": model_residual_tracker,
        }
        last_gap_id = session_continuity_tracker.get("last_gap_id")
        if last_gap_id:
            for kind in ("blind", "bridge", "observed", "comparison"):
                path = os.path.join(GAP_DETAIL_DIR, f"{last_gap_id}_{kind}.json")
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        payload.setdefault("gap_detail", {})[kind] = json.load(f)
                except Exception:
                    pass
    return payload


def write_analysis_export(state=None, full=False, also_download=True):
    payload = build_analysis_export(state=state, full=full)
    local_path = ANALYSIS_EXPORT_FILE if not full else "storage/mor_analysis_export_full.json"
    ok_local, info_local = _safe_write_json(local_path, payload)
    download_path = ANALYSIS_EXPORT_FULL_DOWNLOAD_FILE if full else ANALYSIS_EXPORT_DOWNLOAD_FILE
    ok_download, info_download = (False, "SKIPPED")
    if also_download:
        ok_download, info_download = _safe_write_json(download_path, payload)
    if not full:
        # Tiny audit index, one record per state; avoids duplicating the full snapshot.
        append_jsonl(ANALYSIS_EXPORT_HISTORY_FILE, {
            "time": payload["generated_at"],
            "state_id": payload.get("current_state", {}).get("state_id"),
            "price": payload.get("current_state", {}).get("price"),
            "PFL1": payload["derived"].get("PFL1", {}),
            "EFS1": payload["derived"].get("EFS1", {}),
            "EH1": payload["derived"].get("EH1", {}),
            "GSR1": payload["derived"].get("GSR1", {}),
            "CGE1": payload["derived"].get("CGE1", {}),
            "AAL1": payload["derived"].get("AAL1", {}),
            "ERL1": payload["derived"].get("ERL1", {}),
            "GDX1": payload["derived"].get("GDX1", {}),
            "SCR1": payload["derived"].get("SCR1", {}),
            "GAP1": _gap_summary(payload["derived"].get("GAP1", {})),
            "GRC1": _gap_summary(payload["derived"].get("GRC1", {})),
            "RES1": payload["derived"].get("RES1", {}),
        })
    return {
        "ok_local": ok_local, "local": info_local,
        "ok_download": ok_download, "download": info_download,
        "full": bool(full),
    }


def compute_execution_readiness(state):
    raw_action = str(state.get("action", "HOLD")).upper()
    gdx = state.get("geometry_testnet_bridge", {})
    aal = state.get("action_arbitration", {})
    action_source = "STRATEGY"
    action = raw_action
    horizon = int(state.get("prediction_horizon", TRADE_HORIZON))

    if isinstance(aal, dict) and aal.get("ready", False):
        aal_action = str(aal.get("final_action", raw_action)).upper()
        aal_horizon = int(aal.get("final_horizon", horizon) or horizon)
        aal_status = str(aal.get("status", ""))
        if aal_status == "CONFLICT_HOLD":
            action = "HOLD"
            horizon = aal_horizon
            action_source = ACTION_ARBITRATION_VERSION
        elif aal_status in ("GEOMETRY_OVERRIDE_TESTNET", "GEOMETRY_ONLY_TESTNET"):
            action = aal_action if aal_action in ("BUY", "SELL") else "HOLD"
            horizon = aal_horizon
            action_source = ACTION_ARBITRATION_VERSION

    features = state.get("state_features", {})
    regime = str(state.get("regime", "RANGE"))
    strategy = str(state.get("chosen_strategy", "NONE"))

    tg = tradeability_gate(features, horizon, action)
    if action in ("BUY", "SELL"):
        if strategy != "NONE":
            eg_full = edge_gate_at_horizon(regime, strategy, action, horizon)
            eg = {
                "allowed": bool(eg_full.get("allowed", False)),
                "reason": eg_full.get("reason", "UNKNOWN"),
                **dict(eg_full.get("horizon_info", {}) or {}),
            }
        else:
            eg = {
                "allowed": False,
                "reason": "NO_STRATEGY_EDGE_CONTEXT",
                "validated": False,
                "avg_trade_net_edge_pct": None,
                "min_samples_across_horizons": 0,
            }
    else:
        eg = {"allowed": True, "reason": "HOLD", "validated": True}

    alignment, tilt_rows = erl1_geometry_alignment(state, action)
    topo_score, topology_class, propagation_direction = erl1_topology_support(state, action)
    evidence = erl1_flag_evidence(state, horizon)
    evidence_score = 0.0
    if evidence:
        best = evidence[0]
        evidence_score = min(
            1.0,
            0.5 * best["rate"] / max(ERL1_MIN_FLAG_RATE, 1e-9)
            + 0.5 * min(1.0, best["samples"] / 50.0),
        )

    gsr = state.get("geometric_stability_reversal", {})
    cf = gsr.get("counterfactual", {}) if isinstance(gsr, dict) else {}
    if action in ("BUY", "SELL") and isinstance(cf, dict) and action in cf:
        side_gsr = cf.get(action, {}) or {}
        gsr_ready = bool(gsr.get("ready", False))
        gsr_cont = float(side_gsr.get("continuation_index", 0.0) or 0.0)
        gsr_rev = float(side_gsr.get("reversal_index", 0.0) or 0.0)
        gsr_testnet_safe = bool(side_gsr.get("testnet_safe", False))
        gsr_strict_safe = bool(side_gsr.get("strict_safe", False))
    else:
        gsr_ready = bool(gsr.get("ready", False))
        gsr_cont = float(gsr.get("continuation_index", 0.0) or 0.0)
        gsr_rev = float(gsr.get("reversal_index", 1.0) if not gsr_ready else gsr.get("reversal_index", 0.0))
        gsr_testnet_safe = bool(gsr.get("testnet_safe", False))
        gsr_strict_safe = bool(gsr.get("strict_safe", False))

    gsr_quality = max(0.0, min(1.0, gsr_cont * (1.0 - 0.70 * gsr_rev))) if gsr_ready else 0.0

    eh = state.get("execution_horizon_arbitration", {})
    eh_exec = None
    if isinstance(eh, dict):
        if action_source in (GEOMETRY_TESTNET_BRIDGE_VERSION, ACTION_ARBITRATION_VERSION):
            for row in eh.get("rows", []) or []:
                if (
                    str(row.get("action", "")).upper() == action
                    and int(row.get("horizon", -1) or -1) == horizon
                ):
                    eh_exec = row
                    break
        else:
            eh_exec = eh.get("execution_candidate")
    eh_score = float(eh_exec.get("score", 0.0) or 0.0) if isinstance(eh_exec, dict) else 0.0
    eh_status = str(eh_exec.get("status", "HOLD")) if isinstance(eh_exec, dict) else "HOLD"

    score = (
        (0.22 if bool(tg.get("allowed", False)) else 0.0)
        + (0.28 if bool(eg.get("allowed", False)) else 0.0)
        + 0.15 * alignment
        + 0.10 * topo_score
        + 0.15 * evidence_score
        + 0.10 * gsr_quality
    )
    score = max(0.0, min(1.0, 0.85 * score + 0.15 * eh_score))

    blockers = []
    if action == "HOLD":
        blockers.append("NO_DIRECTIONAL_ACTION")
    if action_source in (GEOMETRY_TESTNET_BRIDGE_VERSION, ACTION_ARBITRATION_VERSION) and action in ("BUY", "SELL"):
        blockers.append("GEOMETRY_ACTION_TESTNET_ONLY")
    if action_source == ACTION_ARBITRATION_VERSION and action == "HOLD":
        blockers.append("AAL1_CONFLICT_HOLD")
    if not bool(tg.get("allowed", False)):
        blockers.append("TRADEABILITY_GATE")
    if not bool(eg.get("allowed", False)):
        blockers.append("EDGE_GATE")
    if alignment < ERL1_MIN_GEOMETRY_ALIGNMENT:
        blockers.append("GEOMETRY_NOT_ALIGNED")
    if not evidence:
        blockers.append("NO_VALIDATED_ACTIVE_FLAG")
    if not gsr_ready:
        blockers.append("GSR1_WARMUP")
    else:
        if gsr_cont < GSR1_MIN_CONTINUATION:
            blockers.append("GSR1_CONTINUATION_LOW")
        if gsr_rev > GSR1_MAX_REVERSAL_RISK:
            blockers.append("GSR1_REVERSAL_RISK")
    if action in ("BUY", "SELL") and eh_status != "READY":
        blockers.append("EH1_NOT_READY")

    pfl = state.get("phase_front_lag", {})
    if action in ("BUY", "SELL") and isinstance(pfl, dict) and pfl.get("ready", False):
        pfl_dir = str(pfl.get("front_direction", "NONE"))
        expected_dir = "UP" if action == "BUY" else "DOWN"
        if (
            pfl_dir in ("UP", "DOWN")
            and pfl_dir != expected_dir
            and float(pfl.get("strength", 0.0) or 0.0) >= PFL1_CONFLICT_BLOCK
        ):
            blockers.append("PHASE_FRONT_OPPOSES_ACTION")

    preflight_ok = bool(execution_runtime.get("preflight_ok", False))
    if EXECUTION_MODE in ("TESTNET", "LIVE") and not preflight_ok:
        blockers.append("EXCHANGE_PREFLIGHT")
    if score < ERL1_MIN_SCORE:
        blockers.append("ERL1_SCORE_LOW")

    strict_ready = len(blockers) == 0 and action_source == "STRATEGY"

    relaxed_core = (
        TESTNET_RELAX_GATES
        and action in ("BUY", "SELL")
        and preflight_ok
        and alignment >= 0.34
        and gsr_testnet_safe
        and eh_status in ("OBSERVE", "CANDIDATE", "READY")
        and (
            action_source == "STRATEGY"
            or (
                action_source in (GEOMETRY_TESTNET_BRIDGE_VERSION, ACTION_ARBITRATION_VERSION)
                and TESTNET_GEOMETRY_ACTIONS
                and TESTNET_ACTION_ARBITRATION
                and bool(gdx.get("allowed", False))
            )
        )
    )
    testnet_ready = strict_ready or relaxed_core

    return {
        "version": EXECUTION_READINESS_VERSION,
        "mode": EXECUTION_MODE,
        "raw_action": raw_action,
        "action": action,
        "execution_action": action,
        "action_source": action_source,
        "execution_horizon": int(horizon),
        "score": round(score, 4),
        "strict_ready": strict_ready,
        "testnet_ready": testnet_ready,
        "geometry_alignment": round(alignment, 4),
        "tilts": [{"h": h, "tilt_deg": round(t, 3), "state": ss} for h, t, ss in tilt_rows],
        "topology_class": topology_class,
        "topology_support": round(topo_score, 4),
        "propagation_direction": propagation_direction,
        "validated_flags": evidence,
        "candidate_tradeability_gate": dict(tg),
        "candidate_edge_gate": dict(eg),
        "gsr1": dict(gsr),
        "gsr1_quality": round(gsr_quality, 4),
        "gsr1_continuation": round(gsr_cont, 4),
        "gsr1_reversal": round(gsr_rev, 4),
        "gsr1_testnet_safe": bool(gsr_testnet_safe),
        "gsr1_strict_safe": bool(gsr_strict_safe),
        "eh1": dict(eh) if isinstance(eh, dict) else {},
        "eh1_score": round(eh_score, 4),
        "eh1_status": eh_status,
        "pfl1": dict(state.get("phase_front_lag", {})) if isinstance(state.get("phase_front_lag", {}), dict) else {},
        "geometry_testnet_bridge": dict(gdx) if isinstance(gdx, dict) else {},
        "action_arbitration": dict(aal) if isinstance(aal, dict) else {},
        "conditional_geometry_edge": dict(state.get("conditional_geometry_edge", {})) if isinstance(state.get("conditional_geometry_edge", {}), dict) else {},
        "blockers": blockers,
        "preflight_ok": preflight_ok,
        "preflight_reason": str(execution_runtime.get("preflight_reason", "NOT_CHECKED")),
        "live_armed": LIVE_ARMED,
    }


def exchange_risk_governor(action, market_price, mode=None):
    mode = (mode or EXECUTION_MODE).upper()
    if not execution_runtime.get("preflight_ok", False):
        return {"allowed": False, "reason": "EXCHANGE_PREFLIGHT_FAILED", "notional_usdt": 0.0}

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    last_ms = int(execution_runtime.get("last_order_epoch_ms", 0) or 0)
    if last_ms and (now_ms - last_ms) < EXCHANGE_ORDER_COOLDOWN_SECONDS * 1000:
        return {"allowed": False, "reason": "ORDER_COOLDOWN", "notional_usdt": 0.0}

    try:
        account = binance_account(mode)
        usdt = binance_free_balance(account, "USDT")
        btc = binance_free_balance(account, "BTC")
    except Exception as e:
        return {"allowed": False, "reason": "ACCOUNT_READ_FAILED: " + str(e)[:160], "notional_usdt": 0.0}

    reserve = 1.0 - EXCHANGE_BALANCE_RESERVE_FRACTION
    min_notional = binance_symbol_min_notional(mode)

    if action == "BUY":
        notional = min(EXCHANGE_MAX_NOTIONAL_USDT, max(0.0, usdt * reserve))
    elif action == "SELL":
        notional = min(EXCHANGE_MAX_NOTIONAL_USDT, max(0.0, btc * float(market_price) * reserve))
    else:
        return {"allowed": False, "reason": "HOLD", "notional_usdt": 0.0}

    if notional + 1e-9 < min_notional:
        return {"allowed": False, "reason": f"BELOW_MIN_NOTIONAL_{min_notional:.4f}", "notional_usdt": 0.0}

    return {
        "allowed": True,
        "reason": "OK",
        "notional_usdt": round(notional, 8),
        "free_usdt": round(usdt, 8),
        "free_btc": round(btc, 12),
        "min_notional": round(min_notional, 8),
    }


def execute_exchange_order(state, mode=None):
    mode = (mode or EXECUTION_MODE).upper()
    readiness = state.get("execution_readiness", {})
    raw_action = str(state.get("action", "HOLD")).upper()
    action = raw_action

    # GDX1 can originate only a relaxed TESTNET action. LIVE always uses the
    # original strategy action and can never inherit a geometry-only direction.
    if (
        mode == "TESTNET"
        and TESTNET_RELAX_GATES
        and TESTNET_GEOMETRY_ACTIONS
        and isinstance(readiness, dict)
        and readiness.get("action_source") in (GEOMETRY_TESTNET_BRIDGE_VERSION, ACTION_ARBITRATION_VERSION)
        and bool(readiness.get("testnet_ready", False))
    ):
        candidate = str(readiness.get("execution_action", "HOLD")).upper()
        if candidate in ("BUY", "SELL"):
            action = candidate

    if mode == "LIVE":
        if not LIVE_ARMED:
            event = {"time": now_iso(), "state_id": state.get("state_id"), "action": action, "status": "BLOCKED_LIVE_NOT_ARMED", "reason": "MOR_LIVE_ARM phrase missing"}
            append_jsonl(EXCHANGE_TRADES_FILE, event)
            return event
        if not bool(readiness.get("strict_ready", False)):
            event = {"time": now_iso(), "state_id": state.get("state_id"), "action": action, "status": "BLOCKED_ERL1", "reason": ",".join(readiness.get("blockers", []))}
            append_jsonl(EXCHANGE_TRADES_FILE, event)
            return event
    elif mode == "TESTNET":
        if not bool(readiness.get("testnet_ready", False)):
            event = {"time": now_iso(), "state_id": state.get("state_id"), "action": action, "status": "BLOCKED_ERL1_TESTNET", "reason": ",".join(readiness.get("blockers", []))}
            append_jsonl(EXCHANGE_TRADES_FILE, event)
            return event
    else:
        return execute_paper_order(state)

    decision = exchange_risk_governor(action, state["price"], mode=mode)
    if not decision.get("allowed", False):
        event = {"time": now_iso(), "state_id": state.get("state_id"), "action": action, "status": "BLOCKED_EXCHANGE_RISK", "reason": decision.get("reason", "UNKNOWN")}
        append_jsonl(EXCHANGE_TRADES_FILE, event)
        print("EXCHANGE RISK GOVERNOR:", action, "BLOCKED |", event["reason"])
        return event

    notional = float(decision["notional_usdt"])
    params = {
        "symbol": SYMBOL,
        "side": action,
        "type": "MARKET",
        "quoteOrderQty": f"{notional:.8f}",
        "newOrderRespType": "FULL",
    }

    # Validate exact signed order shape first. /order/test never reaches matching engine.
    try:
        binance_signed_request("POST", "/api/v3/order/test", params, mode=mode)
        response = binance_signed_request("POST", "/api/v3/order", params, mode=mode)
    except Exception as e:
        event = {"time": now_iso(), "state_id": state.get("state_id"), "action": action, "status": "EXCHANGE_ERROR", "reason": str(e)[:500], "mode": mode, "notional_usdt": notional}
        append_jsonl(EXCHANGE_TRADES_FILE, event)
        return event

    execution_runtime["last_order_epoch_ms"] = int(datetime.now(timezone.utc).timestamp() * 1000)
    execution_runtime["last_order_id"] = response.get("orderId")
    execution_runtime["orders_sent"] = int(execution_runtime.get("orders_sent", 0)) + 1
    save_execution_state()

    event = {
        "time": now_iso(),
        "state_id": state.get("state_id"),
        "strategy": state.get("chosen_strategy"),
        "action": action,
        "status": "FILLED_TESTNET" if mode == "TESTNET" else "FILLED_LIVE",
        "mode": mode,
        "market_reference_price": state.get("price"),
        "notional_usdt": notional,
        "order_id": response.get("orderId"),
        "client_order_id": response.get("clientOrderId"),
        "executed_qty": response.get("executedQty"),
        "cummulative_quote_qty": response.get("cummulativeQuoteQty"),
        "exchange_status": response.get("status"),
        "readiness_score": readiness.get("score"),
    }
    append_jsonl(EXCHANGE_TRADES_FILE, event)
    print("=== EXCHANGE ORDER ===")
    print(mode, action, event["status"], "| notional=", f"{notional:.2f}", "USDT | orderId=", event["order_id"])
    return event


def phase_front_lag_dashboard_html():
    p = latest_phase_front_lag if isinstance(latest_phase_front_lag, dict) else {}
    seq = " → ".join(p.get("sequence", []) or []) or "warmup"
    direction = str(p.get("front_direction", "NONE"))
    mode = str(p.get("propagation_mode", "NONE"))
    front = f"H{int(p.get('from_horizon',0) or 0)}→H{int(p.get('front_horizon',0) or 0)}" if int(p.get("front_horizon",0) or 0) else "none"
    events = p.get("current_events", []) or []
    event_html = "".join(
        '<div class="erl-flag">'
        f'<b>H{int(e.get("horizon",0))} {geo_escape_html(str(e.get("from","F")))}→{geo_escape_html(str(e.get("to","F")))}</b>'
        f'<span>{geo_escape_html(str(e.get("direction","NONE")))}</span>'
        f'<span>Δ {int(e.get("delta",0)):+d}</span>'
        f'<span>{geo_escape_html(str(e.get("event_id","")))}</span>'
        '</div>' for e in events[-5:]
    ) or '<div class="sub">No new scale-state transition in this candle.</div>'
    return (
        '<section class="card erl-card">'
        '<h2>PFL1 · PHASE FRONT LAG / PROPAGATION</h2>'
        '<div class="model-note">Tracks the motion of a phase/deformation front through H5→H120. A front is linked from actual scale-state transitions; lag is elapsed time, v is log2(horizon)/minute. Research-only: it may veto a strong contradiction, never invent an order.</div>'
        '<div class="ctd-summary">'
        f'<div class="ctd-kpi"><b>STATE</b><br>{geo_escape_html(str(p.get("state","WARMUP")))}</div>'
        f'<div class="ctd-kpi"><b>FRONT</b><br>{geo_escape_html(direction)} · {geo_escape_html(mode)}</div>'
        f'<div class="ctd-kpi"><b>SCALE PATH</b><br>{geo_escape_html(front)}</div>'
        f'<div class="ctd-kpi"><b>LAG</b><br>{float(p.get("latency_minutes",0)):.2f}m</div>'
        f'<div class="ctd-kpi"><b>v SCALE</b><br>{float(p.get("velocity_log2h_per_min",0)):+.3f} log2H/m</div>'
        f'<div class="ctd-kpi"><b>STRENGTH</b><br>{float(p.get("strength",0)):.2f}</div>'
        f'<div class="ctd-kpi"><b>AGE</b><br>{float(p.get("age_minutes",0)):.1f}m</div>'
        f'<div class="ctd-kpi"><b>BIAS</b><br>{geo_escape_html(str(p.get("forecast_bias","NONE")))}</div>'
        '</div>'
        f'<div class="flags" style="margin-top:10px"><b>PHASE SEQUENCE</b><br>{geo_escape_html(seq)}</div>'
        '<div class="gol-leaders-title">CURRENT FRONT EVENTS</div>' + event_html + '</section>'
    )


def execution_horizon_arbitration_dashboard_html():
    e = latest_execution_horizon_arbitration if isinstance(latest_execution_horizon_arbitration, dict) else {}
    rows = e.get("rows", []) or []
    by_h = {}
    for row in rows:
        by_h.setdefault(int(row.get("horizon", 0) or 0), []).append(row)
    horizon_html = []
    for h in SHADOW_HORIZONS:
        side_rows = sorted(by_h.get(int(h), []), key=lambda r: float(r.get("score", 0.0)), reverse=True)
        best = side_rows[0] if side_rows else {}
        buy = next((r for r in side_rows if r.get("action") == "BUY"), {})
        sell = next((r for r in side_rows if r.get("action") == "SELL"), {})
        horizon_html.append(
            '<div class="erl-flag">'
            f'<b>H{int(h)} · {geo_escape_html(str(best.get("action","NONE")))} · {geo_escape_html(str(best.get("status","WARMUP")))}</b>'
            f'<span>Q {float(best.get("score",0)):.2f} · cost {float(best.get("cost_coverage",0)):.2f}x</span>'
            f'<span>B {float(buy.get("score",0)):.2f} / S {float(sell.get("score",0)):.2f}</span>'
            f'<span>tilt {float(best.get("local_geometry",{}).get("tilt_deg",0)):+.0f}° · GI {float(best.get("gsr_continuation",0)):.2f} · PF {float(best.get("pfl_score",0)):.2f}</span>'
            '</div>')
    return (
        '<section class="card erl-card">'
        '<h2>EH1 · EXECUTION HORIZON ARBITRATION</h2>'
        '<div class="model-note">H5/H15/H30/H60 are scored for BUY and SELL from cost coverage + horizon evidence + local cone geometry + counterfactual GSR + learned edge + PFL1 propagation. HOLD remains HOLD; EH1 only changes the horizon of an existing directional action.</div>'
        '<div class="ctd-summary">'
        f'<div class="ctd-kpi"><b>BEST GEOMETRY</b><br>{geo_escape_html(str(e.get("selected_action","NONE")))} @ H{int(e.get("selected_horizon",TRADE_HORIZON))}</div>'
        f'<div class="ctd-kpi"><b>BEST Q</b><br>{float(e.get("selected_score",0)):.2f} · {geo_escape_html(str(e.get("status","WARMUP")))}</div>'
        f'<div class="ctd-kpi"><b>RAW ACTION</b><br>{geo_escape_html(str(e.get("raw_action","HOLD")))}</div>'
        f'<div class="ctd-kpi"><b>EXEC HORIZON</b><br>H{int(e.get("execution_horizon",TRADE_HORIZON))} · {geo_escape_html(str(e.get("execution_status","HOLD")))}</div>'
        f'<div class="ctd-kpi"><b>EXEC Q</b><br>{float(e.get("execution_score",0)):.2f}</div>'
        f'<div class="ctd-kpi"><b>CONFLICT</b><br>{"YES" if e.get("direction_conflict") else "NO"}</div>'
        '</div>' + ''.join(horizon_html) + '</section>')


def economic_front_surface_dashboard_html():
    e = latest_economic_front_surface if isinstance(latest_economic_front_surface, dict) else {}
    rows = e.get("rows", []) if isinstance(e.get("rows", []), list) else []
    row_html = []
    for r in rows:
        cov = float(r.get("cost_coverage", 0.0) or 0.0)
        width = max(0.0, min(100.0, cov * 100.0))
        row_html.append(
            '<div class="efs-row">'
            f'<div><b>H{int(r.get("horizon",0))}</b> · {geo_escape_html(str(r.get("direction","NONE")))} · {geo_escape_html(str(r.get("classification","SUBCOST")))}</div>'
            f'<div class="sub">costCov {cov:.2f}x · Q {float(r.get("quality",0)):.2f} · motion {float(r.get("motion_budget_pct",0)):.4f}% · required {float(r.get("required_move_pct",0)):.4f}%</div>'
            f'<div style="height:7px;border:1px solid #334155;border-radius:6px;overflow:hidden;margin-top:5px"><div style="height:100%;width:{width:.1f}%;background:#7dd3fc"></div></div>'
            '</div>'
        )
    if not row_html:
        row_html = ['<div class="sub">EFS1 warmup.</div>']
    peak_h = e.get("peak_horizon")
    return (
        '<section class="card erl-card">'
        '<h2>EFS1 · ECONOMIC FRONT SURFACE</h2>'
        '<div class="model-note">Estimated motion-budget / execution-cost coverage across horizons. The peak is the current economically strongest scale, not a trade instruction. EFS1 is research-only and cannot bypass execution gates.</div>'
        '<div class="ctd-summary">'
        f'<div class="ctd-kpi"><b>STATE</b><br>{geo_escape_html(str(e.get("state","WARMUP")))}</div>'
        f'<div class="ctd-kpi"><b>PEAK</b><br>{("H"+str(peak_h)) if peak_h else "NONE"}</div>'
        f'<div class="ctd-kpi"><b>COST COVERAGE</b><br>{float(e.get("peak_cost_coverage",0)):.2f}x</div>'
        f'<div class="ctd-kpi"><b>DIRECTION</b><br>{geo_escape_html(str(e.get("peak_direction","NONE")))}</div>'
        f'<div class="ctd-kpi"><b>PEAK Q</b><br>{float(e.get("peak_quality",0)):.2f}</div>'
        f'<div class="ctd-kpi"><b>PEAK DRIFT</b><br>{geo_escape_html(str(e.get("peak_drift","NONE")))}</div>'
        '</div>' + ''.join(row_html) + '</section>'
    )


def geometric_stability_reversal_dashboard_html():
    g = latest_geometric_stability_reversal if isinstance(latest_geometric_stability_reversal, dict) else {}
    domain = g.get("domain") or {}
    shock = ("H" + str(g.get("shock_horizon"))) if g.get("shock_horizon") is not None else "none"
    cf = g.get("counterfactual", {}) if isinstance(g.get("counterfactual", {}), dict) else {}
    buy = cf.get("BUY", {}) if isinstance(cf.get("BUY", {}), dict) else {}
    sell = cf.get("SELL", {}) if isinstance(cf.get("SELL", {}), dict) else {}
    domain_text = (
        f"{domain.get('direction','NONE')} H{domain.get('start_h','?')}→H{domain.get('end_h','?')} age={float(domain.get('age_minutes',0)):.1f}m"
        if domain else "none"
    )
    return (
        '<section class="card erl-card">'
        '<h2>GSR1 · GEOMETRIC STABILITY / REVERSAL</h2>'
        '<div class="model-note">Continuation and reversal are separated. GI rewards directional alignment × coherence × persistence. RI rises with rotation energy × incoherence × shock. GSR1 never creates an action; it only gates execution readiness.</div>'
        '<div class="ctd-summary">'
        f'<div class="ctd-kpi"><b>VERDICT</b><br>{geo_escape_html(str(g.get("verdict","WARMUP")))}</div>'
        f'<div class="ctd-kpi"><b>GEOMETRY BIAS</b><br>{geo_escape_html(str(g.get("geometry_preferred_action","NONE")))}</div>'
        f'<div class="ctd-kpi"><b>BUY GI / RI</b><br>{float(buy.get("continuation_index",0)):.2f} / {float(buy.get("reversal_index",0)):.2f}</div>'
        f'<div class="ctd-kpi"><b>SELL GI / RI</b><br>{float(sell.get("continuation_index",0)):.2f} / {float(sell.get("reversal_index",0)):.2f}</div>'
        f'<div class="ctd-kpi"><b>GI / CONT</b><br>{float(g.get("continuation_index",0)):.2f} / {GSR1_MIN_CONTINUATION:.2f}</div>'
        f'<div class="ctd-kpi"><b>RI / REV</b><br>{float(g.get("reversal_index",0)):.2f} / {GSR1_MAX_REVERSAL_RISK:.2f}</div>'
        f'<div class="ctd-kpi"><b>STABILITY</b><br>{float(g.get("stability_index",0)):.2f}</div>'
        f'<div class="ctd-kpi"><b>COHERENCE</b><br>{float(g.get("rotation_coherence",0)):.2f}</div>'
        f'<div class="ctd-kpi"><b>ROT ENERGY</b><br>{float(g.get("rotation_energy_deg_per_min",0)):.2f}°/m</div>'
        f'<div class="ctd-kpi"><b>PERSISTENCE</b><br>{float(g.get("persistence",0)):.2f}</div>'
        f'<div class="ctd-kpi"><b>SHOCK</b><br>{geo_escape_html(shock)}</div>'
        '</div>'
        f'<div class="flags" style="margin-top:10px"><b>DOMAIN</b><br>{geo_escape_html(domain_text)}</div>'
        f'<div class="flags" style="margin-top:8px"><b>TOPOLOGY / PROP</b><br>{geo_escape_html(str(g.get("topology_class","NONE")))} · {geo_escape_html(str(g.get("propagation_direction","NONE")))}</div>'
        f'<div class="flags" style="margin-top:8px"><b>GATES</b><br>STRICT={"PASS" if g.get("strict_safe") else "BLOCK"} · TESTNET={"PASS" if g.get("testnet_safe") else "BLOCK"}</div>'
        '</section>'
    )


def bipolar_pressure_dashboard_html():
    b = latest_bipolar_pressure if isinstance(latest_bipolar_pressure, dict) else {}
    rows = b.get("horizons", {}) if isinstance(b.get("horizons", {}), dict) else {}
    row_html = []
    for h in BPM1_HORIZONS:
        r = rows.get(str(h), {}) or {}
        cross = " CROSS" if r.get("zero_cross") else ""
        row_html.append(
            f'<div class="erl-flag"><b>H{h}{cross}</b>'
            f'<span>q {float(r.get("q",0)):+.3f}</span>'
            f'<span>I {float(r.get("I",0)):.3f}</span>'
            f'<span>P {float(r.get("P",0)):+.3f}</span>'
            f'<span>dq {float(r.get("dq_per_min",0)):+.3f}/m</span>'
            f'<span>d²q {float(r.get("ddq_per_min2",0)):+.3f}/m²</span></div>'
        )
    crossings = b.get("crossings", []) or []
    cross_text = " · ".join(
        (f'{"GLOBAL" if str(x.get("horizon"))=="GLOBAL" else "H"+str(x.get("horizon"))}: '
         f'{int(x.get("from_sign",0)):+d}→{int(x.get("to_sign",0)):+d} '
         f'S={float(x.get("score",0)):.2f}')
        for x in crossings[-8:]
    ) or "NONE"
    return (
        '<section class="card erl-card">'
        '<h2>BPM1 · BIPOLAR PRESSURE FIELD</h2>'
        '<div class="model-note">Continuous common language for CTD/SPT/PFL/GSR/EFS: q∈[-1,1] is direction, I∈[0,1] is evidence/activity, P=q·I is structural push. q≈0 with high I is tense balance, not absence. Research-only in v1.23.</div>'
        '<div class="ctd-summary">'
        f'<div class="ctd-kpi"><b>STATE</b><br>{geo_escape_html(str(b.get("state","WARMUP")))}</div>'
        f'<div class="ctd-kpi"><b>q</b><br>{float(b.get("q",0)):+.3f}</div>'
        f'<div class="ctd-kpi"><b>I</b><br>{float(b.get("I",0)):.3f}</div>'
        f'<div class="ctd-kpi"><b>P=q·I</b><br>{float(b.get("P",0)):+.3f}</div>'
        f'<div class="ctd-kpi"><b>TENSION</b><br>{float(b.get("tension",0)):.3f}</div>'
        f'<div class="ctd-kpi"><b>dq/dt</b><br>{float(b.get("dq_per_min",0)):+.3f}/m</div>'
        f'<div class="ctd-kpi"><b>d²q/dt²</b><br>{float(b.get("ddq_per_min2",0)):+.3f}/m²</div>'
        f'<div class="ctd-kpi"><b>ZERO CROSS</b><br>{"YES" if b.get("zero_cross") else "NO"}</div>'
        f'<div class="ctd-kpi"><b>CROSS SCORE</b><br>{float(b.get("cross_score",0)):.3f}</div>'
        '</div>' + ''.join(row_html) +
        f'<div class="flags" style="margin-top:10px"><b>CROSSINGS</b><br>{geo_escape_html(cross_text)}</div>'
        '</section>'
    )


def conditional_geometry_edge_dashboard_html():
    c = latest_conditional_geometry_edge if isinstance(latest_conditional_geometry_edge, dict) else {}
    rows = c.get("rows", []) or []
    selected_action = str(c.get("selected_action", "NONE"))
    selected_h = int(c.get("selected_horizon", TRADE_HORIZON) or TRADE_HORIZON)
    selected = next((r for r in rows if str(r.get("action")) == selected_action and int(r.get("horizon", -1) or -1) == selected_h), {})
    row_html = []
    for h in SHADOW_HORIZONS:
        b = next((r for r in rows if int(r.get("horizon", -1) or -1) == h and r.get("action") == "BUY"), {})
        se = next((r for r in rows if int(r.get("horizon", -1) or -1) == h and r.get("action") == "SELL"), {})
        row_html.append(
            f'<div class="erl-flag"><b>H{h}</b>'
            f'<span>BUY {float(b.get("score",0)):.2f} {geo_escape_html(str(b.get("status","-")))}</span>'
            f'<span>SELL {float(se.get("score",0)):.2f} {geo_escape_html(str(se.get("status","-")))}</span>'
            f'<span>cells B/S {int(b.get("matched_cells",0))}/{int(se.get("matched_cells",0))}</span></div>'
        )
    return (
        '<section class="card erl-card">'
        '<h2>CGE1 · CONDITIONAL GEOMETRY EDGE EVIDENCE</h2>'
        '<div class="model-note">GOL2-conditioned directional evidence for the current geometry. It is research evidence, not a guaranteed expected-net-return estimate.</div>'
        '<div class="ctd-summary">'
        f'<div class="ctd-kpi"><b>BEST</b><br>{geo_escape_html(selected_action)} @ H{selected_h}</div>'
        f'<div class="ctd-kpi"><b>STATUS</b><br>{geo_escape_html(str(c.get("status","WARMUP")))}</div>'
        f'<div class="ctd-kpi"><b>SCORE</b><br>{float(c.get("selected_score",0)):.2f}</div>'
        f'<div class="ctd-kpi"><b>DIR GROSS</b><br>{float(selected.get("directional_gross_rate",0))*100:.1f}%</div>'
        f'<div class="ctd-kpi"><b>COST COVERED</b><br>{float(selected.get("gross_cost_covered_rate",0))*100:.1f}%</div>'
        f'<div class="ctd-kpi"><b>ASYM</b><br>{float(selected.get("oriented_asymmetry",0)):+.2f}</div>'
        '</div>' + ''.join(row_html) + '</section>'
    )


def action_arbitration_dashboard_html():
    a = latest_action_arbitration if isinstance(latest_action_arbitration, dict) else {}
    blockers = " · ".join(a.get("blockers", []) or []) or "NONE"
    return (
        '<section class="card erl-card">'
        '<h2>AAL1 · ACTION ARBITRATION</h2>'
        '<div class="model-note">Strategy and geometry are independent votes. Strong opposite geometry may veto to HOLD. Geometry may reverse/originate direction only in explicitly armed relaxed TESTNET; LIVE reversal is disabled.</div>'
        '<div class="ctd-summary">'
        f'<div class="ctd-kpi"><b>STATUS</b><br>{geo_escape_html(str(a.get("status","WARMUP")))}</div>'
        f'<div class="ctd-kpi"><b>STRATEGY</b><br>{geo_escape_html(str(a.get("strategy_action","HOLD")))}</div>'
        f'<div class="ctd-kpi"><b>GEOMETRY</b><br>{geo_escape_html(str(a.get("geometry_action","NONE")))} @ H{int(a.get("geometry_horizon",TRADE_HORIZON) or TRADE_HORIZON)}</div>'
        f'<div class="ctd-kpi"><b>FINAL</b><br>{geo_escape_html(str(a.get("final_action","HOLD")))}</div>'
        f'<div class="ctd-kpi"><b>GEO Q</b><br>{float(a.get("geometry_score",0)):.2f}</div>'
        f'<div class="ctd-kpi"><b>COST COV</b><br>{float(a.get("geometry_cost_coverage",0)):.2f}x</div>'
        f'<div class="ctd-kpi"><b>GSR Q</b><br>{float(a.get("geometry_gsr_quality",0)):.2f}</div>'
        f'<div class="ctd-kpi"><b>CGE1</b><br>{geo_escape_html(str(a.get("cge1_status","UNVALIDATED")))} · {float(a.get("cge1_score",0)):.2f}</div>'
        f'<div class="ctd-kpi"><b>EDGE</b><br>{"PASS" if a.get("geometry_edge_allowed") else "BLOCK"}</div>'
        f'<div class="ctd-kpi"><b>OVERRIDE ARM</b><br>{"YES" if a.get("testnet_override_armed") else "NO"}</div>'
        '</div>'
        f'<div class="flags" style="margin-top:10px"><b>BLOCKERS</b><br>{geo_escape_html(blockers)}</div>'
        '</section>'
    )


def geometry_testnet_bridge_dashboard_html():
    g = latest_geometry_testnet_bridge if isinstance(latest_geometry_testnet_bridge, dict) else {}
    blockers = " · ".join(g.get("blockers", []) or []) or "NONE"
    status = "ARMED" if g.get("allowed", False) else "BLOCKED"
    return (
        '<section class="card erl-card">'
        '<h2>GDX1 · GEOMETRY TESTNET BRIDGE</h2>'
        '<div class="model-note">Explicit TESTNET-only bridge from research geometry to an integration order. It cannot originate LIVE trades and never rewrites the research action.</div>'
        '<div class="ctd-summary">'
        f'<div class="ctd-kpi"><b>STATUS</b><br>{geo_escape_html(status)}</div>'
        f'<div class="ctd-kpi"><b>ACTION</b><br>{geo_escape_html(str(g.get("action","HOLD")))}</div>'
        f'<div class="ctd-kpi"><b>HORIZON</b><br>H{int(g.get("horizon",TRADE_HORIZON) or TRADE_HORIZON)}</div>'
        f'<div class="ctd-kpi"><b>SCORE</b><br>{float(g.get("score",0)):.2f} / {GDX1_MIN_SCORE:.2f}</div>'
        f'<div class="ctd-kpi"><b>COST COVERAGE</b><br>{float(g.get("cost_coverage",0)):.2f}x / {GDX1_MIN_COST_COVERAGE:.2f}x</div>'
        f'<div class="ctd-kpi"><b>LOCAL GEO</b><br>{float(g.get("local_geometry_score",0)):.2f}</div>'
        f'<div class="ctd-kpi"><b>GSR Q</b><br>{float(g.get("gsr_quality",0)):.2f}</div>'
        f'<div class="ctd-kpi"><b>PFL</b><br>{float(g.get("pfl_score",0)):.2f}</div>'
        f'<div class="ctd-kpi"><b>EFS MATCH</b><br>{"YES" if g.get("efs_match") else "NO"}</div>'
        '</div>'
        f'<div class="flags" style="margin-top:10px"><b>BLOCKERS</b><br>{geo_escape_html(blockers)}</div>'
        '</section>'
    )


def execution_readiness_dashboard_html():
    r = latest_execution_readiness if isinstance(latest_execution_readiness, dict) else {}
    blockers = " · ".join(r.get("blockers", [])) or "NONE"
    flags = r.get("validated_flags", []) or []
    flag_html = "".join(
        f'<div class="erl-flag"><b>{geo_escape_html(str(x.get("flag","")))}</b><span>n {int(x.get("samples",0))}</span><span>rate {float(x.get("rate",0))*100:.1f}%</span><span>avg {float(x.get("avg_best_net_pct",0)):+.4f}%</span></div>'
        for x in flags
    ) or '<div class="sub">No validated active flag yet.</div>'
    status = "READY" if r.get("strict_ready") else ("TESTNET_READY" if r.get("testnet_ready") else "BLOCKED")
    return (
        '<section class="card erl-card">'
        '<h2>ERL1 · EXECUTION READINESS</h2>'
        '<div class="model-note">Research-to-execution bridge. LIVE keeps Tradeability + Edge + GSR1 + ERL1 + exchange-risk as independent hard gates. Relaxed TESTNET may bypass research validation, but GSR1 remains a non-bypassable geometry-safety gate.</div>'
        '<div class="ctd-summary">'
        f'<div class="ctd-kpi"><b>MODE</b><br>{geo_escape_html(str(r.get("mode",EXECUTION_MODE)))}</div>'
        f'<div class="ctd-kpi"><b>STATUS</b><br>{geo_escape_html(status)}</div>'
        f'<div class="ctd-kpi"><b>ACTION</b><br>{geo_escape_html(str(r.get("action","HOLD")))}</div>'
        f'<div class="ctd-kpi"><b>SOURCE</b><br>{geo_escape_html(str(r.get("action_source","STRATEGY")))}</div>'
        f'<div class="ctd-kpi"><b>HORIZON</b><br>H{int(r.get("execution_horizon",TRADE_HORIZON) or TRADE_HORIZON)}</div>'
        f'<div class="ctd-kpi"><b>SCORE</b><br>{float(r.get("score",0)):.2f} / {ERL1_MIN_SCORE:.2f}</div>'
        f'<div class="ctd-kpi"><b>GEO ALIGN</b><br>{float(r.get("geometry_alignment",0))*100:.0f}%</div>'
        f'<div class="ctd-kpi"><b>TOPOLOGY</b><br>{geo_escape_html(str(r.get("topology_class","NONE")))}</div>'
        f'<div class="ctd-kpi"><b>GSR GI/RI</b><br>{float(r.get("gsr1_continuation",0)):.2f} / {float(r.get("gsr1_reversal",0)):.2f}</div>'
        f'<div class="ctd-kpi"><b>EH1</b><br>{geo_escape_html(str(r.get("eh1_status","HOLD")))} · {float(r.get("eh1_score",0)):.2f}</div>'
        f'<div class="ctd-kpi"><b>PFL1</b><br>{geo_escape_html(str(r.get("pfl1",{}).get("front_direction","NONE")))} · {float(r.get("pfl1",{}).get("strength",0)):.2f}</div>'
        f'<div class="ctd-kpi"><b>PREFLIGHT</b><br>{geo_escape_html(str(r.get("preflight_reason",execution_runtime.get("preflight_reason","NOT_CHECKED"))))}</div>'
        f'<div class="ctd-kpi"><b>LIVE ARM</b><br>{"YES" if LIVE_ARMED else "NO"}</div>'
        '</div>'
        f'<div class="flags" style="margin-top:10px"><b>BLOCKERS</b><br>{geo_escape_html(blockers)}</div>'
        '<div class="gol-leaders-title">ACTIVE VALIDATED STRUCTURAL EVIDENCE</div>'
        + flag_html + '</section>'
    )

def risk_governor(action, market_price):
    m = portfolio_metrics(market_price)
    equity = m["equity_usdt"]

    if action == "HOLD":
        return {
            "allowed": False,
            "reason": "HOLD",
            "notional_usdt": 0.0,
        }

    if action == "BUY":
        if m["drawdown_pct"] >= MAX_DRAWDOWN_PCT:
            return {
                "allowed": False,
                "reason": "MAX_DRAWDOWN_GUARDRAIL",
                "notional_usdt": 0.0,
            }

        max_exposure_value = equity * MAX_BTC_EXPOSURE
        remaining_exposure = max(
            0.0,
            max_exposure_value - m["btc_value_usdt"],
        )

        budget = min(
            equity * TRADE_FRACTION,
            remaining_exposure,
            portfolio["usdt"],
        )

        # Reserve simulated fee inside cash budget.
        budget = budget / (1.0 + PAPER_FEE_RATE)

        if budget < MIN_PAPER_NOTIONAL:
            return {
                "allowed": False,
                "reason": "INSUFFICIENT_BUY_CAPACITY",
                "notional_usdt": 0.0,
            }

        return {
            "allowed": True,
            "reason": "OK",
            "notional_usdt": budget,
        }

    if action == "SELL":
        btc_value = portfolio["btc"] * market_price

        if btc_value < MIN_PAPER_NOTIONAL:
            return {
                "allowed": False,
                "reason": "NO_SPOT_BTC_INVENTORY",
                "notional_usdt": 0.0,
            }

        notional = min(
            equity * TRADE_FRACTION,
            btc_value,
        )

        if notional < MIN_PAPER_NOTIONAL:
            return {
                "allowed": False,
                "reason": "SELL_BELOW_MIN_NOTIONAL",
                "notional_usdt": 0.0,
            }

        return {
            "allowed": True,
            "reason": "OK",
            "notional_usdt": notional,
        }

    return {
        "allowed": False,
        "reason": "UNKNOWN_ACTION",
        "notional_usdt": 0.0,
    }


def execute_paper_order(state):
    action = state["action"]
    market_price = state["price"]
    decision = risk_governor(
        action,
        market_price,
    )

    if not decision["allowed"]:
        event = {
            "time": now_iso(),
            "state_id": state["state_id"],
            "strategy": state["chosen_strategy"],
            "action": action,
            "market_price": market_price,
            "status": "BLOCKED",
            "reason": decision["reason"],
        }

        append_jsonl(
            PAPER_TRADES_FILE,
            event,
        )

        print(
            "RISK GOVERNOR:",
            action,
            "BLOCKED |",
            decision["reason"],
        )

        return event

    notional = decision["notional_usdt"]

    if action == "BUY":
        exec_price = market_price * (
            1.0 + PAPER_SLIPPAGE_RATE
        )

        qty = notional / exec_price
        fee = notional * PAPER_FEE_RATE
        cash_cost = notional + fee

        # Final numerical guard against floating overspend.
        if cash_cost > portfolio["usdt"]:
            cash_cost = portfolio["usdt"]
            notional = cash_cost / (
                1.0 + PAPER_FEE_RATE
            )
            fee = notional * PAPER_FEE_RATE
            qty = notional / exec_price

        portfolio["usdt"] -= cash_cost
        portfolio["btc"] += qty
        portfolio["btc_cost_basis_usdt"] += cash_cost
        portfolio["total_fees_usdt"] += fee
        portfolio["paper_trades"] += 1

        event = {
            "time": now_iso(),
            "state_id": state["state_id"],
            "strategy": state["chosen_strategy"],
            "action": "BUY",
            "status": "FILLED_PAPER",
            "market_price": market_price,
            "execution_price": exec_price,
            "qty_btc": qty,
            "notional_usdt": notional,
            "fee_usdt": fee,
            "slippage_rate": PAPER_SLIPPAGE_RATE,
        }

    else:
        exec_price = market_price * (
            1.0 - PAPER_SLIPPAGE_RATE
        )

        target_qty = notional / exec_price
        qty_before = portfolio["btc"]
        qty = min(
            target_qty,
            qty_before,
        )

        if qty <= 0:
            return {
                "status": "BLOCKED",
                "reason": "NO_SPOT_BTC_INVENTORY",
            }

        gross_proceeds = qty * exec_price
        fee = gross_proceeds * PAPER_FEE_RATE
        net_proceeds = gross_proceeds - fee

        basis_fraction = (
            qty / qty_before
            if qty_before > 0
            else 0.0
        )

        allocated_basis = (
            portfolio["btc_cost_basis_usdt"]
            * basis_fraction
        )

        realized = (
            net_proceeds
            - allocated_basis
        )

        portfolio["btc"] -= qty
        portfolio["btc_cost_basis_usdt"] -= (
            allocated_basis
        )
        portfolio["usdt"] += net_proceeds
        portfolio["realized_pnl_usdt"] += realized
        portfolio["total_fees_usdt"] += fee
        portfolio["paper_trades"] += 1

        if portfolio["btc"] < 1e-12:
            portfolio["btc"] = 0.0
            portfolio["btc_cost_basis_usdt"] = 0.0

        event = {
            "time": now_iso(),
            "state_id": state["state_id"],
            "strategy": state["chosen_strategy"],
            "action": "SELL",
            "status": "FILLED_PAPER",
            "market_price": market_price,
            "execution_price": exec_price,
            "qty_btc": qty,
            "gross_proceeds_usdt": gross_proceeds,
            "net_proceeds_usdt": net_proceeds,
            "fee_usdt": fee,
            "realized_pnl_usdt": realized,
            "slippage_rate": PAPER_SLIPPAGE_RATE,
        }

    append_jsonl(
        PAPER_TRADES_FILE,
        event,
    )

    snap = portfolio_snapshot(
        market_price,
        state["state_id"],
        "PAPER_ORDER",
    )

    print()
    print("=== PAPER ORDER ===")
    print(
        event["action"],
        event["status"],
        "| strategy=",
        state["chosen_strategy"],
    )
    print(
        "market=",
        f"{market_price:.2f}",
        "| exec=",
        f"{event['execution_price']:.2f}",
    )
    print(
        "USDT=",
        f"{portfolio['usdt']:.2f}",
        "| BTC=",
        f"{portfolio['btc']:.8f}",
    )
    print(
        "equity=",
        f"{snap['equity_usdt']:.2f}",
        "| exposure=",
        f"{snap['exposure_pct']:.2f}%",
    )
    print(
        "drawdown=",
        f"{snap['drawdown_pct']:.4f}%",
        "| fees=",
        f"{portfolio['total_fees_usdt']:.4f}",
    )
    print("===================")

    save_runtime()
    return event


def fetch_due_close_map(due_times):
    """
    Resolve all overdue horizons with one recent-kline request whenever
    possible. Pending multi-horizon shadows span at most ~60 minutes at
    shutdown, so one <=1000-candle window is normally sufficient.
    """
    due_times = sorted({
        int(x)
        for x in due_times
    })

    if not due_times:
        return {}

    start = max(
        0,
        due_times[0]
        - 2 * MINUTE_MS,
    )

    span_minutes = int(
        math.ceil(
            (
                due_times[-1]
                - start
            )
            / MINUTE_MS
        )
    ) + 5

    limit = min(
        1000,
        max(
            10,
            span_minutes,
        ),
    )

    data = rest_klines(
        limit=limit,
        start_time=start,
    )

    candles_by_close = {
        int(k[6]): {
            "close": float(k[4]),
            "close_time_ms": int(k[6]),
        }
        for k in data
    }

    available_times = sorted(
        candles_by_close
    )

    result = {}

    for due in due_times:
        exact = candles_by_close.get(due)

        if exact is not None:
            result[due] = exact
            continue

        # Defensive fallback: first closed candle at/after due.
        for close_time in available_times:
            if close_time >= due:
                result[due] = (
                    candles_by_close[
                        close_time
                    ]
                )
                break

    return result


def classify_market():
    # Preserve v0.9 production semantics:
    # regime/trend still use only the most recent 30 closed candles.
    view = list(candles)[-30:]

    closes = [c["close"] for c in view]
    highs = [c["high"] for c in view]
    lows = [c["low"] for c in view]

    if len(closes) < 10:
        return "WARMUP", 0.0, 0.0

    trend_pct = (
        (closes[-1] - closes[0])
        / closes[0]
    ) * 100

    avg_range = sum(
        (
            (h - l)
            / ((h + l) / 2)
        ) * 100
        for h, l in zip(
            highs,
            lows,
        )
    ) / len(highs)

    if avg_range > 0.35:
        regime = "HIGH_VOLATILITY"
    elif trend_pct > 0.30:
        regime = "TREND_UP"
    elif trend_pct < -0.30:
        regime = "TREND_DOWN"
    else:
        regime = "RANGE"

    return (
        regime,
        trend_pct,
        avg_range,
    )


def strategy_signals(regime, trend_pct):
    signals = {
        "MOMENTUM": "HOLD",
        "MEAN_REVERSION": "HOLD",
        "BREAKOUT": "HOLD",
    }

    if regime == "TREND_UP":
        signals["MOMENTUM"] = "BUY"

    elif regime == "TREND_DOWN":
        signals["MOMENTUM"] = "SELL"

    elif regime == "RANGE":
        if trend_pct < -0.10:
            signals["MEAN_REVERSION"] = "BUY"
        elif trend_pct > 0.10:
            signals["MEAN_REVERSION"] = "SELL"

    elif regime == "HIGH_VOLATILITY":
        if trend_pct > 0.20:
            signals["BREAKOUT"] = "BUY"
        elif trend_pct < -0.20:
            signals["BREAKOUT"] = "SELL"

    return signals



def mean_value(values):
    values = list(values)

    if not values:
        return 0.0

    return sum(values) / len(values)


def std_value(values):
    values = list(values)

    if len(values) < 2:
        return 0.0

    mu = mean_value(values)

    variance = sum(
        (x - mu) ** 2
        for x in values
    ) / len(values)

    return math.sqrt(variance)


def pct_return(a, b):
    if not a:
        return 0.0

    return (
        (b - a) / a
    ) * 100.0


def trend_over_bars(
    closes,
    bars,
):
    if len(closes) < bars + 1:
        return 0.0

    return pct_return(
        closes[-bars - 1],
        closes[-1],
    )


def candle_range_pct(candle):
    mid = (
        candle["high"]
        + candle["low"]
    ) / 2.0

    if mid <= 0:
        return 0.0

    return (
        (
            candle["high"]
            - candle["low"]
        )
        / mid
    ) * 100.0


def roundtrip_buy_break_even_move_pct():
    ratio = (
        (1.0 + PAPER_FEE_RATE)
        * (1.0 + PAPER_SLIPPAGE_RATE)
    ) / (
        (1.0 - PAPER_SLIPPAGE_RATE)
        * (1.0 - PAPER_FEE_RATE)
    )

    return (
        (ratio - 1.0) * 100.0
        + TRADEABILITY_SAFETY_MARGIN_PCT
    )


def roundtrip_sell_break_even_move_pct():
    # Required DOWN move magnitude for:
    # owned BTC -> SELL now -> buy back same BTC later.
    ratio = (
        (1.0 - PAPER_SLIPPAGE_RATE)
        * (1.0 - PAPER_FEE_RATE)
    ) / (
        (1.0 + PAPER_SLIPPAGE_RATE)
        * (1.0 + PAPER_FEE_RATE)
    )

    return (
        (1.0 - ratio) * 100.0
        + TRADEABILITY_SAFETY_MARGIN_PCT
    )


def feature_tradeability_score(
    features,
    horizon,
):
    key = (
        f"motion_budget_h"
        f"{int(horizon)}_pct"
    )

    budget = float(
        features.get(key, 0.0)
    )

    barrier = float(
        features.get(
            "required_move_buy_pct",
            0.0,
        )
    )

    if barrier <= 0:
        return 0.0

    return budget / barrier


def compute_state_features():
    view = list(candles)

    if len(view) < 21:
        return {
            "version":
                STATE_REP_VERSION,
            "ready": False,
            "history_length":
                len(view),
        }

    closes = [
        float(c["close"])
        for c in view
    ]
    highs = [
        float(c["high"])
        for c in view
    ]
    lows = [
        float(c["low"])
        for c in view
    ]
    volumes = [
        float(c["volume"])
        for c in view
    ]

    returns = [
        pct_return(
            closes[i - 1],
            closes[i],
        )
        for i in range(
            1,
            len(closes),
        )
    ]

    # ATR-like normalized true ranges.
    true_ranges_pct = []

    for i in range(
        1,
        len(view),
    ):
        prev_close = closes[i - 1]
        high = highs[i]
        low = lows[i]

        tr = max(
            high - low,
            abs(
                high - prev_close
            ),
            abs(
                low - prev_close
            ),
        )

        tr_pct = (
            (tr / prev_close) * 100.0
            if prev_close > 0
            else 0.0
        )

        true_ranges_pct.append(
            tr_pct
        )

    atr14 = mean_value(
        true_ranges_pct[-14:]
    )

    rv20 = std_value(
        returns[-20:]
    )

    rv60 = std_value(
        returns[-60:]
    )

    range20 = [
        candle_range_pct(c)
        for c in view[-20:]
    ]

    range5 = [
        candle_range_pct(c)
        for c in view[-5:]
    ]

    avg_range20 = mean_value(
        range20
    )

    avg_range5 = mean_value(
        range5
    )

    compression = (
        avg_range5 / avg_range20
        if avg_range20 > 0
        else 1.0
    )

    sma20 = mean_value(
        closes[-20:]
    )

    last = closes[-1]

    distance_sma20 = (
        pct_return(
            sma20,
            last,
        )
        if sma20 > 0
        else 0.0
    )

    prior_high20 = max(
        highs[-21:-1]
    )

    prior_low20 = min(
        lows[-21:-1]
    )

    breakout_strength = 0.0

    if last > prior_high20:
        breakout_strength = (
            pct_return(
                prior_high20,
                last,
            )
        )
    elif last < prior_low20:
        breakout_strength = -abs(
            pct_return(
                prior_low20,
                last,
            )
        )

    low20 = min(
        lows[-20:]
    )

    high20 = max(
        highs[-20:]
    )

    range_position20 = (
        (last - low20)
        / (high20 - low20)
        if high20 > low20
        else 0.5
    )

    current_volume = volumes[-1]
    prior_volume20 = volumes[-21:-1]

    vol_mean20 = mean_value(
        prior_volume20
    )

    vol_std20 = std_value(
        prior_volume20
    )

    volume_z20 = (
        (
            current_volume
            - vol_mean20
        )
        / vol_std20
        if vol_std20 > 0
        else 0.0
    )

    volume_ratio20 = (
        current_volume
        / vol_mean20
        if vol_mean20 > 0
        else 1.0
    )

    current = view[-1]

    candle_span = (
        current["high"]
        - current["low"]
    )

    if candle_span > 0:
        body_ratio = (
            abs(
                current["close"]
                - current["open"]
            )
            / candle_span
        )

        upper_wick = (
            current["high"]
            - max(
                current["open"],
                current["close"],
            )
        ) / candle_span

        lower_wick = (
            min(
                current["open"],
                current["close"],
            )
            - current["low"]
        ) / candle_span
    else:
        body_ratio = 0.0
        upper_wick = 0.0
        lower_wick = 0.0

    trend5 = trend_over_bars(
        closes,
        5,
    )

    trend15 = trend_over_bars(
        closes,
        15,
    )

    trend30 = trend_over_bars(
        closes,
        30,
    )

    trend60 = trend_over_bars(
        closes,
        60,
    )

    signs = [
        1 if x > 0
        else -1 if x < 0
        else 0
        for x in (
            trend5,
            trend15,
            trend30,
        )
    ]

    if all(
        s > 0
        for s in signs
    ):
        alignment = 1
    elif all(
        s < 0
        for s in signs
    ):
        alignment = -1
    else:
        alignment = 0

    required_buy = (
        roundtrip_buy_break_even_move_pct()
    )

    required_sell = (
        roundtrip_sell_break_even_move_pct()
    )

    features = {
        "version":
            STATE_REP_VERSION,
        "ready": True,
        "history_length":
            len(view),
        "price": last,

        "trend_5_pct":
            round(trend5, 6),
        "trend_15_pct":
            round(trend15, 6),
        "trend_30_pct":
            round(trend30, 6),
        "trend_60_pct":
            round(trend60, 6),
        "slope_alignment":
            alignment,

        "atr_14_pct":
            round(atr14, 6),
        "realized_vol_20_pct":
            round(rv20, 6),
        "realized_vol_60_pct":
            round(rv60, 6),
        "avg_range_20_pct":
            round(avg_range20, 6),

        "volume_z_20":
            round(volume_z20, 6),
        "volume_ratio_20":
            round(volume_ratio20, 6),

        "distance_sma20_pct":
            round(
                distance_sma20,
                6,
            ),
        "range_position_20":
            round(
                range_position20,
                6,
            ),
        "breakout_strength_20_pct":
            round(
                breakout_strength,
                6,
            ),
        "compression_5_20":
            round(
                compression,
                6,
            ),

        "candle_body_ratio":
            round(body_ratio, 6),
        "upper_wick_ratio":
            round(upper_wick, 6),
        "lower_wick_ratio":
            round(lower_wick, 6),

        "required_move_buy_pct":
            round(required_buy, 6),
        "required_move_sell_pct":
            round(required_sell, 6),
    }

    # Preregistered heuristic:
    # optimistic motion envelope = max(ATR, realized vol) * sqrt(H).
    # The lab will test whether this is a useful gate.
    base_motion = max(
        atr14,
        rv20,
        avg_range20,
    )

    for horizon in (
        TRADEABILITY_HORIZONS
    ):
        motion_budget = (
            base_motion
            * math.sqrt(
                float(horizon)
            )
        )

        score = (
            motion_budget
            / required_buy
            if required_buy > 0
            else 0.0
        )

        p_tradeable = (
            score
            / (1.0 + score)
            if score >= 0
            else 0.0
        )

        features[
            f"motion_budget_h{horizon}_pct"
        ] = round(
            motion_budget,
            6,
        )

        features[
            f"tradeability_score_h{horizon}"
        ] = round(
            score,
            6,
        )

        features[
            f"p_tradeable_h{horizon}"
        ] = round(
            p_tradeable,
            6,
        )

    return features


def tradeability_gate(
    features,
    horizon,
    action,
):
    if action == "HOLD":
        return {
            "allowed": True,
            "reason": "HOLD",
            "score": 0.0,
            "threshold":
                TRADEABILITY_SCORE_THRESHOLD,
            "motion_budget_pct":
                0.0,
            "required_move_pct":
                features.get(
                    "required_move_buy_pct",
                    0.0,
                ),
        }

    if not TRADEABILITY_GATE_ENABLED:
        return {
            "allowed": True,
            "reason":
                "TRADEABILITY_GATE_DISABLED",
            "score": None,
            "threshold":
                TRADEABILITY_SCORE_THRESHOLD,
            "motion_budget_pct":
                None,
            "required_move_pct":
                None,
        }

    if not features.get(
        "ready",
        False,
    ):
        return {
            "allowed": False,
            "reason":
                "STATE_FEATURES_WARMUP",
            "score": 0.0,
            "threshold":
                TRADEABILITY_SCORE_THRESHOLD,
            "motion_budget_pct":
                0.0,
            "required_move_pct":
                0.0,
        }

    horizon = int(horizon)

    budget = float(
        features.get(
            f"motion_budget_h{horizon}_pct",
            0.0,
        )
    )

    required = float(
        features.get(
            (
                "required_move_buy_pct"
                if action == "BUY"
                else
                "required_move_sell_pct"
            ),
            0.0,
        )
    )

    score = (
        budget / required
        if required > 0
        else 0.0
    )

    allowed = (
        score
        >= TRADEABILITY_SCORE_THRESHOLD
    )

    return {
        "allowed": allowed,
        "reason": (
            "MOTION_BUDGET_ABOVE_COST"
            if allowed
            else
            "MOTION_BUDGET_BELOW_COST"
        ),
        "score":
            round(score, 6),
        "threshold":
            TRADEABILITY_SCORE_THRESHOLD,
        "motion_budget_pct":
            round(budget, 6),
        "required_move_pct":
            round(required, 6),
    }


def tradeability_bucket(
    features,
    horizon,
):
    score = float(
        features.get(
            f"tradeability_score_h{int(horizon)}",
            0.0,
        )
    )

    if score < 0.25:
        score_band = "S0_<0.25"
    elif score < 0.5:
        score_band = "S1_0.25-0.5"
    elif score < 1.0:
        score_band = "S2_0.5-1"
    elif score < 1.5:
        score_band = "S3_1-1.5"
    elif score < 2.0:
        score_band = "S4_1.5-2"
    else:
        score_band = "S5_>=2"

    vz = float(
        features.get(
            "volume_z_20",
            0.0,
        )
    )

    if vz < -1.0:
        volume_band = "VOL_LOW"
    elif vz > 1.0:
        volume_band = "VOL_HIGH"
    else:
        volume_band = "VOL_NORMAL"

    comp = float(
        features.get(
            "compression_5_20",
            1.0,
        )
    )

    if comp < 0.7:
        compression_band = "COMPRESSED"
    elif comp > 1.3:
        compression_band = "EXPANDED"
    else:
        compression_band = "NORMAL"

    alignment = int(
        features.get(
            "slope_alignment",
            0,
        )
    )

    alignment_band = {
        -1: "ALIGN_DOWN",
        0: "ALIGN_MIXED",
        1: "ALIGN_UP",
    }.get(
        alignment,
        "ALIGN_MIXED",
    )

    return (
        f"H{int(horizon)}|"
        f"{score_band}|"
        f"{volume_band}|"
        f"{compression_band}|"
        f"{alignment_band}"
    )


def create_tradeability_probe(
    state,
    close_time_ms,
    horizon,
):
    features = state[
        "state_features"
    ]

    horizon = int(horizon)

    score = float(
        features.get(
            f"tradeability_score_h{horizon}",
            0.0,
        )
    )

    p_tradeable = float(
        features.get(
            f"p_tradeable_h{horizon}",
            0.0,
        )
    )

    probe = {
        "probe_id":
            f"TG-{state['state_id']}-H{horizon}",
        "created_at": now_iso(),
        "state_id":
            state["state_id"],
        "state_representation":
            STATE_REP_VERSION,
        "regime":
            state["regime"],
        "entry_price":
            state["price"],
        "horizon_candles":
            horizon,
        "entry_close_time_ms":
            close_time_ms,
        "due_close_time_ms":
            close_time_ms
            + horizon
            * MINUTE_MS,

        "score":
            round(score, 6),
        "p_tradeable":
            round(p_tradeable, 6),
        "predicted_tradeable":
            score
            >= TRADEABILITY_SCORE_THRESHOLD,

        "features": features,
        "portfolio_snapshot":
            state.get(
                "paper_portfolio_before",
                {},
            ),
        "status": "FROZEN",
    }

    probe["fingerprint"] = (
        fingerprint(probe)
    )

    pending_tradeability_probes.append(
        probe
    )


def create_multi_horizon_tradeability_probes(
    state,
    close_time_ms,
):
    for horizon in (
        TRADEABILITY_HORIZONS
    ):
        create_tradeability_probe(
            state,
            close_time_ms,
            horizon,
        )


def update_tradeability_feature_matrix(
    probe,
    observed_market_tradeable,
    best_net_pct,
):
    horizon = int(
        probe["horizon_candles"]
    )

    key = tradeability_bucket(
        probe["features"],
        horizon,
    )

    cell = (
        tradeability_feature_matrix.setdefault(
            key,
            {
                "horizon_candles":
                    horizon,
                "samples": 0,
                "market_tradeable": 0,
                "best_net_sum_pct":
                    0.0,
                "abs_move_sum_pct":
                    0.0,
                "last_updated":
                    None,
            },
        )
    )

    cell["samples"] += 1

    if observed_market_tradeable:
        cell[
            "market_tradeable"
        ] += 1

    cell[
        "best_net_sum_pct"
    ] += best_net_pct

    cell["last_updated"] = (
        now_iso()
    )

    n = max(
        1,
        cell["samples"],
    )

    cell[
        "market_tradeable_rate"
    ] = round(
        cell["market_tradeable"]
        / n,
        6,
    )

    cell[
        "avg_best_net_pct"
    ] = round(
        cell[
            "best_net_sum_pct"
        ] / n,
        6,
    )


def resolve_tradeability_probe(
    probe,
    exit_price,
    observed_close_time_ms,
):
    entry = float(
        probe["entry_price"]
    )

    raw_move = pct_return(
        entry,
        exit_price,
    )

    buy_net = (
        simulate_buy_roundtrip_return_pct(
            entry,
            exit_price,
        )
    )

    sell_net = (
        simulate_sell_owned_roundtrip_return_pct(
            entry,
            exit_price,
        )
    )

    best_net = max(
        0.0,
        buy_net,
        sell_net,
    )

    market_tradeable = (
        best_net > 0.0
    )

    predicted = bool(
        probe[
            "predicted_tradeable"
        ]
    )

    p = float(
        probe[
            "p_tradeable"
        ]
    )

    outcome = (
        1.0
        if market_tradeable
        else 0.0
    )

    brier = (
        p - outcome
    ) ** 2

    hkey = str(
        int(
            probe[
                "horizon_candles"
            ]
        )
    )

    metric = (
        tradeability_metrics[hkey]
    )

    metric["samples"] += 1

    if predicted:
        metric[
            "predicted_positive"
        ] += 1

    if market_tradeable:
        metric[
            "observed_market_positive"
        ] += 1

    if predicted and market_tradeable:
        metric["tp"] += 1
    elif predicted and not market_tradeable:
        metric["fp"] += 1
    elif (
        not predicted
        and market_tradeable
    ):
        metric["fn"] += 1
    else:
        metric["tn"] += 1

    metric["brier_sum"] += brier
    metric[
        "abs_move_sum_pct"
    ] += abs(raw_move)
    metric[
        "best_net_sum_pct"
    ] += best_net

    n = max(
        1,
        metric["samples"],
    )

    metric["brier"] = round(
        metric["brier_sum"] / n,
        6,
    )

    metric["observed_rate"] = round(
        metric[
            "observed_market_positive"
        ] / n,
        6,
    )

    metric["predicted_rate"] = round(
        metric[
            "predicted_positive"
        ] / n,
        6,
    )

    precision_den = (
        metric["tp"]
        + metric["fp"]
    )

    recall_den = (
        metric["tp"]
        + metric["fn"]
    )

    metric["precision"] = (
        round(
            metric["tp"]
            / precision_den,
            6,
        )
        if precision_den > 0
        else None
    )

    metric["recall"] = (
        round(
            metric["tp"]
            / recall_den,
            6,
        )
        if recall_den > 0
        else None
    )

    metric[
        "avg_abs_move_pct"
    ] = round(
        metric[
            "abs_move_sum_pct"
        ] / n,
        6,
    )

    metric[
        "avg_best_net_pct"
    ] = round(
        metric[
            "best_net_sum_pct"
        ] / n,
        6,
    )

    update_tradeability_feature_matrix(
        probe,
        market_tradeable,
        best_net,
    )

    fact = {
        "probe_id":
            probe["probe_id"],
        "observed_at": now_iso(),
        "observed_close_time_ms":
            observed_close_time_ms,
        "state_id":
            probe["state_id"],
        "state_representation":
            STATE_REP_VERSION,
        "regime":
            probe["regime"],
        "horizon_candles":
            probe[
                "horizon_candles"
            ],
        "entry_price":
            entry,
        "exit_price":
            exit_price,
        "raw_move_pct":
            round(
                raw_move,
                6,
            ),
        "buy_roundtrip_net_pct":
            round(
                buy_net,
                6,
            ),
        "sell_owned_roundtrip_net_pct":
            round(
                sell_net,
                6,
            ),
        "best_market_net_pct":
            round(
                best_net,
                6,
            ),
        "market_tradeable":
            market_tradeable,
        "predicted_tradeable":
            predicted,
        "tradeability_score":
            probe["score"],
        "p_tradeable": p,
        "brier":
            round(
                brier,
                6,
            ),
        "features":
            probe["features"],
        "probe_hash":
            probe["fingerprint"],
        "status": "OBSERVED",
    }

    append_jsonl(
        TRADEABILITY_FACTS_FILE,
        fact,
    )

    print()
    print(
        "=== TRADEABILITY FACT",
        f"H={probe['horizon_candles']}",
        "===",
    )

    print(
        probe["probe_id"],
        "|",
        f"move={raw_move:+.5f}%",
        "|",
        f"best_net={best_net:+.5f}%",
    )

    print(
        "predicted=",
        predicted,
        "| observed=",
        market_tradeable,
        "| score=",
        f"{probe['score']:.3f}",
        "| p=",
        f"{p:.3f}",
        "| Brier=",
        f"{brier:.4f}",
    )

    print(
        "BUY_RT=",
        f"{buy_net:+.5f}%",
        "| SELL_OWNED_RT=",
        f"{sell_net:+.5f}%",
    )

    print(
        "SR1:",
        f"ATR={probe['features'].get('atr_14_pct', 0):.5f}%",
        f"RV20={probe['features'].get('realized_vol_20_pct', 0):.5f}%",
        f"VolZ={probe['features'].get('volume_z_20', 0):+.2f}",
        f"Comp={probe['features'].get('compression_5_20', 1):.2f}",
    )

    print(
        "==========================",
    )


def evaluate_tradeability_due(
    current_price,
    current_close_time_ms,
):
    global pending_tradeability_probes

    remaining = []

    for probe in (
        pending_tradeability_probes
    ):
        if (
            current_close_time_ms
            < probe[
                "due_close_time_ms"
            ]
        ):
            remaining.append(
                probe
            )
            continue

        resolve_tradeability_probe(
            probe,
            current_price,
            current_close_time_ms,
        )

    pending_tradeability_probes = (
        remaining
    )


def restore_tradeability_overdue():
    global pending_tradeability_probes

    if not pending_tradeability_probes:
        return

    now_ms = int(
        datetime.now(
            timezone.utc
        ).timestamp()
        * 1000
    )

    overdue = [
        int(
            p[
                "due_close_time_ms"
            ]
        )
        for p
        in pending_tradeability_probes
        if int(
            p[
                "due_close_time_ms"
            ]
        ) <= now_ms
    ]

    if not overdue:
        return

    print(
        "Recovering",
        len(set(overdue)),
        "tradeability horizon(s)...",
    )

    exact = fetch_due_close_map(
        overdue
    )

    remaining = []

    for probe in (
        pending_tradeability_probes
    ):
        result = exact.get(
            int(
                probe[
                    "due_close_time_ms"
                ]
            )
        )

        if result is None:
            remaining.append(
                probe
            )
            continue

        resolve_tradeability_probe(
            probe,
            result["close"],
            result["close_time_ms"],
        )

    pending_tradeability_probes = (
        remaining
    )


def print_tradeability_summary():
    print()
    print(
        "=== SR1 TRADEABILITY SUMMARY ==="
    )

    for horizon in (
        TRADEABILITY_HORIZONS
    ):
        m = tradeability_metrics[
            str(horizon)
        ]

        precision = (
            "n/a"
            if m.get("precision")
            is None
            else
            f"{m['precision']*100:.1f}%"
        )

        recall = (
            "n/a"
            if m.get("recall")
            is None
            else
            f"{m['recall']*100:.1f}%"
        )

        print(
            f"H{horizon}:",
            f"n={m['samples']}",
            f"observed={m.get('observed_rate', 0)*100:.1f}%",
            f"predicted={m.get('predicted_rate', 0)*100:.1f}%",
            f"precision={precision}",
            f"recall={recall}",
            f"Brier={m.get('brier', 0):.4f}",
        )

    leaders = tradeability_leaderboard(
        limit=3
    )

    if leaders:
        print(
            "feature buckets with highest",
            "observed tradeability:"
        )

        for i, cell in enumerate(
            leaders,
            1,
        ):
            print(
                f"{i}.",
                cell["key"],
                f"rate={cell.get('market_tradeable_rate', 0)*100:.1f}%",
                f"n={cell.get('samples', 0)}",
                f"avg_best_net={cell.get('avg_best_net_pct', 0):+.5f}%",
            )
    else:
        print(
            "validated feature buckets:",
            "NONE YET",
        )

    print(
        "================================",
    )



def corridor_window_features(
    view,
    window,
    price,
    required_move_pct,
):
    window = int(window)

    if len(view) < window:
        return {
            "ready": False,
            "window": window,
        }

    current = view[-window:]

    highs = [
        float(c["high"])
        for c in current
    ]

    lows = [
        float(c["low"])
        for c in current
    ]

    high = max(highs)
    low = min(lows)

    width_pct = (
        ((high - low) / price) * 100.0
        if price > 0
        else 0.0
    )

    if high > low:
        position = (
            (price - low)
            / (high - low)
        )
    else:
        position = 0.5

    position = max(
        0.0,
        min(
            1.0,
            position,
        ),
    )

    dist_upper_pct = (
        ((high - price) / price) * 100.0
        if price > 0
        else 0.0
    )

    dist_lower_pct = (
        ((price - low) / price) * 100.0
        if price > 0
        else 0.0
    )

    edge_distance_norm = min(
        position,
        1.0 - position,
    )

    edge_pressure = (
        1.0
        - min(
            1.0,
            2.0
            * edge_distance_norm,
        )
    )

    cost_normalized = (
        width_pct / required_move_pct
        if required_move_pct > 0
        else 0.0
    )

    # Compare current same-sized corridor with the immediately
    # previous same-sized corridor (one candle earlier).
    if len(view) >= window + 1:
        previous = view[
            -window - 1:-1
        ]

        prev_high = max(
            float(c["high"])
            for c in previous
        )

        prev_low = min(
            float(c["low"])
            for c in previous
        )

        prev_width_pct = (
            (
                (prev_high - prev_low)
                / float(
                    previous[-1][
                        "close"
                    ]
                )
            )
            * 100.0
            if float(
                previous[-1]["close"]
            ) > 0
            else 0.0
        )

        expansion_ratio = (
            width_pct
            / prev_width_pct
            if prev_width_pct > 0
            else 1.0
        )

        breakout_up_pct = (
            pct_return(
                prev_high,
                price,
            )
            if price > prev_high
            else 0.0
        )

        breakout_down_pct = (
            -abs(
                pct_return(
                    prev_low,
                    price,
                )
            )
            if price < prev_low
            else 0.0
        )
    else:
        prev_high = high
        prev_low = low
        prev_width_pct = width_pct
        expansion_ratio = 1.0
        breakout_up_pct = 0.0
        breakout_down_pct = 0.0

    return {
        "ready": True,
        "window": window,
        "high": round(
            high,
            8,
        ),
        "low": round(
            low,
            8,
        ),
        "width_pct": round(
            width_pct,
            6,
        ),
        "position": round(
            position,
            6,
        ),
        "distance_upper_pct": round(
            dist_upper_pct,
            6,
        ),
        "distance_lower_pct": round(
            dist_lower_pct,
            6,
        ),
        "edge_pressure": round(
            edge_pressure,
            6,
        ),
        "cost_normalized_width": round(
            cost_normalized,
            6,
        ),
        "previous_width_pct": round(
            prev_width_pct,
            6,
        ),
        "expansion_ratio": round(
            expansion_ratio,
            6,
        ),
        "breakout_up_pct": round(
            breakout_up_pct,
            6,
        ),
        "breakout_down_pct": round(
            breakout_down_pct,
            6,
        ),
    }


def corridor_regime_label(
    windows,
    ratios,
):
    c5 = windows.get("5", {})
    c15 = windows.get("15", {})
    c30 = windows.get("30", {})
    c60 = windows.get("60", {})

    breakout_30 = (
        float(
            c30.get(
                "breakout_up_pct",
                0.0,
            )
        )
        > 0.0
        or float(
            c30.get(
                "breakout_down_pct",
                0.0,
            )
        )
        < 0.0
    )

    breakout_60 = (
        float(
            c60.get(
                "breakout_up_pct",
                0.0,
            )
        )
        > 0.0
        or float(
            c60.get(
                "breakout_down_pct",
                0.0,
            )
        )
        < 0.0
    )

    nested = (
        float(
            ratios.get(
                "width_5_30",
                1.0,
            )
        )
        <= CORRIDOR_NESTED_MICRO_RATIO
        and float(
            ratios.get(
                "width_15_60",
                1.0,
            )
        )
        <= CORRIDOR_NESTED_MESO_RATIO
    )

    micro_expand = (
        float(
            c5.get(
                "expansion_ratio",
                1.0,
            )
        )
        >= CORRIDOR_EXPANSION_RATIO
    )

    meso_expand = (
        float(
            c15.get(
                "expansion_ratio",
                1.0,
            )
        )
        >= CORRIDOR_EXPANSION_RATIO
    )

    contracted = (
        float(
            c5.get(
                "expansion_ratio",
                1.0,
            )
        )
        <= CORRIDOR_CONTRACTION_RATIO
        and float(
            c15.get(
                "expansion_ratio",
                1.0,
            )
        )
        <= 1.0
    )

    edge_pressure = max(
        float(
            c30.get(
                "edge_pressure",
                0.0,
            )
        ),
        float(
            c60.get(
                "edge_pressure",
                0.0,
            )
        ),
    )

    if breakout_60:
        return "MACRO_BREAKOUT"

    if breakout_30:
        return "MESO_BREAKOUT"

    if nested and micro_expand:
        return "NESTED_COMPRESSION_RELEASE"

    if nested:
        return "NESTED_COMPRESSION"

    if (
        micro_expand
        and meso_expand
    ):
        return "MULTISCALE_EXPANSION"

    if micro_expand:
        return "MICRO_EXPANSION"

    if contracted:
        return "MULTISCALE_CONTRACTION"

    if (
        edge_pressure
        >= CORRIDOR_EDGE_PRESSURE_THRESHOLD
    ):
        return "EDGE_PRESSURE"

    return "NESTED_RANGE"


def compute_corridor_features():
    view = list(candles)

    if len(view) < max(
        CORRIDOR_WINDOWS
    ):
        return {
            "version":
                CORRIDOR_VERSION,
            "ready": False,
            "history_length":
                len(view),
        }

    price = float(
        view[-1]["close"]
    )

    required_move = (
        roundtrip_buy_break_even_move_pct()
    )

    windows = {}

    for window in CORRIDOR_WINDOWS:
        windows[str(window)] = (
            corridor_window_features(
                view,
                window,
                price,
                required_move,
            )
        )

    def safe_ratio(a, b):
        return (
            a / b
            if b > 0
            else 0.0
        )

    w5 = float(
        windows["5"][
            "width_pct"
        ]
    )

    w15 = float(
        windows["15"][
            "width_pct"
        ]
    )

    w30 = float(
        windows["30"][
            "width_pct"
        ]
    )

    w60 = float(
        windows["60"][
            "width_pct"
        ]
    )

    w120 = float(
        windows["120"][
            "width_pct"
        ]
    )

    ratios = {
        "width_5_15": round(
            safe_ratio(
                w5,
                w15,
            ),
            6,
        ),
        "width_5_30": round(
            safe_ratio(
                w5,
                w30,
            ),
            6,
        ),
        "width_15_30": round(
            safe_ratio(
                w15,
                w30,
            ),
            6,
        ),
        "width_15_60": round(
            safe_ratio(
                w15,
                w60,
            ),
            6,
        ),
        "width_30_60": round(
            safe_ratio(
                w30,
                w60,
            ),
            6,
        ),
        "width_30_120": round(
            safe_ratio(
                w30,
                w120,
            ),
            6,
        ),
        "width_60_120": round(
            safe_ratio(
                w60,
                w120,
            ),
            6,
        ),
    }

    label = corridor_regime_label(
        windows,
        ratios,
    )

    # Research-only pure corridor capacity score.
    # It deliberately does NOT use SR1 ATR/RV. This lets us compare
    # whether nested corridor geometry adds information independently.
    score_by_horizon = {}

    for horizon in CORRIDOR_HORIZONS:
        key = str(
            int(horizon)
        )

        width = float(
            windows[key][
                "width_pct"
            ]
        )

        score = (
            width
            / required_move
            if required_move > 0
            else 0.0
        )

        p = (
            score
            / (1.0 + score)
            if score >= 0
            else 0.0
        )

        score_by_horizon[key] = {
            "corridor_capacity_score":
                round(
                    score,
                    6,
                ),
            "p_tradeable":
                round(
                    p,
                    6,
                ),
            "predicted_tradeable":
                score
                >= CORRIDOR_SCORE_THRESHOLD,
        }

    return {
        "version":
            CORRIDOR_VERSION,
        "ready": True,
        "history_length":
            len(view),
        "price": price,
        "required_move_pct":
            round(
                required_move,
                6,
            ),
        "state_label":
            label,
        "windows":
            windows,
        "ratios":
            ratios,
        "score_by_horizon":
            score_by_horizon,
    }


def corridor_bucket(
    features,
    horizon,
):
    horizon = int(horizon)

    label = features.get(
        "state_label",
        "UNKNOWN",
    )

    w = features[
        "windows"
    ][str(horizon)]

    cnr = float(
        w.get(
            "cost_normalized_width",
            0.0,
        )
    )

    if cnr < 0.25:
        cnr_band = "CNR0_<0.25"
    elif cnr < 0.5:
        cnr_band = "CNR1_0.25-0.5"
    elif cnr < 1.0:
        cnr_band = "CNR2_0.5-1"
    elif cnr < 1.5:
        cnr_band = "CNR3_1-1.5"
    elif cnr < 2.0:
        cnr_band = "CNR4_1.5-2"
    else:
        cnr_band = "CNR5_>=2"

    exp = float(
        w.get(
            "expansion_ratio",
            1.0,
        )
    )

    if exp <= CORRIDOR_CONTRACTION_RATIO:
        exp_band = "CONTRACTING"
    elif exp >= CORRIDOR_EXPANSION_RATIO:
        exp_band = "EXPANDING"
    else:
        exp_band = "STABLE"

    pressure = float(
        w.get(
            "edge_pressure",
            0.0,
        )
    )

    pressure_band = (
        "EDGE"
        if pressure
        >= CORRIDOR_EDGE_PRESSURE_THRESHOLD
        else "CENTER"
    )

    return (
        f"H{horizon}|"
        f"{label}|"
        f"{cnr_band}|"
        f"{exp_band}|"
        f"{pressure_band}"
    )


def create_corridor_probe(
    state,
    close_time_ms,
    horizon,
):
    corridor = state[
        "corridor_features"
    ]

    sr1 = state[
        "state_features"
    ]

    horizon = int(horizon)

    cor_score = float(
        corridor[
            "score_by_horizon"
        ][str(horizon)][
            "corridor_capacity_score"
        ]
    )

    cor_p = float(
        corridor[
            "score_by_horizon"
        ][str(horizon)][
            "p_tradeable"
        ]
    )

    sr1_score = float(
        sr1.get(
            f"tradeability_score_h{horizon}",
            0.0,
        )
    )

    sr1_p = float(
        sr1.get(
            f"p_tradeable_h{horizon}",
            0.0,
        )
    )

    probe = {
        "probe_id":
            f"COR-{state['state_id']}-H{horizon}",
        "created_at": now_iso(),
        "state_id":
            state["state_id"],
        "corridor_version":
            CORRIDOR_VERSION,
        "state_representation":
            STATE_REP_VERSION,
        "regime":
            state["regime"],
        "entry_price":
            state["price"],
        "horizon_candles":
            horizon,
        "entry_close_time_ms":
            close_time_ms,
        "due_close_time_ms":
            close_time_ms
            + horizon
            * MINUTE_MS,

        "corridor_score":
            round(
                cor_score,
                6,
            ),
        "corridor_p":
            round(
                cor_p,
                6,
            ),
        "corridor_predicted_tradeable":
            cor_score
            >= CORRIDOR_SCORE_THRESHOLD,

        "sr1_score":
            round(
                sr1_score,
                6,
            ),
        "sr1_p":
            round(
                sr1_p,
                6,
            ),
        "sr1_predicted_tradeable":
            sr1_score
            >= TRADEABILITY_SCORE_THRESHOLD,

        "corridor_features":
            corridor,
        "status": "FROZEN",
    }

    probe["fingerprint"] = (
        fingerprint(probe)
    )

    pending_corridor_probes.append(
        probe
    )


def create_multi_horizon_corridor_probes(
    state,
    close_time_ms,
):
    if not CORRIDOR_RESEARCH_ENABLED:
        return

    if not state[
        "corridor_features"
    ].get(
        "ready",
        False,
    ):
        return

    for horizon in CORRIDOR_HORIZONS:
        create_corridor_probe(
            state,
            close_time_ms,
            horizon,
        )


def update_corridor_matrix(
    probe,
    observed_market_tradeable,
    best_net_pct,
):
    features = probe[
        "corridor_features"
    ]

    horizon = int(
        probe["horizon_candles"]
    )

    key = corridor_bucket(
        features,
        horizon,
    )

    cell = (
        corridor_feature_matrix.setdefault(
            key,
            {
                "horizon_candles":
                    horizon,
                "state_label":
                    features[
                        "state_label"
                    ],
                "samples": 0,
                "market_tradeable": 0,
                "best_net_sum_pct":
                    0.0,
                "last_updated":
                    None,
            },
        )
    )

    cell["samples"] += 1

    if observed_market_tradeable:
        cell[
            "market_tradeable"
        ] += 1

    cell[
        "best_net_sum_pct"
    ] += best_net_pct

    cell["last_updated"] = (
        now_iso()
    )

    n = max(
        1,
        cell["samples"],
    )

    cell[
        "market_tradeable_rate"
    ] = round(
        cell[
            "market_tradeable"
        ] / n,
        6,
    )

    cell[
        "avg_best_net_pct"
    ] = round(
        cell[
            "best_net_sum_pct"
        ] / n,
        6,
    )


def resolve_corridor_probe(
    probe,
    exit_price,
    observed_close_time_ms,
):
    entry = float(
        probe["entry_price"]
    )

    raw_move = pct_return(
        entry,
        exit_price,
    )

    buy_net = (
        simulate_buy_roundtrip_return_pct(
            entry,
            exit_price,
        )
    )

    sell_net = (
        simulate_sell_owned_roundtrip_return_pct(
            entry,
            exit_price,
        )
    )

    best_net = max(
        0.0,
        buy_net,
        sell_net,
    )

    observed = (
        best_net > 0.0
    )

    cor_pred = bool(
        probe[
            "corridor_predicted_tradeable"
        ]
    )

    sr1_pred = bool(
        probe[
            "sr1_predicted_tradeable"
        ]
    )

    cor_p = float(
        probe[
            "corridor_p"
        ]
    )

    outcome = (
        1.0
        if observed
        else 0.0
    )

    brier = (
        cor_p - outcome
    ) ** 2

    h = str(
        int(
            probe[
                "horizon_candles"
            ]
        )
    )

    m = corridor_metrics[h]

    m["samples"] += 1

    if cor_pred:
        m[
            "predicted_positive"
        ] += 1

    if observed:
        m[
            "observed_market_positive"
        ] += 1

    if cor_pred and observed:
        m["tp"] += 1
    elif cor_pred and not observed:
        m["fp"] += 1
    elif (
        not cor_pred
        and observed
    ):
        m["fn"] += 1
    else:
        m["tn"] += 1

    m["brier_sum"] += brier
    m[
        "best_net_sum_pct"
    ] += best_net
    m[
        "abs_move_sum_pct"
    ] += abs(raw_move)

    n = max(
        1,
        m["samples"],
    )

    m["brier"] = round(
        m["brier_sum"] / n,
        6,
    )

    m["observed_rate"] = round(
        m[
            "observed_market_positive"
        ] / n,
        6,
    )

    m["predicted_rate"] = round(
        m[
            "predicted_positive"
        ] / n,
        6,
    )

    pd = (
        m["tp"] + m["fp"]
    )

    rd = (
        m["tp"] + m["fn"]
    )

    m["precision"] = (
        round(
            m["tp"] / pd,
            6,
        )
        if pd > 0
        else None
    )

    m["recall"] = (
        round(
            m["tp"] / rd,
            6,
        )
        if rd > 0
        else None
    )

    compare = corridor_compare[h]

    compare["samples"] += 1

    cor_correct = (
        cor_pred == observed
    )

    sr1_correct = (
        sr1_pred == observed
    )

    if cor_pred != sr1_pred:
        compare[
            "disagreements"
        ] += 1

    if cor_correct and sr1_correct:
        compare[
            "both_correct"
        ] += 1
    elif (
        not cor_correct
        and not sr1_correct
    ):
        compare[
            "both_wrong"
        ] += 1
    elif sr1_correct:
        compare[
            "sr1_only_correct"
        ] += 1
    else:
        compare[
            "corridor_only_correct"
        ] += 1

    update_corridor_matrix(
        probe,
        observed,
        best_net,
    )

    fact = {
        "probe_id":
            probe["probe_id"],
        "observed_at": now_iso(),
        "observed_close_time_ms":
            observed_close_time_ms,
        "state_id":
            probe["state_id"],
        "corridor_version":
            CORRIDOR_VERSION,
        "horizon_candles":
            probe[
                "horizon_candles"
            ],
        "entry_price":
            entry,
        "exit_price":
            exit_price,
        "raw_move_pct":
            round(
                raw_move,
                6,
            ),
        "buy_roundtrip_net_pct":
            round(
                buy_net,
                6,
            ),
        "sell_owned_roundtrip_net_pct":
            round(
                sell_net,
                6,
            ),
        "best_market_net_pct":
            round(
                best_net,
                6,
            ),
        "observed_market_tradeable":
            observed,
        "corridor_predicted_tradeable":
            cor_pred,
        "sr1_predicted_tradeable":
            sr1_pred,
        "corridor_score":
            probe[
                "corridor_score"
            ],
        "corridor_p":
            cor_p,
        "sr1_score":
            probe[
                "sr1_score"
            ],
        "brier":
            round(
                brier,
                6,
            ),
        "corridor_features":
            probe[
                "corridor_features"
            ],
        "probe_hash":
            probe["fingerprint"],
        "research_only": True,
        "status": "OBSERVED",
    }

    append_jsonl(
        CORRIDOR_FACTS_FILE,
        fact,
    )

    cf = probe[
        "corridor_features"
    ]

    window = cf[
        "windows"
    ][str(
        probe[
            "horizon_candles"
        ]
    )]

    print()
    print(
        "=== CORRIDOR FACT",
        f"H={probe['horizon_candles']}",
        "===",
    )

    print(
        probe["probe_id"],
        "|",
        cf["state_label"],
        "|",
        f"move={raw_move:+.5f}%",
        "|",
        f"best_net={best_net:+.5f}%",
    )

    print(
        "COR1:",
        "pred=",
        cor_pred,
        f"score={probe['corridor_score']:.3f}",
        f"p={cor_p:.3f}",
        f"Brier={brier:.4f}",
        "| observed=",
        observed,
    )

    print(
        "SR1:",
        "pred=",
        sr1_pred,
        f"score={probe['sr1_score']:.3f}",
        "| winner=",
        (
            "BOTH"
            if cor_correct
            and sr1_correct
            else "COR1"
            if cor_correct
            else "SR1"
            if sr1_correct
            else "NONE"
        ),
    )

    print(
        "corridor:",
        f"W={window['width_pct']:.5f}%",
        f"CNR={window['cost_normalized_width']:.3f}",
        f"pos={window['position']:.2f}",
        f"edge={window['edge_pressure']:.2f}",
        f"exp={window['expansion_ratio']:.2f}",
    )

    print(
        "nested:",
        f"W5/W30={cf['ratios']['width_5_30']:.3f}",
        f"W15/W60={cf['ratios']['width_15_60']:.3f}",
        f"W30/W120={cf['ratios']['width_30_120']:.3f}",
    )

    print(
        "=======================",
    )


def evaluate_corridor_due(
    current_price,
    current_close_time_ms,
):
    global pending_corridor_probes

    remaining = []

    for probe in pending_corridor_probes:
        if (
            current_close_time_ms
            < probe[
                "due_close_time_ms"
            ]
        ):
            remaining.append(
                probe
            )
            continue

        resolve_corridor_probe(
            probe,
            current_price,
            current_close_time_ms,
        )

    pending_corridor_probes = (
        remaining
    )


def restore_corridor_overdue():
    global pending_corridor_probes

    if not pending_corridor_probes:
        return

    now_ms = int(
        datetime.now(
            timezone.utc
        ).timestamp()
        * 1000
    )

    overdue = [
        int(
            p[
                "due_close_time_ms"
            ]
        )
        for p
        in pending_corridor_probes
        if int(
            p[
                "due_close_time_ms"
            ]
        ) <= now_ms
    ]

    if not overdue:
        return

    print(
        "Recovering",
        len(set(overdue)),
        "corridor horizon(s)...",
    )

    exact = fetch_due_close_map(
        overdue
    )

    remaining = []

    for probe in pending_corridor_probes:
        result = exact.get(
            int(
                probe[
                    "due_close_time_ms"
                ]
            )
        )

        if result is None:
            remaining.append(
                probe
            )
            continue

        resolve_corridor_probe(
            probe,
            result["close"],
            result["close_time_ms"],
        )

    pending_corridor_probes = (
        remaining
    )


def print_corridor_summary():
    print()
    print(
        "=== COR1 CORRIDOR SUMMARY ==="
    )

    for horizon in CORRIDOR_HORIZONS:
        h = str(horizon)
        m = corridor_metrics[h]
        c = corridor_compare[h]

        precision = (
            "n/a"
            if m.get("precision")
            is None
            else
            f"{m['precision']*100:.1f}%"
        )

        recall = (
            "n/a"
            if m.get("recall")
            is None
            else
            f"{m['recall']*100:.1f}%"
        )

        print(
            f"H{horizon}:",
            f"n={m['samples']}",
            f"obs={m.get('observed_rate', 0)*100:.1f}%",
            f"pred={m.get('predicted_rate', 0)*100:.1f}%",
            f"precision={precision}",
            f"recall={recall}",
            f"Brier={m.get('brier', 0):.4f}",
            "| vs SR1:",
            f"COR-only={c['corridor_only_correct']}",
            f"SR1-only={c['sr1_only_correct']}",
            f"disagree={c['disagreements']}",
        )

    leaders = corridor_leaderboard(
        limit=3
    )

    if leaders:
        print(
            "highest observed tradeability buckets:"
        )

        for i, cell in enumerate(
            leaders,
            1,
        ):
            print(
                f"{i}.",
                cell["key"],
                f"rate={cell.get('market_tradeable_rate', 0)*100:.1f}%",
                f"n={cell.get('samples', 0)}",
                f"avg_net={cell.get('avg_best_net_pct', 0):+.5f}%",
            )
    else:
        print(
            "validated corridor buckets:",
            "NONE YET",
        )

    print(
        "RESEARCH ONLY — COR1 does not execute trades."
    )
    print(
        "==============================",
    )



def bool_flag(value):
    return bool(value)


def compute_corridor2_multilabel(
    corridor_features,
    sr1_features,
):
    global last_corridor2_flags

    if not corridor_features.get(
        "ready",
        False,
    ):
        return {
            "version":
                CORRIDOR2_VERSION,
            "ready": False,
            "active_flags": [],
            "transitions": [],
        }

    w = corridor_features[
        "windows"
    ]

    r = corridor_features[
        "ratios"
    ]

    def width(window):
        return float(
            w[str(window)].get(
                "width_pct",
                0.0,
            )
        )

    def cnr(window):
        return float(
            w[str(window)].get(
                "cost_normalized_width",
                0.0,
            )
        )

    def pos(window):
        return float(
            w[str(window)].get(
                "position",
                0.5,
            )
        )

    def pressure(window):
        return float(
            w[str(window)].get(
                "edge_pressure",
                0.0,
            )
        )

    def expansion(window):
        return float(
            w[str(window)].get(
                "expansion_ratio",
                1.0,
            )
        )

    def breakout_up(window):
        return float(
            w[str(window)].get(
                "breakout_up_pct",
                0.0,
            )
        ) > 0.0

    def breakout_down(window):
        return float(
            w[str(window)].get(
                "breakout_down_pct",
                0.0,
            )
        ) < 0.0

    nested_micro = (
        float(
            r.get(
                "width_5_30",
                1.0,
            )
        )
        <= CORRIDOR_NESTED_MICRO_RATIO
    )

    nested_meso = (
        float(
            r.get(
                "width_15_60",
                1.0,
            )
        )
        <= CORRIDOR_NESTED_MESO_RATIO
    )

    edge_up_30 = (
        pressure(30)
        >= CORRIDOR_EDGE_PRESSURE_THRESHOLD
        and pos(30)
        > CORRIDOR2_POSITION_MID
    )

    edge_down_30 = (
        pressure(30)
        >= CORRIDOR_EDGE_PRESSURE_THRESHOLD
        and pos(30)
        < CORRIDOR2_POSITION_MID
    )

    edge_up_60 = (
        pressure(60)
        >= CORRIDOR_EDGE_PRESSURE_THRESHOLD
        and pos(60)
        > CORRIDOR2_POSITION_MID
    )

    edge_down_60 = (
        pressure(60)
        >= CORRIDOR_EDGE_PRESSURE_THRESHOLD
        and pos(60)
        < CORRIDOR2_POSITION_MID
    )

    edge_up_120 = (
        pressure(120)
        >= CORRIDOR_EDGE_PRESSURE_THRESHOLD
        and pos(120)
        > CORRIDOR2_POSITION_MID
    )

    edge_down_120 = (
        pressure(120)
        >= CORRIDOR_EDGE_PRESSURE_THRESHOLD
        and pos(120)
        < CORRIDOR2_POSITION_MID
    )

    micro_expanding = (
        expansion(5)
        >= CORRIDOR_EXPANSION_RATIO
    )

    meso_expanding = (
        expansion(15)
        >= CORRIDOR_EXPANSION_RATIO
    )

    c30_expanding = (
        expansion(30)
        >= CORRIDOR_EXPANSION_RATIO
    )

    c60_expanding = (
        expansion(60)
        >= CORRIDOR_EXPANSION_RATIO
    )

    micro_contracting = (
        expansion(5)
        <= CORRIDOR_CONTRACTION_RATIO
    )

    meso_contracting = (
        expansion(15)
        <= CORRIDOR_CONTRACTION_RATIO
    )

    volume_z = float(
        sr1_features.get(
            "volume_z_20",
            0.0,
        )
    )

    sr1_comp = float(
        sr1_features.get(
            "compression_5_20",
            1.0,
        )
    )

    slope_alignment = int(
        sr1_features.get(
            "slope_alignment",
            0,
        )
    )

    flag_map = {
        # Pure geometry.
        "NESTED_MICRO":
            nested_micro,
        "NESTED_MESO":
            nested_meso,

        "EDGE_UP_30":
            edge_up_30,
        "EDGE_DOWN_30":
            edge_down_30,
        "EDGE_UP_60":
            edge_up_60,
        "EDGE_DOWN_60":
            edge_down_60,
        "EDGE_UP_120":
            edge_up_120,
        "EDGE_DOWN_120":
            edge_down_120,

        "MICRO_EXPANDING":
            micro_expanding,
        "MESO_EXPANDING":
            meso_expanding,
        "C30_EXPANDING":
            c30_expanding,
        "C60_EXPANDING":
            c60_expanding,

        "MICRO_CONTRACTING":
            micro_contracting,
        "MESO_CONTRACTING":
            meso_contracting,

        "BREAKOUT_UP_30":
            breakout_up(30),
        "BREAKOUT_DOWN_30":
            breakout_down(30),
        "BREAKOUT_UP_60":
            breakout_up(60),
        "BREAKOUT_DOWN_60":
            breakout_down(60),

        # Economic capacity of whole corridors.
        "C30_ABOVE_COST":
            cnr(30) >= 1.0,
        "C60_ABOVE_COST":
            cnr(60) >= 1.0,
        "C120_ABOVE_COST":
            cnr(120) >= 1.0,
        "MACRO_SUBCOST":
            cnr(120) < 1.0,

        # SR1 contextual flags are kept explicit, not mixed into COR1.
        "VOLUME_EXTREME_HIGH":
            volume_z
            >= CORRIDOR2_VOLUME_Z_EXTREME,
        "VOLUME_EXTREME_LOW":
            volume_z
            <= -CORRIDOR2_VOLUME_Z_EXTREME,
        "SR1_LOCAL_EXPANSION":
            sr1_comp
            >= CORRIDOR_EXPANSION_RATIO,
        "SR1_LOCAL_COMPRESSION":
            sr1_comp
            <= CORRIDOR_CONTRACTION_RATIO,
        "SLOPE_ALIGN_UP":
            slope_alignment > 0,
        "SLOPE_ALIGN_DOWN":
            slope_alignment < 0,
    }

    # Composite relations: multiple truths may coexist.
    flag_map.update(
        {
            "COMPRESSED_AT_UPPER_EDGE":
                nested_micro
                and edge_up_30,
            "COMPRESSED_AT_LOWER_EDGE":
                nested_micro
                and edge_down_30,
            "COMPRESSION_WITH_VOLUME_ANOMALY":
                nested_micro
                and abs(volume_z)
                >= CORRIDOR2_VOLUME_Z_EXTREME,
            "UPPER_EDGE_WITH_VOLUME":
                edge_up_30
                and volume_z
                >= CORRIDOR2_VOLUME_Z_EXTREME,
            "LOWER_EDGE_WITH_VOLUME":
                edge_down_30
                and volume_z
                >= CORRIDOR2_VOLUME_Z_EXTREME,
            "COMPRESSION_RELEASE":
                nested_micro
                and micro_expanding,
            "COMPRESSION_RELEASE_UP":
                nested_micro
                and micro_expanding
                and (
                    edge_up_30
                    or breakout_up(30)
                ),
            "COMPRESSION_RELEASE_DOWN":
                nested_micro
                and micro_expanding
                and (
                    edge_down_30
                    or breakout_down(30)
                ),
            "MULTISCALE_EXPANSION":
                micro_expanding
                and meso_expanding,
            "MACRO_CAPACITY_AVAILABLE":
                cnr(120) >= 1.0,
        }
    )

    active_flags = sorted(
        name
        for name, enabled
        in flag_map.items()
        if enabled
    )

    previous = dict(
        last_corridor2_flags
        or {}
    )

    transitions = []

    # Only core flags generate ENTER/EXIT events; composites are observed
    # as state labels and do not recursively explode transition space.
    transition_core = [
        "NESTED_MICRO",
        "NESTED_MESO",
        "EDGE_UP_30",
        "EDGE_DOWN_30",
        "MICRO_EXPANDING",
        "MESO_EXPANDING",
        "BREAKOUT_UP_30",
        "BREAKOUT_DOWN_30",
        "BREAKOUT_UP_60",
        "BREAKOUT_DOWN_60",
        "C30_ABOVE_COST",
        "C60_ABOVE_COST",
        "C120_ABOVE_COST",
        "VOLUME_EXTREME_HIGH",
        "VOLUME_EXTREME_LOW",
    ]

    if previous:
        for name in transition_core:
            old = bool(
                previous.get(
                    name,
                    False,
                )
            )

            new = bool(
                flag_map.get(
                    name,
                    False,
                )
            )

            if new and not old:
                transitions.append(
                    "ENTER_" + name
                )
            elif old and not new:
                transitions.append(
                    "EXIT_" + name
                )

        # Structural transitions with a direction.
        if (
            bool(
                previous.get(
                    "NESTED_MICRO",
                    False,
                )
            )
            and micro_expanding
            and not bool(
                previous.get(
                    "MICRO_EXPANDING",
                    False,
                )
            )
        ):
            transitions.append(
                "NESTED_TO_MICRO_EXPANSION"
            )

        if (
            bool(
                previous.get(
                    "MACRO_SUBCOST",
                    False,
                )
            )
            and cnr(120) >= 1.0
        ):
            transitions.append(
                "MACRO_CROSSED_COST_BARRIER"
            )

        if (
            bool(
                previous.get(
                    "EDGE_UP_30",
                    False,
                )
            )
            and breakout_up(30)
        ):
            transitions.append(
                "UPPER_EDGE_TO_BREAKOUT"
            )

        if (
            bool(
                previous.get(
                    "EDGE_DOWN_30",
                    False,
                )
            )
            and breakout_down(30)
        ):
            transitions.append(
                "LOWER_EDGE_TO_BREAKOUT"
            )

    transitions = sorted(
        set(transitions)
    )

    # Compact signature intentionally excludes high-cardinality numbers.
    signature_core = [
        x
        for x in active_flags
        if x in {
            "NESTED_MICRO",
            "NESTED_MESO",
            "EDGE_UP_30",
            "EDGE_DOWN_30",
            "MICRO_EXPANDING",
            "MESO_EXPANDING",
            "C30_ABOVE_COST",
            "C60_ABOVE_COST",
            "C120_ABOVE_COST",
            "VOLUME_EXTREME_HIGH",
            "VOLUME_EXTREME_LOW",
            "COMPRESSION_RELEASE_UP",
            "COMPRESSION_RELEASE_DOWN",
        }
    ]

    signature = (
        "+".join(
            signature_core
        )
        if signature_core
        else "BASE"
    )

    # Persist only booleans needed to derive the next transition.
    last_corridor2_flags = {
        name: bool(
            flag_map.get(
                name,
                False,
            )
        )
        for name in set(
            transition_core
            + ["MACRO_SUBCOST"]
        )
    }

    return {
        "version":
            CORRIDOR2_VERSION,
        "ready": True,
        "active_flags":
            active_flags,
        "flag_map":
            {
                k: bool(v)
                for k, v
                in flag_map.items()
            },
        "transitions":
            transitions,
        "signature":
            signature,
        "context": {
            "volume_z_20":
                round(
                    volume_z,
                    6,
                ),
            "sr1_compression_5_20":
                round(
                    sr1_comp,
                    6,
                ),
            "slope_alignment":
                slope_alignment,
        },
    }


def create_corridor2_probe(
    state,
    close_time_ms,
    horizon,
):
    cor2 = state[
        "corridor_multilabel"
    ]

    if not cor2.get(
        "ready",
        False,
    ):
        return

    horizon = int(horizon)

    probe = {
        "probe_id":
            f"COR2-{state['state_id']}-H{horizon}",
        "created_at": now_iso(),
        "state_id":
            state["state_id"],
        "corridor2_version":
            CORRIDOR2_VERSION,
        "corridor1_version":
            CORRIDOR_VERSION,
        "state_representation":
            STATE_REP_VERSION,
        "regime":
            state["regime"],
        "entry_price":
            state["price"],
        "horizon_candles":
            horizon,
        "entry_close_time_ms":
            close_time_ms,
        "due_close_time_ms":
            close_time_ms
            + horizon
            * MINUTE_MS,
        "active_flags":
            list(
                cor2[
                    "active_flags"
                ]
            ),
        "transitions":
            list(
                cor2[
                    "transitions"
                ]
            ),
        "signature":
            cor2[
                "signature"
            ],
        "context":
            cor2[
                "context"
            ],
        "corridor_features":
            state[
                "corridor_features"
            ],
        "status":
            "FROZEN",
    }

    probe["fingerprint"] = (
        fingerprint(probe)
    )

    pending_corridor2_probes.append(
        probe
    )


def create_multi_horizon_corridor2_probes(
    state,
    close_time_ms,
):
    if not CORRIDOR2_RESEARCH_ENABLED:
        return

    for horizon in CORRIDOR_HORIZONS:
        create_corridor2_probe(
            state,
            close_time_ms,
            horizon,
        )


def update_corridor2_cell(
    key,
    horizon,
    observed,
    best_net_pct,
):
    cell = (
        corridor2_feature_matrix.setdefault(
            key,
            {
                "horizon_candles":
                    int(horizon),
                "samples": 0,
                "market_tradeable": 0,
                "best_net_sum_pct":
                    0.0,
                "last_updated":
                    None,
            },
        )
    )

    cell["samples"] += 1

    if observed:
        cell[
            "market_tradeable"
        ] += 1

    cell[
        "best_net_sum_pct"
    ] += float(
        best_net_pct
    )

    cell["last_updated"] = (
        now_iso()
    )

    n = max(
        1,
        cell["samples"],
    )

    cell[
        "market_tradeable_rate"
    ] = round(
        cell[
            "market_tradeable"
        ] / n,
        6,
    )

    cell[
        "avg_best_net_pct"
    ] = round(
        cell[
            "best_net_sum_pct"
        ] / n,
        6,
    )


def resolve_corridor2_probe(
    probe,
    exit_price,
    observed_close_time_ms,
):
    entry = float(
        probe[
            "entry_price"
        ]
    )

    raw_move = pct_return(
        entry,
        exit_price,
    )

    buy_net = (
        simulate_buy_roundtrip_return_pct(
            entry,
            exit_price,
        )
    )

    sell_net = (
        simulate_sell_owned_roundtrip_return_pct(
            entry,
            exit_price,
        )
    )

    best_net = max(
        0.0,
        buy_net,
        sell_net,
    )

    observed = (
        best_net > 0.0
    )

    horizon = int(
        probe[
            "horizon_candles"
        ]
    )

    m = corridor2_metrics[
        str(horizon)
    ]

    m["samples"] += 1

    if observed:
        m[
            "observed_market_positive"
        ] += 1

    m[
        "best_net_sum_pct"
    ] += best_net

    m[
        "active_flag_sum"
    ] += len(
        probe[
            "active_flags"
        ]
    )

    m[
        "active_transition_sum"
    ] += len(
        probe[
            "transitions"
        ]
    )

    n = max(
        1,
        m["samples"],
    )

    m[
        "observed_rate"
    ] = round(
        m[
            "observed_market_positive"
        ] / n,
        6,
    )

    m[
        "avg_best_net_pct"
    ] = round(
        m[
            "best_net_sum_pct"
        ] / n,
        6,
    )

    m[
        "avg_active_flags"
    ] = round(
        m[
            "active_flag_sum"
        ] / n,
        6,
    )

    m[
        "avg_active_transitions"
    ] = round(
        m[
            "active_transition_sum"
        ] / n,
        6,
    )

    # Update one cell for every active relation.
    for flag in probe[
        "active_flags"
    ]:
        update_corridor2_cell(
            (
                f"FLAG:H{horizon}|"
                f"{flag}"
            ),
            horizon,
            observed,
            best_net,
        )

    for transition in probe[
        "transitions"
    ]:
        update_corridor2_cell(
            (
                f"TRANS:H{horizon}|"
                f"{transition}"
            ),
            horizon,
            observed,
            best_net,
        )

    update_corridor2_cell(
        (
            f"SIG:H{horizon}|"
            f"{probe['signature']}"
        ),
        horizon,
        observed,
        best_net,
    )

    fact = {
        "probe_id":
            probe[
                "probe_id"
            ],
        "observed_at":
            now_iso(),
        "observed_close_time_ms":
            observed_close_time_ms,
        "state_id":
            probe[
                "state_id"
            ],
        "corridor2_version":
            CORRIDOR2_VERSION,
        "horizon_candles":
            horizon,
        "entry_price":
            entry,
        "exit_price":
            exit_price,
        "raw_move_pct":
            round(
                raw_move,
                6,
            ),
        "buy_roundtrip_net_pct":
            round(
                buy_net,
                6,
            ),
        "sell_owned_roundtrip_net_pct":
            round(
                sell_net,
                6,
            ),
        "best_market_net_pct":
            round(
                best_net,
                6,
            ),
        "observed_market_tradeable":
            observed,
        "active_flags":
            probe[
                "active_flags"
            ],
        "transitions":
            probe[
                "transitions"
            ],
        "signature":
            probe[
                "signature"
            ],
        "context":
            probe[
                "context"
            ],
        "probe_hash":
            probe[
                "fingerprint"
            ],
        "research_only":
            True,
        "status":
            "OBSERVED",
    }

    append_jsonl(
        CORRIDOR2_FACTS_FILE,
        fact,
    )

    print()
    print(
        "=== COR2 MULTI-LABEL FACT",
        f"H={horizon}",
        "===",
    )

    print(
        probe[
            "probe_id"
        ],
        "| move=",
        f"{raw_move:+.5f}%",
        "| best_net=",
        f"{best_net:+.5f}%",
        "| tradeable=",
        observed,
    )

    print(
        "FLAGS:",
        (
            ",".join(
                probe[
                    "active_flags"
                ][:8]
            )
            if probe[
                "active_flags"
            ]
            else "NONE"
        ),
        (
            f"...(+{len(probe['active_flags'])-8})"
            if len(
                probe[
                    "active_flags"
                ]
            ) > 8
            else ""
        ),
    )

    print(
        "TRANS:",
        (
            ",".join(
                probe[
                    "transitions"
                ]
            )
            if probe[
                "transitions"
            ]
            else "NONE"
        ),
    )

    print(
        "SIG:",
        probe[
            "signature"
        ],
    )

    print(
        "================================",
    )


def evaluate_corridor2_due(
    current_price,
    current_close_time_ms,
):
    global pending_corridor2_probes

    remaining = []

    for probe in (
        pending_corridor2_probes
    ):
        if (
            current_close_time_ms
            < probe[
                "due_close_time_ms"
            ]
        ):
            remaining.append(
                probe
            )
            continue

        resolve_corridor2_probe(
            probe,
            current_price,
            current_close_time_ms,
        )

    pending_corridor2_probes = (
        remaining
    )


def restore_corridor2_overdue():
    global pending_corridor2_probes

    if not pending_corridor2_probes:
        return

    now_ms = int(
        datetime.now(
            timezone.utc
        ).timestamp()
        * 1000
    )

    overdue = [
        int(
            p[
                "due_close_time_ms"
            ]
        )
        for p
        in pending_corridor2_probes
        if int(
            p[
                "due_close_time_ms"
            ]
        ) <= now_ms
    ]

    if not overdue:
        return

    print(
        "Recovering",
        len(
            set(overdue)
        ),
        "COR2 horizon(s)...",
    )

    exact = fetch_due_close_map(
        overdue
    )

    remaining = []

    for probe in (
        pending_corridor2_probes
    ):
        result = exact.get(
            int(
                probe[
                    "due_close_time_ms"
                ]
            )
        )

        if result is None:
            remaining.append(
                probe
            )
            continue

        resolve_corridor2_probe(
            probe,
            result[
                "close"
            ],
            result[
                "close_time_ms"
            ],
        )

    pending_corridor2_probes = (
        remaining
    )


def print_corridor2_summary():
    print()
    print(
        "=== COR2 MULTI-LABEL SUMMARY ==="
    )

    for horizon in (
        CORRIDOR_HORIZONS
    ):
        m = corridor2_metrics[
            str(horizon)
        ]

        print(
            f"H{horizon}:",
            f"n={m['samples']}",
            f"observed={m.get('observed_rate', 0)*100:.1f}%",
            f"avg_net={m.get('avg_best_net_pct', 0):+.5f}%",
            f"flags={m.get('avg_active_flags', 0):.1f}",
            f"trans={m.get('avg_active_transitions', 0):.2f}",
        )

    flags = corridor2_leaderboard(
        "FLAG:",
        limit=5,
    )

    transitions = corridor2_leaderboard(
        "TRANS:",
        limit=5,
    )

    if flags:
        print(
            "validated flag leaders:"
        )

        for i, cell in enumerate(
            flags,
            1,
        ):
            print(
                f"{i}.",
                cell[
                    "key"
                ],
                f"rate={cell.get('market_tradeable_rate', 0)*100:.1f}%",
                f"n={cell.get('samples', 0)}",
                f"avg_net={cell.get('avg_best_net_pct', 0):+.5f}%",
            )
    else:
        print(
            "validated flag leaders:",
            "NONE YET",
        )

    if transitions:
        print(
            "validated transition leaders:"
        )

        for i, cell in enumerate(
            transitions,
            1,
        ):
            print(
                f"{i}.",
                cell[
                    "key"
                ],
                f"rate={cell.get('market_tradeable_rate', 0)*100:.1f}%",
                f"n={cell.get('samples', 0)}",
                f"avg_net={cell.get('avg_best_net_pct', 0):+.5f}%",
            )
    else:
        print(
            "validated transition leaders:",
            "NONE YET",
        )

    print(
        "RESEARCH ONLY — COR2 is not connected to execution."
    )

    print(
        "================================",
    )



def corridor3_age_band(minutes):
    if minutes is None:
        return "AGE_UNKNOWN"

    x = float(minutes)

    if x < 1.0:
        return "AGE_0"
    if x < 3.0:
        return "AGE_1_2"
    if x < 8.0:
        return "AGE_3_7"
    if x < 16.0:
        return "AGE_8_15"
    if x < 31.0:
        return "AGE_16_30"

    return "AGE_31_PLUS"


def corridor3_structural_band(value):
    x = abs(float(value))

    if x < 0.10:
        return "STRUCT_TINY"
    if x < 0.35:
        return "STRUCT_SMALL"
    if x < 0.75:
        return "STRUCT_MEDIUM"
    if x < 1.50:
        return "STRUCT_LARGE"

    return "STRUCT_EXTREME"


def corridor3_economic_band(value):
    x = abs(float(value))

    if x < 0.05:
        return "ECON_TINY"
    if x < 0.10:
        return "ECON_SMALL"
    if x < 0.25:
        return "ECON_MEDIUM"
    if x < 0.50:
        return "ECON_LARGE"

    return "ECON_EXTREME"


def corridor3_window_scale(
    window_features,
    required_move_pct,
):
    current_width = float(
        window_features.get(
            "width_pct",
            0.0,
        )
    )

    previous_width = float(
        window_features.get(
            "previous_width_pct",
            current_width,
        )
    )

    raw_ratio = float(
        window_features.get(
            "expansion_ratio",
            1.0,
        )
    )

    required = max(
        float(required_move_pct),
        1e-12,
    )

    epsilon = max(
        required
        * CORRIDOR3_EPSILON_COST_FRACTION,
        1e-9,
    )

    stable_log_ratio = math.log(
        (current_width + epsilon)
        / (previous_width + epsilon)
    )

    delta_width_pct = (
        current_width
        - previous_width
    )

    delta_cost_normalized = (
        delta_width_pct
        / required
    )

    current_cnr = (
        current_width
        / required
    )

    previous_cnr = (
        previous_width
        / required
    )

    breakout_distance_pct = max(
        abs(
            float(
                window_features.get(
                    "breakout_up_pct",
                    0.0,
                )
            )
        ),
        abs(
            float(
                window_features.get(
                    "breakout_down_pct",
                    0.0,
                )
            )
        ),
    )

    breakout_cost_normalized = (
        breakout_distance_pct
        / required
    )

    if delta_width_pct > 1e-12:
        direction = "EXPANDING"
    elif delta_width_pct < -1e-12:
        direction = "CONTRACTING"
    else:
        direction = "STABLE"

    return {
        "raw_expansion_ratio":
            round(
                raw_ratio,
                6,
            ),
        "stable_log_ratio":
            round(
                stable_log_ratio,
                6,
            ),
        "structural_magnitude":
            round(
                abs(
                    stable_log_ratio
                ),
                6,
            ),
        "delta_width_pct":
            round(
                delta_width_pct,
                6,
            ),
        "delta_cost_normalized":
            round(
                delta_cost_normalized,
                6,
            ),
        "economic_magnitude":
            round(
                abs(
                    delta_cost_normalized
                ),
                6,
            ),
        "current_cnr":
            round(
                current_cnr,
                6,
            ),
        "previous_cnr":
            round(
                previous_cnr,
                6,
            ),
        "breakout_distance_pct":
            round(
                breakout_distance_pct,
                6,
            ),
        "breakout_cost_normalized":
            round(
                breakout_cost_normalized,
                6,
            ),
        "direction":
            direction,
        "structural_band":
            corridor3_structural_band(
                stable_log_ratio
            ),
        "economic_band":
            corridor3_economic_band(
                delta_cost_normalized
            ),
    }


def corridor3_transition_window(
    transition,
):
    name = str(transition)

    if (
        "CONE_" in name
        or "_CONE_" in name
    ):
        if name.endswith("_5"):
            return 5
        if name.endswith("_15"):
            return 15
        if name.endswith("_30"):
            return 30
        if name.endswith("_60"):
            return 60
        if name.endswith("_120"):
            return 120

    if (
        "MICRO" in name
        or name
        == "NESTED_TO_MICRO_EXPANSION"
    ):
        return 5

    if "MESO" in name:
        return 15

    if (
        "_30" in name
        or "UPPER_EDGE_TO_BREAKOUT"
        in name
        or "LOWER_EDGE_TO_BREAKOUT"
        in name
    ):
        return 30

    if "_60" in name:
        return 60

    if (
        "_120" in name
        or "MACRO_CROSSED_COST_BARRIER"
        in name
    ):
        return 120

    return 30


def corridor3_update_tracker(
    cor2,
    close_time_ms,
):
    global corridor3_tracker

    now_ms = int(
        close_time_ms
    )

    active_flags = set(
        cor2.get(
            "active_flags",
            [],
        )
    )

    onsets = dict(
        corridor3_tracker.get(
            "flag_onsets_ms",
            {},
        )
        or {}
    )

    # Drop inactive state clocks.
    onsets = {
        name: int(ts)
        for name, ts
        in onsets.items()
        if name in active_flags
    }

    # Start clocks for newly active labels.
    for name in active_flags:
        if name not in onsets:
            onsets[name] = now_ms

    flag_ages = {
        name: round(
            max(
                0,
                now_ms
                - int(
                    onsets[name]
                ),
            )
            / CORRIDOR3_MINUTE_MS,
            3,
        )
        for name in sorted(
            active_flags
        )
    }

    signature = cor2.get(
        "signature",
        "BASE",
    )

    previous_signature = (
        corridor3_tracker.get(
            "signature"
        )
    )

    signature_onset = (
        corridor3_tracker.get(
            "signature_onset_ms"
        )
    )

    if (
        signature
        != previous_signature
        or signature_onset is None
    ):
        signature_onset = now_ms

    signature_age = round(
        max(
            0,
            now_ms
            - int(
                signature_onset
            ),
        )
        / CORRIDOR3_MINUTE_MS,
        3,
    )

    transitions = list(
        cor2.get(
            "transitions",
            [],
        )
    )

    last_transition_ms = (
        corridor3_tracker.get(
            "last_transition_ms"
        )
    )

    last_transition_names = list(
        corridor3_tracker.get(
            "last_transition_names",
            [],
        )
        or []
    )

    if transitions:
        last_transition_ms = now_ms
        last_transition_names = (
            transitions
        )

    if last_transition_ms is None:
        transition_age = None
    else:
        transition_age = round(
            max(
                0,
                now_ms
                - int(
                    last_transition_ms
                ),
            )
            / CORRIDOR3_MINUTE_MS,
            3,
        )

    corridor3_tracker = {
        "flag_onsets_ms":
            onsets,
        "signature":
            signature,
        "signature_onset_ms":
            signature_onset,
        "last_transition_ms":
            last_transition_ms,
        "last_transition_names":
            last_transition_names,
    }

    return {
        "flag_age_minutes":
            flag_ages,
        "signature_age_minutes":
            signature_age,
        "transition_age_minutes":
            transition_age,
        "last_transition_names":
            last_transition_names,
    }


def compute_corridor3_scale_age(
    corridor_features,
    cor2,
    close_time_ms,
):
    if not corridor_features.get(
        "ready",
        False,
    ):
        return {
            "version":
                CORRIDOR3_VERSION,
            "ready": False,
        }

    required = float(
        corridor_features.get(
            "required_move_pct",
            0.0,
        )
    )

    scale_by_window = {}

    for window in (
        CORRIDOR_WINDOWS
    ):
        scale_by_window[
            str(window)
        ] = corridor3_window_scale(
            corridor_features[
                "windows"
            ][str(window)],
            required,
        )

    age_state = (
        corridor3_update_tracker(
            cor2,
            close_time_ms,
        )
    )

    transition_scales = []

    for transition in cor2.get(
        "transitions",
        [],
    ):
        window = (
            corridor3_transition_window(
                transition
            )
        )

        scale = dict(
            scale_by_window[
                str(window)
            ]
        )

        transition_scales.append(
            {
                "transition":
                    transition,
                "window":
                    window,
                "structural_magnitude":
                    scale[
                        "structural_magnitude"
                    ],
                "economic_magnitude":
                    scale[
                        "economic_magnitude"
                    ],
                "stable_log_ratio":
                    scale[
                        "stable_log_ratio"
                    ],
                "delta_cost_normalized":
                    scale[
                        "delta_cost_normalized"
                    ],
                "raw_expansion_ratio":
                    scale[
                        "raw_expansion_ratio"
                    ],
                "current_cnr":
                    scale[
                        "current_cnr"
                    ],
                "structural_band":
                    scale[
                        "structural_band"
                    ],
                "economic_band":
                    scale[
                        "economic_band"
                    ],
            }
        )

    # Dominant structural change across nested scales.
    dominant_window = max(
        CORRIDOR_WINDOWS,
        key=lambda x: (
            scale_by_window[
                str(x)
            ][
                "structural_magnitude"
            ]
        ),
    )

    dominant = scale_by_window[
        str(dominant_window)
    ]

    return {
        "version":
            CORRIDOR3_VERSION,
        "ready": True,
        "required_move_pct":
            round(
                required,
                6,
            ),
        "scale_by_window":
            scale_by_window,
        "transition_scales":
            transition_scales,
        "dominant_window":
            int(
                dominant_window
            ),
        "dominant_structural_magnitude":
            dominant[
                "structural_magnitude"
            ],
        "dominant_economic_magnitude":
            dominant[
                "economic_magnitude"
            ],
        "ages":
            age_state,
    }


def create_corridor3_probe(
    state,
    close_time_ms,
    horizon,
):
    cor3 = state[
        "corridor_scale_age"
    ]

    if not cor3.get(
        "ready",
        False,
    ):
        return

    horizon = int(
        horizon
    )

    cor2 = state[
        "corridor_multilabel"
    ]

    probe = {
        "probe_id":
            f"COR3-{state['state_id']}-H{horizon}",
        "created_at":
            now_iso(),
        "state_id":
            state["state_id"],
        "corridor3_version":
            CORRIDOR3_VERSION,
        "corridor2_version":
            CORRIDOR2_VERSION,
        "corridor1_version":
            CORRIDOR_VERSION,
        "state_representation":
            STATE_REP_VERSION,
        "regime":
            state["regime"],
        "entry_price":
            state["price"],
        "horizon_candles":
            horizon,
        "entry_close_time_ms":
            int(
                close_time_ms
            ),
        "due_close_time_ms":
            int(
                close_time_ms
            )
            + horizon
            * MINUTE_MS,
        "signature":
            cor2.get(
                "signature",
                "BASE",
            ),
        "active_flags":
            list(
                cor2.get(
                    "active_flags",
                    [],
                )
            ),
        "transitions":
            list(
                cor2.get(
                    "transitions",
                    [],
                )
            ),
        "scale_by_window":
            cor3[
                "scale_by_window"
            ],
        "transition_scales":
            cor3[
                "transition_scales"
            ],
        "ages":
            cor3[
                "ages"
            ],
        "dominant_window":
            cor3[
                "dominant_window"
            ],
        "dominant_structural_magnitude":
            cor3[
                "dominant_structural_magnitude"
            ],
        "dominant_economic_magnitude":
            cor3[
                "dominant_economic_magnitude"
            ],
        "cone_model":
            state.get(
                "geometry_state",
                {},
            ).get(
                "cone_model",
                {},
            ),
        "status":
            "FROZEN",
    }

    probe["fingerprint"] = (
        fingerprint(
            probe
        )
    )

    pending_corridor3_probes.append(
        probe
    )


def create_multi_horizon_corridor3_probes(
    state,
    close_time_ms,
):
    if not CORRIDOR3_RESEARCH_ENABLED:
        return

    for horizon in (
        CORRIDOR_HORIZONS
    ):
        create_corridor3_probe(
            state,
            close_time_ms,
            horizon,
        )


def corridor3_update_cell(
    key,
    horizon,
    observed,
    best_net_pct,
    structural_magnitude,
    economic_magnitude,
):
    cell = (
        corridor3_feature_matrix.setdefault(
            key,
            {
                "horizon_candles":
                    int(horizon),
                "samples": 0,
                "market_tradeable": 0,
                "best_net_sum_pct":
                    0.0,
                "structural_sum":
                    0.0,
                "economic_sum":
                    0.0,
                "last_updated":
                    None,
            },
        )
    )

    cell["samples"] += 1

    if observed:
        cell[
            "market_tradeable"
        ] += 1

    cell[
        "best_net_sum_pct"
    ] += float(
        best_net_pct
    )

    cell[
        "structural_sum"
    ] += abs(
        float(
            structural_magnitude
        )
    )

    cell[
        "economic_sum"
    ] += abs(
        float(
            economic_magnitude
        )
    )

    cell["last_updated"] = (
        now_iso()
    )

    n = max(
        1,
        cell["samples"],
    )

    cell[
        "market_tradeable_rate"
    ] = round(
        cell[
            "market_tradeable"
        ] / n,
        6,
    )

    cell[
        "avg_best_net_pct"
    ] = round(
        cell[
            "best_net_sum_pct"
        ] / n,
        6,
    )

    cell[
        "avg_structural_magnitude"
    ] = round(
        cell[
            "structural_sum"
        ] / n,
        6,
    )

    cell[
        "avg_economic_magnitude"
    ] = round(
        cell[
            "economic_sum"
        ] / n,
        6,
    )


def resolve_corridor3_probe(
    probe,
    exit_price,
    observed_close_time_ms,
):
    entry = float(
        probe[
            "entry_price"
        ]
    )

    raw_move = pct_return(
        entry,
        exit_price,
    )

    buy_net = (
        simulate_buy_roundtrip_return_pct(
            entry,
            exit_price,
        )
    )

    sell_net = (
        simulate_sell_owned_roundtrip_return_pct(
            entry,
            exit_price,
        )
    )

    best_net = max(
        0.0,
        buy_net,
        sell_net,
    )

    observed = (
        best_net > 0.0
    )

    horizon = int(
        probe[
            "horizon_candles"
        ]
    )

    m = corridor3_metrics[
        str(horizon)
    ]

    m["samples"] += 1

    if observed:
        m[
            "observed_market_positive"
        ] += 1

    m[
        "best_net_sum_pct"
    ] += best_net

    m[
        "structural_magnitude_sum"
    ] += float(
        probe[
            "dominant_structural_magnitude"
        ]
    )

    m[
        "economic_magnitude_sum"
    ] += float(
        probe[
            "dominant_economic_magnitude"
        ]
    )

    signature_age = float(
        probe[
            "ages"
        ].get(
            "signature_age_minutes",
            0.0,
        )
        or 0.0
    )

    transition_age_raw = (
        probe[
            "ages"
        ].get(
            "transition_age_minutes"
        )
    )

    transition_age = float(
        transition_age_raw
        or 0.0
    )

    m[
        "signature_age_sum_minutes"
    ] += signature_age

    m[
        "transition_age_sum_minutes"
    ] += transition_age

    n = max(
        1,
        m["samples"],
    )

    m[
        "observed_rate"
    ] = round(
        m[
            "observed_market_positive"
        ] / n,
        6,
    )

    m[
        "avg_best_net_pct"
    ] = round(
        m[
            "best_net_sum_pct"
        ] / n,
        6,
    )

    m[
        "avg_structural_magnitude"
    ] = round(
        m[
            "structural_magnitude_sum"
        ] / n,
        6,
    )

    m[
        "avg_economic_magnitude"
    ] = round(
        m[
            "economic_magnitude_sum"
        ] / n,
        6,
    )

    m[
        "avg_signature_age_minutes"
    ] = round(
        m[
            "signature_age_sum_minutes"
        ] / n,
        6,
    )

    m[
        "avg_transition_age_minutes"
    ] = round(
        m[
            "transition_age_sum_minutes"
        ] / n,
        6,
    )

    # 1) Transition-specific scale cells.
    for item in probe[
        "transition_scales"
    ]:
        key = (
            f"TRANS_SCALE:H{horizon}|"
            f"{item['transition']}|"
            f"{item['structural_band']}|"
            f"{item['economic_band']}"
        )

        corridor3_update_cell(
            key,
            horizon,
            observed,
            best_net,
            item[
                "structural_magnitude"
            ],
            item[
                "economic_magnitude"
            ],
        )

    # 2) Flag dwell-age cells.
    flag_ages = probe[
        "ages"
    ].get(
        "flag_age_minutes",
        {},
    )

    for flag in probe[
        "active_flags"
    ]:
        age = flag_ages.get(
            flag,
            0.0,
        )

        key = (
            f"FLAG_AGE:H{horizon}|"
            f"{flag}|"
            f"{corridor3_age_band(age)}"
        )

        corridor3_update_cell(
            key,
            horizon,
            observed,
            best_net,
            probe[
                "dominant_structural_magnitude"
            ],
            probe[
                "dominant_economic_magnitude"
            ],
        )

    # 3) Signature dwell-age cell.
    sig_key = (
        f"SIG_AGE:H{horizon}|"
        f"{probe['signature']}|"
        f"{corridor3_age_band(signature_age)}"
    )

    corridor3_update_cell(
        sig_key,
        horizon,
        observed,
        best_net,
        probe[
            "dominant_structural_magnitude"
        ],
        probe[
            "dominant_economic_magnitude"
        ],
    )

    fact = {
        "probe_id":
            probe[
                "probe_id"
            ],
        "observed_at":
            now_iso(),
        "observed_close_time_ms":
            int(
                observed_close_time_ms
            ),
        "state_id":
            probe[
                "state_id"
            ],
        "corridor3_version":
            CORRIDOR3_VERSION,
        "horizon_candles":
            horizon,
        "entry_price":
            entry,
        "exit_price":
            exit_price,
        "raw_move_pct":
            round(
                raw_move,
                6,
            ),
        "buy_roundtrip_net_pct":
            round(
                buy_net,
                6,
            ),
        "sell_owned_roundtrip_net_pct":
            round(
                sell_net,
                6,
            ),
        "best_market_net_pct":
            round(
                best_net,
                6,
            ),
        "observed_market_tradeable":
            observed,
        "signature":
            probe[
                "signature"
            ],
        "active_flags":
            probe[
                "active_flags"
            ],
        "transitions":
            probe[
                "transitions"
            ],
        "transition_scales":
            probe[
                "transition_scales"
            ],
        "scale_by_window":
            probe[
                "scale_by_window"
            ],
        "ages":
            probe[
                "ages"
            ],
        "dominant_window":
            probe[
                "dominant_window"
            ],
        "dominant_structural_magnitude":
            probe[
                "dominant_structural_magnitude"
            ],
        "dominant_economic_magnitude":
            probe[
                "dominant_economic_magnitude"
            ],
        "cone_model":
            probe.get(
                "cone_model",
                {},
            ),
        "probe_hash":
            probe[
                "fingerprint"
            ],
        "research_only":
            True,
        "status":
            "OBSERVED",
    }

    append_jsonl(
        CORRIDOR3_FACTS_FILE,
        fact,
    )

    print()
    print(
        "=== COR3 SCALE/AGE FACT",
        f"H={horizon}",
        "===",
    )

    print(
        probe[
            "probe_id"
        ],
        "| move=",
        f"{raw_move:+.5f}%",
        "| best_net=",
        f"{best_net:+.5f}%",
        "| tradeable=",
        observed,
    )

    ages = probe[
        "ages"
    ]

    print(
        "AGE:",
        f"sig={ages.get('signature_age_minutes', 0):.1f}m",
        "| since_transition=",
        (
            "n/a"
            if ages.get(
                "transition_age_minutes"
            ) is None
            else
            f"{ages.get('transition_age_minutes'):.1f}m"
        ),
    )

    if probe[
        "transition_scales"
    ]:
        for item in probe[
            "transition_scales"
        ][:4]:
            print(
                "TRANS_SCALE:",
                item[
                    "transition"
                ],
                f"W{item['window']}",
                f"raw={item['raw_expansion_ratio']:.2f}x",
                f"log={item['stable_log_ratio']:+.3f}",
                f"dCost={item['delta_cost_normalized']:+.3f}x",
                item[
                    "structural_band"
                ],
                item[
                    "economic_band"
                ],
            )
    else:
        dom = probe[
            "scale_by_window"
        ][str(
            probe[
                "dominant_window"
            ]
        )]

        print(
            "DOMINANT_SCALE:",
            f"W{probe['dominant_window']}",
            f"raw={dom['raw_expansion_ratio']:.2f}x",
            f"log={dom['stable_log_ratio']:+.3f}",
            f"dCost={dom['delta_cost_normalized']:+.3f}x",
            dom[
                "structural_band"
            ],
            dom[
                "economic_band"
            ],
        )

    print(
        "SIG:",
        probe[
            "signature"
        ],
    )

    print(
        "================================",
    )


def evaluate_corridor3_due(
    current_price,
    current_close_time_ms,
):
    global pending_corridor3_probes

    remaining = []

    for probe in (
        pending_corridor3_probes
    ):
        if (
            current_close_time_ms
            < probe[
                "due_close_time_ms"
            ]
        ):
            remaining.append(
                probe
            )
            continue

        resolve_corridor3_probe(
            probe,
            current_price,
            current_close_time_ms,
        )

    pending_corridor3_probes = (
        remaining
    )


def restore_corridor3_overdue():
    global pending_corridor3_probes

    if not pending_corridor3_probes:
        return

    now_ms = int(
        datetime.now(
            timezone.utc
        ).timestamp()
        * 1000
    )

    overdue = [
        int(
            p[
                "due_close_time_ms"
            ]
        )
        for p
        in pending_corridor3_probes
        if int(
            p[
                "due_close_time_ms"
            ]
        ) <= now_ms
    ]

    if not overdue:
        return

    print(
        "Recovering",
        len(
            set(overdue)
        ),
        "COR3 horizon(s)...",
    )

    exact = fetch_due_close_map(
        overdue
    )

    remaining = []

    for probe in (
        pending_corridor3_probes
    ):
        result = exact.get(
            int(
                probe[
                    "due_close_time_ms"
                ]
            )
        )

        if result is None:
            remaining.append(
                probe
            )
            continue

        resolve_corridor3_probe(
            probe,
            result[
                "close"
            ],
            result[
                "close_time_ms"
            ],
        )

    pending_corridor3_probes = (
        remaining
    )


def print_corridor3_summary():
    print()
    print(
        "=== COR3 SCALE/AGE SUMMARY ==="
    )

    for horizon in (
        CORRIDOR_HORIZONS
    ):
        m = corridor3_metrics[
            str(horizon)
        ]

        print(
            f"H{horizon}:",
            f"n={m['samples']}",
            f"obs={m.get('observed_rate', 0)*100:.1f}%",
            f"avg_net={m.get('avg_best_net_pct', 0):+.5f}%",
            f"struct={m.get('avg_structural_magnitude', 0):.3f}",
            f"econ={m.get('avg_economic_magnitude', 0):.3f}x",
            f"sigAge={m.get('avg_signature_age_minutes', 0):.1f}m",
            f"transAge={m.get('avg_transition_age_minutes', 0):.1f}m",
        )

    transition_leaders = (
        corridor3_leaderboard(
            "TRANS_SCALE:",
            limit=5,
        )
    )

    if transition_leaders:
        print(
            "validated transition-scale leaders:"
        )

        for i, cell in enumerate(
            transition_leaders,
            1,
        ):
            print(
                f"{i}.",
                cell[
                    "key"
                ],
                f"rate={cell.get('market_tradeable_rate', 0)*100:.1f}%",
                f"n={cell.get('samples', 0)}",
                f"avg_net={cell.get('avg_best_net_pct', 0):+.5f}%",
            )
    else:
        print(
            "validated transition-scale leaders:",
            "NONE YET",
        )

    print(
        "RESEARCH ONLY — COR3 does not execute trades."
    )

    print(
        "==============================",
    )



def geo_median(values):
    vals = [
        float(v)
        for v in values
    ]

    if not vals:
        return 0.0

    vals.sort()
    n = len(vals)

    if n % 2:
        return vals[n // 2]

    return (
        vals[n // 2 - 1]
        + vals[n // 2]
    ) / 2.0


def geo_ema(values, alpha=0.30):
    vals = [
        float(v)
        for v in values
    ]

    if not vals:
        return 0.0

    out = vals[0]

    for value in vals[1:]:
        out = (
            alpha * value
            + (1.0 - alpha) * out
        )

    return out


def geo_detect_swings(view):
    points = []
    lookback = int(
        GEOMETRY_SWING_LOOKBACK
    )

    if len(view) < (
        lookback * 2 + 1
    ):
        return points

    for i in range(
        lookback,
        len(view) - lookback,
    ):
        candle = view[i]

        local = view[
            i - lookback:
            i + lookback + 1
        ]

        local_high = max(
            float(x["high"])
            for x in local
        )

        local_low = min(
            float(x["low"])
            for x in local
        )

        high = float(
            candle["high"]
        )

        low = float(
            candle["low"]
        )

        if high >= local_high:
            points.append(
                {
                    "kind": "HIGH",
                    "price": high,
                    "close_time_ms": int(
                        candle[
                            "close_time_ms"
                        ]
                    ),
                    "index": i,
                }
            )

        if low <= local_low:
            points.append(
                {
                    "kind": "LOW",
                    "price": low,
                    "close_time_ms": int(
                        candle[
                            "close_time_ms"
                        ]
                    ),
                    "index": i,
                }
            )

    return points


def geo_cluster_extrema(
    points,
    tolerance_abs,
):
    if not points:
        return []

    ordered = sorted(
        points,
        key=lambda x: float(
            x["price"]
        ),
    )

    groups = [
        [ordered[0]]
    ]

    for point in ordered[1:]:
        current = groups[-1]

        center = sum(
            float(x["price"])
            for x in current
        ) / len(current)

        if abs(
            float(point["price"])
            - center
        ) <= tolerance_abs:
            current.append(point)
        else:
            groups.append(
                [point]
            )

    out = []

    for group in groups:
        prices = [
            float(x["price"])
            for x in group
        ]

        highs = sum(
            1
            for x in group
            if x["kind"] == "HIGH"
        )

        lows = sum(
            1
            for x in group
            if x["kind"] == "LOW"
        )

        level = (
            sum(prices)
            / len(prices)
        )

        first_ms = min(
            int(
                x["close_time_ms"]
            )
            for x in group
        )

        last_ms = max(
            int(
                x["close_time_ms"]
            )
            for x in group
        )

        kind = (
            "RESISTANCE"
            if highs > lows
            else "SUPPORT"
            if lows > highs
            else "MIXED"
        )

        out.append(
            {
                "id":
                    f"{kind[:3]}-{round(level, 2)}",
                "kind":
                    kind,
                "level":
                    round(
                        level,
                        8,
                    ),
                "touches":
                    len(group),
                "high_touches":
                    highs,
                "low_touches":
                    lows,
                "first_close_time_ms":
                    first_ms,
                "last_close_time_ms":
                    last_ms,
            }
        )

    out.sort(
        key=lambda x: (
            -int(
                x["touches"]
            ),
            abs(
                float(
                    x["level"]
                )
            ),
        )
    )

    return out[
        :GEOMETRY_MAX_LEVELS
    ]



def geo_detect_swings_scaled(
    view,
    lookback,
    scale,
):
    points = []

    lookback = max(
        1,
        int(lookback),
    )

    if len(view) < (
        lookback * 2 + 1
    ):
        return points

    for i in range(
        lookback,
        len(view) - lookback,
    ):
        candle = view[i]

        local = view[
            i - lookback:
            i + lookback + 1
        ]

        high = float(
            candle["high"]
        )

        low = float(
            candle["low"]
        )

        local_high = max(
            float(x["high"])
            for x in local
        )

        local_low = min(
            float(x["low"])
            for x in local
        )

        if high >= local_high:
            points.append(
                {
                    "kind": "HIGH",
                    "price": high,
                    "close_time_ms": int(
                        candle[
                            "close_time_ms"
                        ]
                    ),
                    "index": i,
                    "scale": int(scale),
                }
            )

        if low <= local_low:
            points.append(
                {
                    "kind": "LOW",
                    "price": low,
                    "close_time_ms": int(
                        candle[
                            "close_time_ms"
                        ]
                    ),
                    "index": i,
                    "scale": int(scale),
                }
            )

    return points


def geo_cluster_scale_points(
    points,
    tolerance_abs,
    scale,
):
    if not points:
        return []

    ordered = sorted(
        points,
        key=lambda x: float(
            x["price"]
        ),
    )

    groups = [
        [ordered[0]]
    ]

    for point in ordered[1:]:
        current = groups[-1]

        # Robust center avoids one far touch dragging the whole cluster.
        center = geo_median(
            [
                float(x["price"])
                for x in current
            ]
        )

        if abs(
            float(point["price"])
            - center
        ) <= tolerance_abs:
            current.append(point)
        else:
            groups.append(
                [point]
            )

    clusters = []

    for group in groups:
        prices = [
            float(x["price"])
            for x in group
        ]

        highs = sum(
            1
            for x in group
            if x["kind"] == "HIGH"
        )

        lows = sum(
            1
            for x in group
            if x["kind"] == "LOW"
        )

        level = geo_median(
            prices
        )

        first_ms = min(
            int(x["close_time_ms"])
            for x in group
        )

        last_ms = max(
            int(x["close_time_ms"])
            for x in group
        )

        origin_kind = (
            "HIGH_CLUSTER"
            if highs > lows
            else "LOW_CLUSTER"
            if lows > highs
            else "MIXED_CLUSTER"
        )

        clusters.append(
            {
                "id":
                    f"S{int(scale)}-{origin_kind[:3]}-{round(level, 2)}",
                "scale":
                    int(scale),
                "origin_kind":
                    origin_kind,
                "level":
                    round(
                        level,
                        8,
                    ),
                "touches":
                    len(group),
                "high_touches":
                    highs,
                "low_touches":
                    lows,
                "first_close_time_ms":
                    first_ms,
                "last_close_time_ms":
                    last_ms,
                "cluster_span_abs":
                    round(
                        max(prices)
                        - min(prices),
                        8,
                    ),
            }
        )

    # Do not let a huge number of low-value singletons dominate.
    clusters.sort(
        key=lambda x: (
            -int(x["touches"]),
            -int(x["last_close_time_ms"]),
        )
    )

    return clusters[
        :GEOMETRY_LEVELS_PER_SCALE
    ]


def geo_dynamic_level_role(
    level,
    price,
    atr_abs,
    view,
    close_time_ms,
):
    item = dict(level)

    lv = float(
        item["level"]
    )

    price = float(price)
    atr_abs = max(
        float(atr_abs),
        price * 1e-7,
    )

    scale = int(
        item.get(
            "scale",
            30,
        )
    )

    pivot_buffer = max(
        atr_abs
        * GEOMETRY_PIVOT_ATR_MULT,
        price
        * GEOMETRY_SCALE_MIN_PCT.get(
            scale,
            0.005,
        )
        / 100.0,
    )

    break_buffer = max(
        atr_abs
        * GEOMETRY_BREAK_ATR_MULT,
        pivot_buffer
        * 0.75,
    )

    retest_buffer = max(
        atr_abs
        * GEOMETRY_RETEST_ATR_MULT,
        pivot_buffer,
    )

    if price > lv + pivot_buffer:
        current_role = "SUPPORT"
    elif price < lv - pivot_buffer:
        current_role = "RESISTANCE"
    else:
        current_role = "PIVOT"

    origin_kind = str(
        item.get(
            "origin_kind",
            "MIXED_CLUSTER",
        )
    )

    flip = (
        (
            origin_kind == "HIGH_CLUSTER"
            and current_role == "SUPPORT"
        )
        or (
            origin_kind == "LOW_CLUSTER"
            and current_role == "RESISTANCE"
        )
    )

    broken_up = (
        origin_kind
        in {
            "HIGH_CLUSTER",
            "MIXED_CLUSTER",
        }
        and price > lv + break_buffer
    )

    broken_down = (
        origin_kind
        in {
            "LOW_CLUSTER",
            "MIXED_CLUSTER",
        }
        and price < lv - break_buffer
    )

    # Locate candles after the last structural touch.
    last_touch_ms = int(
        item.get(
            "last_close_time_ms",
            close_time_ms,
        )
    )

    post = [
        c
        for c in view
        if int(
            c["close_time_ms"]
        ) > last_touch_ms
    ]

    retested = False
    retest_ms = None

    if flip and post:
        for candle in post:
            low = float(
                candle["low"]
            )

            high = float(
                candle["high"]
            )

            touched = (
                low
                <= lv + retest_buffer
                and high
                >= lv - retest_buffer
            )

            if touched:
                retested = True
                retest_ms = int(
                    candle[
                        "close_time_ms"
                    ]
                )

    age_minutes = max(
        0.0,
        (
            int(close_time_ms)
            - last_touch_ms
        )
        / 60_000.0,
    )

    distance_pct = (
        (
            price / lv - 1.0
        )
        * 100.0
        if lv
        else 0.0
    )

    item.update(
        {
            # Keep "kind" backward compatible for old dashboard/CONE code:
            # it now means CURRENT role, not historical origin.
            "kind":
                current_role,
            "current_role":
                current_role,
            "flip":
                bool(flip),
            "broken_up":
                bool(broken_up),
            "broken_down":
                bool(broken_down),
            "retested":
                bool(retested),
            "retest_close_time_ms":
                retest_ms,
            "age_minutes":
                round(
                    age_minutes,
                    2,
                ),
            "distance_pct":
                round(
                    distance_pct,
                    6,
                ),
            "pivot_buffer_abs":
                round(
                    pivot_buffer,
                    8,
                ),
        }
    )

    return item


def geo_multiscale_levels(
    view,
    price,
    atr_abs,
    close_time_ms,
):
    levels = []
    extrema_total = 0

    usable_scales = [
        scale
        for scale
        in GEOMETRY_LEVEL_SCALES
        if len(view) >= min(
            scale,
            len(view),
        )
    ]

    for scale in usable_scales:
        segment = view[
            -min(
                int(scale),
                len(view),
            ):
        ]

        lookback = (
            GEOMETRY_SCALE_LOOKBACK.get(
                int(scale),
                2,
            )
        )

        points = (
            geo_detect_swings_scaled(
                segment,
                lookback,
                scale,
            )
        )

        extrema_total += len(
            points
        )

        min_pct = (
            GEOMETRY_SCALE_MIN_PCT.get(
                int(scale),
                0.005,
            )
        )

        tolerance_abs = max(
            float(atr_abs)
            * GEOMETRY_SCALE_ATR_TOL.get(
                int(scale),
                0.20,
            ),
            float(price)
            * float(min_pct)
            / 100.0,
        )

        clustered = (
            geo_cluster_scale_points(
                points,
                tolerance_abs,
                scale,
            )
        )

        for item in clustered:
            item[
                "cluster_tolerance_abs"
            ] = round(
                tolerance_abs,
                8,
            )

            levels.append(
                geo_dynamic_level_role(
                    item,
                    price,
                    atr_abs,
                    view,
                    close_time_ms,
                )
            )

    # Cross-scale confluence: same price neighborhood on independent scales.
    confluence_tol = max(
        float(atr_abs)
        * GEOMETRY_CONFLUENCE_ATR_MULT,
        float(price) * 0.00003,
    )

    for item in levels:
        lv = float(
            item["level"]
        )

        peer_scales = sorted(
            set(
                int(peer["scale"])
                for peer in levels
                if abs(
                    float(peer["level"])
                    - lv
                ) <= confluence_tol
            )
        )

        item[
            "confluence_scales"
        ] = peer_scales

        item[
            "confluence_count"
        ] = len(
            peer_scales
        )

        # Strength deliberately separates repeated touches from scale.
        touch_score = min(
            1.0,
            float(
                item["touches"]
            )
            / 5.0,
        )

        scale_score = min(
            1.0,
            math.log(
                max(
                    2.0,
                    float(
                        item["scale"]
                    ),
                )
            )
            / math.log(
                240.0
            ),
        )

        recency_score = math.exp(
            -float(
                item[
                    "age_minutes"
                ]
            )
            / 180.0
        )

        confluence_score = min(
            1.0,
            float(
                item[
                    "confluence_count"
                ]
            )
            / 3.0,
        )

        role_bonus = (
            0.10
            if item[
                "retested"
            ]
            else 0.05
            if item[
                "flip"
            ]
            else 0.0
        )

        strength = (
            0.35
            * touch_score
            + 0.25
            * scale_score
            + 0.20
            * recency_score
            + 0.20
            * confluence_score
            + role_bonus
        )

        item[
            "strength"
        ] = round(
            min(
                1.0,
                strength,
            ),
            4,
        )

    # Deduplicate display candidates only across very close levels from the
    # SAME current role; scale information remains attached as confluence.
    levels.sort(
        key=lambda x: (
            -float(
                x["strength"]
            ),
            abs(
                float(
                    x["distance_pct"]
                )
            ),
        )
    )

    return {
        "levels":
            levels[
                :GEOMETRY_MAX_LEVELS
            ],
        "all_levels":
            levels,
        "extrema_total":
            extrema_total,
        "scales":
            usable_scales,
        "confluence_tolerance_abs":
            round(
                confluence_tol,
                8,
            ),
    }



def geo_build_structural_zones(
    levels,
    price,
    atr_abs,
    close_time_ms,
):
    """
    Merge nearby levels across independent scales into structural zones.

    A zone is not a new prediction. It is a compact representation of
    multiple observed extrema clusters. Current role is dynamic:
      price above zone -> SUPPORT
      price below zone -> RESISTANCE
      price inside/near zone -> PIVOT
    """
    if not levels:
        return []

    price = float(price)
    atr_abs = max(
        float(atr_abs),
        price * 1e-7,
    )

    merge_tol = max(
        atr_abs
        * GEOMETRY_ZONE_MERGE_ATR_MULT,
        price
        * GEOMETRY_ZONE_MERGE_MIN_PCT
        / 100.0,
    )

    ordered = sorted(
        levels,
        key=lambda x: float(
            x["level"]
        ),
    )

    groups = []

    for level in ordered:
        lv = float(
            level["level"]
        )

        if not groups:
            groups.append(
                [level]
            )
            continue

        group = groups[-1]

        weights = [
            max(
                0.10,
                float(
                    x.get(
                        "strength",
                        0.25,
                    )
                ),
            )
            for x in group
        ]

        center = (
            sum(
                float(x["level"]) * w
                for x, w
                in zip(
                    group,
                    weights,
                )
            )
            / max(
                sum(weights),
                1e-9,
            )
        )

        if abs(
            lv - center
        ) <= merge_tol:
            group.append(
                level
            )
        else:
            groups.append(
                [level]
            )

    zones = []

    for idx, group in enumerate(
        groups,
        start=1,
    ):
        weights = [
            max(
                0.10,
                float(
                    x.get(
                        "strength",
                        0.25,
                    )
                ),
            )
            for x in group
        ]

        center = (
            sum(
                float(x["level"]) * w
                for x, w
                in zip(
                    group,
                    weights,
                )
            )
            / max(
                sum(weights),
                1e-9,
            )
        )

        local_half_widths = [
            max(
                atr_abs
                * GEOMETRY_ZONE_HALF_ATR_MULT,
                float(
                    x.get(
                        "cluster_tolerance_abs",
                        0.0,
                    )
                )
                * 0.35,
                price * 1e-7,
            )
            for x in group
        ]

        lower = min(
            float(x["level"]) - hw
            for x, hw
            in zip(
                group,
                local_half_widths,
            )
        )

        upper = max(
            float(x["level"]) + hw
            for x, hw
            in zip(
                group,
                local_half_widths,
            )
        )

        scales = sorted(
            set(
                int(
                    x.get(
                        "scale",
                        0,
                    )
                )
                for x in group
            )
        )

        origin_kinds = sorted(
            set(
                str(
                    x.get(
                        "origin_kind",
                        "UNKNOWN",
                    )
                )
                for x in group
            )
        )

        total_touches = sum(
            int(
                x.get(
                    "touches",
                    0,
                )
            )
            for x in group
        )

        max_strength = max(
            float(
                x.get(
                    "strength",
                    0.0,
                )
            )
            for x in group
        )

        scale_score = min(
            1.0,
            len(scales) / 3.0,
        )

        touch_score = min(
            1.0,
            total_touches / 12.0,
        )

        retest_any = any(
            bool(
                x.get(
                    "retested"
                )
            )
            for x in group
        )

        flip_any = any(
            bool(
                x.get(
                    "flip"
                )
            )
            for x in group
        )

        strength = min(
            1.0,
            0.55 * max_strength
            + 0.25 * scale_score
            + 0.15 * touch_score
            + (
                0.05
                if retest_any
                else 0.0
            ),
        )

        role_buffer = max(
            atr_abs
            * GEOMETRY_ZONE_ROLE_ATR_MULT,
            (upper - lower) * 0.15,
        )

        if price > upper + role_buffer:
            role = "SUPPORT"
        elif price < lower - role_buffer:
            role = "RESISTANCE"
        else:
            role = "PIVOT"

        if price < lower:
            distance_abs = lower - price
            boundary = lower
        elif price > upper:
            distance_abs = price - upper
            boundary = upper
        else:
            distance_abs = 0.0
            boundary = price

        decay_scale = max(
            atr_abs
            * GEOMETRY_ZONE_PRESSURE_DECAY_ATR,
            upper - lower,
            price * 1e-7,
        )

        proximity = math.exp(
            -distance_abs
            / decay_scale
        )

        zone_pressure = min(
            1.0,
            proximity
            * (
                0.55
                + 0.45 * strength
            ),
        )

        if role == "SUPPORT":
            pressure_direction = "UP_FROM_SUPPORT"
        elif role == "RESISTANCE":
            pressure_direction = "DOWN_FROM_RESISTANCE"
        else:
            pressure_direction = "INSIDE_PIVOT"

        high_origins = sum(
            1
            for x in group
            if x.get(
                "origin_kind"
            ) == "HIGH_CLUSTER"
        )

        low_origins = sum(
            1
            for x in group
            if x.get(
                "origin_kind"
            ) == "LOW_CLUSTER"
        )

        historical_bias = (
            "HIGH"
            if high_origins > low_origins
            else "LOW"
            if low_origins > high_origins
            else "MIXED"
        )

        zone_flip = (
            (
                historical_bias == "HIGH"
                and role == "SUPPORT"
            )
            or (
                historical_bias == "LOW"
                and role == "RESISTANCE"
            )
            or flip_any
        )

        last_touch_ms = max(
            int(
                x.get(
                    "last_close_time_ms",
                    close_time_ms,
                )
            )
            for x in group
        )

        age_minutes = max(
            0.0,
            (
                int(close_time_ms)
                - last_touch_ms
            )
            / 60_000.0,
        )

        zones.append(
            {
                "id":
                    f"Z{idx}",
                "center":
                    round(
                        center,
                        8,
                    ),
                "lower":
                    round(
                        lower,
                        8,
                    ),
                "upper":
                    round(
                        upper,
                        8,
                    ),
                "width_abs":
                    round(
                        upper - lower,
                        8,
                    ),
                "role":
                    role,
                "historical_bias":
                    historical_bias,
                "origin_kinds":
                    origin_kinds,
                "scales":
                    scales,
                "scale_count":
                    len(scales),
                "members":
                    len(group),
                "touches":
                    total_touches,
                "strength":
                    round(
                        strength,
                        4,
                    ),
                "pressure":
                    round(
                        zone_pressure,
                        4,
                    ),
                "pressure_direction":
                    pressure_direction,
                "distance_abs":
                    round(
                        distance_abs,
                        8,
                    ),
                "distance_pct":
                    round(
                        (
                            distance_abs
                            / price
                            * 100.0
                        )
                        if price
                        else 0.0,
                        6,
                    ),
                "boundary":
                    round(
                        boundary,
                        8,
                    ),
                "flip":
                    bool(
                        zone_flip
                    ),
                "retested":
                    bool(
                        retest_any
                    ),
                "age_minutes":
                    round(
                        age_minutes,
                        2,
                    ),
            }
        )

    zones.sort(
        key=lambda z: (
            -float(
                z["pressure"]
            ),
            -float(
                z["strength"]
            ),
            float(
                z["distance_abs"]
            ),
        )
    )

    return zones[
        :GEOMETRY_MAX_ZONES
    ]


def geo_zone_context(
    zones,
):
    if not zones:
        return {
            "nearest_support_zone":
                None,
            "nearest_resistance_zone":
                None,
            "inside_pivot_zone":
                None,
        }

    supports = sorted(
        [
            z
            for z in zones
            if z["role"]
            == "SUPPORT"
        ],
        key=lambda z: float(
            z["distance_abs"]
        ),
    )

    resistances = sorted(
        [
            z
            for z in zones
            if z["role"]
            == "RESISTANCE"
        ],
        key=lambda z: float(
            z["distance_abs"]
        ),
    )

    pivots = sorted(
        [
            z
            for z in zones
            if z["role"]
            == "PIVOT"
        ],
        key=lambda z: (
            -float(
                z["pressure"]
            ),
            -float(
                z["strength"]
            ),
        ),
    )

    return {
        "nearest_support_zone":
            supports[0]
            if supports
            else None,
        "nearest_resistance_zone":
            resistances[0]
            if resistances
            else None,
        "inside_pivot_zone":
            pivots[0]
            if pivots
            else None,
    }


def geo_motion_label(
    upper_delta_abs,
    lower_delta_abs,
    motion_epsilon_abs,
):
    up = float(
        upper_delta_abs
    )

    low = float(
        lower_delta_abs
    )

    eps = max(
        float(
            motion_epsilon_abs
        ),
        1e-12,
    )

    up_move = abs(up) > eps
    low_move = abs(low) > eps

    if not up_move and not low_move:
        return "STABLE"

    if up > eps and low > eps:
        return "TRANSLATE_UP"

    if up < -eps and low < -eps:
        return "TRANSLATE_DOWN"

    if up < -eps and low > eps:
        return "CONTRACT_TO_CENTER"

    if up > eps and low < -eps:
        return "EXPAND_FROM_CENTER"

    if up > eps:
        return "UPPER_EXPANSION"

    if up < -eps:
        return "UPPER_CONTRACTION"

    if low > eps:
        return "LOWER_CONTRACTION"

    if low < -eps:
        return "LOWER_EXPANSION"

    return "ASYMMETRIC"



def cone_mean(values):
    vals = [float(v) for v in values]
    return (
        sum(vals) / len(vals)
        if vals
        else 0.0
    )


def cone_covariance_2d(points):
    if len(points) < 2:
        return (
            1e-6,
            0.0,
            1e-6,
            0.0,
            0.0,
        )

    xs = [float(p[0]) for p in points]
    ys = [float(p[1]) for p in points]

    mx = cone_mean(xs)
    my = cone_mean(ys)

    n = max(1, len(points) - 1)

    cxx = sum(
        (x - mx) ** 2
        for x in xs
    ) / n

    cyy = sum(
        (y - my) ** 2
        for y in ys
    ) / n

    cxy = sum(
        (x - mx) * (y - my)
        for x, y in zip(xs, ys)
    ) / n

    return (
        cxx,
        cxy,
        cyy,
        mx,
        my,
    )


def cone_eigen_2x2(
    cxx,
    cxy,
    cyy,
):
    cxx = float(cxx)
    cxy = float(cxy)
    cyy = float(cyy)

    trace = cxx + cyy

    disc = math.sqrt(
        max(
            0.0,
            (
                (cxx - cyy) ** 2
                + 4.0 * cxy * cxy
            ),
        )
    )

    l1 = max(
        CONE_COV_REG,
        0.5 * (
            trace + disc
        ),
    )

    l2 = max(
        CONE_COV_REG,
        0.5 * (
            trace - disc
        ),
    )

    if l2 > l1:
        l1, l2 = l2, l1

    phi_deg = math.degrees(
        0.5
        * math.atan2(
            2.0 * cxy,
            cxx - cyy,
        )
    )

    a = math.sqrt(l1)
    b = math.sqrt(l2)

    eccentricity = math.sqrt(
        max(
            0.0,
            1.0
            - (
                b * b
                / max(
                    a * a,
                    1e-12,
                )
            ),
        )
    )

    return {
        "lambda_major":
            l1,
        "lambda_minor":
            l2,
        "a":
            a,
        "b":
            b,
        "eccentricity":
            eccentricity,
        "phi_deg":
            phi_deg,
    }


def cone_angle_distance_deg(
    a,
    b,
    period=180.0,
):
    a = float(a)
    b = float(b)

    diff = abs(
        (
            a - b
            + period / 2.0
        )
        % period
        - period / 2.0
    )

    return diff


def cone_segment_model(
    segment,
    required_move_pct,
    reference_price,
):
    if len(segment) < 3:
        return {
            "ready": False,
        }

    required_move_pct = max(
        float(
            required_move_pct
        ),
        1e-9,
    )

    reference_price = max(
        float(
            reference_price
        ),
        1e-9,
    )

    closes = [
        float(x["close"])
        for x in segment
    ]

    center_price = geo_median(
        closes
    )

    points = []
    prev = closes[0]

    for close in closes:
        displacement_pct = (
            (
                close - center_price
            )
            / reference_price
            * 100.0
        )

        velocity_pct = (
            (
                close / prev - 1.0
            )
            * 100.0
            if prev
            else 0.0
        )

        points.append(
            (
                displacement_pct
                / required_move_pct,
                velocity_pct
                / required_move_pct,
            )
        )

        prev = close

    (
        cxx,
        cxy,
        cyy,
        mx,
        my,
    ) = cone_covariance_2d(
        points
    )

    # Tikhonov-like regularization for stable inverse covariance.
    reg = CONE_COV_REG
    cxx_r = cxx + reg
    cyy_r = cyy + reg

    eig = cone_eigen_2x2(
        cxx_r,
        cxy,
        cyy_r,
    )

    area = (
        math.pi
        * eig["a"]
        * eig["b"]
    )

    high = max(
        float(x["high"])
        for x in segment
    )

    low = min(
        float(x["low"])
        for x in segment
    )

    width_pct = (
        (high - low)
        / reference_price
        * 100.0
    )

    half_width_pct = max(
        width_pct / 2.0,
        required_move_pct
        * 0.05,
        1e-9,
    )

    trend_pct = (
        (
            closes[-1]
            / closes[0]
            - 1.0
        )
        * 100.0
        if closes[0]
        else 0.0
    )

    tilt_ratio = (
        trend_pct
        / half_width_pct
    )

    tilt_deg = math.degrees(
        math.atan(
            tilt_ratio
        )
    )

    # Current point pressure in normalized state space.
    qx, qy = points[-1]
    dx = qx - mx
    dy = qy - my

    det = (
        cxx_r * cyy_r
        - cxy * cxy
    )

    if det <= 1e-12:
        inv_xx = 1.0 / max(
            cxx_r,
            1e-9,
        )
        inv_xy = 0.0
        inv_yy = 1.0 / max(
            cyy_r,
            1e-9,
        )
    else:
        inv_xx = (
            cyy_r / det
        )
        inv_xy = (
            -cxy / det
        )
        inv_yy = (
            cxx_r / det
        )

    gx = (
        inv_xx * dx
        + inv_xy * dy
    )

    gy = (
        inv_xy * dx
        + inv_yy * dy
    )

    mahal_sq = max(
        0.0,
        dx * gx
        + dy * gy,
    )

    mahal = math.sqrt(
        mahal_sq
    )

    pressure = (
        1.0
        - math.exp(
            -0.5
            * mahal_sq
        )
    )

    pressure = max(
        0.0,
        min(
            1.0,
            pressure,
        ),
    )

    pressure_direction_deg = math.degrees(
        math.atan2(
            gy,
            gx,
        )
    )

    if abs(gx) >= abs(gy):
        pressure_axis = (
            "PRICE_UP"
            if gx >= 0
            else "PRICE_DOWN"
        )
    else:
        pressure_axis = (
            "VOL_UP"
            if gy >= 0
            else "VOL_DOWN"
        )

    return {
        "ready":
            True,
        "center_price":
            round(
                center_price,
                8,
            ),
        "cov_xx":
            round(
                cxx,
                8,
            ),
        "cov_xy":
            round(
                cxy,
                8,
            ),
        "cov_yy":
            round(
                cyy,
                8,
            ),
        "a":
            round(
                eig["a"],
                6,
            ),
        "b":
            round(
                eig["b"],
                6,
            ),
        "eccentricity":
            round(
                eig[
                    "eccentricity"
                ],
                6,
            ),
        "phi_deg":
            round(
                eig[
                    "phi_deg"
                ],
                3,
            ),
        "area":
            round(
                area,
                8,
            ),
        "tilt_deg":
            round(
                tilt_deg,
                3,
            ),
        "trend_pct":
            round(
                trend_pct,
                6,
            ),
        "width_pct":
            round(
                width_pct,
                6,
            ),
        "pressure":
            round(
                pressure,
                6,
            ),
        "mahalanobis":
            round(
                mahal,
                6,
            ),
        "pressure_direction_deg":
            round(
                pressure_direction_deg,
                3,
            ),
        "pressure_axis":
            pressure_axis,
    }


def cone_sequence_update(
    horizon,
    metrics,
    close_time_ms,
):
    global cone_tracker

    key = str(
        int(horizon)
    )

    now_ms = int(
        close_time_ms
    )

    sequence = dict(
        cone_tracker.get(
            "sequence",
            {},
        )
        or {}
    )

    state = dict(
        sequence.get(
            key,
            {},
        )
        or {}
    )

    max_age_ms = int(
        CONE_SEQUENCE_MAX_MINUTES
        * 60_000
    )

    def fresh(ts):
        return bool(
            ts
            and now_ms
            - int(ts)
            <= max_age_ms
        )

    area_state = metrics.get(
        "area_state",
        "STABLE",
    )

    tilt_high = (
        abs(
            float(
                metrics.get(
                    "tilt_deg",
                    0.0,
                )
            )
        )
        >= CONE_TILT_THRESHOLD_DEG
    )

    pressure_high = (
        float(
            metrics.get(
                "pressure",
                0.0,
            )
        )
        >= CONE_PRESSURE_HIGH
    )

    completed = False

    if area_state == "COLLAPSE":
        state[
            "collapse_ms"
        ] = now_ms

    if (
        tilt_high
        and fresh(
            state.get(
                "collapse_ms"
            )
        )
    ):
        collapse_ms = int(
            state[
                "collapse_ms"
            ]
        )

        if now_ms >= collapse_ms:
            state[
                "tilt_ms"
            ] = now_ms

    if (
        pressure_high
        and fresh(
            state.get(
                "tilt_ms"
            )
        )
    ):
        tilt_ms = int(
            state[
                "tilt_ms"
            ]
        )

        if now_ms >= tilt_ms:
            state[
                "pressure_ms"
            ] = now_ms

    if (
        area_state == "FLARE"
        and fresh(
            state.get(
                "pressure_ms"
            )
        )
    ):
        pressure_ms = int(
            state[
                "pressure_ms"
            ]
        )

        if now_ms >= pressure_ms:
            completed = True
            state[
                "last_complete_ms"
            ] = now_ms

            # Require a new collapse for another complete sequence.
            state[
                "collapse_ms"
            ] = None
            state[
                "tilt_ms"
            ] = None
            state[
                "pressure_ms"
            ] = None

    sequence[
        key
    ] = state

    cone_tracker[
        "sequence"
    ] = sequence

    return (
        completed,
        state,
    )



def cone_reference_areas(
    view,
    horizon,
    required_move_pct,
    reference_price,
):
    """
    Collect non-overlapping historical ellipse areas immediately before
    the current horizon. The median is a local scale reference.
    """
    h = int(
        horizon
    )

    refs = []

    for k in range(
        1,
        CONE_AREA_REF_SEGMENTS + 1,
    ):
        end = len(view) - k * h
        start = end - h

        if start < 0:
            break

        segment = view[
            start:end
        ]

        model = (
            cone_segment_model(
                segment,
                required_move_pct,
                reference_price,
            )
        )

        if model.get(
            "ready",
            False,
        ):
            refs.append(
                float(
                    model[
                        "area"
                    ]
                )
            )

    return refs


def cone_normalized_area_change(
    area,
    reference_areas,
):
    area = max(
        0.0,
        float(area),
    )

    positive = [
        max(
            0.0,
            float(x),
        )
        for x in reference_areas
    ]

    if positive:
        reference = geo_median(
            positive
        )
    else:
        reference = area

    scale_pool = (
        positive
        + [area]
    )

    local_scale = (
        geo_median(
            scale_pool
        )
        if scale_pool
        else 0.0
    )

    floor = max(
        CONE_AREA_FLOOR,
        0.10
        * local_scale,
    )

    raw_log = math.log(
        (
            area + floor
        )
        / (
            reference + floor
        )
    )

    # Bounded [-1,1], robust against near-zero areas.
    normalized = math.tanh(
        raw_log
    )

    return {
        "reference_area":
            round(
                reference,
                8,
            ),
        "area_floor":
            round(
                floor,
                8,
            ),
        "raw_log":
            round(
                raw_log,
                6,
            ),
        "normalized":
            round(
                normalized,
                6,
            ),
        "reference_n":
            len(
                positive
            ),
    }


def compute_cone_model(
    geometry_horizons,
    corridor_features,
    price,
    close_time_ms,
):
    global cone_tracker

    view = list(candles)

    if (
        not CONE_MODEL_RESEARCH_ENABLED
        or len(view)
        < max(
            CONE_HORIZONS
        )
    ):
        return {
            "version":
                CONE_MODEL_VERSION,
            "ready": False,
        }

    price = float(
        price
    )

    required_move_pct = max(
        float(
            corridor_features.get(
                "required_move_pct",
                0.0,
            )
        ),
        1e-9,
    )

    horizon_models = {}

    flags = {}

    explicit_transitions = []

    for horizon in CONE_HORIZONS:
        h = int(
            horizon
        )

        current = view[
            -h:
        ]

        if len(view) >= 2 * h:
            previous = view[
                -2 * h:
                -h
            ]
        elif len(view) >= h + 1:
            previous = view[
                -h - 1:
                -1
            ]
        else:
            previous = current

        current_model = (
            cone_segment_model(
                current,
                required_move_pct,
                price,
            )
        )

        previous_model = (
            cone_segment_model(
                previous,
                required_move_pct,
                price,
            )
        )

        if (
            not current_model.get(
                "ready",
                False,
            )
            or not previous_model.get(
                "ready",
                False,
            )
        ):
            continue

        area = float(
            current_model[
                "area"
            ]
        )

        prev_area = float(
            previous_model[
                "area"
            ]
        )

        reference_areas = (
            cone_reference_areas(
                view,
                h,
                required_move_pct,
                price,
            )
        )

        area_dynamics = (
            cone_normalized_area_change(
                area,
                reference_areas,
            )
        )

        area_velocity_norm = float(
            area_dynamics[
                "normalized"
            ]
        )

        if (
            area_velocity_norm
            >= CONE_AREA_NORM_THRESHOLD
        ):
            area_state = (
                "FLARE"
            )
        elif (
            area_velocity_norm
            <= -CONE_AREA_NORM_THRESHOLD
        ):
            area_state = (
                "COLLAPSE"
            )
        else:
            area_state = (
                "STABLE"
            )

        # Backward-compatible field now stores the bounded normalized value.
        current_model[
            "area_log_change"
        ] = round(
            area_velocity_norm,
            6,
        )

        current_model[
            "area_velocity_norm"
        ] = round(
            area_velocity_norm,
            6,
        )

        current_model[
            "area_log_change_raw"
        ] = area_dynamics[
            "raw_log"
        ]

        current_model[
            "area_reference"
        ] = area_dynamics[
            "reference_area"
        ]

        current_model[
            "area_reference_n"
        ] = area_dynamics[
            "reference_n"
        ]

        current_model[
            "area_floor"
        ] = area_dynamics[
            "area_floor"
        ]

        current_model[
            "area_state"
        ] = area_state

        current_model[
            "previous_area"
        ] = round(
            prev_area,
            8,
        )

        current_model[
            "previous_phi_deg"
        ] = previous_model[
            "phi_deg"
        ]

        current_model[
            "previous_tilt_deg"
        ] = previous_model[
            "tilt_deg"
        ]

        completed, seq = (
            cone_sequence_update(
                h,
                current_model,
                close_time_ms,
            )
        )

        current_model[
            "sequence_state"
        ] = seq

        current_model[
            "sequence_complete"
        ] = completed

        horizon_models[
            str(h)
        ] = current_model

        tilt = float(
            current_model[
                "tilt_deg"
            ]
        )

        ecc = float(
            current_model[
                "eccentricity"
            ]
        )

        pressure = float(
            current_model[
                "pressure"
            ]
        )

        if (
            tilt
            >= CONE_TILT_THRESHOLD_DEG
        ):
            flags[
                f"CONE_TILT_UP_{h}"
            ] = True

        if (
            tilt
            <= -CONE_TILT_THRESHOLD_DEG
        ):
            flags[
                f"CONE_TILT_DOWN_{h}"
            ] = True

        if (
            ecc
            >= CONE_ECC_HIGH
        ):
            flags[
                f"CONE_ECC_HIGH_{h}"
            ] = True

        if (
            pressure
            >= CONE_PRESSURE_HIGH
        ):
            flags[
                f"CONE_PRESSURE_HIGH_{h}"
            ] = True

            axis = current_model.get(
                "pressure_axis",
                "",
            )

            if axis == "PRICE_UP":
                flags[
                    f"CONE_PRESSURE_UP_{h}"
                ] = True
            elif axis == "PRICE_DOWN":
                flags[
                    f"CONE_PRESSURE_DOWN_{h}"
                ] = True

        if area_state == "FLARE":
            flags[
                f"CONE_FLARE_{h}"
            ] = True
        elif area_state == "COLLAPSE":
            flags[
                f"CONE_COLLAPSE_{h}"
            ] = True

        if completed:
            explicit_transitions.append(
                f"CONE_SEQUENCE_COMPLETE_{h}"
            )

    if len(
        horizon_models
    ) < 3:
        return {
            "version":
                CONE_MODEL_VERSION,
            "ready": False,
        }

    # Cross-scale orientation / tilt twist.
    def twist(
        h1,
        h2,
    ):
        a = horizon_models[
            str(h1)
        ]
        b = horizon_models[
            str(h2)
        ]

        phi_twist = (
            cone_angle_distance_deg(
                a[
                    "phi_deg"
                ],
                b[
                    "phi_deg"
                ],
            )
        )

        tilt_twist = abs(
            float(
                a[
                    "tilt_deg"
                ]
            )
            - float(
                b[
                    "tilt_deg"
                ]
            )
        )

        return {
            "phi_deg":
                round(
                    phi_twist,
                    3,
                ),
            "tilt_deg":
                round(
                    tilt_twist,
                    3,
                ),
            "combined_deg":
                round(
                    max(
                        phi_twist,
                        tilt_twist,
                    ),
                    3,
                ),
        }

    twist_5_30 = twist(
        5,
        30,
    )

    twist_5_60 = twist(
        5,
        60,
    )

    twist_30_120 = twist(
        30,
        120,
    )

    if (
        twist_5_30[
            "combined_deg"
        ]
        <= CONE_ALIGN_DEG
    ):
        flags[
            "CONE_ALIGNED_5_30"
        ] = True

    if (
        twist_5_60[
            "combined_deg"
        ]
        >= CONE_TWIST_HIGH_DEG
    ):
        flags[
            "CONE_TWIST_5_60"
        ] = True

    if (
        twist_30_120[
            "combined_deg"
        ]
        >= CONE_TWIST_HIGH_DEG
    ):
        flags[
            "CONE_TWIST_30_120"
        ] = True

    # Spine curvature in (log horizon, center displacement / cost) space.
    required_abs = max(
        price
        * required_move_pct
        / 100.0,
        1e-9,
    )

    def spine_y(h):
        mid = float(
            geometry_horizons[
                str(h)
            ][
                "mid"
            ]
        )

        return (
            mid - price
        ) / required_abs

    x5 = math.log(5.0)
    x30 = math.log(30.0)
    x120 = math.log(120.0)

    y5 = spine_y(5)
    y30 = spine_y(30)
    y120 = spine_y(120)

    slope_a = (
        (y30 - y5)
        / max(
            x30 - x5,
            1e-9,
        )
    )

    slope_b = (
        (y120 - y30)
        / max(
            x120 - x30,
            1e-9,
        )
    )

    bend_deg = math.degrees(
        math.atan(
            slope_b
        )
        - math.atan(
            slope_a
        )
    )

    if bend_deg >= CONE_CURVATURE_DEG:
        flags[
            "CONE_SPINE_CURVE_UP"
        ] = True
    elif bend_deg <= -CONE_CURVATURE_DEG:
        flags[
            "CONE_SPINE_CURVE_DOWN"
        ] = True

    active_flags = sorted(
        key
        for key, enabled
        in flags.items()
        if enabled
    )

    previous_flags = dict(
        cone_tracker.get(
            "flags",
            {},
        )
        or {}
    )

    transitions = []

    if previous_flags:
        all_keys = sorted(
            set(
                previous_flags.keys()
            )
            | set(
                flags.keys()
            )
        )

        for key in all_keys:
            old = bool(
                previous_flags.get(
                    key,
                    False,
                )
            )

            new = bool(
                flags.get(
                    key,
                    False,
                )
            )

            if new and not old:
                transitions.append(
                    "ENTER_"
                    + key
                )
            elif old and not new:
                transitions.append(
                    "EXIT_"
                    + key
                )

    transitions.extend(
        explicit_transitions
    )

    signature_candidates = [
        x
        for x in active_flags
        if (
            x.endswith(
                "_5"
            )
            or x.endswith(
                "_30"
            )
            or x.endswith(
                "_120"
            )
            or x in {
                "CONE_ALIGNED_5_30",
                "CONE_TWIST_5_60",
                "CONE_TWIST_30_120",
                "CONE_SPINE_CURVE_UP",
                "CONE_SPINE_CURVE_DOWN",
            }
        )
    ]

    signature = (
        "+".join(
            signature_candidates[
                :8
            ]
        )
        if signature_candidates
        else "CONE_BASE"
    )

    cone_tracker[
        "flags"
    ] = {
        key: bool(
            value
        )
        for key, value
        in flags.items()
    }

    cone_tracker[
        "last_signature"
    ] = signature

    cone_tracker[
        "last_close_time_ms"
    ] = int(
        close_time_ms
    )

    return {
        "version":
            CONE_MODEL_VERSION,
        "ready": True,
        "required_move_pct":
            round(
                required_move_pct,
                6,
            ),
        "horizons":
            horizon_models,
        "twist":
            {
                "5_30":
                    twist_5_30,
                "5_60":
                    twist_5_60,
                "30_120":
                    twist_30_120,
            },
        "spine_curvature":
            {
                "slope_5_30":
                    round(
                        slope_a,
                        6,
                    ),
                "slope_30_120":
                    round(
                        slope_b,
                        6,
                    ),
                "bend_deg":
                    round(
                        bend_deg,
                        3,
                    ),
            },
        "active_flags":
            active_flags,
        "transitions":
            sorted(
                set(
                    transitions
                )
            ),
        "signature":
            signature,
        "research_only":
            True,
    }



def ctd_tilt_state(tilt_deg):
    tilt = float(tilt_deg)
    if tilt >= CONE_DYNAMICS_STATE_DEADZONE_DEG:
        return "UP"
    if tilt <= -CONE_DYNAMICS_STATE_DEADZONE_DEG:
        return "DOWN"
    return "FLAT"


def ctd_rotation_label(omega):
    value = float(omega)
    speed = abs(value)

    if speed >= CONE_DYNAMICS_ROT_FAST_DEG_PER_MIN:
        band = "FAST"
    elif speed >= CONE_DYNAMICS_ROT_MED_DEG_PER_MIN:
        band = "MED"
    else:
        band = "SLOW"

    if value > 0:
        direction = "UP"
    elif value < 0:
        direction = "DOWN"
    else:
        direction = "FLAT"

    return direction + "_" + band



def teg_scale_coordinate(horizon):
    # Log2 scale coordinate makes 5→10 and 30→60 comparable.
    return math.log(
        max(float(horizon), 1e-9) / 5.0,
        2.0,
    )


def teg_node_token(event):
    return (
        f"H{int(event.get('horizon', 0))}_"
        f"{str(event.get('from_state', 'FLAT'))}"
        f"2{str(event.get('to_state', 'FLAT'))}"
    )


def teg_speed_bucket(edge):
    if bool(edge.get("synchronous", False)):
        return "SYNC"

    speed = abs(
        float(
            edge.get(
                "scale_velocity_log2_per_min",
                0.0,
            )
        )
    )

    if speed >= TRANSITION_EDGE_FAST_LOG2_PER_MIN:
        return "FAST"
    if speed >= TRANSITION_EDGE_SLOW_LOG2_PER_MIN:
        return "MED"
    return "SLOW"


def teg_build_edges(ordered_events):
    edges = []

    for left, right in zip(
        ordered_events,
        ordered_events[1:],
    ):
        t0 = int(left.get("time_ms", 0))
        t1 = int(right.get("time_ms", 0))
        dt = max(
            0.0,
            (t1 - t0) / MINUTE_MS,
        )

        h0 = int(left.get("horizon", 0))
        h1 = int(right.get("horizon", 0))

        ds = (
            teg_scale_coordinate(h1)
            - teg_scale_coordinate(h0)
        )

        synchronous = dt <= 1e-9

        if synchronous:
            velocity = 0.0
        else:
            velocity = ds / dt

        if ds > 0:
            scale_direction = "MICRO_TO_MACRO"
        elif ds < 0:
            scale_direction = "MACRO_TO_MICRO"
        else:
            scale_direction = "SAME_SCALE"

        edge = {
            "from_node": teg_node_token(left),
            "to_node": teg_node_token(right),
            "from_horizon": h0,
            "to_horizon": h1,
            "dt_minutes": round(dt, 4),
            "scale_delta_log2": round(ds, 6),
            "scale_velocity_log2_per_min": round(velocity, 6),
            "scale_direction": scale_direction,
            "synchronous": synchronous,
            "from_omega_deg_per_min": round(
                float(left.get("omega_deg_per_min", 0.0)),
                4,
            ),
            "to_omega_deg_per_min": round(
                float(right.get("omega_deg_per_min", 0.0)),
                4,
            ),
            "from_alpha_deg_per_min2": round(
                float(left.get("alpha_deg_per_min2", 0.0)),
                4,
            ),
            "to_alpha_deg_per_min2": round(
                float(right.get("alpha_deg_per_min2", 0.0)),
                4,
            ),
        }
        edge["speed_bucket"] = teg_speed_bucket(edge)
        edges.append(edge)

    return edges


def teg_edge_path_text(steps):
    if not steps:
        return "NONE"

    return ">".join(
        teg_node_token(step)
        for step in steps
    )


def teg_graph_summary(steps, edges):
    velocities = [
        abs(float(e.get("scale_velocity_log2_per_min", 0.0)))
        for e in edges
        if not e.get("synchronous", False)
    ]

    mean_speed = (
        sum(velocities) / len(velocities)
        if velocities
        else 0.0
    )

    if edges:
        net_ds = sum(
            float(e.get("scale_delta_log2", 0.0))
            for e in edges
        )
        total_dt = sum(
            float(e.get("dt_minutes", 0.0))
            for e in edges
        )
        net_speed = (
            net_ds / total_dt
            if total_dt > 1e-9
            else 0.0
        )
    else:
        net_speed = 0.0

    return {
        "node_path": teg_edge_path_text(steps),
        "edge_count": len(edges),
        "mean_abs_scale_velocity_log2_per_min": round(mean_speed, 6),
        "net_scale_velocity_log2_per_min": round(net_speed, 6),
    }



def teg_simultaneous_groups(events, close_time_ms):
    cutoff = int(
        close_time_ms
        - CONE_DYNAMICS_PROPAGATION_WINDOW_MIN * MINUTE_MS
    )

    recent = sorted(
        [
            e for e in events
            if int(e.get("time_ms", 0)) >= cutoff
        ],
        key=lambda e: (
            int(e.get("time_ms", 0)),
            int(e.get("horizon", 0)),
        ),
    )

    by_time = {}
    for event in recent:
        by_time.setdefault(
            int(event.get("time_ms", 0)),
            [],
        ).append(event)

    groups = []

    for time_ms in sorted(by_time):
        group_events = by_time[time_ms]
        nodes = sorted(
            teg_node_token(e)
            for e in group_events
        )
        coords = [
            teg_scale_coordinate(
                int(e.get("horizon", 0))
            )
            for e in group_events
            if int(e.get("horizon", 0)) > 0
        ]

        groups.append({
            "time_ms": int(time_ms),
            "nodes": nodes,
            "horizons": sorted(
                int(e.get("horizon", 0))
                for e in group_events
            ),
            "size": len(nodes),
            "signature": "{" + ",".join(nodes) + "}",
            "center_log2_scale": round(
                sum(coords) / len(coords)
                if coords else 0.0,
                6,
            ),
            "simultaneous": len(nodes) > 1,
        })

    return groups


def teg_hyperedges(groups):
    out = []

    for left, right in zip(
        groups,
        groups[1:],
    ):
        dt = max(
            0.0,
            (
                int(right.get("time_ms", 0))
                - int(left.get("time_ms", 0))
            ) / MINUTE_MS,
        )

        ds = (
            float(right.get("center_log2_scale", 0.0))
            - float(left.get("center_log2_scale", 0.0))
        )

        out.append({
            "from_group": str(left.get("signature", "{}")),
            "to_group": str(right.get("signature", "{}")),
            "dt_minutes": round(dt, 4),
            "scale_delta_log2": round(ds, 6),
            "scale_velocity_log2_per_min": round(
                ds / dt
                if dt > 1e-9
                else 0.0,
                6,
            ),
        })

    return out


def ctd_propagation_from_events(events, close_time_ms):
    cutoff = int(
        close_time_ms
        - CONE_DYNAMICS_PROPAGATION_WINDOW_MIN * MINUTE_MS
    )

    recent_all = [
        e for e in events
        if int(e.get("time_ms", 0)) >= cutoff
    ]

    hypergroups = teg_simultaneous_groups(
        events,
        close_time_ms,
    )
    hyperedges = teg_hyperedges(
        hypergroups
    )

    recent = [
        e for e in recent_all
        if e.get("to_state") in ("UP", "DOWN")
    ]

    if not recent:
        return {
            "mode": "NONE",
            "direction": "NONE",
            "order": [],
            "steps": [],
            "edges": [],
            "graph": {
                "node_path": "NONE",
                "edge_count": 0,
                "mean_abs_scale_velocity_log2_per_min": 0.0,
                "net_scale_velocity_log2_per_min": 0.0,
            },
            "edge_steps": [
                {
                    "horizon": int(e.get("horizon", 0)),
                    "from_state": str(e.get("from_state", "FLAT")),
                    "to_state": str(e.get("to_state", "FLAT")),
                    "time_ms": int(e.get("time_ms", 0)),
                    "omega_deg_per_min": float(e.get("omega_deg_per_min", 0.0)),
                    "alpha_deg_per_min2": float(e.get("alpha_deg_per_min2", 0.0)),
                    "dwell_minutes": e.get("dwell_minutes"),
                }
                for e in recent_all
            ],
            "hypergroups": hypergroups,
            "hyperedges": hyperedges,
            "first_mover": None,
            "event_count": 0,
        }

    first_by_h = {}
    for e in sorted(recent, key=lambda x: int(x.get("time_ms", 0))):
        k = str(int(e["horizon"]))
        if k not in first_by_h:
            first_by_h[k] = e

    ordered_events = sorted(
        first_by_h.values(),
        key=lambda x: int(x.get("time_ms", 0)),
    )
    order = [int(e["horizon"]) for e in ordered_events]
    steps = [
        {
            "horizon": int(e["horizon"]),
            "from_state": str(e.get("from_state", "FLAT")),
            "to_state": str(e.get("to_state", "FLAT")),
            "time_ms": int(e.get("time_ms", 0)),
            "from_tilt_deg": float(e.get("from_tilt_deg", 0.0)),
            "to_tilt_deg": float(e.get("to_tilt_deg", 0.0)),
            "omega_deg_per_min": float(e.get("omega_deg_per_min", 0.0)),
            "alpha_deg_per_min2": float(e.get("alpha_deg_per_min2", 0.0)),
            "dwell_minutes": e.get("dwell_minutes"),
        }
        for e in ordered_events
    ]

    edges = teg_build_edges(
        ordered_events
    )
    graph = teg_graph_summary(
        steps,
        edges,
    )

    edge_steps = [
        {
            "horizon": int(e.get("horizon", 0)),
            "from_state": str(e.get("from_state", "FLAT")),
            "to_state": str(e.get("to_state", "FLAT")),
            "time_ms": int(e.get("time_ms", 0)),
            "omega_deg_per_min": float(e.get("omega_deg_per_min", 0.0)),
            "alpha_deg_per_min2": float(e.get("alpha_deg_per_min2", 0.0)),
            "dwell_minutes": e.get("dwell_minutes"),
        }
        for e in sorted(
            recent_all,
            key=lambda x: (
                int(x.get("time_ms", 0)),
                int(x.get("horizon", 0)),
            ),
        )
    ]

    up = sum(1 for e in ordered_events if e.get("to_state") == "UP")
    down = sum(1 for e in ordered_events if e.get("to_state") == "DOWN")

    if up > down:
        direction = "UP"
    elif down > up:
        direction = "DOWN"
    else:
        direction = "MIXED"

    rank = {5: 0, 15: 1, 30: 2, 60: 3, 120: 4}
    idx = [rank.get(h, 99) for h in order]

    if len(idx) < 2:
        mode = "LOCAL"
    elif all(b >= a for a, b in zip(idx, idx[1:])):
        mode = "MICRO_TO_MACRO"
    elif all(b <= a for a, b in zip(idx, idx[1:])):
        mode = "MACRO_TO_MICRO"
    else:
        mode = "CROSS_SCALE"

    return {
        "mode": mode,
        "direction": direction,
        "order": order,
        "steps": steps,
        "edges": edges,
        "graph": graph,
        "edge_steps": edge_steps,
        "hypergroups": hypergroups,
        "hyperedges": hyperedges,
        "first_mover": order[0] if order else None,
        "event_count": len(order),
    }



def ctd_inversion_band(states):
    """
    Detect a contiguous block in scale-space that is inverted relative to
    BOTH outer boundaries. Example:
        H5 UP | H15 DOWN | H30 DOWN | H60 DOWN | H120 UP
        -> H15_H60_DOWN_ISLAND
    """
    scales = [5, 15, 30, 60, 120]
    seq = [str(states.get(str(h), "FLAT")) for h in scales]

    best = None

    i = 0
    while i < len(seq):
        state = seq[i]
        j = i
        while j + 1 < len(seq) and seq[j + 1] == state:
            j += 1

        run_len = j - i + 1

        if (
            state in ("UP", "DOWN")
            and run_len >= 2
            and i > 0
            and j < len(seq) - 1
        ):
            left = seq[i - 1]
            right = seq[j + 1]
            opposite = "DOWN" if state == "UP" else "UP"

            if left == opposite and right == opposite:
                candidate = {
                    "start_h": scales[i],
                    "end_h": scales[j],
                    "direction": state,
                    "length": run_len,
                    "signature": (
                        f"H{scales[i]}_H{scales[j]}_{state}_ISLAND"
                    ),
                }

                if (
                    best is None
                    or candidate["length"] > best["length"]
                ):
                    best = candidate

        i = j + 1

    if best is None:
        return {
            "signature": "NONE",
            "start_h": None,
            "end_h": None,
            "direction": "NONE",
            "length": 0,
        }

    return best


def ctd_state_pattern(states):
    symbol = {
        "UP": "U",
        "DOWN": "D",
        "FLAT": "F",
    }
    return "-".join(
        symbol.get(
            str(states.get(str(h), "FLAT")),
            "F",
        )
        for h in (5, 15, 30, 60, 120)
    )



def spt_scale_coord(horizon):
    return math.log(
        max(float(horizon), 1e-9) / 5.0,
        2.0,
    )


def spt_detect_domains(states):
    scales = [5, 15, 30, 60, 120]
    domains = []
    i = 0

    while i < len(scales):
        direction = str(
            states.get(
                str(scales[i]),
                "FLAT",
            )
        )

        if direction not in ("UP", "DOWN", "FLAT"):
            i += 1
            continue

        j = i
        while (
            j + 1 < len(scales)
            and str(
                states.get(
                    str(scales[j + 1]),
                    "FLAT",
                )
            ) == direction
        ):
            j += 1

        block = scales[i:j + 1]
        start_h = block[0]
        end_h = block[-1]
        start_coord = spt_scale_coord(start_h)
        end_coord = spt_scale_coord(end_h)

        left_state = (
            str(
                states.get(
                    str(scales[i - 1]),
                    "FLAT",
                )
            )
            if i > 0
            else "EDGE"
        )
        right_state = (
            str(
                states.get(
                    str(scales[j + 1]),
                    "FLAT",
                )
            )
            if j + 1 < len(scales)
            else "EDGE"
        )

        opposite = (
            "DOWN" if direction == "UP" else
            "UP" if direction == "DOWN" else
            None
        )

        domains.append({
            "direction": direction,
            "scales": block,
            "start_h": start_h,
            "end_h": end_h,
            "start_coord": round(start_coord, 6),
            "end_coord": round(end_coord, 6),
            "center_log2_scale": round(
                (start_coord + end_coord) / 2.0,
                6,
            ),
            "width_octaves": round(
                max(0.0, end_coord - start_coord),
                6,
            ),
            "left_state": left_state,
            "right_state": right_state,
            "island": (
                opposite is not None
                and i > 0
                and j + 1 < len(scales)
                and left_state == opposite
                and right_state == opposite
            ),
            "signature": (
                f"{direction}_H{start_h}"
                if start_h == end_h
                else f"{direction}_H{start_h}_H{end_h}"
            ),
        })

        i = j + 1

    return domains


def spt_boundaries(states):
    scales = [5, 15, 30, 60, 120]
    out = []

    for left_h, right_h in zip(
        scales,
        scales[1:],
    ):
        left = str(
            states.get(
                str(left_h),
                "FLAT",
            )
        )
        right = str(
            states.get(
                str(right_h),
                "FLAT",
            )
        )

        if left == right:
            continue

        out.append({
            "left_h": left_h,
            "right_h": right_h,
            "left_state": left,
            "right_state": right,
            "coord_log2_scale": round(
                (
                    spt_scale_coord(left_h)
                    + spt_scale_coord(right_h)
                ) / 2.0,
                6,
            ),
            "signature": (
                f"H{left_h}_{left}"
                f"|H{right_h}_{right}"
            ),
        })

    return out


def spt_overlap(a, b):
    sa = set(
        int(x)
        for x in a.get("scales", [])
    )
    sb = set(
        int(x)
        for x in b.get("scales", [])
    )

    if not sa or not sb:
        return 0.0

    return (
        len(sa & sb)
        / max(1, len(sa | sb))
    )


def spt_lifecycle(prev, cur, has_prior_observation=True):
    cs = int(cur.get("start_h", 0))
    ce = int(cur.get("end_h", 0))
    cur_full = (cs == 5 and ce == 120)

    if prev is None:
        if not has_prior_observation:
            return "INITIAL_FULL_DOMAIN" if cur_full else "INITIAL_DOMAIN"
        return "NUCLEATE"

    ps = int(prev.get("start_h", 0))
    pe = int(prev.get("end_h", 0))
    prev_full = (ps == 5 and pe == 120)

    # TAKEOVER must be observed as an actual expansion into the whole scale-space.
    if cur_full and not prev_full:
        return "TAKEOVER"

    micro_expand = cs < ps
    micro_contract = cs > ps
    macro_expand = ce > pe
    macro_contract = ce < pe

    if micro_expand and macro_expand:
        return "EXPAND_BOTH"
    if micro_contract and macro_contract:
        return "CONTRACT_BOTH"
    if micro_expand and macro_contract:
        return "DRIFT_MICRO"
    if micro_contract and macro_expand:
        return "DRIFT_MACRO"
    if micro_expand:
        return "EXPAND_MICRO"
    if macro_expand:
        return "EXPAND_MACRO"
    if micro_contract:
        return "CONTRACT_MICRO"
    if macro_contract:
        return "CONTRACT_MACRO"

    return "STABLE"


def spt_majority_state(states, horizons):
    values = [str(states.get(str(h), "FLAT")) for h in horizons]
    up = sum(v == "UP" for v in values)
    down = sum(v == "DOWN" for v in values)
    flat = sum(v == "FLAT" for v in values)
    if up > down and up > flat:
        return "UP"
    if down > up and down > flat:
        return "DOWN"
    if flat >= up and flat >= down:
        return "FLAT"
    return "MIXED"


def spt_topology_class(states, domains, boundaries):
    seq = [str(states.get(str(h), "FLAT")) for h in (5, 15, 30, 60, 120)]

    if all(x == "UP" for x in seq):
        return "FULL_UP_DOMAIN"
    if all(x == "DOWN" for x in seq):
        return "FULL_DOWN_DOMAIN"
    if all(x == "FLAT" for x in seq):
        return "FULL_FLAT_DOMAIN"

    # Explicit one-front scale stacks. These are more informative than a
    # generic SINGLE_FRONT label and prevent U-U-U-U-D from being unresolved.
    if seq == ["UP", "UP", "UP", "UP", "DOWN"]:
        return "UP_DOMAIN_BELOW_MACRO_DOWN"
    if seq == ["DOWN", "DOWN", "DOWN", "DOWN", "UP"]:
        return "DOWN_DOMAIN_BELOW_MACRO_UP"
    if seq == ["UP", "UP", "UP", "DOWN", "DOWN"]:
        return "MICRO_MESO_UP_IN_MACRO_DOWN"
    if seq == ["DOWN", "DOWN", "DOWN", "UP", "UP"]:
        return "MICRO_MESO_DOWN_IN_MACRO_UP"

    islands = [d for d in domains if bool(d.get("island", False))]
    if islands:
        primary = max(islands, key=lambda d: (float(d.get("width_octaves", 0.0)), len(d.get("scales", []))))
        if str(primary.get("direction")) == "UP":
            return "UP_ISLAND_IN_DOWN"
        return "DOWN_ISLAND_IN_UP"

    micro = spt_majority_state(states, (5, 15))
    macro = spt_majority_state(states, (60, 120))

    if micro == "UP" and macro == "DOWN":
        return "MICRO_UP_IN_MACRO_DOWN"
    if micro == "DOWN" and macro == "UP":
        return "MICRO_DOWN_IN_MACRO_UP"
    if micro == "FLAT" and macro == "DOWN":
        return "MICRO_STABLE_IN_MACRO_DOWN"
    if micro == "FLAT" and macro == "UP":
        return "MICRO_STABLE_IN_MACRO_UP"

    if len(boundaries) == 1:
        b = boundaries[0]
        return f"SINGLE_FRONT_{b.get('left_state','FLAT')}_TO_{b.get('right_state','FLAT')}"
    if len(boundaries) >= 2:
        return "MIXED_STACK"

    return "UNRESOLVED_BOUNDARY_STATE"


def spt_track_boundaries(previous, current, dt, close_time_ms, next_boundary_id, has_prior_observation):
    previous = list(previous or [])
    current = [dict(x) for x in (current or [])]
    used_prev = set()
    events = []

    for boundary in current:
        orientation = (str(boundary.get("left_state", "FLAT")), str(boundary.get("right_state", "FLAT")))
        candidates = []
        for idx, prev in enumerate(previous):
            if idx in used_prev:
                continue
            porientation = (str(prev.get("left_state", "FLAT")), str(prev.get("right_state", "FLAT")))
            if porientation != orientation:
                continue
            dist = abs(float(boundary.get("coord_log2_scale", 0.0)) - float(prev.get("coord_log2_scale", 0.0)))
            candidates.append((dist, idx, prev))

        if candidates:
            candidates.sort(key=lambda x: x[0])
            _, idx, prev = candidates[0]
            used_prev.add(idx)
            boundary_id = str(prev.get("boundary_id", f"B{next_boundary_id}"))
            age = float(prev.get("age_minutes", 0.0)) + dt
            old_v = float(prev.get("velocity_log2_per_min", 0.0))
            if dt > 1e-9:
                velocity = (float(boundary.get("coord_log2_scale", 0.0)) - float(prev.get("coord_log2_scale", 0.0))) / dt
                acceleration = (velocity - old_v) / dt
            else:
                velocity = 0.0
                acceleration = 0.0

            if velocity < -PHASE_BOUNDARY_MOVE_EPS:
                life = "MOVE_MICRO"
            elif velocity > PHASE_BOUNDARY_MOVE_EPS:
                life = "MOVE_MACRO"
            else:
                life = "STABLE"
        else:
            prev = None
            boundary_id = f"B{next_boundary_id}"
            next_boundary_id += 1
            age = 0.0
            velocity = 0.0
            acceleration = 0.0
            life = "INITIAL_BOUNDARY" if not has_prior_observation else "BORN"

        boundary.update({
            "boundary_id": boundary_id,
            "age_minutes": round(age, 4),
            "velocity_log2_per_min": round(velocity, 6),
            "acceleration_log2_per_min2": round(acceleration, 6),
            "lifecycle": life,
        })

        if life != "STABLE":
            events.append({
                "time_ms": int(close_time_ms),
                "boundary_id": boundary_id,
                "event": life,
                "left_h": int(boundary.get("left_h", 0)),
                "right_h": int(boundary.get("right_h", 0)),
                "left_state": str(boundary.get("left_state", "FLAT")),
                "right_state": str(boundary.get("right_state", "FLAT")),
                "coord_log2_scale": float(boundary.get("coord_log2_scale", 0.0)),
                "velocity_log2_per_min": float(boundary.get("velocity_log2_per_min", 0.0)),
            })

    for idx, prev in enumerate(previous):
        if idx in used_prev:
            continue
        events.append({
            "time_ms": int(close_time_ms),
            "boundary_id": str(prev.get("boundary_id", f"B?")),
            "event": "ANNIHILATE",
            "left_h": int(prev.get("left_h", 0)),
            "right_h": int(prev.get("right_h", 0)),
            "left_state": str(prev.get("left_state", "FLAT")),
            "right_state": str(prev.get("right_state", "FLAT")),
            "coord_log2_scale": float(prev.get("coord_log2_scale", 0.0)),
            "velocity_log2_per_min": float(prev.get("velocity_log2_per_min", 0.0)),
        })

    return current, events, next_boundary_id


def compute_scale_phase_topology(
    states,
    close_time_ms,
    new_events,
):
    global phase_topology_tracker

    if not PHASE_TOPOLOGY_RESEARCH_ENABLED:
        return {"version": PHASE_TOPOLOGY_VERSION, "ready": False}

    prev_time = phase_topology_tracker.get("last_close_time_ms")
    has_prior_observation = prev_time is not None
    dt = (
        max(1e-9, (int(close_time_ms) - int(prev_time)) / MINUTE_MS)
        if has_prior_observation
        else 0.0
    )

    previous = list(phase_topology_tracker.get("domains", []) or [])
    current = spt_detect_domains(states)
    next_id = int(phase_topology_tracker.get("next_domain_id", 1))
    domain_events = []
    current_ids = set()

    for domain in current:
        candidates = []
        for prev in previous:
            if str(prev.get("direction")) != str(domain.get("direction")):
                continue
            score = spt_overlap(prev, domain)
            if score > 0.0:
                candidates.append((score, prev))

        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            prev = candidates[0][1]
            domain_id = str(prev.get("domain_id", f"D{next_id}"))
        else:
            prev = None
            domain_id = f"D{next_id}"
            next_id += 1

        lifecycle = spt_lifecycle(prev, domain, has_prior_observation=has_prior_observation)

        if prev is None:
            age = 0.0
            center_v = width_v = micro_v = macro_v = 0.0
        else:
            age = float(prev.get("age_minutes", 0.0)) + dt
            if dt > 1e-9:
                center_v = (float(domain["center_log2_scale"]) - float(prev.get("center_log2_scale", domain["center_log2_scale"]))) / dt
                width_v = (float(domain["width_octaves"]) - float(prev.get("width_octaves", domain["width_octaves"]))) / dt
                micro_v = (float(domain["start_coord"]) - float(prev.get("start_coord", domain["start_coord"]))) / dt
                macro_v = (float(domain["end_coord"]) - float(prev.get("end_coord", domain["end_coord"]))) / dt
            else:
                center_v = width_v = micro_v = macro_v = 0.0

        domain.update({
            "domain_id": domain_id,
            "lifecycle": lifecycle,
            "age_minutes": round(age, 4),
            "center_velocity_log2_per_min": round(center_v, 6),
            "width_velocity_oct_per_min": round(width_v, 6),
            "micro_boundary_velocity_log2_per_min": round(micro_v, 6),
            "macro_boundary_velocity_log2_per_min": round(macro_v, 6),
        })
        current_ids.add(domain_id)

        if lifecycle != "STABLE":
            domain_events.append({
                "time_ms": int(close_time_ms),
                "domain_id": domain_id,
                "event": lifecycle,
                "direction": domain["direction"],
                "start_h": domain["start_h"],
                "end_h": domain["end_h"],
                "width_octaves": domain["width_octaves"],
            })

    for prev in previous:
        pid = str(prev.get("domain_id", ""))
        if pid and pid not in current_ids:
            domain_events.append({
                "time_ms": int(close_time_ms),
                "domain_id": pid,
                "event": "COLLAPSE",
                "direction": str(prev.get("direction", "NONE")),
                "start_h": int(prev.get("start_h", 0)),
                "end_h": int(prev.get("end_h", 0)),
                "width_octaves": float(prev.get("width_octaves", 0.0)),
            })

    raw_boundaries = spt_boundaries(states)
    # Integrity invariant: every adjacent state change is a phase boundary.
    # This catches impossible states such as U-U-U-U-D with boundaries=0.
    scale_seq = [str(states.get(str(h), "FLAT")) for h in (5, 15, 30, 60, 120)]
    expected_boundary_count = sum(1 for a, b in zip(scale_seq, scale_seq[1:]) if a != b)
    boundary_integrity_ok = (len(raw_boundaries) == expected_boundary_count)
    boundaries, boundary_events, next_boundary_id = spt_track_boundaries(
        phase_topology_tracker.get("boundaries", []),
        raw_boundaries,
        dt,
        close_time_ms,
        int(phase_topology_tracker.get("next_boundary_id", 1)),
        has_prior_observation,
    )

    sync_groups = []
    if new_events:
        by_time = {}
        for event in new_events:
            by_time.setdefault(int(event.get("time_ms", close_time_ms)), []).append(event)
        for time_ms, group in sorted(by_time.items()):
            nodes = sorted(teg_node_token(e) for e in group)
            sync_groups.append({
                "time_ms": int(time_ms),
                "nodes": nodes,
                "size": len(nodes),
                "simultaneous": len(nodes) > 1,
                "signature": "{" + ",".join(nodes) + "}",
            })

    history = list(phase_topology_tracker.get("events", []) or []) + domain_events
    history = history[-PHASE_TOPOLOGY_EVENT_HISTORY:]
    boundary_history = list(phase_topology_tracker.get("boundary_events", []) or []) + boundary_events
    boundary_history = boundary_history[-PHASE_BOUNDARY_EVENT_HISTORY:]

    islands = [d for d in current if bool(d.get("island", False))]
    if islands:
        primary = max(islands, key=lambda d: (float(d.get("width_octaves", 0.0)), len(d.get("scales", []))))
        primary_island = str(primary.get("signature", "NONE"))
    else:
        primary_island = "NONE"

    pattern = ctd_state_pattern(states)
    topology_class = spt_topology_class(states, current, boundaries)
    previous_class = str(phase_topology_tracker.get("last_topology_class", "UNOBSERVED"))
    topology_transition = (
        f"{previous_class}->{topology_class}"
        if previous_class not in ("UNOBSERVED", topology_class)
        else "NONE"
    )

    signature = (
        pattern + "|" + topology_class + "|" +
        (",".join(f"{d.get('signature')}:{d.get('lifecycle')}" for d in current) if current else "NO_DOMAIN") + "|" +
        (",".join(f"{b.get('boundary_id')}:{b.get('signature')}:{b.get('lifecycle')}" for b in boundaries) if boundaries else "NO_BOUNDARY")
    )

    phase_topology_tracker = {
        "last_close_time_ms": int(close_time_ms),
        "domains": current,
        "boundaries": boundaries,
        "events": history,
        "boundary_events": boundary_history,
        "next_domain_id": next_id,
        "next_boundary_id": next_boundary_id,
        "last_topology_class": topology_class,
        "last_signature": signature,
    }

    active_flags = [f"SPT_CLASS_{topology_class}"]
    if topology_transition != "NONE":
        active_flags.append("SPT_CLASS_TRANSITION")

    for d in current:
        direction = str(d.get("direction", "NONE"))
        start_h = int(d.get("start_h", 0))
        end_h = int(d.get("end_h", 0))
        lifecycle = str(d.get("lifecycle", "STABLE"))
        active_flags.append(f"SPT_DOMAIN_{direction}_H{start_h}_H{end_h}")
        active_flags.append(f"SPT_LIFE_{lifecycle}")
        if bool(d.get("island", False)):
            active_flags.append(f"SPT_ISLAND_{direction}_H{start_h}_H{end_h}")

    for b in boundaries:
        active_flags.append(f"PBD_{b.get('left_state','F')}_TO_{b.get('right_state','F')}")
        life = str(b.get("lifecycle", "STABLE"))
        if life != "STABLE":
            active_flags.append(f"PBD_LIFE_{life}")

    if any(g.get("simultaneous", False) for g in sync_groups):
        active_flags.append("SPT_SYNC_HYPEREVENT")

    return {
        "version": PHASE_TOPOLOGY_VERSION,
        "boundary_version": PHASE_BOUNDARY_VERSION,
        "ready": True,
        "state_pattern": pattern,
        "topology_class": topology_class,
        "topology_transition": topology_transition,
        "domains": current,
        "boundaries": boundaries,
        "domain_events": domain_events,
        "boundary_events": boundary_events,
        "event_history": history,
        "boundary_event_history": boundary_history,
        "sync_groups": sync_groups,
        "domain_count": len(current),
        "boundary_count": len(boundaries),
        "expected_boundary_count": expected_boundary_count,
        "boundary_integrity_ok": bool(boundary_integrity_ok and len(boundaries) == expected_boundary_count),
        "primary_island": primary_island,
        "active_flags": sorted(set(active_flags)),
        "signature": signature,
        "research_only": True,
    }


def compute_cone_transition_dynamics(cone_model, close_time_ms):
    global cone_transition_tracker

    if (
        not CONE_DYNAMICS_RESEARCH_ENABLED
        or not cone_model.get("ready", False)
    ):
        return {
            "version": CONE_DYNAMICS_VERSION,
            "ready": False,
        }

    prev_time = cone_transition_tracker.get("last_close_time_ms")
    if prev_time is None:
        dt = 0.0
    else:
        dt = max(
            (int(close_time_ms) - int(prev_time)) / MINUTE_MS,
            1e-9,
        )

    prev_horizons = dict(
        cone_transition_tracker.get("horizons", {}) or {}
    )
    events = list(
        cone_transition_tracker.get("events", []) or []
    )

    rows = {}
    new_events = []
    signed_omega = []
    abs_omega = []
    shock_horizon = None
    shock_speed = -1.0

    for h in CONE_HORIZONS:
        key = str(int(h))
        model = cone_model.get("horizons", {}).get(key, {})
        tilt = float(model.get("tilt_deg", 0.0))
        state = ctd_tilt_state(tilt)

        prev = prev_horizons.get(key, {})
        prev_tilt = float(prev.get("tilt_deg", tilt))
        prev_omega = float(prev.get("omega_deg_per_min", 0.0))
        prev_state = str(prev.get("tilt_state", state))

        if prev_time is None or dt <= 0:
            omega = 0.0
            alpha = 0.0
        else:
            omega = (tilt - prev_tilt) / dt
            alpha = (omega - prev_omega) / dt

        if prev_time is not None and state != prev_state:
            previous_same_h_event = None
            for old_event in reversed(events):
                if int(old_event.get("horizon", -1)) == int(h):
                    previous_same_h_event = old_event
                    break

            if previous_same_h_event is None:
                dwell_minutes = None
            else:
                dwell_minutes = max(
                    0.0,
                    (
                        int(close_time_ms)
                        - int(previous_same_h_event.get("time_ms", close_time_ms))
                    ) / MINUTE_MS,
                )

            event = {
                "time_ms": int(close_time_ms),
                "horizon": int(h),
                "from_state": prev_state,
                "to_state": state,
                "from_tilt_deg": round(prev_tilt, 4),
                "to_tilt_deg": round(tilt, 4),
                "omega_deg_per_min": round(omega, 4),
                "alpha_deg_per_min2": round(alpha, 4),
                "dwell_minutes": (
                    round(dwell_minutes, 4)
                    if dwell_minutes is not None
                    else None
                ),
            }
            events.append(event)
            new_events.append(event)

        speed = abs(omega)
        if speed > shock_speed:
            shock_speed = speed
            shock_horizon = int(h)

        signed_omega.append(omega)
        abs_omega.append(speed)

        rows[key] = {
            "horizon": int(h),
            "tilt_deg": round(tilt, 4),
            "tilt_state": state,
            "previous_tilt_deg": round(prev_tilt, 4),
            "omega_deg_per_min": round(omega, 4),
            "alpha_deg_per_min2": round(alpha, 4),
            "rotation_label": ctd_rotation_label(omega),
            "state_changed": (
                state != prev_state if prev_time is not None else False
            ),
        }

    history_cutoff = int(
        close_time_ms
        - max(
            60.0,
            CONE_DYNAMICS_PROPAGATION_WINDOW_MIN * 4.0,
        ) * MINUTE_MS
    )
    events = [
        e for e in events
        if int(e.get("time_ms", 0)) >= history_cutoff
    ][-CONE_DYNAMICS_EVENT_HISTORY:]

    propagation = ctd_propagation_from_events(
        events,
        close_time_ms,
    )

    rotation_energy = (
        sum(abs_omega) / max(1, len(abs_omega))
    )
    denominator = sum(abs(x) for x in signed_omega)
    rotation_coherence = (
        abs(sum(signed_omega)) / denominator
        if denominator > 1e-12
        else 0.0
    )
    mean_omega = sum(signed_omega) / max(1, len(signed_omega))

    if mean_omega >= CONE_DYNAMICS_ROT_MED_DEG_PER_MIN:
        rotation_direction = "UP"
    elif mean_omega <= -CONE_DYNAMICS_ROT_MED_DEG_PER_MIN:
        rotation_direction = "DOWN"
    else:
        rotation_direction = "MIXED"

    states = {
        str(h): rows[str(h)]["tilt_state"]
        for h in CONE_HORIZONS
    }

    inversion_band = ctd_inversion_band(
        states
    )
    state_pattern = ctd_state_pattern(
        states
    )

    phase_topology = compute_scale_phase_topology(
        states,
        close_time_ms,
        new_events,
    )

    if (
        states["30"] == "DOWN"
        and states["5"] == "UP"
        and states["15"] == "UP"
        and states["60"] == "UP"
        and states["120"] == "UP"
    ):
        middle_inversion = "H30_ISLAND_DOWN"
    elif (
        states["30"] == "UP"
        and states["5"] == "DOWN"
        and states["15"] == "DOWN"
        and states["60"] == "DOWN"
        and states["120"] == "DOWN"
    ):
        middle_inversion = "H30_ISLAND_UP"
    else:
        middle_inversion = "NONE"

    if (
        states["60"] == "UP"
        and states["120"] == "UP"
        and (
            states["5"] == "DOWN"
            or states["15"] == "DOWN"
        )
    ):
        deformation_front = "MICRO_DOWN_IN_MACRO_UP"
    elif (
        states["60"] == "DOWN"
        and states["120"] == "DOWN"
        and (
            states["5"] == "UP"
            or states["15"] == "UP"
        )
    ):
        deformation_front = "MICRO_UP_IN_MACRO_DOWN"
    elif (
        shock_horizon == 30
        and shock_speed >= CONE_DYNAMICS_ROT_FAST_DEG_PER_MIN
    ):
        deformation_front = "MID_SCALE_SHOCK_H30"
    else:
        deformation_front = "NONE"

    flags = []

    if rotation_energy >= CONE_DYNAMICS_ROT_FAST_DEG_PER_MIN:
        flags.append("CTD_ROTATION_ENERGY_HIGH")

    if (
        rotation_coherence >= 0.70
        and rotation_energy >= CONE_DYNAMICS_ROT_MED_DEG_PER_MIN
    ):
        flags.append("CTD_ROTATION_COHERENT")

    if (
        shock_horizon is not None
        and shock_speed >= CONE_DYNAMICS_ROT_FAST_DEG_PER_MIN
    ):
        flags.append(f"CTD_SHOCK_H{shock_horizon}")

    if middle_inversion != "NONE":
        flags.append("CTD_" + middle_inversion)

    if inversion_band.get("signature") != "NONE":
        flags.append(
            "CTD_BAND_"
            + inversion_band["signature"]
        )

    if deformation_front != "NONE":
        flags.append("CTD_" + deformation_front)

    if propagation["mode"] != "NONE":
        flags.append("CTD_PROP_" + propagation["mode"])

    if propagation["direction"] in ("UP", "DOWN"):
        flags.append("CTD_PROP_DIR_" + propagation["direction"])

    flags.extend(
        phase_topology.get(
            "active_flags",
            [],
        )
    )

    signature = "+".join([
        rotation_direction,
        deformation_front,
        middle_inversion,
        inversion_band.get("signature", "NONE"),
        state_pattern,
        propagation["mode"],
        propagation["direction"],
        (
            f"SHOCK_H{shock_horizon}"
            if shock_horizon is not None
            else "SHOCK_NONE"
        ),
    ])

    cone_transition_tracker["last_close_time_ms"] = int(close_time_ms)
    cone_transition_tracker["horizons"] = rows
    cone_transition_tracker["events"] = events
    cone_transition_tracker["last_signature"] = signature

    return {
        "version": CONE_DYNAMICS_VERSION,
        "ready": True,
        "dt_minutes": round(dt, 4),
        "horizons": rows,
        "rotation_energy_deg_per_min": round(rotation_energy, 4),
        "rotation_coherence": round(rotation_coherence, 6),
        "rotation_direction": rotation_direction,
        "shock_horizon": shock_horizon,
        "shock_speed_deg_per_min": round(max(0.0, shock_speed), 4),
        "middle_inversion": middle_inversion,
        "inversion_band": inversion_band,
        "state_pattern": state_pattern,
        "deformation_front": deformation_front,
        "propagation": propagation,
        "phase_topology": phase_topology,
        "new_events": new_events,
        "active_flags": sorted(set(flags)),
        "signature": signature,
        "research_only": True,
    }


def compute_geometry_layer(
    corridor_features,
    state_features,
    close_time_ms,
):
    global geometry_tracker

    view = list(candles)

    if (
        len(view)
        < max(
            CORRIDOR_WINDOWS
        )
        or not corridor_features.get(
            "ready",
            False,
        )
    ):
        return {
            "version":
                GEOMETRY_VERSION,
            "ready": False,
            "history_length":
                len(view),
        }

    price = float(
        view[-1]["close"]
    )

    atr_pct = float(
        state_features.get(
            "atr_14_pct",
            0.0,
        )
        or 0.0
    )

    atr_abs = (
        price
        * atr_pct
        / 100.0
    )

    cluster_min_abs = (
        price
        * GEOMETRY_CLUSTER_MIN_PCT
        / 100.0
    )

    cluster_tolerance_abs = max(
        atr_abs
        * GEOMETRY_CLUSTER_ATR_MULT,
        cluster_min_abs,
    )

    motion_epsilon_abs = max(
        atr_abs
        * GEOMETRY_MOTION_ATR_FRACTION,
        price * 1e-7,
    )

    horizons = {}

    positive_spine = 0
    negative_spine = 0

    for window in (
        CORRIDOR_WINDOWS
    ):
        current = view[
            -window:
        ]

        closes = [
            float(x["close"])
            for x in current
        ]

        current_high = max(
            float(x["high"])
            for x in current
        )

        current_low = min(
            float(x["low"])
            for x in current
        )

        current_mid = (
            current_high
            + current_low
        ) / 2.0

        robust_mid = (
            geo_median(
                closes
            )
        )

        smooth_mid = (
            geo_ema(
                closes,
                alpha=0.30,
            )
        )

        if (
            len(view)
            >= window * 2
        ):
            previous = view[
                -window * 2:
                -window
            ]
        elif (
            len(view)
            >= window + 1
        ):
            previous = view[
                -window - 1:
                -1
            ]
        else:
            previous = current

        prev_high = max(
            float(x["high"])
            for x in previous
        )

        prev_low = min(
            float(x["low"])
            for x in previous
        )

        prev_mid = (
            prev_high
            + prev_low
        ) / 2.0

        upper_delta_abs = (
            current_high
            - prev_high
        )

        lower_delta_abs = (
            current_low
            - prev_low
        )

        mid_delta_abs = (
            current_mid
            - prev_mid
        )

        mid_velocity_pct = (
            mid_delta_abs
            / price
            * 100.0
            if price
            else 0.0
        )

        if (
            mid_velocity_pct
            > 0.0
        ):
            positive_spine += 1
        elif (
            mid_velocity_pct
            < 0.0
        ):
            negative_spine += 1

        corr = corridor_features[
            "windows"
        ][str(window)]

        horizons[
            str(window)
        ] = {
            "window":
                int(window),
            "upper":
                round(
                    current_high,
                    8,
                ),
            "lower":
                round(
                    current_low,
                    8,
                ),
            "mid":
                round(
                    current_mid,
                    8,
                ),
            "robust_mid":
                round(
                    robust_mid,
                    8,
                ),
            "smooth_mid":
                round(
                    smooth_mid,
                    8,
                ),
            "position":
                float(
                    corr.get(
                        "position",
                        0.5,
                    )
                ),
            "width_pct":
                float(
                    corr.get(
                        "width_pct",
                        0.0,
                    )
                ),
            "cost_normalized_width":
                float(
                    corr.get(
                        "cost_normalized_width",
                        0.0,
                    )
                ),
            "upper_delta_abs":
                round(
                    upper_delta_abs,
                    8,
                ),
            "lower_delta_abs":
                round(
                    lower_delta_abs,
                    8,
                ),
            "mid_delta_abs":
                round(
                    mid_delta_abs,
                    8,
                ),
            "mid_velocity_pct":
                round(
                    mid_velocity_pct,
                    6,
                ),
            "motion":
                geo_motion_label(
                    upper_delta_abs,
                    lower_delta_abs,
                    motion_epsilon_abs,
                ),
        }

    if (
        positive_spine >= 3
        and positive_spine
        > negative_spine
    ):
        spine_state = (
            "SPINE_UP"
        )
    elif (
        negative_spine >= 3
        and negative_spine
        > positive_spine
    ):
        spine_state = (
            "SPINE_DOWN"
        )
    elif (
        positive_spine > 0
        and negative_spine > 0
    ):
        spine_state = (
            "SPINE_TWISTED"
        )
    else:
        spine_state = (
            "SPINE_FLAT"
        )

    multiscale = (
        geo_multiscale_levels(
            view,
            price,
            atr_abs,
            close_time_ms,
        )
    )

    levels = (
        multiscale[
            "levels"
        ]
    )

    structural_zones = (
        geo_build_structural_zones(
            multiscale.get(
                "all_levels",
                levels,
            ),
            price,
            atr_abs,
            close_time_ms,
        )
    )

    zone_context = (
        geo_zone_context(
            structural_zones
        )
    )

    supports = sorted(
        [
            x
            for x in levels
            if x.get(
                "current_role"
            ) == "SUPPORT"
        ],
        key=lambda x: abs(
            price
            - float(
                x["level"]
            )
        ),
    )

    resistances = sorted(
        [
            x
            for x in levels
            if x.get(
                "current_role"
            ) == "RESISTANCE"
        ],
        key=lambda x: abs(
            price
            - float(
                x["level"]
            )
        ),
    )

    nearest_support = (
        supports[0]
        if supports
        else None
    )

    nearest_resistance = (
        resistances[0]
        if resistances
        else None
    )

    near_threshold_abs = max(
        atr_abs
        * GEOMETRY_NEAR_LEVEL_ATR_MULT,
        cluster_min_abs,
    )

    flags = {
        "GEO_SPINE_UP":
            spine_state
            == "SPINE_UP",
        "GEO_SPINE_DOWN":
            spine_state
            == "SPINE_DOWN",
        "GEO_SPINE_TWISTED":
            spine_state
            == "SPINE_TWISTED",
        "GEO_NEAR_SUPPORT":
            bool(
                nearest_support
                and abs(
                    price
                    - float(
                        nearest_support[
                            "level"
                        ]
                    )
                )
                <= near_threshold_abs
            ),
        "GEO_NEAR_RESISTANCE":
            bool(
                nearest_resistance
                and abs(
                    price
                    - float(
                        nearest_resistance[
                            "level"
                        ]
                    )
                )
                <= near_threshold_abs
            ),
        "GEO_LEVEL_FLIP_SUPPORT":
            any(
                x.get(
                    "flip"
                )
                and x.get(
                    "current_role"
                ) == "SUPPORT"
                for x in levels
            ),
        "GEO_LEVEL_FLIP_RESISTANCE":
            any(
                x.get(
                    "flip"
                )
                and x.get(
                    "current_role"
                ) == "RESISTANCE"
                for x in levels
            ),
        "GEO_LEVEL_RETEST":
            any(
                x.get(
                    "retested"
                )
                for x in levels
            ),
        "GEO_LEVEL_CONFLUENCE":
            any(
                int(
                    x.get(
                        "confluence_count",
                        1,
                    )
                )
                >= 2
                for x in levels
            ),
        "GEO_INSIDE_PIVOT_ZONE":
            zone_context.get(
                "inside_pivot_zone"
            )
            is not None,
        "GEO_ZONE_PRESSURE_SUPPORT":
            bool(
                zone_context.get(
                    "nearest_support_zone"
                )
                and float(
                    zone_context[
                        "nearest_support_zone"
                    ].get(
                        "pressure",
                        0.0,
                    )
                )
                >= GEOMETRY_ZONE_PRESSURE_THRESHOLD
            ),
        "GEO_ZONE_PRESSURE_RESISTANCE":
            bool(
                zone_context.get(
                    "nearest_resistance_zone"
                )
                and float(
                    zone_context[
                        "nearest_resistance_zone"
                    ].get(
                        "pressure",
                        0.0,
                    )
                )
                >= GEOMETRY_ZONE_PRESSURE_THRESHOLD
            ),
        "GEO_ZONE_FLIP_SUPPORT":
            any(
                z.get(
                    "flip"
                )
                and z.get(
                    "role"
                ) == "SUPPORT"
                for z in structural_zones
            ),
        "GEO_ZONE_FLIP_RESISTANCE":
            any(
                z.get(
                    "flip"
                )
                and z.get(
                    "role"
                ) == "RESISTANCE"
                for z in structural_zones
            ),
        "GEO_ZONE_RETEST":
            any(
                z.get(
                    "retested"
                )
                for z in structural_zones
            ),
        "GEO_TRANSLATE_UP_30":
            horizons[
                "30"
            ][
                "motion"
            ]
            == "TRANSLATE_UP",
        "GEO_TRANSLATE_DOWN_30":
            horizons[
                "30"
            ][
                "motion"
            ]
            == "TRANSLATE_DOWN",
        "GEO_CONTRACT_CENTER_30":
            horizons[
                "30"
            ][
                "motion"
            ]
            == "CONTRACT_TO_CENTER",
        "GEO_EXPAND_CENTER_30":
            horizons[
                "30"
            ][
                "motion"
            ]
            == "EXPAND_FROM_CENTER",
        "GEO_TRANSLATE_UP_120":
            horizons[
                "120"
            ][
                "motion"
            ]
            == "TRANSLATE_UP",
        "GEO_TRANSLATE_DOWN_120":
            horizons[
                "120"
            ][
                "motion"
            ]
            == "TRANSLATE_DOWN",
    }

    active_flags = sorted(
        key
        for key, enabled
        in flags.items()
        if enabled
    )

    previous_flags = dict(
        geometry_tracker.get(
            "flags",
            {},
        )
        or {}
    )

    transitions = []

    if previous_flags:
        for key in sorted(
            flags.keys()
        ):
            old = bool(
                previous_flags.get(
                    key,
                    False,
                )
            )

            new = bool(
                flags.get(
                    key,
                    False,
                )
            )

            if new and not old:
                transitions.append(
                    "ENTER_"
                    + key
                )
            elif old and not new:
                transitions.append(
                    "EXIT_"
                    + key
                )

    signature_core = [
        x
        for x in active_flags
        if x in {
            "GEO_SPINE_UP",
            "GEO_SPINE_DOWN",
            "GEO_SPINE_TWISTED",
            "GEO_NEAR_SUPPORT",
            "GEO_NEAR_RESISTANCE",
            "GEO_LEVEL_FLIP_SUPPORT",
            "GEO_LEVEL_FLIP_RESISTANCE",
            "GEO_LEVEL_RETEST",
            "GEO_LEVEL_CONFLUENCE",
            "GEO_INSIDE_PIVOT_ZONE",
            "GEO_ZONE_PRESSURE_SUPPORT",
            "GEO_ZONE_PRESSURE_RESISTANCE",
            "GEO_ZONE_FLIP_SUPPORT",
            "GEO_ZONE_FLIP_RESISTANCE",
            "GEO_ZONE_RETEST",
            "GEO_TRANSLATE_UP_30",
            "GEO_TRANSLATE_DOWN_30",
            "GEO_CONTRACT_CENTER_30",
            "GEO_EXPAND_CENTER_30",
        }
    ]

    signature = (
        "+".join(
            signature_core
        )
        if signature_core
        else "GEO_BASE"
    )

    cone_model = compute_cone_model(
        horizons,
        corridor_features,
        price,
        close_time_ms,
    )

    cone_dynamics = compute_cone_transition_dynamics(
        cone_model,
        close_time_ms,
    )

    if (
        cone_model.get("ready", False)
        and cone_dynamics.get("ready", False)
    ):
        cone_model["transition_dynamics"] = cone_dynamics
        cone_model["active_flags"] = sorted(
            set(
                list(cone_model.get("active_flags", []))
                + list(cone_dynamics.get("active_flags", []))
            )
        )

    geometry_tracker = {
        "flags":
            {
                key: bool(value)
                for key, value
                in flags.items()
            },
        "last_signature":
            signature,
        "last_close_time_ms":
            int(
                close_time_ms
            ),
        "last_level_ids":
            [
                x["id"]
                for x in levels
            ],
    }

    return {
        "version":
            GEOMETRY_VERSION,
        "ready": True,
        "price":
            round(
                price,
                8,
            ),
        "atr_abs":
            round(
                atr_abs,
                8,
            ),
        "cluster_tolerance_abs":
            round(
                cluster_tolerance_abs,
                8,
            ),
        "spine_state":
            spine_state,
        "positive_spine_scales":
            positive_spine,
        "negative_spine_scales":
            negative_spine,
        "horizons":
            horizons,
        "extrema_count":
            int(
                multiscale[
                    "extrema_total"
                ]
            ),
        "level_scales":
            multiscale[
                "scales"
            ],
        "confluence_tolerance_abs":
            multiscale[
                "confluence_tolerance_abs"
            ],
        "levels":
            levels,
        "structural_zones":
            structural_zones,
        "nearest_support_zone":
            zone_context.get(
                "nearest_support_zone"
            ),
        "nearest_resistance_zone":
            zone_context.get(
                "nearest_resistance_zone"
            ),
        "inside_pivot_zone":
            zone_context.get(
                "inside_pivot_zone"
            ),
        "nearest_support":
            nearest_support,
        "nearest_resistance":
            nearest_resistance,
        "active_flags":
            active_flags,
        "transitions":
            transitions,
        "signature":
            signature,
        "cone_model":
            cone_model,
        "cone_transition_dynamics":
            cone_dynamics,
    }


def augment_corridor2_with_geometry(
    cor2,
    geometry,
):
    if (
        not geometry.get(
            "ready",
            False,
        )
        or not cor2.get(
            "ready",
            False,
        )
    ):
        return cor2

    out = dict(
        cor2
    )

    cone_model = geometry.get(
        "cone_model",
        {},
    )

    active = sorted(
        set(
            list(
                cor2.get(
                    "active_flags",
                    [],
                )
            )
            + list(
                geometry.get(
                    "active_flags",
                    [],
                )
            )
            + list(
                cone_model.get(
                    "active_flags",
                    [],
                )
            )
        )
    )

    transitions = sorted(
        set(
            list(
                cor2.get(
                    "transitions",
                    [],
                )
            )
            + list(
                geometry.get(
                    "transitions",
                    [],
                )
            )
            + list(
                cone_model.get(
                    "transitions",
                    [],
                )
            )
        )
    )

    out[
        "active_flags"
    ] = active

    out[
        "transitions"
    ] = transitions

    flag_map = dict(
        cor2.get(
            "flag_map",
            {},
        )
        or {}
    )

    for flag in geometry.get(
        "active_flags",
        [],
    ):
        flag_map[
            flag
        ] = True

    for flag in cone_model.get(
        "active_flags",
        [],
    ):
        flag_map[
            flag
        ] = True

    out[
        "flag_map"
    ] = flag_map

    geo_sig = geometry.get(
        "signature",
        "GEO_BASE",
    )

    old_sig = cor2.get(
        "signature",
        "BASE",
    )

    if (
        geo_sig
        != "GEO_BASE"
    ):
        out[
            "signature"
        ] = (
            old_sig
            + "+"
            + geo_sig
        )

    out[
        "geometry_signature"
    ] = geo_sig

    cone_sig = cone_model.get(
        "signature",
        "CONE_BASE",
    )

    if cone_sig != "CONE_BASE":
        out[
            "signature"
        ] = (
            out.get(
                "signature",
                old_sig,
            )
            + "+"
            + cone_sig
        )

    out[
        "cone_signature"
    ] = cone_sig

    return out



def gol_safe_horizon_model(
    cone,
    horizon,
):
    return (
        cone.get(
            "horizons",
            {},
        ).get(
            str(int(horizon)),
            {},
        )
        or {}
    )


def gol_geometry_features(
    state,
):
    """
    Freeze a compact, JSON-safe geometric representation.
    No future candle information is used here.
    """
    geometry = state.get(
        "geometry_state",
        {},
    )

    cone = geometry.get(
        "cone_model",
        {},
    )

    if (
        not geometry.get(
            "ready",
            False,
        )
        or not cone.get(
            "ready",
            False,
        )
    ):
        return {
            "ready": False,
        }

    raw = {}

    for h in (
        5,
        15,
        30,
        60,
        120,
    ):
        m = gol_safe_horizon_model(
            cone,
            h,
        )

        raw[str(h)] = {
            "tilt_deg":
                round(
                    float(
                        m.get(
                            "tilt_deg",
                            0.0,
                        )
                    ),
                    4,
                ),
            "orientation_deg":
                round(
                    float(
                        m.get(
                            "phi_deg",
                            0.0,
                        )
                    ),
                    4,
                ),
            "eccentricity":
                round(
                    float(
                        m.get(
                            "eccentricity",
                            0.0,
                        )
                    ),
                    6,
                ),
            "pressure":
                round(
                    float(
                        m.get(
                            "pressure",
                            0.0,
                        )
                    ),
                    6,
                ),
            "area_velocity_norm":
                round(
                    float(
                        m.get(
                            "area_velocity_norm",
                            m.get(
                                "area_log_change",
                                0.0,
                            ),
                        )
                    ),
                    6,
                ),
            "area_state":
                str(
                    m.get(
                        "area_state",
                        "STABLE",
                    )
                ),
            "omega_deg_per_min":
                round(
                    float(
                        cone.get(
                            "transition_dynamics",
                            {},
                        ).get(
                            "horizons",
                            {},
                        ).get(
                            str(h),
                            {},
                        ).get(
                            "omega_deg_per_min",
                            0.0,
                        )
                    ),
                    6,
                ),
            "alpha_deg_per_min2":
                round(
                    float(
                        cone.get(
                            "transition_dynamics",
                            {},
                        ).get(
                            "horizons",
                            {},
                        ).get(
                            str(h),
                            {},
                        ).get(
                            "alpha_deg_per_min2",
                            0.0,
                        )
                    ),
                    6,
                ),
            "tilt_state":
                str(
                    cone.get(
                        "transition_dynamics",
                        {},
                    ).get(
                        "horizons",
                        {},
                    ).get(
                        str(h),
                        {},
                    ).get(
                        "tilt_state",
                        ctd_tilt_state(
                            m.get(
                                "tilt_deg",
                                0.0,
                            )
                        ),
                    )
                ),
        }

    macro_tilt = (
        0.20 * raw["30"]["tilt_deg"]
        + 0.35 * raw["60"]["tilt_deg"]
        + 0.45 * raw["120"]["tilt_deg"]
    )

    micro_tilt = (
        0.55 * raw["5"]["tilt_deg"]
        + 0.45 * raw["15"]["tilt_deg"]
    )

    if macro_tilt >= 20.0:
        macro_direction = "UP"
    elif macro_tilt <= -20.0:
        macro_direction = "DOWN"
    else:
        macro_direction = "FLAT"

    if micro_tilt >= 12.0:
        micro_direction = "UP"
    elif micro_tilt <= -12.0:
        micro_direction = "DOWN"
    else:
        micro_direction = "FLAT"

    if (
        macro_direction == "UP"
        and micro_direction == "DOWN"
    ):
        scale_relation = "PULLBACK_IN_UP"
    elif (
        macro_direction == "DOWN"
        and micro_direction == "UP"
    ):
        scale_relation = "PULLBACK_IN_DOWN"
    elif (
        macro_direction == "UP"
        and micro_direction == "UP"
    ):
        scale_relation = "ALIGNED_UP"
    elif (
        macro_direction == "DOWN"
        and micro_direction == "DOWN"
    ):
        scale_relation = "ALIGNED_DOWN"
    else:
        scale_relation = "MIXED"

    macro_ecc = (
        raw["60"]["eccentricity"]
        + raw["120"]["eccentricity"]
    ) / 2.0

    macro_pressure = max(
        raw["60"]["pressure"],
        raw["120"]["pressure"],
    )

    twist = cone.get(
        "twist",
        {},
    )

    twist_5_60 = float(
        twist.get(
            "5_60",
            {},
        ).get(
            "combined_deg",
            0.0,
        )
    )

    twist_30_120 = float(
        twist.get(
            "30_120",
            {},
        ).get(
            "combined_deg",
            0.0,
        )
    )

    bend = float(
        cone.get(
            "spine_curvature",
            {},
        ).get(
            "bend_deg",
            0.0,
        )
    )

    pivot = geometry.get(
        "inside_pivot_zone"
    )

    support = geometry.get(
        "nearest_support_zone"
    )

    resistance = geometry.get(
        "nearest_resistance_zone"
    )

    zone = None
    zone_context = "NONE"

    if pivot:
        zone = pivot
        zone_context = "PIVOT"
    else:
        candidates = [
            (
                "SUPPORT",
                support,
            ),
            (
                "RESISTANCE",
                resistance,
            ),
        ]

        candidates = [
            x
            for x in candidates
            if x[1]
        ]

        if candidates:
            zone_context, zone = min(
                candidates,
                key=lambda x: float(
                    x[1].get(
                        "distance_abs",
                        1e99,
                    )
                ),
            )

    if zone:
        zone_pressure = float(
            zone.get(
                "pressure",
                0.0,
            )
        )

        zone_strength = float(
            zone.get(
                "strength",
                0.0,
            )
        )

        zone_flip = bool(
            zone.get(
                "flip",
                False,
            )
        )

        zone_retest = bool(
            zone.get(
                "retested",
                False,
            )
        )

        zone_scales = list(
            zone.get(
                "scales",
                [],
            )
        )
    else:
        zone_pressure = 0.0
        zone_strength = 0.0
        zone_flip = False
        zone_retest = False
        zone_scales = []

    raw_zone_context = zone_context

    if zone is None:
        zone_context = "NONE"
        zone_relevance = "NONE"
    elif raw_zone_context == "PIVOT":
        zone_context = "PIVOT"
        zone_relevance = "INSIDE"
    elif zone_pressure < GOL_ZONE_NEAR_PRESSURE:
        zone_context = "NONE"
        zone_relevance = "FAR"
    elif zone_pressure < GOL_ZONE_ACTIVE_PRESSURE:
        zone_context = raw_zone_context
        zone_relevance = "NEAR"
    else:
        zone_context = raw_zone_context
        zone_relevance = "ACTIVE"

    macro_area = (
        raw["30"]["area_state"]
        + "/"
        + raw["60"]["area_state"]
        + "/"
        + raw["120"]["area_state"]
    )

    ctd_state = cone.get(
        "transition_dynamics",
        {},
    )
    ctd_prop = ctd_state.get(
        "propagation",
        {},
    )

    return {
        "ready": True,
        "cone_version":
            cone.get(
                "version",
                CONE_MODEL_VERSION,
            ),
        "geometry_version":
            geometry.get(
                "version",
                GEOMETRY_VERSION,
            ),
        "raw_horizons":
            raw,
        "macro_tilt_deg":
            round(
                macro_tilt,
                4,
            ),
        "micro_tilt_deg":
            round(
                micro_tilt,
                4,
            ),
        "macro_direction":
            macro_direction,
        "micro_direction":
            micro_direction,
        "scale_relation":
            scale_relation,
        "macro_eccentricity":
            round(
                macro_ecc,
                6,
            ),
        "macro_pressure":
            round(
                macro_pressure,
                6,
            ),
        "twist_5_60_deg":
            round(
                twist_5_60,
                4,
            ),
        "twist_30_120_deg":
            round(
                twist_30_120,
                4,
            ),
        "spine_bend_deg":
            round(
                bend,
                4,
            ),
        "spine_state":
            geometry.get(
                "spine_state",
                "UNKNOWN",
            ),
        "macro_area_signature":
            macro_area,
        "zone_context":
            zone_context,
        "raw_zone_context":
            raw_zone_context,
        "zone_relevance":
            zone_relevance,
        "zone_pressure":
            round(
                zone_pressure,
                6,
            ),
        "zone_strength":
            round(
                zone_strength,
                6,
            ),
        "zone_flip":
            zone_flip,
        "zone_retest":
            zone_retest,
        "zone_scales":
            zone_scales,
        "rotation_energy_deg_per_min":
            round(
                float(
                    ctd_state.get(
                        "rotation_energy_deg_per_min",
                        0.0,
                    )
                ),
                6,
            ),
        "rotation_coherence":
            round(
                float(
                    ctd_state.get(
                        "rotation_coherence",
                        0.0,
                    )
                ),
                6,
            ),
        "rotation_direction":
            str(
                ctd_state.get(
                    "rotation_direction",
                    "MIXED",
                )
            ),
        "shock_horizon":
            ctd_state.get(
                "shock_horizon"
            ),
        "middle_inversion":
            str(
                ctd_state.get(
                    "middle_inversion",
                    "NONE",
                )
            ),
        "inversion_band":
            dict(
                ctd_state.get(
                    "inversion_band",
                    {},
                )
            ),
        "state_pattern":
            str(
                ctd_state.get(
                    "state_pattern",
                    "F-F-F-F-F",
                )
            ),
        "deformation_front":
            str(
                ctd_state.get(
                    "deformation_front",
                    "NONE",
                )
            ),
        "propagation_mode":
            str(
                ctd_prop.get(
                    "mode",
                    "NONE",
                )
            ),
        "propagation_direction":
            str(
                ctd_prop.get(
                    "direction",
                    "NONE",
                )
            ),
        "propagation_order":
            list(
                ctd_prop.get(
                    "order",
                    [],
                )
            ),
        "propagation_steps":
            list(
                ctd_prop.get(
                    "steps",
                    [],
                )
            ),
        "propagation_edges":
            list(
                ctd_prop.get(
                    "edges",
                    [],
                )
            ),
        "propagation_edge_steps":
            list(
                ctd_prop.get(
                    "edge_steps",
                    [],
                )
            ),
        "propagation_hypergroups":
            list(
                ctd_prop.get(
                    "hypergroups",
                    [],
                )
            ),
        "propagation_hyperedges":
            list(
                ctd_prop.get(
                    "hyperedges",
                    [],
                )
            ),
        "transition_edge_graph":
            dict(
                ctd_prop.get(
                    "graph",
                    {},
                )
            ),
        "ctd_signature":
            str(
                ctd_state.get(
                    "signature",
                    "CTD_BASE",
                )
            ),
        "phase_topology":
            dict(
                ctd_state.get(
                    "phase_topology",
                    {},
                )
            ),
        "ctd_flags":
            list(
                ctd_state.get(
                    "active_flags",
                    [],
                )
            ),
        "phase_front_lag": {
            "ready": bool(state.get("phase_front_lag", {}).get("ready", False)),
            "front_direction": str(state.get("phase_front_lag", {}).get("front_direction", "NONE")),
            "forecast_bias": str(state.get("phase_front_lag", {}).get("forecast_bias", "NONE")),
            "propagation_mode": str(state.get("phase_front_lag", {}).get("propagation_mode", "NONE")),
            "from_horizon": int(state.get("phase_front_lag", {}).get("from_horizon", 0) or 0),
            "front_horizon": int(state.get("phase_front_lag", {}).get("front_horizon", 0) or 0),
            "latency_minutes": round(float(state.get("phase_front_lag", {}).get("latency_minutes", 0.0) or 0.0), 4),
            "velocity_log2h_per_min": round(float(state.get("phase_front_lag", {}).get("velocity_log2h_per_min", 0.0) or 0.0), 6),
            "strength": round(float(state.get("phase_front_lag", {}).get("strength", 0.0) or 0.0), 4),
            "sequence_path": str(state.get("phase_front_lag", {}).get("sequence_path", "")),
        },
        "geometry_flags":
            list(
                geometry.get(
                    "active_flags",
                    [],
                )
            ),
        "cone_flags":
            list(
                cone.get(
                    "active_flags",
                    [],
                )
            ),
    }


def gol_feature_keys(
    features,
    horizon,
):
    """
    Multiple low-dimensional cells are learned in parallel. This avoids
    one giant sparse signature and lets us discover which geometric pieces
    actually carry outcome information.
    """
    h = int(horizon)

    keys = [
        f"H{h}|MACRO_{features['macro_direction']}",
        f"H{h}|MICRO_{features['micro_direction']}",
        f"H{h}|REL_{features['scale_relation']}",
        f"H{h}|ZONE_{features['zone_context']}",
        f"H{h}|ZONE_REL_{features['zone_relevance']}",
        f"H{h}|SPINE_{features['spine_state']}",
        f"H{h}|AREA_{features['macro_area_signature']}",
    ]

    pfl = features.get("phase_front_lag", {})
    if isinstance(pfl, dict) and pfl.get("ready", False):
        pf_strength = float(pfl.get("strength", 0.0) or 0.0)
        pf_band = "HIGH" if pf_strength >= 0.65 else "MID" if pf_strength >= 0.35 else "LOW"
        keys.extend([
            f"H{h}|PFL_DIR_{pfl.get('front_direction','NONE')}",
            f"H{h}|PFL_MODE_{pfl.get('propagation_mode','NONE')}",
            f"H{h}|PFL_STRENGTH_{pf_band}",
        ])

    ecc = float(
        features[
            "macro_eccentricity"
        ]
    )

    pressure = float(
        features[
            "macro_pressure"
        ]
    )

    zpressure = float(
        features[
            "zone_pressure"
        ]
    )

    twist = float(
        features[
            "twist_5_60_deg"
        ]
    )

    bend = float(
        features[
            "spine_bend_deg"
        ]
    )

    keys.append(
        f"H{h}|ECC_"
        + (
            "HIGH"
            if ecc >= 0.85
            else "MID"
            if ecc >= 0.60
            else "LOW"
        )
    )

    keys.append(
        f"H{h}|CP_"
        + (
            "HIGH"
            if pressure >= 0.60
            else "MID"
            if pressure >= 0.30
            else "LOW"
        )
    )

    keys.append(
        f"H{h}|ZP_"
        + (
            "HIGH"
            if zpressure >= 0.60
            else "MID"
            if zpressure >= 0.30
            else "LOW"
        )
    )

    keys.append(
        f"H{h}|TWIST_"
        + (
            "HIGH"
            if twist >= 40.0
            else "MID"
            if twist >= 20.0
            else "LOW"
        )
    )

    keys.append(
        f"H{h}|BEND_"
        + (
            "UP"
            if bend >= 12.0
            else "DOWN"
            if bend <= -12.0
            else "FLAT"
        )
    )

    rotation_energy = float(
        features.get(
            "rotation_energy_deg_per_min",
            0.0,
        )
    )
    rotation_coherence = float(
        features.get(
            "rotation_coherence",
            0.0,
        )
    )

    keys.append(
        f"H{h}|ROT_"
        + (
            "HIGH"
            if rotation_energy >= CONE_DYNAMICS_ROT_FAST_DEG_PER_MIN
            else "MID"
            if rotation_energy >= CONE_DYNAMICS_ROT_MED_DEG_PER_MIN
            else "LOW"
        )
    )

    keys.append(
        f"H{h}|ROT_DIR_{features.get('rotation_direction','MIXED')}"
    )

    keys.append(
        f"H{h}|ROT_COH_"
        + (
            "HIGH"
            if rotation_coherence >= 0.70
            else "MID"
            if rotation_coherence >= 0.35
            else "LOW"
        )
    )

    shock_horizon = features.get("shock_horizon")
    if shock_horizon is not None:
        keys.append(
            f"H{h}|SHOCK_H{int(shock_horizon)}"
        )

    middle_inversion = features.get(
        "middle_inversion",
        "NONE",
    )
    if middle_inversion != "NONE":
        keys.append(
            f"H{h}|{middle_inversion}"
        )

    inversion_band = features.get(
        "inversion_band",
        {},
    )
    inversion_band_sig = str(
        inversion_band.get(
            "signature",
            "NONE",
        )
    )
    if inversion_band_sig != "NONE":
        keys.append(
            f"H{h}|BAND_{inversion_band_sig}"
        )

    deformation_front = features.get(
        "deformation_front",
        "NONE",
    )
    if deformation_front != "NONE":
        keys.append(
            f"H{h}|FRONT_{deformation_front}"
        )

    prop_mode = features.get(
        "propagation_mode",
        "NONE",
    )
    prop_dir = features.get(
        "propagation_direction",
        "NONE",
    )

    if prop_mode != "NONE":
        keys.append(
            f"H{h}|PROP_{prop_mode}"
        )

    if prop_dir in ("UP", "DOWN"):
        keys.append(
            f"H{h}|PROP_DIR_{prop_dir}"
        )

    for scale in (5, 15, 30, 60, 120):
        dyn = features["raw_horizons"][str(scale)]
        omega = float(
            dyn.get(
                "omega_deg_per_min",
                0.0,
            )
        )
        alpha = float(
            dyn.get(
                "alpha_deg_per_min2",
                0.0,
            )
        )

        if abs(omega) >= CONE_DYNAMICS_ROT_FAST_DEG_PER_MIN:
            keys.append(
                f"H{h}|H{scale}_ROT_"
                + (
                    "UP_FAST"
                    if omega > 0
                    else "DOWN_FAST"
                )
            )

        if abs(alpha) >= CONE_DYNAMICS_ACCEL_FAST_DEG_PER_MIN2:
            keys.append(
                f"H{h}|H{scale}_ACC_"
                + (
                    "UP_FAST"
                    if alpha > 0
                    else "DOWN_FAST"
                )
            )

    if (
        features.get("zone_context") in ("SUPPORT", "RESISTANCE")
    ):
        keys.append(
            f"H{h}|ZONE_{features['zone_context']}_{features['zone_relevance']}"
        )

    if (
        features.get("zone_flip")
        and features.get("zone_relevance") != "FAR"
    ):
        keys.append(
            f"H{h}|ZONE_FLIP"
        )

    if (
        features.get("zone_retest")
        and features.get("zone_relevance") != "FAR"
    ):
        keys.append(
            f"H{h}|ZONE_RETEST"
        )

    # High-value hypotheses from the cone idea.
    if (
        features[
            "macro_direction"
        ] == "UP"
        and features[
            "scale_relation"
        ] == "PULLBACK_IN_UP"
        and features[
            "zone_context"
        ] == "SUPPORT"
        and features["zone_relevance"] in ("NEAR", "ACTIVE", "INSIDE")
    ):
        keys.append(
            f"H{h}|COMBO_MACRO_UP_PULLBACK_SUPPORT"
        )

    if (
        features[
            "macro_direction"
        ] == "DOWN"
        and features[
            "scale_relation"
        ] == "PULLBACK_IN_DOWN"
        and features[
            "zone_context"
        ] == "RESISTANCE"
        and features["zone_relevance"] in ("NEAR", "ACTIVE", "INSIDE")
    ):
        keys.append(
            f"H{h}|COMBO_MACRO_DOWN_PULLBACK_RESISTANCE"
        )

    if (
        features[
            "macro_direction"
        ] == "UP"
        and features[
            "micro_direction"
        ] == "UP"
        and pressure >= 0.60
    ):
        keys.append(
            f"H{h}|COMBO_ALIGNED_UP_PRESSURE"
        )

    if (
        features[
            "macro_direction"
        ] == "DOWN"
        and features[
            "micro_direction"
        ] == "DOWN"
        and pressure >= 0.60
    ):
        keys.append(
            f"H{h}|COMBO_ALIGNED_DOWN_PRESSURE"
        )

    topology = dict(
        features.get(
            "phase_topology",
            {},
        )
    )

    if topology.get("ready", False):
        pattern = str(
            topology.get(
                "state_pattern",
                "F-F-F-F-F",
            )
        )
        keys.append(
            f"H{h}|SPT_PATTERN_{pattern}"
        )

        keys.append(
            f"H{h}|SPT_BOUNDARIES_{int(topology.get('boundary_count',0))}"
        )

        topology_class = str(
            topology.get(
                "topology_class",
                "UNRESOLVED_BOUNDARY_STATE",
            )
        )
        keys.append(
            f"H{h}|SPT_CLASS_{topology_class}"
        )

        topology_transition = str(
            topology.get(
                "topology_transition",
                "NONE",
            )
        )
        if topology_transition != "NONE":
            keys.append(
                f"H{h}|SPT_CLASS_TRANS_{topology_transition}"
            )

        for boundary in topology.get(
            "boundaries",
            [],
        ):
            b_life = str(boundary.get("lifecycle", "STABLE"))
            b_left = str(boundary.get("left_state", "FLAT"))
            b_right = str(boundary.get("right_state", "FLAT"))
            keys.append(
                f"H{h}|PBD_{b_left}_TO_{b_right}_{b_life}"
            )
            bv = float(boundary.get("velocity_log2_per_min", 0.0))
            if bv < -PHASE_BOUNDARY_MOVE_EPS:
                keys.append(f"H{h}|PBD_MOVE_MICRO")
            elif bv > PHASE_BOUNDARY_MOVE_EPS:
                keys.append(f"H{h}|PBD_MOVE_MACRO")

        primary_island = str(
            topology.get(
                "primary_island",
                "NONE",
            )
        )
        if primary_island != "NONE":
            keys.append(
                f"H{h}|SPT_PRIMARY_ISLAND_{primary_island}"
            )

        for domain in topology.get(
            "domains",
            [],
        ):
            direction = str(
                domain.get(
                    "direction",
                    "NONE",
                )
            )
            start_h = int(
                domain.get(
                    "start_h",
                    0,
                )
            )
            end_h = int(
                domain.get(
                    "end_h",
                    0,
                )
            )
            lifecycle = str(
                domain.get(
                    "lifecycle",
                    "STABLE",
                )
            )

            keys.append(
                f"H{h}|SPT_DOMAIN_{direction}_H{start_h}_H{end_h}"
            )
            keys.append(
                f"H{h}|SPT_LIFE_{direction}_{lifecycle}"
            )

            if bool(
                domain.get(
                    "island",
                    False,
                )
            ):
                keys.append(
                    f"H{h}|SPT_ISLAND_{direction}_H{start_h}_H{end_h}_{lifecycle}"
                )

            width_v = float(
                domain.get(
                    "width_velocity_oct_per_min",
                    0.0,
                )
            )
            if width_v > 1e-9:
                keys.append(
                    f"H{h}|SPT_WIDTH_EXPANDING_{direction}"
                )
            elif width_v < -1e-9:
                keys.append(
                    f"H{h}|SPT_WIDTH_CONTRACTING_{direction}"
                )

            center_v = float(
                domain.get(
                    "center_velocity_log2_per_min",
                    0.0,
                )
            )
            if center_v > 1e-9:
                keys.append(
                    f"H{h}|SPT_DRIFT_MACRO_{direction}"
                )
            elif center_v < -1e-9:
                keys.append(
                    f"H{h}|SPT_DRIFT_MICRO_{direction}"
                )

        for group in topology.get(
            "sync_groups",
            [],
        ):
            if bool(
                group.get(
                    "simultaneous",
                    False,
                )
            ):
                keys.append(
                    f"H{h}|SPT_SYNC_{group.get('signature','{}')}"
                )

    return sorted(
        set(keys)
    )


def create_geometry_outcome_probe(
    state,
    close_time_ms,
    horizon,
):
    if not GEOMETRY_OUTCOME_RESEARCH_ENABLED:
        return

    features = gol_geometry_features(
        state
    )

    if not features.get(
        "ready",
        False,
    ):
        return

    horizon = int(
        horizon
    )

    entry = float(
        state[
            "price"
        ]
    )

    probe = {
        "probe_id":
            f"GOL-{state['state_id']}-H{horizon}",
        "created_at":
            now_iso(),
        "state_id":
            state[
                "state_id"
            ],
        "horizon_candles":
            horizon,
        "entry_price":
            entry,
        "entry_close_time_ms":
            int(
                close_time_ms
            ),
        "due_close_time_ms":
            int(
                close_time_ms
            )
            + horizon
            * MINUTE_MS,
        "geometry":
            features,
        "feature_keys":
            gol_feature_keys(
                features,
                horizon,
            ),
        "transition_keys":
            tom_transition_keys(
                features,
                horizon,
            ),
        "transition_path":
            tom_path_text(
                features
            ),
        "edge_keys":
            teg_transition_keys(
                features,
                horizon,
            ),
        "edge_path":
            teg_edge_path_text(
                features.get(
                    "propagation_edge_steps",
                    features.get(
                        "propagation_steps",
                        [],
                    ),
                )
            ),
        "path_high":
            entry,
        "path_low":
            entry,
        "path_last_close":
            entry,
        "path_candles":
            0,
        "status":
            "FROZEN",
    }

    probe[
        "fingerprint"
    ] = fingerprint(
        probe
    )

    pending_geometry_outcome_probes.append(
        probe
    )

    append_jsonl(
        GEOMETRY_OUTCOME_STATES_FILE,
        {
            "probe_id":
                probe[
                    "probe_id"
                ],
            "created_at":
                probe[
                    "created_at"
                ],
            "state_id":
                probe[
                    "state_id"
                ],
            "horizon_candles":
                horizon,
            "entry_price":
                entry,
            "entry_close_time_ms":
                int(
                    close_time_ms
                ),
            "due_close_time_ms":
                probe[
                    "due_close_time_ms"
                ],
            "geometry":
                features,
            "feature_keys":
                probe[
                    "feature_keys"
                ],
            "transition_keys":
                probe[
                    "transition_keys"
                ],
            "transition_path":
                probe[
                    "transition_path"
                ],
            "edge_keys":
                probe[
                    "edge_keys"
                ],
            "edge_path":
                probe[
                    "edge_path"
                ],
            "probe_hash":
                probe[
                    "fingerprint"
                ],
            "status":
                "FROZEN",
        },
    )


def create_multi_horizon_geometry_outcome_probes(
    state,
    close_time_ms,
):
    for horizon in (
        GEOMETRY_OUTCOME_HORIZONS
    ):
        create_geometry_outcome_probe(
            state,
            close_time_ms,
            horizon,
        )



def tom_arrow(state):
    if state == "UP":
        return "UP"
    if state == "DOWN":
        return "DOWN"
    return "FLAT"


def tom_path_text(features):
    steps = list(
        features.get(
            "propagation_steps",
            [],
        )
    )

    if not steps:
        order = list(
            features.get(
                "propagation_order",
                [],
            )
        )
        direction = str(
            features.get(
                "propagation_direction",
                "NONE",
            )
        )
        steps = [
            {
                "horizon": int(h),
                "to_state": direction,
            }
            for h in order
        ]

    if not steps:
        return "NONE"

    return ">".join(
        f"H{int(s['horizon'])}_{tom_arrow(str(s.get('to_state','FLAT')))}"
        for s in steps
    )


def tom_transition_keys(
    features,
    horizon,
):
    """
    Hierarchical route cells:
    - family: propagation mode + direction
    - exact observed order
    - short prefixes (less sparse)
    - shock/front/band context
    - state-pattern context
    """
    h = int(horizon)
    keys = []

    mode = str(
        features.get(
            "propagation_mode",
            "NONE",
        )
    )
    direction = str(
        features.get(
            "propagation_direction",
            "NONE",
        )
    )
    path = tom_path_text(
        features
    )

    if mode != "NONE":
        keys.append(
            f"H{h}|TOM_FAMILY_{mode}_{direction}"
        )

    if path != "NONE":
        keys.append(
            f"H{h}|TOM_PATH_{path}"
        )

        parts = path.split(">")

        if len(parts) >= 2:
            keys.append(
                f"H{h}|TOM_PREFIX2_{'>'.join(parts[:2])}"
            )

        if len(parts) >= 3:
            keys.append(
                f"H{h}|TOM_PREFIX3_{'>'.join(parts[:3])}"
            )

    shock = features.get(
        "shock_horizon"
    )
    if shock is not None:
        keys.append(
            f"H{h}|TOM_SHOCK_H{int(shock)}_{direction}"
        )

    front = str(
        features.get(
            "deformation_front",
            "NONE",
        )
    )
    if front != "NONE":
        keys.append(
            f"H{h}|TOM_FRONT_{front}_{direction}"
        )

    band = str(
        features.get(
            "inversion_band",
            {},
        ).get(
            "signature",
            "NONE",
        )
    )
    if band != "NONE":
        keys.append(
            f"H{h}|TOM_BAND_{band}"
        )

    pattern = str(
        features.get(
            "state_pattern",
            "F-F-F-F-F",
        )
    )
    keys.append(
        f"H{h}|TOM_PATTERN_{pattern}"
    )

    if (
        mode != "NONE"
        or shock is not None
        or front != "NONE"
        or band != "NONE"
    ):
        keys.append(
            f"H{h}|TOM_ROUTE_"
            + "|".join(
                [
                    mode,
                    direction,
                    (
                        f"SHOCK_H{int(shock)}"
                        if shock is not None
                        else "NO_SHOCK"
                    ),
                    (
                        front
                        if front != "NONE"
                        else "NO_FRONT"
                    ),
                    (
                        band
                        if band != "NONE"
                        else "NO_BAND"
                    ),
                ]
            )
        )

    return sorted(
        set(
            keys
        )
    )



def teg_transition_keys(
    features,
    horizon,
):
    h = int(horizon)
    steps = list(
        features.get(
            "propagation_edge_steps",
            features.get(
                "propagation_steps",
                [],
            ),
        )
    )
    edges = list(
        features.get(
            "propagation_edges",
            [],
        )
    )
    hypergroups = list(
        features.get(
            "propagation_hypergroups",
            [],
        )
    )

    if not steps:
        return []

    keys = []

    node_path = teg_edge_path_text(
        steps
    )

    keys.append(
        f"H{h}|TEG_PATH_{node_path}"
    )

    node_parts = node_path.split(">")

    if len(node_parts) >= 2:
        keys.append(
            f"H{h}|TEG_PREFIX2_{'>'.join(node_parts[:2])}"
        )

    if len(node_parts) >= 3:
        keys.append(
            f"H{h}|TEG_PREFIX3_{'>'.join(node_parts[:3])}"
        )

    for edge in edges:
        token = (
            f"H{h}|TEG_EDGE_"
            f"{edge.get('from_node','NONE')}"
            f"__{edge.get('to_node','NONE')}"
            f"__{edge.get('scale_direction','NONE')}"
            f"__{edge.get('speed_bucket','SLOW')}"
        )
        keys.append(token)

    for group in hypergroups:
        if bool(
            group.get(
                "simultaneous",
                False,
            )
        ):
            keys.append(
                f"H{h}|TEG_SYNC_{group.get('signature','{}')}"
            )

    graph = dict(
        features.get(
            "transition_edge_graph",
            {},
        )
    )

    mean_speed = abs(
        float(
            graph.get(
                "mean_abs_scale_velocity_log2_per_min",
                0.0,
            )
        )
    )

    if mean_speed >= TRANSITION_EDGE_FAST_LOG2_PER_MIN:
        speed_class = "FAST"
    elif mean_speed >= TRANSITION_EDGE_SLOW_LOG2_PER_MIN:
        speed_class = "MED"
    else:
        speed_class = "SLOW"

    keys.append(
        f"H{h}|TEG_SPEED_{speed_class}"
    )

    mode = str(
        features.get(
            "propagation_mode",
            "NONE",
        )
    )
    direction = str(
        features.get(
            "propagation_direction",
            "NONE",
        )
    )

    keys.append(
        f"H{h}|TEG_FAMILY_{mode}_{direction}_{speed_class}"
    )

    return sorted(set(keys))


def teg_update_matrix_cell(
    key,
    horizon,
    path_tradeable,
    terminal_tradeable,
    best_mfe_net,
    terminal_best_net,
    max_up_pct,
    max_down_pct,
    winner,
):
    # Same outcome semantics as TOM1, but grouped by full transition edges.
    cell = transition_edge_matrix.setdefault(
        key,
        {
            "horizon_candles": int(horizon),
            "samples": 0,
            "path_tradeable": 0,
            "terminal_tradeable": 0,
            "up_path_wins": 0,
            "down_path_wins": 0,
            "flat_path_wins": 0,
            "best_mfe_net_sum_pct": 0.0,
            "terminal_best_net_sum_pct": 0.0,
            "max_up_sum_pct": 0.0,
            "max_down_abs_sum_pct": 0.0,
            "last_updated": None,
        },
    )

    cell["samples"] += 1

    if path_tradeable:
        cell["path_tradeable"] += 1
    if terminal_tradeable:
        cell["terminal_tradeable"] += 1

    if winner == "UP":
        cell["up_path_wins"] += 1
    elif winner == "DOWN":
        cell["down_path_wins"] += 1
    else:
        cell["flat_path_wins"] += 1

    cell["best_mfe_net_sum_pct"] += float(best_mfe_net)
    cell["terminal_best_net_sum_pct"] += float(terminal_best_net)
    cell["max_up_sum_pct"] += float(max_up_pct)
    cell["max_down_abs_sum_pct"] += abs(float(max_down_pct))
    cell["last_updated"] = now_iso()

    n = max(1, int(cell["samples"]))

    cell["path_tradeable_rate"] = round(
        cell["path_tradeable"] / n,
        6,
    )
    cell["terminal_tradeable_rate"] = round(
        cell["terminal_tradeable"] / n,
        6,
    )
    cell["up_win_rate"] = round(
        cell["up_path_wins"] / n,
        6,
    )
    cell["down_win_rate"] = round(
        cell["down_path_wins"] / n,
        6,
    )
    cell["flat_win_rate"] = round(
        cell["flat_path_wins"] / n,
        6,
    )
    cell["avg_best_mfe_net_pct"] = round(
        cell["best_mfe_net_sum_pct"] / n,
        6,
    )
    cell["avg_terminal_best_net_pct"] = round(
        cell["terminal_best_net_sum_pct"] / n,
        6,
    )
    cell["avg_max_up_pct"] = round(
        cell["max_up_sum_pct"] / n,
        6,
    )
    cell["avg_max_down_abs_pct"] = round(
        cell["max_down_abs_sum_pct"] / n,
        6,
    )


def transition_edge_leaderboard(
    limit=8,
):
    rows = []

    for key, cell in transition_edge_matrix.items():
        if int(cell.get("samples", 0)) < TRANSITION_EDGE_MIN_LEADER_SAMPLES:
            continue

        rows.append({
            "key": key,
            **cell,
        })

    rows.sort(
        key=lambda x: (
            float(x.get("avg_best_mfe_net_pct", 0.0)),
            float(x.get("path_tradeable_rate", 0.0)),
            int(x.get("samples", 0)),
        ),
        reverse=True,
    )

    return rows[:int(limit)]


def tom_update_matrix_cell(
    key,
    horizon,
    path_tradeable,
    terminal_tradeable,
    best_mfe_net,
    terminal_best_net,
    max_up_pct,
    max_down_pct,
    winner,
):
    cell = transition_outcome_matrix.setdefault(
        key,
        {
            "horizon_candles": int(horizon),
            "samples": 0,
            "path_tradeable": 0,
            "terminal_tradeable": 0,
            "up_path_wins": 0,
            "down_path_wins": 0,
            "flat_path_wins": 0,
            "best_mfe_net_sum_pct": 0.0,
            "terminal_best_net_sum_pct": 0.0,
            "max_up_sum_pct": 0.0,
            "max_down_abs_sum_pct": 0.0,
            "last_updated": None,
        },
    )

    cell["samples"] += 1

    if path_tradeable:
        cell["path_tradeable"] += 1
    if terminal_tradeable:
        cell["terminal_tradeable"] += 1

    if winner == "UP":
        cell["up_path_wins"] += 1
    elif winner == "DOWN":
        cell["down_path_wins"] += 1
    else:
        cell["flat_path_wins"] += 1

    cell["best_mfe_net_sum_pct"] += float(best_mfe_net)
    cell["terminal_best_net_sum_pct"] += float(terminal_best_net)
    cell["max_up_sum_pct"] += float(max_up_pct)
    cell["max_down_abs_sum_pct"] += abs(float(max_down_pct))
    cell["last_updated"] = now_iso()

    n = max(1, int(cell["samples"]))

    cell["path_tradeable_rate"] = round(
        cell["path_tradeable"] / n,
        6,
    )
    cell["terminal_tradeable_rate"] = round(
        cell["terminal_tradeable"] / n,
        6,
    )
    cell["up_win_rate"] = round(
        cell["up_path_wins"] / n,
        6,
    )
    cell["down_win_rate"] = round(
        cell["down_path_wins"] / n,
        6,
    )
    cell["flat_win_rate"] = round(
        cell["flat_path_wins"] / n,
        6,
    )
    cell["avg_best_mfe_net_pct"] = round(
        cell["best_mfe_net_sum_pct"] / n,
        6,
    )
    cell["avg_terminal_best_net_pct"] = round(
        cell["terminal_best_net_sum_pct"] / n,
        6,
    )
    cell["avg_max_up_pct"] = round(
        cell["max_up_sum_pct"] / n,
        6,
    )
    cell["avg_max_down_abs_pct"] = round(
        cell["max_down_abs_sum_pct"] / n,
        6,
    )


def transition_outcome_leaderboard(
    limit=8,
):
    rows = []

    for key, cell in transition_outcome_matrix.items():
        n = int(
            cell.get(
                "samples",
                0,
            )
        )

        if n < TRANSITION_OUTCOME_MIN_LEADER_SAMPLES:
            continue

        rows.append({
            "key": key,
            **cell,
        })

    rows.sort(
        key=lambda x: (
            float(x.get("avg_best_mfe_net_pct", 0.0)),
            float(x.get("path_tradeable_rate", 0.0)),
            int(x.get("samples", 0)),
        ),
        reverse=True,
    )

    return rows[:int(limit)]


def gol_update_matrix_cell(
    key,
    horizon,
    path_tradeable,
    terminal_tradeable,
    best_mfe_net,
    terminal_best_net,
    max_up_pct,
    max_down_pct,
    winner,
    gross_winner="FLAT",
    gross_best_excursion_pct=0.0,
    cost_coverage_ratio=0.0,
    path_asymmetry=0.0,
    gross_cost_covered=False,
    gross_near_cost=False,
):
    cell = geometry_outcome_matrix.setdefault(
        key,
        {
            "horizon_candles": int(horizon),
            "samples": 0,
            "path_tradeable": 0,
            "terminal_tradeable": 0,
            "up_path_wins": 0,
            "down_path_wins": 0,
            "flat_path_wins": 0,
            "best_mfe_net_sum_pct": 0.0,
            "terminal_best_net_sum_pct": 0.0,
            "max_up_sum_pct": 0.0,
            "max_down_abs_sum_pct": 0.0,
            "gross_up_wins": 0,
            "gross_down_wins": 0,
            "gross_flat_wins": 0,
            "gross_cost_covered": 0,
            "gross_near_cost": 0,
            "gross_best_excursion_sum_pct": 0.0,
            "cost_coverage_ratio_sum": 0.0,
            "path_asymmetry_sum": 0.0,
            "last_updated": None,
        },
    )

    # Migration-safe defaults for v1.13 matrix cells.
    for k, v in {
        "gross_up_wins": 0,
        "gross_down_wins": 0,
        "gross_flat_wins": 0,
        "gross_cost_covered": 0,
        "gross_near_cost": 0,
        "gross_best_excursion_sum_pct": 0.0,
        "cost_coverage_ratio_sum": 0.0,
        "path_asymmetry_sum": 0.0,
    }.items():
        cell.setdefault(k, v)

    cell["samples"] += 1
    if path_tradeable:
        cell["path_tradeable"] += 1
    if terminal_tradeable:
        cell["terminal_tradeable"] += 1

    if winner == "UP":
        cell["up_path_wins"] += 1
    elif winner == "DOWN":
        cell["down_path_wins"] += 1
    else:
        cell["flat_path_wins"] += 1

    if gross_winner == "UP":
        cell["gross_up_wins"] += 1
    elif gross_winner == "DOWN":
        cell["gross_down_wins"] += 1
    else:
        cell["gross_flat_wins"] += 1

    if gross_cost_covered:
        cell["gross_cost_covered"] += 1
    if gross_near_cost:
        cell["gross_near_cost"] += 1

    cell["best_mfe_net_sum_pct"] += float(best_mfe_net)
    cell["terminal_best_net_sum_pct"] += float(terminal_best_net)
    cell["max_up_sum_pct"] += float(max_up_pct)
    cell["max_down_abs_sum_pct"] += abs(float(max_down_pct))
    cell["gross_best_excursion_sum_pct"] += float(gross_best_excursion_pct)
    cell["cost_coverage_ratio_sum"] += float(cost_coverage_ratio)
    cell["path_asymmetry_sum"] += float(path_asymmetry)
    cell["last_updated"] = now_iso()

    n = max(1, int(cell["samples"]))
    cell["path_tradeable_rate"] = round(cell["path_tradeable"] / n, 6)
    cell["terminal_tradeable_rate"] = round(cell["terminal_tradeable"] / n, 6)
    cell["up_win_rate"] = round(cell["up_path_wins"] / n, 6)
    cell["down_win_rate"] = round(cell["down_path_wins"] / n, 6)
    cell["gross_up_win_rate"] = round(cell["gross_up_wins"] / n, 6)
    cell["gross_down_win_rate"] = round(cell["gross_down_wins"] / n, 6)
    cell["gross_cost_covered_rate"] = round(cell["gross_cost_covered"] / n, 6)
    cell["gross_near_cost_rate"] = round(cell["gross_near_cost"] / n, 6)
    cell["avg_best_mfe_net_pct"] = round(cell["best_mfe_net_sum_pct"] / n, 6)
    cell["avg_terminal_best_net_pct"] = round(cell["terminal_best_net_sum_pct"] / n, 6)
    cell["avg_max_up_pct"] = round(cell["max_up_sum_pct"] / n, 6)
    cell["avg_max_down_abs_pct"] = round(cell["max_down_abs_sum_pct"] / n, 6)
    cell["avg_gross_best_excursion_pct"] = round(cell["gross_best_excursion_sum_pct"] / n, 6)
    cell["avg_cost_coverage_ratio"] = round(cell["cost_coverage_ratio_sum"] / n, 6)
    cell["avg_path_asymmetry"] = round(cell["path_asymmetry_sum"] / n, 6)


def resolve_geometry_outcome_probe(
    probe,
    observed_close_time_ms,
):
    entry = float(
        probe[
            "entry_price"
        ]
    )

    exit_price = float(
        probe.get(
            "path_last_close",
            entry,
        )
    )

    path_high = max(
        entry,
        float(
            probe.get(
                "path_high",
                entry,
            )
        ),
    )

    path_low = min(
        entry,
        float(
            probe.get(
                "path_low",
                entry,
            )
        ),
    )

    close_return = pct_return(
        entry,
        exit_price,
    )

    max_up_pct = pct_return(
        entry,
        path_high,
    )

    max_down_pct = pct_return(
        entry,
        path_low,
    )

    # Hindsight opportunity labels: did the future path contain enough
    # movement to overcome simulated round-trip costs?
    buy_mfe_net = (
        simulate_buy_roundtrip_return_pct(
            entry,
            path_high,
        )
    )

    sell_mfe_net = (
        simulate_sell_owned_roundtrip_return_pct(
            entry,
            path_low,
        )
    )

    terminal_buy_net = (
        simulate_buy_roundtrip_return_pct(
            entry,
            exit_price,
        )
    )

    terminal_sell_net = (
        simulate_sell_owned_roundtrip_return_pct(
            entry,
            exit_price,
        )
    )

    best_mfe_net = max(
        0.0,
        buy_mfe_net,
        sell_mfe_net,
    )

    terminal_best_net = max(
        0.0,
        terminal_buy_net,
        terminal_sell_net,
    )

    path_tradeable = (
        best_mfe_net > 0.0
    )

    terminal_tradeable = (
        terminal_best_net > 0.0
    )

    # GOL2 latent motion: learn geometry even when fees/slippage make
    # the path economically untradeable. This never changes execution.
    gross_up_excursion_pct = max(0.0, float(max_up_pct))
    gross_down_excursion_pct = max(0.0, abs(float(max_down_pct)))
    gross_best_excursion_pct = max(
        gross_up_excursion_pct,
        gross_down_excursion_pct,
    )

    if gross_best_excursion_pct <= GOL2_GROSS_FLAT_EPS_PCT:
        gross_winner = "FLAT"
    elif gross_up_excursion_pct > gross_down_excursion_pct:
        gross_winner = "UP"
    elif gross_down_excursion_pct > gross_up_excursion_pct:
        gross_winner = "DOWN"
    else:
        gross_winner = "FLAT"

    path_asymmetry = (
        (gross_up_excursion_pct - gross_down_excursion_pct)
        / max(
            GOL2_GROSS_FLAT_EPS_PCT,
            gross_up_excursion_pct + gross_down_excursion_pct,
        )
    )

    buy_cost_floor_pct = max(
        0.0,
        -simulate_buy_roundtrip_return_pct(entry, entry),
    )
    sell_cost_floor_pct = max(
        0.0,
        -simulate_sell_owned_roundtrip_return_pct(entry, entry),
    )
    cost_floor_pct = max(
        GOL2_GROSS_FLAT_EPS_PCT,
        min(buy_cost_floor_pct, sell_cost_floor_pct),
    )
    cost_coverage_ratio = gross_best_excursion_pct / cost_floor_pct
    gross_cost_covered = cost_coverage_ratio >= 1.0
    gross_near_cost = cost_coverage_ratio >= GOL2_COST_COVERAGE_NEAR

    if path_tradeable:
        latent_move_class = "TRADEABLE"
    elif gross_cost_covered:
        latent_move_class = "COST_REACHED_BUT_NET_NEGATIVE"
    elif gross_near_cost:
        latent_move_class = "NEAR_COST"
    elif gross_best_excursion_pct > GOL2_GROSS_FLAT_EPS_PCT:
        latent_move_class = "SUBCOST"
    else:
        latent_move_class = "NO_MOVE"

    if (
        best_mfe_net <= 0.0
    ):
        winner = "FLAT"
    elif (
        buy_mfe_net
        > sell_mfe_net
    ):
        winner = "UP"
    elif (
        sell_mfe_net
        > buy_mfe_net
    ):
        winner = "DOWN"
    else:
        winner = "FLAT"

    hkey = str(
        int(
            probe[
                "horizon_candles"
            ]
        )
    )

    metric = (
        geometry_outcome_metrics[
            hkey
        ]
    )

    metric[
        "samples"
    ] += 1

    if path_tradeable:
        metric[
            "path_tradeable"
        ] += 1

    if terminal_tradeable:
        metric[
            "terminal_tradeable"
        ] += 1

    if winner == "UP":
        metric[
            "up_path_wins"
        ] += 1
    elif winner == "DOWN":
        metric[
            "down_path_wins"
        ] += 1
    else:
        metric[
            "flat_path_wins"
        ] += 1

    metric[
        "max_up_sum_pct"
    ] += max_up_pct

    metric[
        "max_down_abs_sum_pct"
    ] += abs(
        max_down_pct
    )

    metric[
        "buy_mfe_net_sum_pct"
    ] += buy_mfe_net

    metric[
        "sell_mfe_net_sum_pct"
    ] += sell_mfe_net

    metric[
        "best_mfe_net_sum_pct"
    ] += best_mfe_net

    metric[
        "terminal_best_net_sum_pct"
    ] += terminal_best_net

    # Migration-safe GOL2 aggregate fields.
    for k, v in {
        "gross_up_wins": 0,
        "gross_down_wins": 0,
        "gross_flat_wins": 0,
        "gross_cost_covered": 0,
        "gross_near_cost": 0,
        "gross_best_excursion_sum_pct": 0.0,
        "cost_coverage_ratio_sum": 0.0,
        "path_asymmetry_sum": 0.0,
    }.items():
        metric.setdefault(k, v)

    if gross_winner == "UP":
        metric["gross_up_wins"] += 1
    elif gross_winner == "DOWN":
        metric["gross_down_wins"] += 1
    else:
        metric["gross_flat_wins"] += 1

    if gross_cost_covered:
        metric["gross_cost_covered"] += 1
    if gross_near_cost:
        metric["gross_near_cost"] += 1

    metric["gross_best_excursion_sum_pct"] += gross_best_excursion_pct
    metric["cost_coverage_ratio_sum"] += cost_coverage_ratio
    metric["path_asymmetry_sum"] += path_asymmetry

    n = max(
        1,
        metric[
            "samples"
        ],
    )

    metric[
        "path_tradeable_rate"
    ] = round(
        metric[
            "path_tradeable"
        ] / n,
        6,
    )

    metric[
        "terminal_tradeable_rate"
    ] = round(
        metric[
            "terminal_tradeable"
        ] / n,
        6,
    )

    metric[
        "up_win_rate"
    ] = round(
        metric[
            "up_path_wins"
        ] / n,
        6,
    )

    metric[
        "down_win_rate"
    ] = round(
        metric[
            "down_path_wins"
        ] / n,
        6,
    )

    metric[
        "avg_max_up_pct"
    ] = round(
        metric[
            "max_up_sum_pct"
        ] / n,
        6,
    )

    metric[
        "avg_max_down_abs_pct"
    ] = round(
        metric[
            "max_down_abs_sum_pct"
        ] / n,
        6,
    )

    metric[
        "avg_best_mfe_net_pct"
    ] = round(
        metric[
            "best_mfe_net_sum_pct"
        ] / n,
        6,
    )

    metric[
        "avg_terminal_best_net_pct"
    ] = round(
        metric[
            "terminal_best_net_sum_pct"
        ] / n,
        6,
    )

    metric["gross_up_win_rate"] = round(metric["gross_up_wins"] / n, 6)
    metric["gross_down_win_rate"] = round(metric["gross_down_wins"] / n, 6)
    metric["gross_cost_covered_rate"] = round(metric["gross_cost_covered"] / n, 6)
    metric["gross_near_cost_rate"] = round(metric["gross_near_cost"] / n, 6)
    metric["avg_gross_best_excursion_pct"] = round(metric["gross_best_excursion_sum_pct"] / n, 6)
    metric["avg_cost_coverage_ratio"] = round(metric["cost_coverage_ratio_sum"] / n, 6)
    metric["avg_path_asymmetry"] = round(metric["path_asymmetry_sum"] / n, 6)

    for key in probe.get(
        "feature_keys",
        [],
    ):
        gol_update_matrix_cell(
            key,
            probe[
                "horizon_candles"
            ],
            path_tradeable,
            terminal_tradeable,
            best_mfe_net,
            terminal_best_net,
            max_up_pct,
            max_down_pct,
            winner,
            gross_winner=gross_winner,
            gross_best_excursion_pct=gross_best_excursion_pct,
            cost_coverage_ratio=cost_coverage_ratio,
            path_asymmetry=path_asymmetry,
            gross_cost_covered=gross_cost_covered,
            gross_near_cost=gross_near_cost,
        )

    # Migration-safe: old pending GOL probes from v1.10 do not contain
    # transition_keys, so derive them from the frozen geometry.
    transition_keys = probe.get(
        "transition_keys"
    )

    if not isinstance(
        transition_keys,
        list,
    ):
        transition_keys = tom_transition_keys(
            probe[
                "geometry"
            ],
            probe[
                "horizon_candles"
            ],
        )

    for key in transition_keys:
        tom_update_matrix_cell(
            key,
            probe[
                "horizon_candles"
            ],
            path_tradeable,
            terminal_tradeable,
            best_mfe_net,
            terminal_best_net,
            max_up_pct,
            max_down_pct,
            winner,
        )

    transition_path = probe.get(
        "transition_path"
    ) or tom_path_text(
        probe[
            "geometry"
        ]
    )

    # Migration-safe for v1.11 pending probes.
    edge_keys = probe.get(
        "edge_keys"
    )
    if not isinstance(
        edge_keys,
        list,
    ):
        edge_keys = teg_transition_keys(
            probe["geometry"],
            probe["horizon_candles"],
        )

    for key in edge_keys:
        teg_update_matrix_cell(
            key,
            probe["horizon_candles"],
            path_tradeable,
            terminal_tradeable,
            best_mfe_net,
            terminal_best_net,
            max_up_pct,
            max_down_pct,
            winner,
        )

    edge_path = probe.get(
        "edge_path"
    ) or teg_edge_path_text(
        probe["geometry"].get(
            "propagation_edge_steps",
            probe["geometry"].get(
                "propagation_steps",
                [],
            ),
        )
    )

    fact = {
        "probe_id":
            probe[
                "probe_id"
            ],
        "observed_at":
            now_iso(),
        "observed_close_time_ms":
            int(
                observed_close_time_ms
            ),
        "state_id":
            probe[
                "state_id"
            ],
        "horizon_candles":
            int(
                probe[
                    "horizon_candles"
                ]
            ),
        "entry_price":
            round(
                entry,
                8,
            ),
        "exit_price":
            round(
                exit_price,
                8,
            ),
        "path_high":
            round(
                path_high,
                8,
            ),
        "path_low":
            round(
                path_low,
                8,
            ),
        "path_candles":
            int(
                probe.get(
                    "path_candles",
                    0,
                )
            ),
        "terminal_return_pct":
            round(
                close_return,
                6,
            ),
        "max_up_pct":
            round(
                max_up_pct,
                6,
            ),
        "max_down_pct":
            round(
                max_down_pct,
                6,
            ),
        "buy_mfe_net_pct":
            round(
                buy_mfe_net,
                6,
            ),
        "sell_mfe_net_pct":
            round(
                sell_mfe_net,
                6,
            ),
        "best_mfe_net_pct":
            round(
                best_mfe_net,
                6,
            ),
        "terminal_buy_net_pct":
            round(
                terminal_buy_net,
                6,
            ),
        "terminal_sell_net_pct":
            round(
                terminal_sell_net,
                6,
            ),
        "terminal_best_net_pct":
            round(
                terminal_best_net,
                6,
            ),
        "path_tradeable":
            path_tradeable,
        "terminal_tradeable":
            terminal_tradeable,
        "path_winner":
            winner,
        "gross_path_winner":
            gross_winner,
        "gross_best_excursion_pct":
            round(gross_best_excursion_pct, 6),
        "cost_floor_pct":
            round(cost_floor_pct, 6),
        "cost_coverage_ratio":
            round(cost_coverage_ratio, 6),
        "path_asymmetry":
            round(path_asymmetry, 6),
        "latent_move_class":
            latent_move_class,
        "geometry":
            probe[
                "geometry"
            ],
        "feature_keys":
            probe.get(
                "feature_keys",
                [],
            ),
        "transition_keys":
            transition_keys,
        "transition_path":
            transition_path,
        "edge_keys":
            edge_keys,
        "edge_path":
            edge_path,
        "probe_hash":
            probe[
                "fingerprint"
            ],
        "research_only":
            True,
        "status":
            "OBSERVED",
    }

    append_jsonl(
        GEOMETRY_OUTCOME_FACTS_FILE,
        fact,
    )

    print()
    print(
        "=== GEOMETRIC OUTCOME FACT",
        f"H={probe['horizon_candles']}",
        "===",
    )

    print(
        probe[
            "probe_id"
        ],
        "|",
        f"close={close_return:+.5f}%",
        "|",
        f"up={max_up_pct:+.5f}%",
        "|",
        f"down={max_down_pct:+.5f}%",
    )

    print(
        "MFE NET:",
        f"BUY={buy_mfe_net:+.5f}%",
        f"SELL={sell_mfe_net:+.5f}%",
        f"BEST={best_mfe_net:+.5f}%",
        "|",
        "winner=" + winner,
    )

    print(
        "GOL2 LATENT:",
        f"gross={gross_best_excursion_pct:+.5f}%",
        f"dir={gross_winner}",
        f"costCov={cost_coverage_ratio:.2f}x",
        f"asym={path_asymmetry:+.2f}",
        f"class={latent_move_class}",
    )

    g = probe[
        "geometry"
    ]

    print(
        "GEO:",
        f"macro={g['macro_direction']} {g['macro_tilt_deg']:+.1f}°",
        f"micro={g['micro_direction']} {g['micro_tilt_deg']:+.1f}°",
        f"rel={g['scale_relation']}",
        f"zone={g['zone_context']}",
        f"relv={g['zone_relevance']}",
        f"Pz={g['zone_pressure']:.2f}",
    )

    print(
        "TOM1:",
        f"path={transition_path}",
        f"cells={len(transition_keys)}",
        f"band={g.get('inversion_band',{}).get('signature','NONE')}",
    )

    graph = dict(
        g.get(
            "transition_edge_graph",
            {},
        )
    )

    print(
        "TEG1:",
        f"path={edge_path}",
        f"cells={len(edge_keys)}",
        f"vscale={graph.get('net_scale_velocity_log2_per_min',0.0):+.3f} log2H/min",
    )

    topology = dict(g.get("phase_topology", {}))
    print(
        "SPT2/PBD1:",
        f"class={topology.get('topology_class','UNRESOLVED_BOUNDARY_STATE')}",
        f"pattern={topology.get('state_pattern','F-F-F-F-F')}",
        f"domains={topology.get('domain_count',0)}",
        f"boundaries={topology.get('boundary_count',0)}",
        f"island={topology.get('primary_island','NONE')}",
    )

    print(
        "CTD:",
        f"rotE={g.get('rotation_energy_deg_per_min',0.0):.2f}deg/m",
        f"coh={g.get('rotation_coherence',0.0):.2f}",
        (
            f"shock=H{g.get('shock_horizon')}"
            if g.get("shock_horizon") is not None
            else "shock=none"
        ),
        f"front={g.get('deformation_front','NONE')}",
        f"prop={g.get('propagation_mode','NONE')}/{g.get('propagation_direction','NONE')}",
    )

    print(
        "================================",
    )


def evaluate_geometry_outcome_due(
    candle,
):
    global pending_geometry_outcome_probes

    current_close_time_ms = int(
        candle[
            "close_time_ms"
        ]
    )

    remaining = []

    for probe in (
        pending_geometry_outcome_probes
    ):
        entry_time = int(
            probe[
                "entry_close_time_ms"
            ]
        )

        if (
            current_close_time_ms
            > entry_time
            and current_close_time_ms
            <= int(
                probe[
                    "due_close_time_ms"
                ]
            )
        ):
            probe[
                "path_high"
            ] = max(
                float(
                    probe.get(
                        "path_high",
                        probe[
                            "entry_price"
                        ],
                    )
                ),
                float(
                    candle[
                        "high"
                    ]
                ),
            )

            probe[
                "path_low"
            ] = min(
                float(
                    probe.get(
                        "path_low",
                        probe[
                            "entry_price"
                        ],
                    )
                ),
                float(
                    candle[
                        "low"
                    ]
                ),
            )

            probe[
                "path_last_close"
            ] = float(
                candle[
                    "close"
                ]
            )

            probe[
                "path_candles"
            ] = int(
                probe.get(
                    "path_candles",
                    0,
                )
            ) + 1

        if (
            current_close_time_ms
            >= int(
                probe[
                    "due_close_time_ms"
                ]
            )
        ):
            resolve_geometry_outcome_probe(
                probe,
                current_close_time_ms,
            )
        else:
            remaining.append(
                probe
            )

    pending_geometry_outcome_probes = (
        remaining
    )


def restore_geometry_outcome_overdue():
    """
    Rebuild the missing future path from REST after a restart.
    Pending horizons are <=60m, so one compact REST window is enough
    for ordinary downtime and avoids one request per probe.
    """
    global pending_geometry_outcome_probes

    if not pending_geometry_outcome_probes:
        return

    now_ms = int(
        datetime.now(
            timezone.utc
        ).timestamp()
        * 1000
    )

    overdue = [
        p
        for p in pending_geometry_outcome_probes
        if int(
            p[
                "due_close_time_ms"
            ]
        )
        <= now_ms
    ]

    if not overdue:
        return

    earliest = min(
        int(
            p[
                "entry_close_time_ms"
            ]
        )
        for p in overdue
    )

    latest = max(
        int(
            p[
                "due_close_time_ms"
            ]
        )
        for p in overdue
    )

    span_minutes = int(
        math.ceil(
            (
                latest
                - earliest
            )
            / MINUTE_MS
        )
    ) + 5

    data = rest_klines(
        limit=min(
            1000,
            max(
                10,
                span_minutes,
            ),
        ),
        start_time=max(
            0,
            earliest
            - MINUTE_MS,
        ),
    )

    recovered = [
        {
            "high":
                float(k[2]),
            "low":
                float(k[3]),
            "close":
                float(k[4]),
            "close_time_ms":
                int(k[6]),
        }
        for k in data
    ]

    remaining = []

    for probe in (
        pending_geometry_outcome_probes
    ):
        due = int(
            probe[
                "due_close_time_ms"
            ]
        )

        if due > now_ms:
            remaining.append(
                probe
            )
            continue

        entry_time = int(
            probe[
                "entry_close_time_ms"
            ]
        )

        path = [
            c
            for c in recovered
            if (
                c[
                    "close_time_ms"
                ]
                > entry_time
                and c[
                    "close_time_ms"
                ]
                <= due
            )
        ]

        if not path:
            remaining.append(
                probe
            )
            continue

        probe[
            "path_high"
        ] = max(
            float(
                probe.get(
                    "path_high",
                    probe[
                        "entry_price"
                    ],
                )
            ),
            max(
                c[
                    "high"
                ]
                for c in path
            ),
        )

        probe[
            "path_low"
        ] = min(
            float(
                probe.get(
                    "path_low",
                    probe[
                        "entry_price"
                    ],
                )
            ),
            min(
                c[
                    "low"
                ]
                for c in path
            ),
        )

        probe[
            "path_last_close"
        ] = path[-1][
            "close"
        ]

        probe[
            "path_candles"
        ] = max(
            int(
                probe.get(
                    "path_candles",
                    0,
                )
            ),
            len(path),
        )

        resolve_geometry_outcome_probe(
            probe,
            path[-1][
                "close_time_ms"
            ],
        )

    pending_geometry_outcome_probes = (
        remaining
    )


def geometry_outcome_leaderboard(
    limit=8,
):
    rows = []

    for key, cell in (
        geometry_outcome_matrix.items()
    ):
        n = int(
            cell.get(
                "samples",
                0,
            )
        )

        if (
            n
            < GEOMETRY_OUTCOME_MIN_LEADER_SAMPLES
        ):
            continue

        rows.append(
            {
                "key":
                    key,
                **cell,
            }
        )

    rows.sort(
        key=lambda x: (
            float(x.get("avg_cost_coverage_ratio", 0.0)),
            float(x.get("avg_gross_best_excursion_pct", 0.0)),
            float(x.get("path_tradeable_rate", 0.0)),
            int(x.get("samples", 0)),
        ),
        reverse=True,
    )

    return rows[
        :int(limit)
    ]


def print_geometry_outcome_summary():
    print()
    print(
        "=== GOL2 GEOMETRIC OUTCOME SUMMARY ==="
    )

    for horizon in (
        GEOMETRY_OUTCOME_HORIZONS
    ):
        m = geometry_outcome_metrics[
            str(horizon)
        ]

        n = int(
            m.get(
                "samples",
                0,
            )
        )

        print(
            f"H{horizon}:",
            f"n={n}",
            f"path={m.get('path_tradeable_rate', 0)*100:.1f}%",
            f"terminal={m.get('terminal_tradeable_rate', 0)*100:.1f}%",
            f"up={m.get('up_win_rate', 0)*100:.1f}%",
            f"down={m.get('down_win_rate', 0)*100:.1f}%",
            f"MFE_net={m.get('avg_best_mfe_net_pct', 0):+.5f}%",
            f"gross={m.get('avg_gross_best_excursion_pct', 0):.5f}%",
            f"costCov={m.get('avg_cost_coverage_ratio', 0):.2f}x",
            f"grossUP={m.get('gross_up_win_rate', 0)*100:.1f}%",
            f"grossDN={m.get('gross_down_win_rate', 0)*100:.1f}%",
        )

    leaders = geometry_outcome_leaderboard(
        limit=5
    )

    if leaders:
        print(
            "geometry cells with highest observed future-path edge:"
        )

        for i, cell in enumerate(
            leaders,
            1,
        ):
            print(
                f"{i}.",
                cell[
                    "key"
                ],
                f"n={cell.get('samples', 0)}",
                f"path={cell.get('path_tradeable_rate', 0)*100:.1f}%",
                f"MFE_net={cell.get('avg_best_mfe_net_pct', 0):+.5f}%",
                f"gross={cell.get('avg_gross_best_excursion_pct', 0):.5f}%",
                f"costCov={cell.get('avg_cost_coverage_ratio', 0):.2f}x",
                f"asym={cell.get('avg_path_asymmetry', 0):+.2f}",
            )
    else:
        print(
            "validated geometry cells:",
            "NONE YET",
        )

    print(
        "RESEARCH ONLY — GOL2 latent labels do not execute trades."
    )
    print(
        "======================================",
    )





def phase_topology_dashboard_html(
    cone_dynamics,
):
    topology = dict(
        cone_dynamics.get(
            "phase_topology",
            {},
        )
    )

    if not topology.get("ready", False):
        return (
            '<section class="card">'
            '<h2>SPT2 · SCALE-SPACE PHASE TOPOLOGY + PBD1</h2>'
            '<div class="sub">Topology warmup.</div>'
            '</section>'
        )

    domains = list(
        topology.get(
            "domains",
            [],
        )
    )
    boundaries = list(
        topology.get(
            "boundaries",
            [],
        )
    )
    sync_groups = [
        g
        for g in topology.get(
            "sync_groups",
            [],
        )
        if bool(
            g.get(
                "simultaneous",
                False,
            )
        )
    ]

    if domains:
        domain_rows = "".join(
            f"""
            <div class="spt-domain">
              <b>{geo_escape_html(str(d.get('domain_id','')))}</b>
              <span>{geo_escape_html(str(d.get('direction','')))} H{int(d.get('start_h',0))}→H{int(d.get('end_h',0))}</span>
              <span>W {float(d.get('width_octaves',0)):.2f} oct</span>
              <span>{geo_escape_html(str(d.get('lifecycle','STABLE')))}</span>
              <span>vC {float(d.get('center_velocity_log2_per_min',0)):+.2f}</span>
              <span>vW {float(d.get('width_velocity_oct_per_min',0)):+.2f}</span>
            </div>
            """
            for d in domains
        )
    else:
        domain_rows = (
            '<div class="sub">'
            'No UP/DOWN phase-domain.'
            '</div>'
        )

    if boundaries:
        boundary_rows = "".join(
            f"""
            <div class="spt-boundary">
              <b>{geo_escape_html(str(b.get('boundary_id','B?')))} · H{int(b.get('left_h',0))}/H{int(b.get('right_h',0))}</b>
              <span>{geo_escape_html(str(b.get('left_state','')))} | {geo_escape_html(str(b.get('right_state','')))}</span>
              <span>{geo_escape_html(str(b.get('lifecycle','STABLE')))}</span>
              <span>v {float(b.get('velocity_log2_per_min',0)):+.2f}</span>
              <span>a {float(b.get('acceleration_log2_per_min2',0)):+.2f}</span>
            </div>
            """
            for b in boundaries
        )
    else:
        boundary_rows = (
            '<div class="sub">'
            'No internal phase boundary.'
            '</div>'
        )

    events = list(
        topology.get(
            "domain_events",
            [],
        )
    )

    if events:
        event_rows = "".join(
            f"""
            <div class="spt-event">
              <b>{geo_escape_html(str(e.get('domain_id','')))}</b>
              <span>{geo_escape_html(str(e.get('event','')))}</span>
              <span>{geo_escape_html(str(e.get('direction','')))} H{int(e.get('start_h',0))}→H{int(e.get('end_h',0))}</span>
            </div>
            """
            for e in events
        )
    else:
        event_rows = (
            '<div class="sub">'
            'No domain lifecycle transition this state.'
            '</div>'
        )

    if sync_groups:
        sync_rows = "".join(
            f"""
            <div class="spt-sync">
              <b>SYNC ×{int(g.get('size',0))}</b>
              <span>{geo_escape_html(str(g.get('signature','{}')))}</span>
            </div>
            """
            for g in sync_groups
        )
    else:
        sync_rows = (
            '<div class="sub">'
            'No simultaneous transition hyperevent this state.'
            '</div>'
        )

    return (
        '<section class="card">'
        '<h2>SPT2 · SCALE-SPACE PHASE TOPOLOGY + PBD1</h2>'
        '<div class="model-note">'
        'Contiguous UP/DOWN scales are phase-domains. '
        'SPT2 tracks domain birth, expansion, contraction, drift, takeover '
        'and collapse. Same-timestamp transitions remain synchronous '
        'hyperevents instead of invented order. Research only.'
        '</div>'
        '<div class="ctd-summary">'
        f'<div class="ctd-kpi"><b>PATTERN</b><br>{geo_escape_html(str(topology.get("state_pattern","F-F-F-F-F")))}</div>'
        f'<div class="ctd-kpi"><b>TOPOLOGY CLASS</b><br>{geo_escape_html(str(topology.get("topology_class","UNRESOLVED_BOUNDARY_STATE")))}</div>'
        f'<div class="ctd-kpi"><b>CLASS TRANSITION</b><br>{geo_escape_html(str(topology.get("topology_transition","NONE")))}</div>'
        f'<div class="ctd-kpi"><b>DOMAINS</b><br>{int(topology.get("domain_count",0))}</div>'
        f'<div class="ctd-kpi"><b>BOUNDARIES</b><br>{int(topology.get("boundary_count",0))}</div>'
        f'<div class="ctd-kpi"><b>PRIMARY ISLAND</b><br>{geo_escape_html(str(topology.get("primary_island","NONE")))}</div>'
        '</div>'
        '<div class="gol-leaders-title">PHASE DOMAINS</div>'
        + domain_rows
        + '<div class="gol-leaders-title">PHASE BOUNDARIES</div>'
        + boundary_rows
        + '<div class="gol-leaders-title">DOMAIN LIFECYCLE EVENTS</div>'
        + event_rows
        + '<div class="gol-leaders-title">SYNCHRONOUS HYPEREVENTS</div>'
        + sync_rows
        + '</section>'
    )


def transition_edge_dashboard_html(
    cone_dynamics,
):
    leaders = transition_edge_leaderboard(
        limit=8
    )

    prop = cone_dynamics.get(
        "propagation",
        {},
    )

    steps = list(
        prop.get(
            "steps",
            [],
        )
    )
    edges = list(
        prop.get(
            "edges",
            [],
        )
    )
    graph = dict(
        prop.get(
            "graph",
            {},
        )
    )

    if steps:
        node_rows = "".join(
            f"""
            <div class="teg-node">
              <b>H{int(s.get('horizon',0))}</b>
              <span>{geo_escape_html(str(s.get('from_state','FLAT')))} → {geo_escape_html(str(s.get('to_state','FLAT')))}</span>
              <span>ω {float(s.get('omega_deg_per_min',0)):+.1f}°/m</span>
              <span>α {float(s.get('alpha_deg_per_min2',0)):+.1f}°/m²</span>
              <span>dwell {('n/a' if s.get('dwell_minutes') is None else f"{float(s.get('dwell_minutes')):.1f}m")}</span>
            </div>
            """
            for s in steps
        )
    else:
        node_rows = '<div class="sub">No transition nodes in the current propagation window.</div>'

    if edges:
        edge_rows = "".join(
            f"""
            <div class="teg-edge">
              <b>{geo_escape_html(str(e.get('from_node','')))}</b>
              <span>→</span>
              <b>{geo_escape_html(str(e.get('to_node','')))}</b>
              <span>Δt {float(e.get('dt_minutes',0)):.1f}m</span>
              <span>v {float(e.get('scale_velocity_log2_per_min',0)):+.2f}</span>
              <span>{geo_escape_html(str(e.get('speed_bucket','SLOW')))}</span>
            </div>
            """
            for e in edges
        )
    else:
        edge_rows = '<div class="sub">Need at least two transition nodes to form an edge.</div>'

    if leaders:
        leader_rows = "".join(
            f"""
            <div class="teg-leader">
              <b>{geo_escape_html(str(x['key']))}</b>
              <span>n {int(x.get('samples',0))}</span>
              <span>path {float(x.get('path_tradeable_rate',0))*100:.1f}%</span>
              <span>↑ {float(x.get('up_win_rate',0))*100:.1f}%</span>
              <span>↓ {float(x.get('down_win_rate',0))*100:.1f}%</span>
              <span>MFE {float(x.get('avg_best_mfe_net_pct',0)):+.4f}%</span>
            </div>
            """
            for x in leaders
        )
    else:
        leader_rows = (
            '<div class="sub">'
            f'Collecting edge outcomes. Validation threshold: {TRANSITION_EDGE_MIN_LEADER_SAMPLES} mature samples.'
            '</div>'
        )

    return (
        '<section class="card cone-card">'
        '<h2>TEG1 · TRANSITION EDGE GRAPH</h2>'
        '<div class="model-note">'
        'Node = a full scale-state transition (FROM → TO). '
        'Edge = elapsed time + signed movement through log-horizon scale-space. '
        'This preserves UP→FLAT vs DOWN→FLAT as different causal histories. Research only.'
        '</div>'
        '<div class="ctd-summary">'
        f'<div class="ctd-kpi"><b>NODE PATH</b><br>{geo_escape_html(str(graph.get("node_path","NONE")))}</div>'
        f'<div class="ctd-kpi"><b>EDGES</b><br>{int(graph.get("edge_count",0))}</div>'
        f'<div class="ctd-kpi"><b>MEAN |Vscale|</b><br>{float(graph.get("mean_abs_scale_velocity_log2_per_min",0)):.3f}</div>'
        f'<div class="ctd-kpi"><b>NET Vscale</b><br>{float(graph.get("net_scale_velocity_log2_per_min",0)):+.3f}</div>'
        '</div>'
        '<div class="gol-leaders-title">CURRENT NODES</div>'
        + node_rows
        + '<div class="gol-leaders-title">CURRENT EDGES</div>'
        + edge_rows
        + '<div class="gol-leaders-title">VALIDATED EDGE ROUTES</div>'
        + leader_rows
        + f'<div class="tom-kpi"><b>EDGE CELLS</b><br>{len(transition_edge_matrix)}</div>'
        + '</section>'
    )


def transition_outcome_dashboard_html():
    leaders = transition_outcome_leaderboard(
        limit=8
    )

    if leaders:
        rows = "".join(
            f"""
            <div class="tom-leader">
              <b>{geo_escape_html(str(x['key']))}</b>
              <span>n {int(x.get('samples', 0))}</span>
              <span>path {float(x.get('path_tradeable_rate', 0))*100:.1f}%</span>
              <span>↑ {float(x.get('up_win_rate', 0))*100:.1f}%</span>
              <span>↓ {float(x.get('down_win_rate', 0))*100:.1f}%</span>
              <span>MFE {float(x.get('avg_best_mfe_net_pct', 0)):+.4f}%</span>
            </div>
            """
            for x in leaders
        )
    else:
        rows = (
            '<div class="sub">'
            'Collecting deformation routes. A route becomes a leader after '
            f'{TRANSITION_OUTCOME_MIN_LEADER_SAMPLES} mature outcomes.'
            '</div>'
        )

    return (
        '<section class="card">'
        '<h2>TOM1 · TRANSITION OUTCOME MATRIX</h2>'
        '<div class="model-note">'
        'Learns the outcome of temporal deformation routes, not only static '
        'cone shape. Example: H5_UP → H15_UP → H30_UP is learned separately '
        'from H30_DOWN → H15_DOWN → H5_DOWN. Research only.'
        '</div>'
        f'<div class="tom-kpi"><b>ROUTE CELLS</b><br>{len(transition_outcome_matrix)}</div>'
        '<div class="gol-leaders-title">VALIDATED ROUTES</div>'
        + rows
        + '</section>'
    )


def geometry_outcome_dashboard_html():
    rows = []

    for horizon in (
        GEOMETRY_OUTCOME_HORIZONS
    ):
        m = geometry_outcome_metrics[
            str(horizon)
        ]

        rows.append(
            f"""
            <div class="gol-row">
              <b>H{horizon}</b>
              <span>n {int(m.get('samples', 0))}</span>
              <span>path {float(m.get('path_tradeable_rate', 0))*100:.1f}%</span>
              <span>terminal {float(m.get('terminal_tradeable_rate', 0))*100:.1f}%</span>
              <span>↑ {float(m.get('up_win_rate', 0))*100:.1f}%</span>
              <span>↓ {float(m.get('down_win_rate', 0))*100:.1f}%</span>
              <span>MFE net {float(m.get('avg_best_mfe_net_pct', 0)):+.4f}%</span>
              <span>gross {float(m.get('avg_gross_best_excursion_pct', 0)):.4f}%</span>
              <span>cost× {float(m.get('avg_cost_coverage_ratio', 0)):.2f}</span>
              <span>asym {float(m.get('avg_path_asymmetry', 0)):+.2f}</span>
            </div>
            """
        )

    leaders = geometry_outcome_leaderboard(
        limit=5
    )

    if leaders:
        leader_html = "".join(
            f"""
            <div class="gol-leader">
              <b>{geo_escape_html(str(x['key']))}</b>
              <span>n {int(x.get('samples', 0))}</span>
              <span>path {float(x.get('path_tradeable_rate', 0))*100:.1f}%</span>
              <span>MFE {float(x.get('avg_best_mfe_net_pct', 0)):+.4f}%</span>
              <span>gross {float(x.get('avg_gross_best_excursion_pct', 0)):.4f}%</span>
              <span>cost× {float(x.get('avg_cost_coverage_ratio', 0)):.2f}</span>
              <span>asym {float(x.get('avg_path_asymmetry', 0)):+.2f}</span>
            </div>
            """
            for x in leaders
        )
    else:
        leader_html = (
            '<div class="sub">'
            'No validated geometry cell yet — collecting frozen outcomes.'
            '</div>'
        )

    return (
        '<section class="card">'
        '<h2>GOL2 · GEOMETRIC + LATENT OUTCOME LEARNER</h2>'
        '<div class="model-note">'
        'Frozen geometry → future path. Strict net-after-cost labels remain, while GOL2 also learns gross/latent motion and cost coverage. '
        'Path = hindsight opportunity label; terminal = horizon-close outcome. '
        'Research only.'
        '</div>'
        + "".join(rows)
        + '<div class="gol-leaders-title">LEADERS</div>'
        + leader_html
        + '</section>'
    )


def geo_ascii_bar(
    position,
    width=None,
):
    if width is None:
        width = (
            GEOMETRY_ASCII_WIDTH
        )

    width = max(
        9,
        int(width),
    )

    pos = max(
        0.0,
        min(
            1.0,
            float(
                position
            ),
        ),
    )

    idx = int(
        round(
            pos
            * (
                width - 1
            )
        )
    )

    chars = [
        "·"
        for _ in range(
            width
        )
    ]

    chars[0] = "L"
    chars[-1] = "U"
    chars[idx] = "●"

    return "".join(
        chars
    )


def print_geometry_layer(
    geometry,
):
    if not geometry.get(
        "ready",
        False,
    ):
        return

    print(
        "GEO1:",
        geometry[
            "spine_state"
        ],
        f"extrema={geometry['extrema_count']}",
        f"levels={len(geometry['levels'])}",
    )

    for window in (
        5,
        30,
        120,
    ):
        item = geometry[
            "horizons"
        ][str(window)]

        print(
            f"GEO H{window}:",
            geo_ascii_bar(
                item[
                    "position"
                ],
                width=25,
            ),
            f"mid={item['mid']:.2f}",
            f"v={item['mid_velocity_pct']:+.4f}%",
            item[
                "motion"
            ],
        )

    support = geometry.get(
        "nearest_support"
    )

    resistance = geometry.get(
        "nearest_resistance"
    )

    if (
        support
        or resistance
    ):
        print(
            "GEO LEVELS:",
            (
                "SUP="
                + f"{support['level']:.2f}"
                + f"[H{support.get('scale','?')}]"
                + f"({support['touches']}x"
                + f",S={support.get('strength',0):.2f})"
                if support
                else "SUP=n/a"
            ),
            "|",
            (
                "RES="
                + f"{resistance['level']:.2f}"
                + f"[H{resistance.get('scale','?')}]"
                + f"({resistance['touches']}x"
                + f",S={resistance.get('strength',0):.2f})"
                if resistance
                else "RES=n/a"
            ),
        )

        flipped = [
            x
            for x in geometry.get(
                "levels",
                [],
            )
            if x.get(
                "flip"
            )
        ]

        zones = geometry.get(
            "structural_zones",
            [],
        )

        if zones:
            top_zone = zones[0]

            print(
                "GEO ZONE:",
                top_zone["id"],
                f"{top_zone['lower']:.2f}-{top_zone['upper']:.2f}",
                top_zone["role"],
                "scales="
                + ",".join(
                    "H" + str(x)
                    for x in top_zone.get(
                        "scales",
                        [],
                    )
                ),
                f"S={top_zone['strength']:.2f}",
                f"Pz={top_zone['pressure']:.2f}",
            )

        if flipped:
            top = flipped[0]
            print(
                "GEO FLIP:",
                top.get(
                    "origin_kind"
                ),
                "=>",
                top.get(
                    "current_role"
                ),
                f"@{top['level']:.2f}",
                f"H{top.get('scale','?')}",
                (
                    "RETESTED"
                    if top.get(
                        "retested"
                    )
                    else "not_retested"
                ),
            )

    if geometry.get(
        "transitions"
    ):
        print(
            "GEO TRANS:",
            ",".join(
                geometry[
                    "transitions"
                ][:8]
            ),
        )




def print_cone_model(
    geometry,
):
    cone = geometry.get(
        "cone_model",
        {},
    )

    if not cone.get(
        "ready",
        False,
    ):
        return

    curvature = cone.get(
        "spine_curvature",
        {},
    )

    print(
        "CONE2:",
        f"bend={curvature.get('bend_deg', 0.0):+.1f}deg",
        f"twist5/60={cone.get('twist', {}).get('5_60', {}).get('combined_deg', 0.0):.1f}deg",
        f"flags={len(cone.get('active_flags', []))}",
    )

    for h in (
        5,
        30,
        120,
    ):
        m = cone[
            "horizons"
        ][str(h)]

        print(
            f"CONE H{h}:",
            f"tilt={m['tilt_deg']:+.1f}°",
            f"phi={m['phi_deg']:+.1f}°",
            f"e={m['eccentricity']:.2f}",
            f"P={m['pressure']:.2f}",
            f"AΔn={m['area_velocity_norm']:+.2f}",
            m[
                "area_state"
            ],
        )

    if cone.get(
        "transitions"
    ):
        print(
            "CONE TRANS:",
            ",".join(
                cone[
                    "transitions"
                ][:8]
            ),
        )


def cone_clamp(value, lo=0.0, hi=1.0):
    return max(lo, min(hi, float(value)))


def build_cone_svg(geometry):
    if not geometry.get("ready", False):
        return ""

    y_map = {
        5: 455,
        15: 385,
        30: 305,
        60: 210,
        120: 105,
    }

    macro = geometry["horizons"]["120"]
    macro_width_abs = max(
        1e-9,
        float(macro["upper"]) - float(macro["lower"]),
    )
    price = float(geometry["price"])

    slice_parts = []
    centers = []
    rx_map = {}

    for h in CONE_HORIZONS:
        d = geometry["horizons"][str(h)]
        width_abs = max(
            0.0,
            float(d["upper"]) - float(d["lower"]),
        )
        ratio = cone_clamp(width_abs / macro_width_abs)
        rx = max(
            CONE_MIN_RX,
            CONE_MIN_RX + ratio * (CONE_MAX_RX - CONE_MIN_RX),
        )
        ry = 10.0 + rx * 0.105

        displacement = (
            float(d["mid"]) - price
        ) / max(macro_width_abs / 2.0, 1e-9)
        displacement = max(-1.0, min(1.0, displacement))
        cx = CONE_CENTER_X + displacement * 105.0
        cy = y_map[h]

        centers.append((cx, cy))
        rx_map[h] = rx

        motion = str(d.get("motion", ""))
        width_pct = float(d.get("width_pct", 0.0))
        pos = float(d.get("position", 0.5))

        cone_model = geometry.get(
            "cone_model",
            {},
        )

        model = cone_model.get(
            "horizons",
            {},
        ).get(
            str(h),
            {},
        )

        eccentricity = float(
            model.get(
                "eccentricity",
                0.0,
            )
        )

        phi_deg = float(
            model.get(
                "phi_deg",
                0.0,
            )
        )

        tilt_deg = float(
            model.get(
                "tilt_deg",
                0.0,
            )
        )

        pressure = float(
            model.get(
                "pressure",
                0.0,
            )
        )

        area_state = str(
            model.get(
                "area_state",
                "STABLE",
            )
        )

        # Ellipse aspect ratio from statistical eccentricity.
        b_over_a = math.sqrt(
            max(
                0.02,
                1.0
                - eccentricity
                * eccentricity,
            )
        )

        ry = max(
            8.0,
            rx
            * (
                0.12
                + 0.24
                * b_over_a
            ),
        )

        screen_phi = max(
            -55.0,
            min(
                55.0,
                phi_deg,
            ),
        )

        tilt_dx = (
            max(
                -1.0,
                min(
                    1.0,
                    tilt_deg / 60.0,
                ),
            )
            * 55.0
        )

        pressure_axis = str(
            model.get(
                "pressure_axis",
                "",
            )
        )

        pressure_sign = (
            1.0
            if pressure_axis == "PRICE_UP"
            else -1.0
            if pressure_axis == "PRICE_DOWN"
            else 0.0
        )

        pressure_x = (
            cx
            + pressure_sign
            * pressure
            * rx
            * 0.80
        )

        slice_parts.append(
            '<g class="slice">'
            f'<ellipse cx="{cx:.2f}" cy="{cy:.2f}" rx="{rx:.2f}" ry="{ry:.2f}" '
            f'transform="rotate({screen_phi:.2f} {cx:.2f} {cy:.2f})" class="slice-outer"/>'
            f'<ellipse cx="{cx:.2f}" cy="{cy:.2f}" rx="{rx*0.72:.2f}" ry="{ry*0.72:.2f}" '
            f'transform="rotate({screen_phi:.2f} {cx:.2f} {cy:.2f})" class="slice-viable"/>'
            f'<ellipse cx="{cx:.2f}" cy="{cy:.2f}" rx="{rx*0.45:.2f}" ry="{ry*0.45:.2f}" '
            f'transform="rotate({screen_phi:.2f} {cx:.2f} {cy:.2f})" class="slice-effective"/>'
            f'<line x1="{cx:.2f}" y1="{cy:.2f}" x2="{cx+tilt_dx:.2f}" y2="{cy-28:.2f}" class="tilt-vector"/>'
            f'<line x1="{cx:.2f}" y1="{cy+7:.2f}" x2="{pressure_x:.2f}" y2="{cy+7:.2f}" class="pressure-vector"/>'
            f'<circle cx="{pressure_x:.2f}" cy="{cy+7:.2f}" r="{3+pressure*3:.2f}" class="pressure-node"/>'
            f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="4" class="spine-node"/>'
            f'<text x="{cx-rx-38:.2f}" y="{cy+4:.2f}" class="slice-label">H{h}</text>'
            f'<text x="{cx+rx+8:.2f}" y="{cy+4:.2f}" class="slice-meta">'
            f'W {width_pct:.4f}% · tilt {tilt_deg:+.0f}° · e {eccentricity:.2f} · P {pressure:.2f} · {area_state}</text>'
            '</g>'
        )

    spine_points = " ".join(
        f"{cx:.2f},{cy:.2f}"
        for cx, cy in centers
    )

    first_cx, first_cy = centers[0]
    last_cx, last_cy = centers[-1]
    first_rx = rx_map[5]
    last_rx = rx_map[120]

    # Structural zones projected onto the macro H120 slice.
    macro_low = float(macro["lower"])
    macro_high = float(macro["upper"])
    level_parts = []

    zones = geometry.get(
        "structural_zones",
        [],
    )

    for zone in zones[:7]:
        zl = float(
            zone["lower"]
        )

        zu = float(
            zone["upper"]
        )

        if macro_high <= macro_low:
            u1 = 0.5
            u2 = 0.5
        else:
            u1 = cone_clamp(
                (zl - macro_low)
                / (
                    macro_high
                    - macro_low
                )
            )
            u2 = cone_clamp(
                (zu - macro_low)
                / (
                    macro_high
                    - macro_low
                )
            )

        x1 = (
            last_cx
            - last_rx
            + u1
            * 2.0
            * last_rx
        )

        x2 = (
            last_cx
            - last_rx
            + u2
            * 2.0
            * last_rx
        )

        width = max(
            3.0,
            x2 - x1,
        )

        role = str(
            zone[
                "role"
            ]
        )

        scales = ",".join(
            "H" + str(s)
            for s in zone.get(
                "scales",
                [],
            )
        )

        zone_class = (
            "zone-support"
            if role == "SUPPORT"
            else "zone-resistance"
            if role == "RESISTANCE"
            else "zone-pivot"
        )

        level_parts.append(
            f'<rect x="{x1:.2f}" y="{last_cy-76:.2f}" '
            f'width="{width:.2f}" height="152" class="struct-zone {zone_class}"/>'
            f'<text x="{x1+3:.2f}" y="{last_cy-82:.2f}" class="struct-label">'
            f'{zone["id"]} {role[:3]} · {scales} · Pz {zone["pressure"]:.2f}</text>'
        )

    domain_parts = []

    topology = (
        geometry.get(
            "cone_transition_dynamics",
            {},
        ).get(
            "phase_topology",
            {},
        )
    )

    for idx_domain, domain in enumerate(
        topology.get(
            "domains",
            [],
        )
    ):
        start_h = int(
            domain.get(
                "start_h",
                0,
            )
        )
        end_h = int(
            domain.get(
                "end_h",
                0,
            )
        )

        if (
            start_h not in y_map
            or end_h not in y_map
        ):
            continue

        y1 = float(y_map[start_h])
        y2 = float(y_map[end_h])
        top = min(y1, y2)
        bottom = max(y1, y2)
        x = 815.0 + idx_domain * 14.0

        cls = (
            "phase-up"
            if str(domain.get("direction")) == "UP"
            else "phase-down"
        )
        life = str(
            domain.get(
                "lifecycle",
                "STABLE",
            )
        )

        domain_parts.append(
            f'<line x1="{x:.2f}" y1="{top:.2f}" x2="{x:.2f}" y2="{bottom:.2f}" class="phase-domain {cls}"/>'
            f'<line x1="{x-6:.2f}" y1="{top:.2f}" x2="{x+6:.2f}" y2="{top:.2f}" class="phase-domain {cls}"/>'
            f'<line x1="{x-6:.2f}" y1="{bottom:.2f}" x2="{x+6:.2f}" y2="{bottom:.2f}" class="phase-domain {cls}"/>'
            f'<text x="{x+8:.2f}" y="{(top+bottom)/2:.2f}" class="phase-label">{domain.get("domain_id","")} {life}</text>'
        )

    front_parts = []
    pfl = latest_phase_front_lag if isinstance(latest_phase_front_lag, dict) else {}
    if pfl.get("ready", False) and str(pfl.get("front_direction", "NONE")) in ("UP", "DOWN"):
        fh1 = int(pfl.get("from_horizon", 0) or 0)
        fh2 = int(pfl.get("front_horizon", 0) or 0)
        if fh1 in y_map and fh2 in y_map:
            x = 785.0
            y1 = float(y_map[fh1])
            y2 = float(y_map[fh2])
            cls = "pfl-front-up" if str(pfl.get("front_direction")) == "UP" else "pfl-front-down"
            front_parts.append(
                f'<line x1="{x:.1f}" y1="{y1:.1f}" x2="{x:.1f}" y2="{y2:.1f}" class="pfl-front {cls}"/>'
                f'<circle cx="{x:.1f}" cy="{y1:.1f}" r="5" class="{cls}"/>'
                f'<circle cx="{x:.1f}" cy="{y2:.1f}" r="7" class="{cls}"/>'
                f'<text x="{x+10:.1f}" y="{(y1+y2)/2:.1f}" class="phase-label">PFL1 {geo_escape_html(str(pfl.get("front_direction")))} {geo_escape_html(str(pfl.get("propagation_mode")))} S={float(pfl.get("strength",0)):.2f}</text>'
            )

    svg = (
        '<svg class="cone-svg" viewBox="0 0 900 560" '
        'role="img" aria-label="Elliptic future cone">'
        '<defs>'
        '<filter id="coneGlow"><feGaussianBlur stdDeviation="3" result="blur"/>'
        '<feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge>'
        '</filter>'
        '</defs>'
        f'<line x1="{first_cx-first_rx:.2f}" y1="{first_cy:.2f}" '
        f'x2="{last_cx-last_rx:.2f}" y2="{last_cy:.2f}" class="cone-wall"/>'
        f'<line x1="{first_cx+first_rx:.2f}" y1="{first_cy:.2f}" '
        f'x2="{last_cx+last_rx:.2f}" y2="{last_cy:.2f}" class="cone-wall"/>'
        + "".join(level_parts)
        + "".join(domain_parts)
        + "".join(front_parts)
        + "".join(slice_parts)
        + f'<polyline points="{spine_points}" class="spine-line"/>'
        + f'<line x1="{first_cx:.2f}" y1="{first_cy+42:.2f}" '
          f'x2="{first_cx:.2f}" y2="{first_cy+8:.2f}" class="apex-link"/>'
        + f'<circle cx="{first_cx:.2f}" cy="{first_cy+42:.2f}" r="5" class="apex-node"/>'
        + f'<text x="{first_cx:.2f}" y="{first_cy+64:.2f}" text-anchor="middle" class="apex-label">'
          f'xₜ · {price:.2f}</text>'
        + '<text x="28" y="34" class="axis-label">future horizon ↑</text>'
        + '<text x="570" y="34" class="legend-text">'
          'outer=possible · dashed=viable · inner=effective</text>'
        + '</svg>'
    )

    return svg


def geo_escape_html(value):
    return (
        str(value)
        .replace(
            "&",
            "&amp;",
        )
        .replace(
            "<",
            "&lt;",
        )
        .replace(
            ">",
            "&gt;",
        )
        .replace(
            '"',
            "&quot;",
        )
    )


def write_geometry_dashboard(
    geometry,
    state_id,
    timestamp,
):
    if not geometry.get(
        "ready",
        False,
    ):
        return

    price = float(
        geometry[
            "price"
        ]
    )

    cone_svg = build_cone_svg(
        geometry
    )

    macro = geometry[
        "horizons"
    ][
        "120"
    ]

    macro_low = float(
        macro[
            "lower"
        ]
    )

    macro_high = float(
        macro[
            "upper"
        ]
    )

    macro_width = max(
        1e-12,
        macro_high
        - macro_low,
    )

    corridor_rows = []

    nested_rows = []

    for window in (
        CORRIDOR_WINDOWS
    ):
        item = geometry[
            "horizons"
        ][str(window)]

        pos = max(
            0.0,
            min(
                1.0,
                float(
                    item[
                        "position"
                    ]
                ),
            ),
        )

        marker_pct = (
            pos
            * 100.0
        )

        mid_pos = (
            (
                float(
                    item[
                        "mid"
                    ]
                )
                - float(
                    item[
                        "lower"
                    ]
                )
            )
            / max(
                1e-12,
                float(
                    item[
                        "upper"
                    ]
                )
                - float(
                    item[
                        "lower"
                    ]
                ),
            )
            * 100.0
        )

        corridor_rows.append(
            f"""
            <div class="row">
              <div class="h">H{window}</div>
              <div class="track">
                <div class="mid" style="left:{mid_pos:.2f}%"></div>
                <div class="price" style="left:{marker_pct:.2f}%"></div>
              </div>
              <div class="meta">
                <b>{item['motion']}</b>
                <span>W {item['width_pct']:.4f}%</span>
                <span>mid v {item['mid_velocity_pct']:+.4f}%</span>
              </div>
              <div class="ends">
                <span>{item['lower']:.2f}</span>
                <span>{item['upper']:.2f}</span>
              </div>
            </div>
            """
        )

        left = (
            (
                float(
                    item[
                        "lower"
                    ]
                )
                - macro_low
            )
            / macro_width
            * 100.0
        )

        right = (
            (
                float(
                    item[
                        "upper"
                    ]
                )
                - macro_low
            )
            / macro_width
            * 100.0
        )

        left = max(
            0.0,
            min(
                100.0,
                left,
            ),
        )

        right = max(
            left,
            min(
                100.0,
                right,
            ),
        )

        nested_rows.append(
            f"""
            <div class="nest-row">
              <span class="nest-label">H{window}</span>
              <div class="nest-track">
                <div class="nest-box"
                     style="left:{left:.2f}%;width:{max(0.5,right-left):.2f}%"></div>
                <div class="macro-price"
                     style="left:{((price-macro_low)/macro_width*100.0):.2f}%"></div>
              </div>
            </div>
            """
        )

    zone_rows = []

    for zone in geometry.get(
        "structural_zones",
        [],
    ):
        zone_badges = []

        if zone.get(
            "flip"
        ):
            zone_badges.append(
                "FLIP"
            )

        if zone.get(
            "retested"
        ):
            zone_badges.append(
                "RETEST"
            )

        if int(
            zone.get(
                "scale_count",
                1,
            )
        ) >= 2:
            zone_badges.append(
                "MULTISCALE"
            )

        zone_rows.append(
            f"""
            <div class="zone-row">
              <span class="pill">{geo_escape_html(zone['role'])}</span>
              <b>{float(zone['lower']):.2f} — {float(zone['upper']):.2f}</b>
              <span>{', '.join('H'+str(s) for s in zone.get('scales', []))}</span>
              <span>S {float(zone['strength']):.2f} · Pz {float(zone['pressure']):.2f}</span>
              <span>{int(zone['touches'])} touches · age {float(zone['age_minutes']):.0f}m</span>
              <span>{geo_escape_html(' · '.join(zone_badges) if zone_badges else '—')}</span>
            </div>
            """
        )

    level_rows = []

    for level in geometry.get(
        "levels",
        [],
    )[:10]:
        distance_pct = (
            (
                price
                - float(
                    level[
                        "level"
                    ]
                )
            )
            / float(
                level[
                    "level"
                ]
            )
            * 100.0
            if float(
                level[
                    "level"
                ]
            )
            else 0.0
        )

        origin = geo_escape_html(
            level.get(
                "origin_kind",
                "UNKNOWN",
            )
        )

        role = geo_escape_html(
            level.get(
                "current_role",
                level.get(
                    "kind",
                    "PIVOT",
                ),
            )
        )

        state_badges = []

        if level.get(
            "flip"
        ):
            state_badges.append(
                "FLIP"
            )

        if level.get(
            "retested"
        ):
            state_badges.append(
                "RETEST"
            )

        if int(
            level.get(
                "confluence_count",
                1,
            )
        ) >= 2:
            state_badges.append(
                "CONFLUENCE"
            )

        badges = (
            " · ".join(
                state_badges
            )
            if state_badges
            else "—"
        )

        level_rows.append(
            f"""
            <div class="level level-v2">
              <span class="pill">{role}</span>
              <b>{float(level['level']):.2f}</b>
              <span>H{int(level.get('scale',0))} · S {float(level.get('strength',0)):.2f}</span>
              <span>{int(level['touches'])} touches · age {float(level.get('age_minutes',0)):.0f}m</span>
              <span class="origin">{origin} → {role}</span>
              <span class="badges">{geo_escape_html(badges)}</span>
              <span>Δ {distance_pct:+.4f}%</span>
            </div>
            """
        )

    flags = " · ".join(
        geometry.get(
            "active_flags",
            [],
        )
    ) or "GEO_BASE"

    transitions = " · ".join(
        geometry.get(
            "transitions",
            [],
        )
    ) or "none"

    cone_model = geometry.get(
        "cone_model",
        {},
    )

    cone_rows = []

    if cone_model.get(
        "ready",
        False,
    ):
        for h in CONE_HORIZONS:
            m = cone_model[
                "horizons"
            ][str(h)]

            cone_rows.append(
                f"""
                <div class="cone-model-row">
                  <b>H{h}</b>
                  <span>tilt {m['tilt_deg']:+.1f}°</span>
                  <span>φ {m['phi_deg']:+.1f}°</span>
                  <span>e {m['eccentricity']:.2f}</span>
                  <span>P {m['pressure']:.2f}</span>
                  <span>ΔAₙ {m['area_velocity_norm']:+.2f}</span>
                  <span class="pill">{m['area_state']}</span>
                </div>
                """
            )

    cone_dynamics = cone_model.get(
        "transition_dynamics",
        {},
    )

    ctd_rows = []

    if cone_dynamics.get(
        "ready",
        False,
    ):
        for h in CONE_HORIZONS:
            d = cone_dynamics.get(
                "horizons",
                {},
            ).get(
                str(h),
                {},
            )

            ctd_rows.append(
                f"""
                <div class="ctd-row">
                  <b>H{h}</b>
                  <span>{geo_escape_html(str(d.get('tilt_state','FLAT')))}</span>
                  <span>θ {float(d.get('tilt_deg',0)):+.1f}°</span>
                  <span>ω {float(d.get('omega_deg_per_min',0)):+.1f}°/m</span>
                  <span>α {float(d.get('alpha_deg_per_min2',0)):+.1f}°/m²</span>
                  <span class="pill">{geo_escape_html(str(d.get('rotation_label','FLAT_SLOW')))}</span>
                </div>
                """
            )

    cone_flags = " · ".join(
        cone_model.get(
            "active_flags",
            [],
        )
    ) or "CONE_BASE"

    cone_transitions = " · ".join(
        cone_model.get(
            "transitions",
            [],
        )
    ) or "none"

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="{GEOMETRY_HTML_REFRESH_SECONDS}">
<title>MOR Geometry v1.14 · Scale-Space Phase Topology</title>
<style>
:root {{
  color-scheme: dark;
  --bg:#07090d;
  --panel:#10141b;
  --line:#27303d;
  --text:#edf3ff;
  --muted:#8f9bad;
  --hot:#ffb54c;
  --cold:#7ed8ff;
  --ok:#76f7a8;
}}
*{{box-sizing:border-box}}
body{{
  margin:0;
  background:
    radial-gradient(circle at top right,#142233 0,transparent 35%),
    var(--bg);
  color:var(--text);
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
}}
main{{max-width:980px;margin:auto;padding:16px}}
header{{
  display:grid;
  gap:8px;
  margin-bottom:16px;
}}
h1{{font-size:22px;margin:0}}
.sub{{color:var(--muted);font-size:12px}}
.price-big{{font-size:34px;font-weight:800}}
.grid{{
  display:grid;
  grid-template-columns:repeat(auto-fit,minmax(280px,1fr));
  gap:12px;
}}
.card{{
  background:rgba(16,20,27,.92);
  border:1px solid var(--line);
  border-radius:16px;
  padding:14px;
  box-shadow:0 14px 35px rgba(0,0,0,.24);
}}
.card h2{{font-size:14px;margin:0 0 12px;color:var(--cold)}}
.row{{margin:0 0 16px}}
.h{{font-weight:800;margin-bottom:7px}}
.track{{
  height:16px;
  border-radius:999px;
  border:1px solid #384354;
  background:linear-gradient(90deg,#13243b,#1c2b2a,#3d2519);
  position:relative;
}}
.price{{
  position:absolute;top:-5px;width:3px;height:24px;
  background:var(--hot);box-shadow:0 0 12px var(--hot);
}}
.mid{{
  position:absolute;top:2px;width:2px;height:10px;
  background:var(--cold);
}}
.meta{{display:flex;flex-wrap:wrap;gap:8px;margin-top:6px;font-size:11px}}
.meta span,.sub,.ends{{color:var(--muted)}}
.ends{{display:flex;justify-content:space-between;font-size:10px;margin-top:4px}}
.nest-row{{display:flex;gap:8px;align-items:center;margin:8px 0}}
.nest-label{{width:36px;font-weight:800}}
.nest-track{{
  position:relative;flex:1;height:20px;border:1px solid var(--line);
  border-radius:8px;background:#0a0e14;
}}
.nest-box{{
  position:absolute;top:3px;height:12px;border:1px solid var(--cold);
  background:rgba(126,216,255,.12);border-radius:4px;
}}
.macro-price{{
  position:absolute;top:-2px;width:2px;height:22px;background:var(--hot);
}}
.level{{
  display:grid;grid-template-columns:100px 1fr 1fr 1fr;
  gap:8px;padding:8px 0;border-bottom:1px dashed #26303c;
  font-size:11px;
}}
.level-v2{{
  grid-template-columns:100px 1fr 1fr 1.3fr;
}}
.level-v2 .origin{{color:#aab7ca}}
.level-v2 .badges{{color:#ffcf83}}
.zone-row{{
  display:grid;
  grid-template-columns:100px 1.3fr 1fr 1fr 1.2fr 1fr;
  gap:8px;
  align-items:center;
  padding:9px 0;
  border-bottom:1px dashed #26303c;
  font-size:10px;
}}
.struct-zone{{stroke-width:1}}
.gol-row{{
  display:grid;
  grid-template-columns:42px repeat(6,minmax(70px,1fr));
  gap:7px;
  align-items:center;
  padding:8px 0;
  border-bottom:1px dashed #26303c;
  font-size:10px;
}}
.gol-leaders-title{{
  margin-top:14px;
  margin-bottom:6px;
  color:#8fdcff;
  font-weight:700;
  font-size:11px;
}}
.spt-domain{{
  display:grid;
  grid-template-columns:.45fr 1.3fr .7fr .85fr .65fr .65fr;
  gap:7px;
  padding:7px 0;
  border-bottom:1px dashed #26303c;
  font-size:9px;
}}
.spt-boundary{{
  display:grid;
  grid-template-columns:1.3fr 1fr .9fr .7fr .7fr;
  gap:7px;
  padding:7px 0;
  border-bottom:1px dashed #26303c;
  font-size:9px;
}}
.spt-event{{
  display:grid;
  grid-template-columns:.5fr .9fr 1.5fr;
  gap:7px;
  padding:7px 0;
  border-bottom:1px dashed #26303c;
  font-size:9px;
}}
.spt-sync{{
  display:grid;
  grid-template-columns:.6fr 2.4fr;
  gap:7px;
  padding:7px 0;
  border-bottom:1px dashed #26303c;
  font-size:9px;
}}
.teg-node{{
  display:grid;
  grid-template-columns:42px 1.2fr .8fr .8fr .7fr;
  gap:7px;
  padding:7px 0;
  border-bottom:1px dashed #26303c;
  font-size:9px;
}}
.teg-edge{{
  display:grid;
  grid-template-columns:1.5fr .2fr 1.5fr .6fr .6fr .5fr;
  gap:7px;
  padding:7px 0;
  border-bottom:1px dashed #26303c;
  font-size:9px;
}}
.teg-leader{{
  display:grid;
  grid-template-columns:2.6fr .45fr .65fr .55fr .55fr .8fr;
  gap:7px;
  padding:7px 0;
  border-bottom:1px dashed #26303c;
  font-size:9px;
}}
.tom-leader{{
  display:grid;
  grid-template-columns:2.6fr .45fr .65fr .55fr .55fr .8fr;
  gap:7px;
  padding:7px 0;
  border-bottom:1px dashed #26303c;
  font-size:9px;
}}
.tom-kpi{{
  display:inline-block;
  border:1px solid #26303c;
  border-radius:10px;
  padding:8px 10px;
  font-size:10px;
  margin-bottom:8px;
}}
.erl-flag{{
  display:grid;
  grid-template-columns:2.2fr .5fr .7fr .8fr;
  gap:7px;
  padding:7px 0;
  border-bottom:1px dashed #26303c;
  font-size:9px;
}}
.gol-leader{{
  display:grid;
  grid-template-columns:2.2fr .5fr .7fr .8fr;
  gap:7px;
  padding:7px 0;
  border-bottom:1px dashed #26303c;
  font-size:9px;
}}
.zone-support{{fill:rgba(118,247,168,.10);stroke:#76f7a8;stroke-opacity:.55}}
.zone-resistance{{fill:rgba(255,127,159,.10);stroke:#ff7f9f;stroke-opacity:.55}}
.zone-pivot{{fill:rgba(255,207,131,.10);stroke:#ffcf83;stroke-opacity:.55}}
.pill{{
  display:inline-flex;align-items:center;justify-content:center;
  padding:2px 6px;border:1px solid #354252;border-radius:999px;
  color:var(--ok);
}}
.flags{{font-size:11px;line-height:1.65;color:#cbd6e6}}
.cone-card{{margin-bottom:12px;overflow:hidden}}
.cone-svg{{width:100%;height:auto;display:block}}
.slice-outer{{fill:rgba(126,216,255,.03);stroke:#7ed8ff;stroke-width:2}}
.slice-viable{{fill:rgba(118,247,168,.02);stroke:#76f7a8;stroke-width:1.4;stroke-dasharray:8 6}}
.slice-effective{{fill:rgba(255,181,76,.05);stroke:#ffb54c;stroke-width:1.4}}
.spine-line{{fill:none;stroke:#76f7a8;stroke-width:3;filter:url(#coneGlow)}}
.spine-node{{fill:#76f7a8}}
.cone-wall{{stroke:#52677e;stroke-width:1.5;stroke-dasharray:5 7}}
.phase-domain{{stroke-width:4;opacity:.78}}
.phase-up{{stroke:#76f7a8}}
.phase-down{{stroke:#ff7f9f}}
.phase-label{{fill:#aab7ca;font-size:8px}}
.pfl-front{{stroke-width:4;stroke-dasharray:7 5;opacity:.92}}
.pfl-front-up{{stroke:#76f7a8;fill:#76f7a8}}
.pfl-front-down{{stroke:#ff7f9f;fill:#ff7f9f}}
.struct-line{{stroke:#ffb54c;stroke-width:1;stroke-opacity:.42;stroke-dasharray:4 6}}
.struct-label,.slice-label,.slice-meta,.axis-label,.legend-text,.apex-label{{fill:#aab7ca;font-size:12px}}
.slice-label{{fill:#edf3ff;font-weight:700}}
.slice-meta{{font-size:10px}}
.struct-label,.legend-text{{font-size:9px}}
.apex-node{{fill:#ffb54c;filter:url(#coneGlow)}}
.apex-link{{stroke:#ffb54c;stroke-width:1.4}}
.apex-label{{fill:#ffcf83;font-size:11px}}
.tilt-vector{{stroke:#b496ff;stroke-width:2;stroke-opacity:.9}}
.pressure-vector{{stroke:#ff7f9f;stroke-width:2;stroke-opacity:.75}}
.pressure-node{{fill:#ff7f9f;filter:url(#coneGlow)}}
.ctd-row{{
  display:grid;
  grid-template-columns:42px repeat(5,minmax(74px,1fr));
  gap:7px;
  align-items:center;
  padding:8px 0;
  border-bottom:1px dashed #26303c;
  font-size:10px;
}}
.ctd-summary{{
  display:grid;
  grid-template-columns:repeat(auto-fit,minmax(135px,1fr));
  gap:8px;
  margin-top:12px;
}}
.ctd-kpi{{
  border:1px solid #26303c;
  border-radius:10px;
  padding:8px;
  font-size:10px;
}}
.cone-model-row{{
  display:grid;
  grid-template-columns:42px repeat(6,minmax(70px,1fr));
  gap:7px;
  align-items:center;
  padding:8px 0;
  border-bottom:1px dashed #26303c;
  font-size:10px;
}}
.model-note{{
  font-size:10px;color:var(--muted);line-height:1.5;margin-bottom:10px
}}
@media(max-width:700px){{
  .cone-model-row,.ctd-row{{
    grid-template-columns:42px 1fr 1fr;
  }}
}}
@media(max-width:560px){{
  .level,.level-v2,.zone-row,.gol-row,.gol-leader,.tom-leader,.teg-node,.teg-edge,.teg-leader,.spt-domain,.spt-boundary,.spt-event,.spt-sync,.erl-flag{{grid-template-columns:92px 1fr;}}
  .price-big{{font-size:28px}}
}}
</style>
</head>
<body>
<main>
<header>
  <div class="sub">MOR TRADER v1.23 · GEO3 + CONE2.1 + CTD1 + GOL2 + TOM1 + TEG1 + SPT2 + PBD1 + GSR1 + PFL1 + EFS1 + BPM1 + EH1 + CGE1 + AAL1 + GDX1 + SCR1 + GAP1 + GRC1 + RES1 + ERL1 · {geo_escape_html(state_id)} · {geo_escape_html(timestamp)}</div>
  <h1>Dynamic Future Cone + Bipolar Pressure Field</h1>
  <div class="price-big">{price:.2f} USDT</div>
  <div class="sub">
    {geo_escape_html(geometry['spine_state'])}
    · extrema {geometry['extrema_count']}
    · levels {len(geometry['levels'])}
  </div>
</header>

{gap_lab_dashboard_html()}

{phase_front_lag_dashboard_html()}

{economic_front_surface_dashboard_html()}

{bipolar_pressure_dashboard_html()}

{execution_horizon_arbitration_dashboard_html()}

{conditional_geometry_edge_dashboard_html()}
{action_arbitration_dashboard_html()}
{geometry_testnet_bridge_dashboard_html()}

{execution_readiness_dashboard_html()}

{geometric_stability_reversal_dashboard_html()}

<section class="card cone-card">
<h2>ELLIPTIC FUTURE CONE</h2>
{cone_svg}
</section>

<section class="card cone-card">
<h2>CONE GEOMETRY MODEL · G_H</h2>
<div class="model-note">
Observational state-space proxy: ellipse geometry is estimated from normalized
price displacement + velocity. It is research data, not a literal future oracle.
</div>
{''.join(cone_rows) if cone_rows else '<div class="sub">CONE2 warmup.</div>'}
<div class="flags"><b>CONE FLAGS</b><br>{geo_escape_html(cone_flags)}</div>
<br>
<div class="flags"><b>CONE TRANSITIONS</b><br>{geo_escape_html(cone_transitions)}</div>
</section>

<section class="card cone-card">
<h2>CONE TRANSITION DYNAMICS · CTD1</h2>
<div class="model-note">
The cone is treated as a moving geometry. For every slice:
ω = dθ/dt and α = dω/dt. CTD1 also records which scale changes first
and whether deformation propagates micro→macro, macro→micro, or cross-scale.
</div>
{''.join(ctd_rows) if ctd_rows else '<div class="sub">CTD1 warmup: one previous state is required.</div>'}
<div class="ctd-summary">
  <div class="ctd-kpi"><b>ROTATION ENERGY</b><br>{float(cone_dynamics.get('rotation_energy_deg_per_min',0)):.2f}°/m</div>
  <div class="ctd-kpi"><b>COHERENCE</b><br>{float(cone_dynamics.get('rotation_coherence',0)):.2f}</div>
  <div class="ctd-kpi"><b>SHOCK</b><br>{('H'+str(cone_dynamics.get('shock_horizon'))) if cone_dynamics.get('shock_horizon') is not None else 'none'}</div>
  <div class="ctd-kpi"><b>FRONT</b><br>{geo_escape_html(str(cone_dynamics.get('deformation_front','NONE')))}</div>
  <div class="ctd-kpi"><b>MIDDLE</b><br>{geo_escape_html(str(cone_dynamics.get('middle_inversion','NONE')))}</div>
  <div class="ctd-kpi"><b>BAND</b><br>{geo_escape_html(str(cone_dynamics.get('inversion_band',{}).get('signature','NONE')))}</div>
  <div class="ctd-kpi"><b>STATE PATTERN</b><br>{geo_escape_html(str(cone_dynamics.get('state_pattern','F-F-F-F-F')))}</div>
  <div class="ctd-kpi"><b>PROPAGATION</b><br>{geo_escape_html(str(cone_dynamics.get('propagation',{}).get('mode','NONE')))} / {geo_escape_html(str(cone_dynamics.get('propagation',{}).get('direction','NONE')))}</div>
</div>
<div class="flags" style="margin-top:10px"><b>ORDER</b><br>{geo_escape_html(' → '.join('H'+str(x) for x in cone_dynamics.get('propagation',{}).get('order',[])) or 'none')}</div>
<div class="flags" style="margin-top:8px"><b>CTD FLAGS</b><br>{geo_escape_html(' · '.join(cone_dynamics.get('active_flags',[])) or 'CTD_BASE')}</div>
</section>

{phase_topology_dashboard_html(cone_dynamics)}

{transition_edge_dashboard_html(cone_dynamics)}

<div class="grid">
<section class="card">
<h2>CORRIDOR GEOMETRY</h2>
{''.join(corridor_rows)}
</section>

<section class="card">
<h2>NESTED MAP inside H120</h2>
{''.join(nested_rows)}
</section>

{geometry_outcome_dashboard_html()}

{transition_outcome_dashboard_html()}

<section class="card">
<h2>STRUCTURAL ZONES</h2>
<div class="model-note">
Nearby levels from independent scales are merged into zones.
Pz = proximity × structural strength.
</div>
{''.join(zone_rows) if zone_rows else '<div class="sub">No structural zones yet.</div>'}
</section>

<section class="card">
<h2>EXTREMUM MEMORY · RAW CLUSTERS</h2>
{''.join(level_rows) if level_rows else '<div class="sub">No stable clusters yet.</div>'}
</section>

<section class="card">
<h2>STATE / TRANSITIONS</h2>
<div class="flags"><b>FLAGS</b><br>{geo_escape_html(flags)}</div>
<br>
<div class="flags"><b>TRANSITIONS</b><br>{geo_escape_html(transitions)}</div>
</section>
</div>
</main>
</body>
</html>
"""

    with open(
        GEOMETRY_DASHBOARD_FILE,
        "w",
        encoding="utf-8",
    ) as f:
        f.write(html)

    tmp = (
        GEOMETRY_SNAPSHOT_FILE
        + ".tmp"
    )

    with open(
        tmp,
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            {
                "version":
                    "1.17",
                "state_id":
                    state_id,
                "time":
                    timestamp,
                "geometry":
                    geometry,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    os.replace(
        tmp,
        GEOMETRY_SNAPSHOT_FILE,
    )


def probability_key(regime, strategy, horizon):
    return f"{regime}:{strategy}:H{int(horizon)}"


def get_probability(
    regime,
    strategy,
    horizon=TRADE_HORIZON,
):
    key = probability_key(
        regime,
        strategy,
        horizon,
    )

    s = stats.get(key)

    # v0.7 migration: old probability stats had no horizon.
    # They are valid only for the old 5-candle evaluation.
    if (
        s is None
        and int(horizon) == TRADE_HORIZON
    ):
        s = stats.get(
            f"{regime}:{strategy}"
        )

    if not isinstance(s, dict):
        s = {
            "success": 0,
            "fail": 0,
        }

    # Beta(2,2) prior.
    alpha = 2 + int(s.get("success", 0))
    beta = 2 + int(s.get("fail", 0))

    return alpha / (alpha + beta)


def update_learning(
    regime,
    strategy,
    success,
    horizon=TRADE_HORIZON,
):
    key = probability_key(
        regime,
        strategy,
        horizon,
    )

    if key not in stats:
        stats[key] = {
            "success": 0,
            "fail": 0,
        }

    if success:
        stats[key]["success"] += 1
    else:
        stats[key]["fail"] += 1


def cognitive_loop(regime, signals):
    candidates = []

    for strategy, action in signals.items():
        if action == "HOLD":
            continue

        p = get_probability(regime, strategy)
        candidates.append((p, strategy, action))

    if not candidates:
        return "NONE", "HOLD", 0.0

    candidates.sort(reverse=True)
    p, strategy, action = candidates[0]

    return strategy, action, p



def matrix_key(regime, strategy, horizon):
    return (
        f"{regime}|{strategy}|H{int(horizon)}"
    )


def get_matrix_cell(
    regime,
    strategy,
    horizon,
):
    return horizon_matrix.get(
        matrix_key(
            regime,
            strategy,
            horizon,
        )
    )


def update_horizon_matrix(
    shadow,
    horizon,
    economic,
):
    """
    Update policy-performance cells for every strategy signal
    frozen at this state. A strategy's HOLD is a legitimate policy
    outcome of 0 incremental trade return. BUY/SELL are counted as
    trade-edge samples only when executable from the frozen portfolio.
    """
    regime = shadow["regime"]
    signals = shadow.get("signals", {})

    for strategy, proposed_action in signals.items():
        key = matrix_key(
            regime,
            strategy,
            horizon,
        )

        cell = horizon_matrix.setdefault(
            key,
            {
                "regime": regime,
                "strategy": strategy,
                "horizon_candles": int(horizon),
                "samples": 0,
                "evaluable_policy_samples": 0,
                "policy_net_sum_pct": 0.0,
                "policy_positive_samples": 0,
                "trade_signal_samples": 0,
                "eligible_trade_samples": 0,
                "ineligible_trade_samples": 0,
                "trade_net_sum_pct": 0.0,
                "trade_positive_samples": 0,
                "regret_sum_pct": 0.0,
                "last_updated": None,
            },
        )

        cell["samples"] += 1
        cell["last_updated"] = now_iso()

        if economic is None:
            continue

        eligibility = economic["eligibility"]
        net_returns = economic["net_returns_pct"]

        action = proposed_action
        eligible = True

        if action in ("BUY", "SELL"):
            cell["trade_signal_samples"] += 1
            eligible = bool(
                eligibility[action]["eligible"]
            )

            if eligible:
                cell["eligible_trade_samples"] += 1
            else:
                cell["ineligible_trade_samples"] += 1

        if action == "HOLD":
            policy_net = 0.0

        elif eligible:
            policy_net = float(
                net_returns[action]
            )

        else:
            # An ineligible action cannot be executed by this
            # Spot portfolio, so it is excluded from net-edge mean.
            continue

        cell["evaluable_policy_samples"] += 1
        cell["policy_net_sum_pct"] += policy_net

        if policy_net > 0:
            cell["policy_positive_samples"] += 1

        if action in ("BUY", "SELL"):
            cell["trade_net_sum_pct"] += policy_net
            if policy_net > 0:
                cell["trade_positive_samples"] += 1

        best_return = float(
            economic["economic_best_return_pct"]
        )

        cell["regret_sum_pct"] += max(
            0.0,
            best_return - policy_net,
        )

        n_policy = max(
            1,
            cell["evaluable_policy_samples"],
        )

        cell["avg_policy_net_edge_pct"] = round(
            cell["policy_net_sum_pct"] / n_policy,
            6,
        )

        cell["policy_positive_rate"] = round(
            cell["policy_positive_samples"]
            / n_policy,
            6,
        )

        cell["avg_regret_pct"] = round(
            cell["regret_sum_pct"] / n_policy,
            6,
        )

        n_trade = cell["eligible_trade_samples"]

        if n_trade > 0:
            cell["avg_trade_net_edge_pct"] = round(
                cell["trade_net_sum_pct"] / n_trade,
                6,
            )

            cell["trade_positive_rate"] = round(
                cell["trade_positive_samples"]
                / n_trade,
                6,
            )
        else:
            cell["avg_trade_net_edge_pct"] = None
            cell["trade_positive_rate"] = None


def horizon_system_cell(horizon):
    key = str(int(horizon))

    return horizon_system_metrics.setdefault(
        key,
        {
            "horizon_candles": int(horizon),
            "economic_samples": 0,
            "executed_trade_samples": 0,
            "positive_opportunities": 0,
            "opportunities_captured": 0,
            "executed_net_sum_pct": 0.0,
            "economic_regret_sum_pct": 0.0,
        },
    )


def update_horizon_system_metrics(
    horizon,
    economic,
):
    if economic is None:
        return

    cell = horizon_system_cell(horizon)

    cell["economic_samples"] += 1

    executed_action = economic[
        "executed_action"
    ]

    if executed_action in ("BUY", "SELL"):
        cell["executed_trade_samples"] += 1

    executed_net = float(
        economic["executed_net_return_pct"]
    )

    cell["executed_net_sum_pct"] += executed_net

    regret = float(
        economic["economic_opportunity_cost_pct"]
    )

    cell["economic_regret_sum_pct"] += regret

    best_action = economic[
        "economic_best_action"
    ]

    best_return = float(
        economic["economic_best_return_pct"]
    )

    if (
        best_action in ("BUY", "SELL")
        and best_return > 0
    ):
        cell["positive_opportunities"] += 1

        if executed_action == best_action:
            cell["opportunities_captured"] += 1

    n = max(
        1,
        cell["economic_samples"],
    )

    cell["trade_coverage"] = round(
        cell["executed_trade_samples"] / n,
        6,
    )

    cell["avg_executed_net_pct"] = round(
        cell["executed_net_sum_pct"] / n,
        6,
    )

    cell["avg_economic_regret_pct"] = round(
        cell["economic_regret_sum_pct"] / n,
        6,
    )

    opp = cell["positive_opportunities"]

    cell["opportunity_capture_rate"] = (
        round(
            cell["opportunities_captured"]
            / opp,
            6,
        )
        if opp > 0
        else None
    )


def select_prediction_horizon(
    regime,
    strategy,
):
    """
    Pre-registered adaptive rule:
    - until EVERY candidate horizon has at least N eligible
      trade samples for this regime/strategy, keep H=5;
    - after that, use the horizon with highest observed mean
      executable trade edge.
    This selects only the paper prediction horizon. No live order.
    """
    cells = []

    for horizon in SHADOW_HORIZONS:
        cell = get_matrix_cell(
            regime,
            strategy,
            horizon,
        )

        cells.append(
            (
                horizon,
                cell,
            )
        )

    fully_validated = all(
        isinstance(cell, dict)
        and int(
            cell.get(
                "eligible_trade_samples",
                0,
            )
        ) >= MIN_ADAPTIVE_TRADE_SAMPLES
        for _, cell in cells
    )

    if not fully_validated:
        default_cell = get_matrix_cell(
            regime,
            strategy,
            TRADE_HORIZON,
        )

        return {
            "horizon": TRADE_HORIZON,
            "source": "DEFAULT_EXPLORATION",
            "validated": False,
            "avg_trade_net_edge_pct": (
                default_cell.get(
                    "avg_trade_net_edge_pct"
                )
                if isinstance(
                    default_cell,
                    dict,
                )
                else None
            ),
            "min_samples_across_horizons": min(
                [
                    int(
                        cell.get(
                            "eligible_trade_samples",
                            0,
                        )
                    )
                    if isinstance(cell, dict)
                    else 0
                    for _, cell in cells
                ]
                or [0]
            ),
        }

    ranked = []

    for horizon, cell in cells:
        edge = cell.get(
            "avg_trade_net_edge_pct"
        )

        edge_value = (
            float(edge)
            if edge is not None
            else float("-inf")
        )

        ranked.append(
            (
                edge_value,
                -int(horizon),  # tie -> shorter horizon
                int(horizon),
            )
        )

    ranked.sort(reverse=True)

    best_edge, _, best_horizon = ranked[0]

    return {
        "horizon": best_horizon,
        "source": "ADAPTIVE_MATRIX",
        "validated": True,
        "avg_trade_net_edge_pct": best_edge,
        "min_samples_across_horizons": min(
            int(
                cell.get(
                    "eligible_trade_samples",
                    0,
                )
            )
            for _, cell in cells
        ),
    }


def edge_gate(
    regime,
    strategy,
    action,
):
    if action == "HOLD":
        return {
            "allowed": True,
            "reason": "HOLD",
            "horizon_info": {
                "horizon": TRADE_HORIZON,
                "source": "HOLD",
                "validated": True,
                "avg_trade_net_edge_pct": 0.0,
                "min_samples_across_horizons": 0,
            },
        }

    info = select_prediction_horizon(
        regime,
        strategy,
    )

    if not REQUIRE_VALIDATED_POSITIVE_EDGE:
        return {
            "allowed": True,
            "reason": "EDGE_GATE_DISABLED",
            "horizon_info": info,
        }

    if not info["validated"]:
        adaptive_metrics[
            "edge_gate_blocked_unvalidated"
        ] += 1

        return {
            "allowed": False,
            "reason": "EDGE_NOT_VALIDATED",
            "horizon_info": info,
        }

    edge = info[
        "avg_trade_net_edge_pct"
    ]

    if (
        edge is None
        or float(edge)
        <= POSITIVE_EDGE_EPSILON_PCT
    ):
        adaptive_metrics[
            "edge_gate_blocked_nonpositive"
        ] += 1

        return {
            "allowed": False,
            "reason": "NO_POSITIVE_ESTIMATED_EDGE",
            "horizon_info": info,
        }

    adaptive_metrics[
        "edge_gate_allowed"
    ] += 1

    if info["source"] == "ADAPTIVE_MATRIX":
        adaptive_metrics[
            "adaptive_horizon_used"
        ] += 1

    return {
        "allowed": True,
        "reason": "POSITIVE_EDGE_VALIDATED",
        "horizon_info": info,
    }


def print_horizon_summary(
    regime,
    strategy,
):
    if strategy == "NONE":
        return

    print("HORIZON MATRIX:")

    parts = []

    for h in SHADOW_HORIZONS:
        cell = get_matrix_cell(
            regime,
            strategy,
            h,
        )

        if not isinstance(cell, dict):
            parts.append(
                f"H{h}:n=0 edge=?"
            )
            continue

        n = int(
            cell.get(
                "eligible_trade_samples",
                0,
            )
        )

        edge = cell.get(
            "avg_trade_net_edge_pct"
        )

        edge_text = (
            f"{float(edge):+.5f}%"
            if edge is not None
            else "?"
        )

        parts.append(
            f"H{h}:n={n} edge={edge_text}"
        )

    print(" | ".join(parts))



def surface_policy_action(
    strategy,
    trend_pct,
    volatility_pct,
    trend_threshold_pct,
    vol_gate_pct=None,
):
    """
    Frozen research policy. It does not change production signals.

    MEAN_REVERSION:
      fade trend after |trend| >= threshold.

    MOMENTUM:
      follow trend after |trend| >= threshold.

    BREAKOUT:
      follow trend only when BOTH trend threshold and
      volatility gate are satisfied.
    """
    threshold = float(
        trend_threshold_pct
    )

    trend = float(trend_pct)
    vol = float(volatility_pct)

    if abs(trend) < threshold:
        return "HOLD"

    if strategy == "MEAN_REVERSION":
        return (
            "SELL"
            if trend > 0
            else "BUY"
        )

    if strategy == "MOMENTUM":
        return (
            "BUY"
            if trend > 0
            else "SELL"
        )

    if strategy == "BREAKOUT":
        if vol_gate_pct is None:
            return "HOLD"

        if vol < float(vol_gate_pct):
            return "HOLD"

        return (
            "BUY"
            if trend > 0
            else "SELL"
        )

    return "HOLD"


def surface_policy_configs():
    configs = []

    for threshold in (
        SURFACE_TREND_THRESHOLDS_PCT
    ):
        configs.append(
            {
                "strategy":
                    "MEAN_REVERSION",
                "trend_threshold_pct":
                    float(threshold),
                "vol_gate_pct": None,
            }
        )

        configs.append(
            {
                "strategy":
                    "MOMENTUM",
                "trend_threshold_pct":
                    float(threshold),
                "vol_gate_pct": None,
            }
        )

        for vol_gate in (
            SURFACE_BREAKOUT_VOL_GATES_PCT
        ):
            configs.append(
                {
                    "strategy":
                        "BREAKOUT",
                    "trend_threshold_pct":
                        float(threshold),
                    "vol_gate_pct":
                        float(vol_gate),
                }
            )

    return configs


SURFACE_POLICY_CONFIGS = (
    surface_policy_configs()
)


def surface_cell_key(
    regime,
    strategy,
    horizon,
    trend_threshold_pct,
    vol_gate_pct,
):
    vol_text = (
        "NA"
        if vol_gate_pct is None
        else f"{float(vol_gate_pct):.6f}"
    )

    return (
        f"{regime}|{strategy}|"
        f"H{int(horizon)}|"
        f"T{float(trend_threshold_pct):.6f}|"
        f"V{vol_text}"
    )


def create_surface_probe(
    state,
    close_time_ms,
    horizon_candles,
):
    horizon = int(horizon_candles)

    pre = state.get(
        "paper_portfolio_before",
        {},
    )

    probe = {
        "surface_id":
            f"PS-{state['state_id']}-H{horizon}",
        "created_at": now_iso(),
        "grid_version":
            SURFACE_GRID_VERSION,
        "grid_hash":
            surface_grid_hash(),
        "state_id": state["state_id"],
        "regime": state["regime"],
        "entry_price": state["price"],
        "trend_pct":
            float(state["trend_pct"]),
        "volatility_pct":
            float(
                state[
                    "volatility_pct"
                ]
            ),
        "portfolio_snapshot": {
            "usdt": float(
                pre.get("usdt", 0.0)
            ),
            "btc": float(
                pre.get("btc", 0.0)
            ),
            "equity_usdt": float(
                pre.get(
                    "equity_usdt",
                    0.0,
                )
            ),
            "exposure_pct": float(
                pre.get(
                    "exposure_pct",
                    0.0,
                )
            ),
            "drawdown_pct": float(
                pre.get(
                    "drawdown_pct",
                    0.0,
                )
            ),
        },
        "entry_close_time_ms":
            close_time_ms,
        "due_close_time_ms":
            close_time_ms
            + horizon
            * MINUTE_MS,
        "horizon_candles":
            horizon,
        "status": "FROZEN",
    }

    probe["fingerprint"] = (
        fingerprint(probe)
    )

    pending_surface_shadows.append(
        probe
    )


def create_multi_horizon_surface_probes(
    state,
    close_time_ms,
):
    for horizon in SHADOW_HORIZONS:
        create_surface_probe(
            state,
            close_time_ms,
            horizon,
        )


def update_surface_cell(
    probe,
    config,
    action,
    economic_best_action,
    economic_best_return,
    directional_return,
    executable_eligible,
    executable_net_return,
):
    key = surface_cell_key(
        probe["regime"],
        config["strategy"],
        probe["horizon_candles"],
        config[
            "trend_threshold_pct"
        ],
        config.get(
            "vol_gate_pct"
        ),
    )

    cell = parameter_surface.setdefault(
        key,
        {
            "regime":
                probe["regime"],
            "strategy":
                config["strategy"],
            "horizon_candles":
                int(
                    probe[
                        "horizon_candles"
                    ]
                ),
            "trend_threshold_pct":
                float(
                    config[
                        "trend_threshold_pct"
                    ]
                ),
            "vol_gate_pct":
                config.get(
                    "vol_gate_pct"
                ),
            "samples": 0,
            "active_signal_samples": 0,
            "eligible_trade_samples": 0,
            "blocked_trade_samples": 0,
            "positive_trade_samples": 0,
            "directional_return_sum_pct":
                0.0,
            "trade_net_sum_pct": 0.0,
            "policy_net_sum_pct": 0.0,
            "regret_sum_pct": 0.0,
            "positive_opportunities": 0,
            "opportunities_captured": 0,
            "last_updated": None,
        },
    )

    cell["samples"] += 1
    cell["last_updated"] = now_iso()

    surface_metrics[
        "policy_evaluations"
    ] += 1

    effective_action = action
    policy_net = 0.0

    if action != "HOLD":
        cell[
            "active_signal_samples"
        ] += 1

        surface_metrics[
            "active_signal_evaluations"
        ] += 1

        if executable_eligible:
            cell[
                "eligible_trade_samples"
            ] += 1

            surface_metrics[
                "eligible_trade_evaluations"
            ] += 1

            cell[
                "trade_net_sum_pct"
            ] += executable_net_return

            if executable_net_return > 0:
                cell[
                    "positive_trade_samples"
                ] += 1

                surface_metrics[
                    "positive_trade_evaluations"
                ] += 1

            policy_net = (
                executable_net_return
            )
        else:
            # Spot/Risk-compatible fallback.
            cell[
                "blocked_trade_samples"
            ] += 1
            effective_action = "HOLD"
            policy_net = 0.0

    cell[
        "directional_return_sum_pct"
    ] += directional_return

    cell[
        "policy_net_sum_pct"
    ] += policy_net

    regret = max(
        0.0,
        economic_best_return
        - policy_net,
    )

    cell["regret_sum_pct"] += regret

    if (
        economic_best_action
        in ("BUY", "SELL")
        and economic_best_return > 0
    ):
        cell[
            "positive_opportunities"
        ] += 1

        if (
            effective_action
            == economic_best_action
        ):
            cell[
                "opportunities_captured"
            ] += 1

    n = max(
        1,
        cell["samples"],
    )

    active_n = max(
        1,
        cell[
            "active_signal_samples"
        ],
    )

    eligible_n = cell[
        "eligible_trade_samples"
    ]

    cell[
        "trade_coverage"
    ] = round(
        cell[
            "active_signal_samples"
        ] / n,
        6,
    )

    cell[
        "eligible_trade_rate"
    ] = round(
        cell[
            "eligible_trade_samples"
        ] / active_n,
        6,
    )

    cell[
        "avg_directional_return_pct"
    ] = round(
        cell[
            "directional_return_sum_pct"
        ] / n,
        6,
    )

    cell[
        "avg_policy_net_pct"
    ] = round(
        cell[
            "policy_net_sum_pct"
        ] / n,
        6,
    )

    cell[
        "avg_regret_pct"
    ] = round(
        cell["regret_sum_pct"] / n,
        6,
    )

    if eligible_n > 0:
        cell[
            "avg_trade_net_edge_pct"
        ] = round(
            cell[
                "trade_net_sum_pct"
            ] / eligible_n,
            6,
        )

        cell[
            "trade_positive_rate"
        ] = round(
            cell[
                "positive_trade_samples"
            ] / eligible_n,
            6,
        )
    else:
        cell[
            "avg_trade_net_edge_pct"
        ] = None

        cell[
            "trade_positive_rate"
        ] = None

    opp = cell[
        "positive_opportunities"
    ]

    cell[
        "opportunity_capture_rate"
    ] = (
        round(
            cell[
                "opportunities_captured"
            ] / opp,
            6,
        )
        if opp > 0
        else None
    )


def resolve_surface_probe(
    probe,
    exit_price,
    observed_close_time_ms,
):
    surface_metrics[
        "resolved_probes"
    ] += 1

    entry = float(
        probe["entry_price"]
    )

    raw_return = (
        (exit_price - entry)
        / entry
    ) * 100.0

    snapshot = probe[
        "portfolio_snapshot"
    ]

    eligibility = (
        shadow_economic_eligibility(
            snapshot,
            entry,
        )
    )

    net_returns = {
        "BUY":
            simulate_buy_roundtrip_return_pct(
                entry,
                exit_price,
            ),
        "SELL":
            simulate_sell_owned_roundtrip_return_pct(
                entry,
                exit_price,
            ),
        "HOLD": 0.0,
    }

    econ_best_action, econ_best_return = (
        economic_best_action(
            net_returns,
            eligibility,
        )
    )

    active = 0
    eligible_active = 0
    positive_active = 0

    best_this_fact = None

    for config in SURFACE_POLICY_CONFIGS:
        action = surface_policy_action(
            config["strategy"],
            probe["trend_pct"],
            probe["volatility_pct"],
            config[
                "trend_threshold_pct"
            ],
            config.get(
                "vol_gate_pct"
            ),
        )

        directional_return = (
            action_return(
                action,
                raw_return,
            )
        )

        if action == "HOLD":
            executable_eligible = True
            executable_net = 0.0
        else:
            active += 1

            executable_eligible = bool(
                eligibility[action][
                    "eligible"
                ]
            )

            if executable_eligible:
                eligible_active += 1
                executable_net = float(
                    net_returns[action]
                )

                if executable_net > 0:
                    positive_active += 1
            else:
                executable_net = 0.0

        update_surface_cell(
            probe,
            config,
            action,
            econ_best_action,
            econ_best_return,
            directional_return,
            executable_eligible,
            executable_net,
        )

        # Per-fact best is descriptive hindsight only.
        if (
            action != "HOLD"
            and executable_eligible
        ):
            candidate = (
                executable_net,
                config["strategy"],
                config[
                    "trend_threshold_pct"
                ],
                config.get(
                    "vol_gate_pct"
                ),
                action,
            )

            if (
                best_this_fact is None
                or candidate[0]
                > best_this_fact[0]
            ):
                best_this_fact = candidate

    fact = {
        "surface_id":
            probe["surface_id"],
        "observed_at": now_iso(),
        "observed_close_time_ms":
            observed_close_time_ms,
        "grid_version":
            probe["grid_version"],
        "grid_hash":
            probe["grid_hash"],
        "state_id":
            probe["state_id"],
        "regime":
            probe["regime"],
        "horizon_candles":
            probe[
                "horizon_candles"
            ],
        "entry_price": entry,
        "exit_price": exit_price,
        "trend_pct":
            probe["trend_pct"],
        "volatility_pct":
            probe["volatility_pct"],
        "raw_move_pct":
            round(raw_return, 6),
        "policies_evaluated":
            len(
                SURFACE_POLICY_CONFIGS
            ),
        "active_signals": active,
        "eligible_active_signals":
            eligible_active,
        "positive_active_signals":
            positive_active,
        "economic_best_action":
            econ_best_action,
        "economic_best_return_pct":
            round(
                econ_best_return,
                6,
            ),
        "best_this_fact": (
            {
                "net_pct":
                    round(
                        best_this_fact[0],
                        6,
                    ),
                "strategy":
                    best_this_fact[1],
                "trend_threshold_pct":
                    best_this_fact[2],
                "vol_gate_pct":
                    best_this_fact[3],
                "action":
                    best_this_fact[4],
            }
            if best_this_fact
            is not None
            else None
        ),
        "probe_hash":
            probe["fingerprint"],
        "research_only": True,
        "status": "OBSERVED",
    }

    append_jsonl(
        SURFACE_FACTS_FILE,
        fact,
    )

    print()
    print(
        "=== PARAMETER SURFACE FACT",
        f"H={probe['horizon_candles']}",
        "===",
    )

    print(
        probe["surface_id"],
        "| regime=",
        probe["regime"],
    )

    print(
        f"trend={probe['trend_pct']:+.5f}%",
        "|",
        f"vol={probe['volatility_pct']:.5f}%",
        "|",
        f"move={raw_return:+.5f}%",
    )

    print(
        "policies=",
        len(
            SURFACE_POLICY_CONFIGS
        ),
        "| active=",
        active,
        "| eligible=",
        eligible_active,
        "| positive=",
        positive_active,
    )

    if best_this_fact is None:
        print(
            "observed best active:",
            "none executable",
        )
    else:
        print(
            "observed best active:",
            best_this_fact[1],
            f"T={best_this_fact[2]:.3f}%",
            (
                ""
                if best_this_fact[3]
                is None
                else
                f"V={best_this_fact[3]:.3f}%"
            ),
            best_this_fact[4],
            f"net={best_this_fact[0]:+.5f}%",
            "(HINDSIGHT ONLY)",
        )

    print(
        "economic best:",
        econ_best_action,
        f"{econ_best_return:+.5f}%",
    )

    validated = surface_leaderboard(
        limit=1
    )

    if validated:
        top = validated[0]

        print(
            "research leader:",
            top["regime"],
            top["strategy"],
            f"H={top['horizon_candles']}",
            f"T={top['trend_threshold_pct']:.3f}%",
            (
                ""
                if top["vol_gate_pct"]
                is None
                else
                f"V={top['vol_gate_pct']:.3f}%"
            ),
            f"edge={top['avg_trade_net_edge_pct']:+.5f}%",
            f"n={top['eligible_trade_samples']}",
        )
    else:
        print(
            "research leader:",
            "NO VALIDATED CONFIG YET",
        )

    print(
        "surface cells:",
        len(parameter_surface),
        "| probes pending:",
        len(
            pending_surface_shadows
        ),
    )

    print(
        "================================",
    )


def evaluate_surface_due(
    current_price,
    current_close_time_ms,
):
    global pending_surface_shadows

    remaining = []

    for probe in pending_surface_shadows:
        if (
            current_close_time_ms
            < probe[
                "due_close_time_ms"
            ]
        ):
            remaining.append(
                probe
            )
            continue

        resolve_surface_probe(
            probe,
            current_price,
            current_close_time_ms,
        )

    pending_surface_shadows = (
        remaining
    )


def restore_surface_overdue():
    global pending_surface_shadows

    if not pending_surface_shadows:
        return

    now_ms = int(
        datetime.now(
            timezone.utc
        ).timestamp() * 1000
    )

    overdue = [
        int(
            x[
                "due_close_time_ms"
            ]
        )
        for x
        in pending_surface_shadows
        if int(
            x[
                "due_close_time_ms"
            ]
        ) <= now_ms
    ]

    if not overdue:
        return

    print(
        "Recovering",
        len(set(overdue)),
        "surface horizon(s)...",
    )

    try:
        exact = fetch_due_close_map(
            overdue
        )
    except Exception as e:
        print(
            "Surface recovery error:",
            e,
        )
        return

    remaining = []

    for probe in pending_surface_shadows:
        result = exact.get(
            int(
                probe[
                    "due_close_time_ms"
                ]
            )
        )

        if result is None:
            remaining.append(
                probe
            )
            continue

        resolve_surface_probe(
            probe,
            result["close"],
            result["close_time_ms"],
        )

    pending_surface_shadows = (
        remaining
    )


def print_surface_summary():
    print()
    print("=== SURFACE RESEARCH SUMMARY ===")
    print(
        "grid=",
        SURFACE_GRID_VERSION,
        "| policies/state=",
        len(
            SURFACE_POLICY_CONFIGS
        ),
        "| cells=",
        len(parameter_surface),
        "| pending=",
        len(
            pending_surface_shadows
        ),
    )

    print(
        "evaluations=",
        surface_metrics[
            "policy_evaluations"
        ],
        "| active=",
        surface_metrics[
            "active_signal_evaluations"
        ],
        "| eligible=",
        surface_metrics[
            "eligible_trade_evaluations"
        ],
        "| positive=",
        surface_metrics[
            "positive_trade_evaluations"
        ],
    )

    leaders = surface_leaderboard(
        limit=5
    )

    if not leaders:
        print(
            "validated leaders:",
            "NONE YET",
        )
    else:
        print("validated leaders:")

        for i, item in enumerate(
            leaders,
            1,
        ):
            print(
                f"{i}.",
                item["regime"],
                item["strategy"],
                f"H={item['horizon_candles']}",
                f"T={item['trend_threshold_pct']:.3f}%",
                (
                    ""
                    if item["vol_gate_pct"]
                    is None
                    else
                    f"V={item['vol_gate_pct']:.3f}%"
                ),
                f"edge={item['avg_trade_net_edge_pct']:+.5f}%",
                f"pos={item['trade_positive_rate']*100:.1f}%",
                f"n={item['eligible_trade_samples']}",
            )

    print(
        "RESEARCH ONLY — not connected to execution."
    )
    print(
        "================================",
    )


def fingerprint(obj):
    raw = json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()

    return hashlib.sha256(raw).hexdigest()


def freeze_prediction(
    state,
    strategy,
    action,
    probability,
    close_time_ms,
    horizon_candles=TRADE_HORIZON,
):
    horizon_candles = int(
        horizon_candles
    )

    prediction = {
        "experiment_id":
            f"EXP-{state['state_id']}-H{horizon_candles}",
        "frozen_at": now_iso(),
        "state_id": state["state_id"],
        "regime": state["regime"],
        "strategy": strategy,
        "action": action,
        "entry_price": state["price"],
        "p_success": round(
            probability,
            4,
        ),
        "horizon_candles":
            horizon_candles,
        "entry_close_time_ms":
            close_time_ms,
        "due_close_time_ms":
            close_time_ms
            + horizon_candles
            * MINUTE_MS,
        "status": "FROZEN",
    }

    prediction["fingerprint"] = (
        fingerprint(prediction)
    )

    pending_predictions.append(
        prediction
    )

    append_jsonl(
        PREDICTIONS_FILE,
        prediction,
    )

    save_runtime()

    print()
    print("=== PREDICTION FROZEN ===")
    print(prediction["experiment_id"])
    print(strategy, "=>", action)
    print(
        f'entry={state["price"]:.2f}'
    )
    print(
        "P(success)=",
        prediction["p_success"],
    )
    print(
        "horizon=",
        horizon_candles,
        "candles",
    )
    print(
        "hash=",
        prediction["fingerprint"][:16],
        "...",
    )
    print("=========================")


def create_shadow(
    state,
    close_time_ms,
    horizon_candles,
    executed_action="HOLD",
    execution_status="NO_ORDER",
    execution_reason="",
):
    horizon_candles = int(
        horizon_candles
    )

    pre = state.get(
        "paper_portfolio_before",
        {},
    )

    shadow = {
        "shadow_id":
            f"SHADOW-{state['state_id']}-H{horizon_candles}",
        "created_at": now_iso(),
        "state_id": state["state_id"],
        "regime": state["regime"],
        "entry_price": state["price"],

        "chosen_strategy":
            state["chosen_strategy"],
        "chosen_action":
            state["action"],

        "executed_action":
            executed_action,
        "execution_status":
            execution_status,
        "execution_reason":
            execution_reason,

        "signals": state["signals"],

        "portfolio_snapshot": {
            "usdt": float(
                pre.get("usdt", 0.0)
            ),
            "btc": float(
                pre.get("btc", 0.0)
            ),
            "equity_usdt": float(
                pre.get(
                    "equity_usdt",
                    0.0,
                )
            ),
            "exposure_pct": float(
                pre.get(
                    "exposure_pct",
                    0.0,
                )
            ),
            "drawdown_pct": float(
                pre.get(
                    "drawdown_pct",
                    0.0,
                )
            ),
        },

        "paper_assumptions": {
            "fee_rate":
                PAPER_FEE_RATE,
            "slippage_rate":
                PAPER_SLIPPAGE_RATE,
            "trade_fraction":
                TRADE_FRACTION,
            "max_btc_exposure":
                MAX_BTC_EXPOSURE,
            "max_drawdown_pct":
                MAX_DRAWDOWN_PCT,
            "min_notional_usdt":
                MIN_PAPER_NOTIONAL,
        },

        "entry_close_time_ms":
            close_time_ms,
        "due_close_time_ms":
            close_time_ms
            + horizon_candles
            * MINUTE_MS,
        "horizon_candles":
            horizon_candles,
        "status": "FROZEN",
    }

    shadow["fingerprint"] = (
        fingerprint(shadow)
    )

    pending_shadows.append(
        shadow
    )


def create_multi_horizon_shadows(
    state,
    close_time_ms,
    executed_action,
    execution_status,
    execution_reason,
):
    for horizon in SHADOW_HORIZONS:
        create_shadow(
            state,
            close_time_ms,
            horizon_candles=horizon,
            executed_action=executed_action,
            execution_status=execution_status,
            execution_reason=execution_reason,
        )

    save_runtime()


def action_return(action, raw_return_pct):
    if action == "BUY":
        return raw_return_pct
    if action == "SELL":
        return -raw_return_pct
    return 0.0


def directional_best_action(raw_return_pct):
    # Strict directional label. Economic value is handled separately.
    eps = 1e-12
    if raw_return_pct > eps:
        return "BUY"
    if raw_return_pct < -eps:
        return "SELL"
    return "HOLD"


def simulate_buy_roundtrip_return_pct(entry_price, exit_price):
    """
    Isolated paper counterfactual:
    start with cash -> BUY at entry -> SELL at horizon.
    Includes simulated slippage and fee on both legs.
    """
    start_cash = 1.0

    buy_exec = entry_price * (1.0 + PAPER_SLIPPAGE_RATE)
    buy_notional = start_cash / (1.0 + PAPER_FEE_RATE)
    buy_fee = buy_notional * PAPER_FEE_RATE
    qty = buy_notional / buy_exec

    sell_exec = exit_price * (1.0 - PAPER_SLIPPAGE_RATE)
    sell_gross = qty * sell_exec
    sell_fee = sell_gross * PAPER_FEE_RATE
    final_cash = sell_gross - sell_fee

    return ((final_cash - start_cash) / start_cash) * 100.0


def simulate_sell_owned_roundtrip_return_pct(entry_price, exit_price):
    """
    Spot-compliant bearish counterfactual:
    start with owned BTC -> SELL at entry -> BUY BACK the same BTC at horizon.
    Result is incremental cash benefit/loss versus keeping the inventory.
    Includes simulated slippage and fee on both legs.
    """
    initial_market_value = 1.0
    qty = initial_market_value / entry_price

    sell_exec = entry_price * (1.0 - PAPER_SLIPPAGE_RATE)
    sell_gross = qty * sell_exec
    sell_fee = sell_gross * PAPER_FEE_RATE
    cash_after_sell = sell_gross - sell_fee

    buy_exec = exit_price * (1.0 + PAPER_SLIPPAGE_RATE)
    buy_notional = qty * buy_exec
    buy_fee = buy_notional * PAPER_FEE_RATE
    rebuy_cost = buy_notional + buy_fee

    incremental_cash = cash_after_sell - rebuy_cost
    return (incremental_cash / initial_market_value) * 100.0


def shadow_economic_eligibility(snapshot, entry_price):
    equity = float(snapshot.get("equity_usdt", 0.0))
    usdt = float(snapshot.get("usdt", 0.0))
    btc = float(snapshot.get("btc", 0.0))
    drawdown = float(snapshot.get("drawdown_pct", 0.0))

    btc_value = btc * entry_price

    buy_eligible = False
    buy_reason = "INSUFFICIENT_BUY_CAPACITY"

    if drawdown >= MAX_DRAWDOWN_PCT:
        buy_reason = "MAX_DRAWDOWN_GUARDRAIL"
    else:
        max_exposure_value = equity * MAX_BTC_EXPOSURE
        remaining_exposure = max(
            0.0,
            max_exposure_value - btc_value,
        )

        budget = min(
            equity * TRADE_FRACTION,
            remaining_exposure,
            usdt,
        )
        budget = budget / (1.0 + PAPER_FEE_RATE)

        if budget >= MIN_PAPER_NOTIONAL:
            buy_eligible = True
            buy_reason = "OK"

    sell_eligible = btc_value >= MIN_PAPER_NOTIONAL
    sell_reason = (
        "OK"
        if sell_eligible
        else "NO_SPOT_BTC_INVENTORY"
    )

    return {
        "BUY": {
            "eligible": buy_eligible,
            "reason": buy_reason,
        },
        "SELL": {
            "eligible": sell_eligible,
            "reason": sell_reason,
        },
        "HOLD": {
            "eligible": True,
            "reason": "OK",
        },
    }


def economic_best_action(net_returns, eligibility):
    candidates = {
        "HOLD": 0.0,
    }

    for action in ("BUY", "SELL"):
        if eligibility[action]["eligible"]:
            candidates[action] = net_returns[action]

    # If no executable trade beats zero after costs, HOLD is economically best.
    best_trade = max(candidates, key=candidates.get)
    if candidates[best_trade] <= 0.0:
        return "HOLD", 0.0

    return best_trade, candidates[best_trade]


def resolve_prediction(p, exit_price, observed_close_time_ms):
    entry = p["entry_price"]
    raw_return = ((exit_price - entry) / entry) * 100
    directional_return = action_return(p["action"], raw_return)
    success = directional_return > 0

    brier = (
        p["p_success"] - (1 if success else 0)
    ) ** 2

    fact = {
        "experiment_id": p["experiment_id"],
        "observed_at": now_iso(),
        "observed_close_time_ms": observed_close_time_ms,
        "entry_price": entry,
        "exit_price": exit_price,
        "action": p["action"],
        "strategy": p["strategy"],
        "regime": p["regime"],
        "return_pct": round(directional_return, 5),
        "success": success,
        "p_success": p["p_success"],
        "brier": round(brier, 5),
        "prediction_hash": p["fingerprint"],
        "status": "OBSERVED",
    }

    append_jsonl(FACTS_FILE, fact)

    update_learning(
        p["regime"],
        p["strategy"],
        success,
        horizon=int(
            p.get(
                "horizon_candles",
                TRADE_HORIZON,
            )
        ),
    )

    print()
    print("====== FACT ======")
    print(p["experiment_id"])
    print(p["strategy"], p["action"])
    print(f"entry={entry:.2f}")
    print(f"exit={exit_price:.2f}")
    print("return=", f"{directional_return:+.5f}%")
    print("success=", success)
    print("Brier=", f"{brier:.4f}")
    print(
        "new P=",
        round(
            get_probability(
                    p["regime"],
                    p["strategy"],
                    int(
                        p.get(
                            "horizon_candles",
                            TRADE_HORIZON,
                        )
                    ),
                ),
            4,
        ),
    )
    print("==================")


def resolve_shadow(s, exit_price, observed_close_time_ms):
    global shadow_metrics

    horizon = int(
        s.get(
            "horizon_candles",
            TRADE_HORIZON,
        )
    )

    entry = s["entry_price"]
    raw_return = ((exit_price - entry) / entry) * 100

    # ------------------------------------------------------------
    # Layer 1: DIRECTIONAL counterfactual
    # Pure price-direction information. No claim of executable profit.
    # ------------------------------------------------------------
    directional_returns = {
        "BUY": raw_return,
        "SELL": -raw_return,
        "HOLD": 0.0,
    }

    directional_best = directional_best_action(raw_return)
    chosen_action = s.get("chosen_action", "HOLD")
    directional_hit = chosen_action == directional_best

    shadow_metrics["resolved"] = (
        int(shadow_metrics.get("resolved", 0)) + 1
    )

    if directional_hit:
        shadow_metrics["directional_hits"] = (
            int(shadow_metrics.get("directional_hits", 0)) + 1
        )

    directional_opportunity_cost = (
        directional_returns[directional_best]
        - directional_returns.get(chosen_action, 0.0)
    )

    # ------------------------------------------------------------
    # Layer 2: EXECUTABLE PAPER economics
    # BUY = cash -> BTC -> cash over the frozen horizon.
    # SELL = owned BTC -> cash -> buy-back same BTC.
    # HOLD = zero incremental trade return.
    # ------------------------------------------------------------
    snapshot = s.get("portfolio_snapshot")
    economic_evaluable = isinstance(snapshot, dict)

    economic = None
    economic_hit = None
    economic_opportunity_cost = None

    if economic_evaluable:
        eligibility = shadow_economic_eligibility(
            snapshot,
            entry,
        )

        net_returns = {
            "BUY": simulate_buy_roundtrip_return_pct(
                entry,
                exit_price,
            ),
            "SELL": simulate_sell_owned_roundtrip_return_pct(
                entry,
                exit_price,
            ),
            "HOLD": 0.0,
        }

        econ_best, econ_best_return = economic_best_action(
            net_returns,
            eligibility,
        )

        executed_action = s.get(
            "executed_action",
            "HOLD",
        )

        # A blocked order means the system economically executed HOLD.
        if executed_action not in ("BUY", "SELL"):
            executed_action = "HOLD"

        executed_net = (
            net_returns[executed_action]
            if eligibility.get(
                executed_action,
                {"eligible": True},
            )["eligible"]
            else 0.0
        )

        economic_hit = executed_action == econ_best
        economic_opportunity_cost = (
            econ_best_return - executed_net
        )

        shadow_metrics["economic_evaluable"] = (
            int(shadow_metrics.get("economic_evaluable", 0)) + 1
        )

        if economic_hit:
            shadow_metrics["economic_hits"] = (
                int(shadow_metrics.get("economic_hits", 0)) + 1
            )

        economic = {
            "eligibility": eligibility,
            "net_returns_pct": {
                k: round(v, 5)
                for k, v in net_returns.items()
            },
            "economic_best_action": econ_best,
            "economic_best_return_pct": round(
                econ_best_return,
                5,
            ),
            "executed_action": executed_action,
            "executed_net_return_pct": round(
                executed_net,
                5,
            ),
            "economic_decision_hit": economic_hit,
            "economic_opportunity_cost_pct": round(
                economic_opportunity_cost,
                5,
            ),
        }

    update_horizon_matrix(
        s,
        horizon,
        economic,
    )

    update_horizon_system_metrics(
        horizon,
        economic,
    )

    strategy_returns = {}

    for strategy, proposed_action in s["signals"].items():
        strategy_returns[strategy] = {
            "action": proposed_action,
            "directional_return_pct": round(
                directional_returns.get(
                    proposed_action,
                    0.0,
                ),
                5,
            ),
        }

    dir_total = max(
        1,
        int(shadow_metrics.get("resolved", 0)),
    )

    dir_hits = int(
        shadow_metrics.get("directional_hits", 0)
    )

    econ_total = int(
        shadow_metrics.get("economic_evaluable", 0)
    )

    econ_hits = int(
        shadow_metrics.get("economic_hits", 0)
    )

    directional_accuracy = (
        dir_hits / dir_total
    )

    economic_accuracy = (
        econ_hits / econ_total
        if econ_total > 0
        else None
    )

    fact = {
        "shadow_id": s["shadow_id"],
        "observed_at": now_iso(),
        "observed_close_time_ms": observed_close_time_ms,
        "state_id": s["state_id"],
        "regime": s["regime"],
        "horizon_candles": horizon,
        "entry_price": entry,
        "exit_price": exit_price,

        "directional": {
            "raw_move_pct": round(raw_return, 5),
            "returns_pct": {
                k: round(v, 5)
                for k, v in directional_returns.items()
            },
            "loop_action": chosen_action,
            "best_action": directional_best,
            "decision_hit": directional_hit,
            "opportunity_cost_pct": round(
                directional_opportunity_cost,
                5,
            ),
        },

        "economic": economic,

        "strategy_returns": strategy_returns,

        "accuracy_running": {
            "directional_resolved": dir_total,
            "directional_hits": dir_hits,
            "directional_accuracy": round(
                directional_accuracy,
                5,
            ),
            "economic_evaluable": econ_total,
            "economic_hits": econ_hits,
            "economic_accuracy": (
                round(economic_accuracy, 5)
                if economic_accuracy is not None
                else None
            ),
        },

        "shadow_hash": s["fingerprint"],
        "status": "OBSERVED",
    }

    append_jsonl(SHADOW_FILE, fact)

    print()
    print(f"=== ADAPTIVE SHADOW FACT H={horizon} ===")
    print(s["shadow_id"])

    print(
        "DIRECTIONAL:",
        f"move={raw_return:+.5f}%",
        "| best=",
        directional_best,
        "| loop=",
        chosen_action,
        "| hit=",
        directional_hit,
    )

    print(
        "gross:",
        f"BUY={directional_returns['BUY']:+.5f}%",
        f"SELL={directional_returns['SELL']:+.5f}%",
        "HOLD=+0.00000%",
    )

    if economic is None:
        print(
            "EXECUTABLE NET:",
            "LEGACY SHADOW — portfolio snapshot unavailable",
        )
    else:
        eligibility = economic["eligibility"]
        net = economic["net_returns_pct"]

        buy_text = (
            f"{net['BUY']:+.5f}%"
            if eligibility["BUY"]["eligible"]
            else f"INELIGIBLE({eligibility['BUY']['reason']})"
        )

        sell_text = (
            f"{net['SELL']:+.5f}%"
            if eligibility["SELL"]["eligible"]
            else f"INELIGIBLE({eligibility['SELL']['reason']})"
        )

        print("EXECUTABLE NET:")
        print(
            "BUY_RT=",
            buy_text,
            "| SELL_OWNED_RT=",
            sell_text,
            "| HOLD=+0.00000%",
        )

        print(
            "economic best=",
            economic["economic_best_action"],
            f"{economic['economic_best_return_pct']:+.5f}%",
            "| executed=",
            economic["executed_action"],
            "| hit=",
            economic["economic_decision_hit"],
        )

        print(
            "economic opportunity cost=",
            f"{economic['economic_opportunity_cost_pct']:+.5f}%",
        )

    print(
        "ACCURACY:",
        f"directional={dir_hits}/{dir_total}"
        f" ({directional_accuracy*100:.1f}%)",
        "| economic=",
        (
            f"{econ_hits}/{econ_total}"
            f" ({economic_accuracy*100:.1f}%)"
            if economic_accuracy is not None
            else "n/a"
        ),
    )

    hsys = horizon_system_cell(
        horizon
    )

    print(
        "HORIZON SYSTEM:",
        f"coverage={hsys.get('trade_coverage', 0.0)*100:.1f}%",
        "| capture=",
        (
            f"{hsys['opportunity_capture_rate']*100:.1f}%"
            if hsys.get(
                "opportunity_capture_rate"
            ) is not None
            else "n/a"
        ),
        "| avg_regret=",
        f"{hsys.get('avg_economic_regret_pct', 0.0):+.5f}%",
        "| avg_exec_net=",
        f"{hsys.get('avg_executed_net_pct', 0.0):+.5f}%",
    )

    print("=======================")


def evaluate_due(
    current_price,
    current_close_time_ms,
):
    global pending_predictions
    global pending_shadows

    next_predictions = []

    for p in pending_predictions:
        if current_close_time_ms < p["due_close_time_ms"]:
            next_predictions.append(p)
            continue

        resolve_prediction(
            p,
            current_price,
            current_close_time_ms,
        )

    pending_predictions = next_predictions

    next_shadows = []

    for s in pending_shadows:
        if current_close_time_ms < s["due_close_time_ms"]:
            next_shadows.append(s)
            continue

        resolve_shadow(
            s,
            current_price,
            current_close_time_ms,
        )

    pending_shadows = next_shadows
    save_runtime()


def restore_overdue():
    global pending_predictions
    global pending_shadows

    all_due_times = [
        x["due_close_time_ms"]
        for x in pending_predictions + pending_shadows
    ]

    if not all_due_times:
        return

    now_ms = int(
        datetime.now(timezone.utc).timestamp() * 1000
    )

    overdue_times = sorted({
        t for t in all_due_times
        if t <= now_ms
    })

    if not overdue_times:
        return

    print(
        f"Recovering {len(overdue_times)} overdue horizon(s)..."
    )

    try:
        exact = fetch_due_close_map(
            overdue_times
        )
    except Exception as e:
        print(
            "Batch recovery error:",
            e,
        )
        exact = {}

    next_predictions = []

    for p in pending_predictions:
        result = exact.get(
            p["due_close_time_ms"]
        )

        if result:
            resolve_prediction(
                p,
                result["close"],
                result["close_time_ms"],
            )
        else:
            next_predictions.append(p)

    pending_predictions = next_predictions

    next_shadows = []

    for s in pending_shadows:
        result = exact.get(
            s["due_close_time_ms"]
        )

        if result:
            resolve_shadow(
                s,
                result["close"],
                result["close_time_ms"],
            )
        else:
            next_shadows.append(s)

    pending_shadows = next_shadows
    save_runtime()



def recover_research_after_stream_gap():
    """
    Best-effort recovery after a websocket interruption.

    - Refreshes the rolling 120-candle state window from REST.
    - Resolves any frozen experiments whose horizons elapsed while
      the websocket was unavailable.
    - Does not fabricate missing state IDs or rewrite past predictions.
    """
    try:
        run_gap_experiment_if_needed(trigger="RECONNECT")
    except Exception as e:
        print("Reconnect GAP lab warning:", e)

    if WS_RECONNECT_REFRESH_HISTORY:
        try:
            load_history()
        except Exception as e:
            print(
                "Reconnect history refresh error:",
                e,
            )

    recovery_steps = (
        ("prediction", restore_overdue),
        ("surface", restore_surface_overdue),
        ("tradeability", restore_tradeability_overdue),
        ("COR1", restore_corridor_overdue),
        ("COR2", restore_corridor2_overdue),
        ("COR3", restore_corridor3_overdue),
    )

    for name, fn in recovery_steps:
        try:
            fn()
        except Exception as e:
            print(
                f"Reconnect {name} recovery error:",
                e,
            )


async def main():
    global state_id
    global candle_seq
    global last_close_time_ms
    global latest_execution_readiness
    global latest_geometric_stability_reversal
    global latest_execution_horizon_arbitration
    global latest_economic_front_surface
    global latest_bipolar_pressure
    global latest_geometry_testnet_bridge
    global latest_conditional_geometry_edge
    global latest_action_arbitration
    global latest_phase_front_lag
    global latest_gap_forecast
    global latest_gap_reconciliation
    global latest_model_residual

    print("MOR Trader v1.23")
    print("PFL1 + EFS1 + BPM1 BIPOLAR PRESSURE + GSR1 + EH1 + CGE1 + AAL1 + SCR1/GAP1/GRC1/RES1 SESSION GAP LAB + ERL1")
    print("PAIR:", SYMBOL)
    print("MARKET REST:", REST_BASE)
    print("MARKET WS:", WS_URL)
    print(
        "EXECUTION BRIDGE:",
        EXECUTION_READINESS_VERSION,
        f"mode={EXECUTION_MODE}",
        f"max_order={EXCHANGE_MAX_NOTIONAL_USDT:.2f}USDT",
        f"live_armed={LIVE_ARMED}",
        f"testnet_relax={TESTNET_RELAX_GATES}",
        f"aal1_testnet_override={TESTNET_ACTION_ARBITRATION}",
    )
    print(
        "TRADE PREDICTION HORIZON:",
        TRADE_HORIZON,
        "candles",
    )
    print(
        "SHADOW HORIZONS:",
        ",".join(
            str(x)
            for x in SHADOW_HORIZONS
        ),
        "candles",
    )
    print("PAPER START:", INITIAL_USDT, "USDT")
    print(
        "RISK:",
        f"trade={TRADE_FRACTION*100:.0f}%",
        f"max_btc={MAX_BTC_EXPOSURE*100:.0f}%",
        f"max_dd={MAX_DRAWDOWN_PCT:.1f}%",
    )
    print(
        "SIM COSTS:",
        f"fee={PAPER_FEE_RATE*100:.3f}%",
        f"slippage={PAPER_SLIPPAGE_RATE*100:.3f}%",
    )
    print(
        "SHADOW METRICS:",
        "directional + executable net economics",
    )
    print(
        "ADAPTIVE EDGE GATE:",
        f"min_samples={MIN_ADAPTIVE_TRADE_SAMPLES}/horizon",
        f"positive_edge_required={REQUIRE_VALIDATED_POSITIVE_EDGE}",
    )
    print(
        "PARAMETER SURFACE:",
        f"grid={SURFACE_GRID_VERSION}",
        (
            "ARCHIVED_RESOLVE_ONLY"
            if not SURFACE_RESEARCH_ENABLED
            else "ACTIVE"
        ),
    )
    print(
        "STATE REPRESENTATION:",
        STATE_REP_VERSION,
        f"history={FEATURE_HISTORY_CANDLES} candles",
    )
    print(
        "TRADEABILITY GATE:",
        f"enabled={TRADEABILITY_GATE_ENABLED}",
        f"score>={TRADEABILITY_SCORE_THRESHOLD:.2f}",
        f"break_even≈{roundtrip_buy_break_even_move_pct():.3f}%",
    )
    print(
        "VOLATILITY CORRIDORS:",
        CORRIDOR_VERSION,
        "windows=",
        ",".join(
            str(x)
            for x in CORRIDOR_WINDOWS
        ),
        "RESEARCH_ONLY=True",
    )
    print(
        "MULTI-LABEL CORRIDOR:",
        CORRIDOR2_VERSION,
        "flags+transitions",
        "RESEARCH_ONLY=True",
    )
    print(
        "SCALE/AGE TRANSITIONS:",
        CORRIDOR3_VERSION,
        "stable-log + cost-normalized + dwell-time",
        "RESEARCH_ONLY=True",
    )
    print(
        "GEOMETRY MEMORY:",
        GEOMETRY_VERSION,
        f"history={GEOMETRY_HISTORY_CANDLES}",
        "multiscale-extrema+structural-zones+dynamic-roles+spine",
        "RESEARCH_ONLY=True",
    )
    print(
        "GEOMETRY DASHBOARD:",
        GEOMETRY_DASHBOARD_FILE,
    )
    print(
        "FUTURE CONE:",
        CONE_VERSION,
        "elliptic slices H5/H15/H30/H60/H120",
        "VISUAL_ONLY=True",
    )
    print(
        "CONE MODEL:",
        CONE_MODEL_VERSION,
        "tilt+eccentricity+pressure+normalized-area+twist+curvature",
        "RESEARCH_ONLY=True",
    )
    print(
        "GEOMETRIC OUTCOME LEARNER:",
        GEOMETRY_OUTCOME_VERSION,
        "frozen-geometry -> H5/H15/H30/H60 path+terminal outcomes",
        "RESEARCH_ONLY=True",
    )
    print(
        "SCALE-SPACE PHASE TOPOLOGY:",
        PHASE_TOPOLOGY_VERSION,
        "domains+mixed-topology+moving-boundaries+sync-hyperevents",
        "RESEARCH_ONLY=True",
    )
    print(
        "PHASE BOUNDARY DYNAMICS:",
        PHASE_BOUNDARY_VERSION,
        "boundary id+velocity+acceleration+birth/annihilation",
        "RESEARCH_ONLY=True",
    )
    print(
        "TRANSITION EDGE GRAPH:",
        TRANSITION_EDGE_VERSION,
        "FROM->TO nodes + dt + omega + alpha + scale velocity",
        "RESEARCH_ONLY=True",
    )
    print(
        "TRANSITION OUTCOME MATRIX:",
        TRANSITION_OUTCOME_VERSION,
        "ordered scale transitions -> future outcomes",
        "RESEARCH_ONLY=True",
    )
    print(
        "CONE TRANSITION DYNAMICS:",
        CONE_DYNAMICS_VERSION,
        "theta+omega+alpha+cross-scale propagation",
        "RESEARCH_ONLY=True",
    )
    print(
        "GOL ZONE RELEVANCE:",
        f"near>={GOL_ZONE_NEAR_PRESSURE:.2f}",
        f"active>={GOL_ZONE_ACTIVE_PRESSURE:.2f}",
        "far=>NONE",
    )
    print(
        "STREAM RESILIENCE:",
        f"auto_reconnect={WS_RECONNECT_MIN_SECONDS}-{WS_RECONNECT_MAX_SECONDS}s",
        f"refresh_history={WS_RECONNECT_REFRESH_HISTORY}",
    )
    print(
        "SESSION CONTINUITY:",
        f"blind-before-reveal=True",
        f"separate_replay_plane=True",
        f"execution_during_replay=False",
    )
    print("-" * 60)

    load_runtime()
    start_session_continuity()
    print(
        "SESSION GAP LAB:",
        f"{SESSION_CONTINUITY_VERSION}+{GAP_HYPOTHESIS_VERSION}+{GAP_RECONCILIATION_VERSION}+{MODEL_RESIDUAL_VERSION}",
        f"session={session_continuity_tracker.get('current_session_id')}",
        f"min_gap={GAP_MIN_MINUTES}m",
        f"max_gap={GAP_MAX_MINUTES}m",
    )
    try:
        run_gap_experiment_if_needed(trigger="STARTUP")
    except Exception as e:
        print("SESSION GAP LAB startup warning:", e)
    load_execution_state()

    preflight = exchange_preflight(EXECUTION_MODE)
    print(
        "EXECUTION PREFLIGHT:",
        "OK" if preflight.get("preflight_ok") else "BLOCK",
        "|",
        preflight.get("preflight_reason"),
    )

    try:
        load_history()
    except Exception as e:
        print("History load error:", e)

    if candles:
        last_hist_price = candles[-1]["close"]
        snap = portfolio_snapshot(
            last_hist_price,
            f"X{state_id}",
            "STARTUP",
        )
        print(
            "Paper portfolio:",
            f"USDT={portfolio['usdt']:.2f}",
            f"BTC={portfolio['btc']:.8f}",
            f"equity={snap['equity_usdt']:.2f}",
        )

    try:
        restore_overdue()
    except Exception as e:
        print("Overdue recovery error:", e)

    try:
        restore_surface_overdue()
    except Exception as e:
        print(
            "Surface overdue recovery error:",
            e,
        )

    try:
        restore_tradeability_overdue()
    except Exception as e:
        print(
            "Tradeability recovery error:",
            e,
        )

    try:
        restore_corridor_overdue()
    except Exception as e:
        print(
            "Corridor recovery error:",
            e,
        )

    try:
        restore_corridor2_overdue()
    except Exception as e:
        print(
            "COR2 recovery error:",
            e,
        )

    try:
        restore_corridor3_overdue()
    except Exception as e:
        print(
            "COR3 recovery error:",
            e,
        )

    try:
        restore_geometry_outcome_overdue()
    except Exception as e:
        print(
            "GOL1 recovery error:",
            e,
        )

    print()

    reconnect_attempt = 0

    while True:
        try:
            async with websockets.connect(
                WS_URL,
                ping_interval=20,
                ping_timeout=20,
            ) as ws:

                while True:
                    msg = json.loads(
                        await ws.recv()
                    )

                    k = msg["k"]

                    # Only CLOSED candles become immutable market FACT.
                    if not k["x"]:
                        continue

                    current_close_time_ms = int(k["T"])

                    # Reconnect-safe duplicate guard.
                    if (
                        last_close_time_ms
                        and current_close_time_ms <= last_close_time_ms
                    ):
                        continue

                    previous_close_time_ms = int(last_close_time_ms or 0)
                    gap_from_prev_minutes = (
                        (current_close_time_ms - previous_close_time_ms) / float(MINUTE_MS)
                        if previous_close_time_ms else 1.0
                    )
                    last_close_time_ms = current_close_time_ms
                    candle_seq += 1

                    candle = {
                        "open_time_ms": int(k["t"]),
                        "open": float(k["o"]),
                        "high": float(k["h"]),
                        "low": float(k["l"]),
                        "close": float(k["c"]),
                        "volume": float(k["v"]),
                        "close_time_ms": int(k["T"]),
                    }

                    candles.append(candle)
                    state_id += 1

                    regime, trend, vol = classify_market()

                    state_features = (
                        compute_state_features()
                    )

                    corridor_features = (
                        compute_corridor_features()
                    )

                    corridor_multilabel = (
                        compute_corridor2_multilabel(
                            corridor_features,
                            state_features,
                        )
                    )

                    geometry_state = (
                        compute_geometry_layer(
                            corridor_features,
                            state_features,
                            candle["close_time_ms"],
                        )
                    )

                    corridor_multilabel = (
                        augment_corridor2_with_geometry(
                            corridor_multilabel,
                            geometry_state,
                        )
                    )

                    corridor_scale_age = (
                        compute_corridor3_scale_age(
                            corridor_features,
                            corridor_multilabel,
                            candle["close_time_ms"],
                        )
                    )

                    signals = strategy_signals(
                        regime,
                        trend,
                    )

                    (
                        strategy,
                        action,
                        probability,
                    ) = cognitive_loop(
                        regime,
                        signals,
                    )

                    edge_decision = edge_gate(
                        regime,
                        strategy,
                        action,
                    )

                    horizon_info = edge_decision[
                        "horizon_info"
                    ]

                    prediction_horizon = int(
                        horizon_info["horizon"]
                    )

                    tradeability_decision = (
                        tradeability_gate(
                            state_features,
                            prediction_horizon,
                            action,
                        )
                    )

                    if action != "HOLD":
                        adaptive_metrics[
                            "signals_seen"
                        ] += 1

                        # Calibration belongs to the horizon actually frozen.
                        probability = get_probability(
                            regime,
                            strategy,
                            prediction_horizon,
                        )

                    state = {
                        "state_id": f"X{state_id}",
                        "time": _iso_from_ms(candle["close_time_ms"]),
                        "market_time_ms": int(candle["close_time_ms"]),
                        "market_time": _iso_from_ms(candle["close_time_ms"]),
                        "processed_at": now_iso(),
                        "session_id": session_continuity_tracker.get("current_session_id"),
                        "gap_from_prev_minutes": round(float(gap_from_prev_minutes), 4),
                        "recovered": False,
                        "model_residual_context": dict(latest_model_residual),
                        "symbol": SYMBOL,
                        "price": candle["close"],
                        "regime": regime,
                        "trend_pct": round(trend, 5),
                        "volatility_pct": round(vol, 5),
                        "signals": signals,
                        "chosen_strategy": strategy,
                        "action": action,
                        "p_success": round(
                            probability,
                            4,
                        ),
                        "prediction_horizon":
                            prediction_horizon,
                        "state_representation":
                            STATE_REP_VERSION,
                        "state_features":
                            state_features,
                        "corridor_features":
                            corridor_features,
                        "corridor_multilabel":
                            corridor_multilabel,
                        "corridor_scale_age":
                            corridor_scale_age,
                        "geometry_state":
                            geometry_state,
                        "tradeability_gate":
                            tradeability_decision,
                        "edge_gate": {
                            "allowed":
                                edge_decision["allowed"],
                            "reason":
                                edge_decision["reason"],
                            "source":
                                horizon_info["source"],
                            "validated":
                                horizon_info["validated"],
                            "avg_trade_net_edge_pct":
                                horizon_info[
                                    "avg_trade_net_edge_pct"
                                ],
                            "min_samples_across_horizons":
                                horizon_info[
                                    "min_samples_across_horizons"
                                ],
                        },
                    }

                    latest_phase_front_lag = compute_phase_front_lag(
                        state, candle["close_time_ms"]
                    )
                    state["phase_front_lag"] = dict(latest_phase_front_lag)

                    latest_geometric_stability_reversal = compute_geometric_stability_reversal(
                        state
                    )
                    state["geometric_stability_reversal"] = dict(
                        latest_geometric_stability_reversal
                    )

                    latest_execution_horizon_arbitration = compute_execution_horizon_arbitration(
                        state
                    )
                    state["execution_horizon_arbitration"] = dict(
                        latest_execution_horizon_arbitration
                    )

                    latest_economic_front_surface = compute_economic_front_surface(
                        state, candle["close_time_ms"]
                    )
                    state["economic_front_surface"] = dict(
                        latest_economic_front_surface
                    )

                    latest_bipolar_pressure = compute_bipolar_pressure(
                        state, candle["close_time_ms"]
                    )
                    state["bipolar_pressure_model"] = dict(
                        latest_bipolar_pressure
                    )

                    # EH1 may change only the horizon of an existing BUY/SELL.
                    # HOLD stays HOLD; geometry does not manufacture an order.
                    if action in ("BUY", "SELL"):
                        eh_h = int(latest_execution_horizon_arbitration.get("execution_horizon", prediction_horizon))
                        if eh_h in SHADOW_HORIZONS:
                            prediction_horizon = eh_h
                            tradeability_decision = tradeability_gate(state_features, prediction_horizon, action)
                            edge_decision = edge_gate_at_horizon(regime, strategy, action, prediction_horizon)
                            horizon_info = edge_decision["horizon_info"]
                            probability = get_probability(regime, strategy, prediction_horizon)
                            state["prediction_horizon"] = prediction_horizon
                            state["p_success"] = round(probability, 4)
                            state["tradeability_gate"] = tradeability_decision
                            state["edge_gate"] = {
                                "allowed": edge_decision["allowed"],
                                "reason": edge_decision["reason"],
                                "source": horizon_info["source"],
                                "validated": horizon_info["validated"],
                                "avg_trade_net_edge_pct": horizon_info["avg_trade_net_edge_pct"],
                                "min_samples_across_horizons": horizon_info["min_samples_across_horizons"],
                            }

                    latest_conditional_geometry_edge = compute_conditional_geometry_edge(
                        state
                    )
                    state["conditional_geometry_edge"] = dict(
                        latest_conditional_geometry_edge
                    )

                    latest_action_arbitration = compute_action_arbitration(
                        state
                    )
                    state["action_arbitration"] = dict(
                        latest_action_arbitration
                    )

                    latest_geometry_testnet_bridge = compute_geometry_testnet_bridge(
                        state
                    )
                    state["geometry_testnet_bridge"] = dict(
                        latest_geometry_testnet_bridge
                    )

                    latest_execution_readiness = compute_execution_readiness(
                        state
                    )
                    state["execution_readiness"] = dict(
                        latest_execution_readiness
                    )

                    pre_portfolio = portfolio_metrics(
                        state["price"]
                    )

                    state["paper_portfolio_before"] = {
                        "usdt": round(
                            portfolio["usdt"], 8
                        ),
                        "btc": round(
                            portfolio["btc"], 12
                        ),
                        "equity_usdt": round(
                            pre_portfolio["equity_usdt"], 8
                        ),
                        "exposure_pct": round(
                            pre_portfolio["exposure_pct"], 5
                        ),
                        "drawdown_pct": round(
                            pre_portfolio["drawdown_pct"], 5
                        ),
                    }

                    print()
                    print(
                        f'[{state["state_id"]}]',
                        regime,
                        "|",
                        f'BTC={state["price"]:.2f}',
                    )

                    print(
                        f"trend={trend:+.5f}% | "
                        f"vol={vol:.5f}%"
                    )

                    if state_features.get(
                        "ready",
                        False,
                    ):
                        print(
                            "SR1:",
                            f"ATR14={state_features['atr_14_pct']:.5f}%",
                            f"RV20={state_features['realized_vol_20_pct']:.5f}%",
                            f"VolZ={state_features['volume_z_20']:+.2f}",
                            f"DistSMA={state_features['distance_sma20_pct']:+.4f}%",
                            f"Comp={state_features['compression_5_20']:.2f}",
                        )

                        if action == "HOLD":
                            print(
                                "TRADEABILITY:",
                                f"H={prediction_horizon}",
                                "N/A(HOLD)",
                                f"required≈{state_features.get('required_move_buy_pct', 0.0):.5f}%",
                            )
                        else:
                            print(
                                "TRADEABILITY:",
                                f"H={prediction_horizon}",
                                f"budget={tradeability_decision['motion_budget_pct']:.5f}%",
                                f"required={tradeability_decision['required_move_pct']:.5f}%",
                                f"score={tradeability_decision['score']:.3f}",
                                (
                                    "ALLOW"
                                    if tradeability_decision["allowed"]
                                    else "BLOCK"
                                ),
                            )
                    else:
                        print(
                            "SR1: WARMUP",
                            state_features.get(
                                "history_length",
                                0,
                            ),
                        )

                    if corridor_features.get(
                        "ready",
                        False,
                    ):
                        c30 = corridor_features[
                            "windows"
                        ]["30"]
                        c120 = corridor_features[
                            "windows"
                        ]["120"]

                        print(
                            "COR1:",
                            corridor_features[
                                "state_label"
                            ],
                            f"W30={c30['width_pct']:.4f}%",
                            f"CNR30={c30['cost_normalized_width']:.2f}",
                            f"pos30={c30['position']:.2f}",
                            f"exp30={c30['expansion_ratio']:.2f}",
                            f"W120={c120['width_pct']:.4f}%",
                        )

                        print(
                            "NESTING:",
                            f"W5/W30={corridor_features['ratios']['width_5_30']:.3f}",
                            f"W15/W60={corridor_features['ratios']['width_15_60']:.3f}",
                            f"W30/W120={corridor_features['ratios']['width_30_120']:.3f}",
                        )

                        print(
                            "COR2 FLAGS:",
                            (
                                ",".join(
                                    corridor_multilabel[
                                        "active_flags"
                                    ][:8]
                                )
                                if corridor_multilabel.get(
                                    "active_flags"
                                )
                                else "NONE"
                            ),
                            (
                                f"...(+{len(corridor_multilabel.get('active_flags', []))-8})"
                                if len(
                                    corridor_multilabel.get(
                                        "active_flags",
                                        [],
                                    )
                                ) > 8
                                else ""
                            ),
                        )

                        if corridor_multilabel.get(
                            "transitions"
                        ):
                            print(
                                "COR2 TRANS:",
                                ",".join(
                                    corridor_multilabel[
                                        "transitions"
                                    ]
                                ),
                            )

                        print(
                            "COR2 SIG:",
                            corridor_multilabel.get(
                                "signature",
                                "BASE",
                            ),
                        )

                        if corridor_scale_age.get(
                            "ready",
                            False,
                        ):
                            age_info = (
                                corridor_scale_age[
                                    "ages"
                                ]
                            )

                            dom_w = (
                                corridor_scale_age[
                                    "dominant_window"
                                ]
                            )

                            dom = (
                                corridor_scale_age[
                                    "scale_by_window"
                                ][str(dom_w)]
                            )

                            print(
                                "COR3 SCALE:",
                                f"W{dom_w}",
                                f"raw={dom['raw_expansion_ratio']:.2f}x",
                                f"log={dom['stable_log_ratio']:+.3f}",
                                f"dCost={dom['delta_cost_normalized']:+.3f}x",
                                dom[
                                    "structural_band"
                                ],
                                dom[
                                    "economic_band"
                                ],
                            )

                            print(
                                "COR3 AGE:",
                                f"sig={age_info.get('signature_age_minutes', 0):.1f}m",
                                "since_transition=",
                                (
                                    "n/a"
                                    if age_info.get(
                                        "transition_age_minutes"
                                    ) is None
                                    else
                                    f"{age_info.get('transition_age_minutes'):.1f}m"
                                ),
                            )

                            print_geometry_layer(
                                geometry_state
                            )

                            print_cone_model(
                                geometry_state
                            )
                    else:
                        print(
                            "COR1: WARMUP",
                            corridor_features.get(
                                "history_length",
                                0,
                            ),
                        )

                    print(
                        "signals:",
                        signals,
                    )

                    print(
                        "LOOP:",
                        strategy,
                        "=>",
                        action,
                        "| P=",
                        round(
                            probability,
                            4,
                        ),
                    )

                    if action != "HOLD":
                        print(
                            "EDGE GATE:",
                            "ALLOW"
                            if edge_decision["allowed"]
                            else "BLOCK",
                            "|",
                            edge_decision["reason"],
                            "| H=",
                            prediction_horizon,
                            "| source=",
                            horizon_info["source"],
                            "| edge=",
                            horizon_info[
                                "avg_trade_net_edge_pct"
                            ],
                            "| min_n=",
                            horizon_info[
                                "min_samples_across_horizons"
                            ],
                        )

                        print_horizon_summary(
                            regime,
                            strategy,
                        )

                    print(
                        "PFL1:",
                        f"seq={latest_phase_front_lag.get('sequence_path','-')}",
                        f"front={latest_phase_front_lag.get('front_direction','NONE')}/{latest_phase_front_lag.get('propagation_mode','NONE')}",
                        f"H{latest_phase_front_lag.get('from_horizon',0)}→H{latest_phase_front_lag.get('front_horizon',0)}",
                        f"lag={latest_phase_front_lag.get('latency_minutes',0):.1f}m",
                        f"v={latest_phase_front_lag.get('velocity_log2h_per_min',0):+.3f}",
                        f"S={latest_phase_front_lag.get('strength',0):.2f}",
                    )

                    print(
                        "EFS1:",
                        f"peak=H{latest_economic_front_surface.get('peak_horizon',0)}",
                        f"dir={latest_economic_front_surface.get('peak_direction','NONE')}",
                        f"costCov={latest_economic_front_surface.get('peak_cost_coverage',0):.2f}x",
                        f"Q={latest_economic_front_surface.get('peak_quality',0):.2f}",
                        f"drift={latest_economic_front_surface.get('peak_drift','NONE')}",
                        f"state={latest_economic_front_surface.get('state','WARMUP')}",
                    )

                    print(
                        "BPM1:",
                        f"state={latest_bipolar_pressure.get('state','WARMUP')}",
                        f"q={latest_bipolar_pressure.get('q',0):+.3f}",
                        f"I={latest_bipolar_pressure.get('I',0):.3f}",
                        f"P={latest_bipolar_pressure.get('P',0):+.3f}",
                        f"T={latest_bipolar_pressure.get('tension',0):.3f}",
                        f"dq={latest_bipolar_pressure.get('dq_per_min',0):+.3f}/m",
                        f"cross={'YES' if latest_bipolar_pressure.get('zero_cross') else 'NO'}",
                    )

                    print(
                        "EH1:",
                        f"best={latest_execution_horizon_arbitration.get('selected_action','NONE')}@H{latest_execution_horizon_arbitration.get('selected_horizon',TRADE_HORIZON)}",
                        f"Q={latest_execution_horizon_arbitration.get('selected_score',0):.2f}",
                        f"status={latest_execution_horizon_arbitration.get('status','WARMUP')}",
                        f"raw={action}",
                        f"execH=H{latest_execution_horizon_arbitration.get('execution_horizon',prediction_horizon)}",
                    )

                    _cf = latest_geometric_stability_reversal.get("counterfactual", {})
                    _b = _cf.get("BUY", {}) if isinstance(_cf, dict) else {}
                    _s = _cf.get("SELL", {}) if isinstance(_cf, dict) else {}
                    print(
                        "GSR1:",
                        f"bias={latest_geometric_stability_reversal.get('geometry_preferred_action','NONE')}",
                        f"B={_b.get('continuation_index',0):.2f}/{_b.get('reversal_index',0):.2f}",
                        f"S={_s.get('continuation_index',0):.2f}/{_s.get('reversal_index',0):.2f}",
                        f"GI={latest_geometric_stability_reversal.get('continuation_index',0):.2f}",
                        f"RI={latest_geometric_stability_reversal.get('reversal_index',0):.2f}",
                        f"coh={latest_geometric_stability_reversal.get('rotation_coherence',0):.2f}",
                        f"persist={latest_geometric_stability_reversal.get('persistence',0):.2f}",
                        f"verdict={latest_geometric_stability_reversal.get('verdict','WARMUP')}",
                        "TESTNET_SAFE" if latest_geometric_stability_reversal.get("testnet_safe") else "BLOCK",
                    )

                    print(
                        "CGE1:",
                        f"best={latest_conditional_geometry_edge.get('selected_action','NONE')}@H{latest_conditional_geometry_edge.get('selected_horizon',TRADE_HORIZON)}",
                        f"score={latest_conditional_geometry_edge.get('selected_score',0):.2f}",
                        f"status={latest_conditional_geometry_edge.get('status','WARMUP')}",
                    )

                    print(
                        "AAL1:",
                        f"strategy={latest_action_arbitration.get('strategy_action','HOLD')}",
                        f"geo={latest_action_arbitration.get('geometry_action','NONE')}@H{latest_action_arbitration.get('geometry_horizon',TRADE_HORIZON)}",
                        f"final={latest_action_arbitration.get('final_action','HOLD')}",
                        f"status={latest_action_arbitration.get('status','WARMUP')}",
                        "blockers=" + (",".join(latest_action_arbitration.get("blockers", [])) or "NONE"),
                    )

                    if session_continuity_tracker.get("last_gap"):
                        _lg = session_continuity_tracker.get("last_gap", {})
                        print(
                            "GAPLAB:",
                            f"last={_lg.get('gap_id','-')}",
                            f"dur={int(_lg.get('gap_minutes',0) or 0)}m",
                            f"blindErr={float(_lg.get('blind_total_error',0) or 0):.3f}",
                            f"RESrel={float(model_residual_tracker.get('reliability',1) or 1):.3f}",
                        )

                    print(
                        "ERL1:",
                        f"mode={EXECUTION_MODE}",
                        f"score={latest_execution_readiness.get('score',0):.2f}",
                        "STRICT_READY" if latest_execution_readiness.get("strict_ready") else ("TESTNET_READY" if latest_execution_readiness.get("testnet_ready") else "BLOCK"),
                        f"geo={latest_execution_readiness.get('geometry_alignment',0)*100:.0f}%",
                        f"topo={latest_execution_readiness.get('topology_class','NONE')}",
                        f"eh={latest_execution_readiness.get('eh1_status','HOLD')}:{latest_execution_readiness.get('eh1_score',0):.2f}",
                        "blockers=" + (",".join(latest_execution_readiness.get("blockers", [])) or "NONE"),
                    )

                    print(
                        "PORTFOLIO:",
                        f"USDT={portfolio['usdt']:.2f}",
                        f"BTC={portfolio['btc']:.8f}",
                        f"equity={pre_portfolio['equity_usdt']:.2f}",
                        f"exp={pre_portfolio['exposure_pct']:.2f}%",
                        f"dd={pre_portfolio['drawdown_pct']:.4f}%",
                    )

                    append_jsonl(
                        STATES_FILE,
                        state,
                    )

                    append_jsonl(
                        PHASE_FRONT_STATES_FILE,
                        {
                            "state_id": state["state_id"],
                            "time": state["time"],
                            "symbol": SYMBOL,
                            "phase_front_lag": latest_phase_front_lag,
                        },
                    )

                    append_jsonl(
                        ECONOMIC_FRONT_STATES_FILE,
                        {
                            "state_id": state["state_id"],
                            "time": state["time"],
                            "symbol": SYMBOL,
                            "economic_front_surface": latest_economic_front_surface,
                        },
                    )

                    append_jsonl(
                        STATE_FEATURES_FILE,
                        {
                            "state_id":
                                state["state_id"],
                            "time":
                                state["time"],
                            "symbol":
                                SYMBOL,
                            "regime":
                                regime,
                            "features":
                                state_features,
                        },
                    )

                    append_jsonl(
                        CORRIDOR_STATES_FILE,
                        {
                            "state_id":
                                state["state_id"],
                            "time":
                                state["time"],
                            "symbol":
                                SYMBOL,
                            "regime":
                                regime,
                            "corridor_features":
                                corridor_features,
                        },
                    )

                    append_jsonl(
                        CORRIDOR2_STATES_FILE,
                        {
                            "state_id":
                                state["state_id"],
                            "time":
                                state["time"],
                            "symbol":
                                SYMBOL,
                            "regime":
                                regime,
                            "corridor_multilabel":
                                corridor_multilabel,
                            "corridor_features":
                                corridor_features,
                        },
                    )

                    append_jsonl(
                        CORRIDOR3_STATES_FILE,
                        {
                            "state_id":
                                state["state_id"],
                            "time":
                                state["time"],
                            "symbol":
                                SYMBOL,
                            "regime":
                                regime,
                            "corridor_scale_age":
                                corridor_scale_age,
                            "corridor_multilabel":
                                corridor_multilabel,
                        },
                    )

                    append_jsonl(
                        GEOMETRY_STATES_FILE,
                        {
                            "state_id":
                                state["state_id"],
                            "time":
                                state["time"],
                            "symbol":
                                SYMBOL,
                            "geometry":
                                geometry_state,
                        },
                    )

                    if geometry_state.get(
                        "transitions"
                    ):
                        append_jsonl(
                            GEOMETRY_EVENTS_FILE,
                            {
                                "state_id":
                                    state["state_id"],
                                "time":
                                    state["time"],
                                "symbol":
                                    SYMBOL,
                                "transitions":
                                    geometry_state[
                                        "transitions"
                                    ],
                                "signature":
                                    geometry_state.get(
                                        "signature",
                                        "GEO_BASE",
                                    ),
                            },
                        )

                    cone_state = geometry_state.get(
                        "cone_model",
                        {},
                    )

                    if cone_state.get(
                        "ready",
                        False,
                    ):
                        append_jsonl(
                            CONE_MODEL_STATES_FILE,
                            {
                                "state_id":
                                    state["state_id"],
                                "time":
                                    state["time"],
                                "symbol":
                                    SYMBOL,
                                "cone_model":
                                    cone_state,
                            },
                        )

                        if cone_state.get(
                            "transitions"
                        ):
                            append_jsonl(
                                CONE_MODEL_EVENTS_FILE,
                                {
                                    "state_id":
                                        state["state_id"],
                                    "time":
                                        state["time"],
                                    "symbol":
                                        SYMBOL,
                                    "transitions":
                                        cone_state[
                                            "transitions"
                                        ],
                                    "signature":
                                        cone_state.get(
                                            "signature",
                                            "CONE_BASE",
                                        ),
                                },
                            )

                    try:
                        write_geometry_dashboard(
                            geometry_state,
                            state["state_id"],
                            state["time"],
                        )
                    except Exception as e:
                        print(
                            "Geometry dashboard error:",
                            e,
                        )

                    # 1) Resolve experiments whose horizon is reached.
                    evaluate_due(
                        state["price"],
                        candle["close_time_ms"],
                    )

                    # Parallel v0.9 research lab.
                    # It does not alter action/edge gate/risk governor.
                    evaluate_surface_due(
                        state["price"],
                        candle["close_time_ms"],
                    )

                    evaluate_tradeability_due(
                        state["price"],
                        candle["close_time_ms"],
                    )

                    evaluate_corridor_due(
                        state["price"],
                        candle["close_time_ms"],
                    )

                    evaluate_corridor2_due(
                        state["price"],
                        candle["close_time_ms"],
                    )

                    evaluate_corridor3_due(
                        state["price"],
                        candle["close_time_ms"],
                    )

                    evaluate_geometry_outcome_due(
                        candle
                    )

                    # 2) Freeze a prediction whenever the strategy proposes
                    #    BUY/SELL. This remains an experiment even if the
                    #    economic edge gate blocks paper execution.
                    executed_action = "HOLD"
                    execution_status = "NO_ORDER"
                    execution_reason = "HOLD"

                    if action != "HOLD":
                        freeze_prediction(
                            state,
                            strategy,
                            action,
                            probability,
                            candle["close_time_ms"],
                            horizon_candles=
                                prediction_horizon,
                        )

                        if (
                            (
                                tradeability_decision["allowed"]
                                and edge_decision["allowed"]
                            )
                            or (
                                EXECUTION_MODE == "TESTNET"
                                and TESTNET_RELAX_GATES
                                and latest_execution_readiness.get("testnet_ready", False)
                            )
                        ):
                            # Risk Governor remains the final independent hard layer.
                            if EXECUTION_MODE == "PAPER":
                                paper_event = (
                                    execute_paper_order(
                                        state
                                    )
                                )
                            else:
                                paper_event = (
                                    execute_exchange_order(
                                        state,
                                        EXECUTION_MODE,
                                    )
                                )
                        elif not tradeability_decision[
                            "allowed"
                        ]:
                            paper_event = {
                                "time": now_iso(),
                                "state_id":
                                    state["state_id"],
                                "strategy":
                                    state[
                                        "chosen_strategy"
                                    ],
                                "action": action,
                                "status":
                                    "BLOCKED_TRADEABILITY_GATE",
                                "reason":
                                    tradeability_decision[
                                        "reason"
                                    ],
                                "prediction_horizon":
                                    prediction_horizon,
                                "tradeability_score":
                                    tradeability_decision[
                                        "score"
                                    ],
                                "motion_budget_pct":
                                    tradeability_decision[
                                        "motion_budget_pct"
                                    ],
                                "required_move_pct":
                                    tradeability_decision[
                                        "required_move_pct"
                                    ],
                            }

                            append_jsonl(
                                PAPER_TRADES_FILE,
                                paper_event,
                            )

                            print(
                                "TRADEABILITY GOVERNOR:",
                                action,
                                "BLOCKED |",
                                paper_event["reason"],
                                "| score=",
                                paper_event[
                                    "tradeability_score"
                                ],
                            )
                        else:
                            paper_event = {
                                "time": now_iso(),
                                "state_id":
                                    state["state_id"],
                                "strategy":
                                    state[
                                        "chosen_strategy"
                                    ],
                                "action": action,
                                "status":
                                    "BLOCKED_EDGE_GATE",
                                "reason":
                                    edge_decision[
                                        "reason"
                                    ],
                                "prediction_horizon":
                                    prediction_horizon,
                                "matrix_edge_pct":
                                    horizon_info[
                                        "avg_trade_net_edge_pct"
                                    ],
                            }

                            append_jsonl(
                                PAPER_TRADES_FILE,
                                paper_event,
                            )

                            print(
                                "EDGE GOVERNOR:",
                                action,
                                "BLOCKED |",
                                paper_event["reason"],
                            )

                        execution_status = (
                            paper_event.get(
                                "status",
                                "UNKNOWN",
                            )
                        )

                        execution_reason = (
                            paper_event.get(
                                "reason",
                                "",
                            )
                        )

                        if (
                            execution_status
                            in ("FILLED_PAPER", "FILLED_TESTNET", "FILLED_LIVE")
                        ):
                            executed_action = (
                                paper_event.get(
                                    "action",
                                    "HOLD",
                                )
                            )
                    else:
                        portfolio_snapshot(
                            state["price"],
                            state["state_id"],
                            "HOLD",
                        )

                    # 3) Freeze FOUR independent counterfactual horizons.
                    #    Their predictions are fixed now and mature at
                    #    5/15/30/60 closed candles without post-hoc rewriting.
                    create_multi_horizon_shadows(
                        state,
                        candle["close_time_ms"],
                        executed_action=
                            executed_action,
                        execution_status=
                            execution_status,
                        execution_reason=
                            execution_reason,
                    )

                    # v0.9 parameter surface is now archived.
                    # Existing frozen probes continue resolving, but SR1 does
                    # not generate new PS1 probes.
                    if SURFACE_RESEARCH_ENABLED:
                        create_multi_horizon_surface_probes(
                            state,
                            candle["close_time_ms"],
                        )

                    # v1.0 freezes the expanded state representation at
                    # 5/15/30/60 horizons before future outcomes are known.
                    create_multi_horizon_tradeability_probes(
                        state,
                        candle["close_time_ms"],
                    )

                    create_multi_horizon_corridor_probes(
                        state,
                        candle["close_time_ms"],
                    )

                    create_multi_horizon_corridor2_probes(
                        state,
                        candle["close_time_ms"],
                    )

                    create_multi_horizon_corridor3_probes(
                        state,
                        candle["close_time_ms"],
                    )

                    create_multi_horizon_geometry_outcome_probes(
                        state,
                        candle["close_time_ms"],
                    )

                    # Universe Lab lightweight operator snapshot.
                    try:
                        write_observer_status(state)
                    except Exception as e:
                        print("OBSERVER STATUS warning:", e)

                    # MORX1 compact export is refreshed every closed candle so
                    # the operator can upload one JSON instead of screenshots.
                    try:
                        export_info = write_analysis_export(state, full=False, also_download=True)
                        if state_id % 10 == 0:
                            print(
                                "MORX1:",
                                "latest=", export_info.get("download"),
                                "OK" if export_info.get("ok_download") else "LOCAL_ONLY",
                            )
                    except Exception as e:
                        print("MORX1 export warning:", e)

                    save_runtime()

                    if (
                        SURFACE_RESEARCH_ENABLED
                        and state_id
                        % SURFACE_SUMMARY_EVERY_STATES
                        == 0
                    ):
                        print_surface_summary()

                    if (
                        state_id
                        % TRADEABILITY_SUMMARY_EVERY_STATES
                        == 0
                    ):
                        print_tradeability_summary()

                    if (
                        state_id
                        % CORRIDOR_SUMMARY_EVERY_STATES
                        == 0
                    ):
                        print_corridor_summary()

                    if (
                        state_id
                        % CORRIDOR2_SUMMARY_EVERY_STATES
                        == 0
                    ):
                        print_corridor2_summary()

                    if (
                        state_id
                        % CORRIDOR3_SUMMARY_EVERY_STATES
                        == 0
                    ):
                        print_corridor3_summary()

                    if (
                        state_id
                        % GEOMETRY_OUTCOME_SUMMARY_EVERY_STATES
                        == 0
                    ):
                        print_geometry_outcome_summary()

                    print(
                        "pending:",
                        len(pending_predictions),
                        "| shadows:",
                        len(pending_shadows),
                        "| matrix_cells:",
                        len(horizon_matrix),
                        "| surface:",
                        len(pending_surface_shadows),
                        "| surface_cells:",
                        len(parameter_surface),
                        "| TG:",
                        len(pending_tradeability_probes),
                        "| TG_cells:",
                        len(
                            tradeability_feature_matrix
                        ),
                        "| COR:",
                        len(
                            pending_corridor_probes
                        ),
                        "| COR_cells:",
                        len(
                            corridor_feature_matrix
                        ),
                        "| COR2:",
                        len(
                            pending_corridor2_probes
                        ),
                        "| COR2_cells:",
                        len(
                            corridor2_feature_matrix
                        ),
                        "| COR3:",
                        len(
                            pending_corridor3_probes
                        ),
                        "| COR3_cells:",
                        len(
                            corridor3_feature_matrix
                        ),
                        "| GEO:",
                        (
                            "READY"
                            if geometry_state.get(
                                "ready",
                                False,
                            )
                            else "WARMUP"
                        ),
                        "| GEO_levels:",
                        len(
                            geometry_state.get(
                                "levels",
                                [],
                            )
                        ),
                        "| CONE:",
                        (
                            "READY"
                            if geometry_state.get(
                                "cone_model",
                                {},
                            ).get(
                                "ready",
                                False,
                            )
                            else "WARMUP"
                        ),
                        "| CONE_flags:",
                        len(
                            geometry_state.get(
                                "cone_model",
                                {},
                            ).get(
                                "active_flags",
                                [],
                            )
                        ),
                        "| ZONES:",
                        len(
                            geometry_state.get(
                                "structural_zones",
                                [],
                            )
                        ),
                        "| GOL:",
                        len(
                            pending_geometry_outcome_probes
                        ),
                        "| GOL_cells:",
                        len(
                            geometry_outcome_matrix
                        ),
                        "| TOM_cells:",
                        len(
                            transition_outcome_matrix
                        ),
                        "| TEG_cells:",
                        len(
                            transition_edge_matrix
                        ),
                        "| SPT:",
                        geometry_state.get(
                            "cone_transition_dynamics",
                            {},
                        ).get(
                            "phase_topology",
                            {},
                        ).get(
                            "topology_class",
                            "UNRESOLVED_BOUNDARY_STATE",
                        ),
                        "| EFS1:",
                        (
                            f"H{latest_economic_front_surface.get('peak_horizon',0)}@{latest_economic_front_surface.get('peak_cost_coverage',0):.2f}x"
                            if latest_economic_front_surface.get("ready")
                            else "WARMUP"
                        ),
                        "| CTD:",
                        geometry_state.get(
                            "cone_transition_dynamics",
                            {},
                        ).get(
                            "signature",
                            "CTD_BASE",
                        ),
                    )

                    print("-" * 60)

                reconnect_attempt = 0

        except asyncio.CancelledError:
            raise

        except KeyboardInterrupt:
            raise

        except Exception as e:
            reconnect_attempt += 1

            delay = min(
                WS_RECONNECT_MAX_SECONDS,
                max(
                    WS_RECONNECT_MIN_SECONDS,
                    2 ** min(
                        reconnect_attempt,
                        5,
                    ),
                ),
            )

            print()
            print(
                "STREAM INTERRUPTED:",
                type(e).__name__,
                "|",
                str(e),
            )

            print(
                "Runtime preserved.",
                f"Reconnect in {delay}s...",
            )

            try:
                save_runtime()
            except Exception as save_error:
                print(
                    "Runtime save during reconnect failed:",
                    save_error,
                )

            await asyncio.sleep(delay)

            recover_research_after_stream_gap()

            print(
                "Reconnecting websocket..."
            )


if __name__ == "__main__":
    if "--bpm-selftest" in sys.argv:
        result = run_bpm_selftest()
        print("MOR v1.23 BPM1 SELFTEST")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(0 if result.get("ok") else 1)

    if "--gap-selftest" in sys.argv:
        result = run_gap_selftest()
        print("MOR v1.23 GAP LAB SELFTEST")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(0 if result.get("ok") else 1)

    if "--export" in sys.argv or "--export-full" in sys.argv:
        # Offline export mode: no websocket and no order path.
        load_runtime()
        info = write_analysis_export(
            state=_latest_state_from_storage(),
            full=True,
            also_download=True,
        )
        print("MORX1 FULL EXPORT")
        print("local:", info.get("local"))
        print("download:", info.get("download"))
        print("status:", "OK" if info.get("ok_download") else "DOWNLOAD_PATH_FAILED; local copy exists")
        raise SystemExit(0)

    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        save_runtime()
        print()
        print("Runtime saved.")
        print("MOR Trader v1.23 stopped safely.")
