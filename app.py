"""
Dólar Tracker — Oficial vs MEP vs CCL (minuto a minuto)
========================================================

Webapp en Streamlit que compara la cotización del dólar Oficial, MEP y CCL,
calcula las brechas en % y persiste cada lectura en SQLite para reconstruir
la serie intradiaria. Objetivo: detectar cuándo conviene comprar CCL para
invertir afuera (mirando el nivel del CCL y el "canje" CCL-MEP).

Fuentes de datos (gratuitas, sin API key):
  - dolarapi.com   -> dólar oficial / mayorista (referencia BCRA)
  - data912.com    -> MEP y CCL calculados POR INSTRUMENTO (refresh ~20s)

Cómo correr:
  pip install -r requirements.txt
  streamlit run app.py

Nota sobre "minuto a minuto":
  La app guarda un punto cada vez que se refresca, mientras la tenés abierta.
  Si querés captura continua 24/7 sin la pestaña abierta, corré el bloque
  capture_and_store() desde un script aparte vía cron/proceso, apuntando a
  la MISMA base SQLite (DB_PATH). La app lo va a leer igual.
"""

import sqlite3
from datetime import datetime, date
from pathlib import Path

import pandas as pd
import requests
import streamlit as st
from plotly.subplots import make_subplots
import plotly.graph_objects as go
from streamlit_autorefresh import st_autorefresh

# --------------------------------------------------------------------------- #
# CONFIG
# --------------------------------------------------------------------------- #
DB_PATH = Path(__file__).parent / "dolar_historico.sqlite"
TIMEOUT = 8  # segundos para los requests

DOLARAPI = "https://dolarapi.com/v1/dolares"   # /oficial, /mayorista
DATA912 = "https://data912.com/live"           # /mep, /ccl
DATA912_HIST = "https://data912.com/historical/bonds"  # /AL30, /AL30C, /AL30D...
ARGENTINADATOS = "https://api.argentinadatos.com/v1/cotizaciones/dolares"  # oficial histórico

# Instrumentos habilitados (los más líquidos para MEP/CCL).
TICKERS = ["AL30", "GD30"]
DEFAULT_MEP_TICKER = "AL30"
DEFAULT_CCL_TICKER = "AL30"

st.set_page_config(page_title="Dólar Tracker — Oficial/MEP/CCL",
                   page_icon="💵", layout="wide")


# --------------------------------------------------------------------------- #
# BASE DE DATOS
# --------------------------------------------------------------------------- #
def init_db() -> None:
    """Crea la tabla si no existe."""
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS cotizaciones (
                ts        TEXT PRIMARY KEY,   -- timestamp local ISO
                oficial   REAL,
                mep       REAL,
                ccl       REAL,
                mep_tk    TEXT,               -- instrumento usado para MEP
                ccl_tk    TEXT                -- instrumento usado para CCL
            )
        """)


def store_row(ts: str, oficial, mep, ccl, mep_tk, ccl_tk, replace: bool = False) -> None:
    """Inserta una lectura.
    replace=False (live): OR IGNORE evita duplicar el mismo timestamp.
    replace=True (backfill): OR REPLACE permite recargar cierres con otro instrumento."""
    verb = "REPLACE" if replace else "IGNORE"
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            f"INSERT OR {verb} INTO cotizaciones VALUES (?,?,?,?,?,?)",
            (ts, oficial, mep, ccl, mep_tk, ccl_tk),
        )


def load_history(days: int | None = None) -> pd.DataFrame:
    """Devuelve el histórico como DataFrame.

    days=1  -> sólo hoy
    days=N  -> últimos N días (incluyendo hoy)
    days=None -> todo el histórico
    """
    with sqlite3.connect(DB_PATH) as con:
        df = pd.read_sql_query("SELECT * FROM cotizaciones ORDER BY ts", con)
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["ts"])
    if days is not None:
        cutoff = pd.Timestamp.now().normalize() - pd.Timedelta(days=days - 1)
        df = df[df["ts"] >= cutoff]
    # Brechas calculadas sobre la marcha
    df["brecha_mep"] = (df["mep"] - df["oficial"]) / df["oficial"] * 100
    df["brecha_ccl"] = (df["ccl"] - df["oficial"]) / df["oficial"] * 100
    df["canje"] = (df["ccl"] - df["mep"]) / df["mep"] * 100  # CCL vs MEP
    return df


def resample_df(df: pd.DataFrame, rule: str | None) -> pd.DataFrame:
    """Promedia los puntos por intervalo (ej. '5min', '15min', '1h').
    rule=None devuelve los datos crudos. Útil para que el gráfico no se sature
    cuando hay varios días de capturas minuto a minuto."""
    if df.empty or rule is None:
        return df
    out = (df.set_index("ts")[["oficial", "mep", "ccl"]]
             .resample(rule).mean().dropna(how="all").reset_index())
    out["brecha_mep"] = (out["mep"] - out["oficial"]) / out["oficial"] * 100
    out["brecha_ccl"] = (out["ccl"] - out["oficial"]) / out["oficial"] * 100
    out["canje"] = (out["ccl"] - out["mep"]) / out["mep"] * 100
    return out


# --------------------------------------------------------------------------- #
# FETCH DE DATOS
# --------------------------------------------------------------------------- #
def fetch_oficial(tipo: str = "oficial") -> float | None:
    """Dólar oficial o mayorista desde dolarapi. Devuelve el valor de venta."""
    try:
        r = requests.get(f"{DOLARAPI}/{tipo}", timeout=TIMEOUT)
        r.raise_for_status()
        return float(r.json()["venta"])
    except Exception as e:
        st.warning(f"No pude traer el oficial ({tipo}): {e}")
        return None


@st.cache_data(ttl=15)  # cache corto: data912 refresca cada ~20s
def fetch_data912(endpoint: str) -> list:
    """Trae un panel live de data912 (/mep o /ccl). Devuelve lista de dicts."""
    try:
        r = requests.get(f"{DATA912}/{endpoint}", timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        st.warning(f"No pude traer data912/{endpoint}: {e}")
        return []


def bond_price(panel: list, symbol: str) -> float | None:
    """Último precio de una especie en el panel de bonos. Tolerante a nombres de campo:
    prueba 'c'/'mark'/'close' y, si no, promedia bid/ask."""
    for row in panel:
        if row.get("symbol") == symbol or row.get("ticker") == symbol:
            for key in ("c", "mark", "close", "last"):
                val = row.get(key)
                if val:
                    return float(val)
            bid = row.get("px_bid") or row.get("bid") or 0
            ask = row.get("px_ask") or row.get("ask") or 0
            if bid and ask:
                return (float(bid) + float(ask)) / 2
    return None


def implied_rate(panel: list, base: str, especie: str) -> float | None:
    """Tipo de cambio implícito = precio en pesos / precio en la especie dólar.
    MEP usa la especie 'D' (ej AL30D); CCL usa la especie 'C' (ej AL30C).
    Mismo método que el backfill histórico, por eso son consistentes."""
    pesos = bond_price(panel, base)
    dolar = bond_price(panel, especie)
    if pesos and dolar:
        return pesos / dolar
    return None


@st.cache_data(ttl=3600)
def fetch_bond_hist(ticker: str) -> dict:
    """Histórico OHLC diario de un bono/especie. Devuelve {fecha 'YYYY-MM-DD': cierre}."""
    try:
        r = requests.get(f"{DATA912_HIST}/{ticker}", timeout=20)
        r.raise_for_status()
        out = {}
        for row in r.json():
            d = str(row.get("date"))[:10]      # normalizo a YYYY-MM-DD
            c = row.get("c")                   # 'c' = cierre
            if d and c:
                out[d] = float(c)
        return out
    except Exception as e:
        st.warning(f"No pude traer histórico de {ticker}: {e}")
        return {}


@st.cache_data(ttl=3600)
def fetch_oficial_hist(tipo: str) -> dict:
    """Histórico del dólar oficial/mayorista (argentinadatos). Devuelve {fecha: venta}."""
    try:
        r = requests.get(f"{ARGENTINADATOS}/{tipo}", timeout=20)
        r.raise_for_status()
        out = {}
        for row in r.json():
            d = str(row.get("fecha"))[:10]
            v = row.get("venta")
            if d and v:
                out[d] = float(v)
        return out
    except Exception as e:
        st.warning(f"No pude traer histórico oficial ({tipo}): {e}")
        return {}


def backfill(oficial_tipo: str, mep_tk: str, ccl_tk: str) -> int:
    """Reconstruye cierres diarios pasados y los inserta en la base.

    MEP = cierre(base, pesos) / cierre(especie D, dólar MEP)
    CCL = cierre(base, pesos) / cierre(especie C, dólar cable)
    Sólo guarda fechas donde están los tres valores (MEP, CCL y oficial).
    Devuelve la cantidad de días cargados.
    """
    mep_base = fetch_bond_hist(mep_tk)            # ej AL30 (pesos)
    mep_d = fetch_bond_hist(mep_tk + "D")         # ej AL30D
    ccl_base = fetch_bond_hist(ccl_tk)            # ej AL30 (pesos)
    ccl_c = fetch_bond_hist(ccl_tk + "C")         # ej AL30C
    oficial = fetch_oficial_hist(oficial_tipo)

    if not all([mep_base, mep_d, ccl_base, ccl_c, oficial]):
        return 0

    fechas = set(mep_base) & set(mep_d) & set(ccl_base) & set(ccl_c) & set(oficial)
    n = 0
    for f in sorted(fechas):
        if not (mep_d[f] and ccl_c[f]):
            continue
        mep = mep_base[f] / mep_d[f]
        ccl = ccl_base[f] / ccl_c[f]
        # ts al cierre del día (fuera de rueda) para no chocar con puntos intradía
        store_row(f + "T23:59:00", oficial[f], mep, ccl, mep_tk, ccl_tk, replace=True)
        n += 1
    return n


def capture_and_store(oficial_tipo: str, mep_tk: str, ccl_tk: str) -> dict:
    """Captura los 3 valores actuales, los guarda y los devuelve.
    MEP/CCL se calculan desde el panel de bonos (mismo método que el histórico)."""
    oficial = fetch_oficial(oficial_tipo)
    bonds = fetch_data912("arg_bonds")
    mep = implied_rate(bonds, mep_tk, mep_tk + "D")   # ej AL30 / AL30D
    ccl = implied_rate(bonds, ccl_tk, ccl_tk + "C")   # ej AL30 / AL30C

    ts = datetime.now().isoformat(timespec="seconds")
    if None not in (oficial, mep, ccl):
        store_row(ts, oficial, mep, ccl, mep_tk, ccl_tk)

    return {"ts": ts, "oficial": oficial, "mep": mep, "ccl": ccl}


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
init_db()
st.title("💵 Dólar Tracker — Oficial · MEP · CCL")
st.caption("Comparación intradiaria con brechas. CCL/MEP por instrumento vía data912; "
           "oficial vía dolarapi. Los datos no son tiempo real (refresh ~20s).")

# --- Sidebar: controles ---------------------------------------------------- #
with st.sidebar:
    st.header("⚙️ Configuración")
    refresh_seg = st.slider("Intervalo de refresh (seg)", 30, 300, 60, step=10)
    oficial_tipo = st.selectbox("Referencia oficial", ["oficial", "mayorista"], index=0)

    mep_tk = st.selectbox("Instrumento MEP", TICKERS,
                          index=TICKERS.index(DEFAULT_MEP_TICKER))
    ccl_tk = st.selectbox("Instrumento CCL", TICKERS,
                          index=TICKERS.index(DEFAULT_CCL_TICKER))

    st.divider()
    st.subheader("📅 Histórico")
    rango_opts = {"Hoy": 1, "Últimos 3 días": 3, "Últimos 7 días": 7,
                  "Últimos 30 días": 30, "Todo": None}
    rango_label = st.selectbox("Rango a mostrar", list(rango_opts.keys()), index=2)
    rango_days = rango_opts[rango_label]

    gran_opts = {"Auto": "auto", "Crudo (1 min)": None, "5 min": "5min",
                 "15 min": "15min", "1 hora": "1h"}
    gran_label = st.selectbox("Granularidad del gráfico", list(gran_opts.keys()), index=0)
    gran_rule = gran_opts[gran_label]

    st.divider()
    st.subheader("⬇️ Backfill")
    st.caption("Carga cierres diarios pasados (AL30/GD30) recalculando el implícito. "
               "Con una vez alcanza; podés repetirlo para actualizar.")
    if st.button("Cargar histórico diario"):
        with st.spinner("Bajando cierres diarios de data912 + oficial…"):
            n = backfill(oficial_tipo, mep_tk, ccl_tk)
        if n:
            st.success(f"Cargué {n} cierres diarios.")
            st.rerun()
        else:
            st.error("No pude cargar el histórico (endpoints caídos o tickers sin datos).")

    st.divider()
    if st.button("🗑️ Borrar histórico"):
        with sqlite3.connect(DB_PATH) as con:
            con.execute("DELETE FROM cotizaciones")
        st.success("Histórico borrado.")

# --- Autorefresh ----------------------------------------------------------- #
# Devuelve un contador que incrementa en cada refresh automático. Lo usamos
# para capturar SÓLO en ticks reales del timer (no cuando tocás un widget).
tick = st_autorefresh(interval=refresh_seg * 1000, key="auto")

if st.session_state.get("last_tick") != tick:
    st.session_state["last_tick"] = tick
    snap = capture_and_store(oficial_tipo, mep_tk, ccl_tk)
else:
    # Captura igual en la primera carga / cambio de instrumento, sin duplicar tick
    snap = capture_and_store(oficial_tipo, mep_tk, ccl_tk)

# --- Carga de datos -------------------------------------------------------- #
# 'today' (crudo) alimenta las métricas/deltas del momento.
# 'view' es el rango elegido por el usuario, resampleado para el gráfico.
today = load_history(days=1)


def auto_rule(days: int | None) -> str | None:
    if days == 1:
        return None
    if days is None or days > 7:
        return "1h"
    if days <= 3:
        return "5min"
    return "15min"


rule = auto_rule(rango_days) if gran_rule == "auto" else gran_rule
view = resample_df(load_history(days=rango_days), rule)


# --- Métricas (valor actual + delta vs lectura anterior) ------------------- #
def delta(col: str):
    if len(today) >= 2:
        return round(today[col].iloc[-1] - today[col].iloc[-2], 2)
    return None

c1, c2, c3, c4 = st.columns(4)
c1.metric(f"Oficial ({oficial_tipo})",
          f"${snap['oficial']:,.2f}" if snap['oficial'] else "s/d",
          delta(("oficial")))
c2.metric(f"MEP ({mep_tk})",
          f"${snap['mep']:,.2f}" if snap['mep'] else "s/d",
          delta("mep"))
c3.metric(f"CCL ({ccl_tk})",
          f"${snap['ccl']:,.2f}" if snap['ccl'] else "s/d",
          delta("ccl"))
if not today.empty:
    c4.metric("Canje CCL-MEP", f"{today['canje'].iloc[-1]:.2f}%",
              delta("canje"),
              help="Costo de pasar de MEP a CCL (sacar la plata afuera). "
                   "Más bajo = más barato el cable.")

# --- Brechas en texto ------------------------------------------------------ #
if not today.empty:
    last = today.iloc[-1]
    st.info(
        f"**Brecha MEP/oficial:** {last['brecha_mep']:.1f}%  ·  "
        f"**Brecha CCL/oficial:** {last['brecha_ccl']:.1f}%  ·  "
        f"**Canje CCL-MEP:** {last['canje']:.2f}%"
    )

# --- Gráfico --------------------------------------------------------------- #
if view.empty or len(view) < 2:
    st.warning("Todavía no hay suficientes puntos para graficar en este rango. "
               "Dejá la app abierta para ir acumulando la serie.")
else:
    titulo = f"Cotización nominal (ARS) — {rango_label.lower()}"
    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.08,
        row_heights=[0.6, 0.4],
        subplot_titles=(titulo, "Brechas (%)"),
    )
    # Panel 1: nominal
    fig.add_trace(go.Scatter(x=view["ts"], y=view["oficial"], name="Oficial",
                             line=dict(color="#6b7280")), row=1, col=1)
    fig.add_trace(go.Scatter(x=view["ts"], y=view["mep"], name="MEP",
                             line=dict(color="#6366f1")), row=1, col=1)
    fig.add_trace(go.Scatter(x=view["ts"], y=view["ccl"], name="CCL",
                             line=dict(color="#22c55e")), row=1, col=1)
    # Panel 2: brechas
    fig.add_trace(go.Scatter(x=view["ts"], y=view["brecha_mep"], name="Brecha MEP",
                             line=dict(color="#6366f1", dash="dot")), row=2, col=1)
    fig.add_trace(go.Scatter(x=view["ts"], y=view["brecha_ccl"], name="Brecha CCL",
                             line=dict(color="#22c55e", dash="dot")), row=2, col=1)
    fig.add_trace(go.Scatter(x=view["ts"], y=view["canje"], name="Canje CCL-MEP",
                             line=dict(color="#f97316")), row=2, col=1)

    fig.update_layout(height=620, hovermode="x unified",
                      legend=dict(orientation="h", y=1.08))
    fig.update_yaxes(title_text="ARS", row=1, col=1)
    fig.update_yaxes(title_text="%", row=2, col=1)
    st.plotly_chart(fig, use_container_width=True)

    with st.expander("📋 Ver datos / descargar CSV"):
        st.dataframe(view.iloc[::-1], use_container_width=True)
        st.download_button("Descargar CSV", view.to_csv(index=False),
                           file_name=f"dolar_{rango_label}_{date.today()}.csv",
                           mime="text/csv")
