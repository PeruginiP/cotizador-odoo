import csv
import io
import os
import re
from google import genai
from google.genai import types

SYSTEM_PROMPT = """# ROL Y OBJETIVO
Eres un especialista en extracción de datos contables argentinos (normativa AFIP/ARCA) e integración ERP con Odoo. Tu tarea es extraer datos de facturas de proveedores y estructurarlos en un formato CSV estricto, diseñado para conciliar facturas contra Órdenes de Compra (OC) preexistentes en Odoo.

# REGLAS CRÍTICAS DE NEGOCIO Y EXTRACCIÓN
1. **EMISOR vs. RECEPTOR:**
   - Extrae únicamente los datos fiscales del **EMISOR** (proveedor, ubicado generalmente en el cuadrante superior izquierdo).
   - Ignora por completo el CUIT y razón social del receptor/cliente ("Señor/es", "Destinatario").
2. **BÚSQUEDA EXHAUSTIVA DE ORDEN DE COMPRA (`purchase_order_number`):**
   - Búscalo en campos dedicados ("Orden de Compra", "O.C.", "Ref. Cliente", "Pedido") y también dentro de textos libres, notas al pie, remitos o descripción de líneas (ej: "S/OC 4502", "PO00123", "93757").
   - Extrae únicamente el identificador limpio. Si hay múltiples OC, sepáralas con barra (`/`). Si no existe, deja el campo vacío.
3. **TRATAMIENTO DE LÍNEAS Y PRECIOS (SIN IVA):**
   - Crea una fila en el CSV por cada línea de producto/servicio facturado. Ignora líneas meramente informativas salvo que contengan la OC.
   - En Facturas "A" o "M", extrae precios unitarios y subtotales **NETOS (sin IVA)**. No inventes alícuotas por línea.
4. **DESCUENTOS (LÍNEA vs. PIE DE PÁGINA):**
   - `discount_percentage`: Solo descuentos de esa fila (si hay cascada ej. 10%+5%, calcula el equivalente único `14.5`).
   - `global_discount_amount`: Descuentos globales en el pie antes de impuestos como positivo (ej: `1500.00`), o `0.0`.
5. **IMPUESTOS Y PERCEPCIONES (PIE DE FACTURA):**
   - Separa el IVA total (`total_vat`) de otros tributos/percepciones (`other_taxes_perceptions`).
6. **REGLAS DE FORMATO TÉCNICO:**
   - Repite los datos de cabecera y pie en todas las filas del mismo comprobante.
   - Números Float con punto decimal (`.`), sin separador de miles ni símbolo de moneda. Nulos: `0.0`.
   - Fechas ISO `YYYY-MM-DD`.
   - `invoice_number`: 5 dígitos punto de venta + guion + 8 dígitos número (ej: `00004-00011848`).
   - Delimitador coma (`,`). Encierra SIEMPRE entre comillas dobles (`"..."`) `vendor_name`, `description` y `perceptions_detail`. Reemplaza comillas dobles internas por simples (`'`).
   - Devuelve ÚNICAMENTE texto plano CSV sin bloques Markdown.

# ESTRUCTURA CSV REQUERIDA (CABECERAS EXACTAS EN FILA 1)
vendor_name,vendor_cuit,document_type,invoice_letter,invoice_number,invoice_date,due_date,cae_number,currency,exchange_rate,purchase_order_number,product_code,description,quantity,unit_price,discount_percentage,line_net_total,gross_subtotal,global_discount_amount,net_taxable_base,exempt_amount,total_vat,other_taxes_perceptions,perceptions_detail,total_amount"""


def _safe_float(val, default=0.0) -> float:
    if val is None:
        return default
    s = str(val).strip().replace("$", "").replace(" ", "")
    if not s:
        return default
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return default


def _clean_csv_text(raw_text: str) -> str:
    txt = raw_text.strip()
    txt = re.sub(r"^```(?:csv)?\s*", "", txt, flags=re.IGNORECASE)
    txt = re.sub(r"\s*```$", "", txt)
    return txt.strip()


def _parse_perceptions_detail(detail_str: str) -> list:
    if not detail_str or not detail_str.strip():
        return []
    out = []
    for part in [p.strip() for p in detail_str.split("|") if p.strip()]:
        if ":" in part:
            label, amt_str = part.rsplit(":", 1)
            out.append({"label": label.strip(), "amount": round(_safe_float(amt_str, 0.0), 2)})
        else:
            out.append({"label": part, "amount": 0.0})
    return out


def extract_invoice_with_gemini(file_bytes: bytes, mime_type: str) -> dict:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Falta configurar GEMINI_API_KEY en las variables de entorno.")

    model_name = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()
    thinking_lvl = os.getenv("GEMINI_THINKING_LEVEL", "MINIMAL").strip().upper()

    client = genai.Client(api_key=api_key)
    contents = [
        types.Content(
            role="user",
            parts=[
                types.Part(inline_data=types.Blob(mime_type=mime_type, data=file_bytes)),
                types.Part.from_text(text="Extrae los datos del comprobante en CSV plano según las reglas."),
            ],
        )
    ]

    cfg_kwargs = {"system_instruction": SYSTEM_PROMPT, "temperature": 0.0}
    try:
        cfg_kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=thinking_lvl)
        cfg_kwargs["media_resolution"] = "MEDIA_RESOLUTION_MEDIUM"
        config = types.GenerateContentConfig(**cfg_kwargs)
    except Exception:
        cfg_kwargs.pop("thinking_config", None)
        cfg_kwargs.pop("media_resolution", None)
        config = types.GenerateContentConfig(**cfg_kwargs)

    response = client.models.generate_content(model=model_name, contents=contents, config=config)
    raw_csv = _clean_csv_text(response.text or "")
    if not raw_csv:
        raise ValueError("Gemini devolvió una respuesta vacía.")

    rows = list(csv.DictReader(io.StringIO(raw_csv)))
    if not rows:
        raise ValueError(f"No se pudo parsear el CSV devuelto: {raw_csv[:300]}")

    first = rows[0]
    po_candidates = []
    for r in rows:
        raw_po = (r.get("purchase_order_number") or "").strip()
        if raw_po:
            for tok in re.split(r"[/,;]+", raw_po):
                clean_tok = tok.strip()
                if clean_tok and clean_tok not in po_candidates:
                    po_candidates.append(clean_tok)

    header = {
        "vendor_name": (first.get("vendor_name") or "").strip(),
        "vendor_cuit": (first.get("vendor_cuit") or "").strip(),
        "document_type": (first.get("document_type") or "FACTURA").strip().upper(),
        "invoice_letter": (first.get("invoice_letter") or "A").strip().upper(),
        "invoice_number": (first.get("invoice_number") or "").strip(),
        "invoice_date": (first.get("invoice_date") or "").strip(),
        "due_date": (first.get("due_date") or "").strip(),
        "cae_number": re.sub(r"\D", "", first.get("cae_number") or ""),
        "currency": (first.get("currency") or "ARS").strip().upper(),
        "exchange_rate": _safe_float(first.get("exchange_rate"), 1.0),
        "purchase_order_number": " / ".join(po_candidates),
        "po_references": po_candidates,
        "gross_subtotal": _safe_float(first.get("gross_subtotal"), 0.0),
        "global_discount_amount": _safe_float(first.get("global_discount_amount"), 0.0),
        "net_taxable_base": _safe_float(first.get("net_taxable_base"), 0.0),
        "exempt_amount": _safe_float(first.get("exempt_amount"), 0.0),
        "total_vat": _safe_float(first.get("total_vat"), 0.0),
        "other_taxes_perceptions": _safe_float(first.get("other_taxes_perceptions"), 0.0),
        "perceptions_detail": (first.get("perceptions_detail") or "").strip(),
        "parsed_perceptions": _parse_perceptions_detail(first.get("perceptions_detail") or ""),
        "total_amount": _safe_float(first.get("total_amount"), 0.0),
    }

    lines = []
    for idx, r in enumerate(rows):
        qty = _safe_float(r.get("quantity"), 1.0)
        price = _safe_float(r.get("unit_price"), 0.0)
        disc = _safe_float(r.get("discount_percentage"), 0.0)
        net = _safe_float(r.get("line_net_total"), round(qty * price * (1.0 - disc / 100.0), 2))
        lines.append({
            "index": idx,
            "product_code": (r.get("product_code") or "").strip(),
            "description": (r.get("description") or "").strip(),
            "quantity": qty,
            "unit_price": price,
            "discount_percentage": disc,
            "line_net_total": net,
        })

    usage = {}
    if getattr(response, "usage_metadata", None):
        um = response.usage_metadata
        usage = {
            "prompt_tokens": getattr(um, "prompt_token_count", 0) or 0,
            "output_tokens": getattr(um, "candidates_token_count", 0) or 0,
            "total_tokens": getattr(um, "total_token_count", 0) or 0,
            "model": model_name,
        }

    return {"header": header, "lines": lines, "raw_csv": raw_csv, "usage": usage}