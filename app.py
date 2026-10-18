import io
import json
import os
import re
import sqlite3
import uuid
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Optional

import requests
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pypdf import PdfReader
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from PIL import Image

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("HISABAI_DATA_DIR", APP_DIR / "data")).resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "hisabai.sqlite3"
DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="HisabAI", description="HisabAI - Local Business Assistant")
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  business_name TEXT NOT NULL DEFAULT '',
  gstin TEXT NOT NULL DEFAULT '',
  address TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT '',
  email TEXT NOT NULL DEFAULT '',
  phone TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS clients (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  gstin TEXT NOT NULL DEFAULT '',
  address TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT '',
  email TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS invoices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  invoice_number TEXT NOT NULL UNIQUE,
  invoice_date TEXT NOT NULL,
  due_date TEXT NOT NULL DEFAULT '',
  client_id INTEGER,
  client_name TEXT NOT NULL,
  client_gstin TEXT NOT NULL DEFAULT '',
  client_address TEXT NOT NULL DEFAULT '',
  client_state TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL,
  taxable_value TEXT NOT NULL,
  gst_rate TEXT NOT NULL,
  tax_mode TEXT NOT NULL,
  cgst TEXT NOT NULL,
  sgst TEXT NOT NULL,
  igst TEXT NOT NULL,
  total TEXT NOT NULL,
  notes TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'Unpaid',
  created_at TEXT NOT NULL,
  FOREIGN KEY(client_id) REFERENCES clients(id)
);
CREATE TABLE IF NOT EXISTS expenses (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_filename TEXT NOT NULL,
  vendor TEXT NOT NULL DEFAULT '',
  invoice_number TEXT NOT NULL DEFAULT '',
  invoice_date TEXT NOT NULL DEFAULT '',
  gstin TEXT NOT NULL DEFAULT '',
  taxable_value TEXT NOT NULL DEFAULT '0.00',
  cgst TEXT NOT NULL DEFAULT '0.00',
  sgst TEXT NOT NULL DEFAULT '0.00',
  igst TEXT NOT NULL DEFAULT '0.00',
  total TEXT NOT NULL DEFAULT '0.00',
  category TEXT NOT NULL DEFAULT 'Uncategorized',
  raw_text TEXT NOT NULL DEFAULT '',
  ai_used INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
"""

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    with db() as conn:
        conn.executescript(SCHEMA)
        conn.execute("INSERT OR IGNORE INTO settings (id) VALUES (1)")

init_db()

def money(value) -> Decimal:
    try:
        d = Decimal(str(value or "0").replace(",", "").replace("₹", "").strip())
    except (InvalidOperation, ValueError):
        raise ValueError("Enter a valid amount.")
    if not d.is_finite() or d < 0:
        raise ValueError("Amounts must be finite and non-negative.")
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

def fmt(value) -> str:
    return f"{money(value):,.2f}"

def tax_calc(taxable, rate, mode):
    base, rate_d = money(taxable), Decimal(str(rate))
    if rate_d < 0 or rate_d > 100:
        raise ValueError("GST rate must be between 0 and 100.")
    tax = (base * rate_d / Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    cgst = sgst = igst = Decimal("0.00")
    if mode == "intra":
        cgst = (tax / Decimal("2")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        sgst = tax - cgst
    elif mode == "inter":
        igst = tax
    elif mode == "none":
        tax = Decimal("0.00")
    else:
        raise ValueError("Choose intra-state, inter-state, or no GST.")
    total = base + cgst + sgst + igst
    return tuple(fmt(x) for x in (cgst, sgst, igst, total))

def settings_row():
    with db() as conn:
        return conn.execute("SELECT * FROM settings WHERE id=1").fetchone()

def extract_text(filename: str, content: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        reader = PdfReader(io.BytesIO(content))
        return "\n".join(page.extract_text() or "" for page in reader.pages)[:40000]
    if suffix in {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}:
        try:
            import pytesseract
            return pytesseract.image_to_string(Image.open(io.BytesIO(content)))[:40000]
        except Exception as exc:
            raise ValueError("Image OCR couldn't run. Install Tesseract OCR and ensure it is on PATH, then try again. Details: " + str(exc))
    if suffix in {".txt", ".csv"}:
        return content.decode("utf-8", errors="replace")[:40000]
    raise ValueError("Supported file types: PDF, PNG, JPG, TIFF, WEBP, TXT, CSV.")

def heuristic_extract(text: str, filename: str) -> dict:
    # Conservative fallback: parse common labels and totals, but leave uncertain fields blank.
    def first(patterns, default=""):
        for pat in patterns:
            m = re.search(pat, text, flags=re.I | re.M)
            if m:
                return m.group(1).strip()[:200]
        return default
    gstins = re.findall(r"\b\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]\b", text.upper())
    amounts = []
    for m in re.finditer(r"(?:₹|Rs\.?|INR)?\s*([0-9][0-9,]*(?:\.\d{1,2})?)", text, flags=re.I):
        try:
            amounts.append(money(m.group(1)))
        except ValueError:
            pass
    total = ""
    for pat in [
        r"(?:grand\s+total|invoice\s+total|total\s+amount|amount\s+payable|net\s+payable)\s*[:\-]?\s*(?:₹|Rs\.?|INR)?\s*([0-9,]+(?:\.\d{1,2})?)",
    ]:
        m = re.search(pat, text, flags=re.I)
        if m:
            total = fmt(m.group(1)); break
    if not total and amounts:
        total = fmt(max(amounts))
    date_value = first([
        r"(?:invoice\s+date|date)\s*[:\-]?\s*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
        r"\b(\d{4}-\d{2}-\d{2})\b"
    ])
    inv_num = first([r"(?:invoice\s*(?:no\.?|number|#))\s*[:\-]?\s*([A-Z0-9/-]+)"])
    vendor = first([r"^\s*([A-Za-z][A-Za-z0-9 &.,'-]{2,})\s*$"])
    taxable = first([r"(?:taxable\s+value|taxable\s+amount|sub\s*total|subtotal)\s*[:\-]?\s*(?:₹|Rs\.?|INR)?\s*([0-9,]+(?:\.\d{1,2})?)"])
    cgst = first([r"CGST(?:\s*@\s*\d+(?:\.\d+)?%)?\s*[:\-]?\s*(?:₹|Rs\.?|INR)?\s*([0-9,]+(?:\.\d{1,2})?)"], "0.00")
    sgst = first([r"SGST(?:\s*@\s*\d+(?:\.\d+)?%)?\s*[:\-]?\s*(?:₹|Rs\.?|INR)?\s*([0-9,]+(?:\.\d{1,2})?)"], "0.00")
    igst = first([r"IGST(?:\s*@\s*\d+(?:\.\d+)?%)?\s*[:\-]?\s*(?:₹|Rs\.?|INR)?\s*([0-9,]+(?:\.\d{1,2})?)"], "0.00")
    return {
        "vendor": vendor if vendor and vendor.lower() not in {"invoice", "tax invoice"} else "",
        "invoice_number": inv_num,
        "invoice_date": date_value,
        "gstin": gstins[0] if gstins else "",
        "taxable_value": fmt(taxable) if taxable else "",
        "cgst": fmt(cgst), "sgst": fmt(sgst), "igst": fmt(igst),
        "total": total,
        "category": "Uncategorized",
        "needs_review": True,
        "method": "Local text heuristics (no model)"
    }

def ollama_extract(text: str) -> Optional[dict]:
    if os.getenv("HISABAI_DISABLE_OLLAMA", "").lower() in {"1", "true", "yes"}:
        return None
    model = os.getenv("HISABAI_MODEL", "gemma3:4b")
    schema = {
        "type": "object",
        "properties": {
            "vendor": {"type": "string"},
            "invoice_number": {"type": "string"},
            "invoice_date": {"type": "string"},
            "gstin": {"type": "string"},
            "taxable_value": {"type": "string"},
            "cgst": {"type": "string"},
            "sgst": {"type": "string"},
            "igst": {"type": "string"},
            "total": {"type": "string"},
            "category": {"type": "string"}
        },
        "required": ["vendor", "invoice_number", "invoice_date", "gstin", "taxable_value", "cgst", "sgst", "igst", "total", "category"]
    }
    prompt = (
        "Extract fields from this Indian supplier invoice or receipt. Return only values clearly supported by the text. "
        "Use empty strings for missing or uncertain values. Amounts must be numeric strings with 2 decimals and no currency symbol. "
        "Do not calculate or invent GSTIN, dates, invoice numbers, or amounts. Category should be a short label such as Software, Travel, Office supplies, Meals, or Uncategorized. "
        "For dates, preserve the source format. Text follows:\n\n" + text[:16000]
    )
    try:
        response = requests.post(
            "http://127.0.0.1:11434/api/generate",
            json={"model": model, "prompt": prompt, "stream": False, "format": schema, "options": {"temperature": 0}},
            timeout=90
        )
        response.raise_for_status()
        result = json.loads(response.json().get("response", "{}"))
        result["needs_review"] = True
        result["method"] = f"Local Ollama model: {model}"
        return result
    except Exception:
        return None

def parse_date_to_iso(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    for fmt_in in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d-%m-%y", "%m/%d/%Y"):
        try:
            return datetime.strptime(value, fmt_in).date().isoformat()
        except ValueError:
            pass
    return value[:30]

def render_page(request: Request, template: str, **context):
    return templates.TemplateResponse(request=request, name=template, context=context)

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    with db() as conn:
        invoices = conn.execute("SELECT * FROM invoices ORDER BY id DESC LIMIT 8").fetchall()
        expenses = conn.execute("SELECT * FROM expenses ORDER BY id DESC LIMIT 5").fetchall()
        invoice_count = conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
        unpaid = conn.execute("SELECT COALESCE(SUM(CAST(total AS REAL)),0) FROM invoices WHERE status != 'Paid'").fetchone()[0]
        expense_total = conn.execute("SELECT COALESCE(SUM(CAST(total AS REAL)),0) FROM expenses").fetchone()[0]
        client_count = conn.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
    return render_page(request, "dashboard.html", title="Dashboard", invoices=invoices, expenses=expenses,
                       invoice_count=invoice_count, unpaid=fmt(unpaid), expense_total=fmt(expense_total), client_count=client_count)

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, saved: int = 0):
    return render_page(request, "settings.html", title="Business settings", settings=settings_row(), saved=saved)

@app.post("/settings")
def save_settings(
    business_name: str = Form(""), gstin: str = Form(""), address: str = Form(""),
    state: str = Form(""), email: str = Form(""), phone: str = Form("")
):
    with db() as conn:
        conn.execute("""UPDATE settings SET business_name=?,gstin=?,address=?,state=?,email=?,phone=? WHERE id=1""",
                     (business_name.strip(), gstin.strip().upper(), address.strip(), state.strip(), email.strip(), phone.strip()))
    return RedirectResponse("/settings?saved=1", status_code=303)

@app.get("/clients", response_class=HTMLResponse)
def clients_page(request: Request):
    with db() as conn:
        clients = conn.execute("SELECT * FROM clients ORDER BY name").fetchall()
    return render_page(request, "clients.html", title="Clients", clients=clients)

@app.post("/clients")
def add_client(name: str = Form(...), gstin: str = Form(""), address: str = Form(""), state: str = Form(""), email: str = Form("")):
    if not name.strip():
        raise HTTPException(400, "Client name is required.")
    with db() as conn:
        conn.execute("INSERT INTO clients(name,gstin,address,state,email) VALUES(?,?,?,?,?)",
                     (name.strip(), gstin.strip().upper(), address.strip(), state.strip(), email.strip()))
    return RedirectResponse("/clients", status_code=303)

@app.get("/invoices/new", response_class=HTMLResponse)
def new_invoice(request: Request, error: str = ""):
    with db() as conn:
        clients = conn.execute("SELECT * FROM clients ORDER BY name").fetchall()
    return render_page(request, "invoice_new.html", title="Create invoice", clients=clients, error=error, today=date.today().isoformat())

@app.post("/invoices/new")
def create_invoice(
    invoice_number: str = Form(...), invoice_date: str = Form(...), due_date: str = Form(""),
    client_id: str = Form(""), client_name: str = Form(""), client_gstin: str = Form(""),
    client_address: str = Form(""), client_state: str = Form(""), description: str = Form(...),
    taxable_value: str = Form(...), gst_rate: str = Form("18"), tax_mode: str = Form("intra"),
    notes: str = Form("")
):
    try:
        base = money(taxable_value)
        rate = Decimal(gst_rate)
        cgst, sgst, igst, total = tax_calc(base, rate, tax_mode)
    except (ValueError, InvalidOperation) as exc:
        return RedirectResponse("/invoices/new?error=" + str(exc).replace(" ", "%20"), status_code=303)
    cid = int(client_id) if client_id.isdigit() else None
    if cid:
        with db() as conn:
            c = conn.execute("SELECT * FROM clients WHERE id=?", (cid,)).fetchone()
        if c:
            client_name, client_gstin, client_address, client_state = c["name"], c["gstin"], c["address"], c["state"]
    if not client_name.strip() or not description.strip():
        return RedirectResponse("/invoices/new?error=Client%20and%20description%20are%20required", status_code=303)
    try:
        with db() as conn:
            conn.execute("""INSERT INTO invoices(invoice_number,invoice_date,due_date,client_id,client_name,client_gstin,client_address,client_state,description,taxable_value,gst_rate,tax_mode,cgst,sgst,igst,total,notes,status,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (invoice_number.strip(), invoice_date, due_date, cid, client_name.strip(), client_gstin.strip().upper(),
             client_address.strip(), client_state.strip(), description.strip(), fmt(base), str(rate), tax_mode,
             cgst, sgst, igst, total, notes.strip(), "Unpaid", datetime.now().isoformat(timespec="seconds")))
            inv_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    except sqlite3.IntegrityError:
        return RedirectResponse("/invoices/new?error=Invoice%20number%20already%20exists", status_code=303)
    return RedirectResponse(f"/invoices/{inv_id}", status_code=303)

@app.get("/invoices/{invoice_id}", response_class=HTMLResponse)
def invoice_detail(request: Request, invoice_id: int):
    with db() as conn:
        invoice = conn.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if not invoice:
        raise HTTPException(404, "Invoice not found.")
    return render_page(request, "invoice_detail.html", title=invoice["invoice_number"], invoice=invoice, settings=settings_row())

@app.post("/invoices/{invoice_id}/status")
def update_status(invoice_id: int, status: str = Form(...)):
    if status not in {"Unpaid", "Paid", "Overdue"}:
        raise HTTPException(400, "Invalid status")
    with db() as conn:
        conn.execute("UPDATE invoices SET status=? WHERE id=?", (status, invoice_id))
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)

@app.get("/invoices/{invoice_id}/pdf")
def invoice_pdf(invoice_id: int):
    with db() as conn:
        inv = conn.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
    if not inv:
        raise HTTPException(404, "Invoice not found.")
    settings = settings_row()
    buff = io.BytesIO()
    doc = SimpleDocTemplate(buff, pagesize=A4, rightMargin=42, leftMargin=42, topMargin=42, bottomMargin=42)
    styles = getSampleStyleSheet()
    story = [Paragraph("TAX INVOICE", styles["Title"]), Spacer(1, 12)]
    business = "<br/>".join([str(settings["business_name"] or "Your Business"), str(settings["address"] or "").replace("&","&amp;").replace("<","&lt;").replace(">","&gt;"), f"GSTIN: {settings['gstin'] or 'Not provided'}", f"State: {settings['state'] or 'Not provided'}"])
    story.append(Paragraph(business, styles["Normal"]))
    story.append(Spacer(1, 16))
    recipient = "<br/>".join([f"<b>Bill To:</b> {inv['client_name']}", f"GSTIN: {inv['client_gstin'] or 'Not provided'}", inv["client_address"] or "", f"State: {inv['client_state'] or 'Not provided'}"])
    info = [[Paragraph(recipient, styles["Normal"]), Paragraph(f"<b>Invoice:</b> {inv['invoice_number']}<br/><b>Date:</b> {inv['invoice_date']}<br/><b>Due:</b> {inv['due_date'] or '—'}", styles["Normal"])]]
    tbl = Table(info, colWidths=[270, 200])
    tbl.setStyle(TableStyle([("VALIGN",(0,0),(-1,-1),"TOP"),("BOX",(0,0),(-1,-1),0.5,colors.grey),("PADDING",(0,0),(-1,-1),9)]))
    story.extend([tbl, Spacer(1, 18)])
    rows = [["Description", "Taxable value (INR)", "GST rate", "Tax (INR)", "Total (INR)"]]
    tax = money(inv["cgst"]) + money(inv["sgst"]) + money(inv["igst"])
    rows.append([inv["description"], fmt(inv["taxable_value"]), f"{inv['gst_rate']}%", fmt(tax), fmt(inv["total"])])
    items = Table(rows, colWidths=[180, 95, 55, 70, 75], repeatRows=1)
    items.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.HexColor("#172554")),("TEXTCOLOR",(0,0),(-1,0),colors.white),("GRID",(0,0),(-1,-1),0.4,colors.grey),("VALIGN",(0,0),(-1,-1),"TOP"),("PADDING",(0,0),(-1,-1),7)]))
    story.append(items)
    story.append(Spacer(1, 12))
    tax_rows = [["Tax breakdown", "Amount (INR)"], ["CGST", fmt(inv["cgst"])], ["SGST", fmt(inv["sgst"])], ["IGST", fmt(inv["igst"])], ["Grand total", fmt(inv["total"])]]
    tax_table = Table(tax_rows, colWidths=[320, 150], hAlign="RIGHT")
    tax_table.setStyle(TableStyle([("GRID",(0,0),(-1,-1),0.4,colors.grey),("BACKGROUND",(0,0),(-1,0),colors.HexColor("#e2e8f0")),("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),("FONTNAME",(0,-1),(-1,-1),"Helvetica-Bold"),("PADDING",(0,0),(-1,-1),6)]))
    story.append(tax_table)
    if inv["notes"]:
        story += [Spacer(1, 14), Paragraph("<b>Notes:</b> " + inv["notes"].replace("&","&amp;").replace("<","&lt;").replace(">","&gt;"), styles["Normal"])]
    story += [Spacer(1, 24), Paragraph("Generated by HisabAI. Verify tax treatment and statutory requirements before issuing.", styles["Italic"])]
    doc.build(story)
    return Response(buff.getvalue(), media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{inv["invoice_number"]}.pdf"'})

@app.get("/expenses", response_class=HTMLResponse)
def expenses_page(request: Request):
    with db() as conn:
        expenses = conn.execute("SELECT * FROM expenses ORDER BY id DESC").fetchall()
    return render_page(request, "expenses.html", title="Expenses", expenses=expenses)

@app.get("/expenses/import", response_class=HTMLResponse)
def import_page(request: Request, error: str = ""):
    return render_page(request, "expense_import.html", title="Import receipt", error=error)

@app.post("/expenses/import")
async def import_expense(request: Request, file: UploadFile = File(...)):
    filename = Path(file.filename or "upload").name
    content = await file.read()
    if len(content) > 15 * 1024 * 1024:
        return RedirectResponse("/expenses/import?error=File%20is%20larger%20than%2015MB", status_code=303)
    try:
        text = extract_text(filename, content)
        if not text.strip():
            return render_page(request, "expense_review.html", title="Review extraction", data=heuristic_extract("", filename), filename=filename, raw_text="", warning="No text was extracted. For scanned PDFs, OCR support is not available for this file.")
    except ValueError as exc:
        return RedirectResponse("/expenses/import?error=" + str(exc).replace(" ", "%20"), status_code=303)
    result = ollama_extract(text)
    ai_used = bool(result)
    if not result:
        result = heuristic_extract(text, filename)
    result["invoice_date"] = parse_date_to_iso(result.get("invoice_date", ""))
    result["ai_used"] = ai_used
    # Keep upload content local. The file is stored for future review; filename is sanitized.
    stored_name = f"{uuid.uuid4().hex}_{filename}"
    (UPLOAD_DIR / stored_name).write_bytes(content)
    return render_page(request, "expense_review.html", title="Review extraction", data=result, filename=stored_name, raw_text=text[:10000],
                       warning="" if ai_used else "Ollama was unavailable or disabled. Local heuristic extraction was used; please verify every field.")

@app.post("/expenses/save")
def save_expense(
    source_filename: str = Form(...), vendor: str = Form(""), invoice_number: str = Form(""),
    invoice_date: str = Form(""), gstin: str = Form(""), taxable_value: str = Form("0"),
    cgst: str = Form("0"), sgst: str = Form("0"), igst: str = Form("0"), total: str = Form("0"),
    category: str = Form("Uncategorized"), raw_text: str = Form(""), ai_used: int = Form(0)
):
    try:
        taxable_value, cgst, sgst, igst, total = [fmt(x) for x in (taxable_value, cgst, sgst, igst, total)]
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    with db() as conn:
        conn.execute("""INSERT INTO expenses(source_filename,vendor,invoice_number,invoice_date,gstin,taxable_value,cgst,sgst,igst,total,category,raw_text,ai_used,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (Path(source_filename).name, vendor.strip(), invoice_number.strip(), invoice_date.strip(), gstin.strip().upper(),
         taxable_value,cgst,sgst,igst,total,category.strip() or "Uncategorized",raw_text[:10000],int(bool(ai_used)),datetime.now().isoformat(timespec="seconds")))
    return RedirectResponse("/expenses", status_code=303)

@app.get("/export/expenses.csv")
def export_expenses():
    import csv
    with db() as conn:
        rows = conn.execute("SELECT vendor,invoice_number,invoice_date,gstin,taxable_value,cgst,sgst,igst,total,category,source_filename FROM expenses ORDER BY id").fetchall()
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Vendor","Invoice Number","Invoice Date","GSTIN","Taxable Value INR","CGST INR","SGST INR","IGST INR","Total INR","Category","Source File"])
    for row in rows:
        writer.writerow(list(row))
    return Response("\ufeff" + out.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="hisabai-expenses.csv"'})

@app.get("/health")
def health():
    ollama_ok = False
    model = os.getenv("HISABAI_MODEL", "gemma3:4b")
    try:
        r = requests.get("http://127.0.0.1:11434/api/tags", timeout=1.5)
        ollama_ok = r.ok
    except Exception:
        pass
    return {"status": "ok", "database": str(DB_PATH), "ollama_reachable": ollama_ok, "model": model,
            "offline_capable": True, "note": "The app does not send records to a remote API."}