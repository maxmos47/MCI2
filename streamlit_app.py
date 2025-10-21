import json
from typing import Dict, List, Tuple

import pandas as pd
import streamlit as st
import gspread
from google.oauth2.service_account import Credentials

st.set_page_config(page_title="Patient Dashboard", page_icon="🩺", layout="centered")

# =========================
# CONFIG: Google Sheets
# =========================
SPREADSHEET_ID = (st.secrets.get("gsheets", {}).get("spreadsheet_id", "") or "").strip()
WORKSHEET_NAME = st.secrets.get("gsheets", {}).get("worksheet_name", "Secondary")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

def get_gs_client():
    if "gcp_service_account" not in st.secrets:
        st.error("Missing [gcp_service_account] in secrets.toml")
        st.stop()
    info = dict(st.secrets["gcp_service_account"])
    pk = info.get("private_key", "")
    if pk and ("\\n" in pk) and ("\n" not in pk):
        info["private_key"] = pk.replace("\\n", "\n")
    if "BEGIN PRIVATE KEY" not in info.get("private_key", ""):
        st.error("Invalid private_key format in secrets.toml")
        st.stop()
    try:
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    except Exception as e:
        st.error(f"Failed to build credentials: {e}")
        st.stop()
    return gspread.authorize(creds)

def open_ws():
    if not SPREADSHEET_ID:
        st.error("Missing [gsheets].spreadsheet_id in secrets.toml")
        st.stop()
    gc = get_gs_client()
    try:
        sh = gc.open_by_key(SPREADSHEET_ID)
    except Exception as e:
        st.error("เปิดสเปรดชีตไม่สำเร็จ (ตรวจสิทธิ์/Spreadsheet ID):\n" + str(e))
        st.stop()
    try:
        ws = sh.worksheet(WORKSHEET_NAME)
    except Exception as e:
        st.error(f"หา worksheet ชื่อ '{WORKSHEET_NAME}' ไม่เจอ: {e}")
        st.stop()
    return ws

ALLOWED_V = ["Priority 1", "Priority 2", "Priority 3"]
YN = ["Yes", "No"]

# Keep phase-2 payload after L–Q submit (avoid extra reload)
if "next_after_lq" not in st.session_state:
    st.session_state["next_after_lq"] = None

# =========================
# Helpers for query params
# =========================
def get_query_params() -> Dict[str, str]:
    try:
        q = st.query_params
        return {k: v for k, v in q.items()}
    except Exception:
        return {k: v[0] for k, v in st.experimental_get_query_params().items()}

def set_query_params(**kwargs):
    try:
        st.query_params.clear()
        st.query_params.update(kwargs)
    except Exception:
        st.experimental_set_query_params(**kwargs)

# =========================
# Utility: column helpers
# =========================
def col_letter_to_index(letter: str) -> int:
    """A -> 1, B -> 2, ..."""
    letter = letter.upper()
    result = 0
    for ch in letter:
        result = result * 26 + (ord(ch) - ord('A') + 1)
    return result

def index_to_col_letter(idx: int) -> str:
    """1 -> A, 2 -> B, ..."""
    letters = ""
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        letters = chr(65 + rem) + letters
    return letters

# =========================
# Sheets data access layer
# =========================
def get_header_and_row(ws, row: int) -> Tuple[List[str], List[str]]:
    """Return (headers, values) where headers are row 1 and values are row N."""
    headers = ws.row_values(1)
    vals = ws.row_values(row)
    if len(vals) < len(headers):
        vals = vals + [""] * (len(headers) - len(vals))
    return headers, vals

def slice_dict_by_cols(headers: List[str], vals: List[str], start_col: str, end_col: str) -> Dict[str, str]:
    s = col_letter_to_index(start_col) - 1  # 0-based
    e = col_letter_to_index(end_col) - 1
    out = {}
    for i in range(s, e + 1):
        if i < len(headers):
            out[headers[i]] = vals[i] if i < len(vals) else ""
    return out

def build_payloads_from_row(ws, sheet_row: int, mode: str) -> Dict:
    headers, vals = get_header_and_row(ws, sheet_row)

    AK = slice_dict_by_cols(headers, vals, "A", "K")
    LQ_dict = slice_dict_by_cols(headers, vals, "L", "Q")
    headers_LQ = list(LQ_dict.keys())
    current_LQ = [LQ_dict[h] if LQ_dict[h] in YN else ("Yes" if str(LQ_dict[h]).strip().lower() == "yes" else "No") for h in headers_LQ]
    RU = slice_dict_by_cols(headers, vals, "R", "U")
    Vcol_idx = col_letter_to_index("V") - 1
    current_V = vals[Vcol_idx] if Vcol_idx < len(vals) else ""
    AC = slice_dict_by_cols(headers, vals, "A", "C")
    A_C_R_U = {**AC, **RU}
    RV = slice_dict_by_cols(headers, vals, "R", "V")
    A_C_R_V = {**AC, **RV}

    data = {"status": "ok"}
    if mode == "edit1":
        data["A_K"] = AK
        data["headers_LQ"] = headers_LQ
        data["current_LQ"] = current_LQ
    elif mode == "edit2":
        data["A_C_R_U"] = A_C_R_U
        data["current_V"] = current_V
    elif mode == "view":
        data["A_C_R_V"] = A_C_R_V
    return data

def update_LQ(ws, sheet_row: int, lq_values: Dict[str, str]) -> Dict:
    headers = ws.row_values(1)
    updates = []
    for h, v in lq_values.items():
        if h in headers:
            col_idx = headers.index(h) + 1  # 1-based
            a1 = f"{ws.title}!{index_to_col_letter(col_idx)}{sheet_row}"
            updates.append({"range": a1, "majorDimension": "ROWS", "values": [[v]]})
    if updates:
        ws.spreadsheet.values_batch_update(body={"valueInputOption": "RAW", "data": updates})
    data_next = build_payloads_from_row(ws, sheet_row, mode="edit2")
    return {"status": "ok", "next": data_next}

def update_V(ws, sheet_row: int, v_value: str) -> Dict:
    V_idx = col_letter_to_index("V")
    a1 = f"{index_to_col_letter(V_idx)}{sheet_row}"
    ws.update_acell(a1, v_value)
    headers, vals = get_header_and_row(ws, sheet_row)
    AC = slice_dict_by_cols(headers, vals, "A", "C")
    RV = slice_dict_by_cols(headers, vals, "R", "V")
    return {"status": "ok", "final": {"A_C_R_V": {**AC, **RV}}}

# =========================
# Card UI (mobile-friendly)
# =========================
st.markdown("""
<style>
.kv-card{border:1px solid #e5e7eb;padding:12px;border-radius:14px;margin-bottom:10px;box-shadow:0 1px 4px rgba(0,0,0,0.06);background:#fff;}
.kv-label{font-size:0.9rem;color:#6b7280;margin-bottom:2px;}
.kv-value{font-size:1.05rem;font-weight:600;word-break:break-word;}
@media (max-width: 640px){
  .kv-card{padding:12px;}
  .kv-value{font-size:1.06rem;}
}
</style>
""", unsafe_allow_html=True)

def _pairs_from_row(df_one_row: pd.DataFrame) -> List[Tuple[str, str]]:
    s = df_one_row.iloc[0]
    pairs: List[Tuple[str, str]] = []
    for col in df_one_row.columns:
        val = s[col]
        if pd.isna(val):
            val = ""
        pairs.append((str(col), str(val)))
    return pairs

def render_kv_grid(df_one_row: pd.DataFrame, title: str = "", cols: int = 2):
    if title:
        st.subheader(title)
    items = _pairs_from_row(df_one_row)
    n = len(items)
    for i in range(0, n, cols):
        row_items = items[i:i+cols]
        col_objs = st.columns(len(row_items))
        for c, (label, value) in zip(col_objs, row_items):
            with c:
                st.markdown(
                    f"""
                    <div class="kv-card">
                      <div class="kv-label">{label}</div>
                      <div class="kv-value">{value if value!='' else '-'}</div>
                    </div>
                    """,
                    unsafe_allow_html=True
                )

# =========================
# Timer config (ปรับได้จาก secrets)
# =========================
ORIGIN_COL = st.secrets.get("timer", {}).get("origin_column", "Q")  # ถ้า origin ไม่ได้อยู่ที่ Q ให้เปลี่ยนใน secrets
USE_GAS_FIRST = bool(st.secrets.get("gas", {}).get("webapp_url", ""))  # ถ้ามี GAS จะใช้เป็นแหล่งข้อมูลหลัก

# =========================
# (ทางเลือก) ดึงจาก GAS ถ้ามี
# =========================
import requests

def gas_get_row(row: int) -> dict:
    gas_url = st.secrets.get("gas", {}).get("webapp_url", "")
    if not gas_url:
        return {}
    params = {"action": "get", "row": str(row)}
    token = st.secrets.get("gas", {}).get("token", "")
    if token:
        params["token"] = token
    r = requests.get(gas_url, params=params, timeout=20)
    r.raise_for_status()
    return r.json()

def gas_start_timer(row: int) -> dict:
    gas_url = st.secrets.get("gas", {}).get("webapp_url", "")
    if not gas_url:
        return {}
    payload = {"action": "start_timer", "row": str(row)}
    token = st.secrets.get("gas", {}).get("token", "")
    if token:
        payload["token"] = token
    r = requests.post(gas_url, data=payload, timeout=20)
    r.raise_for_status()
    return r.json()

# =========================
# Timer helpers (อ่านจากชีทโดยตรง)
# =========================
def parse_seconds(value) -> int:
    """รองรับ: 120, '120', '02:00', '00:01:30' และ numeric day-fraction ของ Google Sheets"""
    try:
        if value is None or value == "":
            return 0
        if hasattr(value, "hour") and hasattr(value, "minute") and hasattr(value, "second"):
            return max(0, int(value.hour) * 3600 + int(value.minute) * 60 + int(value.second))
        if isinstance(value, (int, float)):
            if 0 < float(value) < 2:
                return max(0, int(round(float(value) * 86400)))
            return max(0, int(round(float(value))))
        s = str(value).strip()
        if not s:
            return 0
        if s.isdigit() or (s.startswith("-") and s[1:].isdigit()):
            return max(0, int(s))
        parts = s.split(":")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            return max(0, int(parts[0]) * 60 + int(parts[1]))
        if len(parts) == 3 and all(p.isdigit() for p in parts):
            return max(0, int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2]))
    except Exception:
        pass
    return 0

def read_timer_state_from_sheet(ws, sheet_row: int) -> dict:
    """อ่าน ORIGIN(Raw) จาก ORIGIN_COL, และอ่าน R/S: คืน {origin, t0_epoch, end_epoch, debug}"""
    headers, vals = get_header_and_row(ws, sheet_row)
    # หา index ตามตัวอักษรคอลัมน์
    q_idx = col_letter_to_index(ORIGIN_COL) - 1
    r_idx = col_letter_to_index("R") - 1
    s_idx = col_letter_to_index("S") - 1

    origin_raw = vals[q_idx] if q_idx < len(vals) else ""
    t0_raw     = vals[r_idx] if r_idx < len(vals) else ""
    end_raw    = vals[s_idx] if s_idx < len(vals) else ""

    origin = parse_seconds(origin_raw)
    try:
        t0_epoch = int(float(t0_raw)) if str(t0_raw).strip() != "" else 0
    except Exception:
        t0_epoch = 0
    try:
        end_epoch = int(float(end_raw)) if str(end_raw).strip() != "" else 0
    except Exception:
        end_epoch = 0

    return {"origin": origin, "t0_epoch": t0_epoch, "end_epoch": end_epoch,
            "debug": {"origin_raw": origin_raw, "R": t0_raw, "S": end_raw, "ORIGIN_COL": ORIGIN_COL}}

def start_timer_if_needed_on_sheet(ws, sheet_row: int, origin: int, t0_epoch: int, end_epoch: int) -> tuple[int, int]:
    """ตั้งค่า R/S ครั้งแรก (idempotent) บนชีท"""
    if origin <= 0:
        return t0_epoch, end_epoch
    if t0_epoch > 0 and end_epoch > 0:
        return t0_epoch, end_epoch

    now = int(pd.Timestamp.utcnow().timestamp())
    t0 = now if t0_epoch <= 0 else t0_epoch
    end_ = t0 + origin if end_epoch <= 0 else end_epoch

    r_a1 = f"{index_to_col_letter(col_letter_to_index('R'))}{sheet_row}"
    s_a1 = f"{index_to_col_letter(col_letter_to_index('S'))}{sheet_row}"
    ws.spreadsheet.values_batch_update(body={
        "valueInputOption": "RAW",
        "data": [
            {"range": f"{ws.title}!{r_a1}", "majorDimension": "ROWS", "values": [[t0]]},
            {"range": f"{ws.title}!{s_a1}", "majorDimension": "ROWS", "values": [[end_]]},
        ]
    })
    return t0, end_

def fmt_hms(secs: int) -> str:
    secs = max(0, int(secs))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

def render_countdown(origin_seconds: int, remaining: int):
    """ตัวเลข HH:MM:SS + progress bar (JS, ไม่ต้อง rerun)"""
    import streamlit.components.v1 as components
    initial_digits = fmt_hms(remaining)
    progress_value = max(0, (origin_seconds - remaining) if origin_seconds else 0)
    progress_max = max(1, origin_seconds if origin_seconds > 0 else 1)

    components.html(
        f"""
        <div style="border:1px dashed #94a3b8;padding:12px;border-radius:12px;background:#f8fafc">
          <span style="font-size:0.8rem;background:#e2e8f0;border-radius:999px;padding:4px 10px;color:#334155;margin-right:10px">⏳ Server timer</span>
          <span id="digits" style="font-weight:800;letter-spacing:1px;line-height:1;font-size:2.6rem">{initial_digits}</span>
          <div style="margin-top:10px">
            <progress id="pg" max="{progress_max}" value="{progress_value}" style="width:100%"></progress>
          </div>
        </div>
        <script>
          (function() {{
            let remaining = {remaining};
            const origin = {origin_seconds};
            const digits = document.getElementById('digits');
            const pg = document.getElementById('pg');
            function fmt(n) {{ return String(n).padStart(2,'0'); }}
            function render() {{
              let s = Math.max(0, Math.floor(remaining));
              let h = Math.floor(s/3600);
              let m = Math.floor((s%3600)/60);
              let ss = s%60;
              digits.textContent = `${{fmt(h)}}:${{fmt(m)}}:${{fmt(ss)}}`;
              if (origin > 0 && pg) {{
                pg.max = origin;
                pg.value = Math.min(origin, Math.max(0, origin - s));
              }}
            }}
            render();
            const intv = setInterval(() => {{
              remaining -= 1;
              if (remaining <= 0) {{ remaining = 0; render(); clearInterval(intv); return; }}
              render();
            }}, 1000);
          }})();
        </script>
        """,
        height=160,
    )

# =========================
# ใช้งานจริง: อ่านสถานะ/เริ่ม/แสดงผล
# =========================
try:
    origin_seconds = 0
    t0_epoch = 0
    end_epoch = 0
    debug_info = {}

    if USE_GAS_FIRST:
        # 1) พยายามอ่านจาก GAS ก่อน (เหมือน Primary)
        try:
            # display_row มาจากด้านบนของไฟล์คุณ
            gas_row = display_row
            gas_data = gas_get_row(row=gas_row)
            if gas_data.get("status") == "ok":
                origin_seconds = int(gas_data.get("timer_seconds", 0) or 0)
                t0_epoch = int(gas_data.get("t0_epoch", 0) or 0)
                end_epoch = int(gas_data.get("end_epoch", 0) or 0)
                # ถ้า Q>0 แต่มันยังไม่ start ที่เซิร์ฟเวอร์ → start ที่ GAS
                if origin_seconds > 0 and end_epoch == 0:
                    started = gas_start_timer(row=gas_row)
                    if started.get("status") == "ok":
                        t0_epoch = int(started.get("t0_epoch", t0_epoch) or 0)
                        end_epoch = int(started.get("end_epoch", end_epoch) or 0)
        except Exception as e:
            st.warning(f"GAS fetch failed, fallback to sheet: {e}")

    if not USE_GAS_FIRST or end_epoch == 0:
        # 2) ถ้ายังไม่ได้ end_epoch → fallback อ่านจากชีทโดยตรง
        ts = read_timer_state_from_sheet(ws, sheet_row)
        origin_seconds = origin_seconds or int(ts["origin"])
        t0_epoch = t0_epoch or int(ts["t0_epoch"])
        end_epoch = end_epoch or int(ts["end_epoch"])
        debug_info = ts.get("debug", {})

        # ถ้า Q>0 แต่ R/S ยังว่าง → ตั้ง R/S ในชีทเลย (idempotent)
        if origin_seconds > 0 and end_epoch == 0:
            t0_epoch, end_epoch = start_timer_if_needed_on_sheet(ws, sheet_row, origin_seconds, t0_epoch, end_epoch)

    # คำนวณเวลาที่เหลือ “จาก S เป็นหลัก”
    now = int(pd.Timestamp.utcnow().timestamp())
    remaining = max(0, end_epoch - now) if end_epoch else 0

    # แสดง
    render_countdown(origin_seconds, remaining)

    # Panel debug เล็ก ๆ (ช่วยไล่เคสค่า Q เป็น Yes/No)
    with st.expander("🛠 Timer diagnostics"):
        st.write({
            "origin_seconds": origin_seconds,
            "t0_epoch": t0_epoch,
            "end_epoch": end_epoch,
            "remaining_now": remaining,
            "use_gas_first": USE_GAS_FIRST,
            "origin_column": ORIGIN_COL,
            **({"sheet_debug": debug_info} if debug_info else {})
        })

    # แจ้งเตือนเคสยอดฮิต
    if origin_seconds == 0 and end_epoch == 0:
        st.info("Timer เป็น 0: ตรวจดูว่าในชีทคอลัมน์ต้นทาง (ค่า ORIGIN_COL ปัจจุบันคือ "
                f"'{ORIGIN_COL}') มีวินาที/เวลาอยู่จริงหรือไม่ (เช่น 120, 02:00, 00:01:30)")

except Exception as e:
    st.warning(f"Timer error: {e}")

# =========================
# Main UI
# =========================
st.markdown("### 🩺 Patient Information")

qp = get_query_params()
display_row_str = qp.get("row", "1")
mode = qp.get("mode", "edit1")  # edit1 -> L–Q; edit2 -> V; view -> final

# Interpret URL row=1 as sheet row 2 (headers at row 1)
try:
    display_row = int(display_row_str)
    if display_row < 1:
        display_row = 1
except ValueError:
    display_row = 1

sheet_row = display_row + 1  # shift by 1 so that "row=1" targets sheet row 2

ws = open_ws()
has_inline_phase2 = st.session_state["next_after_lq"] is not None

# ---------- TIMER: อ่าน/ตั้งค่า และแสดงเคาน์ดาวน์ ----------
try:
    ts = read_timer_state(ws, sheet_row)
    origin_seconds = int(ts["origin"])
    t0_epoch = int(ts["t0_epoch"])
    end_epoch = int(ts["end_epoch"])

    # ถ้ายังไม่เริ่ม แต่ Q > 0 -> เริ่ม (เขียน R,S)
    if origin_seconds > 0 and end_epoch == 0:
        t0_epoch, end_epoch = start_timer_if_needed(ws, sheet_row, origin_seconds, t0_epoch, end_epoch)

    now = int(pd.Timestamp.utcnow().timestamp())
    remaining = max(0, end_epoch - now) if end_epoch else 0

    render_countdown(origin_seconds, remaining)

    if origin_seconds == 0:
        st.info("Timer: ค่า Q ไม่ถูกต้องหรือเป็นค่าว่าง (รองรับ 120, '02:00', '00:01:30' หรือเวลาแบบ day-fraction)")
except Exception as e:
    st.warning(f"Timer error: {e}")

# Prepare dataframes by mode
if mode == "edit1" and not has_inline_phase2:
    try:
        data = build_payloads_from_row(ws, sheet_row=sheet_row, mode="edit1")
    except Exception as e:
        st.error(f"Failed to read sheet: {e}")
        st.stop()
    df_AK = pd.DataFrame([data.get("A_K", {})])
    headers_LQ = data.get("headers_LQ", ["L","M","N","O","P","Q"])
    current_LQ = data.get("current_LQ", [])
elif mode == "edit2" and not has_inline_phase2:
    try:
        data = build_payloads_from_row(ws, sheet_row=sheet_row, mode="edit2")
    except Exception as e:
        st.error(f"Failed to read sheet: {e}")
        st.stop()
    df_AC_RU = pd.DataFrame([data.get("A_C_R_U", {})])
    current_V = data.get("current_V", "")
elif mode == "view":
    try:
        data = build_payloads_from_row(ws, sheet_row=sheet_row, mode="view")
    except Exception as e:
        st.error(f"Failed to read sheet: {e}")
        st.stop()
    df_AC_RV = pd.DataFrame([data.get("A_C_R_V", {})])

# ============ Modes ============
if mode == "view":
    render_kv_grid(df_AC_RV, title="Patient", cols=2)
    st.success("Triage completed")
    if st.button("Triage again"):
        st.session_state["next_after_lq"] = None
        set_query_params(row=str(display_row), mode="edit1")
        st.rerun()

elif mode == "edit2" and not has_inline_phase2:
    render_kv_grid(df_AC_RU, title="Patient", cols=2)
    st.markdown("#### Secondary Triage")
    idx = ALLOWED_V.index(current_V) if current_V in ALLOWED_V else 0
    with st.form("form_v", border=True):
        v_value = st.selectbox("Select Triage priority", ALLOWED_V, index=idx)
        submitted = st.form_submit_button("Submit")
    if submitted:
        try:
            res = update_V(ws, sheet_row=sheet_row, v_value=v_value)
            if res.get("status") == "ok":
                final = res.get("final", {})
                df_final = pd.DataFrame([final.get("A_C_R_V", {})])
                render_kv_grid(df_final, title="Patient", cols=2)
                st.success("Saved. Final view (no form).")
                set_query_params(row=str(display_row), mode="view")
            else:
                st.error(f"Update V failed: {res}")
        except Exception as e:
            st.error(f"Failed to update V: {e}")

else:
    # Phase 1: A–K + L–Q form
    if not has_inline_phase2:
        render_kv_grid(df_AK, title="Patient", cols=2)
        st.markdown("#### Treatment")
        l_col, r_col = st.columns(2)
        selections = {}
        curr_vals = current_LQ if current_LQ and len(current_LQ) == 6 else ["No"] * 6

        with st.form("form_lq", border=True):
            with l_col:
                for i, label in enumerate(headers_LQ[:3]):
                    default = True if curr_vals[i] == "Yes" else False
                    chk = st.checkbox(f"{label}", value=default)
                    selections[label] = "Yes" if chk else "No"
            with r_col:
                for i, label in enumerate(headers_LQ[3:6], start=3):
                    default = True if curr_vals[i] == "Yes" else False
                    chk = st.checkbox(f"{label}", value=default)
                    selections[label] = "Yes" if chk else "No"

            submitted = st.form_submit_button("Submit")

        if submitted:
            try:
                res = update_LQ(ws, sheet_row=sheet_row, lq_values=selections)
                if res.get("status") == "ok":
                    st.session_state["next_after_lq"] = res.get("next", {})
                else:
                    st.error(f"Update L–Q failed: {res}")
            except Exception as e:
                st.error(f"Failed to update L–Q: {e}")

    # Inline phase 2 after L–Q submit
    nxt = st.session_state.get("next_after_lq")
    if nxt:
        df_ru = pd.DataFrame([nxt.get("A_C_R_U", {})])
        render_kv_grid(df_ru, title="Patient", cols=2)

        st.markdown("#### Secondary Triage")
        current_V2 = nxt.get("current_V", "")
        idx2 = ALLOWED_V.index(current_V2) if current_V2 in ALLOWED_V else 0
        with st.form("form_v_inline", border=True):
            v_value = st.selectbox("Select Triage priority", ALLOWED_V, index=idx2)
            v_submitted = st.form_submit_button("Submit")

        if v_submitted:
            try:
                res2 = update_V(ws, sheet_row=sheet_row, v_value=v_value)
                if res2.get("status") == "ok":
                    final = res2.get("final", {})
                    df_final = pd.DataFrame([final.get("A_C_R_V", {})])
                    render_kv_grid(df_final, title="Patient", cols=2)
                    st.success("Triage เรียบร้อย")
                    st.session_state["next_after_lq"] = None
                    set_query_params(row=str(display_row), mode="view")
                else:
                    st.error(f"Update V failed: {res2}")
            except Exception as e:
                st.error(f"Failed to update V: {e}")
