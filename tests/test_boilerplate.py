from text_extraction.boilerplate import load_boilerplate_rules, strip_boilerplate

RULES = {"example.org": frozenset({"Share this page", "| Title |"})}


def test_strip_boilerplate_exact_lines_only() -> None:
    text = (
        "Intro\n"
        "  Share this page  \n"
        "Share this page now\n"
        "| Title |\n"
        "Body text with | Title | inside\n"
    )
    assert strip_boilerplate("example.org", text, RULES) == (
        "Intro\nShare this page now\nBody text with | Title | inside\n"
    )


def test_www_host_matches() -> None:
    text = "Share this page\nBody"
    assert strip_boilerplate("www.example.org", text, RULES) == "Body"
    assert strip_boilerplate("WWW.Example.org.", text, RULES) == "Body"
    assert (
        strip_boilerplate("example.org", text, {"www.example.org": RULES["example.org"]}) == "Body"
    )


def test_other_hosts_and_subdomains_are_untouched() -> None:
    text = "Share this page\nBody"
    assert strip_boilerplate("other.org", text, RULES) is text
    assert strip_boilerplate("blog.example.org", text, RULES) is text
    assert strip_boilerplate("example.org", "", RULES) == ""


def test_packaged_rules_load_once_and_are_read_only() -> None:
    rules = load_boilerplate_rules()
    assert load_boilerplate_rules() is rules
    assert "No compatible source was found for this media." in rules["av.tib.eu"]
    assert not any(host.startswith("www.") for host in rules)
    assert all(isinstance(lines, frozenset) and lines for lines in rules.values())
    assert strip_boilerplate("www.av.tib.eu", "Formal Metadata\nLecture notes", rules) == (
        "Lecture notes"
    )
