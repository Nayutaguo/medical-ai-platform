"""WSGI entry point for production servers."""

from medical_ai.api import create_app
from medical_ai.config import Settings

# Production posture is explicit here rather than inferred from a possibly
# missing environment variable. Settings validation refuses anonymous
# analytics, insecure cookies, or the unscoped MCP compatibility adapter.
app = create_app(Settings(app_environment="production"))
