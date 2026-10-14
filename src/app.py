"""Entry point (Streamlit runs this file).

It runs the real app (pantry_app.py) and catches anything unexpected: the user sees a
friendly message, the error is saved, and the admin gets one email per distinct error per hour.
"""
import html
import logging
import os
import traceback
from pathlib import Path

import streamlit as st
from streamlit.runtime.scriptrunner_utils.exceptions import ScriptControlException

APP_FILE = Path(os.environ.get("PANTRY_APP_FILE") or Path(__file__).with_name("pantry_app.py"))
log = logging.getLogger("smart_pantry")


@st.cache_resource
def _error_db():
    from db import Database
    return Database()


def _signature(exc: BaseException) -> str:
    """Same bug → same signature: error type + where it was raised in our code."""
    frames = [f for f in traceback.extract_tb(exc.__traceback__) if "site-packages" not in f.filename]
    where = f"{Path(frames[-1].filename).name}:{frames[-1].lineno}" if frames else "?"
    return f"{type(exc).__name__}@{where}"


def _report(exc: BaseException):
    from notifications import admin_emails, send_admin_email
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    user = st.session_state.get("email") or "(not logged in)"
    sig = _signature(exc)
    log.error("Unhandled error %s\n%s", sig, tb)
    try:
        notify = _error_db().log_error(sig, f"{type(exc).__name__}: {exc}", tb, user)
    except Exception:
        log.exception("Could not save the error")
        notify = True
    if notify:
        send_admin_email(f"Smart Pantry error: {sig}",
                         f"<p><b>User:</b> {html.escape(user)}</p><p><b>Error:</b> {html.escape(str(exc))}</p>"
                         f"<pre style='font-size:12px'>{html.escape(tb[-6000:])}</pre>")
    return (user or "").lower() in admin_emails()


try:
    code = compile(APP_FILE.read_text(encoding="utf-8"), str(APP_FILE), "exec")
    exec(code, {"__name__": "__main__", "__file__": str(APP_FILE)})
except ScriptControlException:
    raise  # st.rerun() / st.stop() are normal control flow, not errors
except Exception as exc:  # noqa: BLE001 — this is the last line of defence
    is_admin = _report(exc)
    st.error("Something went wrong — we've been notified and will look into it. Please try again in a moment.\n\n"
             "משהו השתבש — קיבלנו התראה ונבדוק את זה. נסו שוב בעוד רגע.")
    if st.button("↻ Reload"):
        st.rerun()
    if is_admin:
        st.exception(exc)
