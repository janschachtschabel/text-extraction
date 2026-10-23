import pytest

from text_extraction.envelope import ErrorCode
from text_extraction.quality import Quality, assess, count_words, quality_error

ARTICLE_TEXT = " ".join(
    f"Sentence number {i} explains how photosynthesis turns light into sugar." for i in range(40)
)
ARTICLE_HTML = f"<html><head><title>Photosynthesis</title></head><body><main><h1>Photosynthesis</h1><p>{ARTICLE_TEXT}</p></main></body></html>"  # noqa: E501


def test_count_words_counts_tokens_with_letters_or_digits() -> None:
    assert count_words("") == 0
    assert count_words("# Title\n\n| a | b |\n| --- | --- |\n- item 42 ... **bold**") == 6
    assert count_words("naïve café 3,5") == 3


def test_normal_article_passes() -> None:
    q = assess(ARTICLE_HTML, ARTICLE_TEXT, min_words=50)
    assert q.word_count > 300
    assert q.visible_chars > 500
    assert q.script_count == 0
    assert not q.js_heavy
    assert not q.bot_challenge
    assert not q.soft_404
    assert not q.login_wall
    assert not q.browser_recommended
    assert quality_error(q, 50) is None


def test_js_heavy_spa_shell() -> None:
    html = (
        '<html><head><script src="/app.js"></script></head>'
        '<body><div id="root"></div><script>boot()</script></body></html>'
    )
    q = assess(html, "", min_words=50)
    assert q.js_heavy
    assert q.script_count == 2
    assert q.visible_chars == 0
    assert q.browser_recommended
    assert quality_error(q, 50) == ("failed", ErrorCode.EMPTY_CONTENT)


@pytest.mark.parametrize(
    "html",
    [
        "<body><div id='app'></div></body>",
        "<body><div id='__next'> </div></body>",
        "<body><noscript>You need to enable JavaScript to run this app.</noscript></body>",
        "<body><p>Loading</p><script src='x.js'></script></body>",
    ],
)
def test_js_heavy_hints(html: str) -> None:
    assert assess(html, "Loading", min_words=50).js_heavy


@pytest.mark.parametrize(
    "html",
    [
        "<body><p>A short static page.</p></body>",
        "<body><div id='root'>Rendered on the server</div></body>",
        "<body><p>Static</p><script type='application/ld+json'>{}</script></body>",
    ],
)
def test_short_static_pages_are_not_js_heavy(html: str) -> None:
    q = assess(html, "A short static page.", min_words=50)
    assert not q.js_heavy
    assert not q.browser_recommended


def test_long_page_with_scripts_is_not_js_heavy() -> None:
    html = f"<body><p>{ARTICLE_TEXT}</p><script src='a.js'></script></body>"
    q = assess(html, ARTICLE_TEXT, min_words=50)
    assert q.script_count == 1
    assert not q.js_heavy


def test_scripts_and_styles_are_not_visible_text() -> None:
    html = "<body><style>p{color:red}</style><script>var a = 1;</script><p>Hello   world</p></body>"
    assert assess(html, "Hello world", min_words=1).visible_chars == len("Hello world")


def test_cloudflare_challenge() -> None:
    html = (
        "<html><head><title>Just a moment...</title></head><body>"
        "<h1>example.org</h1><p>Verifying you are human. This may take a few seconds.</p>"
        "<p>Enable JavaScript and cookies to continue</p>"
        "<script>window._cf_chl_opt = {};</script></body></html>"
    )
    q = assess(html, "Verifying you are human. This may take a few seconds.", min_words=50)
    assert q.bot_challenge
    assert q.browser_recommended
    assert quality_error(q, 50) == ("failed", ErrorCode.BOT_PROTECTION)


@pytest.mark.parametrize(
    "text",
    [
        "Checking your browser before accessing the site.",
        "Making sure you\N{RIGHT SINGLE QUOTATION MARK}re not a bot!  Anubis uses Proof-of-Work.",
        "Please complete the CAPTCHA to continue.",
        "Access denied. Protected by   DataDome.",
        "Press & hold to confirm you are a human. PerimeterX",
    ],
)
def test_bot_challenge_phrases(text: str) -> None:
    q = assess(f"<body><p>{text}</p></body>", text, min_words=50)
    assert q.bot_challenge


def test_challenge_markup_marker() -> None:
    html = '<body><p>Please wait</p><form action="/?__cf_chl_f_tk=x"></form></body>'
    assert assess(html, "Please wait", min_words=50).bot_challenge


def test_long_article_mentioning_captcha_is_not_a_bot_challenge() -> None:
    text = ARTICLE_TEXT + " A captcha tells computers and humans apart. Just a moment, please."
    q = assess(f"<body><p>{text}</p></body>", text, min_words=50)
    assert not q.bot_challenge


def test_soft_404_short_page() -> None:
    text = "Seite nicht gefunden. Die angeforderte Seite existiert nicht."
    q = assess(f"<body><h1>{text}</h1></body>", text, min_words=50)
    assert q.soft_404
    assert quality_error(q, 50) == ("failed", ErrorCode.LIKELY_ERROR_PAGE)
    english = assess("<body><h1>Error 404</h1></body>", "Oops! Page   Not\nFound", min_words=1)
    assert english.soft_404


def test_long_page_mentioning_404_is_not_soft_404() -> None:
    text = ARTICLE_TEXT + " The HTTP status 404 means page not found."
    q = assess(f"<body><p>{text}</p></body>", text, min_words=50)
    assert q.word_count > 50
    assert not q.soft_404


def test_numbers_containing_404_are_not_soft_404() -> None:
    q = assess(None, "Invoice 14040 total 1404 euro", min_words=1)
    assert not q.soft_404


def test_login_wall() -> None:
    html = (
        "<body><h1>Sign in</h1><form><input type='text' name='user'>"
        "<input type='PASSWORD' name='pw'><button>Log in</button></form></body>"
    )
    q = assess(html, "Sign in Log in", min_words=50)
    assert q.login_wall
    assert quality_error(q, 50) == ("failed", ErrorCode.LOGIN_REQUIRED)


def test_article_with_comment_form_is_not_a_login_wall() -> None:
    html = f"<body><p>{ARTICLE_TEXT}</p><input type='password'></body>"
    assert not assess(html, ARTICLE_TEXT, min_words=50).login_wall


def test_too_short_is_partial() -> None:
    text = "A short but real page with ten words and nothing wrong."
    q = assess(f"<body><p>{text}</p></body>", text, min_words=50)
    assert q.word_count == 11
    assert quality_error(q, 50) == ("partial", ErrorCode.CONTENT_TOO_SHORT)
    assert quality_error(q, 11) is None
    assert quality_error(q, 0) is None


def test_empty_is_failed() -> None:
    q = assess("<body><p>Something</p></body>", "  \n ", min_words=50)
    assert q.word_count == 0
    assert quality_error(q, 50) == ("failed", ErrorCode.EMPTY_CONTENT)
    assert quality_error(q, 0) == ("failed", ErrorCode.EMPTY_CONTENT)


def test_plain_text_has_no_markup_signals() -> None:
    q = assess(None, ARTICLE_TEXT, min_words=50)
    assert q.script_count == 0
    assert q.visible_chars == len(" ".join(ARTICLE_TEXT.split()))
    assert not q.js_heavy
    assert not q.login_wall
    assert quality_error(q, 50) is None


def test_plain_text_can_still_be_a_challenge_or_soft_404() -> None:
    assert assess(
        None, "Checking your browser before accessing example.org", min_words=1
    ).bot_challenge
    assert assess(None, "404 - page not found", min_words=1).soft_404


def test_error_priority() -> None:
    everything = Quality(
        visible_chars=10,
        word_count=0,
        script_count=0,
        js_heavy=False,
        bot_challenge=True,
        soft_404=True,
        login_wall=True,
        browser_recommended=True,
    )
    assert quality_error(everything, 50) == ("failed", ErrorCode.BOT_PROTECTION)
    no_bot = everything.model_copy(update={"bot_challenge": False})
    assert quality_error(no_bot, 50) == ("failed", ErrorCode.LIKELY_ERROR_PAGE)
    only_login = no_bot.model_copy(update={"soft_404": False})
    assert quality_error(only_login, 50) == ("failed", ErrorCode.LOGIN_REQUIRED)
