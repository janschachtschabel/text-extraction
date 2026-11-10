"""Collapsed solution panels: a hidden block that a "Solution" or "Hint" button opens.

Exercise pages ship the answer in the HTML and hide it until a click. A reader gets it on
demand, so it is part of the page. Panels that a menu or other control opens stay hidden.
"""

import re
from typing import Final

from bs4 import BeautifulSoup, Tag

from text_extraction.convert.dom import HIDDEN_STYLE, attr

SOLUTION_LABEL: Final = re.compile(
    r"^(lösung(en|svorschlag|sweg)?|musterlösung|antwort(en)?|tipps?|hinweis(e)?|erklärung"
    r"|solution(s)?|show (the )?solution|answer(s)?|show answer|hint(s)?)\b",
    re.IGNORECASE,
)
_ARROWS = "▾▸►▼+"
_SPACE = re.compile(r"\s+")
_HIDDEN_CLASS = "hidden"


def _label(control: Tag) -> str:
    text = _SPACE.sub(" ", control.get_text().replace("\xa0", " ")).strip()
    return text.lstrip(_ARROWS + " ").strip()


def _is_collapsed(tag: Tag) -> bool:
    return (
        tag.get("hidden") not in (None, "until-found")
        or _HIDDEN_CLASS in attr(tag, "class").split()
        or bool(HIDDEN_STYLE.search(attr(tag, "style")))
    )


def _show(tag: Tag) -> None:
    if tag.get("hidden") is not None and tag.get("hidden") != "until-found":
        del tag["hidden"]
    classes = [c for c in attr(tag, "class").split() if c != _HIDDEN_CLASS]
    if "class" in tag.attrs:
        if classes:
            tag["class"] = " ".join(classes)
        else:
            del tag["class"]
    style = attr(tag, "style")
    if style and HIDDEN_STYLE.search(style):
        kept = [
            part
            for part in style.split(";")
            if part.strip() and not HIDDEN_STYLE.fullmatch(part.strip())
        ]
        if kept:
            tag["style"] = "; ".join(part.strip() for part in kept)
        else:
            del tag["style"]


def _panel_of(soup: BeautifulSoup, control: Tag) -> Tag | None:
    target = attr(control, "aria-controls").split()
    if target:
        panel = soup.find(id=target[0])
        if isinstance(panel, Tag) and _is_collapsed(panel):
            return panel
    candidates = [control]
    parent = control.parent
    if isinstance(parent, Tag) and len(parent.find_all(True, recursive=False)) == 1:
        candidates.append(parent)
    for candidate in candidates:
        sibling = candidate.find_next_sibling(True)
        if sibling is not None and _is_collapsed(sibling):
            return sibling
    return None


def _is_toggle(control: Tag) -> bool:
    """A control that opens something on the page: not a link to another page, and not a
    link in the page's header or footer."""
    has_target = bool(attr(control, "aria-controls"))
    if control.name == "a" and not (
        has_target
        or control.get("href") is None
        or attr(control, "href").startswith("#")
        or attr(control, "role") == "button"
    ):
        return False
    if control.name != "button" and not has_target:
        return control.find_parent(["header", "footer"]) is None
    return True


def reveal_solution_panels(soup: BeautifulSoup) -> int:
    """Open the solution panels in place and return how many were revealed.

    A label paragraph goes in front of each revealed panel: the control itself often sits in a
    navigation that is removed later."""
    revealed = 0
    done: set[int] = set()
    for details in soup.find_all("details"):
        summary = details.find("summary", recursive=False)
        is_solution = summary is not None and SOLUTION_LABEL.match(_label(summary))
        if is_solution and "open" not in details.attrs:
            details["open"] = ""
            revealed += 1
    for control in soup.select('button, [role="button"], a'):
        label = _label(control)
        if not SOLUTION_LABEL.match(label) or not _is_toggle(control):
            continue
        panel = _panel_of(soup, control)
        if panel is None or id(panel) in done:
            continue
        done.add(id(panel))
        _show(panel)
        for descendant in panel.find_all(_is_collapsed):
            _show(descendant)
        heading = soup.new_tag("p")
        strong = soup.new_tag("strong")
        strong.string = label
        heading.append(strong)
        panel.insert_before(heading)
        revealed += 1
    return revealed
