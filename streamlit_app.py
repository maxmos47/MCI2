import json
from typing import Dict, List, Tuple

import pandas as pd
import streamlit as st
import gspread
from google.oauth2.service_account import Credentials
import requests  # ใช้เรียก GAS (Primary timer)

st.set_page_config(page_title="Patient Dashboard", page_icon="🩺", layout="centered")

# =========================
# CONFIG: Google Sheets (ของ Secondary เอง)
# =========================
SPREADSHEET_ID = (st.secrets.get("gsheets", {}).get("spreadsheet_id", "") or "").strip()
WORKSHEET_NAME = st.secrets.get("gsheets", {}).get("worksheet_name", "Secondary")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# =========================
# Session flags (timer / expiry / treated)
# =========================
if "next_after_lq" not in st.session_state:
    st.session_state["next_after_lq"] = None
if "timer_stopped" not in st.session_state:
    st.session_state["timer_stopped"] = False  # หยุดนับเพราะ submit หรือหมดเวลา
if "expired_processed" not in st.session_state:
    st.session_state["expired_processed"] = False  # กันเพิ่ม Z ซ้ำตอนหมดเวลา
if "treated" not in st.session_state:
    st.session_state["treated"] = False  # ผู้ป่วยได้รับการรักษาแล้ว

# =========================
# Helpers: Google Sheets client
# =========================
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

# =========================
# Query params (row / mode)
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

qp = get_query_params()
display_row_str = qp.get("row", "1")
mode = qp.get("mode", "edit1")  # "edit1" A–K + L–Q, "edit2" R–U + V, "view" A–C + R–V

try:
    display_row = int(display_row_str)
    if display_row < 1:
        display_row = 1
except ValueError:
    display_row = 1

sheet_row = display_row + 1  # header อยู่บรรทัด 1

# =========================
# Utility: column helpers
# =========================
def col_letter_to_index(letter: str) -> int:
    letter = letter.upper()
    result = 0
    for ch in letter:
        result = result * 26 + (ord(ch) - ord('A') + 1)
    return result

def index_to_col_letter(idx: int) -> str:
    letters = ""
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        letters = chr(65 + rem) + letters
    return letters

# =========================
# Data access (rows / updates)
# =========================
def get_header_and_row(ws, row: int) -> Tuple[List[str], List[str]]:
    headers = ws.row_values(1)
    vals = ws.row_values(row)
    if len(vals) < len(headers):
        vals = vals + [""] * (len(headers) - len(vals))
    return headers, vals

def slice_dict_by_cols(headers: List[str], vals: List[str], start_col: str, end_col: str) -> Dict[str, str]:
    s = col_letter_to_index(start_col) - 1
    e = col_letter_to_index(end_col) - 1
    out = {}
    for i in range(s, e + 1):
        if i < len(headers):
            out[headers[i]] = vals[i] if i < len(vals) else ""
    return out

ALLOWED_V = ["Priority 1", "Priority 2", "Priority 3"]
YN = ["Yes", "No"]

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
    if mode == "edit2":
        data["A_C_R_U"] = A_C_R_U
        data["current_V"] = current_V
    if mode == "view":
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

def increment_Z(ws, sheet_row: int) -> int:
    """Z = Z + 1"""
    Z_idx = col_letter_to_index("Z")
    a1 = f"{index_to_col_letter(Z_idx)}{sheet_row}"
    try:
        cur = ws.acell(a1).value
    except Exception:
        cur = ""
    try:
        base = int(float(cur))
    except Exception:
        base = 0
    new_val = base + 1
    ws.update_acell(a1, new_val)
    return new_val

# =========================
# Card UI
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
# GAS helpers (Primary timer)
# =========================
def gas_get_row(row: int) -> dict:
    url = st.secrets.get("gas", {}).get("webapp_url", "")
    if not url:
        return {}
    params = {"action": "get", "row": str(row)}
    tok = st.secrets.get("gas", {}).get("token", "")
    if tok:
        params["token"] = tok
    r = requests.get(url, params=params, timeout=20)
    try:
        r.raise_for_status()
    except Exception as e:
        st.error(f"GAS HTTP error: {e}\nResponse: {r.text}")
        raise
    return r.json()

def gas_start_timer(row: int) -> dict:
    url = st.secrets.get("gas", {}).get("webapp_url", "")
    if not url:
        return {}
    data = {"action": "start_timer", "row": str(row)}
    tok = st.secrets.get("gas", {}).get("token", "")
    if tok:
        data["token"] = tok
    r = requests.post(url, data=data, timeout=20)
    r.raise_for_status()
    return r.json()

def gas_stop_timer(row: int) -> dict:
    """หยุดที่ต้นทาง (ถ้ามี endpoint stop_timer); ถ้าไม่มีจะไม่ error"""
    url = st.secrets.get("gas", {}).get("webapp_url", "")
    if not url:
        return {}
    data = {"action": "stop_timer", "row": str(row)}
    tok = st.secrets.get("gas", {}).get("token", "")
    if tok:
        data["token"] = tok
    r = requests.post(url, data=data, timeout=20)
    try:
        r.raise_for_status()
        return r.json()
    except Exception:
        return {"status": "noop"}

# =========================
# Timer helpers (fallback อ่านจาก Secondary ถ้าไม่มี GAS)
# =========================
def parse_seconds(value) -> int:
    """รองรับ: 120, '02:00', '00:01:30', และ numeric day-fraction"""
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

def read_timer_state(ws, sheet_row: int) -> dict:
    headers, vals = get_header_and_row(ws, sheet_row)
    q_idx = col_letter_to_index("Q") - 1
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

    return {"origin": origin, "t0_epoch": t0_epoch, "end_epoch": end_epoch}

def start_timer_if_needed(ws, sheet_row: int, origin: int, t0_epoch: int, end_epoch: int) -> Tuple[int, int]:
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

def render_countdown(origin_seconds: int, remaining: int, paused: bool = False):
    """โชว์นับถอยหลัง; เมื่อถึง 0 → ซ่อนและ reload หน้า (ทั้งแอป), paused=True → ไม่วาดอะไร (ซ่อน)"""
    import streamlit.components.v1 as components
    if paused:
        return

    def _fmt(secs: int) -> str:
        secs = max(0, int(secs))
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    initial_digits = _fmt(remaining)
    progress_value = max(0, (origin_seconds - remaining) if origin_seconds else 0)
    progress_max = max(1, origin_seconds if origin_seconds > 0 else 1)

    components.html(
        f"""
        <div id="timerWrap" style="border:1px dashed #94a3b8;padding:12px;border-radius:12px;background:#f8fafc">
          <span style="font-size:0.8rem;background:#e2e8f0;border-radius:999px;padding:4px 10px;color:#334155;margin-right:10px">⏳ คนไข้กำลังจะเสียชีวิตใน</span>
          <span id="digits" style="font-weight:600;letter-spacing:1px;line-height:1;font-size:2.6rem">{initial_digits}</span>
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
            const wrap = document.getElementById('timerWrap');
            function fmt(n) {{ return String(n).padStart(2,'0'); }}
            function render() {{
              let s = Math.max(0, Math.floor(remaining));
              let h = Math.floor(s/3600);
              let m = Math.floor((s%3600)/60);
              let ss = s%60;
              if (digits) digits.textContent = `${{fmt(h)}}:${{fmt(m)}}:${{fmt(ss)}}`;
              if (origin > 0 && pg) {{
                pg.max = origin;
                pg.value = Math.min(origin, Math.max(0, origin - s));
              }}
            }}
            function hardReloadParent() {{
              try {{ window.parent.postMessage({{is_streamlit_message: true, type: "streamlit:rerun"}}, "*"); }} catch (e) {{}}
              try {{ (window.parent || window.top || window).location.reload(); }} catch (e) {{}}
            }}
            render();
            const intv = setInterval(() => {{
              remaining -= 1;
              if (remaining <= 0) {{
                remaining = 0;
                render();
                clearInterval(intv);
                if (wrap) wrap.style.display = 'none';
                setTimeout(hardReloadParent, 50);
                return;
              }}
              render();
            }}, 1000);
          }})();
        </script>
        """,
        height=160,
    )

def show_lock_overlay(message: str, variant: str = "expired"):
    """
    variant:
      - "treated"  → โทนเขียว ไอคอน ✅ (คนไข้ได้รับการรักษาแล้ว)
      - "expired"  → โทนแดง ไอคอน ⛔ (คนไข้เสียชีวิตแล้ว)
    """
    if variant == "treated":
        icon = "✅"
        accent = "#16a34a"   # green-600
        bg_pill = "#dcfce7"  # green-100
    else:
        icon = "⛔"
        accent = "#ef4444"   # red-500
        bg_pill = "#fee2e2"  # red-100

    st.markdown(
        f"""
        <style>
        .lock-overlay {{
          position: fixed; inset: 0;
          background: rgba(2,6,23,.65);
          z-index: 99999;
          display: flex; align-items: center; justify-content: center;
          backdrop-filter: blur(2px);
        }}
        .lock-card {{
          background: #fff; color:#111827;
          padding: 28px 32px; border-radius: 16px;
          box-shadow: 0 12px 32px rgba(0,0,0,.28);
          max-width: 90vw; text-align:center; min-width: 320px;
          border-top: 6px solid {accent};
        }}
        .lock-icon {{
          width: 64px; height: 64px; border-radius: 999px;
          display: inline-flex; align-items: center; justify-content: center;
          font-size: 34px; margin-bottom: 10px;
          background: {bg_pill}; color: {accent};
        }}
        .lock-card h2 {{ margin: 6px 0 8px 0; font-size: 1.5rem; color:#111827; }}
        .lock-card p  {{ margin: 0; font-size: 1rem; color:#4b5563; }}
        </style>
        <div class="lock-overlay">
          <div class="lock-card">
            <div class="lock-icon">{icon}</div>
            <h2>{message}</h2>
            <p>ฟอร์มถูกล็อกแล้ว ไม่สามารถแก้ไขได้</p>
          </div>
        </div>
        """,
        unsafe_allow_html=True
    )

# =========================
# Main
# =========================
st.markdown("### 🩺 Patient Information")
ws = open_ws()
has_inline_phase2 = st.session_state["next_after_lq"] is not None

# ---------- TIMER (GAS เป็นหลัก; fallback Secondary) ----------
origin_seconds = 0
t0_epoch = 0
end_epoch = 0

# 1) GAS
try:
    g = gas_get_row(row=display_row)
    if g and g.get("status") == "ok":
        origin_seconds = int(g.get("timer_seconds", 0) or 0)
        t0_epoch = int(g.get("t0_epoch", 0) or 0)
        end_epoch = int(g.get("end_epoch", 0) or 0)
        if origin_seconds > 0 and end_epoch == 0:
            s = gas_start_timer(row=display_row)
            if s.get("status") == "ok":
                t0_epoch = int(s.get("t0_epoch", t0_epoch) or 0)
                end_epoch = int(s.get("end_epoch", end_epoch) or 0)
except Exception as e:
    st.warning(f"GAS error, fallback to sheet: {e}")

# 2) fallback Secondary
if end_epoch == 0:
    try:
        ts = read_timer_state(ws, sheet_row)
        origin_seconds = origin_seconds or int(ts["origin"])
        t0_epoch = t0_epoch or int(ts["t0_epoch"])
        end_epoch = end_epoch or int(ts["end_epoch"])
        if origin_seconds > 0 and end_epoch == 0:
            t0_epoch, end_epoch = start_timer_if_needed(ws, sheet_row, origin_seconds, t0_epoch, end_epoch)
    except Exception as e:
        st.warning(f"Sheet timer fallback error: {e}")

# ===== คำนวณเวลาที่เหลือ =====
now = int(pd.Timestamp.utcnow().timestamp())
remaining = max(0, end_epoch - now) if end_epoch else 0

# ===== หมดเวลา → เพิ่ม Z + ล็อก + rerun (ให้รอบถัดไป lock ทั้งหน้าและเอาปุ่มออก) =====
if (remaining <= 0) and (not st.session_state["expired_processed"]) and (not st.session_state["treated"]):
    try:
        increment_Z(ws, sheet_row)
    except Exception as e:
        st.warning(f"ไม่สามารถอัปเดตคอลัมน์ Z ได้: {e}")
    st.session_state["expired_processed"] = True
    st.session_state["timer_stopped"] = True
    st.rerun()

# ===== สถานะล็อก (หมดเวลา/รักษาแล้ว/กดหยุด) =====
expired = (remaining <= 0) or st.session_state["expired_processed"]
treated = st.session_state["treated"]
locked  = expired or treated or st.session_state["timer_stopped"]

# ===== แสดง/ซ่อนตัวจับเวลา =====
if not locked:
    render_countdown(origin_seconds, remaining, paused=False)

# ===== ข้อความและ Overlay ตามสถานะ =====
if locked:
    if treated:
        show_lock_overlay("คนไข้ได้รับการรักษาแล้ว", variant="treated")
        st.success("คนไข้ได้รับการรักษาแล้ว")
    else:
        show_lock_overlay("คนไข้เสียชีวิตแล้ว", variant="expired")
        st.error("คนไข้เสียชีวิตแล้ว")

# ---------------- Defaults (กัน NameError) ----------------
df_AK = None
df_AC_RU = None
df_AC_RV = None
headers_LQ = ["L","M","N","O","P","Q"]
current_LQ = []
current_V = ""

# ===== เตรียม payload ตามโหมด =====
if mode == "edit1" and not has_inline_phase2:
    try:
        data = build_payloads_from_row(ws, sheet_row=sheet_row, mode="edit1")
        df_AK = pd.DataFrame([data.get("A_K", {})])
        headers_LQ = data.get("headers_LQ", headers_LQ)
        current_LQ = data.get("current_LQ", current_LQ)
    except Exception as e:
        st.error(f"Failed to read sheet: {e}")
        st.stop()

if mode == "edit2" and not has_inline_phase2:
    try:
        data = build_payloads_from_row(ws, sheet_row=sheet_row, mode="edit2")
        df_AC_RU = pd.DataFrame([data.get("A_C_R_U", {})])
        current_V = data.get("current_V", current_V)
    except Exception as e:
        st.error(f"Failed to read sheet: {e}")
        st.stop()

if mode == "view":
    try:
        data = build_payloads_from_row(ws, sheet_row=sheet_row, mode="view")
        df_AC_RV = pd.DataFrame([data.get("A_C_R_V", {})])
    except Exception as e:
        st.error(f"Failed to read sheet: {e}")
        st.stop()

# ============ Modes ============
if mode == "view":
    if df_AC_RV is not None:
        render_kv_grid(df_AC_RV, title="Patient", cols=2)
    if treated:
        st.success("คนไข้ได้รับการรักษาแล้ว")
    elif st.session_state["expired_processed"]:
        st.error("คนไข้เสียชีวิตแล้ว")
    else:
        st.success("Triage completed")
    if (not locked) and st.button("Triage again"):
        st.session_state["next_after_lq"] = None
        set_query_params(row=str(display_row), mode="edit1")
        st.rerun()

elif mode == "edit2" and not has_inline_phase2:
    if df_AC_RU is None:
        data = build_payloads_from_row(ws, sheet_row=sheet_row, mode="edit2")
        df_AC_RU = pd.DataFrame([data.get("A_C_R_U", {})])
        current_V = data.get("current_V", current_V)

    render_kv_grid(df_AC_RU, title="Patient", cols=2)
    st.markdown("#### Secondary Triage")

    if not locked:
        idx = ALLOWED_V.index(current_V) if current_V in ALLOWED_V else 0
        with st.form("form_v"):
            v_value = st.selectbox("Select Triage priority", ALLOWED_V, index=idx)
            submitted = st.form_submit_button("Submit Triage")
        if submitted:
            try:
                res = update_V(ws, sheet_row=sheet_row, v_value=v_value)
                if res.get("status") == "ok":
                    try:
                        gas_stop_timer(display_row)  # ถ้ามี endpoint
                    except Exception:
                        pass
                    st.session_state["treated"] = True
                    st.session_state["timer_stopped"] = True
                    st.toast("⏸ Timer Stopped")
                    set_query_params(row=str(display_row), mode="view")
                    st.rerun()
                else:
                    st.error(f"Update V failed: {res}")
            except Exception as e:
                st.error(f"Failed to update V: {e}")
    else:
        st.info("หน้าถูกล็อกเนื่องจากหมดเวลา/ปิดการรักษาแล้ว")

else:
    # Phase 1: A–K + L–Q form
    if not has_inline_phase2:
        if df_AK is None:
            _data_edit1 = build_payloads_from_row(ws, sheet_row=sheet_row, mode="edit1")
            df_AK = pd.DataFrame([_data_edit1.get("A_K", {})])
            headers_LQ = _data_edit1.get("headers_LQ", ["L","M","N","O","P","Q"])
            current_LQ = _data_edit1.get("current_LQ", [])

        render_kv_grid(df_AK, title="Patient", cols=2)
        st.markdown("#### Treatment")

        if not locked:
            l_col, r_col = st.columns(2)
            selections = {}
            curr_vals = current_LQ if current_LQ and len(current_LQ) == 6 else ["No"] * 6

            with st.form("form_lq"):
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

                submitted = st.form_submit_button("Submit Treatment")

            if submitted:
                try:
                    res = update_LQ(ws, sheet_row=sheet_row, lq_values=selections)
                    if res.get("status") == "ok":
                        st.session_state["next_after_lq"] = res.get("next", {})
                    else:
                        st.error(f"Update L–Q failed: {res}")
                except Exception as e:
                    st.error(f"Failed to update L–Q: {e}")
        else:
            st.info("หน้าถูกล็อกเนื่องจากหมดเวลา/ปิดการรักษาแล้ว")

    # Inline phase 2 after L–Q submit
    nxt = st.session_state.get("next_after_lq")
    if nxt:
        df_ru = pd.DataFrame([nxt.get("A_C_R_U", {})])
        render_kv_grid(df_ru, title="Treatment Result", cols=2)
        st.markdown("#### Secondary Triage")

        if not locked:
            current_V2 = nxt.get("current_V", "")
            idx2 = ALLOWED_V.index(current_V2) if current_V2 in ALLOWED_V else 0
            with st.form("form_v_inline"):
                v_value = st.selectbox("Select Triage priority", ALLOWED_V, index=idx2)
                v_submitted = st.form_submit_button("Submit Triage")

            if v_submitted:
                try:
                    res2 = update_V(ws, sheet_row=sheet_row, v_value=v_value)
                    if res2.get("status") == "ok":
                        try:
                            gas_stop_timer(display_row)
                        except Exception:
                            pass
                        st.session_state["treated"] = True
                        st.session_state["timer_stopped"] = True
                        st.toast("⏸ Timer Stopped")
                        st.session_state["next_after_lq"] = None
                        set_query_params(row=str(display_row), mode="view")
                        st.rerun()
                    else:
                        st.error(f"Update V failed: {res2}")
                except Exception as e:
                    st.error(f"Failed to update V: {e}")
        else:
            st.info("หน้าถูกล็อกเนื่องจากหมดเวลา/ปิดการรักษาแล้ว")
