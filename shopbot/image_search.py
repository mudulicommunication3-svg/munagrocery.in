"""Product image lookup for MUNNA MOBILE & Grocery.

REPLACED: the old Google Custom Search (Custom Search JSON API) logic was
removed because the project's Google Cloud project kept returning 403
(Custom Search not enabled / quota). Image lookup now goes straight to
JioMart's own catalog API, extracted from the working project
``jm1.8.0_3.py`` - see ``jiomart_search`` for the endpoint/header details.

Public API kept identical so ``admin_handler.py`` needed no changes:

    fetch_images(query, start_index=1) -> List[str]   up to 5 image URLs
    search_configured()                 -> bool        always True now
    reset_service()                     -> None        no-op
    MAX_START_INDEX, NUM_PER_PAGE       -> int         pagination constants

``fetch_images`` never raises: network failures, HTTP errors and malformed
responses are logged and yield an empty list, so the admin flow can offer a
retry / skip path instead of crashing (same contract as before).
"""

from __future__ import annotations

from typing import Any, List

import jiomart_search

# Re-exported for admin_handler (imported from here before the rewrite).
NUM_PER_PAGE = jiomart_search.NUM_PER_PAGE
MAX_START_INDEX = jiomart_search.MAX_START_INDEX


def fetch_images(query: Any, start_index: int = 1) -> List[str]:
    """Return up to 5 JioMart image URLs for ``query`` (1-based paging).

    Results come from the first pincode in the fallback list where the
    product is in stock (753003 -> 753001 -> 751010). Returns [] on failure.
    """
    return jiomart_search.collect_images(query, start_index=start_index)


def search_configured() -> bool:
    """Always True - the JioMart catalog API needs no API key."""
    return True


def reset_service() -> None:
    """No-op kept for API compatibility (tests / credential changes)."""


__all__ = [
    "MAX_START_INDEX",
    "NUM_PER_PAGE",
    "fetch_images",
    "reset_service",
    "search_configured",
]
