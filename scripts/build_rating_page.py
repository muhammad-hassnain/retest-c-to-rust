#!/usr/bin/env python3
"""Build the blind second-rating page of the results explorer: explorer/rate.html.

Reads docs/reviews/second-rater/cases-spec.json (12 blinded cases: system, input, the C and Rust
outputs, and the C and Rust source paths, without the first rater's labels). The source files are
third-party code and are not embedded: the page reads them from artifacts/ through explorer/serve.py
(GET /src?path=...), so it needs the fetched artifacts. A path ending in `#extracted_code` names an
AdaTrans conversation JSON; the server returns its last non-empty extracted_code (the final Rust). Answers are saved by explorer/serve.py to
docs/reviews/second-rater/ratings.json (POST /api/ratings); without the server they stay in the
browser (localStorage) and can be copied out.

    python3 -I scripts/build_rating_page.py      # then: python3 explorer/serve.py -> /rate.html
"""
import json, os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    spec = json.load(open(os.path.join(ROOT, "docs/reviews/second-rater/cases-spec.json")))
    name = lambda p: os.path.basename(p.split("#")[0])[:-5] + ".rs" if p.endswith("#extracted_code") else os.path.basename(p)
    files = lambda ps: [{"name": name(p), "path": p} for p in ps]
    cases = [dict(c, c=files(c["c"]), rust=files(c["rust"])) for c in spec]
    tpl = open(os.path.join(ROOT, "scripts/rating_template.html")).read()
    data = json.dumps(cases, ensure_ascii=False).replace("</", "<\\/").replace("<!--", "<\\!--")
    page = ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n</head>\n<body>\n'
            + tpl.replace("__CASES__", data) + "\n</body>\n</html>\n")
    out = os.path.join(ROOT, "explorer/rate.html")
    open(out, "w").write(page)
    print("wrote", out, f"({len(page) // 1024} KB, {len(cases)} cases)")


if __name__ == "__main__":
    main()
