from html.parser import HTMLParser

from wavebench.harness.preview import PreviewIdentity


class Page(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.elements = []
        self.text = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def handle_data(self, data):
        self.text.append(data)


def test_identity_escapes_user_text_and_preserves_preview_path():
    name = 'Model "A" <script>alert(1)</script>'
    prompt = 'Build <canvas> & "buttons"\nwith keyboard controls'
    identity = PreviewIdentity(name, "vendor/model", 2, "aabbccdd00112233", prompt)
    page = Page(identity.page('/game/index.html?mode="easy"&sound=1', 2).decode())
    assert not any(tag == "script" for tag, attrs in page.elements)
    frame = next(attrs for tag, attrs in page.elements if tag == "iframe")
    assert frame["src"] == '/game/index.html?mode="easy"&sound=1'
    assert frame["title"] == f"#2 · {name} generated app"
    text = "".join(page.text)
    assert identity.label in text
    assert "Attempt 2" in text
    assert 'Build <canvas> & "buttons" with keyboard controls' in text
    assert "vendor/model" in next(attrs["title"] for tag, attrs in page.elements if tag == "h1")


def test_repeated_models_and_runs_have_distinct_stable_labels():
    first = PreviewIdentity("Same model", "vendor/model", 1, "12345678aaaa", "same prompt")
    second = PreviewIdentity("Same model", "vendor/model", 2, "12345678aaaa", "same prompt")
    rerun = PreviewIdentity("Same model", "vendor/model", 1, "87654321bbbb", "same prompt")
    assert len({first.label, second.label, rerun.label}) == 3
    assert first.label in first.page("/", 1).decode()
    assert first.label in first.page("/", 2).decode()


def test_labels_remove_terminal_controls_and_keep_long_prompts_in_header():
    prompt = "word " * 100
    identity = PreviewIdentity("Model\x1b\x07\nname", "vendor/model", 1, "abcdefgh", prompt)
    source = identity.page("/", 1).decode()
    page = Page(source)
    assert "\x1b" not in source and "\x07" not in source
    assert identity.label == "#1 · Model name · Run abcdefgh"
    assert prompt.strip() in next(attrs["title"] for tag, attrs in page.elements if tag == "p")
