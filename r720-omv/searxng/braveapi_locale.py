# SPDX-License-Identifier: AGPL-3.0-or-later
"""Brave Search API engine that honours the search locale.

Mounted read-only over the image as ``searx/engines/braveapi_locale.py`` (see
compose.yaml) and enabled from the server-side settings.yml as "brave api".

Why not upstream's ``braveapi`` engine:

- It sends no ``country`` or ``search_lang``, so the API answers with its
  defaults (country=US). This instance defaults to en-GB and every scraper
  engine sends a UK region, so upstream's engine would be the one US-skewed
  source in the mix. Here the SearXNG locale is mapped onto Brave's parameters.
- It sends the time filter as ``time_range``; Brave's documented parameter is
  ``freshness`` (pd/pw/pm/py).
- It needs the key in settings.yml. This one reads ``BRAVE_API_KEY`` from the
  environment, which Komodo fills from the secret ``[[BRAVE_API_KEY]]``
  variable, so the key is in neither the repo nor the server's config files.

Why the API at all: since 2026-11-16 search.brave.com answers this IP's scraper
requests with 429 on every query, while the same image and settings via the
VPN got 5/5. The API is paid ($5 free credit/month, ~1,000 requests), so the
engine is ``disabled: true`` in settings.yml: clients that send no preferences
(OpenWebUI, ZeroClaw, PriceBuddy) never call it, and it is switched on per
browser in Preferences -> Engines. Keep a usage limit set in the Brave
dashboard so the card can never be charged.
"""

import os
from urllib.parse import urlencode

from searx.engines.brave import parse_search_result
from searx.result_types import EngineResults

about = {
    "website": "https://api.search.brave.com/",
    "wikidata_id": None,
    "official_api_documentation": "https://api-dashboard.search.brave.com/api-reference/web/search/get",
    "use_official_api": True,
    "require_api_key": True,
    "results": "JSON",
}

categories = ["general", "web"]
paging = True
max_page = 10  # API offset is 0-9
safesearch = True
time_range_support = True

base_url = "https://api.search.brave.com/res/v1/web/search"
results_per_page = 20  # API maximum

# A missing Komodo variable reaches the container as the literal "[[NAME]]".
api_key = os.environ.get("BRAVE_API_KEY", "").strip()
if api_key.startswith("[["):
    api_key = ""

safesearch_map = {0: "off", 1: "moderate", 2: "strict"}
freshness_map = {"day": "pd", "week": "pw", "month": "pm", "year": "py"}

# Allowed values from the API reference (2026-11-18). Anything else is left
# out rather than sent, so an unsupported locale cannot get the request
# rejected.
COUNTRIES = {
    "AR", "AU", "AT", "BE", "BR", "CA", "CL", "DK", "FI", "FR", "DE", "GR", "HK",
    "IN", "ID", "IT", "JP", "KR", "MY", "MX", "NL", "NZ", "NO", "CN", "PL", "PT",
    "PH", "RU", "SA", "ZA", "ES", "SE", "CH", "TW", "TR", "GB", "US",
}
SEARCH_LANGS = {
    "ar", "eu", "bn", "bg", "ca", "zh-hans", "zh-hant", "hr", "cs", "da", "nl",
    "en", "en-gb", "et", "fi", "fr", "gl", "de", "el", "gu", "he", "hi", "hu",
    "is", "it", "ja", "kn", "ko", "lv", "lt", "ms", "ml", "mr", "nb", "pl",
    "pt-br", "pt-pt", "pa", "ro", "ru", "sr", "sk", "sl", "es", "sv", "ta", "te",
    "th", "tr", "uk", "vi",
}


def setup(_engine_settings) -> bool:
    if not api_key:
        logger.error("BRAVE_API_KEY is not set; brave api engine not loaded")  # type: ignore[name-defined] # noqa: F821
        return False
    return True


def locale_args(sxng_locale: str) -> dict[str, str]:
    """Map a SearXNG locale ("en-GB", "en", "all") to Brave's parameters."""
    lang, _, region = (sxng_locale or "all").partition("-")
    lang = lang.lower()
    region = region.upper()

    args = {"country": region if region in COUNTRIES else "ALL"}

    candidates = []
    if lang == "pt" and region in ("BR", "PT"):
        candidates.append(f"pt-{region.lower()}")
    if lang == "en" and region == "GB":
        candidates.append("en-gb")
    if lang == "zh":
        candidates.append("zh-hant" if region in ("TW", "HK") else "zh-hans")
    candidates.append(lang)
    for cand in candidates:
        if cand in SEARCH_LANGS:
            args["search_lang"] = cand
            break
    return args


def request(query: str, params) -> None:
    args = {
        "q": query,
        "count": results_per_page,
        "offset": params["pageno"] - 1,
        "safesearch": safesearch_map.get(params["safesearch"], "off"),
        "text_decorations": "false",
        **locale_args(params.get("searxng_locale", "all")),
    }
    if params.get("time_range") in freshness_map:
        args["freshness"] = freshness_map[params["time_range"]]

    params["url"] = f"{base_url}?{urlencode(args)}"
    params["headers"]["X-Subscription-Token"] = api_key
    params["headers"]["Accept"] = "application/json"
    params["headers"]["Cache-Control"] = "no-cache"


def response(resp) -> EngineResults:
    res = EngineResults()
    for result in (resp.json().get("web") or {}).get("results", []):
        res.add(parse_search_result(result))
    return res
