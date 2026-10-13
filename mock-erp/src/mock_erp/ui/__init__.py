"""Server-rendered pages under /ui.

Both modules add routes to the same blueprint when they are imported, so both are
imported here. The app only needs `views.bp`.
"""

from mock_erp.ui import journal_views, views

__all__ = ["journal_views", "views"]
