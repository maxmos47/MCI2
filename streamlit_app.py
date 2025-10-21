import json
from typing import Dict, List, Tuple

import pandas as pd
import streamlit as st
import gspread
from google.oauth2.service_account import Credentials
import requests  # ใช้เรียก GAS

st.set_page_config(page_title="Patient Dashboard", page_icon="🩺", layout="centered")

# =========================
# CONFIG (Secondary sheet)
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

# ------- session state -------
if "next_after_lq" not in st.session_state:
    st.session_state["next_after_lq"] = None
if "timer_stopped" not in st.session_state:
    st.session_state["timer_stopped"] = False      # หยุดบน UI (จาก submit หรือหมดเวลา)
if "expired_processed" not in st.session_state:
    st.session_state["expired_processed"] = False  # กันเพิ่ม Z ซ้ำเมื่อหมดเวลา

# =========================
# Query params helpers
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
# Column helpers
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
# Sheet access
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

def build_payloads_from_row(ws, sheet_row: int, mode: str) -> Dict:
    headers, vals = get_header_and_row(ws, sheet_row)

    AK = slice_dict_by_cols(headers, vals, "A", "K")
    LQ_dict = slice_dict_by_cols(headers, vals, "L", "Q")
    headers_LQ = list(LQ_dict.keys())
    current_LQ = [LQ_dict[h] if LQ_dict[h] in YN else ("Yes" if String(LQ_dict[h]).toLowerCase() === "yes" else "No") for h in headers_LQ] if False else [
        LQ_dict[h] if LQ_dict[h] in YN else ("Yes" if str(LQ_dict[h]).strip().lower() == "yes" else "No") for h in headers_LQ
    ]
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
            col_idx = headers.index(h) + 1
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
    Z_idx = col_letter_to_index("Z")
    cell = f"{index_to_col_letter(Z_idx)}{sheet_row}"
    try:
        cur = ws.acell(cell).value
    except Exception:
        cur = ""
    try:
        base = int(float(cur))
    except Exception:
        base = 0
    new_val = base + 1
    ws.update_acell(cell, new_val)
    return new_val

# =========================
# GAS helpers (Primary)
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
    r.raise_for_status()
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
    url = st.secrets.get("gas", {}).get("webapp_url", "")
    if not url:
        return {}
    data = {"action": "stop_timer", "row": str(row)}
    tok = st.secrets.get("gas", {}).get("token", "")
    if tok:
        data["token"] = tok
    r = requests.post(url, data=data, timeout=20)
    r.raise_for_status()
    return r.json()

# =========================
# UI Helpers (cards + countdown)
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

def fmt_hms(secs: int) -> str:
    secs = max(0, int(secs))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

def render_countdown(origin_seconds: int, remaining: int, paused: bool = False):
    import streamlit.components.v1 as components
    initial_digits = fmt_hms(remaining)
    progress_value = max(0, (origin_seconds - remaining) if origin_seconds else 0)
    progress_max = max(1, origin_seconds if origin_seconds > 0 else 1)
    if paused:
        components.html(
            f"""
            <div style="border:1px dashed #94a3b8;padding:12px;border-radius:12px;background:#f8fafc">
              <span style="font-size:0.8rem;background:#e2e8f0;border-radius:999px;padding:4px 10px;color:#334155;margin-right:10px">⏸ Stopped</span>
              <span style="font-weight:800;letter-spacing:1px;line-height:1;font-size:2.6rem">{initial_digits}</span>
              <div style="margin-top:10px">
                <progress max="{progress_max}" value="{progress_value}" style="width:100%"></progress>
              </div>
            </div>
            """,
            height=160,
        )
        return
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
# Main UI
# =========================
st.markdown("### 🩺 Patient Information")

qp = get_query_params()
display_row_str = qp.get("row", "1")
mode = qp.get("mode", "edit1")  # edit1 -> L–Q; edit2 -> V; view -> final

try:
    display_row = max(1, int(display_row_str))
except ValueError:
    display_row = 1

sheet_row = display_row + 1  # header offset

ws = open_ws()
has_inline_phase2 = st.session_state["next_after_lq"] is not None

# ---------- TIMER: ใช้ GAS (Primary) เป็นหลัก / fallback ชีท Secondary ----------
origin_seconds = 0
t0_epoch = 0
end_epoch = 0

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

if end_epoch == 0:
    # fallback: secondary sheet (Q/R/S ที่ secondary เอง หากมี)
    headers, vals = get_header_and_row(ws, sheet_row)
    def _parse_seconds_local(v):
        try:
            if v is None or v == "": return 0
            if isinstance(v, (int,float)):
                if 0 < float(v) < 2: return int(round(float(v)*86400))
                return int(round(float(v)))
            s = str(v).strip()
            if not s: return 0
            if s.isdigit() or (s.startswith("-") and s[1:].isdigit()): return max(0,int(s))
            p = s.split(":")
            if len(p)==2 and all(x.isdigit() for x in p): return int(p[0])*60+int(p[1])
            if len(p)==3 and all(x.isdigit() for x in p): return int(p[0])*3600+int(p[1])*60+int(p[2])
        except: pass
        return 0
    q = _parse_seconds_local(vals[col_letter_to_index("Q")-1] if len(vals)>=col_letter_to_index("Q") else "")
    r = int(float(vals[col_letter_to_index("R")-1])) if len(vals)>=col_letter_to_index("R") and str(vals[col_letter_to_index("R")-1]).strip()!="" else 0
    s_ = int(float(vals[col_letter_to_index("S")-1])) if len(vals)>=col_letter_to_index("S") and str(vals[col_letter_to_index("S")-1]).strip()!="" else 0
    origin_seconds = origin_seconds or q
    t0_epoch = t0_epoch or r
    end_epoch = end_epoch or s_
    if origin_seconds>0 and end_epoch==0:
        now_ts = int(pd.Timestamp.utcnow().timestamp())
        t0_epoch = t0_epoch or now_ts
        end_epoch = t0_epoch + origin_seconds
        r_a1 = f"{index_to_col_letter(col_letter_to_index('R'))}{sheet_row}"
        s_a1 = f"{index_to_col_letter(col_letter_to_index('S'))}{sheet_row}"
        ws.spreadsheet.values_batch_update(body={
            "valueInputOption":"RAW",
            "data":[
                {"range": f"{ws.title}!{r_a1}", "majorDimension":"ROWS", "values":[[t0_epoch]]},
                {"range": f"{ws.title}!{s_a1}", "majorDimension":"ROWS", "values":[[end_epoch]]},
            ]
        })

# คำนวณเวลาคงเหลือ และจัดการหมดเวลา
now = int(pd.Timestamp.utcnow().timestamp())
remaining = max(0, end_epoch - now) if end_epoch else 0

# ถ้าหมดเวลา แล้วยังไม่เคยเพิ่ม Z/ล็อกฟอร์ม → ทำเลย
if (remaining <= 0) and (not st.session_state["expired_processed"]):
    try:
        increment_Z(ws, sheet_row)
        st.session_state["expired_processed"] = True
        st.session_state["timer_stopped"] = True
        st.error("คนไข้เสียชีวิตแล้ว")
    except Exception as e:
        st.warning(f"ไม่สามารถอัปเดตคอลัมน์ Z ได้: {e}")

# -------------- เตรียมข้อมูลแต่ละโหมด (หลังจากอัปเดตหมดเวลาแล้ว) --------------
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

# -------------- แสดง Timer (หลังจัดการฟอร์ม/หมดเวลาแล้วเสมอ) --------------
render_countdown(origin_seconds, remaining, paused=st.session_state["timer_stopped"])

# ฟอร์มถูกล็อกจาก 2 เงื่อนไข: หมดเวลา หรือ เคยกด Submit Treatment แล้ว
form_disabled = st.session_state["timer_stopped"]

# ============ Modes ============
if mode == "view":
    render_kv_grid(df_AC_RV, title="Patient", cols=2)
    if st.session_state["expired_processed"]:
        st.error("คนไข้เสียชีวิตแล้ว")
    else:
        st.success("Triage completed")
    if st.button("Triage again", disabled=form_disabled):
        st.session_state["next_after_lq"] = None
        set_query_params(row=str(display_row), mode="edit1")
        st.rerun()

elif mode == "edit2" and not has_inline_phase2:
    render_kv_grid(df_AC_RU, title="Patient", cols=2)
    st.markdown("#### Secondary Triage")
    idx = ALLOWED_V.index(current_V) if current_V in ALLOWED_V else 0
    with st.form("form_v", border=True):
        v_value = st.selectbox("Select Triage priority", ALLOWED_V, index=idx, disabled=form_disabled)
        submitted = st.form_submit_button("Submit Treatment", disabled=form_disabled)
    if submitted:
        try:
            res = update_V(ws, sheet_row=sheet_row, v_value=v_value)
            if res.get("status") == "ok":
                # หยุดเวลาใน GAS → ให้ S=end=now ที่ต้นทางจริง
                try:
                    s = gas_stop_timer(row=display_row)
                    if s.get("status") != "ok":
                        st.warning(f"stop_timer (GAS) ไม่สำเร็จ: {s}")
                except Exception as ee:
                    # fallback: เซ็ต S ในชีท Secondary
                    try:
                        now_ts = int(pd.Timestamp.utcnow().timestamp())
                        s_a1 = f"{index_to_col_letter(col_letter_to_index('S'))}{sheet_row}"
                        ws.update_acell(s_a1, now_ts)
                    except Exception as e2:
                        st.warning(f"fallback stop (sheet) ไม่สำเร็จ: {e2}")

                st.session_state["timer_stopped"] = True
                final = res.get("final", {})
                df_final = pd.DataFrame([final.get("A_C_R_V", {})])
                render_kv_grid(df_final, title="Patient", cols=2)
                st.success("Saved. Final view (no form).")
                set_query_params(row=str(display_row), mode="view")
                st.rerun()
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
                    chk = st.checkbox(f"{label}", value=default, disabled=form_disabled)
                    selections[label] = "Yes" if chk else "No"
            with r_col:
                for i, label in enumerate(headers_LQ[3:6], start=3):
                    default = True if curr_vals[i] == "Yes" else False
                    chk = st.checkbox(f"{label}", value=default, disabled=form_disabled)
                    selections[label] = "Yes" if chk else "No"

            submitted = st.form_submit_button("Submit", disabled=form_disabled)

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
            v_value = st.selectbox("Select Triage priority", ALLOWED_V, index=idx2, disabled=form_disabled)
            v_submitted = st.form_submit_button("Submit Treatment", disabled=form_disabled)

        if v_submitted:
            try:
                res2 = update_V(ws, sheet_row=sheet_row, v_value=v_value)
                if res2.get("status") == "ok":
                    # หยุดเวลาใน GAS
                    try:
                        s = gas_stop_timer(row=display_row)
                        if s.get("status") != "ok":
                            st.warning(f"stop_timer (GAS) ไม่สำเร็จ: {s}")
                    except Exception as ee:
                        try:
                            now_ts = int(pd.Timestamp.utcnow().timestamp())
                            s_a1 = f"{index_to_col_letter(col_letter_to_index('S'))}{sheet_row}"
                            ws.update_acell(s_a1, now_ts)
                        except Exception as e2:
                            st.warning(f"fallback stop (sheet) ไม่สำเร็จ: {e2}")

                    st.session_state["timer_stopped"] = True
                    final = res2.get("final", {})
                    df_final = pd.DataFrame([final.get("A_C_R_V", {})])
                    render_kv_grid(df_final, title="Patient", cols=2)
                    st.success("Triage เรียบร้อย")
                    st.session_state["next_after_lq"] = None
                    set_query_params(row=str(display_row), mode="view")
                    st.rerun()
                else:
                    st.error(f"Update V failed: {res2}")
            except Exception as e:
                st.error(f"Failed to update V: {e}")
