import csv
import io
import os
import re
import time
from datetime import datetime, timedelta
from google import genai
from google.genai import types

SYSTEM_PROMPT = """# ROL Y OBJETIVO
Eres un especialista en extracción de datos contables argentinos (normativa AFIP/ARCA) e integración ERP con Odoo. Tu tarea es extraer datos de facturas de proveedores y estructurarlos en un formato CSV estricto, diseñado para conciliar facturas contra Órdenes de Compra (OC) preexistentes en Odoo.

# REGLAS CRÍTICAS DE NEGOCIO Y EXTRACCIÓN

1. **EMISOR vs. RECEPTOR:**
   - Extrae únicamente los datos fiscales del **EMISOR** (proveedor, ubicado generalmente en el cuadrante superior izquierdo).
   - Ignora por completo el CUIT y razón social del receptor/cliente ("Señor/es", "Destinatario").

2. **VENCIMIENTO DE PAGO vs. VENCIMIENTO DE CAE (REGLA DE UBICACIÓN FÍSICA):**
   - **TRAMPA COMÚN EN ARGENTINA:** En el tercio inferior / pie de página de las facturas electrónicas aparece el número de `C.A.E.` y justo al lado o debajo dice **"Vencimiento XX/XX/XXXX"** (suele ser entre 10 y 11 días después de la fecha de emisión). **ESA FECHA ES EL VENCIMIENTO DEL CAE DE AFIP, NO EL VENCIMIENTO DE PAGO.** Está TERMINANTEMENTEMENTE PROHIBIDO poner esa fecha del pie en `due_date`.
   - Solo completa `due_date` si en el **ENCABEZADO SUPERIOR** existe un recuadro explícito de "Vencimiento de Pago" con una fecha impresa distinta al Vto. del CAE. Si en el encabezado solo dice `Condiciones de venta: 30 días Fecha Factura` (sin fecha calendario impresa arriba), deja `due_date` **VACÍO (`""`)** y copia el texto exacto en `payment_terms`.

3. **ORDEN DE COMPRA LIMPIA (`purchase_order_number`):**
   - Busca en "Orden Compra", "O.C.", "Ref. Cliente" (NO uses "Nota de Pedido" ni "Remito" salvo que no exista Orden de Compra).
   - Limpia descuentos o comentarios pegados al número: por ejemplo, si dice `"Orden Compra: 94076 5%"`, extrae únicamente `94076`.

4. **DESCUENTO GLOBAL EN PIE DE PÁGINA (`global_discount_amount`):**
   - Si las líneas no tienen descuento individual (`0.0`), pero en el pie antes del SUBTOTAL / IVA hay un renglón `DESCUENTOS` (ej: `DESCUENTOS $ 11,751,864.46` / `44.79%`), coloca ese monto exacto (`11751864.46`) en `global_discount_amount` y coloca la suma bruta de líneas en `gross_subtotal` y el subtotal restado en `net_taxable_base`.

5. **TRATAMIENTO DE LÍNEAS, CÓDIGOS Y PRECIOS (SIN IVA):**
   - Crea una fila en el CSV por cada línea de producto/servicio facturado. Ignora líneas meramente informativas ("Son pesos...", despachos de importación, textos legales) salvo que aporten el número de OC.
   - En `product_code`, extrae todos los códigos de artículo del proveedor visibles en esa línea (si hay más de uno, únelos con ` / `).
   - En Facturas "A" o "M", extrae precios unitarios y subtotales de línea **NETOS (sin IVA)**. Si el proveedor no detalla alícuota de IVA por línea, NO la inventes.

6. **DESCUENTOS E IMPUESTOS DE PIE DE FACTURA:**
   - `discount_percentage`: Descuento porcentual de esa fila. Si hay cascada (ej: 10% + 5%), calcula el equivalente único (`14.5`).
   - Separa claramente el IVA total (`total_vat`) de percepciones y otros tributos (`other_taxes_perceptions`).
   - En `perceptions_detail`, usa SIEMPRE el separador ` | ` (barra vertical) entre cada percepción.

7. **REGLAS DE FORMATO TÉCNICO:**
   - **Estructura Plana:** Repite los datos de cabecera y totales de pie en todas las filas del mismo comprobante.
   - **Números (Float):** Punto (`.`) como separador decimal, sin separador de miles ni símbolos de moneda (`1234.56`). Nulos numéricos: `0.0`.
   - **Fechas:** Formato ISO estricto `YYYY-MM-DD`.
   - **Comprobante Odoo AR (`invoice_number`):** Formatea SIEMPRE con ceros a la izquierda: **5 dígitos** punto de venta + guion + **8 dígitos** número (ej: si dice `0004-141869`, escribe `00004-00141869`).
   - **Texto y CSV:** Delimitador coma (`,`). Encierra SIEMPRE entre comillas dobles (`"..."`) `vendor_name`, `payment_terms`, `product_code`, `description` y `perceptions_detail`. Reemplaza comillas dobles internas por simples (`'`).
   - **Salida Pura:** Devuelve ÚNICAMENTE el texto plano del CSV, sin bloques ```csv ni explicaciones.

# ESTRUCTURA CSV REQUERIDA (CABECERAS EXACTAS EN FILA 1)
vendor_name,vendor_cuit,document_type,invoice_letter,invoice_number,invoice_date,due_date,payment_terms,cae_number,currency,exchange_rate,purchase_order_number,product_code,description,quantity,unit_price,discount_percentage,line_net_total,gross_subtotal,global_discount_amount,net_taxable_base,exempt_amount,total_vat,other_taxes_perceptions,perceptions_detail,total_amount
"""


def _sanitize_and_compute_due_date(invoice_date_str: str, due_date_str: str, payment_terms: str) -> str:
    inv_dt = None
    if invoice_date_str:
        try:
            inv_dt = datetime.strptime(invoice_date_str, "%Y-%m-%d")
        except ValueError:
            pass

    term_days = None
    if payment_terms and not re.search(r"\d+\s*[/,-]\s*\d+", payment_terms):
        m = re.search(r"\b(\d{1,3})\s*d[ií]as?\b", payment_terms.lower())
        if m:
            term_days = int(m.group(1))

    if due_date_str and inv_dt:
        try:
            due_dt = datetime.strptime(due_date_str, "%Y-%m-%d")
            delta_days = (due_dt - inv_dt).days
            if (term_days and abs(delta_days - term_days) > 3 and 8 <= delta_days <= 12) or (
                not term_days and 8 <= delta_days <= 12 and "contado" not in (payment_terms or "").lower()
            ):
                due_date_str = ""
        except ValueError:
            due_date_str = ""

    if not due_date_str and inv_dt and term_days is not None:
        return (inv_dt + timedelta(days=term_days)).strftime("%Y-%m-%d")

    return due_date_str or ""


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


def _normalize_ar_doc_number(raw_num: str) -> str:
    s = (raw_num or "").strip()
    if not s:
        return ""
    if "-" in s:
        parts = s.split("-", 1)
        pos = re.sub(r"\D", "", parts[0])
        num = re.sub(r"\D", "", parts[1])
        if pos and num:
            return f"{pos.zfill(5)[-5:]}-{num.zfill(8)[-8:]}"
    digits = re.sub(r"\D", "", s)
    if len(digits) > 8:
        return f"{digits[:-8].zfill(5)[-5:]}-{digits[-8:]}"
    if digits:
        return f"00001-{digits.zfill(8)[-8:]}"
    return s


def _parse_perceptions_detail(detail_str: str) -> list:
    if not detail_str or not detail_str.strip():
        return []
    out = []
    for part in [p.strip() for p in re.split(r"\s*[|;]\s*", detail_str) if p.strip()]:
        if ":" in part:
            label, amt_str = part.rsplit(":", 1)
            amt_clean = re.sub(r"[^\d.,-]", "", amt_str)
            out.append({"label": label.strip(), "amount": round(_safe_float(amt_clean, 0.0), 2)})
        else:
            out.append({"label": part, "amount": 0.0})
    return out


def extract_invoice_with_gemini(file_bytes: bytes, mime_type: str) -> dict:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Falta configurar GEMINI_API_KEY en las variables de entorno.")

    primary_model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
    thinking_lvl = os.getenv("GEMINI_THINKING_LEVEL", "LOW").strip().upper()

    fallback_candidates = [
        primary_model,
        "gemini-3.8-flash",
        "gemini-3.5-flash-lite",
        "gemini-2.5-flash",
        "gemini-2.0-flash",
    ]
    models_to_try = list(dict.fromkeys(m for m in fallback_candidates if m))

    client = genai.Client(api_key=api_key)
    contents = [
        types.Content(
            role="user",
            parts=[
                types.Part(inline_data=types.Blob(mime_type=mime_type, data=file_bytes)),
                types.Part.from_text(
                    text="Extrae los datos del comprobante en CSV plano según las reglas."
                ),
            ],
        )
    ]

    response = None
    used_model = primary_model
    last_error = None

    for model_name in models_to_try:
        cfg_kwargs = {"system_instruction": SYSTEM_PROMPT, "temperature": 0.0}
        try:
            if "3.8" in model_name or "3.5" in model_name:
                lvl = "LOW" if ("3.8" in model_name and thinking_lvl == "MINIMAL") else thinking_lvl
                cfg_kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=lvl)
            cfg_kwargs["media_resolution"] = "MEDIA_RESOLUTION_MEDIUM"
            config = types.GenerateContentConfig(**cfg_kwargs)
        except Exception:
            cfg_kwargs.pop("thinking_config", None)
            cfg_kwargs.pop("media_resolution", None)
            config = types.GenerateContentConfig(**cfg_kwargs)

        for attempt in range(2):
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=config,
                )
                used_model = model_name
                break
            except Exception as exc:
                last_error = exc
                err_str = str(exc).upper()
                is_transient = any(
                    code in err_str
                    for code in ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "OVERLOADED", "500")
                )
                if is_transient:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                if "404" in err_str or "NOT_FOUND" in err_str or "INVALID_ARGUMENT" in err_str:
                    break
                raise

        if response is not None:
            break

    if response is None:
        raise RuntimeError(
            f"No se obtuvo respuesta de los modelos Gemini. Último error: {type(last_error).__name__}: {last_error}"
        )

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
                clean_tok = re.sub(r"\s+\d+(?:[.,]\d+)?\s*%.*$", "", tok.strip()).strip()
                if clean_tok and clean_tok not in po_candidates:
                    po_candidates.append(clean_tok)

    inv_date_clean = (first.get("invoice_date") or "").strip()
    raw_due_clean = (first.get("due_date") or "").strip()
    pay_terms_clean = (first.get("payment_terms") or "").strip()

    header = {
        "vendor_name": (first.get("vendor_name") or "").strip(),
        "vendor_cuit": (first.get("vendor_cuit") or "").strip(),
        "document_type": (first.get("document_type") or "FACTURA").strip().upper(),
        "invoice_letter": (first.get("invoice_letter") or "A").strip().upper(),
        "invoice_number": _normalize_ar_doc_number(first.get("invoice_number") or ""),
        "invoice_date": inv_date_clean,
        "payment_terms": pay_terms_clean,
        "due_date": _sanitize_and_compute_due_date(inv_date_clean, raw_due_clean, pay_terms_clean),
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
        raw_pcode = (r.get("product_code") or "").strip()
        code_list = [c.strip() for c in re.split(r"[/|;]+", raw_pcode) if c.strip()]
        lines.append({
            "index": idx,
            "product_code": raw_pcode,
            "product_codes": code_list,
            "description": (r.get("description") or "").strip(),
            "quantity": qty,
            "unit_price": price,
            "discount_percentage": disc,
            "line_net_total": net,
        })

    # AHORA SÍ (después de poblar lines): prorratear descuento global de pie si las líneas vinieron con 0%
    global_disc = header["global_discount_amount"]
    sum_lines_gross = sum(l["line_net_total"] for l in lines)
    if global_disc > 0 and sum_lines_gross > global_disc and all(l["discount_percentage"] == 0.0 for l in lines):
        exact_disc_pct = round((global_disc / sum_lines_gross) * 100.0, 4)
        for l in lines:
            l["discount_percentage"] = exact_disc_pct
            l["line_net_total"] = round(l["quantity"] * l["unit_price"] * (1.0 - exact_disc_pct / 100.0), 2)

    usage = {}
    if getattr(response, "usage_metadata", None):
        um = response.usage_metadata
        usage = {
            "prompt_tokens": getattr(um, "prompt_token_count", 0) or 0,
            "output_tokens": getattr(um, "candidates_token_count", 0) or 0,
            "total_tokens": getattr(um, "total_token_count", 0) or 0,
            "model": used_model,
        }

    return {"header": header, "lines": lines, "raw_csv": raw_csv, "usage": usage}