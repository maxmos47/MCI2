import streamlit as st
import pandas as pd

import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta, timezone

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

def get_header_and_row(ws, row: int):
    headers = ws.row_values(1)
    vals = ws.row_values(row)
    if len(vals) < len(headers):
        vals = vals + [""] * (len(headers) - len(vals))
    return headers, vals

def slice_dict_by_cols(headers, vals, start_col: str, end_col: str):
    s = col_letter_to_index(start_col) - 1
    e = col_letter_to_index(end_col) - 1
    out = {}
    for i in range(s, e + 1):
        if i < len(headers):
            out[headers[i]] = vals[i] if i < len(vals) else ""
    return out

# Lock/timing helpers
DEFAULT_TREATMENT_WINDOW_SECONDS = 300  # fallback if AA is empty

def now_utc_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def parse_utc_iso(s: str|None):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None

def read_lock_state(ws, sheet_row: int):
    headers = ws.row_values(1)
    vals = ws.row_values(sheet_row)
    if len(vals) < len(headers):
        vals += [""] * (len(headers) - len(vals))
    def get_col(col_letter: str):
        idx = col_letter_to_index(col_letter) - 1
        return vals[idx] if idx < len(vals) else ""
    return {
        "start":    get_col("W"),
        "deadline": get_col("X"),
        "token":    get_col("Y"),
        "started":  get_col("Z"),
        "window":   get_col("AA"),  # AA is immediately after Z
    }

def write_lock_state(ws, sheet_row: int, start_iso: str, deadline_iso: str, token: str):
    ws.spreadsheet.values_batch_update(body={
        "valueInputOption": "RAW",
        "data": [
            {"range": f"{ws.title}!W{sheet_row}", "values": [[start_iso]]},
            {"range": f"{ws.title}!X{sheet_row}", "values": [[deadline_iso]]},
            {"range": f"{ws.title}!Y{sheet_row}", "values": [[token]]},
            {"range": f"{ws.title}!Z{sheet_row}", "values": [["0"]]},  # TreatmentStarted reset
            # AA (WindowSec) user-managed per row; not written here
        ]
    })

def mark_treatment_started(ws, sheet_row: int):
    ws.update_acell(f"Z{sheet_row}", "1")

def resolve_window_seconds(raw_window: str) -> int:
    try:
        s = int(str(raw_window).strip())
        return max(1, s)
    except Exception:
        return DEFAULT_TREATMENT_WINDOW_SECONDS


st.set_page_config(page_title='Secondary Triage', page_icon='🩺', layout='centered')
st.markdown('### 🩺 Secondary Triage')

# URL params
qp = st.query_params if hasattr(st, 'query_params') else st.experimental_get_query_params()
def _get_param(name, default=None):
    val = qp.get(name)
    if isinstance(val, list):
        return val[0] if val else default
    return val if val is not None else default
row_str = _get_param('row', '1')
token_param = (_get_param('token', '') or '').strip()
mode = _get_param('mode', 'edit1')
try:
    display_row = max(1, int(row_str))
except:
    display_row = 1
sheet_row = display_row + 1

ws = open_ws()

# Build data
def build_payloads_from_row(ws, sheet_row: int, which: str):
    headers, vals = get_header_and_row(ws, sheet_row)
    AK = slice_dict_by_cols(headers, vals, 'A', 'K')
    RU = slice_dict_by_cols(headers, vals, 'R', 'U')
    AC = slice_dict_by_cols(headers, vals, 'A', 'C')
    RV = slice_dict_by_cols(headers, vals, 'R', 'V')
    payload = {'A_K': AK, 'A_C_R_U': {**AC, **RU}, 'A_C_R_V': {**AC, **RV}}
    LQ = slice_dict_by_cols(headers, vals, 'L', 'Q')
    payload['headers_LQ'] = list(LQ.keys())
    YN = ['Yes', 'No']
    payload['current_LQ'] = [LQ[h] if LQ[h] in YN else ('Yes' if str(LQ[h]).strip().lower()=='yes' else 'No') for h in payload['headers_LQ']]
    Vcol_idx = col_letter_to_index('V') - 1
    payload['current_V'] = vals[Vcol_idx] if Vcol_idx < len(vals) else ''
    return payload
data = build_payloads_from_row(ws, sheet_row, which=mode)
df_AK = pd.DataFrame([data.get('A_K', {})])
df_AC_RU = pd.DataFrame([data.get('A_C_R_U', {})])
df_AC_RV = pd.DataFrame([data.get('A_C_R_V', {})])
headers_LQ = data.get('headers_LQ', ['L','M','N','O','P','Q'])
current_LQ = data.get('current_LQ', [])
current_V = data.get('current_V', '')
ALLOWED_V = ['Priority 1', 'Priority 2', 'Priority 3']

def render_kv_grid(df_one_row: pd.DataFrame, title: str = '', cols: int = 2):
    if title:
        st.subheader(title)
    s = df_one_row.iloc[0]
    items = [(str(c), '' if pd.isna(s[c]) else str(s[c])) for c in df_one_row.columns]
    for i in range(0, len(items), cols):
        row_items = items[i:i+cols]
        cols_ui = st.columns(len(row_items))
        for c_ui, (label, value) in zip(cols_ui, row_items):
            with c_ui:
                st.markdown(f"<div style='border:1px solid #e5e7eb;padding:12px;border-radius:12px;margin-bottom:8px'><div style='color:#6b7280;font-size:0.9rem'>{label}</div><div style='font-weight:600'>{value if value!='' else '-'}</div></div>", unsafe_allow_html=True)

# Enforce token/deadline
state = read_lock_state(ws, sheet_row)
deadline_dt = parse_utc_iso(state['deadline'])
lock_token = (state['token'] or '').strip()
started = (state['started'] or '').strip()
now = datetime.now(timezone.utc)
def can_edit():
    return (deadline_dt is not None) and (now <= deadline_dt) and (token_param != '' and token_param == lock_token) and (started not in ('1','true','TRUE'))
editable = can_edit()

render_kv_grid(df_AK, title='Patient', cols=2)

if (deadline_dt is None) or (now > deadline_dt) or (token_param == '') or (token_param != lock_token):
    st.error('**คนไข้เสียชีวิตแล้ว**')
    st.stop()

if started in ('1','true','TRUE'):
    st.warning('เคสนี้เริ่มรักษาแล้ว (อ่านอย่างเดียว)')
    editable = False

if mode == 'view':
    render_kv_grid(df_AC_RV, title='Patient', cols=2)
    st.success('Triage completed')
    st.stop()

if mode == 'edit2':
    render_kv_grid(df_AC_RU, title='Patient', cols=2)
    st.markdown('#### Secondary Triage')
    if not editable:
        st.warning('โหมดอ่านอย่างเดียว')
        st.stop()
    idx = ALLOWED_V.index(current_V) if current_V in ALLOWED_V else 0
    with st.form('form_v', border=True):
        v_value = st.selectbox('Select Triage priority', ALLOWED_V, index=idx)
        submitted = st.form_submit_button('Submit')
    if submitted:
        V_idx = col_letter_to_index('V')
        a1 = f"{index_to_col_letter(V_idx)}{sheet_row}"
        ws.update_acell(a1, v_value)
        mark_treatment_started(ws, sheet_row)
        headers, vals = get_header_and_row(ws, sheet_row)
        AC = slice_dict_by_cols(headers, vals, 'A', 'C')
        RV = slice_dict_by_cols(headers, vals, 'R', 'V')
        df_final = pd.DataFrame([{**AC, **RV}])
        render_kv_grid(df_final, title='Patient', cols=2)
        st.success('Saved. Final view (no form).')
        st.stop()

st.markdown('#### Treatment')
if not editable:
    st.warning('โหมดอ่านอย่างเดียว')
    st.stop()

selections = {}
curr_vals = current_LQ if current_LQ and len(current_LQ) == 6 else ['No'] * 6
l_col, r_col = st.columns(2)
with st.form('form_lq', border=True):
    with l_col:
        for i, label in enumerate(headers_LQ[:3]):
            default = (curr_vals[i] == 'Yes')
            chk = st.checkbox(f'{label}', value=default)
            selections[label] = 'Yes' if chk else 'No'
    with r_col:
        for i, label in enumerate(headers_LQ[3:6], start=3):
            default = (curr_vals[i] == 'Yes')
            chk = st.checkbox(f'{label}', value=default)
            selections[label] = 'Yes' if chk else 'No'
    submitted = st.form_submit_button('Submit')

if submitted:
    headers = ws.row_values(1)
    updates = []
    for h, v in selections.items():
        if h in headers:
            col_idx = headers.index(h) + 1
            a1 = f"{ws.title}!{index_to_col_letter(col_idx)}{sheet_row}"
            updates.append({'range': a1, 'majorDimension': 'ROWS', 'values': [[v]]})
    if updates:
        ws.spreadsheet.values_batch_update(body={'valueInputOption': 'RAW', 'data': updates})
    mark_treatment_started(ws, sheet_row)
    headers, vals = get_header_and_row(ws, sheet_row)
    AC = slice_dict_by_cols(headers, vals, 'A', 'C')
    RU = slice_dict_by_cols(headers, vals, 'R', 'U')
    df_ru = pd.DataFrame([{**AC, **RU}])
    render_kv_grid(df_ru, title='Patient', cols=2)
    st.success('Saved L–Q. Continue to choose Priority (mode=edit2).')