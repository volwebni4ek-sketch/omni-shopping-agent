"""Human-in-the-loop (HITL) HTML report renderer and FastAPI web dashboard."""

from omni_agent.ui.dashboard_app import app, create_app
from omni_agent.ui.report_renderer import render_standalone_hitl_html

__all__ = [
    "app",
    "create_app",
    "render_standalone_hitl_html",
]
