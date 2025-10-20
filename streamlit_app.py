import streamlit as st
import pandas as pd
import time

import pandas as pd
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta, timezone
import hmac, hashlib, json, base64

SPREADSHEET_ID = (st.secrets.get("gsheets", {}).get("spreadsheet_id", "") or "").strip()
WORKSHEET_NAME = st.secrets.get("gsheets", {}).get("worksheet_name", "Secondary")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",  # read-only for safety
    "https://www.googleapis.com/auth/drive.readonly",
]

def get_gs_client():
    if "gcp_service_account" not in st.secrets:
        st.error("Missing [gcp_service_account] in secrets")
        st.stop()
    info = dict(st.secrets["gcp_service_account"])
    pk = info.get("private_key", "")
    if pk and ("\\n" in pk) and ("\n" not in pk):
        info["private_key"] = pk.replace("\\n", "\n")
    try:
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    except Exception as e:
        st.error(f"Failed to build credentials: {e}")
        st.stop()
    return gspread.authorize(creds)

def open_ws():
    if not SPREADSHEET_ID:
        st.error("Missing [gsheets].spreadsheet_id in secrets")
        st.stop()
    gc = get_gs_client()
    try:
        sh = gc.open_by_key(SPREADSHEET_ID)
        ws = sh.worksheet(WORKSHEET_NAME)
    except Exception as e:
        st.error(f"Open worksheet failed: {e}")
        st.stop()
    return ws

def col_letter_to_index(letter: str) -> int:
    letter = letter.upper()
    result = 0
    for ch in letter:
        result = result * 26 + (ord(ch) - ord('A') + 1)
    return result

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

# ------- Stateless signed token (HMAC) -------
def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

def b64url_json(obj) -> str:
    return b64url(json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))

def sign_token(payload: dict, secret: str) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    h = b64url_json(header)
    p = b64url_json(payload)
    signing_input = f"{h}.{p}".encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{h}.{p}.{b64url(sig)}"

def verify_token(token: str, secret: str) -> dict | None:
    try:
        h, p, s = token.split(".")
        signing_input = f"{h}.{p}".encode("utf-8")
        expected = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
        got = base64.urlsafe_b64decode(s + "==")
        if not hmac.compare_digest(expected, got):
            return None
        payload = json.loads(base64.urlsafe_b64decode(p + "==").decode("utf-8"))
        return payload
    except Exception:
        return None


st.set_page_config(page_title='Secondary Triage', page_icon='🩺', layout='centered')
st.markdown('### 🩺 Secondary Triage — Stateless Verify')

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

# Verify token (stateless)
secret = (st.secrets.get('auth', {}).get('hmac_secret', '') or '').strip()
if not secret:
    st.error('Missing [auth].hmac_secret in secrets')
    st.stop()
payload = verify_token(token_param, secret) if token_param else None
if not payload or int(payload.get('row', -1)) != display_row:
    st.error('**คนไข้เสียชีวิตแล้ว**')
    st.stop()
if int(time.time()) > int(payload.get('exp', 0)):
    st.error('**คนไข้เสียชีวิตแล้ว**')
    st.stop()

ws = open_ws()

# Build data (read-only from sheet)
def build_payloads(ws, sheet_row: int):
    headers, vals = get_header_and_row(ws, sheet_row)
    AK = slice_dict_by_cols(headers, vals, 'A', 'K')
    RU = slice_dict_by_cols(headers, vals, 'R', 'U')
    AC = slice_dict_by_cols(headers, vals, 'A', 'C')
    RV = slice_dict_by_cols(headers, vals, 'R', 'V')
    LQ = slice_dict_by_cols(headers, vals, 'L', 'Q')
    YN = ['Yes', 'No']
    return {
        'A_K': AK,
        'A_C_R_U': {**AC, **RU},
        'A_C_R_V': {**AC, **RV},
        'headers_LQ': list(LQ.keys()),
        'current_LQ': [LQ[h] if LQ[h] in YN else ('Yes' if str(LQ[h]).strip().lower()=='yes' else 'No') for h in LQ.keys()],
        'current_V': RV.get(list(RV.keys())[-1], '') if RV else ''
    }
data = build_payloads(ws, sheet_row)

def render_kv_grid(df_one_row: pd.DataFrame, title: str = '', cols: int = 2):
    if title: st.subheader(title)
    s = df_one_row.iloc[0]
    items = [(str(c), '' if pd.isna(s[c]) else str(s[c])) for c in df_one_row.columns]
    for i in range(0, len(items), cols):
        row_items = items[i:i+cols]
        cols_ui = st.columns(len(row_items))
        for c_ui, (label, value) in zip(cols_ui, row_items):
            with c_ui:
                st.markdown(f"<div style='border:1px solid #e5e7eb;padding:12px;border-radius:12px;margin-bottom:8px'><div style='color:#6b7280;font-size:0.9rem'>{label}</div><div style='font-weight:600'>{value if value!='' else '-'}</div></div>", unsafe_allow_html=True)

# Grids
df_AK = pd.DataFrame([data['A_K']])
df_AC_RU = pd.DataFrame([data['A_C_R_U']])
df_AC_RV = pd.DataFrame([data['A_C_R_V']])
headers_LQ = data['headers_LQ']
current_LQ = data['current_LQ']
current_V = data['current_V']
ALLOWED_V = ['Priority 1', 'Priority 2', 'Priority 3']

render_kv_grid(df_AK, title='Patient', cols=2)

# Editable is granted because token valid + not expired (no sheet writes needed)
if mode == 'view':
    render_kv_grid(df_AC_RV, title='Patient', cols=2)
    st.success('Triage completed')
    st.stop()

# edit2 (choose V)
if mode == 'edit2':
    render_kv_grid(df_AC_RU, title='Patient', cols=2)
    st.markdown('#### Secondary Triage')
    idx = ALLOWED_V.index(current_V) if current_V in ALLOWED_V else 0
    with st.form('form_v', border=True):
        v_value = st.selectbox('Select Triage priority', ALLOWED_V, index=idx)
        submitted = st.form_submit_button('Submit')
    if submitted:
        # Write to column V (needs write scope) — optional; keep read-only if you prefer
        try:
            ws = gspread.authorize(Credentials.from_service_account_info(dict(st.secrets['gcp_service_account']), scopes=['https://www.googleapis.com/auth/spreadsheets','https://www.googleapis.com/auth/drive'])).open_by_key(SPREADSHEET_ID).worksheet(WORKSHEET_NAME)
            V_idx = col_letter_to_index('V')
            a1 = f"V{sheet_row}"
            ws.update_acell(a1, v_value)
        except Exception as e:
            st.error(f'บันทึกไม่สำเร็จ (สิทธิ์เขียน): {e}')
            st.stop()
        st.success('Saved. Final view (no form).')
        st.stop()

# default: edit1 (L–Q)
st.markdown('#### Treatment')
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
    try:
        ws = gspread.authorize(Credentials.from_service_account_info(dict(st.secrets['gcp_service_account']), scopes=['https://www.googleapis.com/auth/spreadsheets','https://www.googleapis.com/auth/drive'])).open_by_key(SPREADSHEET_ID).worksheet(WORKSHEET_NAME)
        headers = ws.row_values(1)
        updates = []
        for h, v in selections.items():
            if h in headers:
                col_idx = headers.index(h) + 1
                a1 = f"{chr(64+col_idx)}{sheet_row}" if col_idx<=26 else f"{chr(64+(col_idx-1)//26)}{chr(64+(col_idx-1)%26+1)}{sheet_row}"
                updates.append({'range': a1, 'majorDimension': 'ROWS', 'values': [[v]]})
        if updates:
            ws.spreadsheet.values_batch_update(body={'valueInputOption': 'RAW', 'data': updates})
        st.success('Saved L–Q. Continue to choose Priority (mode=edit2).')
    except Exception as e:
        st.error(f'บันทึกไม่สำเร็จ (สิทธิ์เขียน): {e}')