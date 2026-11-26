"""Solve the GePNIC document-download CAPTCHA (DocDownCaptcha) with local Tesseract OCR.

State e-procurement portals (NIC GePNIC) put every tender document behind a CAPTCHA form: the page
embeds the CAPTCHA as a base64 PNG and the answer is posted back together with the form's hidden
Tapestry fields. This module reads that image with Tesseract (the same engine the tracker already
uses for scanned PDFs), removing the coloured noise dots/lines and the solid black squares first.
Several Tesseract settings vote on the answer; while they disagree the CAPTCHA is refreshed for free
(a fresh GET renders a new image), and only an agreed reading is submitted.

Without Tesseract (or with captcha.enabled: false) solving is skipped and the portal keeps its old
behaviour: the tender's detail-page text is read instead of the documents.
"""

from __future__ import annotations

import base64
import logging
import re
import shutil
import tempfile
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin

import pymupdf

log = logging.getLogger(__name__)

WHITELIST = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
_TESSERACT_DEFAULT = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
_FORM = re.compile(r'id="frmCaptcha"')
_INPUT = re.compile(r'<input[^>]+name="([^"]+)"[^>]*value="([^"]*)"')
_IMAGE = re.compile(r'id="captchaImage"[^>]+src="data:image/png;base64,([^"]+)"')


def tesseract_command(configured: str = "") -> str | None:
    """The Tesseract executable: configured path, PATH, or the usual Windows install location."""
    if configured and Path(configured).is_file():
        return configured
    found = shutil.which("tesseract")
    if found:
        return found
    return _TESSERACT_DEFAULT if Path(_TESSERACT_DEFAULT).is_file() else None


def parse_captcha_form(html: str) -> tuple[dict, bytes | None]:
    """(hidden form fields, CAPTCHA png) of a DocDownCaptcha page; (empty, None) when there is none."""
    start = _FORM.search(html)
    if not start:
        return {}, None
    region = html[start.start():html.find("</form>", start.start())]
    fields = {m.group(1): m.group(2) for m in _INPUT.finditer(region)}
    image = _IMAGE.search(region)
    return fields, base64.b64decode(image.group(1)) if image else None


def clean_captcha(png: bytes) -> bytes:
    """White background, black text with its antialiasing; coloured noise and solid squares removed."""
    gray, w, h, _ = _clean_mask(png)
    out = bytearray(w * h * 3)
    for p in range(w * h):
        v = gray[p]
        o = p * 3
        out[o] = out[o + 1] = out[o + 2] = v
    return pymupdf.Pixmap(pymupdf.csRGB, w, h, bytes(out), 0).tobytes("png")


def _clean_mask(png: bytes) -> tuple[bytearray, int, int, bytearray]:
    pix = pymupdf.Pixmap(png)
    has_alpha = bool(pix.alpha)
    if pix.colorspace != pymupdf.csRGB:
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
    n = 4 if has_alpha else 3
    src = pix.samples
    w, h = pix.width, pix.height
    gray = bytearray(w * h)
    mask = bytearray(w * h)
    for p in range(w * h):
        i = p * n
        r, g, b = src[i], src[i + 1], src[i + 2]
        a = src[i + 3] / 255 if has_alpha else 1.0
        r = r * a + 255 * (1 - a)
        g = g * a + 255 * (1 - a)
        b = b * a + 255 * (1 - a)
        if max(r, g, b) - min(r, g, b) >= 70:      # coloured noise dot / line
            gray[p] = 255
        else:
            gray[p] = int((r + g + b) / 3)
            mask[p] = 1 if gray[p] < 120 else 0
    # Solid small squares are noise blocks; letters are larger and less filled.
    seen = bytearray(w * h)
    for start in range(w * h):
        if not mask[start] or seen[start]:
            continue
        stack, comp = [start], []
        seen[start] = 1
        x0 = y0 = 10 ** 9
        x1 = y1 = -1
        while stack:
            p = stack.pop()
            comp.append(p)
            x, y = p % w, p // w
            x0, y0 = min(x0, x), min(y0, y)
            x1, y1 = max(x1, x), max(y1, y)
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    nx, ny = x + dx, y + dy
                    if 0 <= nx < w and 0 <= ny < h:
                        q = ny * w + nx
                        if mask[q] and not seen[q]:
                            seen[q] = 1
                            stack.append(q)
        bw, bh = x1 - x0 + 1, y1 - y0 + 1
        if len(comp) <= 130 and len(comp) / (bw * bh) > 0.75 and 3 <= bw <= 14 and 3 <= bh <= 14:
            for p in comp:
                gray[p] = 255
                mask[p] = 0
    return gray, w, h, mask


def _char_images(png: bytes) -> list[bytes]:
    """Split the cleaned CAPTCHA into one image per character (column gaps), for single-char OCR."""
    gray, w, h, mask = _clean_mask(png)
    cols = [sum(mask[y * w + x] for y in range(h)) for x in range(w)]
    spans, start = [], None
    for x in range(w):
        if cols[x] > 0 and start is None:
            start = x
        elif cols[x] == 0 and start is not None:
            if x - start >= 3:
                spans.append((start, x))
            start = None
    if start is not None and w - start >= 3:
        spans.append((start, w))
    # merge spans separated by a 1-2 px gap (broken strokes of one glyph)
    merged = []
    for s in spans:
        if merged and s[0] - merged[-1][1] <= 2:
            merged[-1] = (merged[-1][0], s[1])
        else:
            merged.append(list(s) if False else (s[0], s[1]))
    out = []
    for x0, x1 in merged:
        if x1 - x0 < 4:
            continue
        cw, ch = x1 - x0, h
        buf = bytearray(b"\xff" * (cw * ch * 3))
        for y in range(h):
            for x in range(x0, x1):
                v = gray[y * w + x]
                o = (y * cw + (x - x0)) * 3
                buf[o] = buf[o + 1] = buf[o + 2] = v
        out.append(pymupdf.Pixmap(pymupdf.csRGB, cw, ch, bytes(buf), 0).tobytes("png"))
    return out


def _ocr_char(png: bytes, command: str) -> str:
    import pytesseract

    pytesseract.pytesseract.tesseract_cmd = command
    big = _scaled(png, 6, 10)
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
        fh.write(big)
        path = fh.name
    try:
        text = pytesseract.image_to_string(path, config=f"--psm 10 -c tessedit_char_whitelist={WHITELIST}")
    finally:
        Path(path).unlink(missing_ok=True)
    return re.sub(r"[^A-Za-z0-9]", "", text)[:1]


def _scaled(png: bytes, factor: int, pad: int) -> bytes:
    pix = pymupdf.Pixmap(png)
    w, h = pix.width * factor, pix.height * factor
    big = pymupdf.Pixmap(pix, w, h)
    cw, ch = w + 2 * pad, h + 2 * pad
    dst = bytearray(b"\xff" * (cw * ch * 3))
    src = big.samples
    for y in range(h):
        do = (y + pad) * cw * 3 + pad * 3
        so = y * big.stride
        dst[do:do + w * 3] = src[so:so + w * 3]
    return pymupdf.Pixmap(pymupdf.csRGB, cw, ch, bytes(dst), 0).tobytes("png")


def _ocr_tesseract(png: bytes, command: str, psm: int) -> str:
    import pytesseract

    pytesseract.pytesseract.tesseract_cmd = command
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as fh:
        fh.write(png)
        path = fh.name
    try:
        text = pytesseract.image_to_string(path, config=f"--psm {psm} -c tessedit_char_whitelist={WHITELIST}")
    finally:
        Path(path).unlink(missing_ok=True)
    return re.sub(r"[^A-Za-z0-9]", "", text)


def _ocr_mupdf(png: bytes, tessdata: Path) -> str:
    with pymupdf.open(stream=png, filetype="png") as doc:
        page = doc[0]
        tp = page.get_textpage_ocr(language="eng", dpi=300, full=True, tessdata=str(tessdata))
        text = page.get_text(textpage=tp)
    return re.sub(r"[^A-Za-z0-9]", "", text)


def read_captcha(png: bytes, command: str, tessdata: Path) -> list[str]:
    """Independent OCR readings of one CAPTCHA image (whole-line modes + per-character)."""
    big = _scaled(clean_captcha(png), 4, 14)
    vals = [_ocr_tesseract(big, command, psm) for psm in (7, 8, 13)]
    chars = [_ocr_char(c, command) for c in _char_images(png)]
    joined = "".join(c for c in chars if c)
    if joined:
        vals.append(joined)
    if tessdata.exists():
        try:
            vals.append(_ocr_mupdf(big, tessdata))
        except Exception as exc:  # noqa: BLE001 - one engine failing must not stop the others
            log.debug("CAPTCHA OCR (PyMuPDF) failed: %s", exc)
    return vals


def vote(vals: list[str]) -> tuple[str, int]:
    """The reading most engines agree on, and how many agree (0 = nothing usable)."""
    valid = [v for v in vals if 5 <= len(v) <= 7]
    if not valid:
        return "", 0
    return Counter(valid).most_common(1)[0]


def is_file(content: bytes) -> bool:
    return content[:4] == b"%PDF" or content[:2] == b"PK"


_AJAX_IMG = re.compile(r'<response id="captchaImage"[^>]*>.*?src="data:image/png;base64,([^"]+)"', re.S)


def refresh_captcha(fetcher, app: str, fields: dict) -> tuple[bytes | None, dict]:
    """Press the form's refresh button (a Tapestry event). The portal stores and returns a new CAPTCHA
    and rotates the form token, so the returned fields must be used for the next submit. Returns
    (new png, fields to submit with); (None, fields) when the refresh could not be read."""
    post = dict(fields)
    post["captchaText"] = ""
    post["methodArguments"] = "[]"
    post["beventtarget.id"] = "captcha"
    post["beventtype"] = "click"
    post["bcomponentid"] = "captcha"
    post["bcomponentidpath"] = "DocDownCaptcha/captcha"
    post["beventname"] = "onclick"
    fetcher._wait()
    try:
        resp = fetcher.session.post(app, data=post, timeout=(15, 60), verify=fetcher.verify,
                                    headers={"X-Requested-With": "XMLHttpRequest"})
    except Exception as exc:  # noqa: BLE001
        log.debug("CAPTCHA refresh failed: %s", exc)
        return None, fields
    m = _AJAX_IMG.search(resp.text)
    if m:
        return base64.b64decode(m.group(1)), fields
    new_fields, png = parse_captcha_form(resp.text)
    return png, (new_fields or fields)


def _download_links(html: str) -> list[tuple[str, str]]:
    """(href, label) of the unlocked file links on a tender page: the zip bundle first, then PDFs."""
    out = []
    for href, label in re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.S):
        text = " ".join(re.sub(r"<[^>]+>", " ", label).split())
        if "component=" in href and ("zip" in text.lower() or text.lower().endswith(".pdf")):
            out.append((href.replace("&amp;", "&"), text))
    out.sort(key=lambda pt: 0 if "zip" in pt[1].lower() else 1)
    return out


def solve(fetcher, app: str, url: str, config: dict, tessdata: Path,
          transform=lambda s: s) -> tuple[bytes | None, str]:
    """Download the file behind a GePNIC document link, solving its CAPTCHA on the way.

    Returns (file bytes, error). The fetcher's session is reused so the portal's session cookie and
    the form's token stay valid. A correct answer unlocks the document in the session and returns the
    tender page; the file is then fetched with a second GET of the same link.
    """
    command = tesseract_command(str(config.get("tesseract_path") or ""))
    if not command:
        return None, "Tesseract OCR is not installed; CAPTCHA not solved"
    attempts = max(1, int(config.get("max_attempts", 6)))
    refreshes = max(0, int(config.get("max_refreshes", 3)))
    resp = fetcher.get(url)
    if resp.error or not resp.status or resp.status >= 400:
        return None, resp.error or f"HTTP {resp.status}"
    if is_file(resp.content):
        return resp.content, ""
    fields, png = parse_captcha_form(resp.text)
    if png is None:
        return None, "document link showed no CAPTCHA form and no file"
    for attempt in range(1, attempts + 1):
        stored, fields = refresh_captcha(fetcher, app, fields)
        if stored:
            png = stored
        vals = read_captcha(png, command, tessdata)
        guess, agreed = vote(vals)
        free = 0
        while agreed < 2 and free < refreshes:
            free += 1
            stored, fields = refresh_captcha(fetcher, app, fields)
            if not stored:
                break
            png = stored
            vals = read_captcha(png, command, tessdata)
            guess, agreed = vote(vals)
        if not guess:
            log.info("[%s] CAPTCHA unreadable (attempt %d); refreshing", fetcher.site.code, attempt)
            continue
        post = dict(fields)
        post["captchaText"] = transform(guess)
        post["Submit"] = "Submit"
        fetcher._wait()
        try:
            answer = fetcher.session.post(app, data=post, timeout=(15, 90), verify=fetcher.verify)
        except Exception as exc:  # noqa: BLE001 - requests errors
            return None, f"{type(exc).__name__}: {str(exc)[:120]}"
        if is_file(answer.content):
            log.info("[%s] CAPTCHA solved on attempt %d (%d engines agreed)", fetcher.site.code, attempt, agreed)
            return answer.content, ""
        new_fields, new_png = parse_captcha_form(answer.text)
        if new_png is None:
            # Correct answer: the tender page now lists unlocked download links; fetch the file.
            for href, text in _download_links(answer.text):
                again = fetcher.get(urljoin(app, href))
                if is_file(again.content):
                    log.info("[%s] CAPTCHA solved on attempt %d (%d engines agreed); %s",
                             fetcher.site.code, attempt, agreed, text[:40])
                    return again.content, ""
            return None, "CAPTCHA accepted but no unlocked download link returned a file"
        fields, png = new_fields, new_png
    return None, f"CAPTCHA not solved in {attempts} attempts"
