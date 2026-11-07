"""Stop outgoing calls (connectors, webhooks, API bots) from reaching private networks when super admins turn that on."""
import ipaddress
import socket
import urllib.parse


class BlockedAddress(ValueError):
    pass


def _private(ip):
    addr = ipaddress.ip_address(ip)
    return addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved or addr.is_multicast or addr.is_unspecified


def check_url(url):
    """Raise BlockedAddress if the platform blocks private networks and ``url`` resolves to one."""
    from .models import get_settings

    if not get_settings().block_private_networks:
        return
    host = urllib.parse.urlsplit(url).hostname or ""
    if not host:
        raise BlockedAddress("The URL has no host.")
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return  # unresolvable: the request itself will fail
    for info in infos:
        if _private(info[4][0]):
            raise BlockedAddress(f"Calls to private network addresses ({host}) are blocked by the platform administrator.")
