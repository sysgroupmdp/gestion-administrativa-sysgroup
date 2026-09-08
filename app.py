
import streamlit as st
import sqlite3
from pathlib import Path
from datetime import date, datetime
import pandas as pd
import re
import io
import smtplib
import ssl
from email.message import EmailMessage
from pypdf import PdfReader

APP_DIR = Path(__file__).parent
DB_PATH = APP_DIR / "control_cuentas.db"
PDF_DIR = APP_DIR / "facturas"
PDF_DIR.mkdir(exist_ok=True)

st.set_page_config(page_title="S&S Group · Gestión Administrativa", page_icon="📊", layout="wide")

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
        tipo_comprobante TEXT DEFAULT 'A',
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

def ensure_demo_data():
    if len(get_clientes(False)) == 0:
        demo = [
            ("JUAREZ CESAR", None, "Aviso de pago", 760000),
            ("EULOGIO CONDORI", None, "Aviso de pago", 320000),
            ("ROJAS MARTIN", None, "Aviso de pago", 240000),
            ("GAUTHIER WALTER", None, "Aviso de pago", 400000),
            ("PROSEGUR S.A.", None, "Factura", 0),
        ]
        for n,c,m,h in demo:
            execute("""INSERT INTO clientes(nombre,cuit,modalidad,honorario,vigente_desde,dia_generacion,activo)
                       VALUES(?,?,?,?,?,?,1)""",(n,c,m,h,date.today().replace(day=1).isoformat(),1))


def ensure_emitters():
    emisores = [
        ("Juan Ignacio Sirvent", "20-37769536-5"),
        ("Martín Nicolás Sirvent", "20-35140724-8"),
        ("Melissa Jennifer Bulacio Juarez", "27-41149423-9"),
    ]
    conn = get_conn()
    for nombre, cuit in emisores:
        conn.execute(
            "INSERT INTO emisores(nombre,cuit,activo) VALUES(?,?,1) "
            "ON CONFLICT(cuit) DO UPDATE SET nombre=excluded.nombre",
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
    try:
        return bool(st.secrets.get("gmail_app_password"))
    except Exception:
        return False

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
        password = st.secrets["gmail_app_password"]
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

def registrar_factura_autorizada(comprobante_id, pdf_path):
    """Se llamará desde el conector ARCA cuando exista CAE y PDF final."""
    row = query_df("SELECT * FROM comprobantes_arca WHERE id=?", (comprobante_id,))
    if len(row) != 1:
        return False, "Comprobante inexistente."
    r = row.iloc[0]
    execute("UPDATE comprobantes_arca SET estado_arca='AUTORIZADA', pdf_path=? WHERE id=?", (str(pdf_path), comprobante_id))

    comp_ref = f"ARCA-{comprobante_id}"
    try:
        execute("""INSERT INTO movimientos
        (fecha,cliente_id,tipo,descripcion,importe,periodo,comprobante,pdf_path,estado_conciliacion,creado_en)
        VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (r["fecha_emision"], int(r["cliente_id"]), "Factura/Cargo", "Factura autorizada ARCA", float(r["total"]),
         r["periodo_desde"] or r["fecha_emision"][:7] + "-01", comp_ref, str(pdf_path), "Pendiente", datetime.now().isoformat()))
    except sqlite3.IntegrityError:
        pass

    cliente = query_df("SELECT email_facturacion,envio_automatico_factura FROM clientes WHERE id=?", (int(r["cliente_id"]),))
    if len(cliente) and int(cliente.iloc[0].get("envio_automatico_factura") or 0) == 1:
        return enviar_factura_por_email(comprobante_id)
    return True, "Factura autorizada y registrada. Envío automático desactivado para este cliente."

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

init_db()
ensure_demo_data()
ensure_emitters()
ensure_invoice_items()

st.title("S&S Group · Gestión Administrativa")
st.caption("Clientes · Facturación · Cuenta corriente · Pagos · Avisos · Trabajos puntuales · ARCA")

# Generación automática silenciosa al abrir.
# Si SQLite está momentáneamente ocupada, no hacemos caer toda la app.
try:
    generate_monthly_notices()
except sqlite3.OperationalError as e:
    if "locked" not in str(e).lower():
        raise

tabs = st.tabs(["Panel","Emitir factura","Facturas recibidas/PDF","Avisos de pago","Pagos","Trabajos extras","Clientes","Ítems / Leyendas","Cuenta corriente","Exportar","ARCA"])

with tabs[0]:
    saldos = balances_df()
    c1,c2,c3,c4 = st.columns(4)
    c1.metric("Total por cobrar", money(saldos["saldo"].clip(lower=0).sum()))
    c2.metric("Clientes activos", len(saldos))
    c3.metric("Clientes con deuda", int((saldos["saldo"]>0).sum()))
    movs = query_df("SELECT COUNT(*) n FROM movimientos").iloc[0]["n"]
    c4.metric("Movimientos", int(movs))
    st.subheader("Estado por cliente")
    show = saldos.copy()
    show["honorario"] = show["honorario"].map(money)
    show["saldo"] = show["saldo"].map(money)
    st.dataframe(show[["nombre","modalidad","honorario","saldo"]], use_container_width=True, hide_index=True)


with tabs[1]:
    st.subheader("Emitir factura")
    st.write("Generá primero un borrador. Cada comprobante queda asociado a uno de los tres emisores.")

    clientes = get_clientes(True)
    emisores = get_emisores(True)

    if len(clientes) == 0:
        st.warning("Primero cargá un cliente.")
    elif len(emisores) == 0:
        st.warning("No hay emisores configurados.")
    else:
        nom = st.selectbox("Cliente", clientes["nombre"].tolist(), key="ef_cliente")
        cliente_row = clientes[clientes["nombre"] == nom].iloc[0]

        emisor_options = {emisor_label(r): int(r["id"]) for _, r in emisores.iterrows()}
        default_idx = 0
        if "emisor_predeterminado_id" in cliente_row.index and pd.notna(cliente_row["emisor_predeterminado_id"]):
            ids = list(emisor_options.values())
            if int(cliente_row["emisor_predeterminado_id"]) in ids:
                default_idx = ids.index(int(cliente_row["emisor_predeterminado_id"]))

        c1, c2, c3 = st.columns(3)
        emisor_sel = c1.selectbox("Emisor", list(emisor_options.keys()), index=default_idx, key="ef_emisor")
        fecha_emision = c2.date_input("Fecha de emisión", value=date.today(), key="ef_fecha")
        tipo_periodo = c3.selectbox("Período facturado", ["Mes vigente","Mes vencido","Manual"], key="ef_periodo")

        manual_desde = manual_hasta = None
        if tipo_periodo == "Manual":
            p1, p2 = st.columns(2)
            manual_desde = p1.date_input("Desde", value=date.today().replace(day=1), key="ef_desde")
            manual_hasta = p2.date_input("Hasta", value=date.today(), key="ef_hasta")

        p_desde, p_hasta, p_texto = factura_periodo(fecha_emision, tipo_periodo, manual_desde, manual_hasta)
        st.caption(f"Período seleccionado: **{p_texto}**")

        tipo_comp = st.selectbox("Tipo de comprobante", ["A","B","C","M"], key="ef_tipo_comp")
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
                items.append({
                    "item_catalogo_id": int(r["id"]),
                    "descripcion": desc,
                    "cantidad": cantidad,
                    "precio_unitario": precio,
                    "subtotal": cantidad * precio
                })

        st.markdown("#### Ítem libre opcional")
        libre = st.text_area("Descripción libre", key="ef_libre_desc")
        l1, l2 = st.columns(2)
        lc = l1.number_input("Cantidad libre", min_value=0.0, value=0.0, step=1.0, key="ef_libre_cant")
        lp = l2.number_input("Precio unitario libre", min_value=0.0, value=0.0, step=1000.0, key="ef_libre_precio")
        if libre.strip() and lc > 0:
            items.append({
                "item_catalogo_id": None,
                "descripcion": libre.strip(),
                "cantidad": lc,
                "precio_unitario": lp,
                "subtotal": lc * lp
            })

        total = sum(float(x["subtotal"]) for x in items)
        st.metric("Total del borrador", money(total))
        obs = st.text_area("Observaciones", key="ef_obs")

        if st.button("Guardar borrador de factura", type="primary", key="ef_guardar"):
            if not items:
                st.error("Agregá al menos un ítem.")
            elif total <= 0:
                st.error("El total debe ser mayor a cero.")
            else:
                cid = int(cliente_row["id"])
                emisor_id = emisor_options[emisor_sel]
                fid, total_guardado = guardar_borrador_arca(
                    cid, emisor_id, fecha_emision, p_desde, p_hasta, p_texto,
                    tipo_periodo, tipo_comp, items, obs
                )
                st.success(f"Borrador #{fid} guardado por {money(total_guardado)} a nombre de {emisor_sel}.")

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
            email_fact=st.text_input("Email de facturación")
            envio_auto=st.checkbox("Enviar factura automáticamente por email", value=True)
            if st.form_submit_button("Crear cliente"):
                execute("""INSERT INTO clientes(nombre,cuit,modalidad,honorario,vigente_desde,dia_generacion,activo,email_facturacion,envio_automatico_factura)
                VALUES(?,?,?,?,?,?,1,?,?)""",(n,cuit or None,mod,hon,vd.isoformat(),1,email_fact.strip() or None,1 if envio_auto else 0))
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
    st.subheader("ARCA")
    st.warning("MODO ACTUAL: BORRADOR / SIN EMISIÓN REAL")
    st.write("El sistema ya está preparado para trabajar con tres CUIT emisores distintos.")

    emis = get_emisores(False)
    if len(emis):
        st.markdown("#### Emisores configurados")
        st.dataframe(
            emis[["id","nombre","cuit","condicion_iva","punto_venta","activo"]],
            use_container_width=True,
            hide_index=True
        )

        st.markdown("#### Configuración fiscal básica")
        em_labels = {emisor_label(r): int(r["id"]) for _, r in emis.iterrows()}
        ec = st.selectbox("Emisor a configurar", list(em_labels.keys()), key="arca_cfg_emisor")
        current = emis[emis["id"] == em_labels[ec]].iloc[0]
        cc1, cc2 = st.columns(2)
        cond = cc1.text_input("Condición frente al IVA", value=str(current["condicion_iva"] or ""), key="arca_cond")
        pv = cc2.number_input("Punto de venta", min_value=1, value=int(current["punto_venta"] or 1), step=1, key="arca_pv")
        if st.button("Guardar configuración fiscal", key="arca_guardar_cfg"):
            execute(
                "UPDATE emisores SET condicion_iva=?, punto_venta=? WHERE id=?",
                (cond.strip(), int(pv), em_labels[ec])
            )
            st.success("Configuración fiscal guardada.")
            st.rerun()

    st.markdown("#### Borradores / comprobantes")
    comps = query_df("""
        SELECT a.id,a.fecha_emision,c.nombre cliente,
               COALESCE(e.nombre,'Sin asignar') emisor,
               COALESCE(e.cuit,'') cuit_emisor,
               a.periodo_texto,a.tipo_comprobante,a.total,a.estado_arca,a.cae,
               a.pdf_path,a.email_enviado,a.email_enviado_a,a.email_enviado_en,a.email_error
        FROM comprobantes_arca a
        JOIN clientes c ON c.id=a.cliente_id
        LEFT JOIN emisores e ON e.id=a.emisor_id
        ORDER BY a.id DESC
    """)
    if len(comps):
        filtro = st.selectbox("Filtrar por emisor", ["Todos"] + sorted(comps["emisor"].dropna().unique().tolist()))
        vista = comps.copy()
        if filtro != "Todos":
            vista = vista[vista["emisor"] == filtro]
        vista["total"] = vista["total"].map(money)
        st.dataframe(vista, use_container_width=True, hide_index=True)
    else:
        st.info("Todavía no hay borradores de facturación.")

    st.markdown("#### Correo automático")
    st.write(f"**Remitente:** {MAIL_FROM}  ·  **Asunto:** {MAIL_SUBJECT}")
    st.text_area("Cuerpo predeterminado", value=MAIL_BODY, height=220, disabled=True, key="mail_body_preview")
    if gmail_configurada():
        st.success("Credencial privada de Gmail configurada.")
    else:
        st.info("La app está lista para enviar, pero falta cargar la credencial privada de Gmail al publicarla.")

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
        cdl, csend = st.columns(2)
        p = str(rr.get("pdf_path") or "")
        if p and Path(p).exists():
            cdl.download_button("Descargar PDF", data=Path(p).read_bytes(), file_name=Path(p).name, mime="application/pdf", key=f"pdf_{sel_id}")
        else:
            cdl.info("PDF todavía no disponible")
        if csend.button("Enviar / reenviar factura", key=f"send_{sel_id}"):
            ok, msg = enviar_factura_por_email(int(sel_id))
            (st.success if ok else st.error)(msg)

    st.divider()
    st.markdown("""
    **Próxima etapa de integración ARCA**
    - Certificado digital y clave privada separados por CUIT.
    - Punto de venta configurable por emisor.
    - Autenticación WSAA independiente.
    - Consulta del último comprobante por CUIT / punto de venta / tipo.
    - Solicitud de CAE.
    - Registro automático en la cuenta corriente del cliente.
    - PDF final del comprobante.
    """)

