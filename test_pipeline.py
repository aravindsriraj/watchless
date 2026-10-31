from pipeline import add_seconds, check_quote, guide_markdown, to_seconds

# auto-caption style: lowercase-ish, no punctuation, a few seconds per cue
CUES = [
    (60.0, "okay so first we need a table"),
    (68.5, "here I select a blank table and rename this"),
    (71.2, "table with company enrichment test"),
    (90.0, "now we add the domain column"),
    (400.0, "and that is the funding waterfall"),
]


def test_to_seconds():
    assert to_seconds("04:28") == 268
    assert to_seconds("1:02:03") == 3723
    assert to_seconds("0:07") == 7
    assert add_seconds({"steps": [{"time": "01:10"}], "start": "00:30", "end": "02:00"}) == {
        "steps": [{"time": "01:10", "t_s": 70}], "start": "00:30", "end": "02:00", "start_s": 30, "end_s": 120}


def test_real_quote_verifies_and_snaps():
    # punctuation/case differ from captions, and Gemini's time is a few seconds off
    ok, t = check_quote("Here I select a blank table and rename this table with Company Enrichment test.", 75, CUES)
    assert ok is True and t == 68


def test_fabricated_quote_fails():
    ok, t = check_quote("Always use five providers and set the credit limit to fifty per row", 70, CUES)
    assert ok is False and t == 70


def test_real_quote_at_wrong_time_fails():
    # the words exist, but 5+ minutes away from where the guide claims
    ok, _ = check_quote("and that is the funding waterfall", 70, CUES)
    assert ok is False


def test_stitched_quote_checks_every_part():
    ok, t = check_quote("here I select a blank table... now we add the domain column", 70, CUES)
    assert ok is True and t == 68
    ok, _ = check_quote("here I select a blank table... and then we delete every single row", 70, CUES)
    assert ok is False  # second half was made up


def test_no_captions_is_unknown():
    assert check_quote("anything", 10, []) == (None, 10)


def test_markdown_has_links_marks_and_images():
    guide = {"title": "T", "summary": "S", "sections": [{
        "heading": "Setup", "start_s": 60, "end_s": 120, "intro_md": "",
        "steps": [{"text_md": "Create a table", "t_s": 68, "quote": "q", "verified": True},
                  {"text_md": "Invented step", "t_s": 95, "quote": "q", "verified": False}],
        "screenshots": [{"t_s": 70, "caption": "The new table", "file": "shot_00070.jpg"},
                        {"t_s": 99, "caption": "Missing", "file": None}]}]}
    md = guide_markdown(guide, {"url": "https://www.youtube.com/watch?v=abc", "title": "V", "prompt": "p", "duration": 130})
    assert "[▶ 1:08](https://www.youtube.com/watch?v=abc&t=68s) ✓" in md
    assert "⚠" in md and "1/2 checked" in md
    assert "![The new table](images/shot_00070.jpg)" in md
    assert "Missing" not in md  # failed screenshots are left out, not broken images


def test_step_sub_list_stays_inside_its_step():
    import markdown
    guide = {"title": "T", "summary": "S", "sections": [{
        "heading": "H", "start_s": 0, "end_s": 9, "intro_md": "", "screenshots": [],
        "steps": [{"text_md": "Pick one:\n- **A**: x\n- **B**: y", "t_s": 5, "quote": "q", "verified": True},
                  {"text_md": "Next", "t_s": 8, "quote": "q", "verified": True}]}]}
    html = markdown.markdown(guide_markdown(guide, {"url": "u?v=1", "title": "V", "prompt": "p"}), extensions=["extra", "sane_lists"])
    assert "<ul>" in html and "<strong>A</strong>" in html
    assert html.count("<li>") == 4  # 2 steps + 2 sub-bullets, numbering unbroken
    assert "Pick one: [▶ 0:05]" not in html and "0:05</a> ✓" in html
