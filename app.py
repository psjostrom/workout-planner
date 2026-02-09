import streamlit as st
import datetime as dt
import requests
import statistics
import re
import math
from enum import Enum, auto
from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Optional

# ==========================================
#  USER CONFIGURATION
# ==========================================
RACE_NAME = "EcoTrail"
RACE_DATE = dt.date(2026, 6, 13)
RACE_DISTANCE_KM = 16
PLAN_PREFIX = "eco16"
PLAN_LENGTH_WEEKS = 18
CURRENT_LONG_RUN_KM = 8
WEEKLY_RUNS = 3

# T1D SETTINGS
USER_LTHR = 169
CRASH_DROP_RATE = -3.0
SPIKE_RISE_RATE = 3.0
DEFAULT_CARBS_G = 10

# SYSTEM
API_BASE = "https://intervals.icu/api/v1"

# ==========================================
#  DATA STRUCTURES
# ==========================================
class Intensity(Enum):
    RECOVERY = auto()
    EASY = auto()
    LONG = auto()
    QUALITY = auto()

@dataclass
class T1DProtocol:
    pump_status: str
    carb_intake: str
    notes: List[str] = field(default_factory=list)

@dataclass(frozen=True)
class WorkoutEvent:
    start_date_local: dt.datetime
    name: str
    description: str
    external_id: str

# BASELINE STRATEGIES
DEFAULT_PROTOCOLS: Dict[Intensity, T1DProtocol] = {
    Intensity.RECOVERY: T1DProtocol("PUMP OFF", "10g every 10 minutes", ["Bonus run."]),
    Intensity.EASY:     T1DProtocol("PUMP OFF", "10g every 10 minutes", ["Steady state."]),
    Intensity.LONG:     T1DProtocol("PUMP OFF", "10g every 10 minutes", ["Key session."]),
    Intensity.QUALITY:  T1DProtocol("PUMP OFF", "10g every 10 minutes", ["High intensity."]),
}

# ==========================================
#  HELPER FUNCTIONS (Backend Logic)
# ==========================================

def fetch_recent_activities(api_key):
    url = f"{API_BASE}/athlete/0/activities"
    today = dt.date.today()
    start_date = today - dt.timedelta(days=45)
    params = {"oldest": start_date.isoformat(), "newest": today.isoformat()}
    try:
        resp = requests.get(url, params=params, auth=("API_KEY", api_key))
        if resp.status_code == 200: return resp.json()
    except: pass
    return []

def fetch_streams(api_key, activity_id):
    url = f"{API_BASE}/activity/{activity_id}/streams"
    keys = ["time", "bloodglucose", "glucose", "ga_smooth"]
    try:
        resp = requests.get(url, params={"keys": ",".join(keys)}, auth=("API_KEY", api_key))
        if resp.status_code == 200: return resp.json()
    except: pass
    return []

def fetch_events_for_date(api_key, date_str):
    url = f"{API_BASE}/athlete/0/events"
    params = {"oldest": date_str, "newest": date_str}
    try:
        resp = requests.get(url, params=params, auth=("API_KEY", api_key))
        if resp.status_code == 200: return resp.json()
    except: pass
    return []

def get_last_fuel_amount(api_key, activities):
    if not activities: return DEFAULT_CARBS_G
    sorted_runs = sorted(activities, key=lambda x: x['start_date_local'], reverse=True)
    last_run = sorted_runs[0]

    st.info(f"Analyzing last run: **{last_run['name']}**")

    # 1. Direct Description
    description = last_run.get('description')
    if description:
        match = re.search(r"FUEL:\s*(\d+)g", description, re.IGNORECASE)
        if match: return int(match.group(1))

    # 2. Calendar Lookup
    run_date_str = last_run['start_date_local'].split('T')[0]
    events = fetch_events_for_date(api_key, run_date_str)

    for event in events:
        e_name = event.get('name', '')
        if PLAN_PREFIX.lower() in e_name.lower():
            full_text = (event.get('description', '') + " " + e_name)
            match = re.search(r"FUEL:\s*(\d+)g", full_text, re.IGNORECASE)
            if match:
                st.caption(f"Found history in Calendar Plan ({run_date_str}): {match.group(1)}g")
                return int(match.group(1))

    return DEFAULT_CARBS_G

def calculate_trend(api_key, activities):
    relevant_runs = []
    keywords = ["Sun", "LR", "Long", "Tue", "Tempo", "Hill"]

    for a in activities:
        name = a.get('name', '').lower()
        if PLAN_PREFIX.lower() in name:
            relevant_runs.append(a)

    if not relevant_runs:
        return 0.0, DEFAULT_CARBS_G, False

    current_fuel = get_last_fuel_amount(api_key, relevant_runs)

    drop_rates = []
    for run in relevant_runs[:3]:
        streams = fetch_streams(api_key, run['id'])
        if not streams or not isinstance(streams, list): continue

        g_data, t_data = None, None
        for s in streams:
            if not isinstance(s, dict): continue
            if s.get('type') == 'time': t_data = s.get('data')
            if s.get('type') in ['bloodglucose', 'glucose', 'ga_smooth']: g_data = s.get('data')

        if g_data and t_data and len(t_data) > 1:
            delta = g_data[-1] - g_data[0]
            duration_hr = (t_data[-1] - t_data[0]) / 3600
            if duration_hr > 0.2:
                drop_rates.append(delta / duration_hr)

    avg_trend = statistics.mean(drop_rates) if drop_rates else 0.0
    return avg_trend, current_fuel, True

# ==========================================
#  GENERATION FUNCTIONS (Visuals & Logic)
# ==========================================

def format_iso_local(d: dt.datetime) -> str: return d.replace(microsecond=0).isoformat()

def bpm_to_lthr_pct(bpm_range: Tuple[int, int], lthr: int) -> str:
    min_pct = math.floor((bpm_range[0] / lthr) * 100)
    max_pct = math.ceil((bpm_range[1] / lthr) * 100)
    return f"{min_pct}-{max_pct}% LTHR"

def format_step(duration_str: str, bpm_range: Tuple[int, int], note_prefix: str = "") -> str:
    pct_str = bpm_to_lthr_pct(bpm_range, USER_LTHR)
    step_core = f"{duration_str} {pct_str} ({bpm_range[0]}-{bpm_range[1]} bpm)"
    if note_prefix:
        return f"{note_prefix} {step_core}"
    return step_core

def format_workout_text(display_title: str, steps: List[str], repeats: int = 1) -> str:
    """
    Constructs the workout description.
    Uses 'Main set Nx' syntax if repeats > 1 to group intervals correctly.
    """
    lines = [display_title, "", "Warmup", "- 10m 66-77% LTHR (113-131 bpm)", ""]

    if repeats > 1:
        lines.append(f"Main set {repeats}x")
    else:
        lines.append("Main set")

    lines.extend([f"- {s}" for s in steps])

    lines.append("")
    lines.append("Cooldown")
    lines.append("- 5m 56-66% LTHR (95-112 bpm)")
    return "\n".join(lines) + "\n"

def generate_plan(final_fuel_g):
    events = []
    cutoff_date = dt.date.today()

    race_week_monday = RACE_DATE - dt.timedelta(days=RACE_DATE.weekday())
    plan_start = race_week_monday - dt.timedelta(weeks=PLAN_LENGTH_WEEKS - 1)

    for week in range(1, PLAN_LENGTH_WEEKS + 1):
        week_start = plan_start + dt.timedelta(days=(week - 1) * 7)
        strategy_text = f"PUMP OFF - FUEL: {final_fuel_g}g every 10 minutes"

        # 1. TUE QUALITY
        if week % 2 != 0:
            name = f"W{week:02d} Tue Tempo {PLAN_PREFIX}"
            reps = 3 + int(week/18*3)
            steps = [format_step("8m", (155, 168)), format_step("2m", (95, 112))]
            desc = format_workout_text(strategy_text, steps, repeats=reps)
        else:
            name = f"W{week:02d} Tue Hills {PLAN_PREFIX}"
            reps = 6
            steps = [format_step("2m", (155, 168), "Uphill"), format_step("2m", (95, 112), "Downhill")]
            desc = format_workout_text(strategy_text, steps, repeats=reps)

        d_date = week_start + dt.timedelta(days=1)
        if d_date >= cutoff_date:
            events.append(WorkoutEvent(dt.datetime.combine(d_date, dt.time(12,0)), name, desc, f"{PLAN_PREFIX}-tue-{week}"))

        # 2. THU EASY
        name = f"W{week:02d} Thu Easy {PLAN_PREFIX}"
        dur = 40 + int((week/18)*20)
        desc = format_workout_text(strategy_text, [format_step(f"{dur}m", (113, 131))], repeats=1)
        d_date = week_start + dt.timedelta(days=3)
        if d_date >= cutoff_date:
            events.append(WorkoutEvent(dt.datetime.combine(d_date, dt.time(12,0)), name, desc, f"{PLAN_PREFIX}-thu-{week}"))

        # 3. SAT BONUS (OPTIONAL) <-- HÄR VAR DET SOM SAKNADES
        name = f"W{week:02d} Sat Bonus (Optional) {PLAN_PREFIX}"
        desc = format_workout_text(strategy_text, [format_step("30m", (95, 112))], repeats=1)
        d_date = week_start + dt.timedelta(days=5) # Måndag + 5 = Lördag
        if d_date >= cutoff_date:
            events.append(WorkoutEvent(dt.datetime.combine(d_date, dt.time(12,0)), name, desc, f"{PLAN_PREFIX}-sat-{week}"))

        # 4. SUN LONG
        if week > (PLAN_LENGTH_WEEKS - 2):
             km = int(RACE_DISTANCE_KM * 0.5)
             suffix = " [TAPER]"
        elif week == (PLAN_LENGTH_WEEKS - 2):
             km = RACE_DISTANCE_KM
             suffix = " [RACE TEST]"
        elif week % 4 == 0:
             km = CURRENT_LONG_RUN_KM
             suffix = " [RECOVERY]"
        else:
             km = CURRENT_LONG_RUN_KM + int(((RACE_DISTANCE_KM - CURRENT_LONG_RUN_KM)/15) * (week-1))
             if km > RACE_DISTANCE_KM: km = RACE_DISTANCE_KM
             suffix = ""

        name = f"W{week:02d} Sun LR ({km}km){suffix} {PLAN_PREFIX}"
        desc = format_workout_text(f"{strategy_text} (Trail)", [format_step(f"{km}km", (120, 145))], repeats=1)
        d_date = week_start + dt.timedelta(days=6)
        if d_date >= cutoff_date:
            events.append(WorkoutEvent(dt.datetime.combine(d_date, dt.time(12,0)), name, desc, f"{PLAN_PREFIX}-sun-{week}"))

    return events

def execute_update(api_key, events):
    start_str = format_iso_local(dt.datetime.now())
    end_str = format_iso_local(dt.datetime.now() + dt.timedelta(days=365))
    url = f"{API_BASE}/athlete/0/events"
    params = {"oldest": start_str, "newest": end_str, "category": "WORKOUT"}

    try:
        requests.delete(url, params=params, auth=("API_KEY", api_key))
    except: pass

    url_post = f"{API_BASE}/athlete/0/events/bulk?upsert=true"
    payload = [{"category": "WORKOUT", "type": "Run", "start_date_local": format_iso_local(ev.start_date_local), "name": ev.name, "description": ev.description, "external_id": ev.external_id} for ev in events]
    requests.post(url_post, json=payload, auth=("API_KEY", api_key))
    return len(events)

# ==========================================
#  STREAMLIT APP LAYOUT
# ==========================================

st.set_page_config(page_title="EcoTrail Planner", page_icon="🏃")
st.title("🏃 EcoTrail T1D Planner")

api_key = None
if "INTERVALS_API_KEY" in st.secrets:
    api_key = st.secrets["INTERVALS_API_KEY"]
    st.sidebar.success("API Key loaded from Secrets 🔒")
else:
    with st.sidebar:
        api_key = st.text_input("Intervals.icu API Key", type="password")
        st.caption("Enter key manually or set 'INTERVALS_API_KEY' in secrets.")

if not api_key:
    st.warning("Please configure secrets or enter your API Key to start.")
    st.stop()

if 'analysis_complete' not in st.session_state:
    st.session_state.analysis_complete = False
if 'ready_to_upload' not in st.session_state:
    st.session_state.ready_to_upload = False

# STEP 1
if st.button("🔍 1. Analyze History & Trends"):
    with st.spinner("Fetching data from Intervals.icu..."):
        activities = fetch_recent_activities(api_key)
        avg_trend, current_fuel, found_data = calculate_trend(api_key, activities)
        st.session_state.avg_trend = avg_trend
        st.session_state.current_fuel = current_fuel
        st.session_state.found_data = found_data
        st.session_state.analysis_complete = True
        st.session_state.ready_to_upload = False

# STEP 2
if st.session_state.analysis_complete:
    st.divider()
    st.subheader("Diabetes Strategy Analysis")
    col1, col2, col3 = st.columns(3)
    col1.metric("Trend (mmol/L/h)", f"{st.session_state.avg_trend:+.1f}")
    col2.metric("Current Strategy", f"{st.session_state.current_fuel}g / 10m")

    trend = st.session_state.avg_trend
    current = st.session_state.current_fuel
    suggested = current

    if trend < CRASH_DROP_RATE:
        suggested = current + 5
        st.error(f"📉 CRASH DETECTED (< {CRASH_DROP_RATE}). Suggesting increase.")
    elif trend > SPIKE_RISE_RATE:
        if current > DEFAULT_CARBS_G:
            suggested = current - 5
            st.warning(f"📈 SPIKE DETECTED (> {SPIKE_RISE_RATE}). Suggesting decrease.")
        else:
            st.info("📈 Spike detected, but already at baseline (10g). No change.")
    else:
        st.success("✅ Trend is stable. Maintaining strategy.")

    col3.metric("Suggested", f"{suggested}g / 10m")

    st.divider()
    st.subheader("👇 Make a Decision")
    c1, c2, c3 = st.columns([1,1,2])

    if c1.button(f"Accept ({suggested}g)", type="primary"):
        st.session_state.final_fuel = suggested
        st.session_state.ready_to_upload = True
    if c2.button(f"Keep ({current}g)"):
        st.session_state.final_fuel = current
        st.session_state.ready_to_upload = True
    with c3:
        manual = st.number_input("Or Manual Override (g)", min_value=0, value=suggested, step=1)
        if st.button("Apply Manual"):
            st.session_state.final_fuel = manual
            st.session_state.ready_to_upload = True

# STEP 3
if st.session_state.ready_to_upload:
    st.divider()
    st.header(f"🚀 Updating Plan: {st.session_state.final_fuel}g every 10 min")
    with st.spinner("Deleting old future workouts and uploading new ones..."):
        new_events = generate_plan(st.session_state.final_fuel)
        count = execute_update(api_key, new_events)
        st.success(f"Done! {count} workouts updated in your calendar.")
        st.balloons()
        st.session_state.ready_to_upload = False
        st.session_state.analysis_complete = False