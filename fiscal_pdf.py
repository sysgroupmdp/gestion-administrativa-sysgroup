from __future__ import annotations

import base64
import io
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable

import qrcode
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

CBTE_CODES = {"A": 1, "B": 6, "C": 11, "M": 51}
COPIAS = ("ORIGINAL", "DUPLICADO", "TRIPLICADO")


def _date_ar(value: Any) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%d/%m/%Y")
        except ValueError:
            pass
    return s


def _num_ar(value: Any, decimals: int = 2) -> str:
    try:
        s = f"{float(value):,.{decimals}f}"
    except Exception:
        s = f"{0:,.{decimals}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def _digits(value: Any) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _format_cuit(value: Any) -> str:
    d = _digits(value)
    if len(d) == 11:
        return f"{d[:2]}-{d[2:10]}-{d[10:]}"
    return str(value or "")


def _safe_text(value: Any) -> str:
    if value is None:
        return ""
    # Helvetica/WinAnsi: keep the common Spanish glyphs and replace unsupported chars.
    return str(value).replace("\u2013", "-").replace("\u2014", "-")


def qr_url(*, fecha: str, cuit: str, punto_venta: int, tipo: str, numero: int,
           importe: float, doc_tipo: int, doc_nro: str, cae: str) -> str:
    payload = {
        "ver": 1,
        "fecha": fecha,
        "cuit": int(_digits(cuit)),
        "ptoVta": int(punto_venta),
        "tipoCmp": int(CBTE_CODES[tipo]),
        "nroCmp": int(numero),
        "importe": round(float(importe), 2),
        "moneda": "PES",
        "ctz": 1,
        "tipoDocRec": int(doc_tipo),
        "nroDocRec": int(_digits(doc_nro) or 0),
        "tipoCodAut": "E",
        "codAut": int(cae),
    }
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "https://www.afip.gob.ar/fe/qr/?p=" + base64.b64encode(raw).decode("ascii")


def _qr_reader(url: str) -> ImageReader:
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M,
                       box_size=6, border=1)
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return ImageReader(buf)


def _logo_reader() -> ImageReader | None:
    """Busca el logo junto a fiscal_pdf.py. Si no existe, la factura igual se genera."""
    here = Path(__file__).resolve().parent
    for name in ("logo_ssgroup.jpg", "ISOLOGO BN(1).jpg", "logo_ssgroup.png"):
        p = here / name
        if p.exists():
            try:
                return ImageReader(str(p))
            except Exception:
                pass
    return None


def _fit_text(c: canvas.Canvas, text: str, x: float, y: float, max_width: float,
              font="Helvetica", size=8.0, min_size=5.8) -> float:
    text = _safe_text(text)
    s = size
    while s > min_size and c.stringWidth(text, font, s) > max_width:
        s -= 0.2
    c.setFont(font, s)
    c.drawString(x, y, text)
    return s


def _wrap_lines(c: canvas.Canvas, text: str, max_width: float, font="Helvetica",
                size=7.3, max_lines=7):
    words = _safe_text(text).replace("\n", " \n ").split()
    lines, cur = [], ""
    for w in words:
        if w == "\\n":
            if cur:
                lines.append(cur)
                cur = ""
            continue
        trial = (cur + " " + w).strip()
        if c.stringWidth(trial, font, size) <= max_width:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
        if len(lines) >= max_lines:
            break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    return lines[:max_lines]


def _draw_label_value(c, label, value, x, y, label_w=0, size=7.2, max_width=None):
    c.setFont("Helvetica-Bold", size)
    c.drawString(x, y, label)
    dx = label_w or c.stringWidth(label, "Helvetica-Bold", size) + 4
    if max_width:
        _fit_text(c, value, x + dx, y, max_width - dx, "Helvetica", size, 5.8)
    else:
        c.setFont("Helvetica", size)
        c.drawString(x + dx, y, _safe_text(value))


def _draw_page(c: canvas.Canvas, *, copia: str, emisor: Dict[str, Any], cliente: Dict[str, Any],
               comprobante: Dict[str, Any], items: list[Dict[str, Any]]):
    W, H = A4
    L, R = 12, W - 12
    top = H - 14
    tipo = str(comprobante.get("tipo_comprobante") or "C").upper()
    pv = int(comprobante.get("punto_venta") or emisor.get("punto_venta") or 0)
    nro = int(comprobante.get("numero_comprobante") or 0)
    total = float(comprobante.get("total") or 0)
    cae = str(comprobante.get("cae") or "")
    fecha = str(comprobante.get("fecha_emision") or "")
    cae_vto = str(comprobante.get("vencimiento_cae") or "")
    doc_tipo = int(comprobante.get("doc_tipo") or 80)
    doc_nro = str(cliente.get("cuit") or comprobante.get("doc_nro") or "0")

    # ----- Marco y título de copia -----
    c.setLineWidth(0.75)
    c.rect(L, top - 30, R - L, 30)
    c.setFont("Helvetica-Bold", 13)
    c.drawCentredString(W / 2, top - 20, copia)

    # ----- Encabezado principal, réplica visual del comprobante ARCA -----
    y_top = top - 30
    y_bottom = y_top - 126
    split = L + 292
    c.rect(L, y_bottom, R - L, y_top - y_bottom)
    c.line(split, y_bottom, split, y_top)

    # Logo S&S arriba a la izquierda, única personalización pedida.
    logo = _logo_reader()
    if logo:
        try:
            c.drawImage(logo, L + 8, y_top - 34, width=76, height=25,
                        preserveAspectRatio=True, anchor="sw", mask="auto")
        except Exception:
            pass

    # Nombre del emisor, arriba del panel izquierdo.
    c.setFont("Helvetica-Bold", 8.2)
    c.drawCentredString(L + 184, y_top - 23, _safe_text(emisor.get("nombre", "")))

    # Caja C/COD.011 y FACTURA.
    bx = split - 27
    c.rect(bx, y_top - 47, 42, 47)
    c.setFont("Helvetica-Bold", 24)
    c.drawCentredString(bx + 21, y_top - 25, tipo)
    c.setFont("Helvetica-Bold", 6.5)
    c.drawCentredString(bx + 21, y_top - 39, f"COD. {int(CBTE_CODES.get(tipo, 11)):03d}")
    c.setFont("Helvetica-Bold", 18)
    c.drawString(split + 43, y_top - 28, "FACTURA")

    # Datos emisor lado izquierdo.
    _draw_label_value(c, "Razón Social:", emisor.get("nombre", ""), L + 7, y_top - 65,
                      size=7.3, max_width=265)
    dom = _safe_text(emisor.get("domicilio_fiscal") or "")
    c.setFont("Helvetica-Bold", 7.3); c.drawString(L + 7, y_top - 91, "Domicilio Comercial:")
    dom_lines = _wrap_lines(c, dom, 190, size=7.1, max_lines=2)
    c.setFont("Helvetica", 7.1)
    for i, line in enumerate(dom_lines):
        c.drawString(L + 105, y_top - 91 - i * 9, line)
    _draw_label_value(c, "Condición frente al IVA:", emisor.get("condicion_iva") or "Responsable Monotributo",
                      L + 7, y_bottom + 9, size=7.3, max_width=274)

    # Datos fiscales lado derecho.
    _draw_label_value(c, "Punto de Venta:", f"{pv:05d}", split + 43, y_top - 50, size=7.3)
    _draw_label_value(c, "Comp. Nro:", f"{nro:08d}", split + 158, y_top - 50, size=7.3)
    _draw_label_value(c, "Fecha de Emisión:", _date_ar(fecha), split + 43, y_top - 68, size=7.3)
    _draw_label_value(c, "CUIT:", _digits(emisor.get("cuit")), split + 43, y_top - 88, size=7.3)
    _draw_label_value(c, "Ingresos Brutos:", emisor.get("ingresos_brutos") or _format_cuit(emisor.get("cuit")),
                      split + 43, y_top - 104, size=7.3)
    _draw_label_value(c, "Fecha de Inicio de Actividades:", _date_ar(emisor.get("inicio_actividades")),
                      split + 43, y_top - 120, size=7.0)

    # ----- Período -----
    per_top = y_bottom
    per_bottom = per_top - 22
    c.rect(L, per_bottom, R - L, 22)
    _draw_label_value(c, "Período Facturado Desde:", _date_ar(comprobante.get("periodo_desde")), L + 7, per_bottom + 7, size=7.3)
    _draw_label_value(c, "Hasta:", _date_ar(comprobante.get("periodo_hasta")), L + 236, per_bottom + 7, size=7.3)
    _draw_label_value(c, "Fecha de Vto. para el pago:", _date_ar(comprobante.get("fecha_vto_pago")), L + 360, per_bottom + 7, size=7.3)

    # ----- Receptor -----
    cli_top = per_bottom
    cli_bottom = cli_top - 73
    c.rect(L, cli_bottom, R - L, 73)
    _draw_label_value(c, "CUIT:", _format_cuit(cliente.get("cuit") or doc_nro), L + 7, cli_top - 16, size=7.2)
    _draw_label_value(c, "Apellido y Nombre / Razón Social:", cliente.get("nombre", ""), L + 130, cli_top - 16,
                      size=7.2, max_width=410)
    _draw_label_value(c, "Condición frente al IVA:", cliente.get("condicion_iva_receptor_desc") or "",
                      L + 7, cli_top - 37, size=7.2, max_width=270)
    _draw_label_value(c, "Domicilio:", cliente.get("domicilio") or "", L + 300, cli_top - 37,
                      size=7.2, max_width=250)
    _draw_label_value(c, "Condición de venta:", comprobante.get("condicion_venta") or "Transferencia Bancaria",
                      L + 7, cli_top - 59, size=7.2, max_width=300)

    # ----- Cabecera detalle -----
    det_top = cli_bottom
    header_h = 20
    widths = [39, 143, 60, 43, 77, 49, 70, 86]
    labels = ["Código", "Producto / Servicio", "Cantidad", "U. Medida", "Precio Unit.", "% Bonif", "Imp. Bonif.", "Subtotal"]
    x = L
    c.setFillColor(colors.HexColor("#D9D9D9"))
    c.rect(L, det_top - header_h, R - L, header_h, fill=1, stroke=1)
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 6.6)
    for w, lab in zip(widths, labels):
        c.line(x, det_top - header_h, x, det_top)
        c.drawCentredString(x + w / 2, det_top - 13, lab)
        x += w
    c.line(R, det_top - header_h, R, det_top)

    # ----- Ítems: aspecto abierto como ARCA -----
    item_y = det_top - header_h - 13
    row_gap = 11
    max_desc_width = widths[1] - 6
    for it in items:
        if item_y < 240:
            break
        desc_lines = _wrap_lines(c, str(it.get("descripcion") or ""), max_desc_width,
                                 font="Helvetica", size=7.0, max_lines=8)
        line_count = max(1, len(desc_lines))
        row_h = max(20, line_count * row_gap)
        # código vacío, como el ejemplo de ARCA cuando no se informa código.
        c.setFont("Helvetica", 7.0)
        for j, line in enumerate(desc_lines):
            c.drawString(L + widths[0] + 3, item_y - j * row_gap, line)
        xq = L + widths[0] + widths[1]
        c.drawRightString(xq + widths[2] - 5, item_y, _num_ar(it.get("cantidad") or 0))
        xq += widths[2]
        c.drawCentredString(xq + widths[3] / 2, item_y, "unidades")
        xq += widths[3]
        c.drawRightString(xq + widths[4] - 5, item_y, _num_ar(it.get("precio_unitario") or 0))
        xq += widths[4]
        c.drawRightString(xq + widths[5] - 5, item_y, "0,00")
        xq += widths[5]
        c.drawRightString(xq + widths[6] - 5, item_y, "0,00")
        xq += widths[6]
        c.drawRightString(xq + widths[7] - 5, item_y, _num_ar(it.get("subtotal") or 0))
        item_y -= row_h + 4

    # ----- Totales -----
    tot_bottom, tot_top = 130, 220
    c.rect(L, tot_bottom, R - L, tot_top - tot_bottom)
    tx = R - 135
    c.setFont("Helvetica-Bold", 7.5)
    c.drawRightString(tx, tot_bottom + 50, "Subtotal: $")
    c.drawRightString(tx, tot_bottom + 31, "Importe Otros Tributos: $")
    c.drawRightString(tx, tot_bottom + 12, "Importe Total: $")
    c.drawRightString(R - 10, tot_bottom + 50, _num_ar(total))
    c.drawRightString(R - 10, tot_bottom + 31, "0,00")
    c.drawRightString(R - 10, tot_bottom + 12, _num_ar(total))

    # ----- Leyenda consumidor -----
    cons_bottom, cons_top = 95, 120
    c.rect(L, cons_bottom, R - L, cons_top - cons_bottom)
    c.setFont("Helvetica-Oblique", 7.5)
    c.drawCentredString(W / 2, cons_bottom + 9, '"Orientación al consumidor provincia de Buenos Aires 0800-222-9042"')

    # ----- QR + ARCA + CAE -----
    url = qr_url(fecha=fecha, cuit=str(emisor.get("cuit")), punto_venta=pv, tipo=tipo,
                 numero=nro, importe=total, doc_tipo=doc_tipo, doc_nro=doc_nro, cae=cae)
    c.drawImage(_qr_reader(url), L + 8, 20, width=66, height=66, preserveAspectRatio=True, mask="auto")
    c.setFont("Helvetica-Bold", 19)
    c.drawString(L + 93, 69, "ARCA")
    c.setFont("Helvetica-Bold", 7.5)
    c.drawString(L + 93, 41, "Comprobante Autorizado")
    c.setFont("Helvetica", 5.2)
    c.drawString(L + 93, 27, "Esta Agencia no se responsabiliza por los datos ingresados en el detalle de la operación")
    c.setFont("Helvetica-Bold", 7.5)
    c.drawCentredString(W / 2, 70, "Pág. 1/1")
    c.drawRightString(R - 6, 70, f"CAE N°:  {cae}")
    c.drawRightString(R - 6, 52, f"Fecha de Vto. de CAE:  {_date_ar(cae_vto)}")


def generar_pdf_factura(path: str | Path, *, emisor: Dict[str, Any], cliente: Dict[str, Any],
                        comprobante: Dict[str, Any], items: Iterable[Dict[str, Any]]) -> Path:
    """Genera una Factura C estilo ARCA en ORIGINAL/DUPLICADO/TRIPLICADO.

    Mantiene la misma firma que el generador anterior, por lo que app.py no necesita cambios.
    Los datos fiscales/CAE/QR son dinámicos. El logo S&S se toma de logo_ssgroup.jpg
    ubicado junto a este archivo.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    item_list = list(items)
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setTitle(f"Factura C {int(comprobante.get('punto_venta') or emisor.get('punto_venta') or 0):05d}-{int(comprobante.get('numero_comprobante') or 0):08d}")
    for i, copia in enumerate(COPIAS):
        _draw_page(c, copia=copia, emisor=emisor, cliente=cliente, comprobante=comprobante, items=item_list)
        if i < len(COPIAS) - 1:
            c.showPage()
    c.save()
    return path
