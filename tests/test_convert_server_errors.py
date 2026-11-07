from collections.abc import Callable

import pytest
from bs4 import BeautifulSoup

from tests.scaling import GROWTH_LIMIT, growth
from text_extraction.convert import convert_html
from text_extraction.convert.server_errors import (
    MAX_LISTED_ERRORS,
    MAX_MESSAGE_CHARS,
    SERVER_ERRORS_REMOVED_WARNING,
    ServerError,
    strip_server_errors,
)

FRONT_PAGE = "/var/www/prod.210317/wp-content/themes/wir-lernen-online/front-page.php"


def php_warning(subject: str, line: int = 128) -> str:
    """PHP with ``html_errors`` on, as wirlernenonline.de printed it into its start page."""
    return (
        "<br />\n<b>Warning</b>:  Undefined array key "
        f'"http://w3id.org/openeduhub/vocabs/discipline/{subject}" in '
        f"<b>{FRONT_PAGE}</b> on line <b>{line}</b><br />\n"
    )


def _soup(body: str) -> BeautifulSoup:
    return BeautifulSoup(f"<html><body>{body}</body></html>", "lxml")


def _stripping(body: Callable[[int], str]) -> Callable[[int], Callable[[], object]]:
    """For ``growth``: a fresh page of the given size, and the strip to time on it."""

    def prepare(size: int) -> Callable[[], object]:
        soup = _soup(body(size))
        return lambda: strip_server_errors(soup)

    return prepare


def test_the_warning_code() -> None:
    assert SERVER_ERRORS_REMOVED_WARNING == "server_errors_removed"


def test_strips_html_php_warnings() -> None:
    soup = _soup(
        f"<div class='x'>{php_warning('Sport')}{php_warning('Kunst', 129)}"
        "Mechatronik Germanistik</div>"
    )

    errors = strip_server_errors(soup)

    assert len(errors) == 2
    assert errors[0] == ServerError(
        level="Warning",
        message='Undefined array key "http://w3id.org/openeduhub/vocabs/discipline/Sport"',
        file=FRONT_PAGE,
        line=128,
    )
    assert errors[1].line == 129

    text = soup.get_text()
    for marker in ("Warning", "front-page.php", "on line", "Undefined array key"):
        assert marker not in text
    assert "Mechatronik Germanistik" in text
    assert soup.find("br") is None  # the <br> around each message went with it
    assert soup.find("b") is None  # and so did the emptied <b> elements


def test_strips_plain_text_php_errors() -> None:
    soup = _soup(
        "<div>Before the errors.\n"
        "PHP Fatal error:  Uncaught Error in /srv/app/index.php on line 7\n"
        "Notice: Undefined index: x in C:\\inetpub\\site\\a.php on line 3\n"
        "After the errors.</div>"
    )

    errors = strip_server_errors(soup)

    assert errors == [
        ServerError("Fatal error", "Uncaught Error", "/srv/app/index.php", 7),
        ServerError("Notice", "Undefined index: x", "C:\\inetpub\\site\\a.php", 3),
    ]
    text = soup.get_text()
    assert "Fatal error" not in text
    assert "Notice" not in text
    assert "index.php" not in text
    assert "a.php" not in text
    assert "Before the errors." in text
    assert "After the errors." in text


def test_keeps_a_message_inside_a_line_of_text() -> None:
    # PHP starts every message on a line of its own; one in the middle of a line is quoted.
    text = "Start Deprecated: Function x() is deprecated in /a/b.inc on line 12 end"
    soup = _soup(f"<p>{text}</p>")

    assert strip_server_errors(soup) == []
    assert soup.get_text() == text


def test_keeps_a_message_quoted_in_prose() -> None:
    text = (
        "Ich bekomme Warning: Undefined variable $x in /var/www/html/index.php on line 5 "
        "beim Aufruf."
    )
    soup = _soup(f"<p>{text}</p>")

    assert strip_server_errors(soup) == []
    assert soup.get_text() == text


def test_strips_a_message_on_a_line_of_its_own_after_text() -> None:
    soup = _soup(
        "<div>Start<br/>  Deprecated: Function x() is deprecated in /a/b.inc on line 12</div>"
    )

    assert strip_server_errors(soup) == [
        ServerError("Deprecated", "Function x() is deprecated", "/a/b.inc", 12)
    ]
    assert soup.get_text().strip() == "Start"


def test_every_label_is_recognised() -> None:
    labels = [
        "Warning",
        "Notice",
        "Deprecated",
        "Fatal error",
        "Parse error",
        "Strict Standards",
        "Recoverable fatal error",
        "Catchable fatal error",
    ]
    body = "".join(f"<div>{label}: msg in /x/y.phtml on line 1</div>" for label in labels)
    soup = _soup(body + "<div>kept</div>")

    assert [error.level for error in strip_server_errors(soup)] == labels

    assert soup.get_text().strip() == "kept"


def test_keeps_php_error_text_in_code_and_pre() -> None:
    message = "Warning: foo in /var/www/x.php on line 3"
    soup = _soup(
        f"<p>The message <code>{message}</code> means a key is missing.</p>"
        f"<pre>{message}</pre>"
        f"<pre><b>Warning</b>:  foo in <b>/var/www/x.php</b> on line <b>3</b></pre>"
        f"<textarea>{message}</textarea>"
        f"<div><samp>{message}</samp></div><div><kbd>{message}</kbd></div>"
        f"<div><var>{message}</var></div><div><tt>{message}</tt></div>"
    )

    assert strip_server_errors(soup) == []

    assert soup.get_text().count("/var/www/x.php") == 8


def test_keeps_prose_that_mentions_warnings() -> None:
    soup = _soup("<p>Warning: do not touch the wire in the lab.</p>")

    assert strip_server_errors(soup) == []

    assert soup.get_text() == "Warning: do not touch the wire in the lab."


def test_keeps_an_uncaught_error_that_spans_lines() -> None:
    # PHP 8 writes an uncaught exception with its stack trace over several lines; such a
    # message is not recognised and stays (documented in the user guide).
    text = (
        "PHP Fatal error:  Uncaught Exception: boom in /var/www/a.php:3\n"
        "Stack trace:\n#0 {main}\n  thrown in /var/www/a.php on line 3"
    )
    soup = _soup(f"<div>{text}</div>")

    assert strip_server_errors(soup) == []
    assert soup.get_text() == text


def test_keeps_a_message_without_a_php_path() -> None:
    soup = _soup("<p>Notice: the lab is closed in /summer/ on line 3 of the plan.</p>")

    assert strip_server_errors(soup) == []


def test_linear_on_hostile_input() -> None:
    def page(size: int) -> str:
        return "<div><b>Warning</b>: " + "a " * size + "</div>"

    assert strip_server_errors(_soup(page(200_000))) == []
    assert growth(_stripping(page), 50_000) < GROWTH_LIMIT


def test_linear_on_many_labels_close_to_a_line_number() -> None:
    chunk = "\nWarning: " * 100 + " in /" * 100 + "x on line 1 "

    def page(size: int) -> str:
        return "<div>" + chunk * size + "</div>"

    assert growth(_stripping(page), 20) < GROWTH_LIMIT


def test_identical_messages_are_merged() -> None:
    soup = _soup(f"<div>{php_warning('Sport') * 3}{php_warning('Kunst')}kept</div>")

    errors = strip_server_errors(soup)

    assert [(error.message[-6:], error.count) for error in errors] == [
        ('Sport"', 3),
        ('Kunst"', 1),
    ]
    assert soup.get_text().strip() == "kept"


def test_messages_are_listed_in_document_order() -> None:
    soup = _soup(
        "<div>Notice: first in /a.php on line 1<div>Notice: second in /a.php on line 2</div>"
        "Notice: third in /a.php on line 3</div>"
    )

    assert [error.message for error in strip_server_errors(soup)] == ["first", "second", "third"]


def test_the_list_is_capped_and_the_removal_is_not() -> None:
    body = "".join(f"<div>Warning: m{index} in /a.php on line {index}</div>" for index in range(60))
    soup = _soup(body + "<div>kept</div>")

    errors = strip_server_errors(soup)

    assert len(errors) == MAX_LISTED_ERRORS
    assert soup.get_text().strip() == "kept"


def test_a_long_message_is_cut_and_its_whitespace_collapsed() -> None:
    soup = _soup("<div>Warning: " + "x  " * 200 + " in /a.php on line 1</div>")

    (error,) = strip_server_errors(soup)

    assert len(error.message) == MAX_MESSAGE_CHARS
    assert "  " not in error.message


def test_many_messages_in_one_text_node_take_linear_time() -> None:
    def page(size: int) -> str:
        return "<div>" + "Notice: a in /a.php on line 1\n" * size + "</div>"

    soup = _soup(page(5_000))
    assert strip_server_errors(soup) == [ServerError("Notice", "a", "/a.php", 1, count=5_000)]
    assert soup.get_text().strip() == ""
    # The quadratic version (a copy of the text node per message) grew about 11-fold.
    assert growth(_stripping(page), 5_000) < GROWTH_LIMIT


def test_a_message_between_two_lines_keeps_them_apart() -> None:
    soup = _soup("<div>Line one<br/>Warning: x in /a.php on line 3<br/>Line two</div>")

    assert len(strip_server_errors(soup)) == 1

    assert str(soup.div) == "<div>Line one<br/>Line two</div>"


def test_messages_between_two_lines_leave_one_break() -> None:
    soup = _soup(
        "<div>Line one<br/>\nWarning: x in /a.php on line 3<br/>\n"
        "Warning: y in /a.php on line 4<br/>\nLine two</div>"
    )

    assert len(strip_server_errors(soup)) == 2

    assert "".join(str(soup.div).split()) == "<div>Lineone<br/>Linetwo</div>"


def test_a_message_before_text_on_its_line_keeps_the_line_break() -> None:
    soup = _soup("<div>Line one<br/>Warning: x in /a.php on line 3 Line two</div>")

    assert len(strip_server_errors(soup)) == 1

    assert str(soup.div) == "<div>Line one<br/> Line two</div>"


def test_many_html_messages_in_one_block_take_linear_time() -> None:
    def page(size: int) -> str:
        return "<div>start" + php_warning("Sport") * size + "end</div>"

    soup = _soup(page(1_000))
    assert [error.count for error in strip_server_errors(soup)] == [1_000]
    assert soup.find("b") is None
    assert " ".join(soup.get_text().split()) == "start end"
    # The quadratic version (bs4 removing one node at a time) grew about 16-fold.
    assert growth(_stripping(page), 1_000) < GROWTH_LIMIT


# A message a reader quotes: plain text, as copied from the browser (ruling AU3-R4).
QUOTED = (
    "Warning: Cannot modify header information - headers already sent by (output started at "
    "/var/www/html/config.php:12) in /var/www/html/index.php on line 5"
)
HTML_ERRORS = (
    "<br />\n<b>Warning</b>:  foreach() argument must be of type array in "
    "<b>/var/www/a.php</b> on line <b>12</b><br />"
)
FOREACH = ServerError("Warning", "foreach() argument must be of type array", "/var/www/a.php", 12)
ARTICLE_TEXT = "<p>" + "The lesson is about birds and the forest they live in. " * 6 + "</p>"


@pytest.mark.parametrize(
    "body",
    [
        f"<blockquote>{QUOTED}</blockquote>",
        f"<blockquote><p>{QUOTED}</p></blockquote>",
        f"<p>He wrote: <q>{QUOTED}</q></p>",
        f"<div>The log said<br><cite>{QUOTED}</cite></div>",
        f"<h2>{QUOTED}</h2>",
        f"<ul><li>{QUOTED}</li></ul>",
        f"<table><tr><td>{QUOTED}</td></tr></table>",
        f"<table><tr><th>{QUOTED}</th></tr></table>",
        f"<dl><dt>Message</dt><dd>{QUOTED}</dd></dl>",
        f"<figure><img src='a.png'><figcaption>{QUOTED}</figcaption></figure>",
        "<blockquote><p><strong>Notice</strong>: Undefined index: x in /var/www/a.php on line 7"
        "</p></blockquote>",
    ],
)
def test_keeps_a_plain_message_in_a_quoting_context(body: str) -> None:
    soup = _soup(body)
    text = soup.get_text()

    assert strip_server_errors(soup) == []
    assert soup.get_text() == text


def test_keeps_a_message_a_forum_post_quotes() -> None:
    soup = _soup(
        "<div class='post'><p>Hallo zusammen,<br>nach dem Update sehe ich auf meiner Seite:</p>"
        f"<blockquote>{QUOTED}</blockquote><p>Was kann ich tun?</p></div>"
    )

    assert strip_server_errors(soup) == []
    assert QUOTED in soup.get_text()


def test_removes_a_plain_message_in_a_bare_paragraph_after_a_break() -> None:
    # Known limit (ruling AU3-R4): outside a quoting context the plain form counts, as real
    # PHP output often lands in a <p> or <div> a theme opened, so a forum post that pastes
    # the message on a line of its own in a bare <p> loses it.
    soup = _soup(f"<p>Hallo,<br>nach dem Update sehe ich:<br>{QUOTED}<br>Was tun?</p>")

    assert [error.file for error in strip_server_errors(soup)] == ["/var/www/html/index.php"]
    assert "index.php" not in soup.get_text()
    assert "Was tun?" in soup.get_text()


@pytest.mark.parametrize(
    "body",
    [
        f"{QUOTED}<p>kept</p>",
        f"<div>{QUOTED}</div><p>kept</p>",
        f"<p>{QUOTED}</p><p>kept</p>",
        f"{HTML_ERRORS}<p>kept</p>",
        f"<div>{HTML_ERRORS}</div><p>kept</p>",
        f"<p>{HTML_ERRORS}</p><p>kept</p>",
    ],
)
def test_removes_real_php_output_outside_quoting_contexts(body: str) -> None:
    soup = _soup(body)

    (error,) = strip_server_errors(soup)

    assert error.level == "Warning"
    assert soup.get_text().strip() == "kept"


@pytest.mark.parametrize(
    "body",
    [
        f"<ul><li>{HTML_ERRORS}</li><li>kept</li></ul>",
        f"<table><tr><td>{HTML_ERRORS}</td><td>kept</td></tr></table>",
        f"<blockquote>{HTML_ERRORS}kept</blockquote>",
        "<ul><li><strong>Warning</strong>:  foreach() argument must be of type array in "
        "<strong>/var/www/a.php</strong> on line <strong>12</strong></li><li>kept</li></ul>",
    ],
)
def test_removes_the_html_errors_form_even_in_a_quoting_context(body: str) -> None:
    soup = _soup(body)

    assert strip_server_errors(soup) == [FOREACH]
    assert soup.get_text().strip() == "kept"


def test_keeps_a_whole_bold_quote_in_a_quoting_context() -> None:
    # Only a bold label and a bold path of their own make the html_errors form.
    soup = _soup(f"<ul><li><b>{QUOTED}</b></li></ul>")

    assert strip_server_errors(soup) == []


@pytest.mark.parametrize("extension", [".module", ".theme", ".install", ".engine"])
def test_recognises_drupal_paths(extension: str) -> None:
    path = f"/var/www/sites/all/modules/views/views{extension}"
    soup = _soup(
        "<br />\n<b>Warning</b>: Invalid argument supplied for foreach() in "
        f"<b>{path}</b> on line <b>12</b><br /><p>kept</p>"
    )

    assert strip_server_errors(soup) == [
        ServerError("Warning", "Invalid argument supplied for foreach()", path, 12)
    ]
    assert soup.get_text().strip() == "kept"


XDEBUG = (
    "<br />\n<font size='1'><table class='xdebug-error xe-warning' dir='ltr' border='1' "
    "cellspacing='0' cellpadding='1'>\n<tr><th align='left' bgcolor='#f57900' colspan=\"5\">"
    "<span style='background-color: #cc0000; color: #fce94f; font-size: x-large;'>( ! )</span>"
    " Warning: Undefined variable $x in /var/www/a.php on line <i>3</i></th></tr>\n"
    "<tr><th align='left' bgcolor='#e9b96e' colspan='5'>Call Stack</th></tr>\n"
    "<tr><th align='center' bgcolor='#eeeeec'>#</th><th align='left' bgcolor='#eeeeec'>Time"
    "</th><th align='left' bgcolor='#eeeeec'>Memory</th><th align='left' bgcolor='#eeeeec'>"
    "Function</th><th align='left' bgcolor='#eeeeec'>Location</th></tr>\n"
    "<tr><td bgcolor='#eeeeec' align='center'>1</td><td bgcolor='#eeeeec' align='center'>"
    "0.0001</td><td bgcolor='#eeeeec' align='right'>393936</td><td bgcolor='#eeeeec'>{main}( )"
    "</td><td title='/var/www/a.php' bgcolor='#eeeeec'>.../a.php<b>:</b>0</td></tr>\n"
    "</table></font>\n"
)


def test_removes_an_xdebug_table() -> None:
    soup = _soup(f"<p>before</p>{XDEBUG}<p>after</p>")

    assert strip_server_errors(soup) == [
        ServerError("Warning", "Undefined variable $x", "/var/www/a.php", 3)
    ]
    assert soup.find("table") is None
    assert " ".join(soup.get_text().split()) == "before after"


def test_xdebug_tables_are_merged_and_listed_in_document_order() -> None:
    soup = _soup(
        f"<div>Notice: first in /a.php on line 1</div>{XDEBUG}{XDEBUG}"
        "<div>Notice: last in /a.php on line 2</div>"
    )

    assert [(error.message, error.count) for error in strip_server_errors(soup)] == [
        ("first", 1),
        ("Undefined variable $x", 2),
        ("last", 1),
    ]


def test_keeps_a_table_that_only_has_the_class() -> None:
    soup = _soup("<table class='xdebug-error'><tr><th>( ! ) Results</th></tr></table>")

    assert strip_server_errors(soup) == []
    assert soup.find("table") is not None


@pytest.mark.parametrize(
    "quote",
    [
        f"<blockquote>{QUOTED}</blockquote>",
        f"<p>Hallo zusammen,<br>nach dem Update sehe ich auf meiner Seite:</p>"
        f"<blockquote>{QUOTED}</blockquote><p>Was kann ich tun?</p>",
    ],
)
def test_a_quoted_message_stays_in_the_converted_text(quote: str) -> None:
    page = (
        "<html><body><article><h1>PHP header errors</h1>"
        f"{ARTICLE_TEXT}{quote}{ARTICLE_TEXT}</article></body></html>"
    )

    result = convert_html(page, "https://blog.example.org/php/header-fehler", "markdown")

    assert "headers already sent" in result.content
    assert "/var/www/html/index.php on line 5" in result.content
    assert result.server_errors == []
    assert SERVER_ERRORS_REMOVED_WARNING not in result.warnings


def test_a_drupal_message_is_removed_from_markdown() -> None:
    page = (
        "<html><body><article><h1>Lesson</h1>"
        f"{ARTICLE_TEXT}<br />\n<b>Warning</b>: Invalid argument supplied for foreach() in "
        "<b>/var/www/sites/all/modules/views/views.module</b> on line <b>12</b><br />"
        f"{ARTICLE_TEXT}</article></body></html>"
    )

    result = convert_html(page, "https://example.org/lesson", "markdown")

    assert "**on line" not in result.content
    assert "views.module" not in result.content
    assert [error.file for error in result.server_errors] == [
        "/var/www/sites/all/modules/views/views.module"
    ]


def test_linear_on_many_quoted_labels_in_one_bold_text() -> None:
    # Each candidate in a quoting context reads its piece's bounds once, not per candidate.
    def page(size: int) -> str:
        return (
            "<ul><li><b>"
            + " " * size * 40
            + "\nWarning: a in /a.php on line 1" * size
            + "</b></li></ul>"
        )

    assert growth(_stripping(page), 2_000) < GROWTH_LIMIT


def test_many_xdebug_tables_take_linear_time() -> None:
    def page(size: int) -> str:
        return "<div>" + XDEBUG * size + "</div>"

    assert growth(_stripping(page), 200) < GROWTH_LIMIT


def test_an_xdebug_table_is_read_by_its_own_head() -> None:
    # The outer table has no head cell of its own; the nested one's message removes only it.
    soup = _soup(
        "<table class='xdebug-error'><tr><td>outer<table class='xdebug-error'><tr><th>( ! ) "
        "Warning: Undefined variable $x in /var/www/a.php on line 3</th></tr></table>"
        "</td></tr></table>"
    )

    assert strip_server_errors(soup) == [
        ServerError("Warning", "Undefined variable $x", "/var/www/a.php", 3)
    ]
    assert len(soup.find_all("table")) == 1
    assert soup.get_text() == "outer"


def test_an_xdebug_head_in_a_thead_or_tbody_counts() -> None:
    head = "<tr><th>( ! ) Notice: a in /a.php on line 1</th></tr>"
    soup = _soup(
        f"<table class='xdebug-error'><thead>{head}</thead></table>"
        f"<table class='xdebug-error'><tbody>{head}</tbody></table><p>kept</p>"
    )

    assert strip_server_errors(soup) == [ServerError("Notice", "a", "/a.php", 1, count=2)]
    assert soup.get_text() == "kept"


@pytest.mark.parametrize(
    "nested",
    ["<table class='xdebug-error'><tr><td>", "<table class='xdebug-error'><tr><th>( ! ) a"],
)
def test_nested_xdebug_tables_take_linear_time(nested: str) -> None:
    # Each table reads only its own first row's head cell, up to the next nested table; the
    # version that searched each table's whole subtree took 6 s at 100 KB and 94 s at 400 KB.
    def page(size: int) -> str:
        return nested * (size // len(nested))

    assert growth(_stripping(page), 25_000) < GROWTH_LIMIT
