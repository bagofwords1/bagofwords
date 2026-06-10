"""Small shared bits for transactional emails so invite/welcome look consistent."""


def cta_button(url: str, label: str) -> str:
    """A single dark CTA button (matches the member-added email style)."""
    return (
        f'<a href="{url}" '
        f'style="display:inline-block;padding:10px 18px;background:#111827;color:#ffffff;'
        f'text-decoration:none;border-radius:6px;font-weight:600;">{label}</a>'
    )
