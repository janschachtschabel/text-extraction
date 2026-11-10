"""edu-sharing render pages, read through the repository's public REST API (package 22).

An edu-sharing repository shows a node (a material) at ``<base>/components/render/<id>``, an
Angular shell of a few KiB with no text of its own. The same repository answers the node's
metadata as JSON at ``<base>/rest/node/v1/nodes/-home-/<id>/metadata?propertyFilter=-all-``:
its title, name, description, keywords, licence, a web link (``ccm:wwwurl``) and the URL of its
file (``downloadUrl``). ``render_node`` names the base and the node of a render URL,
``metadata_url`` the metadata request, ``parse_metadata`` reads the answer and ``node_page``
writes a small HTML page of it (with ``licence_url`` as its licence link), with the node's file
as an ``Attachment``. Fetching is the
caller's business (``pipeline_edusharing``).

Everything in the metadata comes from the remote site: each value is escaped where the page
writes it, a URL is kept only when it is an absolute ``http(s)`` URL with a host and without user
info, and a field of an unexpected type is passed over. A render URL's optional version segment
is not read: the metadata is the node's current version's.
"""

import html
import json
import re
from typing import Any, Final
from urllib.parse import urljoin, urlsplit

from text_extraction.convert.attachments import Attachment

_UUID: Final = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
# ``<prefix>/edu-sharing/components/render/<id>``, optionally with a version segment.
_RENDER_PATH: Final = re.compile(
    rf"(?P<base>(?:/[^/]+)*?/edu-sharing)/components/render/(?P<node>{_UUID})(?:/[^/]+)?/?"
)
_DOWNLOAD_RIGHT: Final = "DownloadContent"
_FIXED_LICENCES: Final = {
    "CC_0": "https://creativecommons.org/publicdomain/zero/1.0/",
    "PDM": "https://creativecommons.org/publicdomain/mark/1.0/",
}
_CC_KEY: Final = re.compile(r"CC_BY(?:_NC)?(?:_SA|_ND)?")
_CC_VERSION: Final = re.compile(r"[1-4]\.0")


def render_node(url: str) -> tuple[str, str] | None:
    """The base URL (up to and including ``/edu-sharing``) and the node id of an edu-sharing
    render URL, or None for any other URL. The query and fragment do not matter; a URL with
    user info is none."""
    try:
        parts = urlsplit(url)
        port = parts.port  # raises for a port that is no number
    except ValueError:
        return None
    if parts.scheme not in {"http", "https"} or not parts.hostname or "@" in parts.netloc:
        return None
    found = _RENDER_PATH.fullmatch(parts.path)
    if found is None:
        return None
    host = parts.hostname if ":" not in parts.hostname else f"[{parts.hostname}]"
    origin = f"{parts.scheme}://{host}" + (f":{port}" if port is not None else "")
    return f"{origin}{found['base']}", found["node"].lower()


def metadata_url(base: str, node: str) -> str:
    """The REST request for the metadata of ``node`` in the repository at ``base``."""
    return f"{base}/rest/node/v1/nodes/-home-/{node}/metadata?propertyFilter=-all-"


def parse_metadata(body: bytes) -> dict[str, Any] | None:
    """The metadata answer ``body`` when it is JSON with a ``node`` object, else None (also for
    bytes that are no JSON, or JSON nested too deep to read)."""
    try:
        parsed = json.loads(body)
    except (ValueError, RecursionError):  # UnicodeDecodeError is a ValueError
        return None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("node"), dict):
        return None
    return parsed


def node_page(meta: dict[str, Any], *, base: str | None = None) -> tuple[str, Attachment | None]:
    """A small HTML page of the metadata answer ``meta`` and the node's file.

    The page's ``<head>`` has the ``<title>``, the description and the keywords as ``<meta>``,
    the licence as ``<link rel="license">`` (``licence_url``) and ``base`` (the render URL) as
    ``<link rel="canonical">``, so the page's metadata reads as any page's. Its body has the
    ``<h1>`` of ``node.title``, else ``cclom:title``, else ``node.name``; the description; the
    keywords as a plain comma list; then a table with the licence's display name (linked to the
    licence), the web link (``ccm:wwwurl``) and the file (a link with its name when its URL is
    safe). No label
    of ours is written, so the content words are the node's own. The file is an ``Attachment``
    of ``downloadUrl`` (resolved against ``base`` when relative), ``name``, ``mimetype`` and
    ``size``; None without a safe URL, or when ``access`` is a list without the right to
    download it."""
    node = meta.get("node")
    node = node if isinstance(node, dict) else {}
    properties = node.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    name = _text(node.get("name"))
    title = _text(node.get("title")) or _first(properties, "cclom:title") or name
    mime = _text(node.get("mimetype"))
    descriptions = _all(properties, "cclom:general_description")
    keywords = _all(properties, "cclom:general_keyword")
    licence = licence_url(node)
    licence_name = _first(properties, "ccm:commonlicense_key_DISPLAYNAME") or _first(
        properties, "ccm:commonlicense_key"
    )
    website = _safe_url(_first(properties, "ccm:wwwurl"), base)
    download = _safe_url(_text(node.get("downloadUrl")), base)
    canonical = _safe_url(base, None)
    access = node.get("access")
    if isinstance(access, list) and _DOWNLOAD_RIGHT not in access:
        download = None

    head = ['<meta charset="utf-8">']
    if title:
        head.append(f"<title>{_escaped(title)}</title>")
    if descriptions:
        head.append(f'<meta name="description" content="{_escaped(" ".join(descriptions))}">')
    if keywords:
        head.append(f'<meta name="keywords" content="{_escaped(", ".join(keywords))}">')
    if licence:
        head.append(f'<link rel="license" href="{_escaped(licence)}">')
    if canonical:
        head.append(f'<link rel="canonical" href="{_escaped(canonical)}">')
    parts: list[str] = []
    if title:
        parts.append(f"<h1>{_escaped(title)}</h1>")
    parts.extend(f"<p>{_escaped(text)}</p>" for text in descriptions)
    if keywords:
        parts.append(f"<p>{_escaped(', '.join(keywords))}</p>")
    # The licence, the web link and the file as the rows of a table: a paragraph of a link
    # alone tips Trafilatura into flattening so short a page, a table row does not.
    rows: list[str] = []
    if licence_name:
        rows.append(
            f'<a rel="license" href="{_escaped(licence)}">{_escaped(licence_name)}</a>'
            if licence
            else _escaped(licence_name)
        )
    if website:
        rows.append(f'<a href="{_escaped(website)}">{_escaped(website)}</a>')
    label = name or title or "File"
    if download:
        rows.append(f'<a href="{_escaped(download)}">{_escaped(label)}</a>')
    elif name:
        rows.append(_escaped(name))
    if rows:
        parts.append("<table>" + "".join(f"<tr><td>{row}</td></tr>" for row in rows) + "</table>")
    page = (
        f"<!doctype html><html><head>{''.join(head)}</head>"
        f"<body><main><article>{''.join(parts)}</article></main></body></html>"
    )
    attachment = (
        Attachment(url=download, label=label, mime=mime, size=_size(node.get("size")))
        if download
        else None
    )
    return page, attachment


def licence_url(node: dict[str, Any]) -> str | None:
    """The URL of the node's licence: ``node.license.url``, else ``virtual:licenseurl``, else one
    built from a Creative Commons ``ccm:commonlicense_key`` (with
    ``ccm:commonlicense_cc_version`` for the ``CC_BY`` family); None when none is safe."""
    licence = node.get("license")
    if isinstance(licence, dict) and (found := _safe_url(_text(licence.get("url")), None)):
        return found
    properties = node.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    if found := _safe_url(_first(properties, "virtual:licenseurl"), None):
        return found
    key = (_first(properties, "ccm:commonlicense_key") or "").upper()
    if key in _FIXED_LICENCES:
        return _FIXED_LICENCES[key]
    version = _first(properties, "ccm:commonlicense_cc_version") or ""
    if _CC_KEY.fullmatch(key) and _CC_VERSION.fullmatch(version):
        kind = key.removeprefix("CC_").lower().replace("_", "-")
        return f"https://creativecommons.org/licenses/{kind}/{version}/"
    return None


def _text(value: object) -> str | None:
    """A non-blank string value, its white space collapsed, or None."""
    if not isinstance(value, str):
        return None
    return " ".join(value.split()) or None


def _all(properties: dict[str, Any], key: str) -> list[str]:
    """The non-blank strings of the property ``key`` (a list of strings in the answer)."""
    values = properties.get(key)
    if not isinstance(values, list):
        return []
    return [text for value in values if (text := _text(value)) is not None]


def _first(properties: dict[str, Any], key: str) -> str | None:
    values = _all(properties, key)
    return values[0] if values else None


def _safe_url(value: str | None, base: str | None) -> str | None:
    """``value`` as an absolute http(s) URL with a host and without user info, else None."""
    if not value:
        return None
    if base is not None and not value.startswith("//"):
        value = urljoin(base, value)
    try:
        parts = urlsplit(value)
        parts.port  # noqa: B018 - raises for a port that is no number
    except ValueError:
        return None
    if parts.scheme not in {"http", "https"} or not parts.hostname or "@" in parts.netloc:
        return None
    if any(char.isspace() or ord(char) < 0x20 for char in value):
        return None
    return value


def _size(value: object) -> int | None:
    """The file size in bytes: a whole number, or a string of digits; else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    return None


def _escaped(value: str) -> str:
    return html.escape(value, quote=True)
