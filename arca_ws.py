from __future__ import annotations

import base64
import html
import re
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests


class ARCAError(RuntimeError):
    pass


WSAA_URLS = {
    "HOMOLOGACION": "https://wsaahomo.afip.gov.ar/ws/services/LoginCms",
    "PRODUCCION": "https://wsaa.afip.gov.ar/ws/services/LoginCms",
}

WSFE_URLS = {
    "HOMOLOGACION": "https://wswhomo.afip.gov.ar/wsfev1/service.asmx",
    "PRODUCCION": "https://servicios1.afip.gov.ar/wsfev1/service.asmx",
}

CBTE_CODES = {"A": 1, "B": 6, "C": 11, "M": 51}
IVA_RATE_IDS = {0.0: 3, 10.5: 4, 21.0: 5, 27.0: 6, 5.0: 8, 2.5: 9}


def digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _tag_ends(node: ET.Element, suffix: str) -> bool:
    return node.tag.split("}")[-1] == suffix


def _find_text(root: ET.Element, suffix: str) -> Optional[str]:
    for el in root.iter():
        if _tag_ends(el, suffix):
            return el.text
    return None


def _find_all(root: ET.Element, suffix: str) -> List[ET.Element]:
    return [el for el in root.iter() if _tag_ends(el, suffix)]


def _soap_fault(root: ET.Element) -> Optional[str]:
    fault = _find_text(root, "faultstring") or _find_text(root, "Text")
    return fault.strip() if fault else None


def _parse_soap(response: requests.Response) -> ET.Element:
    try:
        response.raise_for_status()
    except requests.HTTPError as e:
        raise ARCAError(f"ARCA respondió HTTP {response.status_code}: {response.text[:600]}") from e
    try:
        root = ET.fromstring(response.content)
    except ET.ParseError as e:
        raise ARCAError(f"Respuesta XML inválida de ARCA: {response.text[:600]}") from e
    fault = _soap_fault(root)
    if fault:
        raise ARCAError(f"ARCA SOAP Fault: {fault}")
    return root


def _openssl_available() -> bool:
    try:
        subprocess.run(["openssl", "version"], check=True, capture_output=True, timeout=10)
        return True
    except Exception:
        return False


def generar_clave_y_csr(cuit: str, organizacion: str, cn: str = "gestion-sysgroup") -> tuple[bytes, bytes]:
    """Genera clave RSA 2048 y CSR PKCS#10 conforme al formato documentado por ARCA."""
    cuit_d = digits(cuit)
    if len(cuit_d) != 11:
        raise ARCAError("El CUIT debe tener 11 dígitos.")
    if not _openssl_available():
        raise ARCAError("OpenSSL no está disponible en este entorno.")
    org = re.sub(r"[^A-Za-z0-9 .&_-]", "", organizacion or "S&S Group")[:60]
    cn = re.sub(r"[^A-Za-z0-9._-]", "", cn or "gestion-sysgroup")[:60]
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        key_path = td / "private.key"
        csr_path = td / "request.csr"
        subprocess.run(["openssl", "genrsa", "-out", str(key_path), "2048"], check=True, capture_output=True, timeout=30)
        subj = f"/C=AR/O={org}/CN={cn}/serialNumber=CUIT {cuit_d}"
        proc = subprocess.run(
            ["openssl", "req", "-new", "-key", str(key_path), "-subj", subj, "-out", str(csr_path)],
            check=False, capture_output=True, text=True, timeout=30
        )
        if proc.returncode != 0:
            raise ARCAError(f"No se pudo generar el CSR: {proc.stderr.strip()}")
        return key_path.read_bytes(), csr_path.read_bytes()


def _build_tra(service: str = "wsfe") -> bytes:
    now = datetime.now(timezone.utc)
    gen = now - timedelta(minutes=5)
    exp = now + timedelta(minutes=15)
    uid = int(time.time())
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<loginTicketRequest version="1.0">'
        '<header>'
        f'<uniqueId>{uid}</uniqueId>'
        f'<generationTime>{gen.isoformat(timespec="seconds")}</generationTime>'
        f'<expirationTime>{exp.isoformat(timespec="seconds")}</expirationTime>'
        '</header>'
        f'<service>{service}</service>'
        '</loginTicketRequest>'
    )
    return xml.encode("utf-8")


def _sign_cms(tra: bytes, cert_pem: bytes, key_pem: bytes) -> str:
    if not _openssl_available():
        raise ARCAError("OpenSSL no está disponible para firmar el Ticket de Acceso de ARCA.")
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        tra_path = td / "tra.xml"
        cert_path = td / "cert.pem"
        key_path = td / "key.pem"
        cms_path = td / "tra.cms"
        tra_path.write_bytes(tra)
        cert_path.write_bytes(cert_pem)
        key_path.write_bytes(key_pem)
        cmd = [
            "openssl", "smime", "-sign", "-binary",
            "-signer", str(cert_path), "-inkey", str(key_path),
            "-in", str(tra_path), "-out", str(cms_path),
            "-outform", "DER", "-nodetach"
        ]
        proc = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            raise ARCAError(f"No se pudo firmar el acceso a ARCA: {proc.stderr.strip()}")
        return base64.b64encode(cms_path.read_bytes()).decode("ascii")


@dataclass
class TicketAcceso:
    token: str
    sign: str
    expiration_time: str


def wsaa_login(cert_pem: bytes, key_pem: bytes, ambiente: str = "PRODUCCION", service: str = "wsfe") -> TicketAcceso:
    ambiente = ambiente.upper()
    if ambiente not in WSAA_URLS:
        raise ARCAError("Ambiente ARCA inválido.")
    cms = _sign_cms(_build_tra(service), cert_pem, key_pem)
    envelope = f'''<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:wsaa="http://wsaa.view.sua.dvadac.desein.afip.gov">
  <soapenv:Header/>
  <soapenv:Body>
    <wsaa:loginCms><wsaa:in0>{html.escape(cms)}</wsaa:in0></wsaa:loginCms>
  </soapenv:Body>
</soapenv:Envelope>'''.encode("utf-8")
    resp = requests.post(
        WSAA_URLS[ambiente], data=envelope,
        headers={"Content-Type": "text/xml;charset=UTF-8", "SOAPAction": "urn:LoginCms"},
        timeout=45,
    )
    root = _parse_soap(resp)
    result = _find_text(root, "loginCmsReturn")
    if not result:
        raise ARCAError("WSAA no devolvió un Ticket de Acceso.")
    try:
        ticket_xml = ET.fromstring(result)
    except ET.ParseError as e:
        raise ARCAError("WSAA devolvió un Ticket de Acceso inválido.") from e
    token = _find_text(ticket_xml, "token")
    sign = _find_text(ticket_xml, "sign")
    expiration = _find_text(ticket_xml, "expirationTime") or ""
    if not token or not sign:
        raise ARCAError("No se pudieron extraer Token/Sign del Ticket de Acceso.")
    return TicketAcceso(token=token, sign=sign, expiration_time=expiration)


def _auth_xml(ticket: TicketAcceso, cuit: str) -> str:
    cuit_d = digits(cuit)
    if len(cuit_d) != 11:
        raise ARCAError("CUIT emisor inválido.")
    return f"<Auth><Token>{html.escape(ticket.token)}</Token><Sign>{html.escape(ticket.sign)}</Sign><Cuit>{cuit_d}</Cuit></Auth>"


def _wsfe_post(method: str, body_inside: str, ambiente: str) -> ET.Element:
    ambiente = ambiente.upper()
    if ambiente not in WSFE_URLS:
        raise ARCAError("Ambiente WSFE inválido.")
    xml = f'''<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <{method} xmlns="http://ar.gov.afip.dif.FEV1/">{body_inside}</{method}>
  </soap:Body>
</soap:Envelope>'''.encode("utf-8")
    resp = requests.post(
        WSFE_URLS[ambiente], data=xml,
        headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": f'"http://ar.gov.afip.dif.FEV1/{method}"'},
        timeout=45,
    )
    return _parse_soap(resp)


def _collect_errors(root: ET.Element) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for err in _find_all(root, "Err"):
        code = _find_text(err, "Code") or ""
        msg = _find_text(err, "Msg") or ""
        out.append({"code": code, "msg": msg})
    return out


def _collect_obs(root: ET.Element) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for ob in _find_all(root, "Obs"):
        code = _find_text(ob, "Code") or ""
        msg = _find_text(ob, "Msg") or ""
        out.append({"code": code, "msg": msg})
    return out


def wsfe_ultimo_autorizado(ticket: TicketAcceso, cuit: str, punto_venta: int, cbte_tipo: int, ambiente: str) -> int:
    root = _wsfe_post(
        "FECompUltimoAutorizado",
        _auth_xml(ticket, cuit) + f"<PtoVta>{int(punto_venta)}</PtoVta><CbteTipo>{int(cbte_tipo)}</CbteTipo>",
        ambiente,
    )
    errors = _collect_errors(root)
    if errors:
        raise ARCAError("; ".join(f"{e['code']}: {e['msg']}" for e in errors))
    val = _find_text(root, "CbteNro")
    if val is None:
        raise ARCAError("ARCA no devolvió el último comprobante autorizado.")
    return int(val)


def wsfe_puntos_venta(ticket: TicketAcceso, cuit: str, ambiente: str) -> List[Dict[str, str]]:
    root = _wsfe_post("FEParamGetPtosVenta", _auth_xml(ticket, cuit), ambiente)
    errors = _collect_errors(root)
    if errors:
        raise ARCAError("; ".join(f"{e['code']}: {e['msg']}" for e in errors))
    out = []
    for p in _find_all(root, "PtoVenta"):
        out.append({
            "nro": _find_text(p, "Nro") or "",
            "emision_tipo": _find_text(p, "EmisionTipo") or "",
            "bloqueado": _find_text(p, "Bloqueado") or "",
            "fch_baja": _find_text(p, "FchBaja") or "",
        })
    return out


def wsfe_condiciones_iva_receptor(ticket: TicketAcceso, cuit: str, ambiente: str, clase_cmp: Optional[str] = None) -> List[Dict[str, str]]:
    extra = _auth_xml(ticket, cuit)
    if clase_cmp:
        extra += f"<ClaseCmp>{html.escape(clase_cmp.upper())}</ClaseCmp>"
    root = _wsfe_post("FEParamGetCondicionIvaReceptor", extra, ambiente)
    errors = _collect_errors(root)
    if errors:
        raise ARCAError("; ".join(f"{e['code']}: {e['msg']}" for e in errors))
    out = []
    # ARCA devuelve estructuras CondicionIvaReceptor con Id/Desc/Cmp_Clase.
    for node in root.iter():
        local = node.tag.split("}")[-1].lower()
        if "condicion" in local and "receptor" in local and len(list(node)):
            idv = _find_text(node, "Id")
            desc = _find_text(node, "Desc")
            if idv and desc:
                out.append({"id": idv, "desc": desc, "clase": _find_text(node, "Cmp_Clase") or _find_text(node, "ClaseCmp") or ""})
    # dedupe
    seen = set(); dedup = []
    for r in out:
        key = (r["id"], r["desc"], r["clase"])
        if key not in seen:
            seen.add(key); dedup.append(r)
    return dedup


def wsfe_solicitar_cae(
    ticket: TicketAcceso,
    cuit: str,
    ambiente: str,
    *,
    punto_venta: int,
    cbte_tipo: int,
    concepto: int,
    doc_tipo: int,
    doc_nro: str,
    cbte_nro: int,
    fecha_cbte: str,
    imp_total: float,
    imp_neto: float,
    imp_iva: float,
    condicion_iva_receptor_id: int,
    fecha_serv_desde: Optional[str] = None,
    fecha_serv_hasta: Optional[str] = None,
    fecha_vto_pago: Optional[str] = None,
    iva_rate: Optional[float] = None,
) -> Dict[str, Any]:
    def f2(x: float) -> str:
        return f"{float(x):.2f}"

    iva_xml = ""
    if imp_iva > 0:
        rate = float(iva_rate or 21.0)
        rate_id = IVA_RATE_IDS.get(rate)
        if not rate_id:
            raise ARCAError(f"Alícuota IVA {rate}% no soportada por la configuración.")
        iva_xml = (
            "<Iva><AlicIva>"
            f"<Id>{rate_id}</Id><BaseImp>{f2(imp_neto)}</BaseImp><Importe>{f2(imp_iva)}</Importe>"
            "</AlicIva></Iva>"
        )

    serv_xml = ""
    if int(concepto) in (2, 3):
        if not fecha_serv_desde or not fecha_serv_hasta or not fecha_vto_pago:
            raise ARCAError("Para servicios ARCA exige período de servicio y fecha de vencimiento de pago.")
        serv_xml = (
            f"<FchServDesde>{fecha_serv_desde}</FchServDesde>"
            f"<FchServHasta>{fecha_serv_hasta}</FchServHasta>"
            f"<FchVtoPago>{fecha_vto_pago}</FchVtoPago>"
        )

    req = (
        _auth_xml(ticket, cuit)
        + "<FeCAEReq><FeCabReq>"
        + f"<CantReg>1</CantReg><PtoVta>{int(punto_venta)}</PtoVta><CbteTipo>{int(cbte_tipo)}</CbteTipo>"
        + "</FeCabReq><FeDetReq><FECAEDetRequest>"
        + f"<Concepto>{int(concepto)}</Concepto><DocTipo>{int(doc_tipo)}</DocTipo><DocNro>{digits(doc_nro)}</DocNro>"
        + f"<CbteDesde>{int(cbte_nro)}</CbteDesde><CbteHasta>{int(cbte_nro)}</CbteHasta><CbteFch>{fecha_cbte}</CbteFch>"
        + f"<ImpTotal>{f2(imp_total)}</ImpTotal><ImpTotConc>0.00</ImpTotConc><ImpNeto>{f2(imp_neto)}</ImpNeto>"
        + f"<ImpOpEx>0.00</ImpOpEx><ImpTrib>0.00</ImpTrib><ImpIVA>{f2(imp_iva)}</ImpIVA>"
        + serv_xml
        + "<MonId>PES</MonId><MonCotiz>1.000000</MonCotiz>"
        + f"<CondicionIVAReceptorId>{int(condicion_iva_receptor_id)}</CondicionIVAReceptorId>"
        + iva_xml
        + "</FECAEDetRequest></FeDetReq></FeCAEReq>"
    )
    root = _wsfe_post("FECAESolicitar", req, ambiente)
    errors = _collect_errors(root)
    obs = _collect_obs(root)
    result = _find_text(root, "Resultado") or ""
    cae = _find_text(root, "CAE") or ""
    cae_vto = _find_text(root, "CAEFchVto") or ""
    cbte_desde_resp = _find_text(root, "CbteDesde") or str(cbte_nro)
    if errors:
        return {"ok": False, "resultado": result, "errors": errors, "observaciones": obs, "cae": cae, "cae_vto": cae_vto, "cbte_nro": int(cbte_desde_resp)}
    ok = bool(cae) and result.upper() in ("A", "P")
    return {"ok": ok, "resultado": result, "errors": errors, "observaciones": obs, "cae": cae, "cae_vto": cae_vto, "cbte_nro": int(cbte_desde_resp)}
