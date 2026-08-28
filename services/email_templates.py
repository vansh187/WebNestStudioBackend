from html import escape

LOGO_URL = "https://webneststudiobackend-n00h.onrender.com/assets/logo.png"
BRAND_PURPLE = "#7e14ff"
BRAND_INK = "#1a1230"
BRAND_MUTED = "#6b6480"
OTP_GREEN = "#16a34a"
OTP_GREEN_BG = "#e9faf0"


def render_otp_email_html(otp_code: str, purpose: str, expire_minutes: int) -> str:
    """Branded HTML for the OTP verification email: three attention-grabbing
    lines about WebNest Studio, a verification prompt, the code itself
    highlighted in green, and the brand mark underneath. Built with a
    table-based layout and inline styles throughout (no <style> block, no
    flexbox/grid) since that's what actually renders consistently across
    Gmail, Apple Mail, and Outlook's desktop rendering engine."""
    safe_code = escape(otp_code)
    action = escape(purpose.replace("_", " ")) or "verification"

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Verify your email - WebNest Studio</title>
</head>
<body style="margin:0;padding:0;background-color:#f4f2fa;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#f4f2fa;padding:32px 16px;">
  <tr>
    <td align="center">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:520px;background-color:#ffffff;border-radius:16px;overflow:hidden;border:1px solid #ece7f7;">

        <tr>
          <td style="background:linear-gradient(135deg,{BRAND_PURPLE},#4a0aa8);padding:28px 32px;">
            <span style="font-size:20px;font-weight:700;color:#ffffff;letter-spacing:-0.02em;">WebNest Studio</span>
          </td>
        </tr>

        <tr>
          <td style="padding:36px 32px 8px 32px;">
            <p style="margin:0 0 16px 0;font-size:15px;line-height:1.65;color:{BRAND_INK};">
              WebNest Studio designs and engineers award-worthy websites, AI-powered products, and enterprise-grade software that make ambitious brands impossible to ignore.
            </p>
            <p style="margin:0 0 16px 0;font-size:15px;line-height:1.65;color:{BRAND_INK};">
              From idea to launch, our team pairs award-winning design with rock-solid engineering &mdash; React, Java, Python, Spring Boot, and AI, all under one roof.
            </p>
            <p style="margin:0 0 24px 0;font-size:15px;line-height:1.65;color:{BRAND_INK};">
              You're moments away from joining the founders and teams who trust WebNest Studio to build what's next.
            </p>
            <p style="margin:0 0 24px 0;font-size:15px;line-height:1.6;color:{BRAND_INK};">
              Please verify your email address to complete your {action}:
            </p>
          </td>
        </tr>

        <tr>
          <td style="padding:0 32px 8px 32px;" align="center">
            <table role="presentation" cellpadding="0" cellspacing="0" style="background-color:{OTP_GREEN_BG};border:1.5px solid {OTP_GREEN};border-radius:12px;">
              <tr>
                <td style="padding:18px 40px;">
                  <span style="display:block;font-size:34px;font-weight:800;letter-spacing:0.35em;color:{OTP_GREEN};font-family:'Courier New',monospace;">{safe_code}</span>
                </td>
              </tr>
            </table>
          </td>
        </tr>

        <tr>
          <td style="padding:16px 32px 8px 32px;" align="center">
            <p style="margin:0;font-size:13px;line-height:1.6;color:{BRAND_MUTED};">
              This code expires in {expire_minutes} minutes. If you didn't request this, you can safely ignore this email.
            </p>
          </td>
        </tr>

        <tr>
          <td style="padding:28px 32px 8px 32px;" align="center">
            <img src="{LOGO_URL}" width="72" height="72" alt="WebNest Studio" style="display:block;border-radius:50%;border:0;outline:none;">
          </td>
        </tr>

        <tr>
          <td style="padding:12px 32px 28px 32px;" align="center">
            <p style="margin:0;font-size:12px;color:{BRAND_MUTED};">
              &copy; WebNest Studio &middot; webneststudio.co.in
            </p>
          </td>
        </tr>

      </table>
    </td>
  </tr>
</table>
</body>
</html>
"""


def render_plan_email_html(project_type: str) -> str:
    """Branded HTML for the "your project plan is attached" email - same
    table-based/inline-style layout as render_otp_email_html above."""
    safe_project_type = escape(project_type)

    return f"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Your project plan - WebNest Studio</title>
</head>
<body style="margin:0;padding:0;background-color:#f4f2fa;font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color:#f4f2fa;padding:32px 16px;">
  <tr>
    <td align="center">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:520px;background-color:#ffffff;border-radius:16px;overflow:hidden;border:1px solid #ece7f7;">

        <tr>
          <td style="background:linear-gradient(135deg,{BRAND_PURPLE},#4a0aa8);padding:28px 32px;">
            <span style="font-size:20px;font-weight:700;color:#ffffff;letter-spacing:-0.02em;">WebNest Studio</span>
          </td>
        </tr>

        <tr>
          <td style="padding:36px 32px 8px 32px;">
            <p style="margin:0 0 16px 0;font-size:15px;line-height:1.65;color:{BRAND_INK};">
              Thanks for talking through your project with us. Attached is your week-by-week build plan for your {safe_project_type}.
            </p>
            <p style="margin:0 0 24px 0;font-size:15px;line-height:1.65;color:{BRAND_INK};">
              Have questions or want to adjust anything? Just reply to this email and our team will follow up.
            </p>
          </td>
        </tr>

        <tr>
          <td style="padding:8px 32px 28px 32px;" align="center">
            <img src="{LOGO_URL}" width="72" height="72" alt="WebNest Studio" style="display:block;border-radius:50%;border:0;outline:none;">
          </td>
        </tr>

        <tr>
          <td style="padding:12px 32px 28px 32px;" align="center">
            <p style="margin:0;font-size:12px;color:{BRAND_MUTED};">
              &copy; WebNest Studio &middot; webneststudio.co.in
            </p>
          </td>
        </tr>

      </table>
    </td>
  </tr>
</table>
</body>
</html>
"""
