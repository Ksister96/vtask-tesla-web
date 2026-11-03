"""Trip address cache validation and coordinate-based repair helpers."""
import re


def needs_refresh(value):
    """Return True for missing, placeholder, coordinate-only or district-only labels."""
    text = str(value or "").strip()
    if not text or "未知" in text:
        return True
    if re.search(r"\d{1,3}\.\d{3,}", text):
        return True
    return bool(re.fullmatch(r"[^市县区]{0,3}[市县区]", text))


def refresh_addresses(detail, resolver):
    """Repair bad start/end labels from [lat, lon] coordinates; return changed keys."""
    changed = []
    for address_key, coord_key in (("start_addr", "start_coord"),
                                   ("end_addr", "end_coord")):
        if not needs_refresh(detail.get(address_key)):
            continue
        coord = detail.get(coord_key)
        if not isinstance(coord, (list, tuple)) or len(coord) < 2:
            continue
        try:
            lat, lon = float(coord[0]), float(coord[1])
            resolved = str(resolver(lat, lon) or "").strip()
        except (TypeError, ValueError, OverflowError):
            continue
        except Exception:
            continue
        if resolved and not needs_refresh(resolved):
            detail[address_key] = resolved
            changed.append(address_key)
    return changed
