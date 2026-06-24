# Dolar-brecha
Visualizar las cotizaciones del dolar (USD/ARS). La brecha permite saber el costo de enviar dolares al exterior y operar acciones en otros mercados.

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
