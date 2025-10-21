import json
from typing import Dict, List, Tuple
import pandas as pd
import streamlit as st
import gspread
from google.oauth2.service_account import Credentials
import requests

st.set_page_config(page_title="Secondary Triage", page_icon="⏱️", layout="centered")

# =========================
# CONFIG
# =========================
SPREADSHEET_ID = st.secrets["gsheets"]["spreadsheet_id"]
WORKSHEET_NAME = st.secrets["gsheets"]["worksheet_name"]
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

def get_client():
    info = dict(st.secrets["gcp_service_account"])
    pk = info.get("private_key", "").replace("\\n", "\n")
    info["private_key"] = pk
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)

def open_ws():
    gc = get_client()
    sh = gc.open_by_key(SPREADSHEET_ID)
    return sh.worksheet(WORKSHEET_NAME)

ALLOWED_V = ["Priority 1", "Priority 2", "Priority 3"]
YN = ["Yes", "No"]

# =============== GAS FUNCTIONS ===============
def gas_get_row(row: int):
    url = st.secrets["gas"]["webapp_url"]
    data = {"action": "get", "row": str(row)}
    r = requests.get(url, params=data, timeout=15)
    return r.json()

def gas_start_timer(row: int):
    url = st.secrets["gas"]["webapp_url"]
    data = {"action": "start_timer", "row": str(row)}
    r = requests.post(url, data=data, timeout=15)
    return r.json()

def gas_stop_timer(row: int):
    url = st.secrets["gas"]["webapp_url"]
    data = {"action": "stop_timer", "row": str(row)}
    r = requests.post(url, data=data, timeout=15)
    return r.json()

# =============== HELPERS ===============
def col_letter_to_index(letter): return sum((ord(c)-64)*26**i for i,c in enumerate(letter.upper()[::-1]))
def index_to_col_letter(idx):
    letters = ""
    while idx>0: idx, rem = divmod(idx-1,26); letters = chr(65+rem)+letters
    return letters

def increment_Z(ws, row:int):
    Z_idx = col_letter_to_index("Z")
    cell = f"{index_to_col_letter(Z_idx)}{row}"
    val = ws.acell(cell).value
    try: cur = int(val)
    except: cur = 0
    new = cur + 1
    ws.update_acell(cell, new)
    return new

# =============== COUNTDOWN COMPONENT ===============
def render_status_bar(status: str, remaining: int):
    def fmt_hms(s): h, r = divmod(int(max(s,0)),3600); m, s = divmod(r,60); return f"{h:02d}:{m:02d}:{s:02d}"
    if status=="stopped": st.info(f"⏸ Timer stopped at {fmt_hms(remaining)}")
    elif status=="expired": st.error("💀 Time up — คนไข้เสียชีวิตแล้ว")
    else: st.caption("⏳ Counting down…")

def render_countdown(origin_seconds:int, remaining:int, status:str="running"):
    import streamlit.components.v1 as components
    def fmt_hms(s): h,r=divmod(int(max(s,0)),3600); m,s=divmod(r,60); return f"{h:02d}:{m:02d}:{s:02d}"
    digits = fmt_hms(remaining)
    progress_value = max(0, (origin_seconds-remaining))
    progress_max = max(1, origin_seconds or 1)

    color = {"running":("#bae6fd","#0c4a6e","⏳ Running"),
             "stopped":("#fde68a","#92400e","⏸ Stopped"),
             "expired":("#fecaca","#7f1d1d","💀 Time up")}[status]

    if status!="running":
        components.html(f"""
        <div style="border:1px dashed #94a3b8;padding:12px;border-radius:12px;background:#f8fafc">
         <span style="font-size:0.8rem;background:{color[0]};color:{color[1]};border-radius:999px;padding:4px 10px;margin-right:10px">{color[2]}</span>
         <span style="font-weight:800;letter-spacing:1px;font-size:2.6rem">{digits}</span>
         <div style="margin-top:10px"><progress max="{progress_max}" value="{progress_value}" style="width:100%"></progress></div>
        </div>""",height=150); return

    components.html(f"""
    <div style="border:1px dashed #94a3b8;padding:12px;border-radius:12px;background:#f8fafc">
     <span style="font-size:0.8rem;background:{color[0]};color:{color[1]};border-radius:999px;padding:4px 10px;margin-right:10px">{color[2]}</span>
     <span id="digits" style="font-weight:800;letter-spacing:1px;font-size:2.6rem">{digits}</span>
     <div style="margin-top:10px"><progress id="pg" max="{progress_max}" value="{progress_value}" style="width:100%"></progress></div>
    </div>
    <script>
      (function(){{
        let remain={remaining},origin={origin_seconds};
        const d=document.getElementById('digits'),pg=document.getElementById('pg');
        function fmt(n){{return String(n).padStart(2,'0');}}
        function render(){{
          let s=Math.max(0,Math.floor(remain));
          let h=Math.floor(s/3600),m=Math.floor((s%3600)/60),ss=s%60;
          d.textContent=`${{fmt(h)}}:${{fmt(m)}}:${{fmt(ss)}}`;
          if(pg){{pg.value=Math.min(origin,origin-s);pg.max=origin;}}
        }}
        render();
        const i=setInterval(()=>{{remain--;render();if(remain<=0)clearInterval(i);}},1000);
      }})();
    </script>""",height=150)

# =============== MAIN ===============
qp = st.query_params
row = int(qp.get("row", 1))
ws = open_ws()
sheet_row = row + 1

origin_seconds=0; t0=0; end=0
try:
    g = gas_get_row(row)
    origin_seconds=int(g.get("timer_seconds",0))
    t0=int(g.get("t0_epoch",0)); end=int(g.get("end_epoch",0))
    if origin_seconds>0 and end==0:
        s = gas_start_timer(row)
        t0=int(s.get("t0_epoch",t0)); end=int(s.get("end_epoch",end))
except Exception as e:
    st.warning(f"GAS error: {e}")

now=int(pd.Timestamp.utcnow().timestamp())
remaining=max(0,end-now if end else 0)

if "timer_stopped" not in st.session_state: st.session_state["timer_stopped"]=False
if "expired_processed" not in st.session_state: st.session_state["expired_processed"]=False

# หมดเวลา
if remaining<=0 and not st.session_state["expired_processed"]:
    increment_Z(ws,sheet_row)
    st.session_state["expired_processed"]=True
    st.session_state["timer_stopped"]=True
    st.error("คนไข้เสียชีวิตแล้ว")

ui_status="expired" if st.session_state["expired_processed"] else ("stopped" if st.session_state["timer_stopped"] else "running")
render_status_bar(ui_status,remaining)
render_countdown(origin_seconds,remaining,ui_status)

disabled = st.session_state["timer_stopped"] or st.session_state["expired_processed"]

st.subheader("Treatment")
v = st.selectbox("Select Triage priority", ALLOWED_V, disabled=disabled)
if st.button("Submit Treatment", disabled=disabled):
    # update sheet + stop timer
    V_idx=col_letter_to_index("V")
    ws.update_acell(f"{index_to_col_letter(V_idx)}{sheet_row}", v)
    gas_stop_timer(row)
    st.session_state["timer_stopped"]=True
    st.toast("⏸ Timer stopped.")
    st.rerun()
