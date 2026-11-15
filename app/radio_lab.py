"""/radio-lab — Communications & Radio Science Lab.

Simulated preview of the planned 24/7 experiment: a BirdNET event on the home
Pi4 is broadcast over a 4-node Meshtastic mesh, and (next phase) over APRS
to an FT65 ~3 mi away. All animation and numbers are client-side and
simulated; no DB reads, so this page costs the Pi almost nothing.
"""
from datetime import datetime

from flask import Blueprint, render_template

bp = Blueprint("radio_lab", __name__)


@bp.route("/radio-lab")
def radio_lab():
    page_generated = datetime.now().strftime("%A, %B %d, %Y — %I:%M %p")
    return render_template("radio_lab.html", page_generated=page_generated)
