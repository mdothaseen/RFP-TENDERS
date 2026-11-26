"""GePNIC download-CAPTCHA handling: form parsing, noise cleaning, voting and unlocked links."""

import base64

import pymupdf

from tender_tracker import captcha


def _png() -> bytes:
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 12), 0)
    pix.set_rect(pix.irect, (255, 255, 255))
    return pix.tobytes("png")


FORM = f"""<html><body>
<form method="post" action="/nicgep/app" id="frmCaptcha" autocomplete="off">
<input type="hidden" name="formids" value="tokenSecret,captchaText,Submit" />
<input type="hidden" name="component" value="frmCaptcha" />
<input type="hidden" name="page" value="DocDownCaptcha" />
<input type="hidden" name="tokenSecret" id="tokenSecret" value="Sabc123" />
<input type="text" name="captchaText" value="" id="captchaText" />
<img name="captchaImage" id="captchaImage" src="data:image/png;base64,{base64.b64encode(_png()).decode()}" />
<input type="submit" name="Submit" id="Submit" value="Submit" />
</form></body></html>"""


def test_form_fields_and_image_are_read():
    fields, png = captcha.parse_captcha_form(FORM)
    assert fields["tokenSecret"] == "Sabc123" and fields["page"] == "DocDownCaptcha"
    assert png == _png()


def test_no_form_gives_nothing():
    fields, png = captcha.parse_captcha_form("<html>just a page</html>")
    assert fields == {} and png is None


def test_cleaning_keeps_size_and_removes_colour():
    # white bg + black text + a blue noise dot and a solid black square
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 30, 12), 0)
    pix.set_rect(pix.irect, (255, 255, 255))
    pix.set_rect(pymupdf.IRect(2, 2, 4, 10), (0, 0, 0))        # letter stroke (vertical)
    pix.set_rect(pymupdf.IRect(2, 8, 8, 10), (0, 0, 0))        # letter stroke (foot) -> L shape
    pix.set_rect(pymupdf.IRect(12, 2, 14, 4), (0, 0, 255))     # blue noise dot
    pix.set_rect(pymupdf.IRect(20, 2, 24, 6), (0, 0, 0))       # solid noise square
    cleaned = pymupdf.Pixmap(captcha.clean_captcha(pix.tobytes("png")))
    assert (cleaned.width, cleaned.height) == (30, 12)
    assert cleaned.pixel(13, 3)[:3] == (255, 255, 255)        # blue dot removed
    assert cleaned.pixel(22, 4)[:3] == (255, 255, 255)        # solid square removed
    assert cleaned.pixel(3, 5)[:3] == (0, 0, 0)               # letter kept


def test_vote_prefers_agreement():
    guess, n = captcha.vote(["Ab12Cd", "Ab12Cd", "Xx99Yy", "Ab12Cd"])
    assert guess == "Ab12Cd" and n == 3
    assert captcha.vote(["", "a"]) == ("", 0)


def test_unlocked_links_prefer_the_zip():
    html = ('<a href="/app?component=%24DirectLink_0&amp;sp=A">Tendernotice_1.pdf</a>'
            '<a href="/app?component=%24DirectLink_7&amp;sp=B">Download as zip file</a>'
            '<a href="/app?page=Home">Home</a>')
    links = captcha._download_links(html)
    assert [t for _, t in links] == ["Download as zip file", "Tendernotice_1.pdf"]
    assert links[0][0] == "/app?component=%24DirectLink_7&sp=B"


def test_is_file():
    assert captcha.is_file(b"%PDF-1.4 ...") and captcha.is_file(b"PK\x03\x04zip")
    assert not captcha.is_file(b"<html>")
