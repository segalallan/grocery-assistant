"""Visual tweaks Streamlit has no setting for. Call apply_theme() once, right after set_page_config.

Replaces Streamlit's running-man indicator with an animated shopping cart that rolls along
while groceries drop into it, and stops the page from greying out while it refreshes.
"""
import base64

import streamlit as st

ACCENT = "#2E7D5B"   # cart colour
SIZE = 96            # loader width in px

# Self-contained animated SVG (its CSS animations keep running when used as a background image).
CART_SVG = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 80" width="120" height="80">
<style>
  .road {{ stroke:#c9d1cc; stroke-width:2; stroke-dasharray:6 6; animation: road .5s linear infinite; }}
  @keyframes road {{ to {{ stroke-dashoffset: 12; }} }}
  .cart {{ animation: bob .5s ease-in-out infinite alternate; transform-origin: 60px 60px; }}
  @keyframes bob {{ from {{ transform: translateY(0); }} to {{ transform: translateY(-1.5px); }} }}
  .wheel {{ animation: spin .5s linear infinite; }}
  .w1 {{ transform-origin: 44px 66px; }} .w2 {{ transform-origin: 80px 66px; }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
  .item {{ opacity:0; animation: drop 1.8s ease-in infinite; }}
  .i2 {{ animation-delay: .6s; }} .i3 {{ animation-delay: 1.2s; }}
  @keyframes drop {{
    0%   {{ opacity:0; transform: translateY(-22px); }}
    15%  {{ opacity:1; }}
    45%  {{ opacity:1; transform: translateY(0); }}
    70%  {{ opacity:1; transform: translateY(0); }}
    85%,100% {{ opacity:0; transform: translateY(0); }}
  }}
</style>
<line class="road" x1="0" y1="75" x2="120" y2="75"/>
<g class="cart">
  <!-- groceries (drawn behind the basket front) -->
  <g class="item i1"><circle cx="52" cy="34" r="6" fill="#E4572E"/><path d="M52 28 q2 -4 5 -4" stroke="#3B7A2A" stroke-width="2" fill="none"/></g>
  <g class="item i2"><rect x="61" y="24" width="10" height="16" rx="1.5" fill="#4A90D9"/><path d="M61 24 l5 -5 l5 5z" fill="#DDE9F7"/></g>
  <g class="item i3"><ellipse cx="78" cy="34" rx="8" ry="5" fill="#E8B04B"/><path d="M73 32 q5 -3 10 0" stroke="#C58B26" stroke-width="1.2" fill="none"/></g>
  <!-- handle + basket -->
  <path d="M14 18 h10 l8 30" stroke="{ACCENT}" stroke-width="4" fill="none" stroke-linecap="round" stroke-linejoin="round"/>
  <path d="M28 30 h68 l-8 26 h-52 z" fill="#ffffff" stroke="{ACCENT}" stroke-width="4" stroke-linejoin="round"/>
  <path d="M44 32 l2 22 M60 32 v22 M76 32 l-2 22 M32 43 h60" stroke="{ACCENT}" stroke-width="1.6" opacity=".55"/>
  <path d="M36 56 l2 6 h44" stroke="{ACCENT}" stroke-width="3" fill="none" stroke-linecap="round"/>
  <!-- wheels -->
  <g class="wheel w1"><circle cx="44" cy="66" r="6" fill="#fff" stroke="#333" stroke-width="3"/><path d="M44 61 v10 M39 66 h10" stroke="#333" stroke-width="1.5"/></g>
  <g class="wheel w2"><circle cx="80" cy="66" r="6" fill="#fff" stroke="#333" stroke-width="3"/><path d="M80 61 v10 M75 66 h10" stroke="#333" stroke-width="1.5"/></g>
</g>
</svg>"""

_CART_URI = "data:image/svg+xml;base64," + base64.b64encode(CART_SVG.encode()).decode()

_CSS = f"""
<style>
/* Hide Streamlit's running-man icon and "Stop" button. */
[data-testid="stStatusWidget"] {{ visibility: hidden; }}

/* While the app is working, show the rolling cart in a card in the middle of the screen. */
.stApp:has([data-testid="stStatusWidget"])::after {{
  content: "";
  position: fixed; top: 50%; left: 50%; transform: translate(-50%, -50%);
  width: {SIZE + 24}px; height: {int(SIZE * 2 / 3) + 16}px; z-index: 1000002;
  background: #fff url("{_CART_URI}") center / {SIZE}px auto no-repeat;
  border-radius: 14px; box-shadow: 0 4px 18px rgba(0,0,0,.12);
  animation: pantry-fade-in .25s ease-out;
}}
@keyframes pantry-fade-in {{ from {{ opacity: 0; }} to {{ opacity: 1; }} }}

/* Pantry action buttons stay pinned to the top while scrolling the list.
   (The sticky element must be the wrapper Streamlit puts around the row.) */
[data-testid="stLayoutWrapper"]:has(> .st-key-pantry_actions) {{
  position: sticky; top: 3.75rem; z-index: 999;
  background: var(--background-color, #fff);
  box-shadow: 0 6px 8px -8px rgba(0,0,0,.25);
}}
.st-key-pantry_actions {{ padding: .5rem 0; }}
/* Anything scrolled into view (e.g. by keyboard) lands below the pinned bar, not under it. */
[data-testid="stForm"] [data-testid="stCheckbox"] {{ scroll-margin-top: 8rem; }}

/* Don't grey out the page while it refreshes. */
[data-stale="true"] {{ opacity: 1 !important; transition: none !important; }}
</style>
"""


def apply_theme():
    st.markdown(_CSS, unsafe_allow_html=True)


def cart_spinner_html(text: str = "") -> str:
    """The same cart, for use inside the page (e.g. while Quick Add talks to the AI)."""
    label = f'<div style="margin-top:4px;color:#555;font-size:14px;">{text}</div>' if text else ""
    return (f'<div style="text-align:center;padding:8px 0;"><img src="{_CART_URI}" width="{SIZE}" '
            f'alt="Loading">{label}</div>')
