import time

from bs4 import BeautifulSoup

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


def test_strips_a_message_inside_a_line_of_text_and_keeps_the_rest() -> None:
    soup = _soup("<p>Start Deprecated: Function x() is deprecated in /a/b.inc on line 12 end</p>")

    assert strip_server_errors(soup) == [
        ServerError("Deprecated", "Function x() is deprecated", "/a/b.inc", 12)
    ]
    assert " ".join(soup.get_text().split()) == "Start end"


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
    )

    assert strip_server_errors(soup) == []

    assert soup.get_text().count("/var/www/x.php") == 4


def test_keeps_prose_that_mentions_warnings() -> None:
    soup = _soup("<p>Warning: do not touch the wire in the lab.</p>")

    assert strip_server_errors(soup) == []

    assert soup.get_text() == "Warning: do not touch the wire in the lab."


def test_keeps_a_message_without_a_php_path() -> None:
    soup = _soup("<p>Notice: the lab is closed in /summer/ on line 3 of the plan.</p>")

    assert strip_server_errors(soup) == []


def test_linear_on_hostile_input() -> None:
    soup = _soup("<div><b>Warning</b>: " + "a " * 200_000 + "</div>")

    started = time.perf_counter()
    removed = strip_server_errors(soup)

    assert time.perf_counter() - started < 1
    assert removed == []


def test_linear_on_many_labels_close_to_a_line_number() -> None:
    chunk = "Warning: " * 100 + " in /" * 100 + "x on line 1 "
    soup = _soup("<div>" + chunk * 200 + "</div>")

    started = time.perf_counter()
    strip_server_errors(soup)

    assert time.perf_counter() - started < 1


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
    soup = _soup("<div>" + "Notice: a in /a.php on line 1 " * 40_000 + "</div>")

    started = time.perf_counter()
    errors = strip_server_errors(soup)

    assert time.perf_counter() - started < 1
    assert errors == [ServerError("Notice", "a", "/a.php", 1, count=40_000)]
    assert soup.get_text().strip() == ""


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
    soup = _soup("<div>start" + php_warning("Sport") * 20_000 + "end</div>")

    started = time.perf_counter()
    errors = strip_server_errors(soup)

    assert time.perf_counter() - started < 2
    assert [error.count for error in errors] == [20_000]
    assert soup.find("b") is None
    assert " ".join(soup.get_text().split()) == "start end"
