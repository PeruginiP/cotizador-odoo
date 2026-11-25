import csv
import io
import json
import os
import re
import time
from datetime import datetime, timedelta
from typing import List, Optional
from pydantic import BaseModel, Field
from google import genai
from google.genai import types


# --- ESQUEMA ESTRICTO PARA GEMINI STRUCTURED OUTPUTS ---
class ExtractedLineSchema(BaseModel):
    product_code: str = Field(
        default="",
        description="Código(s) del artículo según proveedor. Si hay más de un código en la misma línea (ej. alfanumérico y numérico), unirlos con ' | '.",
    )
    description: str = Field(
        default="",
        description="Descripción limpia del artículo en una sola línea (puedes conservar pulgadas como 1/2\" o 3/4\" sin problemas).",
    )
    uom_label: str = Field(
        default="",
        description="Texto de unidad de medida o presentación si figura (ej: 'Caja x 25 unid.', 'Paq x 20 mts.', 'Rollo x 100m', 'Unitario').",
    )
    pack_size: float = Field(
        default=1.0,
        description="Cantidad de unidades o metros contenidos por bulto/caja/paquete/rollo si se factura por envase cerrado (ej: 'Caja x 25 unid.' -> 25.0, 'Paq x 20 mts.' -> 20.0, 'Unitario' -> 1.0).",
    )
    quantity: float = Field(default=1.0, description="Cantidad facturada en la línea.")
    unit_price: float = Field(
        default=0.0,
        description="Precio unitario NETO sin IVA y ANTES de descuentos de línea. Si hay Precio Lista y Precio Neto bonificado, colocar el Precio Lista aquí y la bonificación en discount_percentage.",
    )
    discount_percentage: float = Field(
        default=0.0,
        description="Porcentaje de descuento/bonificación positivo de esa línea (0 a 100, ej: si dice -37.00 colocar 37.0). Si solo hay descuento global en el pie, colocar 0.0 aquí.",
    )
    line_net_total: float = Field(
        default=0.0,
        description="Subtotal neto de la línea antes de IVA (quantity * unit_price * (1 - discount_percentage/100)).",
    )


class ExtractedPerceptionSchema(BaseModel):
    label: str = Field(description="Nombre o jurisdicción de la percepción/tributo (ej: 'Percep. RG 2408/08', 'Perc. IIBB Córdoba').")
    amount: float = Field(description="Monto del tributo o percepción (solo incluir si es mayor a 0).")


class ExtractedInvoiceSchema(BaseModel):
    vendor_name: str = Field(description="Razón social del EMISOR de la factura (arriba). Ignorar al cliente/receptor.")
    vendor_cuit: str = Field(description="CUIT del EMISOR con guiones XX-XXXXXXXX-X.")
    document_type: str = Field(description="FACTURA, NOTA_CREDITO o NOTA_DEBITO.")
    invoice_letter: str = Field(description="Letra fiscal: A, B, C, M o E.")
    invoice_number: str = Field(description="Punto de venta (5 dígitos) + guion + número (8 dígitos), ej: 00004-00141869.")
    invoice_date: str = Field(description="Fecha de emisión en formato YYYY-MM-DD.")
    due_date: str = Field(
        default="",
        description="Fecha de vencimiento de PAGO comercial (YYYY-MM-DD) SOLO si figura impresa en el encabezado superior. ¡PROHIBIDO usar la fecha de Vencimiento del C.A.E. del pie de página! Si no hay fecha de pago arriba, dejar vacío ''.",
    )
    payment_terms: str = Field(
        default="",
        description="Texto exacto de Condición de venta / pago (ej: '30 días Fecha Factura', 'Pago a 30 días FF').",
    )
    cae_number: str = Field(default="", description="Número de C.A.E. de 14 dígitos numéricos.")
    currency: str = Field(
        description="Código ISO de moneda real del total del comprobante: ARS, USD o EUR. Si el pie dice TOTAL PESOS aunque mencione cotización u$s informativa, es ARS."
    )
    exchange_rate: float = Field(default=1.0, description="Tipo de cambio si la factura es en USD/EUR; si currency es ARS colocar 1.0.")
    reference_exchange_rate: float = Field(
        default=0.0,
        description="Si la factura es en ARS (Pesos) pero menciona en el texto o pie una cotización de dólar de referencia (ej: 'u$s: 1525.00', 'TC BNA 1535'), coloca ese valor numérico aquí (ej: 1525.0). Si no menciona ninguno, coloca 0.0.",
    )
    purchase_order_number: str = Field(
        default="",
        description="Número(s) de Orden de Compra limpios (sin porcentajes ni texto extra; ej: si dice '94076 5%' extraer '94076'). Si hay varios, separar con ' / '.",
    )
    gross_subtotal: float = Field(default=0.0, description="Suma bruta de líneas antes de descuentos globales de pie.")
    global_discount_amount: float = Field(
        default=0.0,
        description="Monto positivo de DESCUENTOS globales restados en el pie antes del Subtotal/IVA (0.0 si no aplica).",
    )
    net_taxable_base: float = Field(default=0.0, description="Subtotal Neto Gravado sujeto a IVA luego de descuentos.")
    exempt_amount: float = Field(default=0.0, description="Importe Exento o No Gravado (0.0 si no aplica).")
    total_vat: float = Field(default=0.0, description="Suma total de IVA (21%, 10.5%, 27%) en el pie.")
    other_taxes_perceptions: float = Field(default=0.0, description="Suma total de Percepciones (IIBB, IVA, Ganancias) e Impuestos Internos, sin IVA.")
    perceptions: List[ExtractedPerceptionSchema] = Field(
        default_factory=list,
        description="Lista de percepciones con importe mayor a 0.0 detalladas en el pie.",
    )
    total_amount: float = Field(default=0.0, description="Importe Total final a pagar del comprobante.")
    lines: List[ExtractedLineSchema] = Field(description="Líneas de artículos/servicios facturados.")


SYSTEM_PROMPT = """Eres un auditor contable argentino experto en normativa AFIP/ARCA e integración con Odoo.
Extrae con precisión quirúrgica los datos de la factura de proveedor adjunta respetando estas reglas críticas:

1. EMISOR vs RECEPTOR: Extrae únicamente razón social y CUIT del EMISOR (proveedor). Ignora los datos del cliente (Argelec S.A.).
2. PROHIBIDO USAR EL VENCIMIENTO DEL CAE COMO DUE_DATE: En el pie de página junto al C.A.E. siempre dice 'Vencimiento' o 'Vto.' (~10 días después de la fecha de factura). Esa fecha es de AFIP, NO de pago. Si en el encabezado superior no hay fecha calendario de vencimiento de pago explícita, deja `due_date` vacío ("") y pon la condición en `payment_terms` (ej: "30 días Fecha Factura").
3. ORDEN DE COMPRA (`purchase_order_number`): Búscala en el encabezado ("Orden Compra", "O.C.") o dentro de las columnas de cada línea ("NRO. OC."). Limpia cualquier porcentaje o nota pegada (si dice "94076 5%", devuelve "94076"). No confundas con Nro. de Remito ni Nota de Pedido.
4. COLUMNAS APILADAS Y MÚLTIPLES CÓDIGOS (ej. Zoloda): Si una celda apila dos códigos para el mismo ítem (ej. `ICAB-01/N` arriba y `160.014` abajo), incluye ambos en `product_code` unidos por ` | ` (`"ICAB-01/N | 160.014"`). Atención cuando dos ítems están juntos o una descripción ocupa dos renglones: crea una entrada en `lines` por cada ítem facturado verificando cantidad, precio lista, bonificación y subtotal.
5. PRECIO LISTA vs PRECIO NETO BONIFICADO: Si la línea muestra Precio Lista (`17095.25`), `% Bonif` (`-37.00`) y Precio Neto (`10770.01`), pon el Precio Lista (`17095.25`) en `unit_price` y `37.0` (positivo) en `discount_percentage`.
6. DESCUENTO GLOBAL DE PIE (ej. Micro Control): Si las líneas vienen a precio bruto sin descuento individual pero en el pie se resta un renglón `DESCUENTOS` antes del Subtotal Neto Gravado, coloca ese monto en `global_discount_amount`.
7. MONEDA REAL Y TC DE REFERENCIA: Si el total final dice `TOTAL PESOS` (aunque mencione una cotización referencial `u$s: 1525.00` en notas), `currency` debe ser `"ARS"`, `exchange_rate` `1.0` y debes guardar `1525.0` en `reference_exchange_rate`. Solo usa `currency = "USD"` cuando la factura esté emitida y totalizada en dólares (en cuyo caso pon el tipo de cambio en `exchange_rate`).
8. ENVASES CERRADOS / MÚLTIPLOS (`uom_label` y `pack_size`): Presta especial atención a la columna de Unidad de Medida o a la descripción cuando indique bultos cerrados como `Caja x 25 unid.`, `Caja x 50 unid.`, `Paq x 20 mts.`, `Bolsa x 100`, `Rollo x 100m`. Copia ese texto en `uom_label` y coloca el factor multiplicador numérico (`25.0`, `50.0`, `20.0`, `100.0`) en `pack_size`. Si es unitario normal, usa `1.0`.
"""
CSV_EXPORT_HEADERS = [
    "vendor_name", "vendor_cuit", "document_type", "invoice_letter", "invoice_number",
    "invoice_date", "due_date", "payment_terms", "cae_number", "currency",
    "exchange_rate", "purchase_order_number", "product_code", "description",
    "quantity", "unit_price", "discount_percentage", "line_net_total",
    "gross_subtotal", "global_discount_amount", "net_taxable_base", "exempt_amount",
    "total_vat", "other_taxes_perceptions", "perceptions_detail", "total_amount",
]


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


def _format_cuit(raw_cuit: str) -> str:
    digits = re.sub(r"\D", "", raw_cuit or "")
    if len(digits) == 11:
        return f"{digits[:2]}-{digits[2:10]}-{digits[10:]}"
    return (raw_cuit or "").strip()


def _sanitize_and_compute_due_date(invoice_date_str: str, due_date_str: str, payment_terms: str) -> str:
    inv_dt = None
    if invoice_date_str:
        try:
            inv_dt = datetime.strptime(invoice_date_str, "%Y-%m-%d")
        except ValueError:
            pass

    term_days = None
    if payment_terms and not re.search(r"\d+\s*[/,-]\s*\d+", payment_terms):
        # Tomar el primer plazo en días mencionado (ej. '30 días Fecha Factura' -> 30)
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


def _build_flat_csv(header: dict, lines: list) -> str:
    """Genera un CSV válido RFC-4180 a partir de los datos limpios para inspección o descarga."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_EXPORT_HEADERS, quoting=csv.QUOTE_MINIMAL)
    writer.writeheader()
    for ln in lines:
        writer.writerow({
            "vendor_name": header["vendor_name"],
            "vendor_cuit": header["vendor_cuit"],
            "document_type": header["document_type"],
            "invoice_letter": header["invoice_letter"],
            "invoice_number": header["invoice_number"],
            "invoice_date": header["invoice_date"],
            "due_date": header["due_date"],
            "payment_terms": header["payment_terms"],
            "cae_number": header["cae_number"],
            "currency": header["currency"],
            "exchange_rate": header["exchange_rate"],
            "purchase_order_number": header["purchase_order_number"],
            "product_code": ln["product_code"],
            "description": ln["description"],
            "quantity": ln["quantity"],
            "unit_price": ln["unit_price"],
            "discount_percentage": ln["discount_percentage"],
            "line_net_total": ln["line_net_total"],
            "gross_subtotal": header["gross_subtotal"],
            "global_discount_amount": header["global_discount_amount"],
            "net_taxable_base": header["net_taxable_base"],
            "exempt_amount": header["exempt_amount"],
            "total_vat": header["total_vat"],
            "other_taxes_perceptions": header["other_taxes_perceptions"],
            "perceptions_detail": header["perceptions_detail"],
            "total_amount": header["total_amount"],
        })
    return buf.getvalue()

def _detect_pack_size(uom_label: str, description: str, gemini_pack: float) -> float:
    if gemini_pack and gemini_pack > 1.0:
        return float(gemini_pack)
    combined = f"{uom_label or ''} {description or ''}"
    # Detecta 'Caja x 25 unid', 'Paq x 20 mts', 'Pack x 10', 'Rollo x 100 m', 'Bolsas x 50'
    m = re.search(
        r"\b(?:caja|paq(?:uete)?|pack|bolsa|rollo|bulto|envase)s?\s*(?:de\s*|x\s*|\*\s*)(\d+(?:[.,]\d+)?)\b",
        combined,
        flags=re.IGNORECASE,
    )
    if m:
        val = float(m.group(1).replace(",", "."))
        if val > 1.0:
            return val
    return 1.0


def calibrate_net_unit_prices(lines: list, target_net_base: float, price_decimals: int = 4) -> list:
    """
    Absorbe cualquier descuento de línea o global dentro de `unit_price` (dejando discount_percentage = 0.0)
    y calibra los decimales para que sum(round(qty * unit_price, 2)) coincida exactamente con target_net_base.
    """
    if not lines:
        return lines

    base_subs = []
    for ln in lines:
        q = float(ln.get("quantity") or 1.0)
        p = float(ln.get("unit_price") or 0.0)
        d = float(ln.get("discount_percentage") or 0.0)
        printed_sub = float(ln.get("line_net_total") or 0.0)
        calc_sub = round(q * p * (1.0 - d / 100.0), 2)
        # Respetar el subtotal impreso de la línea (ej. Zoloda 21540.02 en vez de 21540.01) si coincide dentro de $0.05
        sub = printed_sub if (printed_sub > 0 and abs(printed_sub - calc_sub) <= 0.08) else calc_sub
        base_subs.append(sub)

    sum_base = round(sum(base_subs), 2)
    factor = (target_net_base / sum_base) if (target_net_base > 0 and sum_base > 0) else 1.0

    out = []
    for i, ln in enumerate(lines):
        q = float(ln.get("quantity") or 1.0)
        desired_sub = round(base_subs[i] * factor, 2)
        u_net = round(desired_sub / q, price_decimals) if q > 0 else 0.0
        actual_sub = round(q * u_net, 2)
        out.append({
            **ln,
            "orig_list_price": ln.get("orig_list_price", float(ln.get("unit_price") or 0.0)),
            "orig_discount_pct": ln.get("orig_discount_pct", float(ln.get("discount_percentage") or 0.0)),
            "unit_price": u_net,
            "discount_percentage": 0.0,
            "line_net_total": actual_sub,
        })

    if target_net_base <= 0:
        return out

    # Ajuste fino del último decimal en una línea cuya cantidad permita absorber centavos residuales
    unit_step = 10 ** (-price_decimals)
    for _ in range(200):
        curr_sum = round(sum(round(float(x["quantity"]) * float(x["unit_price"]), 2) for x in out), 2)
        diff = round(target_net_base - curr_sum, 2)
        if abs(diff) < 0.005:
            break
        direction = 1 if diff > 0 else -1
        best_choice = None
        best_err = abs(diff)
        for idx, x in enumerate(out):
            q = float(x["quantity"] or 0.0)
            if q <= 0:
                continue
            cand_u = round(float(x["unit_price"]) + direction * unit_step, price_decimals)
            if cand_u < 0:
                continue
            new_sub = round(q * cand_u, 2)
            delta_sub = round(new_sub - x["line_net_total"], 2)
            if delta_sub == 0:
                continue
            new_err = abs(round(diff - delta_sub, 2))
            if new_err < best_err:
                best_err = new_err
                best_choice = (idx, cand_u, new_sub)
                if new_err == 0:
                    break
        if best_choice is None:
            break
        b_idx, b_u, b_sub = best_choice
        out[b_idx]["unit_price"] = b_u
        out[b_idx]["line_net_total"] = b_sub

    return out

def extract_invoice_with_gemini(file_bytes: bytes, mime_type: str) -> dict:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Falta configurar GEMINI_API_KEY en las variables de entorno.")

    primary_model = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()
    thinking_lvl = os.getenv("GEMINI_THINKING_LEVEL", "LOW").strip().upper()

    fallback_candidates = [
        primary_model,
        "gemini-3.5-flash",
        "gemini-3.8-flash",
    ]
    models_to_try = list(dict.fromkeys(m for m in fallback_candidates if m))

    client = genai.Client(api_key=api_key)
    contents = [
        types.Content(
            role="user",
            parts=[
                types.Part(inline_data=types.Blob(mime_type=mime_type, data=file_bytes)),
                types.Part.from_text(
                    text="Extrae todos los datos fiscales, líneas e impuestos de esta factura de proveedor en el esquema JSON solicitado."
                ),
            ],
        )
    ]

    response = None
    used_model = primary_model
    last_error = None

    for model_name in models_to_try:
        cfg_kwargs = {
            "system_instruction": SYSTEM_PROMPT,
            "temperature": 0.0,
            "response_mime_type": "application/json",
            "response_schema": ExtractedInvoiceSchema,
        }
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
                if any(c in err_str for c in ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "OVERLOADED", "500")):
                    time.sleep(1.5 * (attempt + 1))
                    continue
                if any(c in err_str for c in ("404", "NOT_FOUND", "INVALID_ARGUMENT")):
                    break
                raise

        if response is not None:
            break

    if response is None:
        raise RuntimeError(f"No se obtuvo respuesta de Gemini. Último error: {type(last_error).__name__}: {last_error}")

    # Parsear JSON garantizado por el esquema
    raw_json_text = (response.text or "").strip()
    if not raw_json_text:
        raise ValueError("Gemini devolvió una respuesta vacía.")

    data = json.loads(raw_json_text)

    # Limpiar referencias de OC
    po_candidates = []
    raw_po = (data.get("purchase_order_number") or "").strip()
    if raw_po:
        for tok in re.split(r"[/,;]+", raw_po):
            clean_tok = re.sub(r"\s+\d+(?:[.,]\d+)?\s*%.*$", "", tok.strip()).strip()
            if clean_tok and clean_tok not in po_candidates:
                po_candidates.append(clean_tok)

    inv_date_clean = (data.get("invoice_date") or "").strip()
    raw_due_clean = (data.get("due_date") or "").strip()
    pay_terms_clean = (data.get("payment_terms") or "").strip()

    parsed_perceptions = [
        {"label": (p.get("label") or "").strip(), "amount": round(float(p.get("amount") or 0.0), 2)}
        for p in (data.get("perceptions") or [])
        if float(p.get("amount") or 0.0) > 0
    ]
    perc_detail_str = " | ".join(f"{p['label']}: {p['amount']:.2f}" for p in parsed_perceptions)

    doc_type_raw = (data.get("document_type") or "FACTURA").strip().upper()
    if "CRED" in doc_type_raw or "CRÉD" in doc_type_raw:
        doc_type_norm = "NOTA_CREDITO"
    elif "DEB" in doc_type_raw or "DÉB" in doc_type_raw:
        doc_type_norm = "NOTA_DEBITO"
    else:
        doc_type_norm = "FACTURA"

    header = {
        "vendor_name": (data.get("vendor_name") or "").strip(),
        "vendor_cuit": _format_cuit(data.get("vendor_cuit") or ""),
        "document_type": doc_type_norm,
        "invoice_letter": (data.get("invoice_letter") or "A").strip().upper(),
        "invoice_number": _normalize_ar_doc_number(data.get("invoice_number") or ""),
        "invoice_date": inv_date_clean,
        "payment_terms": pay_terms_clean,
        "due_date": _sanitize_and_compute_due_date(inv_date_clean, raw_due_clean, pay_terms_clean),
        "cae_number": re.sub(r"\D", "", data.get("cae_number") or ""),
        "currency": (data.get("currency") or "ARS").strip().upper(),
        "exchange_rate": float(data.get("exchange_rate") or 1.0),
        "reference_exchange_rate": float(data.get("reference_exchange_rate") or 0.0),
        "purchase_order_number": " / ".join(po_candidates),
        "po_references": po_candidates,
        "gross_subtotal": float(data.get("gross_subtotal") or 0.0),
        "global_discount_amount": abs(float(data.get("global_discount_amount") or 0.0)),
        "net_taxable_base": float(data.get("net_taxable_base") or 0.0),
        "exempt_amount": float(data.get("exempt_amount") or 0.0),
        "total_vat": float(data.get("total_vat") or 0.0),
        "other_taxes_perceptions": float(data.get("other_taxes_perceptions") or 0.0),
        "perceptions_detail": perc_detail_str,
        "parsed_perceptions": parsed_perceptions,
        "total_amount": float(data.get("total_amount") or 0.0),
    }

    lines = []
    for idx, r in enumerate(data.get("lines") or []):
        qty = float(r.get("quantity") or 1.0)
        price = float(r.get("unit_price") or 0.0)
        disc = round(abs(float(r.get("discount_percentage") or 0.0)), 2)
        net = float(r.get("line_net_total") or round(qty * price * (1.0 - disc / 100.0), 2))

        # Protección anti doble-descuento si Gemini ya puso el neto en unit_price
        if disc > 0 and qty > 0 and net > 0:
            if abs((qty * price) - net) <= 1.0 and abs((qty * price * (1.0 - disc / 100.0)) - net) > 5.0:
                disc = 0.0

        raw_pcode = (r.get("product_code") or "").strip()
        code_list = [c.strip() for c in re.split(r"\s*[|;]\s*|\s{2,}", raw_pcode) if c.strip()]
        if len(code_list) == 1 and " / " in raw_pcode:
            code_list = [c.strip() for c in raw_pcode.split(" / ") if c.strip()]

        uom_lbl = (r.get("uom_label") or "").strip()
        desc_clean = (r.get("description") or "").strip()
        pack_mult = _detect_pack_size(uom_lbl, desc_clean, float(r.get("pack_size") or 1.0))

        lines.append({
            "index": idx,
            "product_code": raw_pcode,
            "product_codes": code_list,
            "description": desc_clean,
            "uom_label": uom_lbl,
            "billed_qty": qty,
            "billed_unit_price": price,
            "pack_size": pack_mult,
            "pack_applied": False,
            "quantity": qty,
            "unit_price": price,
            "discount_percentage": disc,
            "line_net_total": round(qty * price * (1.0 - disc / 100.0), 2),
        })

    # Si hay descuento global en el pie (ej. Micro Control) y las líneas vienen en 0%, asignar el % de dto a las líneas
    global_disc = header["global_discount_amount"]
    sum_lines_gross = sum(l["billed_qty"] * l["billed_unit_price"] for l in lines)
    if global_disc > 0 and sum_lines_gross > global_disc and all(l["discount_percentage"] == 0.0 for l in lines):
        disc_2dec = round((global_disc / sum_lines_gross) * 100.0, 2)
        for l in lines:
            l["discount_percentage"] = disc_2dec
            l["line_net_total"] = round(l["quantity"] * l["unit_price"] * (1.0 - disc_2dec / 100.0), 2)

    raw_csv = _build_flat_csv(header, lines)

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