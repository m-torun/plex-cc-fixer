"""What the owner of a library has to say about one show, from a file of their own.

The code knows no show. shows/<Show>.md, named after the show's folder in the library without its year, holds what
applies to that show alone. example.md describes the format.
"""
import os
import re

FOLDER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shows")
OPTIONS = re.compile(r"\A---\n(.*?)\n---\n?", re.S)


def show_name(path):
    """The show a recording belongs to: .../Show (Year)/Season 52/file.ts is "Show"."""
    folder = os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(path))))
    return re.sub(r"\s*\(\d{4}\)$", "", folder)


def load(path, folder=FOLDER):
    """A recording's show file as {"notes": text for the research prompt, "lyrics": bool, "lead_in": seconds or None};
    empty when it has none."""
    show = {"notes": "", "lyrics": False, "lead_in": None}
    try:
        with open(os.path.join(folder, show_name(path) + ".md"), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return show
    options = OPTIONS.match(text)
    if options:
        text = text[options.end():]
        for line in options.group(1).splitlines():
            key, _, value = line.partition(":")
            if key.strip() == "lyrics":
                show["lyrics"] = value.strip().lower() in ("true", "yes", "on")
            elif key.strip() == "lead_in":
                try:
                    show["lead_in"] = max(0.0, float(value))
                except ValueError:
                    pass
    show["notes"] = re.sub(r"<!--.*?-->", "", text, flags=re.S).strip()
    return show
