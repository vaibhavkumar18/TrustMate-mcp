"""Seller identity normalization shared by all trust evidence providers.

Channel3's affiliate purchase URL is deliberately NOT used as the seller URL.
Callers must supply the merchant domain (from the normalized offer seller) or
an independently supplied merchant URL. Public-host validation is performed
before a URL is used by any evidence provider.
"""

import ipaddress
import socket
from urllib.parse import urlsplit

from publicsuffix2 import get_sld

from app.domain.models import SellerIdentity


_AFFILIATE_HOSTS = {"buy.trychannel3.com"}
_INTERNAL_HOSTS = {"localhost", "metadata.google.internal"}


def normalize_domain(value: str | None) -> str | None:
    """Return a normalized public-looking hostname, or ``None``.

    This function performs syntax/IDNA validation only. Public-IP/DNS
    validation for an actual URL is performed by ``normalize_seller``.
    """
    if not isinstance(value, str) or not value.strip():
        return None

    raw = value.strip().rstrip(".")
    try:
        if "://" in raw:
            parsed = urlsplit(raw)
            if parsed.username or parsed.password:
                return None
            raw = parsed.hostname or ""
        else:
            # A bare host may include a conventional port, but not a path or
            # query. A malformed port is rejected by the hostname validation.
            if any(marker in raw for marker in ("/", "?", "#", "@")):
                return None
            if raw.count(":") == 1 and not raw.startswith("["):
                host, port = raw.rsplit(":", 1)
                if port.isdigit():
                    raw = host
    except ValueError:
        return None

    raw = raw.strip().strip("[]").rstrip(".").lower()
    if raw.startswith("www."):
        raw = raw[4:]
    if not raw or raw in _INTERNAL_HOSTS or raw.endswith(".localhost"):
        return None

    try:
        ip = ipaddress.ip_address(raw)
        return str(ip) if ip.is_global else None
    except ValueError:
        pass

    try:
        ascii_host = raw.encode("idna").decode("ascii")
    except UnicodeError:
        return None

    if len(ascii_host) > 253 or "." not in ascii_host:
        return None
    labels = ascii_host.split(".")
    if any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or not all(char.isalnum() or char == "-" for char in label)
        for label in labels
    ):
        return None
    return ascii_host


def registrable_domain(value: str | None) -> str | None:
    """Return the public-suffix-aware registrable domain (eTLD+1)."""
    host = normalize_domain(value)
    if not host:
        return None
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass

    # .example is reserved for documentation and synthetic test/demo sellers.
    if host.endswith(".example"):
        return host
    try:
        result = get_sld(host, strict=True)
    except TypeError:  # Compatibility with older publicsuffix2 releases.
        result = get_sld(host)
    except Exception:
        result = None
    return result.lower() if isinstance(result, str) and result else None


def same_registered_domain(left: str | None, right: str | None) -> bool:
    """Compare registrable domains, never unsafe substring matches."""
    left_domain = registrable_domain(left)
    right_domain = registrable_domain(right)
    return bool(left_domain and right_domain and left_domain == right_domain)


def merchant_homepage_url(domain: str | None) -> str | None:
    """Build the canonical merchant origin from an independently known domain."""
    host = normalize_domain(domain)
    if not host:
        return None
    return f"https://{host}/"


def business_lookup_name(seller_name: str, domain: str | None) -> str:
    """Prefer a human merchant name; derive a query label only for domain names.

    Channel3 sometimes supplies ``seller_name='walmart.com'`` and sometimes a
    display name such as ``Walmart``. Replacing a real display name with a
    domain fragment needlessly weakens place matching, so derive a label only
    when the supplied seller name itself looks like a hostname/URL.
    """
    supplied_name = seller_name.strip() if isinstance(seller_name, str) else ""
    if supplied_name and normalize_domain(supplied_name) is None:
        return supplied_name

    host = normalize_domain(domain) or normalize_domain(supplied_name)
    if host:
        try:
            base = get_sld(host, strict=True) or host
        except TypeError:
            base = get_sld(host) or host
        except Exception:
            base = host
        label = base.split(".", 1)[0]
        if label:
            return label.replace("-", " ").strip()
    return supplied_name


def _is_public_host(host: str, *, allow_synthetic: bool = False) -> bool:
    normalized = normalize_domain(host)
    if not normalized:
        return bool(allow_synthetic and isinstance(host, str) and host.lower().endswith(".example"))
    if allow_synthetic and normalized.endswith(".example"):
        return True
    try:
        return ipaddress.ip_address(normalized).is_global
    except ValueError:
        pass

    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(normalized, 443, type=socket.SOCK_STREAM)
        }
    except (socket.gaierror, OSError):
        return False
    if not addresses:
        return False

    # Fail closed if even one returned A/AAAA address is not globally routable.
    for address in addresses:
        try:
            if not ipaddress.ip_address(address).is_global:
                return False
        except ValueError:
            return False
    return True


def normalize_seller(
    seller_name: str,
    seller_url: str | None,
    *,
    allow_synthetic: bool = False,
) -> SellerIdentity:
    """Normalize a merchant URL without trusting a Channel3 buy URL.

    A malformed, non-HTTPS, credential-bearing, non-public, or non-standard
    port URL raises ``ValueError``. Callers should convert that into an
    unverified/name-only assessment rather than attempting provider requests.
    """
    if not isinstance(seller_name, str) or not seller_name.strip():
        raise ValueError("seller name is required")

    seller_name = seller_name.strip()
    if seller_url:
        if not isinstance(seller_url, str):
            raise ValueError("seller URL must be a string")
        try:
            supplied = urlsplit(seller_url)
        except ValueError as exc:
            raise ValueError("seller URL is malformed") from exc
        supplied_host = (supplied.hostname or "").lower().rstrip(".")
        if supplied_host in _AFFILIATE_HOSTS:
            # The Channel3 URL identifies a redirect service, not the seller.
            seller_url = None

    if not seller_url:
        possible_domain = normalize_domain(seller_name)
        if possible_domain:
            # A host-shaped seller label is still an outbound provider target.
            # Apply the same fail-closed public DNS/IP validation used for an
            # explicit seller URL before deriving a canonical HTTPS origin.
            if not _is_public_host(
                possible_domain,
                allow_synthetic=allow_synthetic,
            ):
                raise ValueError("seller hostname is not publicly reachable")
            root = registrable_domain(possible_domain) or possible_domain
            return SellerIdentity(
                display_name=seller_name,
                seller_key=f"domain:{root}",
                normalized_domain=possible_domain,
                canonical_url=merchant_homepage_url(possible_domain),
                identity_status="domain_matched",
            )
        key = f"name:{seller_name.casefold().strip()}"
        return SellerIdentity(
            display_name=seller_name,
            seller_key=key,
            identity_status="name_only",
        )

    try:
        parsed = urlsplit(seller_url)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("seller URL contains an invalid hostname or port") from exc

    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or port not in (None, 443)
        or not _is_public_host(parsed.hostname, allow_synthetic=allow_synthetic)
    ):
        raise ValueError("seller URL must use a public HTTPS merchant host")

    host = normalize_domain(parsed.hostname)
    if not host:
        raise ValueError("seller URL has an invalid merchant hostname")
    root = registrable_domain(host) or host
    canonical = merchant_homepage_url(host)
    return SellerIdentity(
        display_name=seller_name,
        seller_key=f"domain:{root}",
        normalized_domain=host,
        canonical_url=canonical,
        original_url=seller_url,
        identity_status="domain_matched",
    )
