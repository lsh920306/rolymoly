"""Community Cloud entry point: Streamlit UI and same-origin auction API."""
from pathlib import Path

import streamlit as st
from starlette.middleware import Middleware

from roly.auction_http import TransportDiagnostics, lifespan, routes

app = st.App(str(Path(__file__).with_name("app.py")), routes=routes(), lifespan=lifespan,
             middleware=[Middleware(TransportDiagnostics)])
