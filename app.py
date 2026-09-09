
import streamlit as st
import sqlite3
from pathlib import Path
from datetime import date, datetime
import pandas as pd
import re
import io
import base64
import smtplib
import ssl
from datetime import timedelta
from email.message import EmailMessage
from pypdf import PdfReader
from arca_ws import (ARCAError, CBTE_CODES, digits, generar_clave_y_csr, wsaa_login,
                     wsfe_ultimo_autorizado, wsfe_puntos_venta, wsfe_condiciones_iva_receptor,
                     wsfe_solicitar_cae)
from fiscal_pdf import generar_pdf_factura
try:
    from fiscal_pdf import PDF_TEMPLATE_VERSION
except ImportError:
    PDF_TEMPLATE_VERSION = "PLANTILLA_PDF_SIN_VERSION"

APP_DIR = Path(__file__).parent
DB_PATH = APP_DIR / "control_cuentas.db"
PDF_DIR = APP_DIR / "facturas"
PDF_DIR.mkdir(exist_ok=True)

st.set_page_config(page_title="S&S Group · Gestión Administrativa", page_icon="📊", layout="wide")


def _secret(name, default=None):
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default


def app_password_configurada():
    return bool(_secret("app_password"))


def acceso_autorizado():
    expected = _secret("app_password")
    if not expected:
        return True
    if st.session_state.get("authenticated"):
        return True
    st.title("S&S Group · Gestión Administrativa")
    st.subheader("Acceso privado")
    password = st.text_input("Contraseña", type="password")
    if st.button("Ingresar", type="primary"):
        if password == expected:
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("Contraseña incorrecta.")
    return False


if not acceso_autorizado():
    st.stop()

def get_conn():
    conn = sqlite3.connect(
        DB_PATH,
        check_same_thread=False,
        timeout=30
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout = 30000;")
    return conn

def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS emisores(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nombre TEXT NOT NULL,
        cuit TEXT NOT NULL UNIQUE,
        condicion_iva TEXT,
        punto_venta INTEGER DEFAULT 1,
        activo INTEGER DEFAULT 1,
        observaciones TEXT
    );

    CREATE TABLE IF NOT EXISTS clientes(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nombre TEXT NOT NULL UNIQUE,
        cuit TEXT,
        modalidad TEXT NOT NULL DEFAULT 'Factura',
        honorario REAL DEFAULT 0,
        vigente_desde TEXT,
        dia_generacion INTEGER DEFAULT 1,
        activo INTEGER DEFAULT 1,
        observaciones TEXT
    );

    CREATE TABLE IF NOT EXISTS honorarios(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cliente_id INTEGER NOT NULL,
        vigente_desde TEXT NOT NULL,
        honorario REAL NOT NULL,
        nota TEXT,
        FOREIGN KEY(cliente_id) REFERENCES clientes(id)
    );

    CREATE TABLE IF NOT EXISTS movimientos(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fecha TEXT NOT NULL,
        cliente_id INTEGER NOT NULL,
        tipo TEXT NOT NULL,
        descripcion TEXT,
        importe REAL NOT NULL,
        periodo TEXT,
        comprobante TEXT,
        pdf_path TEXT,
        estado_conciliacion TEXT,
        creado_en TEXT NOT NULL,
        UNIQUE(cliente_id, tipo, periodo, comprobante),
        FOREIGN KEY(cliente_id) REFERENCES clientes(id)
    );

    CREATE TABLE IF NOT EXISTS items_facturacion(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nombre TEXT NOT NULL,
        descripcion TEXT NOT NULL,
        precio_sugerido REAL DEFAULT 0,
        activo INTEGER DEFAULT 1
    );

    CREATE TABLE IF NOT EXISTS comprobantes_arca(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cliente_id INTEGER NOT NULL,
        fecha_emision TEXT NOT NULL,
        periodo_desde TEXT,
        periodo_hasta TEXT,
        periodo_texto TEXT,
        tipo_periodo TEXT,
        tipo_comprobante TEXT DEFAULT 'C',
        punto_venta INTEGER DEFAULT 1,
        numero_comprobante INTEGER,
        cae TEXT,
        vencimiento_cae TEXT,
        estado_arca TEXT DEFAULT 'BORRADOR',
        total REAL NOT NULL DEFAULT 0,
        observaciones TEXT,
        creado_en TEXT NOT NULL,
        FOREIGN KEY(cliente_id) REFERENCES clientes(id)
    );

    CREATE TABLE IF NOT EXISTS comprobante_items(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        comprobante_id INTEGER NOT NULL,
        item_catalogo_id INTEGER,
        descripcion TEXT NOT NULL,
        cantidad REAL DEFAULT 1,
        precio_unitario REAL DEFAULT 0,
        subtotal REAL DEFAULT 0,
        FOREIGN KEY(comprobante_id) REFERENCES comprobantes_arca(id)
    );
    """)
    # Migraciones simples para bases existentes
    cols_clientes = [r["name"] for r in conn.execute("PRAGMA table_info(clientes)").fetchall()]
    if "emisor_predeterminado_id" not in cols_clientes:
        conn.execute("ALTER TABLE clientes ADD COLUMN emisor_predeterminado_id INTEGER")

    cols_comp = [r["name"] for r in conn.execute("PRAGMA table_info(comprobantes_arca)").fetchall()]
    if "emisor_id" not in cols_comp:
        conn.execute("ALTER TABLE comprobantes_arca ADD COLUMN emisor_id INTEGER")

    cols_clientes = [r["name"] for r in conn.execute("PRAGMA table_info(clientes)").fetchall()]
    if "email_facturacion" not in cols_clientes:
        conn.execute("ALTER TABLE clientes ADD COLUMN email_facturacion TEXT")
    if "envio_automatico_factura" not in cols_clientes:
        conn.execute("ALTER TABLE clientes ADD COLUMN envio_automatico_factura INTEGER DEFAULT 1")

    cols_comp = [r["name"] for r in conn.execute("PRAGMA table_info(comprobantes_arca)").fetchall()]
    if "pdf_path" not in cols_comp:
        conn.execute("ALTER TABLE comprobantes_arca ADD COLUMN pdf_path TEXT")
    if "email_enviado" not in cols_comp:
        conn.execute("ALTER TABLE comprobantes_arca ADD COLUMN email_enviado INTEGER DEFAULT 0")
    if "email_enviado_a" not in cols_comp:
        conn.execute("ALTER TABLE comprobantes_arca ADD COLUMN email_enviado_a TEXT")
    if "email_enviado_en" not in cols_comp:
        conn.execute("ALTER TABLE comprobantes_arca ADD COLUMN email_enviado_en TEXT")
    if "email_error" not in cols_comp:
        conn.execute("ALTER TABLE comprobantes_arca ADD COLUMN email_error TEXT")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS email_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            comprobante_id INTEGER,
            cliente_id INTEGER NOT NULL,
            destinatario TEXT NOT NULL,
            asunto TEXT NOT NULL,
            estado TEXT NOT NULL,
            detalle TEXT,
            fecha_hora TEXT NOT NULL,
            FOREIGN KEY(comprobante_id) REFERENCES comprobantes_arca(id),
            FOREIGN KEY(cliente_id) REFERENCES clientes(id)
        )
    """)

    def add_col(table, name, definition):
        cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if name not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    # Datos fiscales del emisor
    add_col("emisores", "domicilio_fiscal", "TEXT")
    add_col("emisores", "ingresos_brutos", "TEXT")
    add_col("emisores", "inicio_actividades", "TEXT")
    add_col("emisores", "regimen_iva", "TEXT")
    add_col("emisores", "iva_alicuota", "REAL DEFAULT 21")
    add_col("emisores", "ambiente_arca", "TEXT DEFAULT 'HOMOLOGACION'")
    add_col("emisores", "precios_incluyen_iva", "INTEGER DEFAULT 1")

    # Datos fiscales del receptor
    add_col("clientes", "domicilio", "TEXT")
    add_col("clientes", "condicion_iva_receptor_id", "INTEGER")
    add_col("clientes", "condicion_iva_receptor_desc", "TEXT")

    # Resultado fiscal del comprobante
    add_col("comprobantes_arca", "cbte_tipo_codigo", "INTEGER")
    add_col("comprobantes_arca", "imp_neto", "REAL")
    add_col("comprobantes_arca", "imp_iva", "REAL")
    add_col("comprobantes_arca", "fecha_vto_pago", "TEXT")
    add_col("comprobantes_arca", "arca_observaciones", "TEXT")
    add_col("comprobantes_arca", "arca_error", "TEXT")

    conn.commit()
    conn.close()

def query_df(sql, params=()):
    conn = get_conn()
    df = pd.read_sql_query(sql, conn, params=params)
    conn.close()
    return df

def execute(sql, params=()):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(sql, params)
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid

def money(v):
    try:
        return f"$ {float(v):,.0f}".replace(",", ".")
    except:
        return "$ 0"

def get_clientes(active_only=True):
    where = "WHERE activo=1" if active_only else ""
    return query_df(f"SELECT * FROM clientes {where} ORDER BY nombre")


INITIAL_CLIENTS = [{'nombre': 'MAGLIANO MARCELO',
  'cuit': None,
  'modalidad': 'Factura',
  'honorario': 0,
  'vigente_desde': '2026-09-01',
  'dia_generacion': 1,
  'activo': 1,
  'observaciones': 'Migrado de Control de cuentas(1).xlsx · 09/09/2026'},
 {'nombre': 'JUAREZ CESAR',
  'cuit': None,
  'modalidad': 'Aviso de pago',
  'honorario': 760000.0,
  'vigente_desde': '2026-09-01',
  'dia_generacion': 1,
  'activo': 1,
  'observaciones': 'Migrado de Control de cuentas(1).xlsx · 09/09/2026'},
 {'nombre': 'EULOGIO CONDORI',
  'cuit': None,
  'modalidad': 'Aviso de pago',
  'honorario': 360000.0,
  'vigente_desde': '2026-09-01',
  'dia_generacion': 1,
  'activo': 1,
  'observaciones': 'Migrado de Control de cuentas(1).xlsx · 09/09/2026'},
 {'nombre': 'ROJAS MARTIN',
  'cuit': None,
  'modalidad': 'Aviso de pago',
  'honorario': 240000.0,
  'vigente_desde': '2026-09-01',
  'dia_generacion': 1,
  'activo': 1,
  'observaciones': 'Migrado de Control de cuentas(1).xlsx · 09/09/2026'},
 {'nombre': 'BIOPARQUE BATAN 2023 S.A.',
  'cuit': '30718359453',
  'modalidad': 'Factura',
  'honorario': 245000.0,
  'vigente_desde': '2026-09-01',
  'dia_generacion': 1,
  'activo': 1,
  'observaciones': 'Migrado de Control de cuentas(1).xlsx · 09/09/2026'},
 {'nombre': 'ALTURAS MS MIRAMAR S.R.L',
  'cuit': '30718579763',
  'modalidad': 'Factura',
  'honorario': 230000.0,
  'vigente_desde': '2026-09-01',
  'dia_generacion': 1,
  'activo': 1,
  'observaciones': 'Migrado de Control de cuentas(1).xlsx · 09/09/2026'},
 {'nombre': 'GAUTHIER WALTER',
  'cuit': None,
  'modalidad': 'Aviso de pago',
  'honorario': 200000.0,
  'vigente_desde': '2026-09-01',
  'dia_generacion': 1,
  'activo': 1,
  'observaciones': 'Migrado de Control de cuentas(1).xlsx · 09/09/2026'},
 {'nombre': 'COOPERATIVA DE TRABAJO COOPECONS LTDA',
  'cuit': '30717179680',
  'modalidad': 'Factura',
  'honorario': 230000.0,
  'vigente_desde': '2026-09-01',
  'dia_generacion': 1,
  'activo': 1,
  'observaciones': 'Migrado de Control de cuentas(1).xlsx · 09/09/2026'},
 {'nombre': 'OBISPADO DE MAR DEL PLATA',
  'cuit': '30542337555',
  'modalidad': 'Factura',
  'honorario': 89000.0,
  'vigente_desde': '2026-09-01',
  'dia_generacion': 1,
  'activo': 1,
  'observaciones': 'Migrado de Control de cuentas(1).xlsx · 09/09/2026'},
 {'nombre': 'COOPERATIVA DE TRABAJO EL CHE LIMITADA',
  'cuit': '33711078199',
  'modalidad': 'Factura',
  'honorario': 89000.0,
  'vigente_desde': '2026-09-01',
  'dia_generacion': 1,
  'activo': 1,
  'observaciones': 'Migrado de Control de cuentas(1).xlsx · 09/09/2026'},
 {'nombre': 'COOPERATIVA DE TRABAJO SEGUIMOS LUCHANDO LTDA',
  'cuit': '30714199753',
  'modalidad': 'Factura',
  'honorario': 200000.0,
  'vigente_desde': '2026-09-01',
  'dia_generacion': 1,
  'activo': 1,
  'observaciones': 'Migrado de Control de cuentas(1).xlsx · 09/09/2026'},
 {'nombre': 'INVERSORA EN CONSTRUCCIONES DE COBO S.A.',
  'cuit': '30711651280',
  'modalidad': 'Aviso de pago',
  'honorario': 460000.0,
  'vigente_desde': '2026-09-01',
  'dia_generacion': 1,
  'activo': 1,
  'observaciones': 'Migrado de Control de cuentas(1).xlsx · 09/09/2026'},
 {'nombre': 'GALVAN',
  'cuit': None,
  'modalidad': 'Factura',
  'honorario': 230000.0,
  'vigente_desde': '2026-09-01',
  'dia_generacion': 1,
  'activo': 1,
  'observaciones': 'Migrado de Control de cuentas(1).xlsx · 09/09/2026'},
 {'nombre': 'SINDICATO DE QUIMICOS',
  'cuit': '30532700414',
  'modalidad': 'Factura',
  'honorario': 192000.0,
  'vigente_desde': '2026-09-01',
  'dia_generacion': 1,
  'activo': 1,
  'observaciones': 'Migrado de Control de cuentas(1).xlsx · 09/09/2026'},
 {'nombre': 'INFINIT',
  'cuit': '30711262713',
  'modalidad': 'Factura',
  'honorario': 108000.0,
  'vigente_desde': '2026-09-01',
  'dia_generacion': 1,
  'activo': 1,
  'observaciones': 'Migrado de Control de cuentas(1).xlsx · 09/09/2026'},
 {'nombre': 'GENARO Y ANDRES DE STEFANO',
  'cuit': '30500689826',
  'modalidad': 'Factura',
  'honorario': 192000.0,
  'vigente_desde': '2026-09-01',
  'dia_generacion': 1,
  'activo': 1,
  'observaciones': 'Migrado de Control de cuentas(1).xlsx · 09/09/2026'},
 {'nombre': 'RUCANEDA S.A.',
  'cuit': '30712269134',
  'modalidad': 'Factura',
  'honorario': 206000.0,
  'vigente_desde': '2026-09-01',
  'dia_generacion': 1,
  'activo': 1,
  'observaciones': 'Migrado de Control de cuentas(1).xlsx · 09/09/2026'},
 {'nombre': 'SARAZOLA',
  'cuit': None,
  'modalidad': 'Factura',
  'honorario': 150000.0,
  'vigente_desde': '2026-09-01',
  'dia_generacion': 1,
  'activo': 1,
  'observaciones': 'Migrado de Control de cuentas(1).xlsx · 09/09/2026'},
 {'nombre': 'RBC CONSTRUCCIONES',
  'cuit': '6 MONOTRIBUTISTAS',
  'modalidad': 'Factura',
  'honorario': 400000.0,
  'vigente_desde': '2026-09-01',
  'dia_generacion': 1,
  'activo': 1,
  'observaciones': 'Migrado de Control de cuentas(1).xlsx · 09/09/2026'},
 {'nombre': 'FIDEICOMISO DAPROTIS 4156 MAR DEL PLATA',
  'cuit': '30717979296',
  'modalidad': 'Factura',
  'honorario': 170000.0,
  'vigente_desde': '2026-09-01',
  'dia_generacion': 1,
  'activo': 1,
  'observaciones': 'Migrado de Control de cuentas(1).xlsx · 09/09/2026'},
 {'nombre': 'OESTE (LAURA FARIAS)',
  'cuit': '27149714826',
  'modalidad': 'Factura',
  'honorario': 130000.0,
  'vigente_desde': '2026-09-01',
  'dia_generacion': 1,
  'activo': 1,
  'observaciones': 'Migrado de Control de cuentas(1).xlsx · 09/09/2026'},
 {'nombre': 'DISTRISUPER S.R.L.',
  'cuit': '30609249206',
  'modalidad': 'Factura',
  'honorario': 190000.0,
  'vigente_desde': '2026-09-01',
  'dia_generacion': 1,
  'activo': 1,
  'observaciones': 'Migrado de Control de cuentas(1).xlsx · 09/09/2026'},
 {'nombre': 'DIMES S.A.',
  'cuit': '33715613439',
  'modalidad': 'Factura',
  'honorario': 101000.0,
  'vigente_desde': '2026-09-01',
  'dia_generacion': 1,
  'activo': 1,
  'observaciones': 'Migrado de Control de cuentas(1).xlsx · 09/09/2026'},
 {'nombre': 'ROCA 2936 MAR DEL PLATA S.A.',
  'cuit': '30717026965',
  'modalidad': 'Factura',
  'honorario': 190000.0,
  'vigente_desde': '2026-09-01',
  'dia_generacion': 1,
  'activo': 1,
  'observaciones': 'Migrado de Control de cuentas(1).xlsx · 09/09/2026'},
 {'nombre': 'RAMOS SANCHEZ ALCIDES',
  'cuit': None,
  'modalidad': 'Aviso de pago',
  'honorario': 450000.0,
  'vigente_desde': '2026-09-01',
  'dia_generacion': 1,
  'activo': 1,
  'observaciones': 'Migrado de Control de cuentas(1).xlsx · 09/09/2026'},
 {'nombre': 'GEHIE GASTRONOMICA SRL (alito)',
  'cuit': '30681375135',
  'modalidad': 'Factura',
  'honorario': 170000.0,
  'vigente_desde': '2026-09-01',
  'dia_generacion': 1,
  'activo': 1,
  'observaciones': 'Migrado de Control de cuentas(1).xlsx · 09/09/2026'},
 {'nombre': 'GUSTAVO RIVERA PLOMERO',
  'cuit': None,
  'modalidad': 'Factura',
  'honorario': 280000.0,
  'vigente_desde': '2026-09-01',
  'dia_generacion': 1,
  'activo': 1,
  'observaciones': 'Migrado de Control de cuentas(1).xlsx · 09/09/2026'},
 {'nombre': 'BARD ATILIO RENE',
  'cuit': '20047463514',
  'modalidad': 'Factura',
  'honorario': 0,
  'vigente_desde': '2026-09-01',
  'dia_generacion': 1,
  'activo': 1,
  'observaciones': 'Migrado de Control de cuentas(1).xlsx · 09/09/2026'},
 {'nombre': 'MAGGI MARCELO Y MAGGI MAURICIO SOC …',
  'cuit': '30688684028',
  'modalidad': 'Factura',
  'honorario': 350000.0,
  'vigente_desde': '2026-09-01',
  'dia_generacion': 1,
  'activo': 1,
  'observaciones': 'Migrado de Control de cuentas(1).xlsx · 09/09/2026'},
 {'nombre': 'CRUZ GEORGE',
  'cuit': None,
  'modalidad': 'Factura',
  'honorario': 230000.0,
  'vigente_desde': '2026-09-01',
  'dia_generacion': 1,
  'activo': 1,
  'observaciones': 'Migrado de Control de cuentas(1).xlsx · 09/09/2026'},
 {'nombre': 'MONDEGO DA GUARDA S.A.',
  'cuit': '30716517361',
  'modalidad': 'Factura',
  'honorario': 350000.0,
  'vigente_desde': '2026-09-01',
  'dia_generacion': 1,
  'activo': 1,
  'observaciones': 'Migrado de Control de cuentas(1).xlsx · 09/09/2026'},
 {'nombre': 'GRUPO BOREAS S.R.L.',
  'cuit': '30714816981',
  'modalidad': 'Factura',
  'honorario': 200000.0,
  'vigente_desde': '2026-09-01',
  'dia_generacion': 1,
  'activo': 1,
  'observaciones': 'Migrado de Control de cuentas(1).xlsx · 09/09/2026'},
 {'nombre': 'COOK MASTER S.A.',
  'cuit': '30708214368',
  'modalidad': 'Factura',
  'honorario': 703000.0,
  'vigente_desde': '2026-09-01',
  'dia_generacion': 1,
  'activo': 1,
  'observaciones': 'Migrado de Control de cuentas(1).xlsx · 09/09/2026'},
 {'nombre': 'UP EXPLANADA S.A.',
  'cuit': '33718243829',
  'modalidad': 'Factura',
  'honorario': 170000.0,
  'vigente_desde': '2026-09-01',
  'dia_generacion': 1,
  'activo': 1,
  'observaciones': 'Migrado de Control de cuentas(1).xlsx · 09/09/2026'},
 {'nombre': 'PEZZANA DIEGO',
  'cuit': '20259572453',
  'modalidad': 'Factura',
  'honorario': 380000.0,
  'vigente_desde': '2026-09-01',
  'dia_generacion': 1,
  'activo': 1,
  'observaciones': 'Migrado de Control de cuentas(1).xlsx · 09/09/2026'},
 {'nombre': 'LUBRIEL SRL',
  'cuit': '30711294704',
  'modalidad': 'Factura',
  'honorario': 195000.0,
  'vigente_desde': '2026-09-01',
  'dia_generacion': 1,
  'activo': 1,
  'observaciones': 'Migrado de Control de cuentas(1).xlsx · 09/09/2026'},
 {'nombre': 'MANALER S.A.',
  'cuit': '30716570440',
  'modalidad': 'Factura',
  'honorario': 0,
  'vigente_desde': '2026-09-01',
  'dia_generacion': 1,
  'activo': 1,
  'observaciones': 'Migrado de Control de cuentas(1).xlsx · 09/09/2026'},
 {'nombre': 'LOGISMAR S.R.L.',
  'cuit': '30708043636',
  'modalidad': 'Factura',
  'honorario': 145000.0,
  'vigente_desde': '2026-09-01',
  'dia_generacion': 1,
  'activo': 1,
  'observaciones': 'Migrado de Control de cuentas(1).xlsx · 09/09/2026'},
 {'nombre': 'USAI ANALIA USAI GABRIELA USAI ESTEBAN S.H.',
  'cuit': '33636629559',
  'modalidad': 'Factura',
  'honorario': 100000.0,
  'vigente_desde': '2026-09-01',
  'dia_generacion': 1,
  'activo': 1,
  'observaciones': 'Migrado de Control de cuentas(1).xlsx · 09/09/2026'},
 {'nombre': 'ANGELICO CORP',
  'cuit': '30718290747',
  'modalidad': 'Factura',
  'honorario': 0,
  'vigente_desde': '2026-09-01',
  'dia_generacion': 1,
  'activo': 1,
  'observaciones': 'Migrado de Control de cuentas(1).xlsx · 09/09/2026'},
 {'nombre': 'PROSEGUR S.A.',
  'cuit': '30575170125',
  'modalidad': 'Factura',
  'honorario': 238000.0,
  'vigente_desde': '2026-09-01',
  'dia_generacion': 1,
  'activo': 1,
  'observaciones': 'Migrado de Control de cuentas(1).xlsx · 09/09/2026'},
 {'nombre': 'JUNCADELLA',
  'cuit': '30546969874',
  'modalidad': 'Factura',
  'honorario': 327900.0,
  'vigente_desde': '2026-09-01',
  'dia_generacion': 1,
  'activo': 1,
  'observaciones': 'Migrado de Control de cuentas(1).xlsx · 09/09/2026'},
 {'nombre': 'MADRID',
  'cuit': None,
  'modalidad': 'Aviso de pago',
  'honorario': 230000.0,
  'vigente_desde': '2026-09-01',
  'dia_generacion': 1,
  'activo': 1,
  'observaciones': 'Migrado de Control de cuentas(1).xlsx · 09/09/2026'},
 {'nombre': 'CAPARARO',
  'cuit': '23272137404',
  'modalidad': 'Factura',
  'honorario': 170000.0,
  'vigente_desde': '2026-09-01',
  'dia_generacion': 1,
  'activo': 1,
  'observaciones': 'Migrado de Control de cuentas(1).xlsx · 09/09/2026'}]

INITIAL_MOVEMENTS = [{'fecha': '2026-09-01',
  'cliente': 'JUAREZ CESAR',
  'tipo': 'Pago',
  'descripcion': 'Aviso de pago',
  'importe': 760000.0,
  'periodo': '2026-09-01',
  'origen': 'JUAREZ',
  'comprobante': None,
  'estado': 'Registrado',
  'seq': 1},
 {'fecha': '2026-09-01',
  'cliente': 'EULOGIO CONDORI',
  'tipo': 'Pago',
  'descripcion': 'Aviso de pago',
  'importe': 360000.0,
  'periodo': '2026-09-01',
  'origen': 'EULOGIO',
  'comprobante': None,
  'estado': 'Registrado',
  'seq': 2},
 {'fecha': '2026-09-01',
  'cliente': 'ROJAS MARTIN',
  'tipo': 'Pago',
  'descripcion': 'Aviso de pago',
  'importe': 240000.0,
  'periodo': '2026-09-01',
  'origen': 'MARTIN ROJAS',
  'comprobante': None,
  'estado': 'Registrado',
  'seq': 3},
 {'fecha': '2026-09-01',
  'cliente': 'BIOPARQUE BATAN 2023 S.A.',
  'tipo': 'Factura/Cargo',
  'descripcion': 'Facturación',
  'importe': 245000.0,
  'periodo': '2026-09-01',
  'origen': 'BIOPARQUE BATAN',
  'comprobante': None,
  'estado': None,
  'seq': 4},
 {'fecha': '2026-09-01',
  'cliente': 'ALTURAS MS MIRAMAR S.R.L',
  'tipo': 'Factura/Cargo',
  'descripcion': 'Facturación',
  'importe': 460000.0,
  'periodo': '2026-09-01',
  'origen': 'ALTURAS MS MIRAMAR',
  'comprobante': None,
  'estado': None,
  'seq': 5},
 {'fecha': '2026-08-01',
  'cliente': 'GAUTHIER WALTER',
  'tipo': 'Pago',
  'descripcion': 'Aviso de pago',
  'importe': 400000.0,
  'periodo': '2026-08-01',
  'origen': 'GAUTHIER',
  'comprobante': None,
  'estado': 'Registrado',
  'seq': 6},
 {'fecha': '2026-09-01',
  'cliente': 'INVERSORA EN CONSTRUCCIONES DE COBO S.A.',
  'tipo': 'Pago',
  'descripcion': 'AVISO DE PAGO',
  'importe': 460000.0,
  'periodo': '2026-09-01',
  'origen': 'AURORA DEL MAR',
  'comprobante': None,
  'estado': 'Registrado',
  'seq': 7},
 {'fecha': '2026-08-01',
  'cliente': 'GALVAN',
  'tipo': 'Pago',
  'descripcion': 'Aviso de pago',
  'importe': 230000.0,
  'periodo': '2026-08-01',
  'origen': 'GALVAN',
  'comprobante': None,
  'estado': 'Registrado',
  'seq': 8},
 {'fecha': '2025-12-01',
  'cliente': 'SINDICATO DE QUIMICOS',
  'tipo': 'Factura/Cargo',
  'descripcion': 'Facturación',
  'importe': 163000.0,
  'periodo': '2025-12-01',
  'origen': 'QUIMICOS',
  'comprobante': None,
  'estado': None,
  'seq': 9},
 {'fecha': '2026-01-01',
  'cliente': 'SINDICATO DE QUIMICOS',
  'tipo': 'Factura/Cargo',
  'descripcion': 'Facturación',
  'importe': 163000.0,
  'periodo': '2026-01-01',
  'origen': 'QUIMICOS',
  'comprobante': None,
  'estado': None,
  'seq': 10},
 {'fecha': '2026-01-05',
  'cliente': 'SINDICATO DE QUIMICOS',
  'tipo': 'Pago',
  'descripcion': 'PAGO',
  'importe': -163000.0,
  'periodo': '2026-01-01',
  'origen': 'QUIMICOS',
  'comprobante': None,
  'estado': 'Registrado',
  'seq': 11},
 {'fecha': '2026-02-01',
  'cliente': 'SINDICATO DE QUIMICOS',
  'tipo': 'Factura/Cargo',
  'descripcion': 'Facturación',
  'importe': 163000.0,
  'periodo': '2026-02-01',
  'origen': 'QUIMICOS',
  'comprobante': None,
  'estado': None,
  'seq': 12},
 {'fecha': '2026-02-20',
  'cliente': 'SINDICATO DE QUIMICOS',
  'tipo': 'Pago',
  'descripcion': 'PAGO',
  'importe': -163000.0,
  'periodo': '2026-02-01',
  'origen': 'QUIMICOS',
  'comprobante': None,
  'estado': 'Registrado',
  'seq': 13},
 {'fecha': '2026-03-01',
  'cliente': 'SINDICATO DE QUIMICOS',
  'tipo': 'Factura/Cargo',
  'descripcion': 'Facturación',
  'importe': 163000.0,
  'periodo': '2026-03-01',
  'origen': 'QUIMICOS',
  'comprobante': None,
  'estado': None,
  'seq': 14},
 {'fecha': '2026-03-02',
  'cliente': 'SINDICATO DE QUIMICOS',
  'tipo': 'Pago',
  'descripcion': 'PAGO',
  'importe': -163000.0,
  'periodo': '2026-03-01',
  'origen': 'QUIMICOS',
  'comprobante': None,
  'estado': 'Registrado',
  'seq': 15},
 {'fecha': '2026-04-01',
  'cliente': 'SINDICATO DE QUIMICOS',
  'tipo': 'Factura/Cargo',
  'descripcion': 'Facturación',
  'importe': 163000.0,
  'periodo': '2026-04-01',
  'origen': 'QUIMICOS',
  'comprobante': None,
  'estado': None,
  'seq': 16},
 {'fecha': '2026-05-01',
  'cliente': 'SINDICATO DE QUIMICOS',
  'tipo': 'Factura/Cargo',
  'descripcion': 'Facturación',
  'importe': 163000.0,
  'periodo': '2026-05-01',
  'origen': 'QUIMICOS',
  'comprobante': None,
  'estado': None,
  'seq': 17},
 {'fecha': '2026-05-01',
  'cliente': 'SINDICATO DE QUIMICOS',
  'tipo': 'Pago',
  'descripcion': 'PAGO',
  'importe': -163000.0,
  'periodo': '2026-05-01',
  'origen': 'QUIMICOS',
  'comprobante': None,
  'estado': 'Registrado',
  'seq': 18},
 {'fecha': '2026-06-01',
  'cliente': 'SINDICATO DE QUIMICOS',
  'tipo': 'Factura/Cargo',
  'descripcion': 'Facturación',
  'importe': 192000.0,
  'periodo': '2026-06-01',
  'origen': 'QUIMICOS',
  'comprobante': None,
  'estado': None,
  'seq': 19},
 {'fecha': '2026-07-01',
  'cliente': 'SINDICATO DE QUIMICOS',
  'tipo': 'Factura/Cargo',
  'descripcion': 'Facturación',
  'importe': 192000.0,
  'periodo': '2026-07-01',
  'origen': 'QUIMICOS',
  'comprobante': None,
  'estado': None,
  'seq': 20},
 {'fecha': '2026-07-10',
  'cliente': 'SINDICATO DE QUIMICOS',
  'tipo': 'Pago',
  'descripcion': 'PAGO',
  'importe': -335000.0,
  'periodo': '2026-07-01',
  'origen': 'QUIMICOS',
  'comprobante': None,
  'estado': 'Registrado',
  'seq': 21},
 {'fecha': '2026-08-01',
  'cliente': 'SINDICATO DE QUIMICOS',
  'tipo': 'Factura/Cargo',
  'descripcion': 'Facturación',
  'importe': 192000.0,
  'periodo': '2026-08-01',
  'origen': 'QUIMICOS',
  'comprobante': None,
  'estado': None,
  'seq': 22},
 {'fecha': '2026-08-01',
  'cliente': 'SINDICATO DE QUIMICOS',
  'tipo': 'Pago',
  'descripcion': 'PAGO',
  'importe': -192000.0,
  'periodo': '2026-08-01',
  'origen': 'QUIMICOS',
  'comprobante': None,
  'estado': 'Registrado',
  'seq': 23},
 {'fecha': '2026-09-01',
  'cliente': 'INFINIT',
  'tipo': 'Factura/Cargo',
  'descripcion': 'Facturación',
  'importe': 130000.0,
  'periodo': '2026-09-01',
  'origen': 'INFINIT',
  'comprobante': None,
  'estado': None,
  'seq': 24},
 {'fecha': '2026-09-01',
  'cliente': 'GENARO Y ANDRES DE STEFANO',
  'tipo': 'Factura/Cargo',
  'descripcion': 'Facturación',
  'importe': 192000.0,
  'periodo': '2026-09-01',
  'origen': 'DE STEFANO SA',
  'comprobante': None,
  'estado': None,
  'seq': 25},
 {'fecha': '2026-07-01',
  'cliente': 'RUCANEDA S.A.',
  'tipo': 'Factura/Cargo',
  'descripcion': 'Emisión de factura',
  'importe': 206000.0,
  'periodo': '2026-07-01',
  'origen': 'RUCANEDA SA',
  'comprobante': None,
  'estado': None,
  'seq': 26},
 {'fecha': '2026-08-01',
  'cliente': 'RUCANEDA S.A.',
  'tipo': 'Factura/Cargo',
  'descripcion': 'Emisión de factura',
  'importe': 206000.0,
  'periodo': '2026-08-01',
  'origen': 'RUCANEDA SA',
  'comprobante': None,
  'estado': None,
  'seq': 27},
 {'fecha': '2026-08-01',
  'cliente': 'SARAZOLA',
  'tipo': 'Factura/Cargo',
  'descripcion': 'SALDO A LA FECHA',
  'importe': 550000.0,
  'periodo': '2026-08-01',
  'origen': 'SARAZOLA',
  'comprobante': None,
  'estado': None,
  'seq': 28},
 {'fecha': '2026-08-20',
  'cliente': 'SARAZOLA',
  'tipo': 'Pago',
  'descripcion': 'PAGO',
  'importe': -200000.0,
  'periodo': '2026-08-01',
  'origen': 'SARAZOLA',
  'comprobante': None,
  'estado': 'Registrado',
  'seq': 29},
 {'fecha': '2026-09-01',
  'cliente': 'RBC CONSTRUCCIONES',
  'tipo': 'Factura/Cargo',
  'descripcion': 'Emisión de factura',
  'importe': 400000.0,
  'periodo': '2026-09-01',
  'origen': 'RBC CONSTRUCCIONES',
  'comprobante': None,
  'estado': None,
  'seq': 30},
 {'fecha': '2026-09-01',
  'cliente': 'RAMOS SANCHEZ ALCIDES',
  'tipo': 'Pago',
  'descripcion': 'Aviso de pago',
  'importe': 450000.0,
  'periodo': '2026-09-01',
  'origen': 'RAMOS SANCHEZ',
  'comprobante': None,
  'estado': 'Registrado',
  'seq': 31},
 {'fecha': '2026-09-01',
  'cliente': 'GEHIE GASTRONOMICA SRL (alito)',
  'tipo': 'Factura/Cargo',
  'descripcion': 'Emisión de factura',
  'importe': 170000.0,
  'periodo': '2026-09-01',
  'origen': 'ALITO',
  'comprobante': None,
  'estado': None,
  'seq': 32},
 {'fecha': '2026-09-01',
  'cliente': 'BARD ATILIO RENE',
  'tipo': 'Factura/Cargo',
  'descripcion': 'Emisión de factura',
  'importe': 180000.0,
  'periodo': '2026-09-01',
  'origen': 'BARD',
  'comprobante': None,
  'estado': None,
  'seq': 33},
 {'fecha': '2026-09-01',
  'cliente': 'CRUZ GEORGE',
  'tipo': 'Factura/Cargo',
  'descripcion': 'Facturación',
  'importe': 230000.0,
  'periodo': '2026-09-01',
  'origen': 'CRUZ',
  'comprobante': None,
  'estado': None,
  'seq': 34},
 {'fecha': '2026-09-01',
  'cliente': 'MONDEGO DA GUARDA S.A.',
  'tipo': 'Factura/Cargo',
  'descripcion': 'Facturación',
  'importe': 350000.0,
  'periodo': '2026-09-01',
  'origen': 'MONDEGO DA GUARDA',
  'comprobante': None,
  'estado': None,
  'seq': 35},
 {'fecha': '2026-09-01',
  'cliente': 'GRUPO BOREAS S.R.L.',
  'tipo': 'Factura/Cargo',
  'descripcion': 'Emisión de factura',
  'importe': 200000.0,
  'periodo': '2026-09-01',
  'origen': 'BOREAS',
  'comprobante': None,
  'estado': None,
  'seq': 36},
 {'fecha': '2026-04-01',
  'cliente': 'COOK MASTER S.A.',
  'tipo': 'Factura/Cargo',
  'descripcion': 'Facturación',
  'importe': 680000.0,
  'periodo': '2026-04-01',
  'origen': 'COOK MASTER',
  'comprobante': None,
  'estado': None,
  'seq': 37},
 {'fecha': '2026-05-04',
  'cliente': 'COOK MASTER S.A.',
  'tipo': 'Factura/Cargo',
  'descripcion': 'Facturación',
  'importe': 680000.0,
  'periodo': '2026-05-01',
  'origen': 'COOK MASTER',
  'comprobante': None,
  'estado': None,
  'seq': 38},
 {'fecha': '2026-05-20',
  'cliente': 'COOK MASTER S.A.',
  'tipo': 'Pago',
  'descripcion': 'PAGO',
  'importe': -680000.0,
  'periodo': '2026-05-01',
  'origen': 'COOK MASTER',
  'comprobante': None,
  'estado': 'Registrado',
  'seq': 39},
 {'fecha': '2026-06-25',
  'cliente': 'COOK MASTER S.A.',
  'tipo': 'Factura/Cargo',
  'descripcion': 'Facturación',
  'importe': 680000.0,
  'periodo': '2026-06-01',
  'origen': 'COOK MASTER',
  'comprobante': None,
  'estado': None,
  'seq': 40},
 {'fecha': '2027-07-01',
  'cliente': 'COOK MASTER S.A.',
  'tipo': 'Factura/Cargo',
  'descripcion': 'Facturación',
  'importe': 680000.0,
  'periodo': '2027-07-01',
  'origen': 'COOK MASTER',
  'comprobante': None,
  'estado': None,
  'seq': 41},
 {'fecha': '2026-08-03',
  'cliente': 'COOK MASTER S.A.',
  'tipo': 'Pago',
  'descripcion': 'PAGO',
  'importe': -680000.0,
  'periodo': '2026-08-01',
  'origen': 'COOK MASTER',
  'comprobante': None,
  'estado': 'Registrado',
  'seq': 42},
 {'fecha': '2026-08-06',
  'cliente': 'COOK MASTER S.A.',
  'tipo': 'Factura/Cargo',
  'descripcion': 'Facturación',
  'importe': 703000.0,
  'periodo': '2026-08-01',
  'origen': 'COOK MASTER',
  'comprobante': None,
  'estado': None,
  'seq': 43},
 {'fecha': '2026-08-20',
  'cliente': 'COOK MASTER S.A.',
  'tipo': 'Pago',
  'descripcion': 'PAGO',
  'importe': -680000.0,
  'periodo': '2026-08-01',
  'origen': 'COOK MASTER',
  'comprobante': None,
  'estado': 'Registrado',
  'seq': 44},
 {'fecha': '2026-09-01',
  'cliente': 'COOK MASTER S.A.',
  'tipo': 'Factura/Cargo',
  'descripcion': 'Facturación',
  'importe': 703000.0,
  'periodo': '2026-09-01',
  'origen': 'COOK MASTER',
  'comprobante': None,
  'estado': None,
  'seq': 45},
 {'fecha': '2026-06-01',
  'cliente': 'PEZZANA DIEGO',
  'tipo': 'Factura/Cargo',
  'descripcion': 'Facturación',
  'importe': 200000.0,
  'periodo': '2026-06-01',
  'origen': 'PEZZANA',
  'comprobante': None,
  'estado': None,
  'seq': 46},
 {'fecha': '2026-07-01',
  'cliente': 'PEZZANA DIEGO',
  'tipo': 'Factura/Cargo',
  'descripcion': 'Facturación',
  'importe': 200000.0,
  'periodo': '2026-07-01',
  'origen': 'PEZZANA',
  'comprobante': None,
  'estado': None,
  'seq': 47},
 {'fecha': '2026-07-01',
  'cliente': 'PEZZANA DIEGO',
  'tipo': 'Factura/Cargo',
  'descripcion': 'PS 51',
  'importe': 230000.0,
  'periodo': '2026-07-01',
  'origen': 'PEZZANA',
  'comprobante': None,
  'estado': None,
  'seq': 48},
 {'fecha': '2026-07-10',
  'cliente': 'PEZZANA DIEGO',
  'tipo': 'Pago',
  'descripcion': 'PAGO',
  'importe': -300000.0,
  'periodo': '2026-07-01',
  'origen': 'PEZZANA',
  'comprobante': None,
  'estado': 'Registrado',
  'seq': 49},
 {'fecha': '2026-08-01',
  'cliente': 'PEZZANA DIEGO',
  'tipo': 'Factura/Cargo',
  'descripcion': 'Facturación',
  'importe': 380000.0,
  'periodo': '2026-08-01',
  'origen': 'PEZZANA',
  'comprobante': None,
  'estado': None,
  'seq': 50},
 {'fecha': '2026-09-01',
  'cliente': 'PEZZANA DIEGO',
  'tipo': 'Factura/Cargo',
  'descripcion': 'Facturación',
  'importe': 380000.0,
  'periodo': '2026-09-01',
  'origen': 'PEZZANA',
  'comprobante': None,
  'estado': None,
  'seq': 51},
 {'fecha': '2026-09-01',
  'cliente': 'MANALER S.A.',
  'tipo': 'Factura/Cargo',
  'descripcion': 'Emisión de factura',
  'importe': 1050000.0,
  'periodo': '2026-09-01',
  'origen': 'MANALER',
  'comprobante': None,
  'estado': None,
  'seq': 52},
 {'fecha': '2026-07-01',
  'cliente': 'ANGELICO CORP',
  'tipo': 'Factura/Cargo',
  'descripcion': 'Emisión de factura',
  'importe': 760000.0,
  'periodo': '2026-07-01',
  'origen': 'ANGELICO CORP',
  'comprobante': None,
  'estado': None,
  'seq': 53},
 {'fecha': '2026-08-01',
  'cliente': 'ANGELICO CORP',
  'tipo': 'Factura/Cargo',
  'descripcion': 'Emisión de factura',
  'importe': 380000.0,
  'periodo': '2026-08-01',
  'origen': 'ANGELICO CORP',
  'comprobante': None,
  'estado': None,
  'seq': 54},
 {'fecha': '2026-08-20',
  'cliente': 'ANGELICO CORP',
  'tipo': 'Pago',
  'descripcion': 'PAGO',
  'importe': -760000.0,
  'periodo': '2026-08-01',
  'origen': 'ANGELICO CORP',
  'comprobante': None,
  'estado': 'Registrado',
  'seq': 55},
 {'fecha': '2026-09-01',
  'cliente': 'ANGELICO CORP',
  'tipo': 'Factura/Cargo',
  'descripcion': 'Emisión de factura',
  'importe': 620000.0,
  'periodo': '2026-09-01',
  'origen': 'ANGELICO CORP',
  'comprobante': None,
  'estado': None,
  'seq': 56},
 {'fecha': '2026-05-10',
  'cliente': 'MADRID',
  'tipo': 'Pago',
  'descripcion': 'Aviso de pago',
  'importe': 230000.0,
  'periodo': '2026-05-01',
  'origen': 'MADRID',
  'comprobante': None,
  'estado': 'Registrado',
  'seq': 57},
 {'fecha': '2026-05-26',
  'cliente': 'MADRID',
  'tipo': 'Pago',
  'descripcion': 'PAGO',
  'importe': -230000.0,
  'periodo': '2026-05-01',
  'origen': 'MADRID',
  'comprobante': None,
  'estado': 'Registrado',
  'seq': 58},
 {'fecha': '2026-06-20',
  'cliente': 'MADRID',
  'tipo': 'Pago',
  'descripcion': 'Aviso de pago',
  'importe': 230000.0,
  'periodo': '2026-06-01',
  'origen': 'MADRID',
  'comprobante': None,
  'estado': 'Registrado',
  'seq': 59},
 {'fecha': '2026-07-20',
  'cliente': 'MADRID',
  'tipo': 'Pago',
  'descripcion': 'Aviso de pago',
  'importe': 230000.0,
  'periodo': '2026-07-01',
  'origen': 'MADRID',
  'comprobante': None,
  'estado': 'Registrado',
  'seq': 60},
 {'fecha': '2026-08-20',
  'cliente': 'MADRID',
  'tipo': 'Pago',
  'descripcion': 'PAGO',
  'importe': -230000.0,
  'periodo': '2026-08-01',
  'origen': 'MADRID',
  'comprobante': None,
  'estado': 'Registrado',
  'seq': 61},
 {'fecha': '2026-09-01',
  'cliente': 'MADRID',
  'tipo': 'Pago',
  'descripcion': 'Aviso de pago',
  'importe': 230000.0,
  'periodo': '2026-09-01',
  'origen': 'MADRID',
  'comprobante': None,
  'estado': 'Registrado',
  'seq': 62},
 {'fecha': '2026-09-28',
  'cliente': 'COOPERATIVA DE TRABAJO SEGUIMOS LUCHANDO LTDA',
  'tipo': 'Factura/Cargo',
  'descripcion': 'Facturación',
  'importe': 240000.0,
  'periodo': '2026-09-01',
  'origen': 'COOPERATIVAS',
  'comprobante': None,
  'estado': None,
  'seq': 63},
 {'fecha': '2026-06-01',
  'cliente': 'PROSEGUR S.A.',
  'tipo': 'Factura/Cargo',
  'descripcion': 'Emisión de factura',
  'importe': 238000.0,
  'periodo': '2026-06-01',
  'origen': 'PROSEGUR',
  'comprobante': None,
  'estado': None,
  'seq': 64},
 {'fecha': '2026-07-01',
  'cliente': 'PROSEGUR S.A.',
  'tipo': 'Factura/Cargo',
  'descripcion': 'Emisión de factura',
  'importe': 238000.0,
  'periodo': '2026-07-01',
  'origen': 'PROSEGUR',
  'comprobante': None,
  'estado': None,
  'seq': 65},
 {'fecha': '2026-08-01',
  'cliente': 'PROSEGUR S.A.',
  'tipo': 'Factura/Cargo',
  'descripcion': 'Emisión de factura',
  'importe': 238000.0,
  'periodo': '2026-08-01',
  'origen': 'PROSEGUR',
  'comprobante': None,
  'estado': None,
  'seq': 66},
 {'fecha': '2026-08-06',
  'cliente': 'PROSEGUR S.A.',
  'tipo': 'Pago',
  'descripcion': 'PAGO',
  'importe': -238000.0,
  'periodo': '2026-08-01',
  'origen': 'PROSEGUR',
  'comprobante': None,
  'estado': 'Registrado',
  'seq': 67},
 {'fecha': '2026-09-01',
  'cliente': 'PROSEGUR S.A.',
  'tipo': 'Factura/Cargo',
  'descripcion': 'Emisión de factura',
  'importe': 238000.0,
  'periodo': '2026-09-01',
  'origen': 'PROSEGUR',
  'comprobante': None,
  'estado': None,
  'seq': 68},
 {'fecha': '2026-06-01',
  'cliente': 'JUNCADELLA',
  'tipo': 'Factura/Cargo',
  'descripcion': 'Emisión de factura',
  'importe': 327900.0,
  'periodo': '2026-06-01',
  'origen': 'PROSEGUR',
  'comprobante': None,
  'estado': None,
  'seq': 69},
 {'fecha': '2026-07-01',
  'cliente': 'JUNCADELLA',
  'tipo': 'Factura/Cargo',
  'descripcion': 'Emisión de factura',
  'importe': 327900.0,
  'periodo': '2026-07-01',
  'origen': 'PROSEGUR',
  'comprobante': None,
  'estado': None,
  'seq': 70},
 {'fecha': '2026-08-06',
  'cliente': 'JUNCADELLA',
  'tipo': 'Pago',
  'descripcion': 'PAGO',
  'importe': -327900.0,
  'periodo': '2026-08-01',
  'origen': 'PROSEGUR',
  'comprobante': None,
  'estado': 'Registrado',
  'seq': 71},
 {'fecha': '2026-08-06',
  'cliente': 'JUNCADELLA',
  'tipo': 'Factura/Cargo',
  'descripcion': 'Emisión de factura',
  'importe': 327900.0,
  'periodo': '2026-08-01',
  'origen': 'PROSEGUR',
  'comprobante': None,
  'estado': None,
  'seq': 72},
 {'fecha': '2026-09-01',
  'cliente': 'JUNCADELLA',
  'tipo': 'Factura/Cargo',
  'descripcion': 'Emisión de factura',
  'importe': 327900.0,
  'periodo': '2026-09-01',
  'origen': 'PROSEGUR',
  'comprobante': None,
  'estado': None,
  'seq': 73}]

SEED_VERSION = "2026-09-09-ultimo-excel-14217700"

def ensure_initial_data():
    """Sincroniza una sola vez la base con el último Excel entregado por Martín."""
    conn = get_conn()
    conn.execute("CREATE TABLE IF NOT EXISTS app_meta(clave TEXT PRIMARY KEY, valor TEXT)")
    row = conn.execute("SELECT valor FROM app_meta WHERE clave='seed_version'").fetchone()
    current_version = row["valor"] if row else None
    if current_version == SEED_VERSION:
        conn.close()
        return

    autorizadas = conn.execute(
        "SELECT COUNT(*) n FROM comprobantes_arca WHERE cae IS NOT NULL AND TRIM(cae)<>''"
    ).fetchone()["n"]
    if autorizadas:
        # Nunca pisamos una base que ya tenga comprobantes fiscales reales/autorizados.
        conn.execute(
            "INSERT INTO app_meta(clave,valor) VALUES('seed_warning',?) "
            "ON CONFLICT(clave) DO UPDATE SET valor=excluded.valor",
            (f"No se migró {SEED_VERSION}: existen comprobantes con CAE.",)
        )
        conn.commit()
        conn.close()
        return

    # En esta etapa todavía no hay comprobantes fiscales válidos: limpiamos borradores
    # y reemplazamos solamente la base administrativa por el último Excel.
    conn.execute("DELETE FROM comprobante_items")
    conn.execute("DELETE FROM comprobantes_arca")
    conn.execute("DELETE FROM movimientos")
    conn.execute("DELETE FROM honorarios")
    conn.execute("DELETE FROM clientes")

    for c in INITIAL_CLIENTS:
        conn.execute("""
            INSERT INTO clientes(
                nombre,cuit,modalidad,honorario,vigente_desde,dia_generacion,activo,observaciones
            ) VALUES(?,?,?,?,?,?,?,?)
        """, (
            c["nombre"], c["cuit"], c["modalidad"], c["honorario"], c["vigente_desde"],
            c["dia_generacion"], c["activo"], c["observaciones"]
        ))

    ids = {r["nombre"]: r["id"] for r in conn.execute("SELECT id,nombre FROM clientes").fetchall()}
    now = datetime.now().isoformat()
    for m in INITIAL_MOVEMENTS:
        cid = ids.get(m["cliente"])
        if not cid:
            continue
        desc = m["descripcion"] or ""
        if m.get("origen"):
            desc = f"{desc} · Origen: {m['origen']}" if desc else f"Origen: {m['origen']}"
        conn.execute("""
            INSERT INTO movimientos(
                fecha,cliente_id,tipo,descripcion,importe,periodo,comprobante,pdf_path,
                estado_conciliacion,creado_en
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
        """, (
            m["fecha"], cid, m["tipo"], desc, m["importe"], m["periodo"],
            m["comprobante"], None, m["estado"], now
        ))

    conn.execute(
        "INSERT INTO app_meta(clave,valor) VALUES('seed_version',?) "
        "ON CONFLICT(clave) DO UPDATE SET valor=excluded.valor",
        (SEED_VERSION,)
    )
    conn.commit()
    conn.close()


def ensure_emitters():
    emisores = [
        ("Juan Ignacio Sirvent", "20-37769536-5"),
        ("Martín Nicolás Sirvent", "20-35140724-8"),
        ("Melissa Jennifer Bulacio Juarez", "27-41149423-9"),
    ]
    conn = get_conn()
    for nombre, cuit in emisores:
        conn.execute(
            """INSERT INTO emisores(nombre,cuit,activo,condicion_iva,regimen_iva,iva_alicuota,precios_incluyen_iva,ambiente_arca)
               VALUES(?,?,1,'Responsable Monotributo','RESPONSABLE_MONOTRIBUTO',0,1,'HOMOLOGACION')
               ON CONFLICT(cuit) DO UPDATE SET
                 nombre=excluded.nombre,
                 condicion_iva='Responsable Monotributo',
                 regimen_iva='RESPONSABLE_MONOTRIBUTO',
                 iva_alicuota=0,
                 precios_incluyen_iva=1""",
            (nombre, cuit)
        )
    conn.commit()
    conn.close()

def get_emisores(active_only=True):
    where = "WHERE activo=1" if active_only else ""
    return query_df(f"SELECT * FROM emisores {where} ORDER BY nombre")

def emisor_label(row):
    return f"{row['nombre']} — {row['cuit']}"

def ensure_invoice_items():
    conn = get_conn()
    n = conn.execute("SELECT COUNT(*) FROM items_facturacion").fetchone()[0]
    if n == 0:
        conn.executemany(
            "INSERT INTO items_facturacion(nombre,descripcion,precio_sugerido,activo) VALUES(?,?,?,1)",
            [
                (
                    "Servicio mensual de Higiene y Seguridad",
                    "Servicio de Higiene y Seguridad en el Trabajo según Ley N° 19.587/72.",
                    0
                ),
                (
                    "Medición Res. SRT 900/15",
                    "Medición de puesta a tierra, continuidad de las masas y prueba de disyuntores según Res. SRT 900/15.",
                    0
                ),
            ]
        )
        conn.commit()
    conn.close()

def month_bounds(d):
    from calendar import monthrange
    return d.replace(day=1), d.replace(day=monthrange(d.year, d.month)[1])

def factura_periodo(fecha_emision, tipo, manual_desde=None, manual_hasta=None):
    if tipo == "Mes vigente":
        d, h = month_bounds(fecha_emision)
        return d, h, d.strftime("%m/%Y")
    if tipo == "Mes vencido":
        if fecha_emision.month == 1:
            base = fecha_emision.replace(year=fecha_emision.year-1, month=12, day=1)
        else:
            base = fecha_emision.replace(month=fecha_emision.month-1, day=1)
        d, h = month_bounds(base)
        return d, h, d.strftime("%m/%Y")
    d, h = manual_desde, manual_hasta
    texto = f"{d.strftime('%d/%m/%Y')} al {h.strftime('%d/%m/%Y')}" if d and h else ""
    return d, h, texto

def catalogo_items():
    return query_df("SELECT * FROM items_facturacion WHERE activo=1 ORDER BY nombre")

def guardar_borrador_arca(cliente_id, emisor_id, fecha_emision, p_desde, p_hasta, p_texto,
                          tipo_periodo, tipo_comprobante, items, observaciones=""):
    conn = get_conn()
    total = sum(float(i["subtotal"]) for i in items)
    cur = conn.execute("""
        INSERT INTO comprobantes_arca(
            cliente_id,emisor_id,fecha_emision,periodo_desde,periodo_hasta,periodo_texto,
            tipo_periodo,tipo_comprobante,total,observaciones,creado_en
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
    """, (
        cliente_id, emisor_id, fecha_emision.isoformat(),
        p_desde.isoformat() if p_desde else None,
        p_hasta.isoformat() if p_hasta else None,
        p_texto, tipo_periodo, tipo_comprobante, total, observaciones,
        datetime.now().isoformat()
    ))
    fid = cur.lastrowid
    for i in items:
        conn.execute("""
            INSERT INTO comprobante_items(
                comprobante_id,item_catalogo_id,descripcion,cantidad,precio_unitario,subtotal
            ) VALUES(?,?,?,?,?,?)
        """, (
            fid, i.get("item_catalogo_id"), i["descripcion"],
            float(i["cantidad"]), float(i["precio_unitario"]), float(i["subtotal"])
        ))
    conn.commit()
    conn.close()
    return fid, total

MAIL_FROM = "sysgroupmdp@gmail.com"
MAIL_SUBJECT = "FACTURACION"
MAIL_BODY = """Estimad@ cliente:

Se adjunta facturación mensual del Servicio de Higiene y Seguridad en el Trabajo según Ley N° 19.587/72 y decretos reglamentarios.

Atte.

Equipo administrativo
Consultora S&S Group
2236168134 / 2235942206
www.sysgroupmdp.com
sysgroupmdp@gmail.com
"""

def gmail_configurada():
    return bool(_secret("gmail_app_password"))

def enviar_factura_por_email(comprobante_id, destinatario=None):
    row = query_df("""
        SELECT a.*, c.nombre cliente, c.email_facturacion, c.envio_automatico_factura
        FROM comprobantes_arca a
        JOIN clientes c ON c.id=a.cliente_id
        WHERE a.id=?
    """, (comprobante_id,))
    if len(row) != 1:
        return False, "No se encontró el comprobante."
    r = row.iloc[0]
    destino = (destinatario or r.get("email_facturacion") or "").strip()
    if not destino:
        return False, "El cliente no tiene email de facturación configurado."
    if str(r.get("estado_arca") or "").upper() != "AUTORIZADA":
        return False, "La factura todavía no está autorizada por ARCA."
    pdf_path = str(r.get("pdf_path") or "").strip()
    if not pdf_path or not Path(pdf_path).exists():
        return False, "La factura autorizada todavía no tiene un PDF disponible."
    if not gmail_configurada():
        return False, "Falta configurar la credencial privada de Gmail en los secretos de la app."

    try:
        password = _secret("gmail_app_password")
        msg = EmailMessage()
        msg["From"] = MAIL_FROM
        msg["To"] = destino
        msg["Subject"] = MAIL_SUBJECT
        msg.set_content(MAIL_BODY)
        pdf_bytes = Path(pdf_path).read_bytes()
        msg.add_attachment(pdf_bytes, maintype="application", subtype="pdf", filename=Path(pdf_path).name)

        context = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as smtp:
            smtp.login(MAIL_FROM, password)
            smtp.send_message(msg)

        now = datetime.now().isoformat(timespec="seconds")
        execute("UPDATE comprobantes_arca SET email_enviado=1,email_enviado_a=?,email_enviado_en=?,email_error=NULL WHERE id=?",
                (destino, now, comprobante_id))
        execute("INSERT INTO email_log(comprobante_id,cliente_id,destinatario,asunto,estado,detalle,fecha_hora) VALUES(?,?,?,?,?,?,?)",
                (comprobante_id, int(r["cliente_id"]), destino, MAIL_SUBJECT, "ENVIADO", None, now))
        return True, f"Email enviado a {destino}."
    except Exception as e:
        now = datetime.now().isoformat(timespec="seconds")
        detalle = str(e)[:500]
        execute("UPDATE comprobantes_arca SET email_error=? WHERE id=?", (detalle, comprobante_id))
        execute("INSERT INTO email_log(comprobante_id,cliente_id,destinatario,asunto,estado,detalle,fecha_hora) VALUES(?,?,?,?,?,?,?)",
                (comprobante_id, int(r["cliente_id"]), destino, MAIL_SUBJECT, "ERROR", detalle, now))
        return False, f"No se pudo enviar el email: {detalle}"

def registrar_factura_autorizada(comprobante_id, pdf_path=None):
    """Registra la deuda de una factura ya autorizada. No vuelve a emitir fiscalmente."""
    row = query_df("SELECT * FROM comprobantes_arca WHERE id=?", (comprobante_id,))
    if len(row) != 1:
        return False, "Comprobante inexistente."
    r = row.iloc[0]
    if pdf_path:
        execute("UPDATE comprobantes_arca SET estado_arca='AUTORIZADA', pdf_path=? WHERE id=?", (str(pdf_path), comprobante_id))
    else:
        execute("UPDATE comprobantes_arca SET estado_arca='AUTORIZADA' WHERE id=?", (comprobante_id,))

    nro = r.get("numero_comprobante")
    pv = r.get("punto_venta")
    t = r.get("tipo_comprobante") or ""
    comp_ref = f"ARCA-{t}-{int(pv or 0):05d}-{int(nro or comprobante_id):08d}"
    try:
        execute("""INSERT INTO movimientos
        (fecha,cliente_id,tipo,descripcion,importe,periodo,comprobante,pdf_path,estado_conciliacion,creado_en)
        VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (r["fecha_emision"], int(r["cliente_id"]), "Factura/Cargo", "Factura autorizada ARCA", float(r["total"]),
         r["periodo_desde"] or r["fecha_emision"][:7] + "-01", comp_ref, str(pdf_path) if pdf_path else None,
         "Pendiente", datetime.now().isoformat()))
    except sqlite3.IntegrityError:
        pass

    if not pdf_path:
        return True, "Factura autorizada y deuda registrada. Falta regenerar el PDF antes de enviarla."
    cliente = query_df("SELECT email_facturacion,envio_automatico_factura FROM clientes WHERE id=?", (int(r["cliente_id"]),))
    if len(cliente) and int(cliente.iloc[0].get("envio_automatico_factura") or 0) == 1:
        ok, msg = enviar_factura_por_email(comprobante_id)
        if not ok:
            return True, "Factura autorizada y registrada. " + msg
        return True, msg
    return True, "Factura autorizada y registrada. Envío automático desactivado para este cliente."


def arca_credenciales(cuit):
    d = digits(cuit)
    cert_b64 = _secret(f"ARCA_{d}_CERT_B64")
    key_b64 = _secret(f"ARCA_{d}_KEY_B64")
    if not cert_b64 or not key_b64:
        return None, None
    try:
        return base64.b64decode(cert_b64), base64.b64decode(key_b64)
    except Exception as e:
        raise ARCAError(f"Credenciales ARCA mal codificadas para CUIT {cuit}: {e}")


def arca_configurada_para(cuit):
    cert, key = arca_credenciales(cuit)
    return bool(cert and key)


def _fiscal_amounts(emisor, tipo_comprobante, importe_ingresado):
    # Los tres emisores son monotributistas: sólo Factura C, sin IVA discriminado.
    tipo = str(tipo_comprobante or "C").upper()
    if tipo != "C":
        raise ARCAError("Los emisores configurados son monotributistas: sólo se permite Factura C.")
    total = round(float(importe_ingresado), 2)
    return total, total, 0.0, 0.0


IVA_RECEPTOR_OPCIONES = {
    "Responsable Inscripto": 1,
    "IVA Exento": 4,
    "Consumidor Final": 5,
    "Responsable Monotributo": 6,
    "Sujeto No Categorizado": 7,
    "IVA No Alcanzado": 15,
}

def _doc_receptor(cliente, total):
    doc = digits(cliente.get("cuit") or "")
    cond = int(cliente.get("condicion_iva_receptor_id") or 0)
    if len(doc) == 11:
        return 80, doc  # CUIT
    if len(doc) in (7, 8):
        return 96, doc  # DNI
    if cond == 5 and float(total) < 10000000:
        return 99, "0"  # Consumidor final no identificado
    return None, None


def _validate_production_ready(emisor, cliente, comprobante):
    faltan = []
    if not app_password_configurada():
        faltan.append("contraseña privada de acceso a la app")
    if not str(emisor.get("domicilio_fiscal") or "").strip():
        faltan.append("domicilio fiscal del emisor")
    if not int(emisor.get("punto_venta") or 0):
        faltan.append("punto de venta ARCA")
    if not arca_configurada_para(emisor.get("cuit")):
        faltan.append("certificado y clave privada ARCA")
    if not cliente.get("condicion_iva_receptor_id") or pd.isna(cliente.get("condicion_iva_receptor_id")):
        faltan.append("condición IVA del cliente")
    doc_tipo, doc_nro = _doc_receptor(cliente, float(comprobante.get("total") or 0))
    if not doc_tipo:
        faltan.append("CUIT/DNI del cliente (o Consumidor Final por debajo del límite de identificación)")
    if not comprobante.get("fecha_vto_pago") or pd.isna(comprobante.get("fecha_vto_pago")):
        faltan.append("fecha de vencimiento de pago")
    if str(comprobante.get("tipo_comprobante") or "C").upper() != "C":
        faltan.append("tipo de comprobante C")
    return faltan


def probar_conexion_arca(emisor_id):
    emis = query_df("SELECT * FROM emisores WHERE id=?", (emisor_id,))
    if len(emis) != 1:
        raise ARCAError("Emisor inexistente.")
    e = emis.iloc[0]
    cert, key = arca_credenciales(e["cuit"])
    if not cert or not key:
        raise ARCAError("Faltan certificado/clave ARCA en Secrets.")
    ambiente = str(e.get("ambiente_arca") or "HOMOLOGACION").upper()
    ta = wsaa_login(cert, key, ambiente=ambiente)
    ptos = wsfe_puntos_venta(ta, e["cuit"], ambiente)
    return ta, ptos


def _safe_filename_part(value):
    s = str(value or "").strip()
    s = re.sub(r'[\\/:*?"<>|]+', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip(' .')
    return s or "CLIENTE"


def nombre_pdf_factura(cliente_nombre, tipo_periodo, periodo_texto, fecha_emision):
    cliente = _safe_filename_part(cliente_nombre)
    tipo_periodo = str(tipo_periodo or "").strip()
    periodo_texto = str(periodo_texto or "").strip()
    fecha = str(fecha_emision or "").strip()
    if tipo_periodo in ("Mes vigente", "Mes vencido") and periodo_texto:
        periodo = periodo_texto.replace("/", "-")
        return f"{cliente} - {periodo}.pdf"
    try:
        fecha_fmt = datetime.strptime(fecha, "%Y-%m-%d").strftime("%d-%m-%Y")
    except Exception:
        fecha_fmt = fecha.replace("/", "-") or date.today().strftime("%d-%m-%Y")
    return f"{cliente} - {fecha_fmt}.pdf"


def _datos_pdf_comprobante(comprobante_id):
    row = query_df("""
        SELECT a.*, c.nombre cliente_nombre,c.cuit cliente_cuit,c.domicilio cliente_domicilio,
               c.condicion_iva_receptor_id,c.condicion_iva_receptor_desc,
               e.id emisor_id,e.nombre emisor_nombre,e.cuit emisor_cuit,e.condicion_iva,
               e.punto_venta emisor_punto_venta,e.domicilio_fiscal,e.ingresos_brutos,
               e.inicio_actividades,e.regimen_iva,e.iva_alicuota,e.ambiente_arca,e.precios_incluyen_iva
        FROM comprobantes_arca a
        JOIN clientes c ON c.id=a.cliente_id
        JOIN emisores e ON e.id=a.emisor_id
        WHERE a.id=?
    """, (comprobante_id,))
    if len(row) != 1:
        raise ValueError("No se encontró el comprobante.")
    r = row.iloc[0]
    emisor = {
        "id": r["emisor_id"], "nombre": r["emisor_nombre"], "cuit": r["emisor_cuit"],
        "condicion_iva": r.get("condicion_iva"), "punto_venta": r.get("emisor_punto_venta"),
        "domicilio_fiscal": r.get("domicilio_fiscal"), "ingresos_brutos": r.get("ingresos_brutos"),
        "inicio_actividades": r.get("inicio_actividades"), "regimen_iva": r.get("regimen_iva"),
        "iva_alicuota": r.get("iva_alicuota"), "ambiente_arca": r.get("ambiente_arca"),
        "precios_incluyen_iva": r.get("precios_incluyen_iva")
    }
    cliente = {
        "id": r["cliente_id"], "nombre": r["cliente_nombre"], "cuit": r["cliente_cuit"],
        "domicilio": r.get("cliente_domicilio"),
        "condicion_iva_receptor_id": r.get("condicion_iva_receptor_id"),
        "condicion_iva_receptor_desc": r.get("condicion_iva_receptor_desc")
    }
    comp = r.to_dict()
    total = float(comp.get("total") or 0)
    comp["doc_tipo"] = _doc_receptor(cliente, total)[0]
    items = query_df("SELECT * FROM comprobante_items WHERE comprobante_id=? ORDER BY id", (comprobante_id,)).to_dict("records")
    return r, emisor, cliente, comp, items


def regenerar_pdf_autorizado(comprobante_id):
    r, emisor, cliente, comp, items = _datos_pdf_comprobante(comprobante_id)
    if str(comp.get("estado_arca") or "").upper() != "AUTORIZADA" or not str(comp.get("cae") or ""):
        return False, "La factura todavía no está autorizada por ARCA."
    filename = nombre_pdf_factura(cliente.get("nombre"), comp.get("tipo_periodo"), comp.get("periodo_texto"), comp.get("fecha_emision"))
    path = PDF_DIR / filename
    generar_pdf_factura(path, emisor=emisor, cliente=cliente, comprobante=comp, items=items)
    execute("UPDATE comprobantes_arca SET pdf_path=? WHERE id=?", (str(path), comprobante_id))
    comp_ref = f"{int(comp.get('punto_venta') or emisor.get('punto_venta') or 0):05d}-{int(comp.get('numero_comprobante') or 0):08d}"
    execute("UPDATE movimientos SET pdf_path=? WHERE cliente_id=? AND comprobante=?", (str(path), int(comp["cliente_id"]), comp_ref))
    return True, f"PDF regenerado con plantilla {PDF_TEMPLATE_VERSION}: {filename}"


def emitir_comprobante_arca(comprobante_id):
    row = query_df("""
        SELECT a.*, c.nombre cliente_nombre,c.cuit cliente_cuit,c.domicilio cliente_domicilio,
               c.email_facturacion,c.envio_automatico_factura,c.condicion_iva_receptor_id,c.condicion_iva_receptor_desc,
               e.nombre emisor_nombre,e.cuit emisor_cuit,e.condicion_iva,e.punto_venta AS emisor_punto_venta,e.domicilio_fiscal,
               e.ingresos_brutos,e.inicio_actividades,e.regimen_iva,e.iva_alicuota,e.ambiente_arca,e.precios_incluyen_iva
        FROM comprobantes_arca a
        JOIN clientes c ON c.id=a.cliente_id
        JOIN emisores e ON e.id=a.emisor_id
        WHERE a.id=?
    """, (comprobante_id,))
    if len(row) != 1:
        return False, "No se encontró el comprobante."
    r = row.iloc[0]
    if str(r.get("estado_arca") or "").upper() == "AUTORIZADA" and str(r.get("cae") or ""):
        return True, "La factura ya está autorizada por ARCA. No se volvió a emitir."

    emisor = {
        "id": r["emisor_id"], "nombre": r["emisor_nombre"], "cuit": r["emisor_cuit"],
        "condicion_iva": r.get("condicion_iva"), "punto_venta": r.get("emisor_punto_venta"),
        "domicilio_fiscal": r.get("domicilio_fiscal"), "ingresos_brutos": r.get("ingresos_brutos"),
        "inicio_actividades": r.get("inicio_actividades"), "regimen_iva": r.get("regimen_iva"),
        "iva_alicuota": r.get("iva_alicuota"), "ambiente_arca": r.get("ambiente_arca"),
        "precios_incluyen_iva": r.get("precios_incluyen_iva")
    }
    cliente = {
        "id": r["cliente_id"], "nombre": r["cliente_nombre"], "cuit": r["cliente_cuit"],
        "domicilio": r.get("cliente_domicilio"),
        "condicion_iva_receptor_id": r.get("condicion_iva_receptor_id"),
        "condicion_iva_receptor_desc": r.get("condicion_iva_receptor_desc")
    }
    faltan = _validate_production_ready(emisor, cliente, r)
    if faltan:
        return False, "Falta completar: " + ", ".join(faltan) + "."

    try:
        cert, key = arca_credenciales(emisor["cuit"])
        ambiente = str(emisor.get("ambiente_arca") or "HOMOLOGACION").upper()
        tipo = "C"
        cbte_tipo = CBTE_CODES[tipo]
        total, neto, iva, rate = _fiscal_amounts(emisor, tipo, float(r["total"]))
        ta = wsaa_login(cert, key, ambiente=ambiente)
        ultimo = wsfe_ultimo_autorizado(ta, emisor["cuit"], int(emisor["punto_venta"]), cbte_tipo, ambiente)
        siguiente = ultimo + 1
        f_em = datetime.strptime(str(r["fecha_emision"]), "%Y-%m-%d").strftime("%Y%m%d")
        f_desde = datetime.strptime(str(r["periodo_desde"]), "%Y-%m-%d").strftime("%Y%m%d") if r.get("periodo_desde") else None
        f_hasta = datetime.strptime(str(r["periodo_hasta"]), "%Y-%m-%d").strftime("%Y%m%d") if r.get("periodo_hasta") else None
        f_vto = datetime.strptime(str(r["fecha_vto_pago"]), "%Y-%m-%d").strftime("%Y%m%d")
        resp = wsfe_solicitar_cae(
            ta, emisor["cuit"], ambiente,
            punto_venta=int(emisor["punto_venta"]), cbte_tipo=cbte_tipo, concepto=2,
            doc_tipo=_doc_receptor(cliente, total)[0], doc_nro=_doc_receptor(cliente, total)[1], cbte_nro=siguiente, fecha_cbte=f_em,
            imp_total=total, imp_neto=neto, imp_iva=iva,
            condicion_iva_receptor_id=int(cliente["condicion_iva_receptor_id"]),
            fecha_serv_desde=f_desde, fecha_serv_hasta=f_hasta, fecha_vto_pago=f_vto,
            iva_rate=rate if iva > 0 else None,
        )
        obs_txt = "; ".join(f"{o['code']}: {o['msg']}" for o in resp.get("observaciones", []))
        err_txt = "; ".join(f"{e['code']}: {e['msg']}" for e in resp.get("errors", []))
        if not resp.get("ok"):
            execute("UPDATE comprobantes_arca SET estado_arca='RECHAZADA',arca_error=?,arca_observaciones=? WHERE id=?",
                    (err_txt or f"Resultado ARCA: {resp.get('resultado')}", obs_txt or None, comprobante_id))
            return False, "ARCA rechazó la factura: " + (err_txt or str(resp.get("resultado")))

        cae = str(resp["cae"])
        cae_vto_raw = str(resp["cae_vto"])
        cae_vto = datetime.strptime(cae_vto_raw, "%Y%m%d").strftime("%Y-%m-%d") if len(cae_vto_raw) == 8 else cae_vto_raw
        execute("""UPDATE comprobantes_arca
                   SET estado_arca='AUTORIZADA',punto_venta=?,numero_comprobante=?,cae=?,vencimiento_cae=?,
                       cbte_tipo_codigo=?,imp_neto=?,imp_iva=?,total=?,arca_observaciones=?,arca_error=NULL
                   WHERE id=?""",
                (int(emisor["punto_venta"]), int(resp["cbte_nro"]), cae, cae_vto, cbte_tipo, neto, iva, total,
                 obs_txt or None, comprobante_id))

        # Persistimos CAE antes de generar PDF para nunca perder una autorización fiscal ya obtenida.
        comp = query_df("SELECT * FROM comprobantes_arca WHERE id=?", (comprobante_id,)).iloc[0].to_dict()
        comp["doc_tipo"] = _doc_receptor(cliente, total)[0]
        items_df = query_df("SELECT * FROM comprobante_items WHERE comprobante_id=? ORDER BY id", (comprobante_id,))
        items = items_df.to_dict("records")
        safe = nombre_pdf_factura(cliente.get("nombre"), comp.get("tipo_periodo"), comp.get("periodo_texto"), comp.get("fecha_emision"))
        pdf_path = PDF_DIR / safe
        try:
            generar_pdf_factura(pdf_path, emisor=emisor, cliente=cliente, comprobante=comp, items=items)
        except Exception as pdf_e:
            registrar_factura_autorizada(comprobante_id, None)
            execute("UPDATE comprobantes_arca SET arca_error=? WHERE id=?",
                    (f"CAE obtenido correctamente; error generando PDF: {pdf_e}", comprobante_id))
            return True, f"ARCA autorizó la factura (CAE {cae}), pero hubo un problema generando el PDF: {pdf_e}"

        ok_reg, msg_reg = registrar_factura_autorizada(comprobante_id, pdf_path)
        prefix = "PRODUCCIÓN" if ambiente == "PRODUCCION" else "HOMOLOGACIÓN"
        return True, f"{prefix}: factura autorizada. N° {int(emisor['punto_venta']):05d}-{int(resp['cbte_nro']):08d} · CAE {cae}. {msg_reg}"
    except ARCAError as e:
        execute("UPDATE comprobantes_arca SET arca_error=? WHERE id=?", (str(e)[:1000], comprobante_id))
        return False, str(e)
    except Exception as e:
        execute("UPDATE comprobantes_arca SET arca_error=? WHERE id=?", (str(e)[:1000], comprobante_id))
        return False, f"Error inesperado al emitir: {e}"

def extract_pdf_text(uploaded):
    try:
        reader = PdfReader(uploaded)
        parts = []
        for p in reader.pages[:4]:
            parts.append(p.extract_text() or "")
        return "\n".join(parts)
    except Exception:
        return ""

def parse_arg_money(s):
    if not s:
        return None
    s = s.strip().replace("$","").replace(" ","")
    # Argentine formatting: 1.234.567,89
    if "," in s and "." in s:
        s = s.replace(".","").replace(",",".")
    elif "," in s:
        s = s.replace(",",".")
    else:
        # Assume dots are thousands when groups of 3 repeat
        if re.match(r"^\d{1,3}(\.\d{3})+$", s):
            s = s.replace(".","")
    try:
        return float(s)
    except:
        return None

def parse_invoice(text):
    out = {"cuit":None, "fecha":None, "comprobante":None, "importe":None}
    if not text:
        return out

    cuit = re.search(r"CUIT[:\s]*(\d{2}-?\d{8}-?\d)", text, re.I)
    if cuit:
        out["cuit"] = re.sub(r"\D","",cuit.group(1))

    fecha = re.search(r"(?:Fecha(?: de Emisi[oó]n)?|Emisi[oó]n)[:\s]*(\d{1,2}/\d{1,2}/\d{4})", text, re.I)
    if fecha:
        try:
            out["fecha"] = datetime.strptime(fecha.group(1), "%d/%m/%Y").date()
        except:
            pass

    comp = re.search(r"(?:Comp\.?|Comprobante|Factura)\s*(?:N[°ºo]\.?\s*)?([A-Z]?\s*\d{3,5}-\d{6,8})", text, re.I)
    if comp:
        out["comprobante"] = re.sub(r"\s+"," ",comp.group(1)).strip()

    pats = [
        r"Importe Total[:\s$]*([\d\.\,]+)",
        r"Total[:\s$]*([\d\.\,]+)",
        r"TOTAL[:\s$]*([\d\.\,]+)"
    ]
    vals = []
    for pat in pats:
        for m in re.findall(pat,text,re.I):
            v = parse_arg_money(m)
            if v and v > 0:
                vals.append(v)
    if vals:
        out["importe"] = max(vals)
    return out

def resolve_cliente(parsed, filename):
    clientes = get_clientes(True)
    if parsed.get("cuit"):
        m = clientes[clientes["cuit"].fillna("").astype(str).str.replace(r"\D","",regex=True)==parsed["cuit"]]
        if len(m)==1:
            return int(m.iloc[0]["id"]), m.iloc[0]["nombre"]
    low = filename.lower()
    for _,r in clientes.iterrows():
        words=[w.lower() for w in re.findall(r"[A-Za-zÁÉÍÓÚÑáéíóúñ0-9]+",r["nombre"]) if len(w)>=5]
        if any(w in low for w in words):
            return int(r["id"]), r["nombre"]
    return None, None

def generate_monthly_notices(period=None):
    if period is None:
        period = date.today().replace(day=1)
    periodo = period.isoformat()
    clientes = query_df("SELECT * FROM clientes WHERE activo=1 AND modalidad='Aviso de pago'")
    generated=0
    for _,r in clientes.iterrows():
        fecha = period.replace(day=min(int(r["dia_generacion"] or 1),28)).isoformat()
        try:
            execute("""INSERT INTO movimientos
            (fecha,cliente_id,tipo,descripcion,importe,periodo,comprobante,pdf_path,estado_conciliacion,creado_en)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (fecha,int(r["id"]),"Aviso de pago",f"Honorarios {period.strftime('%m/%Y')}",
             float(r["honorario"] or 0),periodo,f"AVISO-{period.strftime('%Y%m')}",None,None,datetime.now().isoformat()))
            generated+=1
        except sqlite3.IntegrityError:
            pass
    return generated

def balances_df():
    return query_df("""
    SELECT c.id, c.nombre, c.modalidad, c.honorario,
           COALESCE(SUM(m.importe),0) AS saldo
    FROM clientes c
    LEFT JOIN movimientos m ON m.cliente_id=c.id
    WHERE c.activo=1
    GROUP BY c.id,c.nombre,c.modalidad,c.honorario
    ORDER BY saldo DESC, c.nombre
    """)

def guardar_datos_fiscales_cliente(cliente_id, condicion_desc, documento=None, email=None):
    cond_id = IVA_RECEPTOR_OPCIONES.get(condicion_desc)
    execute("""UPDATE clientes
               SET condicion_iva_receptor_id=?, condicion_iva_receptor_desc=?,
                   cuit=COALESCE(NULLIF(?,''),cuit),
                   email_facturacion=COALESCE(NULLIF(?,''),email_facturacion)
               WHERE id=?""",
            (cond_id, condicion_desc, (documento or "").strip(), (email or "").strip(), int(cliente_id)))


def crear_o_recuperar_cliente_ocasional(nombre, documento, condicion_desc, email=""):
    nombre = (nombre or "").strip()
    documento = (documento or "").strip()
    if not nombre:
        raise ValueError("Ingresá el nombre o razón social del cliente ocasional.")
    cond_id = IVA_RECEPTOR_OPCIONES.get(condicion_desc)
    if not cond_id:
        raise ValueError("Seleccioná la condición IVA del cliente.")
    if documento:
        d = digits(documento)
        existente = query_df("""SELECT * FROM clientes
                               WHERE REPLACE(REPLACE(REPLACE(COALESCE(cuit,''),'-',''),' ',''),'.','')=?
                               LIMIT 1""", (d,))
        if len(existente):
            cid = int(existente.iloc[0]["id"])
            execute("""UPDATE clientes SET nombre=?,activo=1,modalidad='Factura',
                       condicion_iva_receptor_id=?,condicion_iva_receptor_desc=?,
                       email_facturacion=COALESCE(NULLIF(?,''),email_facturacion)
                       WHERE id=?""",
                    (nombre, cond_id, condicion_desc, email.strip(), cid))
            return cid
    existente = query_df("SELECT * FROM clientes WHERE UPPER(nombre)=UPPER(?) LIMIT 1", (nombre,))
    if len(existente):
        cid = int(existente.iloc[0]["id"])
        guardar_datos_fiscales_cliente(cid, condicion_desc, documento, email)
        return cid
    execute("""INSERT INTO clientes(
                nombre,cuit,modalidad,honorario,vigente_desde,dia_generacion,activo,observaciones,
                email_facturacion,envio_automatico_factura,condicion_iva_receptor_id,condicion_iva_receptor_desc
              ) VALUES(?,?, 'Factura',0,?,1,1,'Cliente ocasional creado al facturar',?,1,?,?)""",
            (nombre, documento or None, date.today().replace(day=1).isoformat(),
             email.strip() or None, cond_id, condicion_desc))
    return int(query_df("SELECT id FROM clientes WHERE UPPER(nombre)=UPPER(?) ORDER BY id DESC LIMIT 1", (nombre,)).iloc[0]["id"])

init_db()
ensure_initial_data()
ensure_emitters()
ensure_invoice_items()

st.title("S&S Group · Gestión Administrativa")
st.caption("Clientes · Facturación · Cuenta corriente · Pagos · Avisos · Trabajos puntuales · ARCA")

# Los avisos mensuales no se generan al abrir la app.
# Se generan desde la pestaña "Avisos de pago" para evitar cargos accidentales.

tabs = st.tabs(["Panel","Emitir factura","Facturas recibidas/PDF","Avisos de pago","Pagos","Trabajos extras","Clientes","Ítems / Leyendas","Cuenta corriente","Exportar","ARCA"])

with tabs[0]:
    saldos = balances_df()
    c1,c2,c3,c4 = st.columns(4)
    c1.metric("Total por cobrar", money(saldos["saldo"].clip(lower=0).sum()))
    c2.metric("Clientes activos", len(saldos))
    c3.metric("Clientes con deuda", int((saldos["saldo"]>0).sum()))
    movs = query_df("SELECT COUNT(*) n FROM movimientos").iloc[0]["n"]
    c4.metric("Movimientos", int(movs))
    st.caption("Base actualizada desde Control de cuentas(1).xlsx · saldo fuente: $ 14.217.700.")
    st.subheader("Estado por cliente")
    show = saldos.copy()
    show["honorario"] = show["honorario"].map(money)
    show["saldo"] = show["saldo"].map(money)
    st.dataframe(show[["nombre","modalidad","honorario","saldo"]], use_container_width=True, hide_index=True)


with tabs[1]:
    st.subheader("Emitir factura C")
    st.caption("Los tres emisores son monotributistas. El sistema sólo emite Factura C.")

    emisores = get_emisores(True)
    if len(emisores) == 0:
        st.warning("No hay emisores configurados.")
    else:
        modo_cliente = st.radio("Cliente", ["Habitual", "Ocasional / nuevo"], horizontal=True, key="ef_modo_cliente")
        cliente_row = None
        cliente_id_previo = None
        cliente_nombre = ""
        documento_cliente = ""
        email_cliente = ""
        cond_actual = ""

        if modo_cliente == "Habitual":
            clientes = get_clientes(True)
            nom = st.selectbox("Cliente habitual", clientes["nombre"].tolist(), key="ef_cliente")
            cliente_row = clientes[clientes["nombre"] == nom].iloc[0]
            cliente_id_previo = int(cliente_row["id"])
            cliente_nombre = str(cliente_row["nombre"])
            documento_cliente = str(cliente_row.get("cuit") or "")
            email_cliente = str(cliente_row.get("email_facturacion") or "")
            cond_actual = str(cliente_row.get("condicion_iva_receptor_desc") or "")
        else:
            cno1, cno2 = st.columns(2)
            cliente_nombre = cno1.text_input("Nombre / Razón social", key="ef_oc_nombre")
            documento_cliente = cno2.text_input("CUIT o DNI", key="ef_oc_doc",
                                                help="Para Consumidor Final puede quedar vacío si ARCA permite no identificar la operación.")
            email_cliente = st.text_input("Email (opcional)", key="ef_oc_email")

        cond_labels = ["Seleccionar..."] + list(IVA_RECEPTOR_OPCIONES.keys())
        cond_index = cond_labels.index(cond_actual) if cond_actual in cond_labels else 0
        condicion_cliente = st.selectbox("Condición IVA del cliente", cond_labels, index=cond_index, key="ef_cond_iva")

        emisor_options = {emisor_label(r): int(r["id"]) for _, r in emisores.iterrows()}
        default_idx = 0
        if cliente_row is not None and "emisor_predeterminado_id" in cliente_row.index and pd.notna(cliente_row["emisor_predeterminado_id"]):
            ids = list(emisor_options.values())
            if int(cliente_row["emisor_predeterminado_id"]) in ids:
                default_idx = ids.index(int(cliente_row["emisor_predeterminado_id"]))

        c1, c2, c3 = st.columns(3)
        emisor_sel = c1.selectbox("Emisor", list(emisor_options.keys()), index=default_idx, key="ef_emisor")
        fecha_emision = c2.date_input("Fecha de emisión", value=date.today(), key="ef_fecha")
        tipo_periodo = c3.selectbox("Período", ["Mes vigente","Mes vencido","Manual"], key="ef_periodo")

        manual_desde = manual_hasta = None
        if tipo_periodo == "Manual":
            p1, p2 = st.columns(2)
            manual_desde = p1.date_input("Desde", value=date.today().replace(day=1), key="ef_desde")
            manual_hasta = p2.date_input("Hasta", value=date.today(), key="ef_hasta")

        p_desde, p_hasta, p_texto = factura_periodo(fecha_emision, tipo_periodo, manual_desde, manual_hasta)
        st.caption(f"Período facturado: **{p_texto}** · Comprobante: **Factura C**")
        fecha_vto_pago = st.date_input("Vencimiento de pago", value=fecha_emision + timedelta(days=10), key="ef_vto_pago")

        catalogo = catalogo_items()
        etiquetas = {f"{r['nombre']} — {r['descripcion']}": r for _, r in catalogo.iterrows()}
        seleccion = st.multiselect("Ítems / leyendas", list(etiquetas.keys()), key="ef_items")
        items = []
        for idx, etiqueta in enumerate(seleccion):
            r = etiquetas[etiqueta]
            with st.expander(r["nombre"], expanded=True):
                desc = st.text_area("Descripción", value=r["descripcion"], key=f"ef_desc_{idx}_{r['id']}")
                a, b = st.columns(2)
                cantidad = a.number_input("Cantidad", min_value=0.01, value=1.0, step=1.0, key=f"ef_cant_{idx}_{r['id']}")
                precio = b.number_input("Precio unitario", min_value=0.0, value=float(r["precio_sugerido"] or 0), step=1000.0, key=f"ef_precio_{idx}_{r['id']}")
                items.append({"item_catalogo_id": int(r["id"]), "descripcion": desc,
                              "cantidad": cantidad, "precio_unitario": precio, "subtotal": cantidad * precio})

        st.markdown("#### Ítem libre")
        libre = st.text_area("Descripción", key="ef_libre_desc")
        l1, l2 = st.columns(2)
        lc = l1.number_input("Cantidad", min_value=0.0, value=0.0, step=1.0, key="ef_libre_cant")
        lp = l2.number_input("Precio unitario", min_value=0.0, value=0.0, step=1000.0, key="ef_libre_precio")
        if libre.strip() and lc > 0:
            items.append({"item_catalogo_id": None, "descripcion": libre.strip(),
                          "cantidad": lc, "precio_unitario": lp, "subtotal": lc * lp})

        total = sum(float(x["subtotal"]) for x in items)
        st.metric("Total", money(total))
        with st.expander("Observaciones opcionales"):
            obs = st.text_area("Observaciones", key="ef_obs")

        b1, b2 = st.columns(2)
        guardar = b1.button("Guardar borrador", key="ef_guardar")
        emitir = b2.button("Emitir ahora en ARCA", type="primary", key="ef_emitir_arca")

        if guardar or emitir:
            if condicion_cliente == "Seleccionar...":
                st.error("Seleccioná la condición IVA del cliente.")
            elif not items:
                st.error("Agregá al menos un ítem.")
            elif total <= 0:
                st.error("El total debe ser mayor a cero.")
            elif not cliente_nombre.strip():
                st.error("Ingresá el cliente.")
            else:
                try:
                    if modo_cliente == "Ocasional / nuevo":
                        cid = crear_o_recuperar_cliente_ocasional(
                            cliente_nombre, documento_cliente, condicion_cliente, email_cliente
                        )
                    else:
                        cid = int(cliente_id_previo)
                        guardar_datos_fiscales_cliente(cid, condicion_cliente, documento_cliente, email_cliente)

                    emisor_id = emisor_options[emisor_sel]
                    fid, total_guardado = guardar_borrador_arca(
                        cid, emisor_id, fecha_emision, p_desde, p_hasta, p_texto,
                        tipo_periodo, "C", items, obs
                    )
                    execute("UPDATE comprobantes_arca SET fecha_vto_pago=?,tipo_comprobante='C' WHERE id=?",
                            (fecha_vto_pago.isoformat(), fid))
                    if guardar:
                        st.success(f"Borrador #{fid} guardado por {money(total_guardado)}.")
                    else:
                        ok, msg = emitir_comprobante_arca(fid)
                        (st.success if ok else st.error)(msg)
                        if ok and modo_cliente == "Ocasional / nuevo":
                            st.info("El cliente ocasional quedó guardado automáticamente en Clientes y la factura quedó en Pendientes de pago.")
                except Exception as e:
                    st.error(str(e))

with tabs[2]:
    st.subheader("Carga masiva de facturas PDF existentes")
    st.write("Para facturas ya emitidas por fuera del sistema: subilas juntas y se incorporan a la cuenta corriente.")
    files = st.file_uploader("Facturas PDF", type=["pdf"], accept_multiple_files=True)
    if files:
        parsed_rows=[]
        file_map={}
        for f in files:
            raw=f.getvalue()
            file_map[f.name]=raw
            text=extract_pdf_text(io.BytesIO(raw))
            parsed=parse_invoice(text)
            cid,cname=resolve_cliente(parsed,f.name)
            parsed_rows.append({
                "archivo":f.name,
                "cliente_id":cid,
                "cliente":cname or "",
                "fecha":parsed["fecha"] or date.today(),
                "comprobante":parsed["comprobante"] or "",
                "importe":parsed["importe"] or 0.0,
                "estado":"OK" if cid and parsed["importe"] else "REVISAR"
            })
        df=pd.DataFrame(parsed_rows)
        st.dataframe(df[["archivo","cliente","fecha","comprobante","importe","estado"]], use_container_width=True, hide_index=True)

        clientes = get_clientes(True)
        st.caption("Si algún PDF no fue reconocido, podés cargarlo individualmente abajo.")
        if st.button("Confirmar lote reconocido", type="primary"):
            ok=0; errs=[]
            for row in parsed_rows:
                if not row["cliente_id"] or not row["importe"]:
                    continue
                safe_name = datetime.now().strftime("%Y%m%d%H%M%S_") + re.sub(r"[^A-Za-z0-9_.-]","_",row["archivo"])
                p=PDF_DIR/safe_name
                p.write_bytes(file_map[row["archivo"]])
                periodo=date(row["fecha"].year,row["fecha"].month,1).isoformat()
                try:
                    execute("""INSERT INTO movimientos
                    (fecha,cliente_id,tipo,descripcion,importe,periodo,comprobante,pdf_path,estado_conciliacion,creado_en)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (row["fecha"].isoformat(),row["cliente_id"],"Factura/Cargo","Factura",float(row["importe"]),
                     periodo,row["comprobante"] or row["archivo"],str(p),None,datetime.now().isoformat()))
                    ok+=1
                except sqlite3.IntegrityError:
                    errs.append(row["archivo"])
            st.success(f"Facturas cargadas: {ok}")
            if errs:
                st.warning("Posibles duplicadas: " + ", ".join(errs))

    st.divider()
    st.subheader("Carga manual de una factura")
    clientes=get_clientes(True)
    with st.form("factura_manual"):
        nom=st.selectbox("Cliente", clientes["nombre"].tolist(), key="fm_cli")
        fecha=st.date_input("Fecha", value=date.today(), key="fm_fecha")
        comp=st.text_input("Comprobante", key="fm_comp")
        importe=st.number_input("Importe", min_value=0.0, step=1000.0, key="fm_imp")
        archivo=st.file_uploader("PDF opcional", type=["pdf"], key="fm_pdf")
        submitted=st.form_submit_button("Guardar factura")
        if submitted:
            cid=int(clientes.loc[clientes["nombre"]==nom,"id"].iloc[0])
            pdf_path=None
            if archivo:
                p=PDF_DIR/(datetime.now().strftime("%Y%m%d%H%M%S_")+archivo.name)
                p.write_bytes(archivo.getvalue()); pdf_path=str(p)
            execute("""INSERT INTO movimientos
            (fecha,cliente_id,tipo,descripcion,importe,periodo,comprobante,pdf_path,estado_conciliacion,creado_en)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (fecha.isoformat(),cid,"Factura/Cargo","Factura",importe,fecha.replace(day=1).isoformat(),
             comp or None,pdf_path,None,datetime.now().isoformat()))
            st.success("Factura cargada.")

with tabs[3]:
    st.subheader("Avisos de pago")
    periodo=st.date_input("Período", value=date.today().replace(day=1), key="av_periodo")
    if st.button("Generar avisos del período"):
        n=generate_monthly_notices(periodo.replace(day=1))
        st.success(f"Avisos nuevos generados: {n}")
    avisos = query_df("""
      SELECT c.nombre,c.honorario,
             COALESCE(SUM(m.importe),0) saldo
      FROM clientes c LEFT JOIN movimientos m ON m.cliente_id=c.id
      WHERE c.activo=1 AND c.modalidad='Aviso de pago'
      GROUP BY c.id,c.nombre,c.honorario ORDER BY c.nombre
    """)
    if len(avisos):
        avisos["honorario"]=avisos["honorario"].map(money)
        avisos["saldo"]=avisos["saldo"].map(money)
    st.dataframe(avisos,use_container_width=True,hide_index=True)

with tabs[4]:
    st.subheader("Registrar pago")
    clientes=get_clientes(True)
    with st.form("pago"):
        nom=st.selectbox("Cliente",clientes["nombre"].tolist(),key="pg_cli")
        fecha=st.date_input("Fecha del pago",value=date.today(),key="pg_fecha")
        imp=st.number_input("Importe recibido",min_value=0.0,step=1000.0,key="pg_imp")
        desc=st.text_input("Descripción / referencia",value="Pago recibido")
        if st.form_submit_button("Registrar pago",type="primary"):
            cid=int(clientes.loc[clientes["nombre"]==nom,"id"].iloc[0])
            execute("""INSERT INTO movimientos
            (fecha,cliente_id,tipo,descripcion,importe,periodo,comprobante,pdf_path,estado_conciliacion,creado_en)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (fecha.isoformat(),cid,"Pago",desc,-abs(imp),fecha.replace(day=1).isoformat(),None,None,"Registrado",datetime.now().isoformat()))
            st.success("Pago registrado.")

with tabs[5]:
    st.subheader("Trabajo extra / puntual")
    clientes=get_clientes(True)
    with st.form("extra"):
        nom=st.selectbox("Cliente",clientes["nombre"].tolist(),key="ex_cli")
        fecha=st.date_input("Fecha",value=date.today(),key="ex_fecha")
        concepto=st.text_input("Concepto",placeholder="Ej.: Medición de ruido extraordinaria")
        imp=st.number_input("Importe",min_value=0.0,step=1000.0,key="ex_imp")
        comp=st.text_input("Comprobante / referencia opcional",key="ex_comp")
        if st.form_submit_button("Guardar trabajo"):
            cid=int(clientes.loc[clientes["nombre"]==nom,"id"].iloc[0])
            execute("""INSERT INTO movimientos
            (fecha,cliente_id,tipo,descripcion,importe,periodo,comprobante,pdf_path,estado_conciliacion,creado_en)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (fecha.isoformat(),cid,"Ajuste",concepto or "Trabajo puntual",imp,fecha.replace(day=1).isoformat(),
             comp or None,None,None,datetime.now().isoformat()))
            st.success("Trabajo puntual registrado.")

with tabs[6]:
    st.subheader("Clientes")
    clientes=get_clientes(False)
    st.dataframe(clientes,use_container_width=True,hide_index=True)
    st.divider()
    c1,c2=st.columns(2)
    with c1:
        st.markdown("#### Nuevo cliente")
        with st.form("nuevo_cliente"):
            n=st.text_input("Nombre")
            cuit=st.text_input("CUIT")
            mod=st.selectbox("Modalidad",["Factura","Aviso de pago"])
            hon=st.number_input("Honorario vigente",min_value=0.0,step=1000.0)
            vd=st.date_input("Vigente desde",value=date.today().replace(day=1))
            domicilio=st.text_input("Domicilio fiscal / comercial")
            cond_desc=st.selectbox("Condición frente al IVA", [""] + list(IVA_RECEPTOR_OPCIONES.keys()), key="nc_cond")
            cond_id=IVA_RECEPTOR_OPCIONES.get(cond_desc)
            email_fact=st.text_input("Email de facturación")
            envio_auto=st.checkbox("Enviar factura automáticamente por email", value=True)
            if st.form_submit_button("Crear cliente"):
                execute("""INSERT INTO clientes(nombre,cuit,modalidad,honorario,vigente_desde,dia_generacion,activo,email_facturacion,envio_automatico_factura,domicilio,condicion_iva_receptor_id,condicion_iva_receptor_desc)
                VALUES(?,?,?,?,?,?,1,?,?,?,?,?)""",(n,cuit or None,mod,hon,vd.isoformat(),1,email_fact.strip() or None,1 if envio_auto else 0,
                domicilio.strip() or None, int(cond_id) if cond_id else None, cond_desc or None))
                st.success("Cliente creado.")
    with c2:
        st.markdown("#### Cambiar honorario")
        activos=get_clientes(True)
        with st.form("cambio_honorario"):
            nom=st.selectbox("Cliente",activos["nombre"].tolist(),key="ch_cli")
            nuevo=st.number_input("Nuevo honorario",min_value=0.0,step=1000.0,key="ch_imp")
            vig=st.date_input("Vigente desde",value=date.today().replace(day=1),key="ch_vig")
            nota=st.text_input("Nota",value="Actualización manual")
            if st.form_submit_button("Guardar nuevo honorario"):
                cid=int(activos.loc[activos["nombre"]==nom,"id"].iloc[0])
                execute("INSERT INTO honorarios(cliente_id,vigente_desde,honorario,nota) VALUES(?,?,?,?)",
                        (cid,vig.isoformat(),nuevo,nota))
                execute("UPDATE clientes SET honorario=?, vigente_desde=? WHERE id=?",(nuevo,vig.isoformat(),cid))
                st.success("Honorario actualizado. Los movimientos anteriores no cambian.")

    st.divider()
    st.subheader("Emisor habitual por cliente")
    st.caption("Define qué CUIT aparece seleccionado automáticamente al facturar. Se puede cambiar en cada factura.")
    clientes_asig = get_clientes(True)
    emisores_asig = get_emisores(True)
    if len(clientes_asig) and len(emisores_asig):
        ca = st.selectbox("Cliente", clientes_asig["nombre"].tolist(), key="asig_cliente")
        em_map = {emisor_label(r): int(r["id"]) for _, r in emisores_asig.iterrows()}
        ea = st.selectbox("Emisor habitual", list(em_map.keys()), key="asig_emisor")
        if st.button("Guardar emisor habitual", key="guardar_emisor_habitual"):
            cid = int(clientes_asig.loc[clientes_asig["nombre"] == ca, "id"].iloc[0])
            execute("UPDATE clientes SET emisor_predeterminado_id=? WHERE id=?", (em_map[ea], cid))
            st.success("Emisor habitual actualizado.")
            st.rerun()

    st.divider()
    st.subheader("Datos fiscales del cliente")
    clientes_fisc = get_clientes(True)
    if len(clientes_fisc):
        cf = st.selectbox("Cliente para datos fiscales", clientes_fisc["nombre"].tolist(), key="fisc_cliente_cfg")
        cfr = clientes_fisc[clientes_fisc["nombre"] == cf].iloc[0]
        f1, f2 = st.columns(2)
        fcuit = f1.text_input("CUIT del cliente", value=str(cfr.get("cuit") or ""), key="fisc_cuit")
        fdom = f2.text_input("Domicilio", value=str(cfr.get("domicilio") or ""), key="fisc_dom")
        f3, f4 = st.columns(2)
        current_cond = int(cfr.get("condicion_iva_receptor_id")) if pd.notna(cfr.get("condicion_iva_receptor_id")) else 0
        fidiva = f3.number_input("ID condición IVA receptor (ARCA)", min_value=0, value=current_cond, step=1, key="fisc_idiva")
        fdesc = f4.text_input("Descripción condición IVA", value=str(cfr.get("condicion_iva_receptor_desc") or ""), key="fisc_desciva")
        if st.button("Guardar datos fiscales del cliente", key="fisc_save"):
            execute("UPDATE clientes SET cuit=?,domicilio=?,condicion_iva_receptor_id=?,condicion_iva_receptor_desc=? WHERE id=?",
                    (fcuit.strip() or None, fdom.strip() or None, int(fidiva) if fidiva else None, fdesc.strip() or None, int(cfr["id"])))
            st.success("Datos fiscales guardados.")
            st.rerun()

    st.divider()
    st.subheader("Email de facturación")
    st.caption("El correo sale automáticamente desde sysgroupmdp@gmail.com cuando ARCA autoriza la factura y ya existe el PDF final.")
    clientes_mail = get_clientes(True)
    if len(clientes_mail):
        cm = st.selectbox("Cliente para configurar email", clientes_mail["nombre"].tolist(), key="mail_cliente_cfg")
        cr = clientes_mail[clientes_mail["nombre"] == cm].iloc[0]
        email_actual = str(cr.get("email_facturacion") or "")
        auto_actual = bool(int(cr.get("envio_automatico_factura") or 0))
        mail_dest = st.text_input("Email de facturación del cliente", value=email_actual, key="mail_dest_cfg")
        mail_auto = st.checkbox("Envío automático al autorizar", value=auto_actual, key="mail_auto_cfg")
        if st.button("Guardar configuración de email", key="mail_cfg_save"):
            execute("UPDATE clientes SET email_facturacion=?, envio_automatico_factura=? WHERE id=?",
                    (mail_dest.strip() or None, 1 if mail_auto else 0, int(cr["id"])))
            st.success("Configuración de email guardada.")
            st.rerun()

with tabs[7]:
    st.subheader("Ítems y leyendas de facturación")
    st.write("Guardá conceptos habituales para reutilizarlos al facturar.")
    with st.form("nuevo_item_facturacion"):
        n = st.text_input("Nombre corto", placeholder="Ej.: Medición Res. SRT 900/15")
        d = st.text_area("Leyenda / descripción")
        p = st.number_input("Precio sugerido", min_value=0.0, step=1000.0)
        if st.form_submit_button("Agregar ítem"):
            if not n.strip() or not d.strip():
                st.error("Completá nombre y descripción.")
            else:
                execute(
                    "INSERT INTO items_facturacion(nombre,descripcion,precio_sugerido,activo) VALUES(?,?,?,1)",
                    (n.strip(), d.strip(), p)
                )
                st.success("Ítem guardado.")
                st.rerun()
    st.dataframe(catalogo_items(), use_container_width=True, hide_index=True)

with tabs[8]:
    st.subheader("Cuenta corriente")
    mov = query_df("""
    SELECT m.id,m.fecha,c.nombre cliente,m.tipo,m.descripcion,m.importe,m.periodo,m.comprobante,m.pdf_path
    FROM movimientos m JOIN clientes c ON c.id=m.cliente_id
    ORDER BY m.fecha DESC,m.id DESC
    """)
    if len(mov):
        mov["importe_fmt"]=mov["importe"].map(money)
    st.dataframe(mov.drop(columns=["importe"],errors="ignore"),use_container_width=True,hide_index=True)

with tabs[9]:
    st.subheader("Exportar a Excel")
    st.write("Genera una copia completa de clientes, movimientos, saldos y honorarios.")
    if st.button("Preparar Excel"):
        clientes=query_df("SELECT * FROM clientes ORDER BY nombre")
        movimientos=query_df("""
          SELECT m.*,c.nombre cliente FROM movimientos m JOIN clientes c ON c.id=m.cliente_id
          ORDER BY m.fecha,m.id
        """)
        saldos=balances_df()
        honorarios=query_df("""
          SELECT h.*,c.nombre cliente FROM honorarios h JOIN clientes c ON c.id=h.cliente_id
          ORDER BY h.vigente_desde DESC
        """)
        email_log=query_df("""
          SELECT l.*,c.nombre cliente FROM email_log l JOIN clientes c ON c.id=l.cliente_id
          ORDER BY l.fecha_hora DESC
        """)
        out=io.BytesIO()
        with pd.ExcelWriter(out,engine="openpyxl") as writer:
            clientes.to_excel(writer,index=False,sheet_name="Clientes")
            movimientos.to_excel(writer,index=False,sheet_name="Movimientos")
            saldos.to_excel(writer,index=False,sheet_name="Saldos")
            honorarios.to_excel(writer,index=False,sheet_name="Honorarios")
            email_log.to_excel(writer,index=False,sheet_name="Emails")
        st.download_button("Descargar Excel",data=out.getvalue(),file_name=f"Gestion_administrativa_{date.today().isoformat()}.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    st.divider()
    st.subheader("Respaldo completo")
    if DB_PATH.exists():
        st.download_button(
            "Descargar base de datos",
            data=DB_PATH.read_bytes(),
            file_name=f"gestion_administrativa_ssgroup_{date.today().isoformat()}.db",
            mime="application/octet-stream"
        )



with tabs[10]:
    st.subheader("ARCA · Facturación electrónica real")
    st.caption("Conector WSAA + WSFEv1. La app sólo emite si la configuración fiscal y las credenciales del CUIT están completas.")

    if not app_password_configurada():
        st.error("Antes de habilitar PRODUCCIÓN configurá `app_password` en Secrets. Esta app está publicada en Internet y no es seguro emitir sin acceso privado.")
    else:
        st.success("Acceso privado de la app configurado.")

    emis = get_emisores(False)
    if len(emis):
        st.markdown("#### Emisores")
        show_cols = [c for c in ["id","nombre","cuit","regimen_iva","punto_venta","ambiente_arca","activo"] if c in emis.columns]
        st.dataframe(emis[show_cols], use_container_width=True, hide_index=True)

        em_labels = {emisor_label(r): int(r["id"]) for _, r in emis.iterrows()}
        ec = st.selectbox("Emisor a configurar", list(em_labels.keys()), key="arca_cfg_emisor")
        current = emis[emis["id"] == em_labels[ec]].iloc[0]

        st.markdown("##### Configuración del emisor")
        st.info("Este emisor está fijado como Responsable Monotributo y sólo puede emitir Factura C.")
        a1, a2 = st.columns(2)
        pv = a1.number_input("Punto de venta WSFE", min_value=1, value=int(current.get("punto_venta") or 1), step=1, key="arca_pv")
        amb_opts = ["HOMOLOGACION", "PRODUCCION"]
        amb_current = str(current.get("ambiente_arca") or "HOMOLOGACION").upper()
        ambiente = a2.selectbox("Ambiente", amb_opts, index=amb_opts.index(amb_current) if amb_current in amb_opts else 0, key="arca_amb")

        a3, a4 = st.columns(2)
        domicilio_fiscal = a3.text_input("Domicilio fiscal", value=str(current.get("domicilio_fiscal") or ""), key="arca_dom")
        iibb = a4.text_input("Ingresos Brutos (opcional)", value=str(current.get("ingresos_brutos") or ""), key="arca_iibb")
        ini_txt = str(current.get("inicio_actividades") or "")
        try:
            ini_default = datetime.strptime(ini_txt, "%Y-%m-%d").date() if ini_txt else date.today()
        except Exception:
            ini_default = date.today()
        inicio_act = st.date_input("Inicio de actividades", value=ini_default, key="arca_ini")

        if st.button("Guardar configuración del emisor", key="arca_guardar_cfg"):
            execute("""UPDATE emisores SET condicion_iva='Responsable Monotributo',punto_venta=?,domicilio_fiscal=?,
                       ingresos_brutos=?,inicio_actividades=?,regimen_iva='RESPONSABLE_MONOTRIBUTO',
                       iva_alicuota=0,ambiente_arca=?,precios_incluyen_iva=1 WHERE id=?""",
                    (int(pv), domicilio_fiscal.strip() or None, iibb.strip() or None,
                     inicio_act.isoformat(), ambiente, em_labels[ec]))
            st.success("Configuración guardada.")
            st.rerun()

        cuit_current = str(current["cuit"])
        st.markdown("##### Certificado ARCA")
        if arca_configurada_para(cuit_current):
            st.success(f"Certificado y clave privada cargados en Secrets para {cuit_current}.")
        else:
            st.warning(f"Todavía no hay certificado/clave privada cargados para {cuit_current}.")
            if st.button("Generar clave privada + CSR para este CUIT", key="arca_gen_csr"):
                try:
                    key_bytes, csr_bytes = generar_clave_y_csr(cuit_current, str(current["nombre"]), "gestion-sysgroup")
                    st.session_state["generated_arca_key"] = key_bytes
                    st.session_state["generated_arca_csr"] = csr_bytes
                except Exception as e:
                    st.error(str(e))
            if st.session_state.get("generated_arca_key") and st.session_state.get("generated_arca_csr"):
                st.warning("Guardá la clave privada en un lugar seguro. No la subas a GitHub ni la compartas.")
                d1, d2 = st.columns(2)
                d1.download_button("Descargar clave privada", st.session_state["generated_arca_key"], file_name=f"ARCA_{digits(cuit_current)}.key", mime="application/octet-stream")
                d2.download_button("Descargar CSR", st.session_state["generated_arca_csr"], file_name=f"ARCA_{digits(cuit_current)}.csr", mime="application/pkcs10")

        if st.button("Probar conexión con ARCA (sin emitir)", key="arca_test_conn"):
            try:
                ta, ptos = probar_conexion_arca(em_labels[ec])
                st.success(f"Conexión correcta. Ticket WSAA obtenido; vence {ta.expiration_time}.")
                if ptos:
                    st.dataframe(pd.DataFrame(ptos), use_container_width=True, hide_index=True)
                else:
                    st.info("ARCA no devolvió puntos de venta para este certificado/CUIT.")
            except Exception as e:
                st.error(str(e))


    st.caption(f"Plantilla PDF activa: **{PDF_TEMPLATE_VERSION}** · ORIGINAL + DUPLICADO + TRIPLICADO")

    st.divider()
    st.markdown("#### Facturas preparadas / emitidas")
    comps = query_df("""
        SELECT a.id,a.fecha_emision,c.nombre cliente,
               COALESCE(e.nombre,'Sin asignar') emisor,COALESCE(e.cuit,'') cuit_emisor,
               a.periodo_texto,a.tipo_comprobante,a.punto_venta,a.numero_comprobante,a.total,a.estado_arca,a.cae,
               a.pdf_path,a.email_enviado,a.email_enviado_a,a.email_enviado_en,a.arca_error,a.arca_observaciones
        FROM comprobantes_arca a
        JOIN clientes c ON c.id=a.cliente_id
        LEFT JOIN emisores e ON e.id=a.emisor_id
        ORDER BY a.id DESC
    """)
    if len(comps):
        filtro = st.selectbox("Filtrar por emisor", ["Todos"] + sorted(comps["emisor"].dropna().unique().tolist()), key="arca_filter")
        vista = comps.copy()
        if filtro != "Todos":
            vista = vista[vista["emisor"] == filtro]
        vista_display = vista.copy()
        vista_display["total"] = vista_display["total"].map(money)
        st.dataframe(vista_display, use_container_width=True, hide_index=True)

        drafts = comps[comps["estado_arca"].fillna("BORRADOR").isin(["BORRADOR","RECHAZADA"])]
        if len(drafts):
            did = st.selectbox("Borrador a emitir", drafts["id"].tolist(), key="arca_draft_sel")
            dr = drafts[drafts["id"] == did].iloc[0]
            st.caption(f"{dr['cliente']} · {dr['emisor']} · {dr['tipo_comprobante']} · {money(dr['total'])}")
            if st.button("EMITIR EN ARCA", type="primary", key="arca_emit_existing"):
                ok, msg = emitir_comprobante_arca(int(did))
                (st.success if ok else st.error)(msg)
                if ok:
                    st.rerun()
    else:
        st.info("Todavía no hay facturas preparadas.")

    st.divider()
    st.markdown("#### Correo automático")
    st.write(f"**Remitente:** {MAIL_FROM} · **Asunto:** {MAIL_SUBJECT}")
    st.text_area("Cuerpo predeterminado", value=MAIL_BODY, height=220, disabled=True, key="mail_body_preview")
    if gmail_configurada():
        st.success("Credencial privada de Gmail configurada.")
    else:
        st.warning("Falta configurar la credencial privada de Gmail. La factura puede emitirse en ARCA, pero no se enviará automáticamente hasta hacerlo.")

    autorizadas = query_df("""
        SELECT a.id,c.nombre cliente,c.email_facturacion,a.pdf_path,a.email_enviado,a.email_enviado_a,a.email_enviado_en
        FROM comprobantes_arca a JOIN clientes c ON c.id=a.cliente_id
        WHERE a.estado_arca='AUTORIZADA'
        ORDER BY a.id DESC
    """)
    if len(autorizadas):
        sel_id = st.selectbox("Factura autorizada", autorizadas["id"].tolist(), key="mail_factura_sel")
        rr = autorizadas[autorizadas["id"] == sel_id].iloc[0]
        st.caption(f"Cliente: {rr['cliente']} · Email: {rr.get('email_facturacion') or 'sin configurar'}")
        cdl, cregen, csend = st.columns(3)
        p = str(rr.get("pdf_path") or "")
        if p and Path(p).exists():
            cdl.download_button("Descargar PDF", data=Path(p).read_bytes(), file_name=Path(p).name, mime="application/pdf", key=f"pdf_{sel_id}")
        else:
            cdl.info("PDF todavía no disponible")
        if cregen.button("Regenerar PDF formato ARCA", key=f"regen_{sel_id}"):
            ok, msg = regenerar_pdf_autorizado(int(sel_id))
            (st.success if ok else st.error)(msg)
            if ok:
                st.rerun()
        if csend.button("Enviar / reenviar factura", key=f"send_{sel_id}"):
            ok, msg = enviar_factura_por_email(int(sel_id))
            (st.success if ok else st.error)(msg)

    st.divider()
    st.info("Secuencia fiscal implementada: WSAA → último comprobante → FECAESolicitar → CAE → PDF con QR → cuenta corriente → email. Para PRODUCCIÓN se requiere completar la habilitación/certificados de cada CUIT en ARCA.")

