"""Certificate PDF: A4 landscape with the same content as the web page plus a
QR code of the verification URL. Only this module imports the PDF library."""

import logging
from io import BytesIO
from pathlib import Path

from PIL import Image as PILImage
from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as pdf_canvas

from exams.records import CertificateRecord
from services.email_templates import BRAND_INK, BRAND_MUTED, BRAND_PURPLE

logger = logging.getLogger("webnest.exams.pdf")

_ASSETS = Path(__file__).resolve().parents[2] / "assets"
_LOGO_PATH = _ASSETS / "logo.png"
_PAGE = landscape(A4)

# The designed certificate (assets/certificate_template.jpg, 1536 x 1024,
# "COURSE NAME HERE" placeholder removed). The page keeps the design's 3:2
# shape at A4-landscape width; positions below are in template pixels.
_TEMPLATE_PATH = _ASSETS / "certificate_template.jpg"
_TEMPLATE_SIZE = (1536, 1024)
_TEMPLATE_PAGE = (_PAGE[0], _PAGE[0] * _TEMPLATE_SIZE[1] / _TEMPLATE_SIZE[0])
_GOLD = "#B8862B"
_INK = "#141414"
_MUTED = "#55524c"


def _load_logo():
    """The logo, downscaled once to print size (the asset is 1254 px and
    ~680 KB; embedding it whole made every certificate ~900 KB). None when
    the file is missing or corrupt - the certificate is drawn without it."""
    if not _LOGO_PATH.exists():
        return None
    try:
        with PILImage.open(_LOGO_PATH) as img:
            img.load()
            small = img.convert("RGBA")
            small.thumbnail((300, 300))
        out = BytesIO()
        small.save(out, format="PNG", optimize=True)
        out.seek(0)
        return ImageReader(out)
    except Exception:
        logger.error("logo.png could not be loaded, rendering certificates without it", exc_info=True)
        return None


def _fit_font(c, text: str, font: str, size: float, max_width: float, min_size: float = 12) -> float:
    while size > min_size and c.stringWidth(text, font, size) > max_width:
        size -= 1
    return size


def _split_two(c, text: str, font: str, size: float) -> list[str]:
    """Two lines as even in width as possible; a single over-long word is
    split by characters."""
    words = text.split()
    if len(words) < 2:
        middle = max(1, len(text) // 2)
        return [text[:middle], text[middle:]]
    best, best_width = [text], float("inf")
    for i in range(1, len(words)):
        lines = [" ".join(words[:i]), " ".join(words[i:])]
        widest = max(c.stringWidth(line, font, size) for line in lines)
        if widest < best_width:
            best, best_width = lines, widest
    return best


def fit_lines(c, text: str, font: str, size: float, min_size: float, max_width: float) -> tuple[float, list[str]]:
    """(font size, lines) so `text` fits `max_width`: one line, shrinking down
    to min_size; else two lines, shrinking down to 70% of min_size; else (only
    for absurdly long text) two lines cut with an ellipsis. Never overflows."""
    single = _fit_font(c, text, font, size, max_width, min_size)
    if c.stringWidth(text, font, single) <= max_width:
        return single, [text]
    floor = max(6.0, min_size * 0.7)
    current = min_size
    while current >= floor:
        lines = _split_two(c, text, font, current)
        if all(c.stringWidth(line, font, current) <= max_width for line in lines):
            return current, lines
        current -= 0.5
    lines = _split_two(c, text, font, floor)
    clipped = []
    for line in lines:
        while line and c.stringWidth(line + "…", font, floor) > max_width:
            line = line[:-1]
        clipped.append(line + ("…" if line != lines[len(clipped)] else ""))
    return floor, clipped


def _draw_centred_lines(c, lines: list[str], x: float, last_baseline: float, leading: float) -> None:
    """Draws lines centred on x, the last one on last_baseline."""
    for offset, line in enumerate(reversed(lines)):
        c.drawCentredString(x, last_baseline + offset * leading, line)


def _load_template():
    if not _TEMPLATE_PATH.exists():
        logger.warning("certificate_template.jpg is missing; using the plain certificate layout")
        return None
    try:
        with PILImage.open(_TEMPLATE_PATH) as img:
            img.load()
            if img.size != _TEMPLATE_SIZE:
                logger.error("certificate_template.jpg is %s, expected %s; using the plain layout", img.size, _TEMPLATE_SIZE)
                return None
        return ImageReader(str(_TEMPLATE_PATH))
    except Exception:
        logger.error("certificate_template.jpg could not be loaded; using the plain layout", exc_info=True)
        return None


def _draw_qr(c, url: str, x: float, y: float, size: float) -> None:
    widget = QrCodeWidget(url)
    x1, y1, x2, y2 = widget.getBounds()
    drawing = Drawing(size, size, transform=[size / (x2 - x1), 0, 0, size / (y2 - y1), 0, 0])
    drawing.add(widget)
    renderPDF.draw(drawing, c, x, y)


class CertificatePdfRenderer:
    def __init__(self) -> None:
        self._logo = _load_logo()
        self._template = _load_template()

    def render(self, certificate: CertificateRecord, verify_url: str) -> bytes:
        if self._template is not None:
            return self._render_designed(certificate, verify_url)
        return self._render_plain(certificate, verify_url)

    def _render_designed(self, certificate: CertificateRecord, verify_url: str) -> bytes:
        buffer = BytesIO()
        width, height = _TEMPLATE_PAGE
        scale = width / _TEMPLATE_SIZE[0]

        def at(x_px: float, y_px: float) -> tuple[float, float]:
            return x_px * scale, height - y_px * scale

        c = pdf_canvas.Canvas(buffer, pagesize=_TEMPLATE_PAGE)
        c.setTitle(f"WebNest Studio certificate {certificate.certificate_id}")
        c.setAuthor("WebNest Studio")
        c.drawImage(self._template, 0, 0, width, height)

        # Recipient name, centred on the gold line.
        size, lines = fit_lines(c, certificate.recipient_name, "Times-Bold", 30, 16, 840 * scale)
        c.setFillColor(colors.HexColor(_INK))
        c.setFont("Times-Bold", size)
        x, baseline = at(872, 512)
        _draw_centred_lines(c, lines, x, baseline, size * 1.15)

        # Course title where the design's placeholder was (two lines fit
        # between "has successfully completed the" and the description).
        size, lines = fit_lines(c, certificate.course_title.upper(), "Times-Bold", 31, 16, 800 * scale)
        c.setFillColor(colors.HexColor(_GOLD))
        c.setFont("Times-Bold", size)
        x, baseline = at(880, 641 if len(lines) == 1 else 646)
        _draw_centred_lines(c, lines, x, baseline, size * 1.1)

        # Date above "DATE OF COMPLETION".
        c.setFillColor(colors.HexColor(_INK))
        c.setFont("Helvetica", 11)
        c.drawCentredString(*at(574, 828), f"{certificate.issued_at:%B %d, %Y}")

        # Verification details and QR code in the empty lower area.
        c.setFillColor(colors.HexColor(_MUTED))
        c.setFont("Helvetica", 7.5)
        c.drawString(*at(430, 905), f"Certificate ID: {certificate.certificate_id}")
        c.setFont("Helvetica", 6.5)
        c.drawString(*at(430, 925), f"Verify at {verify_url}")
        qr_px = 128
        qr_x, qr_y = at(960, 795 + qr_px)
        _draw_qr(c, verify_url, qr_x, qr_y, qr_px * scale)
        if certificate.status != "valid":
            c.setFillColor(colors.HexColor("#b42318"))
            c.setFont("Helvetica-Bold", 14)
            c.drawCentredString(*at(872, 450), "REVOKED")

        c.showPage()
        c.save()
        return buffer.getvalue()

    def _render_plain(self, certificate: CertificateRecord, verify_url: str) -> bytes:
        buffer = BytesIO()
        width, height = _PAGE
        c = pdf_canvas.Canvas(buffer, pagesize=_PAGE)
        c.setTitle(f"WebNest Studio certificate {certificate.certificate_id}")
        c.setAuthor("WebNest Studio")

        purple = colors.HexColor(BRAND_PURPLE)
        ink = colors.HexColor(BRAND_INK)
        muted = colors.HexColor(BRAND_MUTED)

        c.setStrokeColor(purple)
        c.setLineWidth(4)
        c.rect(12 * mm, 12 * mm, width - 24 * mm, height - 24 * mm)
        c.setLineWidth(1)
        c.rect(16 * mm, 16 * mm, width - 32 * mm, height - 32 * mm)

        top = height - 30 * mm
        if self._logo is not None:
            try:
                c.drawImage(self._logo, width / 2 - 11 * mm, top - 22 * mm, 22 * mm, 22 * mm, mask="auto")
            except Exception:
                logger.warning("Could not draw the logo on a certificate", exc_info=True)
        y = top - 30 * mm

        c.setFillColor(ink)
        c.setFont("Helvetica-Bold", 14)
        c.drawCentredString(width / 2, y, "WebNest Studio")
        y -= 14 * mm
        c.setFillColor(purple)
        c.setFont("Helvetica-Bold", 30)
        c.drawCentredString(width / 2, y, "Certificate of completion")
        y -= 16 * mm

        c.setFillColor(muted)
        c.setFont("Helvetica", 13)
        c.drawCentredString(width / 2, y, "This certifies that")
        y -= 14 * mm

        # Helvetica covers Latin-1; anything else is drawn as "?" by reportlab
        # rather than failing, so the PDF always renders.
        name = certificate.recipient_name
        c.setFillColor(ink)
        size, lines = fit_lines(c, name, "Helvetica-Bold", 28, 14, width - 80 * mm)
        c.setFont("Helvetica-Bold", size)
        _draw_centred_lines(c, lines, width / 2, y, size * 1.15)
        y -= 12 * mm

        c.setFillColor(muted)
        c.setFont("Helvetica", 13)
        c.drawCentredString(width / 2, y, "has completed the course and passed its final exam")
        y -= 13 * mm

        c.setFillColor(ink)
        title_size, lines = fit_lines(c, certificate.course_title, "Helvetica-Bold", 20, 12, width - 80 * mm)
        c.setFont("Helvetica-Bold", title_size)
        _draw_centred_lines(c, lines, width / 2, y, title_size * 1.15)

        c.setFillColor(muted)
        c.setFont("Helvetica", 10)
        left = 28 * mm
        c.drawString(left, 40 * mm, f"Issued: {certificate.issued_at:%B %d, %Y}")
        c.drawString(left, 34 * mm, f"Certificate ID: {certificate.certificate_id}")
        c.drawString(left, 28 * mm, f"Verify at: {verify_url}")
        if certificate.status != "valid":
            c.setFillColor(colors.HexColor("#b42318"))
            c.setFont("Helvetica-Bold", 12)
            c.drawString(left, 46 * mm, "REVOKED")

        qr_size = 34 * mm
        _draw_qr(c, verify_url, width - 28 * mm - qr_size, 24 * mm, qr_size)

        c.showPage()
        c.save()
        return buffer.getvalue()

