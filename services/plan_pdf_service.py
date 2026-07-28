import logging
from datetime import datetime, timezone
from html import escape
from io import BytesIO
from pathlib import Path

from PIL import Image as PILImage
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib import colors

from core.config import Settings
from services.email_templates import BRAND_INK, BRAND_MUTED, BRAND_PURPLE

logger = logging.getLogger("webnest.plan_pdf")

_LOGO_PATH = Path(__file__).resolve().parent.parent / "assets" / "logo.png"


def _logo_is_valid() -> bool:
    """reportlab's Image flowable is lazy - it doesn't actually decode the
    file until doc.build() runs deep inside the layout engine, by which
    point a try/except around Image(...) construction is too late to catch
    a corrupted file. Eagerly opening it with PIL here (cheap - a static
    asset, not something rendered per-request) means a bad/corrupted logo
    just gets skipped instead of 500ing every plan PDF download."""
    if not _LOGO_PATH.exists():
        return False
    try:
        with PILImage.open(_LOGO_PATH) as img:
            img.verify()
        return True
    except Exception:
        logger.error("logo.png failed validation, rendering the plan PDF without it", exc_info=True)
        return False

_TITLE_STYLE = ParagraphStyle(
    "PlanTitle",
    fontName="Helvetica-Bold",
    fontSize=20,
    leading=25,  # reportlab's ParagraphStyle defaults leading to a flat 12pt
    # regardless of fontSize - left unset, a 20pt title's glyphs overflow a
    # 12pt-tall line box and visually collide with the paragraph after it.
    textColor=colors.HexColor(BRAND_INK),
    spaceAfter=6,
    alignment=TA_CENTER,
)
_SUBTITLE_STYLE = ParagraphStyle(
    "PlanSubtitle",
    fontName="Helvetica",
    fontSize=11,
    leading=15,
    textColor=colors.HexColor(BRAND_MUTED),
    spaceAfter=3,
    alignment=TA_CENTER,
)
_WEEK_HEADING_STYLE = ParagraphStyle(
    "WeekHeading", fontName="Helvetica-Bold", fontSize=12, textColor=colors.white, leading=16
)
_WEEK_TITLE_STYLE = ParagraphStyle(
    "WeekTitle", fontName="Helvetica-Bold", fontSize=12, leading=16, textColor=colors.HexColor(BRAND_INK), spaceAfter=3
)
_WEEK_BODY_STYLE = ParagraphStyle(
    "WeekBody", fontName="Helvetica", fontSize=10, textColor=colors.HexColor(BRAND_INK), leading=14
)


class PlanPdfService:
    """Renders the WebNest Studio branded, week-by-week build-plan document.
    Stateless - never accepts budget/pricing data, so it structurally cannot
    leak pricing into the PDF regardless of what's passed in."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def render_plan_pdf(
        self, project_type: str, total_weeks: int, weeks: list[dict], contact_name: str | None
    ) -> bytes:
        buffer = BytesIO()
        doc = SimpleDocTemplate(
            buffer,
            pagesize=A4,
            topMargin=20 * mm,
            bottomMargin=18 * mm,
            leftMargin=20 * mm,
            rightMargin=20 * mm,
            title=f"WebNest Studio - {project_type} Build Plan",
        )

        story = []
        if _logo_is_valid():
            logo = Image(str(_LOGO_PATH), width=24 * mm, height=24 * mm)
            logo.hAlign = "CENTER"
            story.append(logo)
            story.append(Spacer(1, 10))

        story.append(Paragraph("WebNest Studio", _TITLE_STYLE))
        story.append(Paragraph("Business &amp; Timeline Plan", _SUBTITLE_STYLE))
        story.append(Paragraph(escape(project_type), _SUBTITLE_STYLE))
        generated_for = f"Prepared for {escape(contact_name)}" if contact_name else "Prepared for you"
        generated_at = datetime.now(timezone.utc).strftime("%B %d, %Y")
        story.append(Paragraph(f"{generated_for} &middot; {generated_at} &middot; {total_weeks} weeks", _SUBTITLE_STYLE))
        story.append(Spacer(1, 24))

        for week in weeks:
            header_table = Table(
                [[Paragraph(escape(week["week_range"]), _WEEK_HEADING_STYLE)]],
                colWidths=[170 * mm],
            )
            header_table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(BRAND_PURPLE)),
                        ("LEFTPADDING", (0, 0), (-1, -1), 10),
                        ("TOPPADDING", (0, 0), (-1, -1), 6),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                    ]
                )
            )
            story.append(header_table)
            story.append(Spacer(1, 6))
            story.append(Paragraph(escape(week["title"]), _WEEK_TITLE_STYLE))
            story.append(Paragraph(escape(week["description"]), _WEEK_BODY_STYLE))
            story.append(Spacer(1, 16))

        def _footer(canvas, _doc) -> None:
            canvas.saveState()
            canvas.setFont("Helvetica", 8)
            canvas.setFillColor(colors.HexColor(BRAND_MUTED))
            canvas.drawCentredString(A4[0] / 2, 12 * mm, "webneststudio.co.in")
            canvas.restoreState()

        doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
        return buffer.getvalue()

    def render_plan_html_summary(self, project_type: str, total_weeks: int, weeks: list[dict]) -> str:
        rows = "".join(
            f"""
            <tr>
              <td style="padding:12px 16px;border-bottom:1px solid #ece7f7;">
                <div style="font-size:12px;font-weight:700;color:{BRAND_PURPLE};text-transform:uppercase;letter-spacing:0.04em;">{escape(week["week_range"])}</div>
                <div style="font-size:14px;font-weight:700;color:{BRAND_INK};margin-top:2px;">{escape(week["title"])}</div>
                <div style="font-size:13px;color:{BRAND_MUTED};margin-top:4px;line-height:1.5;">{escape(week["description"])}</div>
              </td>
            </tr>"""
            for week in weeks
        )
        return f"""\
<div style="font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;max-width:560px;border:1px solid #ece7f7;border-radius:12px;overflow:hidden;">
  <div style="background:linear-gradient(135deg,{BRAND_PURPLE},#4a0aa8);padding:16px 20px;">
    <span style="font-size:16px;font-weight:700;color:#ffffff;">Business &amp; Timeline Plan</span>
    <div style="font-size:13px;color:#eee6ff;margin-top:2px;">{escape(project_type)} &middot; {total_weeks} weeks</div>
  </div>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{rows}</table>
</div>"""
