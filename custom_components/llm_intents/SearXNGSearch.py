import html
import logging
import re

import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util.json import JsonObjectType

from .cache import SQLiteCache
from .const import (
    CONF_SEARXNG_NUM_RESULTS,
    CONF_SEARXNG_URL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class SearXNGSearchWebTool(llm.Tool):
    """Tool for searching the web using SearXNG."""

    name = "search_web_searxng"
    description = "Search the web using SearXNG to lookup information and answer user queries. You should use this tool to access additional information about the world"
    response_instruction = """
    Review the results to provide the user with a clear and concise answer to their query.
    If the search results provided do not answer the user request, advise the user of this.
    You may offer to perform related searches for the user, and if confirmed, search new queries to continue assisting the user.
    Your response must be in plain-text, without the use of any formatting, and should be kept to 2-3 sentences.
    """

    parameters = vol.Schema(
        {
            vol.Required("query", description="The query to search for"): str,
        }
    )

    def wrap_response(self, response: dict) -> dict:
        response["instruction"] = self.response_instruction
        return response

    async def cleanup_text(self, text: str) -> str:
        text = html.unescape(text)
        text = re.sub(r"<[^>]+>", "", text)
        text = re.sub(r"\s+", " ", text).strip()

        return text

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Call the tool."""
        config_data = hass.data[DOMAIN].get("config", {})
        entry = next(iter(hass.config_entries.async_entries(DOMAIN)))
        config_data = {**config_data, **entry.options}

        query = tool_input.tool_args["query"]
        _LOGGER.info("SearXNG web search requested for: %s", query)

        base_url = config_data.get(CONF_SEARXNG_URL, "")
        num_results = config_data.get(CONF_SEARXNG_NUM_RESULTS, 2)

        if not base_url:
            return {"error": "SearXNG URL not configured"}

        base_url = str(base_url).rstrip("/")

        try:
            session = async_get_clientsession(hass)

            params = {
                "q": query,
                "format": "json",
                "count": num_results,
            }

            cache = SQLiteCache()
            cached_response = cache.get(__name__, {"base_url": base_url, **params})

            if cached_response:
                return self.wrap_response(cached_response)
            
            headers = {
                "User-Agent": "HomeAssistant-ToolsForAssist/1.0"
            }
            async with session.get(
                f"{base_url}/search",
                params=params,
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    results = []

                    for item in data.get("results", [])[:num_results]:
                        title = item.get("title", "") or ""
                        content = item.get("content", "") or ""
                        url = item.get("url", "") or ""

                        cleaned = await self.cleanup_text(content)
                        # Keep output consistent with Brave: title + description
                        # Include the URL inside the description so the model can cite it.
                        if url:
                            description = f"{cleaned} ({url})" if cleaned else url
                        else:
                            description = cleaned

                        results.append({"title": title, "description": description})

                    response = {"results": results if results else "No results found"}

                    if results:
                        cache.set(__name__, {"base_url": base_url, **params}, response)
                        return self.wrap_response(response)

                    return response

                _LOGGER.error(
                    "SearXNG web search received a HTTP %s error", resp.status
                )
                return {"error": f"Search error: {resp.status}"}

        except Exception as e:
            _LOGGER.error("SearXNG web search error: %s", e)
            return {"error": f"Error searching web: {e!s}"}
