import io, re
import streamlit as st
import pandas as pd

import sys
st.caption(f"Runtime Python: {sys.version}")


# PDF parsing
try:
    import pdfplumber
    PDF_OK = True
except Exception:
    PDF_OK = False

st.set_page_config(page_title="Freight & Duties Calculator", layout="wide")

# ---------- Helpers ----------
def _clean_money_text(t: str) -> str:
    import re as _re
    return _re.sub(r"[\$,]", "", t or "")

def _sum_matches(pattern, text):
    import re as _re
    total = 0.0
    for m in _re.finditer(pattern, text, flags=_re.IGNORECASE | _re.DOTALL):
        try:
            total += float(m.group(1))
        except Exception:
            pass
    return round(total, 2)

def extract_totals_from_text(raw_text: str):
    """
    Heuristics:
      - Tariff: amounts near 9903.01.25, or a 10% duty clue.
      - Import Fee: non-301 duty, MPF, HMF, entry/customs fees.
      - Shipping: origin inland, ocean freight, trucking/delivery (CA/NJ), drayage, chassis, pier pass, demurrage.
    """
    t = _clean_money_text(raw_text)

    # Tariff (Section 301)
    tariff = _sum_matches(r"9903\.?01\.?25.*?(\d+\.\d{2})", t)
    if tariff == 0:
        tariff = _sum_matches(r"(?:10\%|\b10\s*percent\b).*?(?:duty|tariff).*?(\d+\.\d{2})", t)

    # Import fees (non-301 duty & fees)
    import_fee = 0.0
    for rx in [
        r"\bDuty\b(?!.*301).*?(\d+\.\d{2})",
        r"\bMPF\b.*?(\d+\.\d{2})",
        r"\bHMF\b.*?(\d+\.\d{2})",
        r"\bMerchandise\s+Processing\b.*?(\d+\.\d{2})",
        r"\bHarbor\s+Maintenance\b.*?(\d+\.\d{2})",
        r"\bEntry\s+Fee\b.*?(\d+\.\d{2})",
        r"\bCustoms\s+Fee\b.*?(\d+\.\d{2})",
    ]:
        import_fee += _sum_matches(rx, t)
    import_fee = round(import_fee, 2)

    # Shipping costs
    shipping = 0.0
    for rx in [
        r"Origin\s+Inland.*?(\d+\.\d{2})",
        r"Ocean\s+Freight.*?(\d+\.\d{2})",
        r"Delivery.*?(?:CA|NJ|CA\s*&\s*NJ).*?(\d+\.\d{2})",
        r"Truck(?:ing)? .*?(\d+\.\d{2})",
        r"Dray(?:age)? .*?(\d+\.\d{2})",
        r"Chassis .*?(\d+\.\d{2})",
        r"Pier\s*Pass .*?(\d+\.\d{2})",
        r"Demurrage .*?(\d+\.\d{2})",
    ]:
        shipping += _sum_matches(rx, t)
    shipping = round(shipping, 2)

    return {"tariff": tariff, "import_fee": import_fee, "shipping": shipping}

def extract_totals_from_pdf(file) -> dict:
    if not PDF_OK:
        return {"tariff": 0.0, "import_fee": 0.0, "shipping": 0.0, "notes": "pdfplumber not installed"}
    try:
        text = ""
        with pdfplumber.open(file) as pdf:
            for page in pdf.pages:
                text += page.extract_text() or ""
        res = extract_totals_from_text(text)
        res["notes"] = ""
        return res
    except Exception as e:
        return {"tariff": 0.0, "import_fee": 0.0, "shipping": 0.0, "notes": f"PDF parse error: {e}"}

def read_division_table(uploaded, label: str) -> pd.DataFrame:
    # Accept Excel or CSV
    name = uploaded.name.lower()
    if name.endswith(".csv"):
        df = pd.read_csv(uploaded)
    else:
        df = pd.read_excel(uploaded, engine="openpyxl")

    # Normalize headers
    df.columns = [str(c).strip() for c in df.columns]
    lower_map = {c.lower(): c for c in df.columns}

    def pick(colnames):
        for key, real in lower_map.items():
            for cand in colnames:
                if key == cand or cand in key:
                    return real
        return None

    col_po        = pick(["po", "po#", "po no", "po number"])
    col_producer  = pick(["producer"])
    col_item      = pick(["item", "sku", "code"])
    col_desc      = pick(["description", "desc"])
    col_qty       = pick(["qty btls", "qty bottles", "qty", "bottles", "qty_btls", "qty btl"])

    if col_item is None or col_qty is None:
        raise ValueError(f'{label}: Could not find required columns "Item" and "Qty".')

    out = pd.DataFrame({
        "PO":         df[col_po]       if col_po       in df.columns else "",
        "Producer":   df[col_producer] if col_producer in df.columns else "",
        "Item":       df[col_item],
        "Description":df[col_desc]     if col_desc     in df.columns else "",
        "Qty Btls":   pd.to_numeric(df[col_qty], errors="coerce").fillna(0).astype(int)
    })
    out = out[out["Qty Btls"] > 0].reset_index(drop=True)
    return out

def allocate(rows: pd.DataFrame, totals: dict) -> pd.DataFrame:
    bottles = int(rows["Qty Btls"].sum()) if not rows.empty else 0
    if bottles <= 0:
        rows = rows.copy()
        rows["Tariff $/btl"] = 0.0
        rows["Import Fee $/btl"] = 0.0
        rows["Shipping $/btl"] = 0.0
        rows["Inbound $/btl"] = 0.0
        rows["Inbound $ total"] = 0.0
        return rows

    t_per = (totals.get("tariffs", 0.0) or 0.0) / bottles
    i_per = (totals.get("import_fee", totals.get("importFee", 0.0)) or 0.0) / bottles
    s_per = (totals.get("shipping", 0.0) or 0.0) / bottles

    rows = rows.copy()
    rows["Tariff $/btl"]      = round(t_per, 4)
    rows["Import Fee $/btl"]  = round(i_per, 4)
    rows["Shipping $/btl"]    = round(s_per, 4)
    rows["Inbound $/btl"]     = (rows["Tariff $/btl"] + rows["Import Fee $/btl"] + rows["Shipping $/btl"]).round(4)
    rows["Inbound $ total"]   = (rows["Inbound $/btl"] * rows["Qty Btls"]).round(2)
    return rows

def build_summary(ca_btls, ny_btls, ca_totals, ny_totals):
    def mk(name, btls, tar, imp, ship):
        per = ((tar or 0) + (imp or 0) + (ship or 0)) / btls if btls else 0.0
        return {
            "Division": name,
            "Bottles": btls,
            "Tariff total": round(tar or 0, 2),
            "Import Fee total": round(imp or 0, 2),
            "Shipping total": round(ship or 0, 2),
            "Tariff $/btl": round((tar or 0)/btls, 4) if btls else 0.0,
            "Import Fee $/btl": round((imp or 0)/btls, 4) if btls else 0.0,
            "Shipping $/btl": round((ship or 0)/btls, 4) if btls else 0.0,
            "Inbound $/btl (avg)": round(per, 4)
        }
    combined = {
        "tariff": (ca_totals["tariffs"] or 0) + (ny_totals["tariffs"] or 0),
        "import_fee": (ca_totals["import_fee"] or 0) + (ny_totals["import_fee"] or 0),
        "shipping": (ca_totals["shipping"] or 0) + (ny_totals["shipping"] or 0),
        "btls": (ca_btls or 0) + (ny_btls or 0)
    }
    return pd.DataFrame([
        mk("CA", ca_btls, ca_totals["tariffs"], ca_totals["import_fee"], ca_totals["shipping"]),
        mk("NY", ny_btls, ny_totals["tariffs"], ny_totals["import_fee"], ny_totals["shipping"]),
        mk("Combined", combined["btls"], combined["tariff"], combined["import_fee"], combined["shipping"]),
    ])

def to_excel_bytes(sheets: dict) -> bytes:
    bio = io.BytesIO()
    with pd.ExcelWriter(bio, engine="openpyxl") as writer:
        for name, df in sheets.items():
            df.to_excel(writer, index=False, sheet_name=name[:31] or "Sheet")
    bio.seek(0)
    return bio.read()

# ---------- UI ----------
st.title("Freight & Duties Calculator")

st.markdown(
    "Upload the **two PDFs** (freight invoice & entry summary) and the **two spreadsheets** (CA & NY). "
    "Click **Calculate** to produce per-bottle costs and a downloadable Excel."
)

col1, col2 = st.columns(2)
with col1:
    invoice_pdf = st.file_uploader("Freight Invoice (PDF)", type=["pdf"])
    ca_sheet    = st.file_uploader("CA Division (XLSX/CSV)", type=["xlsx","csv"])
with col2:
    entry_pdf   = st.file_uploader("Entry Summary (PDF)", type=["pdf"])
    ny_sheet    = st.file_uploader("NY Division (XLSX/CSV)", type=["xlsx","csv"])

calculate = st.button("Calculate")

if calculate:
    # Basic validations
    if not all([invoice_pdf, entry_pdf, ca_sheet, ny_sheet]):
        st.error("Please upload **all four files**.")
        st.stop()
    if not PDF_OK:
        st.error("PDF support is missing. Ask your admin to add `pdfplumber` to requirements.")
        st.stop()

    # Parse PDFs
    inv_res = extract_totals_from_pdf(invoice_pdf)
    ent_res = extract_totals_from_pdf(entry_pdf)

    # Combined totals (tariff/import from Entry; shipping from Invoice)
    combined_tariff = ent_res["tariff"]
    combined_import = ent_res["import_fee"]
    combined_ship   = inv_res["shipping"]

    # Read division sheets
    try:
        ca_df = read_division_table(ca_sheet, "CA")
        ny_df = read_division_table(ny_sheet, "NY")
    except Exception as e:
        st.error(str(e))
        st.stop()

    ca_btls = int(ca_df["Qty Btls"].sum())
    ny_btls = int(ny_df["Qty Btls"].sum())
    total_btls = ca_btls + ny_btls

    if total_btls == 0:
        st.error("Total bottles = 0. Please check the Qty column in CA/NY files.")
        st.stop()

    # Split totals by bottle share
    share_ca = ca_btls / total_btls
    share_ny = ny_btls / total_btls

    ca_totals = {
        "tariffs": round(combined_tariff * share_ca, 2),
        "import_fee": round(combined_import * share_ca, 2),
        "shipping": round(combined_ship * share_ca, 2),
    }
    ny_totals = {
        "tariffs": round(combined_tariff * share_ny, 2),
        "import_fee": round(combined_import * share_ny, 2),
        "shipping": round(combined_ship * share_ny, 2),
    }

    # Allocations
    ca_alloc = allocate(ca_df, ca_totals)
    ny_alloc = allocate(ny_df, ny_totals)
    summary  = build_summary(ca_btls, ny_btls, ca_totals, ny_totals)

    st.subheader("Detected Totals")
    c1, c2, c3 = st.columns(3)
    c1.metric("Tariff (Entry)", f"${combined_tariff:,.2f}")
    c2.metric("Import Fee excl. tariff (Entry)", f"${combined_import:,.2f}")
    c3.metric("Shipping (Invoice)", f"${combined_ship:,.2f}")

    if inv_res.get("notes") or ent_res.get("notes"):
        st.info("Notes:\n" + "\n".join([x for x in [inv_res.get("notes"), ent_res.get("notes")] if x]))

    st.subheader("Summary (3-way)")
    st.dataframe(summary, use_container_width=True)

    st.subheader("CA per-bottle")
    st.dataframe(ca_alloc, use_container_width=True)

    st.subheader("NY per-bottle")
    st.dataframe(ny_alloc, use_container_width=True)

    # Build download
    excel_bytes = to_excel_bytes({
        "CA (imported)": ca_df,
        "NY (imported)": ny_df,
        "CA per-bottle": ca_alloc,
        "NY per-bottle": ny_alloc,
        "Summary (3-way)": summary
    })
    st.download_button(
        "Download Excel",
        data=excel_bytes,
        file_name="inbound_costs.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )

    st.success("Done.")
