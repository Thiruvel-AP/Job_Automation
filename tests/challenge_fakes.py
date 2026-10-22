"""Page and element fakes for the bot-check detection (apply/formfind.py): no browser. An element answers only what Playwright answers for
the questions `formfind.challenge_shown` asks (is it displayed, how big, what is its src / data-size, what tag)."""
from types import SimpleNamespace


class ChallengeEl:
    def __init__(self, width: float = 304, height: float = 78, visible: bool = True, tag: str = "iframe", **attrs):
        self.width, self.height, self.visible, self.tag = width, height, visible, tag
        self.attrs = {k.replace("_", "-"): v for k, v in attrs.items()}        # data_size="invisible" -> data-size

    def is_visible(self):
        return self.visible

    def bounding_box(self):
        return {"x": 0, "y": 0, "width": self.width, "height": self.height} if self.visible else None

    def get_attribute(self, name):
        return self.attrs.get(name)

    def evaluate(self, js):
        return self.tag.upper()


class ChallengePage:
    """`found` = {selector: [elements]}; a selector that is not in it matches nothing. `text` = the visible text of the page."""

    def __init__(self, found=None, text=""):
        self.found, self.text = found or {}, text
        self.url, self.context = "https://boards.example.test/job", SimpleNamespace(pages=[])

    def query_selector_all(self, sel):
        return list(self.found.get(sel, []))

    def inner_text(self, sel="body"):
        return self.text
