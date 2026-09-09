"""Community Cloud entry point: Streamlit UI and same-origin auction API."""
from pathlib import Path

import streamlit as st

from roly.auction_http import lifespan, routes

app = st.App(str(Path(__file__).with_name("app.py")), routes=routes(), lifespan=lifespan)
