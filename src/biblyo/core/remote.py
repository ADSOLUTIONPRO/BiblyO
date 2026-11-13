# SPDX-License-Identifier: EUPL-1.2
"""Image désignée par une adresse saisie : téléchargée par le serveur, jamais depuis le réseau interne.

Une adresse fournie par une personne ne doit atteindre ni la machine, ni le réseau local, ni les services Docker.
Chaque étape est contrôlée : HTTPS sur le port 443, un nom d'hôte et non une adresse IP, toutes les adresses du nom
publiques. La connexion se fait à l'adresse vérifiée, sans seconde résolution (aucun changement de réponse DNS entre
le contrôle et l'appel), le certificat restant vérifié pour le nom. Les redirections sont suivies une à une, chacune
contrôlée de la même façon. La taille et la durée sont bornées ; les mandataires de l'environnement sont ignorés.

Le contenu n'est pas jugé ici : `core.media` le décode et le refuse s'il n'est pas une image.
"""

from __future__ import annotations

import ipaddress
import socket
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

from biblyo.core.errors import BusinessError
from biblyo.core.i18n import _
from biblyo.core.media import MAX_UPLOAD_BYTES

MAX_URL_LENGTH = 2048
MAX_REDIRECTS = 3
CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 10.0
TOTAL_TIMEOUT = 20.0
CHUNK = 64 * 1024
REDIRECTS = (301, 302, 303, 307, 308)
USER_AGENT = "biblyO/0.1 (logiciel libre de bibliotheque ; image ajoutee par un bibliothecaire)"

Resolver = Callable[[str], Iterable[str]]


class RemoteImageError(BusinessError):
    """Adresse refusée ou téléchargement impossible : le message s'adresse à la personne qui l'a saisie."""


@dataclass(frozen=True)
class RemoteImage:
    body: bytes
    url: str  # adresse finale, après les redirections
    content_type: str


def system_resolver(host: str) -> list[str]:
    try:
        return sorted({str(info[4][0]) for info in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)})
    except OSError as exc:
        raise RemoteImageError(_("Ce nom de serveur est introuvable.")) from exc


def is_public(address: str) -> bool:
    """Adresse joignable sur Internet : ni locale, ni privée, ni réservée, ni lien local, ni partagée, ni multicast."""
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        parsed = parsed.ipv4_mapped
    return parsed.is_global and not parsed.is_multicast and not parsed.is_reserved


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def check(url: str, resolver: Resolver) -> tuple[str, str]:
    """Contrôle une adresse ; rend le nom d'hôte et l'adresse IP publique à laquelle se connecter."""
    if len(url) > MAX_URL_LENGTH:
        raise RemoteImageError(_("Cette adresse est trop longue."))
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError as exc:
        raise RemoteImageError(_("Cette adresse n’est pas valide.")) from exc
    host = (parts.hostname or "").casefold().rstrip(".")
    if parts.scheme != "https":
        raise RemoteImageError(_("Seules les adresses en https:// sont acceptées."))
    if not host or parts.username or parts.password or port not in (None, 443):
        raise RemoteImageError(_("Cette adresse n’est pas acceptée : nom de serveur seul, sans identifiant ni port."))
    if _is_ip_literal(host) or "." not in host:
        raise RemoteImageError(_("Indiquez le nom d’un site public, pas une adresse IP ni un nom local."))
    try:
        host.encode("idna")
    except UnicodeError as exc:
        raise RemoteImageError(_("Ce nom de serveur n’est pas valide.")) from exc
    addresses = list(resolver(host))
    if not addresses:
        raise RemoteImageError(_("Ce nom de serveur est introuvable."))
    if not all(is_public(address) for address in addresses):
        raise RemoteImageError(_("Cette adresse mène à un réseau privé ou local : elle est refusée."))
    ipv4 = [a for a in addresses if ":" not in a]
    return host, (ipv4 or addresses)[0]


def _pinned(url: str, address: str) -> str:
    parts = urlsplit(url)
    netloc = f"[{address}]" if ":" in address else address
    query = f"?{parts.query}" if parts.query else ""
    return f"https://{netloc}{parts.path or '/'}{query}"


def fetch_image(
    url: str,
    *,
    resolver: Resolver = system_resolver,
    client: httpx.Client | None = None,
    clock: Callable[[], float] = time.monotonic,
    max_bytes: int = MAX_UPLOAD_BYTES,
) -> RemoteImage:
    """Télécharge l'image d'une adresse saisie, en suivant au plus trois redirections contrôlées."""
    own = client is None
    http = client or httpx.Client(
        follow_redirects=False,
        trust_env=False,
        headers={"User-Agent": USER_AGENT, "Accept": "image/jpeg, image/png, image/webp;q=0.9, */*;q=0.1"},
        timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT),
    )
    deadline = clock() + TOTAL_TIMEOUT
    current = url.strip()
    try:
        for _hop in range(MAX_REDIRECTS + 1):
            host, address = check(current, resolver)
            try:
                with http.stream(
                    "GET",
                    _pinned(current, address),
                    headers={"Host": host},
                    extensions={"sni_hostname": host},
                ) as response:
                    if response.status_code in REDIRECTS:
                        location = response.headers.get("Location", "")
                        if not location:
                            raise RemoteImageError(_("Le serveur renvoie ailleurs sans dire où."))
                        current = urljoin(current, location)
                        continue
                    if response.status_code != 200:
                        raise RemoteImageError(
                            _("Le serveur a répondu {status} : aucune image n’a été récupérée.").format(
                                status=response.status_code
                            )
                        )
                    declared = response.headers.get("Content-Length", "")
                    if declared.isdigit() and int(declared) > max_bytes:
                        raise RemoteImageError(_("Cette image dépasse 8 Mio."))
                    chunks: list[bytes] = []
                    size = 0
                    for chunk in response.iter_bytes(CHUNK):
                        size += len(chunk)
                        if size > max_bytes:
                            raise RemoteImageError(_("Cette image dépasse 8 Mio."))
                        if clock() > deadline:
                            raise RemoteImageError(_("Le serveur est trop lent : téléchargement interrompu."))
                        chunks.append(chunk)
                    return RemoteImage(b"".join(chunks), current, response.headers.get("Content-Type", ""))
            except httpx.TimeoutException as exc:
                raise RemoteImageError(_("Le serveur est trop lent : téléchargement interrompu.")) from exc
            except httpx.HTTPError as exc:
                raise RemoteImageError(_("Ce serveur est injoignable ou refuse la connexion sécurisée.")) from exc
            if clock() > deadline:
                raise RemoteImageError(_("Le serveur est trop lent : téléchargement interrompu."))
        raise RemoteImageError(_("Trop de redirections : l’image n’a pas été récupérée."))
    finally:
        if own:
            http.close()
