"""URL classification, normalization, and official-documentation helpers.

All functions are stateless module-level callables.
"""

import re
from urllib.parse import urlsplit, urlunsplit

from web_scraper.research.constants import SOURCE_TIERS, TECH_DOC_URLS

# Subdomains that strongly indicate official documentation
_DOC_SUBDOMAINS: tuple[str, ...] = (
    "docs.",
    "developer.",
    "developers.",
    "documentation.",
    "dev.",
    "api.",
    "reference.",
    "learn.",
    "guide.",
)

# URL path segments that strongly indicate official documentation
_DOC_PATH_PATTERNS: tuple[str, ...] = (
    "/docs/",
    "/doc/",
    "/reference/",
    "/api/",
    "/tutorial/",
    "/manual/",
    "/en/stable/",
    "/en/latest/",
    "/changelog/",
)

# Known tech company domains for path-based tier-1 detection
_KNOWN_TECH_DOMAINS: tuple[str, ...] = (
    "python.org",
    "djangoproject.com",
    "fastapi.tiangolo.com",
    "palletsprojects.com",
    "sqlalchemy.org",
    "pydantic.dev",
    "mozilla.org",
    "reactjs.dev",
    "react.dev",
    "nextjs.org",
    "vuejs.org",
    "angular.io",
    "npmjs.com",
    "nodejs.org",
    "typescriptlang.org",
    "docker.com",
    "kubernetes.io",
    "amazon.com",
    "amazonaws.com",
    "google.com",
    "microsoft.com",
    "github.com",
    "openai.com",
    "anthropic.com",
    "huggingface.co",
    "pytorch.org",
    "tensorflow.org",
    "rust-lang.org",
    "go.dev",
    "kotlinlang.org",
    "apple.com",
    "oracle.com",
    "hashicorp.com",
    "postgresql.org",
    "mongodb.com",
    "redis.io",
)


def classify_source_tier(url: str) -> int:
    """Return authority tier 1–5 for *url* (1 = highest, 5 = lowest/unknown).

    Tier 1: Official institutions, primary documents, official tech docs
    Tier 2: Academic research (.edu, major preprint/journal servers)
    Tier 3: Established major media outlets
    Tier 4: Recognized research orgs & reference sites
    Tier 5: Corporate blogs, opinion sites, and everything else
    """
    try:
        parsed = urlsplit(url)
        hostname = parsed.netloc.lower()
        path = parsed.path.lower()
        if hostname.startswith("www."):
            hostname = hostname[4:]

        for tier, domains in SOURCE_TIERS.items():
            if hostname in domains:
                return tier

        # TLD-based fallbacks
        if any(hostname.endswith(f".{tld}") for tld in ("gov", "mil", "int")):
            return 1
        if hostname.endswith(".edu"):
            return 2
        if hostname.endswith(".ac.uk") or hostname.endswith(".ac."):
            return 2

        # Pattern-based official documentation detection
        if any(hostname.startswith(sub) for sub in _DOC_SUBDOMAINS):
            return 1

        if any(pat in path for pat in _DOC_PATH_PATTERNS):
            if any(hostname.endswith(d) for d in _KNOWN_TECH_DOMAINS):
                return 1
            return 4  # Unknown domain with /docs/ path — treat as reference

    except Exception:  # noqa: S110
        pass
    return 5


def get_official_doc_urls_for_query(query: str) -> list[str]:
    """Return official documentation URLs relevant to the query.

    Scans the query for technology keywords and returns the corresponding
    official doc base URLs so they can be injected as priority scrape targets.
    """
    query_lower = query.lower()
    urls: list[str] = []
    seen: set[str] = set()
    for keyword, doc_urls in TECH_DOC_URLS.items():
        if re.search(rf"\b{re.escape(keyword)}\b", query_lower):
            for u in doc_urls:
                if u not in seen:
                    urls.append(u)
                    seen.add(u)
    return urls


def normalize_result_url(url: str) -> str:
    """Normalize URLs so equivalent search results dedupe reliably."""
    parsed = urlsplit(url)
    normalized_path = parsed.path.rstrip("/") or "/"
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            normalized_path,
            parsed.query,
            "",
        )
    )


def extract_result_domain(url: str) -> str:
    """Extract a normalized domain from a result URL."""
    return urlsplit(url).netloc.lower()
