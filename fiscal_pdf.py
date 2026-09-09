from __future__ import annotations

import base64
import io
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List

import qrcode
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle, Image, KeepTogether


CBTE_CODES = {"A": 1, "B": 6, "C": 11, "M": 51}


def _money(v: float) -> str:
    s = f"{float(v):,.2f}"
    return "$ " + s.replace(",", "X").replace(".", ",").replace("X", ".")


def _date_ar(iso: str) -> str:
    if not iso:
        return ""
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(iso, fmt).strftime("%d/%m/%Y")
        except ValueError:
            pass
    return iso


def qr_url(*, fecha: str, cuit: str, punto_venta: int, tipo: str, numero: int, importe: float,
           doc_tipo: int, doc_nro: str, cae: str) -> str:
    payload = {
        "ver": 1,
        "fecha": fecha,
        "cuit": int("".join(ch for ch in cuit if ch.isdigit())),
        "ptoVta": int(punto_venta),
        "tipoCmp": int(CBTE_CODES[tipo]),
        "nroCmp": int(numero),
        "importe": round(float(importe), 2),
        "moneda": "PES",
        "ctz": 1,
        "tipoDocRec": int(doc_tipo),
        "nroDocRec": int("".join(ch for ch in str(doc_nro) if ch.isdigit()) or 0),
        "tipoCodAut": "E",
        "codAut": int(cae),
    }
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    p = base64.b64encode(raw).decode("ascii")
    return f"https://www.afip.gob.ar/fe/qr/?p={p}"


def _qr_image(url: str) -> io.BytesIO:
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


def generar_pdf_factura(
    path: str | Path,
    *,
    emisor: Dict[str, Any],
    cliente: Dict[str, Any],
    comprobante: Dict[str, Any],
    items: Iterable[Dict[str, Any]],
) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tipo = str(comprobante["tipo_comprobante"]).upper()
    numero = int(comprobante["numero_comprobante"])
    pv = int(comprobante["punto_venta"])
    cae = str(comprobante["cae"])
    total = float(comprobante["total"])
    imp_neto = float(comprobante.get("imp_neto") or total)
    imp_iva = float(comprobante.get("imp_iva") or 0)
    fecha = str(comprobante["fecha_emision"])
    doc_tipo = int(comprobante.get("doc_tipo") or 80)
    doc_nro = str(cliente.get("cuit") or comprobante.get("doc_nro") or "0")

    styles = getSampleStyleSheet()
    normal = ParagraphStyle("normal2", parent=styles["Normal"], fontName="Helvetica", fontSize=9, leading=12)
    small = ParagraphStyle("small", parent=normal, fontSize=7.5, leading=10)
    bold = ParagraphStyle("bold", parent=normal, fontName="Helvetica-Bold")
    center = ParagraphStyle("center", parent=bold, alignment=TA_CENTER, fontSize=11)
    right = ParagraphStyle("right", parent=normal, alignment=TA_RIGHT)

    doc = SimpleDocTemplate(str(path), pagesize=A4, leftMargin=14*mm, rightMargin=14*mm, topMargin=12*mm, bottomMargin=12*mm)
    story: List[Any] = []

    issuer_left = [
        Paragraph(f"<b>{emisor.get('nombre','')}</b>", bold),
        Paragraph(str(emisor.get("domicilio_fiscal") or "Domicilio fiscal sin configurar"), normal),
        Paragraph(f"Condición frente al IVA: {emisor.get('condicion_iva') or emisor.get('regimen_iva') or ''}", normal),
    ]
    issuer_right = [
        Paragraph(f"CUIT: {emisor.get('cuit','')}", normal),
        Paragraph(f"Ingresos Brutos: {emisor.get('ingresos_brutos') or ''}", normal),
        Paragraph(f"Inicio de actividades: {_date_ar(str(emisor.get('inicio_actividades') or ''))}", normal),
    ]
    letter_box = Table([[Paragraph(tipo, ParagraphStyle("letter", parent=center, fontSize=24, leading=28)),
                         Paragraph(f"COD. {CBTE_CODES.get(tipo,'')}", center)]], colWidths=[18*mm, 24*mm])
    letter_box.setStyle(TableStyle([("BOX",(0,0),(-1,-1),1,colors.black), ("VALIGN",(0,0),(-1,-1),"MIDDLE")]))

    header = Table([
        [issuer_left, letter_box, issuer_right],
        [Paragraph("FACTURA", ParagraphStyle("title", parent=center, fontSize=16)), "", Paragraph(f"Punto de Venta: {pv:05d}<br/>Comp. Nro: {numero:08d}<br/>Fecha: {_date_ar(fecha)}", normal)],
    ], colWidths=[72*mm, 43*mm, 66*mm])
    header.setStyle(TableStyle([
        ("BOX",(0,0),(-1,-1),1,colors.black),
        ("VALIGN",(0,0),(-1,-1),"TOP"),
        ("SPAN",(0,1),(1,1)),
        ("LEFTPADDING",(0,0),(-1,-1),6), ("RIGHTPADDING",(0,0),(-1,-1),6),
        ("TOPPADDING",(0,0),(-1,-1),5), ("BOTTOMPADDING",(0,0),(-1,-1),5),
    ]))
    story.append(header)
    story.append(Spacer(1, 5*mm))

    client_rows = [
        [Paragraph("Señor(es):", bold), Paragraph(str(cliente.get("nombre") or ""), normal), Paragraph("CUIT:", bold), Paragraph(str(cliente.get("cuit") or ""), normal)],
        [Paragraph("Domicilio:", bold), Paragraph(str(cliente.get("domicilio") or ""), normal), Paragraph("Condición IVA:", bold), Paragraph(str(cliente.get("condicion_iva_receptor_desc") or ""), normal)],
        [Paragraph("Período facturado:", bold), Paragraph(str(comprobante.get("periodo_texto") or ""), normal), Paragraph("Vto. pago:", bold), Paragraph(_date_ar(str(comprobante.get("fecha_vto_pago") or "")), normal)],
    ]
    client_table = Table(client_rows, colWidths=[30*mm, 70*mm, 28*mm, 53*mm])
    client_table.setStyle(TableStyle([("BOX",(0,0),(-1,-1),0.8,colors.black), ("GRID",(0,0),(-1,-1),0.25,colors.grey), ("VALIGN",(0,0),(-1,-1),"TOP"), ("PADDING",(0,0),(-1,-1),4)]))
    story.append(client_table)
    story.append(Spacer(1, 5*mm))

    rows = [[Paragraph("Descripción", bold), Paragraph("Cant.", bold), Paragraph("Precio unit.", bold), Paragraph("Subtotal", bold)]]
    for it in items:
        rows.append([
            Paragraph(str(it.get("descripcion") or ""), normal),
            Paragraph(f"{float(it.get('cantidad') or 0):g}", right),
            Paragraph(_money(float(it.get("precio_unitario") or 0)), right),
            Paragraph(_money(float(it.get("subtotal") or 0)), right),
        ])
    item_table = Table(rows, colWidths=[102*mm, 18*mm, 30*mm, 31*mm], repeatRows=1)
    item_table.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0),colors.HexColor("#EEEEEE")),
        ("GRID",(0,0),(-1,-1),0.4,colors.grey),
        ("VALIGN",(0,0),(-1,-1),"TOP"),
        ("ALIGN",(1,1),(-1,-1),"RIGHT"),
        ("PADDING",(0,0),(-1,-1),4),
    ]))
    story.append(item_table)
    story.append(Spacer(1, 4*mm))

    totals_data = []
    if imp_iva > 0:
        totals_data.extend([[Paragraph("Subtotal neto", bold), Paragraph(_money(imp_neto), right)], [Paragraph("IVA", bold), Paragraph(_money(imp_iva), right)]])
    totals_data.append([Paragraph("IMPORTE TOTAL", ParagraphStyle("tb", parent=bold, fontSize=11)), Paragraph(_money(total), ParagraphStyle("tr", parent=right, fontName="Helvetica-Bold", fontSize=11))])
    totals = Table(totals_data, colWidths=[45*mm, 35*mm], hAlign="RIGHT")
    totals.setStyle(TableStyle([("GRID",(0,0),(-1,-1),0.5,colors.grey), ("PADDING",(0,0),(-1,-1),5)]))
    story.append(totals)
    story.append(Spacer(1, 6*mm))

    url = qr_url(fecha=fecha, cuit=str(emisor["cuit"]), punto_venta=pv, tipo=tipo, numero=numero,
                 importe=total, doc_tipo=doc_tipo, doc_nro=doc_nro, cae=cae)
    qr = Image(_qr_image(url), width=34*mm, height=34*mm)
    footer_text = [
        Paragraph("<b>ARCA · Comprobante Autorizado</b>", bold),
        Paragraph(f"CAE N°: {cae}", normal),
        Paragraph(f"Fecha de Vto. de CAE: {_date_ar(str(comprobante.get('vencimiento_cae') or ''))}", normal),
        Paragraph("El detalle de ítems es administrado por el sistema; la autorización fiscal fue obtenida mediante WSFEv1.", small),
    ]
    footer = Table([[qr, footer_text]], colWidths=[42*mm, 139*mm])
    footer.setStyle(TableStyle([("BOX",(0,0),(-1,-1),0.8,colors.black), ("VALIGN",(0,0),(-1,-1),"MIDDLE"), ("PADDING",(0,0),(-1,-1),6)]))
    story.append(footer)

    doc.build(story)
    return path
