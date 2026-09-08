# S&S Group · Gestión Administrativa

Sistema unificado:
- Control de cuentas corrientes
- Facturas PDF existentes
- Facturación nueva por período vigente / vencido / manual
- Clientes mensuales y trabajos puntuales
- Ítems y leyendas reutilizables
- Pagos
- Avisos de pago
- Exportación a Excel
- Preparado para conexión ARCA

## Publicación
Subir el contenido de esta carpeta al repositorio de GitHub y desplegar `app.py` en Streamlit Community Cloud.

## Importante
ARCA sigue en modo borrador. No solicita CAE real.
SQLite sirve para esta etapa de prueba; antes de producción se migrará a una base persistente online.

## Multiemisor ARCA
El sistema contempla tres emisores:
- Juan Ignacio Sirvent — 20-37769536-5
- Martín Nicolás Sirvent — 20-35140724-8
- Melissa Jennifer Bulacio Juarez — 27-41149423-9

Cada cliente puede tener un emisor habitual y cada comprobante guarda el emisor real.


## Envío automático de facturas
- Remitente: sysgroupmdp@gmail.com
- Asunto fijo: FACTURACION
- Cada cliente puede tener email de facturación y activar/desactivar envío automático.
- El envío se intenta únicamente cuando el comprobante está AUTORIZADA y existe el PDF final.
- Se registra destinatario, fecha/hora, estado y error si lo hubiera.
- Desde ARCA se podrá descargar el PDF y reenviar el correo.
- La credencial de Gmail debe cargarse como secreto privado; nunca dentro de app.py ni del repositorio.
