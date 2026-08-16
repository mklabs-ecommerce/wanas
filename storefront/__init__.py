"""The customer-facing web store's API -- a thin FastAPI router over
backend/services, matching the contract in web/BACKEND-FOR-FRONTEND.md.

Reuses the same database as the WhatsApp bot and the dashboard; it does not
change backend/.
"""
